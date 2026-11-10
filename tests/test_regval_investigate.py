"""Output-contract tests for scripts/regval_investigate.py (T-registry-values-in-pushed-history).

The script handles secret values; these tests pin the property the thread asked for (§0-b):
nothing but allow-listed, shape-checked fields can reach stdout, and stderr / exception text
never does.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

FAKE_SECRET = "s3cr3t-VALUE/with spaces:and=symbols"


def _load() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "scripts" / "regval_investigate.py"
    spec = importlib.util.spec_from_file_location("regval_investigate", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rv = _load()


@pytest.mark.parametrize(
    ("kind", "value"),
    [
        ("sha", FAKE_SECRET),
        ("sha", "abc1234"),  # short sha is not the allowed shape
        ("repo", FAKE_SECRET),
        ("path", FAKE_SECRET),
        ("path", "docs/a b.md"),
        ("item", "{{" + "X" + "}}"),
        ("int", "12a"),
        ("time", "2026-11-10 06:00"),
        ("tag", FAKE_SECRET),
        ("no-such-shape", "anything"),
    ],
)
def test_safe_redacts_anything_off_shape(kind: str, value: str) -> None:
    assert rv.safe(kind, value) == "REDACTED"


def test_safe_passes_allowed_shapes() -> None:
    assert rv.safe("sha", "a" * 40) == "a" * 40
    assert rv.safe("repo", "SpirrowGames/spirrow-tutorail") == "SpirrowGames/spirrow-tutorail"
    assert rv.safe("ints", "1,2,12") == "1,2,12"
    assert rv.safe("time", "2026-10-02T01:28:37+09:00") == "2026-10-02T01:28:37+09:00"


def test_emit_never_prints_an_off_shape_field(capsys: pytest.CaptureFixture[str]) -> None:
    rv.emit(rv.T("ROW"), ("path", FAKE_SECRET), ("item", FAKE_SECRET))
    out = capsys.readouterr().out
    assert FAKE_SECRET not in out
    assert out == "ROW REDACTED REDACTED\n"


def test_reflog_parser_drops_the_message(tmp_path: Path) -> None:
    new = "b" * 40
    log = tmp_path / "HEAD"
    log.write_text(
        f"{'a' * 40} {new} Someone <x@example.com> 1760000000 +0900\tcommit: {FAKE_SECRET}\n"
        f"{new} {'c' * 40} Someone <x@example.com> 1760000100 +0900\tfetch: origin\n",
        encoding="utf-8",
    )
    entries = rv.reflog_entries(str(log))
    assert [(e[1], e[2]) for e in entries] == [(new, "commit"), ("c" * 40, "fetch")]
    assert all(FAKE_SECRET not in repr(e) for e in entries)


@pytest.mark.parametrize(
    ("op", "cls"),
    [
        ("commit", "create"),
        ("commit (amend)", "create"),
        ("cherry-pick", "create"),
        ("rebase (finish)", "create"),
        ("fetch", "import"),
        ("pull", "import"),
        ("checkout", "import"),
        ("reset", "import"),
        ("merge origin/main", "import"),
        ("weird", "other"),
    ],
)
def test_reflog_op_split_create_vs_import(op: str, cls: str) -> None:
    assert rv.op_class(op) == cls


def test_item_of_separates_registry_values_from_patterns() -> None:
    assert rv.item_of("registered as {{PATH_DOCS}}") == ("value", "PATH_DOCS")
    assert rv.item_of("ipv4") == ("pattern", "ipv4")


def test_commit_fetchable_keeps_only_http_status(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rv, "run3", lambda argv: (1, "", f"gh: {FAKE_SECRET} (HTTP 404)"))
    assert rv.commit_fetchable("o/r", "a" * 40) == "no404"
    monkeypatch.setattr(rv, "run3", lambda argv: (1, "", FAKE_SECRET))
    assert rv.commit_fetchable("o/r", "a" * 40) == "rc1"


def test_commit_lookup_asks_for_sha_only(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def fake(argv: list[str]) -> tuple[int, str, str]:
        seen.append(argv)
        return 0, "a" * 40 + "\n", ""

    monkeypatch.setattr(rv, "run3", fake)
    assert rv.commit_fetchable("o/r", "a" * 40) == "yes"
    assert seen[0][-2:] == ["--jq", ".sha"]


def test_main_reports_exception_class_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    def boom(_a: object) -> None:
        raise RuntimeError(FAKE_SECRET)

    monkeypatch.setattr(rv, "cmd_table", boom)
    saved = sys.stderr
    try:
        rc = rv.main(["table", "--state", str(tmp_path / "s.json")])
    finally:
        sys.stderr = saved
    out = capsys.readouterr().out
    assert rc == 2
    assert out == "ERR table RuntimeError\n"
