"""Real-filesystem tests for :class:`spirrow_mindwire.watcher.observer.WatcherObserver`.

These exercise the watchdog → asyncio bridge end-to-end on the local
filesystem. They're light on assertions and heavy on timing — the
observer thread runs in the background, so we use ``asyncio.wait_for``
with a generous timeout to avoid CI flake.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from factories import write_message_file

from spirrow_mindwire.filesystem import ThreadDirLayout
from spirrow_mindwire.watcher.events import ThreadEvent
from spirrow_mindwire.watcher.observer import WatcherObserver

ULID_A = "01ARZ3NDEKTSV4RRFFQ69G5FAV"

EVENT_TIMEOUT = 5.0
"""Seconds to wait for the observer thread to surface an event.

Sized for inotify (Linux); macOS FSEvents and Windows
ReadDirectoryChangesW are not exercised here. Cross-platform support
needs separate manual verification.
"""


async def _assert_no_event(queue: asyncio.Queue[ThreadEvent], timeout: float = 0.5) -> None:
    """Assert no event lands on the queue within ``timeout``.

    ``queue.empty()`` is unreliable here: ``call_soon_threadsafe``
    schedules ``queue.put_nowait`` from the watchdog thread, but the
    event loop may not have ticked yet when the assertion runs.
    Awaiting ``queue.get()`` with a timeout forces the loop to drain
    pending callbacks first and gives a deterministic verdict.
    """
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(queue.get(), timeout=timeout)


def _layout(base: Path) -> ThreadDirLayout:
    return ThreadDirLayout(base_dir=base, thread_id=ULID_A)


def _write_message_atomically(layout: ThreadDirLayout, seq: int, sender: str) -> None:
    """Write a message via the same .tmp → rename dance the watcher expects.

    Thin wrapper over ``factories.write_message_file`` so the existing
    callers stay terse; the body content is irrelevant to the observer
    (which only cares about file-create events on ``messages/``).
    """
    write_message_file(layout, seq, sender, atomic=True)


@pytest.mark.anyio
async def test_observer_emits_event_for_new_message_file(tmp_path: Path) -> None:
    queue: asyncio.Queue[ThreadEvent] = asyncio.Queue()
    loop = asyncio.get_running_loop()
    threads_root = tmp_path / "threads"
    threads_root.mkdir()

    observer = WatcherObserver(threads_root=threads_root, queue=queue, loop=loop)
    observer.start()
    try:
        await asyncio.sleep(0.1)  # let the observer thread settle
        layout = _layout(tmp_path)
        _write_message_atomically(layout, 1, "claude.ai")

        event = await asyncio.wait_for(queue.get(), timeout=EVENT_TIMEOUT)
    finally:
        observer.stop()

    assert event.thread_id == ULID_A
    assert event.seq == 1
    assert event.path.name == "001-from-cai.md"


@pytest.mark.anyio
async def test_observer_ignores_tmp_files(tmp_path: Path) -> None:
    """The .md.tmp staging file must not produce a ThreadEvent."""
    queue: asyncio.Queue[ThreadEvent] = asyncio.Queue()
    loop = asyncio.get_running_loop()
    threads_root = tmp_path / "threads"
    threads_root.mkdir()
    layout = _layout(tmp_path)
    layout.messages_dir.mkdir(parents=True, exist_ok=True)

    observer = WatcherObserver(threads_root=threads_root, queue=queue, loop=loop)
    observer.start()
    try:
        await asyncio.sleep(0.1)
        target = layout.message_path(1, "claude.ai")
        tmp = target.with_name(target.name + ".tmp")
        # Create the .tmp without renaming — should be ignored entirely.
        tmp.write_text("partial", encoding="utf-8")
        await _assert_no_event(queue)
    finally:
        observer.stop()


@pytest.mark.anyio
async def test_observer_ignores_files_outside_thread_layout(tmp_path: Path) -> None:
    """A bare *.md outside ``threads/<ULID>/messages/`` must be ignored."""
    queue: asyncio.Queue[ThreadEvent] = asyncio.Queue()
    loop = asyncio.get_running_loop()
    threads_root = tmp_path / "threads"
    threads_root.mkdir()

    observer = WatcherObserver(threads_root=threads_root, queue=queue, loop=loop)
    observer.start()
    try:
        await asyncio.sleep(0.1)
        # README at the threads root, not in any thread dir.
        (threads_root / "README.md").write_text("hi", encoding="utf-8")
        await _assert_no_event(queue)
    finally:
        observer.stop()


@pytest.mark.anyio
async def test_observer_distinct_seqs_emit_distinct_events(tmp_path: Path) -> None:
    queue: asyncio.Queue[ThreadEvent] = asyncio.Queue()
    loop = asyncio.get_running_loop()
    threads_root = tmp_path / "threads"
    threads_root.mkdir()
    layout = _layout(tmp_path)

    observer = WatcherObserver(threads_root=threads_root, queue=queue, loop=loop)
    observer.start()
    try:
        await asyncio.sleep(0.1)
        _write_message_atomically(layout, 1, "claude.ai")
        _write_message_atomically(layout, 2, "claude-code")

        seqs: set[int] = set()
        # The observer may emit modify/move events around the rename;
        # collect everything until both seqs are seen or we time out.
        deadline = loop.time() + EVENT_TIMEOUT
        while {1, 2} - seqs:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            event = await asyncio.wait_for(queue.get(), timeout=remaining)
            seqs.add(event.seq)
    finally:
        observer.stop()

    assert {1, 2}.issubset(seqs)
