"""Output-contract tests for scripts/regval_investigate.py (T-registry-values-in-pushed-history).

The script handles secret values; these tests pin the property the thread asked for (§0-b):
nothing but allow-listed, shape-checked fields can reach stdout, and stderr / exception text
never does.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

FAKE_SECRET = "-".join(["dummy", "noise", "y" * 5]) + " with spaces:and=symbols"
DUMMY_WORD = "".join(["Dummy", "Word", "0" * 6])  # passes every allow-listed word shape
CFG = ["--sandbox", "sb", "--docs-root", "docs"]


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
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    def boom(_a: object) -> None:
        raise RuntimeError(FAKE_SECRET)

    monkeypatch.setattr(rv, "cmd_table", boom)
    rc = rv.main(["table", "--state", str(tmp_path / "s.json"), *CFG])
    cap = capfd.readouterr()
    assert rc == 2
    assert cap.out == "ERR table RuntimeError\n"
    assert cap.err == ""


# --- 改訂 4 §8: no mutable state of the original clones -----------------------------------

SHA = "d" * 40


@pytest.mark.parametrize(
    "args",
    [
        ("log", "HEAD"),
        ("log", "origin/main"),
        ("log", "--remotes=origin"),
        ("log", "--all"),
        ("log", "-g", SHA),
        ("log", "--walk-reflogs", SHA),
        ("show", "HEAD:docs/x.md"),
        ("show", SHA[:12]),
        ("config", "--get", "remote.origin.url"),
        ("fetch", "origin"),
    ],
)
def test_git_obj_refuses_anything_but_full_sha(
    monkeypatch: pytest.MonkeyPatch, args: tuple[str, ...]
) -> None:
    monkeypatch.setattr(rv, "run3", lambda argv: pytest.fail("git must not run"))
    with pytest.raises(ValueError):
        rv.git_obj("store", *args)


def test_git_obj_accepts_full_sha_and_pathspec(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def fake(argv: list[str]) -> tuple[int, str, str]:
        seen.append(argv)
        return 0, "", ""

    monkeypatch.setattr(rv, "run3", fake)
    rv.git_obj("store", "show", "--format=", "-U0", SHA, "--", "*.md")
    rv.git_obj("store", "rev-parse", f"{SHA}:docs/platform/infra-registry.md")
    rv.git_obj("store", "log", "--format=%H", "--before=2026-10-02T00:00:00Z", SHA, "--", "x")
    assert len(seen) == 3
    assert all("-C" in argv and "store" in argv for argv in seen)


def test_preserved_tips_reads_only_remote_origin_refs(tmp_path: Path) -> None:
    a, b, c = "a" * 40, "b" * 40, "c" * 40
    (tmp_path / "refs" / "remotes" / "origin" / "feature").mkdir(parents=True)
    (tmp_path / "refs" / "remotes" / "origin" / "feature" / "x").write_text(a + "\n")
    (tmp_path / "refs" / "remotes" / "origin" / "HEAD").write_text(
        "ref: refs/remotes/origin/main\n"
    )
    (tmp_path / "refs" / "heads").mkdir(parents=True)
    (tmp_path / "refs" / "heads" / "local").write_text(c + "\n")
    (tmp_path / "packed-refs").write_text(
        f"# pack-refs with: peeled fully-peeled sorted\n{b} refs/remotes/origin/main\n"
        f"{c} refs/heads/other\n^{c}\n"
    )
    assert rv.preserved_tips(str(tmp_path)) == [a, b]


def test_preserved_tips_loose_overrides_packed_of_the_same_name(tmp_path: Path) -> None:
    """PR-gate #565 finding 3: a packed value overridden by a loose file is not a tip."""
    stale, current = "a" * 40, "b" * 40
    (tmp_path / "refs" / "remotes" / "origin").mkdir(parents=True)
    (tmp_path / "refs" / "remotes" / "origin" / "main").write_text(current + "\n")
    (tmp_path / "packed-refs").write_text(
        f"# pack-refs with: peeled fully-peeled sorted\n{stale} refs/remotes/origin/main\n"
    )
    assert rv.preserved_tips(str(tmp_path)) == [current]


def test_preserved_url_takes_origin_only(tmp_path: Path) -> None:
    (tmp_path / "config").write_text(
        '[core]\n\thooksPath = X\n[remote "upstream"]\n\turl = https://github.com/o/up.git\n'
        '[remote "origin"]\n\turl = https://github.com/SpirrowGames/spirrow-tutorail.git\n'
        "\tfetch = +refs/heads/*:refs/remotes/origin/*\n"
    )
    url = rv.preserved_url(str(tmp_path))
    assert rv.repo_of_url(url) == "SpirrowGames/spirrow-tutorail"
