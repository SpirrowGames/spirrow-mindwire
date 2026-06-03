"""Tests for the deterministic design-thread context bundle (ADR-17 N-5/N-6)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from spirrow_mindwire.naysayer.context_bundle import (
    build_context_bundle,
    extract_references,
    parse_adr_index,
)


class _FakeMcp:
    """Returns a fixed ``chatroom_get_thread`` result; records calls."""

    def __init__(self, thread_result: dict[str, Any]) -> None:
        self._result = thread_result
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((name, arguments))
        return self._result


def _thread(
    messages: list[dict[str, Any]], *, title: str = "T", thread_id: str = "T-x"
) -> dict[str, Any]:
    return {"thread": {"thread_id": thread_id, "title": title}, "messages": messages}


def _repo(tmp_path: Path) -> Path:
    """A minimal in-repo layout: a §M ADR index + one referenced spec doc."""
    (tmp_path / "CLAUDE.md").write_text(
        "## §M\n"
        "| ADR | 内容 | thread |\n"
        "|---|---|---|\n"
        "| ADR-2026-05-27-09 (T28) | identity 4 layers | T-t28 |\n"
        "| ADR-2026-05-31-15 | independence gradation | T-t15 |\n",
        encoding="utf-8",
    )
    (tmp_path / "spec").mkdir()
    (tmp_path / "spec" / "FOO.md").write_text("spec foo body", encoding="utf-8")
    return tmp_path


def test_extract_references_complete_sorted_deduped() -> None:
    text = (
        "see ADR-2026-06-03-17 and ADR-2026-05-31-15, also spec/FOO.md "
        "and docs/bar.md; again ADR-2026-06-03-17 / spec/FOO.md"
    )
    adrs, docs = extract_references(text)
    assert adrs == ("ADR-2026-05-31-15", "ADR-2026-06-03-17")  # sorted, deduped
    assert docs == ("docs/bar.md", "spec/FOO.md")


def test_parse_adr_index(tmp_path: Path) -> None:
    claude = _repo(tmp_path) / "CLAUDE.md"
    rows = parse_adr_index(claude.read_text(encoding="utf-8"))
    ids = [r[0] for r in rows]
    assert ids == ["ADR-2026-05-27-09", "ADR-2026-05-31-15"]  # sorted
    assert dict(rows)["ADR-2026-05-31-15"] == "independence gradation"


@pytest.mark.anyio
async def test_build_bundle_is_deterministic(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    msgs = [
        {
            "msg_id": "msg-1",
            "author": "Bohr",
            "type": "propose",
            "content": "refs ADR-2026-05-31-15",
        },
        {"msg_id": "msg-2", "author": "Heisenberg", "type": "answer", "content": "see spec/FOO.md"},
    ]
    b1 = await build_context_bundle(
        _FakeMcp(_thread(msgs)), project="p", thread_id="T-x", repo_root=root
    )
    b2 = await build_context_bundle(
        _FakeMcp(_thread(msgs)), project="p", thread_id="T-x", repo_root=root
    )
    assert b1.text == b2.text
    assert b1.manifest == b2.manifest


@pytest.mark.anyio
async def test_structural_kept_full_others_truncated(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    long_body = "x" * 9000
    msgs = [
        {"msg_id": "msg-1", "author": "a", "type": "report", "content": long_body},  # capped
        {
            "msg_id": "msg-2",
            "author": "a",
            "type": "decide",
            "content": long_body,
        },  # structural → full
    ]
    bundle = await build_context_bundle(
        _FakeMcp(_thread(msgs)), project="p", thread_id="T-x", repo_root=root
    )
    assert bundle.manifest.truncated_msg_ids == ("msg-1",)
    assert "…[truncated" in bundle.text
    assert bundle.text.count(long_body) == 1  # only the decide msg kept whole


@pytest.mark.anyio
async def test_decision_msg_kept_full(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    long_body = "y" * 9000
    msgs = [{"msg_id": "msg-1", "author": "a", "type": "report", "content": long_body}]
    bundle = await build_context_bundle(
        _FakeMcp(_thread(msgs)),
        project="p",
        thread_id="T-x",
        decision_msg_id="msg-1",
        repo_root=root,
    )
    assert bundle.manifest.truncated_msg_ids == ()  # decision msg is never capped


@pytest.mark.anyio
async def test_references_survive_truncation_and_missing_docs_recorded(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    # A ref buried past the per-message cap, plus a referenced doc that is absent.
    buried = "z" * 8000 + " ADR-2026-06-03-16 spec/NOPE.md"
    msgs = [{"msg_id": "msg-1", "author": "a", "type": "report", "content": buried}]
    bundle = await build_context_bundle(
        _FakeMcp(_thread(msgs)), project="p", thread_id="T-x", repo_root=root
    )
    # extraction runs over the FULL text, so the buried ref is still captured
    assert "ADR-2026-06-03-16" in bundle.manifest.referenced_adrs
    assert "spec/NOPE.md" in bundle.manifest.missing_docs
    assert "spec/NOPE.md" not in bundle.manifest.inlined_docs


@pytest.mark.anyio
async def test_gather_calls_get_thread_full(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    fake = _FakeMcp(_thread([]))
    await build_context_bundle(fake, project="p", thread_id="T-x", repo_root=root)
    assert fake.calls == [
        ("chatroom_get_thread", {"project": "p", "thread_id": "T-x", "mode": "full"})
    ]
