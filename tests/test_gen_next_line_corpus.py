"""The posting time ``scripts/gen_next_line_corpus.py`` resolves the corpus at (boundary 5-C).

Spec: T-done-disposition-auto-close msg-9237 §4 (item 1). The corpus records the shapes of past
``NEXT:`` lines and keeps no per-line time, so the script states one: a fixed instant before
``handoff.NONE_MALFORMED_AFTER``. Without it, regenerating the corpus from the cutoff on would
record every ``NEXT: none`` line as ``absent``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

from legacy_none_time import PRE_CUTOFF

from spirrow_mindwire.conductor import handoff as handoff_mod

_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "gen_next_line_corpus.py"


def _load_script() -> Any:
    spec = importlib.util.spec_from_file_location("_gen_next_line_corpus_test_module", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_SCRIPT = _load_script()


def test_the_corpus_time_is_the_time_the_corpus_tests_replay_at() -> None:
    assert _SCRIPT.CORPUS_POSTED_AT == PRE_CUTOFF


def test_the_corpus_time_is_before_the_shipped_cutoff() -> None:
    cutoff = handoff_mod.NONE_MALFORMED_AFTER
    assert cutoff is not None
    assert cutoff > _SCRIPT.CORPUS_POSTED_AT


def test_a_none_line_is_recorded_as_none_with_the_shipped_cutoff() -> None:
    # The shipped cutoff is in force here (nothing is patched). Read without a posting time the
    # same line is refused, which is what the script recorded before it passed one.
    assert _SCRIPT.expectation("NEXT: none") == ("none", "none")
    unstamped = handoff_mod.resolve_handoff("NEXT: none", _SCRIPT.ROSTER)
    assert unstamped.malformed is handoff_mod.MalformedHandoff.LEGACY_NONE_AFTER_CUTOFF


def test_other_lines_resolve_as_before() -> None:
    assert _SCRIPT.expectation("NEXT: Bohr") == ("role", "Bohr")
    assert _SCRIPT.expectation("NEXT: human") == ("human", "human")
