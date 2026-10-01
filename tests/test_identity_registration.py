"""Tests for the T-role-null write half (msg-1704 / msg-1706 as scheduled by msg-4902).

Surfaces:

1. :func:`check_identity_against_classification`, the two-way guard from msg-1706 §1,
   checked in both directions and for both kinds.
2. :func:`build_upsert_identity_args`, the one constructor. It must never emit a payload
   the guard rejects, and it refuses to guess a participant's ``independence_class``.
3. Loader rules for the new optional ``independence_class`` YAML field.
4. ``scripts/register_identities.py``: stops on ``success=False`` (DoD 2, which is the
   PR-A deploy check), and read-back violations produce a distinct exit code.
5. ``scripts/identity_findings.py`` store check, the msg-1706 §4 tamper detection: a
   hand-written record that breaks the pairing is reported.

No test pins Prismind's enum, in either tuple or import form. That is deliberate
(msg-1706 §2 / DoD 4): the only proof the deployed service accepts the value is the live
``upsert_identity`` result, which is recorded in the PR body.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

from spirrow_mindwire.identity import (
    MACHINE_INDEPENDENCE_CLASS,
    ClassificationEntry,
    ClassificationError,
    RegistrationRefusedError,
    build_upsert_identity_args,
    check_identity_against_classification,
    check_store_record,
    default_classification_path,
    load_legitimate_roles,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _entry(
    kind: str, legitimate: frozenset[str], independence_class: str | None = None
) -> ClassificationEntry:
    return ClassificationEntry(
        name=f"{kind}-x",
        key=f"{kind}-x",
        kind=kind,
        legitimate=legitimate,
        primary_source="p::s",
        reason="r",
        independence_class=independence_class,
    )


MACHINE = _entry("machine", frozenset())
PARTICIPANT = _entry("participant", frozenset({"naysayer"}), "independent")


def _codes(entry: ClassificationEntry, ic: object, roles: object) -> list[str]:
    return [
        v.code
        for v in check_identity_against_classification(
            entry, independence_class=ic, allowed_roles=roles
        )
    ]


class TestGuard:
    def test_machine_consistent(self) -> None:
        assert _codes(MACHINE, MACHINE_INDEPENDENCE_CLASS, []) == []

    def test_machine_with_role_rejected(self) -> None:
        codes = _codes(MACHINE, MACHINE_INDEPENDENCE_CLASS, ["naysayer"])
        assert "machine_has_roles" in codes

    @pytest.mark.parametrize("ic", ["main-chain", "independent", None, ""])
    def test_machine_without_machine_class_rejected(self, ic: object) -> None:
        assert "machine_wrong_independence_class" in _codes(MACHINE, ic, [])

    def test_participant_consistent(self) -> None:
        assert _codes(PARTICIPANT, "independent", ["naysayer"]) == []

    def test_participant_with_no_roles_rejected(self) -> None:
        assert "participant_has_no_roles" in _codes(PARTICIPANT, "independent", [])

    def test_participant_marked_machine_rejected(self) -> None:
        codes = _codes(PARTICIPANT, MACHINE_INDEPENDENCE_CLASS, ["naysayer"])
        assert "participant_marked_machine" in codes

    def test_participant_missing_class_rejected(self) -> None:
        assert "participant_missing_independence_class" in _codes(PARTICIPANT, None, ["naysayer"])

    def test_participant_side_is_open_when_yaml_declares_none(self) -> None:
        # msg-1706 §1: the participant side is `!= machine`, NOT a closed set. An entry
        # without a declared value accepts any non-machine class.
        open_entry = _entry("participant", frozenset({"naysayer"}))
        assert _codes(open_entry, "some-unmeasured-class", ["naysayer"]) == []

    def test_participant_class_differs_from_yaml_reported(self) -> None:
        codes = _codes(PARTICIPANT, "main-chain", ["naysayer"])
        assert codes == ["independence_class_differs_from_classification"]

    def test_roles_differ_from_legitimate_reported(self) -> None:
        codes = _codes(PARTICIPANT, "independent", ["naysayer", "proposer"])
        assert codes == ["allowed_roles_differ_from_legitimate"]

    def test_malformed_roles_reported_not_raised(self) -> None:
        assert "allowed_roles_malformed" in _codes(MACHINE, MACHINE_INDEPENDENCE_CLASS, None)


class TestConstructor:
    def test_machine_payload(self) -> None:
        assert build_upsert_identity_args(MACHINE) == {
            "identity_name": "machine-x",
            "independence_class": MACHINE_INDEPENDENCE_CLASS,
            "allowed_roles": [],
        }

    def test_participant_payload(self) -> None:
        assert build_upsert_identity_args(PARTICIPANT) == {
            "identity_name": "participant-x",
            "independence_class": "independent",
            "allowed_roles": ["naysayer"],
        }

    def test_participant_without_class_refused(self) -> None:
        with pytest.raises(RegistrationRefusedError, match="refusing to guess"):
            build_upsert_identity_args(_entry("participant", frozenset({"naysayer"})))

    def test_no_null_independence_class_ever(self) -> None:
        # The defect this PR corrects: the read-half doc prescribed independence_class=null,
        # which the live API rejects (msg-1703 §2).
        for entry in load_legitimate_roles(default_classification_path()).entries:
            args = build_upsert_identity_args(entry)
            assert isinstance(args["independence_class"], str) and args["independence_class"]

    def test_every_shipped_entry_constructs_and_is_guard_clean(self) -> None:
        loaded = load_legitimate_roles(default_classification_path())
        for entry in loaded.entries:
            args = build_upsert_identity_args(entry)
            assert (
                check_identity_against_classification(
                    entry,
                    independence_class=args["independence_class"],
                    allowed_roles=args["allowed_roles"],
                )
                == []
            )
            # kind == machine ⟺ independence_class == machine, read off the payload.
            assert (entry.kind == "machine") == (
                args["independence_class"] == MACHINE_INDEPENDENCE_CLASS
            )


class TestLoaderIndependenceClass:
    def _load(self, tmp_path: Path, extra: str, kind: str, legitimate: str) -> Any:
        path = tmp_path / "legitimate_roles.yaml"
        path.write_text(
            f"""
version: 1
identities:
  - name: a
    kind: {kind}
    legitimate: {legitimate}
    {extra}
    primary_source: "p::s"
    reason: "r"
""".strip(),
            encoding="utf-8",
        )
        return load_legitimate_roles(path)

    def test_participant_value_loaded(self, tmp_path: Path) -> None:
        loaded = self._load(tmp_path, "independence_class: independent", "participant", '["x"]')
        assert loaded.entries[0].independence_class == "independent"

    def test_machine_must_not_declare(self, tmp_path: Path) -> None:
        with pytest.raises(ClassificationError, match="must not declare"):
            self._load(tmp_path, "independence_class: machine", "machine", "[]")

    def test_participant_cannot_declare_machine(self, tmp_path: Path) -> None:
        with pytest.raises(ClassificationError, match="must not declare"):
            self._load(tmp_path, "independence_class: machine", "participant", '["x"]')

    def test_participant_empty_value_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ClassificationError, match="non-empty"):
            self._load(tmp_path, 'independence_class: ""', "participant", '["x"]')


def _load_script(name: str) -> Any:
    path = _REPO_ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_{name}_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_REGISTER = _load_script("register_identities")
_FINDINGS = _load_script("identity_findings")


class _FakeStore:
    """In-memory identity store behind the MCP tool surface.

    ``reject`` lists independence_class values the fake "Prismind" rejects with a clean
    ``success=False``, standing in for an undeployed enum value. ``tamper`` post-edits a
    record so a read-back sees drift.
    """

    def __init__(
        self,
        reject: frozenset[str] = frozenset(),
        tamper: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.reject = reject
        self.tamper = tamper or {}
        self.records: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, name: str, params: dict[str, Any]) -> Any:
        self.calls.append((name, params))
        if name == "upsert_identity":
            if params["independence_class"] in self.reject:
                return {"success": False, "identity": None, "message": "invalid enum"}
            record = dict(params)
            record.update(self.tamper.get(params["identity_name"], {}))
            self.records[params["identity_name"]] = record
            return {"success": True, "identity": record, "created": True}
        if name == "get_identity":
            rec = self.records.get(params["identity_name"])
            if rec is None:
                return {"status": "not_found", "identity_name": params["identity_name"]}
            return {"status": "found", "identity_name": params["identity_name"], "identity": rec}
        if name == "chatroom_list_threads":
            return {"items": [], "total": 0}
        raise AssertionError(name)


def _shipped() -> Any:
    return load_legitimate_roles(default_classification_path())


class TestRegisterScript:
    def test_apply_all_clean(self) -> None:
        store = _FakeStore()
        classification = _shipped()
        results, stopped, code = asyncio.run(
            _REGISTER.apply(store, classification, _REGISTER.plan(classification))
        )
        assert code == _REGISTER.EXIT_OK and stopped is None
        assert len(results) == len(classification.entries)
        assert all(r["readback"]["violations"] == [] for r in results)

    def test_undeployed_machine_value_stops_at_first_machine(self) -> None:
        # DoD 2: success=False is the deploy check; nothing after it is written.
        store = _FakeStore(reject=frozenset({MACHINE_INDEPENDENCE_CLASS}))
        classification = _shipped()
        results, stopped, code = asyncio.run(
            _REGISTER.apply(store, classification, _REGISTER.plan(classification))
        )
        assert code == _REGISTER.EXIT_UPSERT_REFUSED
        first_machine = next(e.name for e in classification.entries if e.kind == "machine")
        assert stopped == first_machine
        assert results[-1]["identity_name"] == first_machine
        upserts = [p["identity_name"] for n, p in store.calls if n == "upsert_identity"]
        assert upserts[-1] == first_machine
        assert first_machine not in store.records

    def test_readback_violation_has_its_own_exit_code(self) -> None:
        store = _FakeStore(tamper={"pr-gate-relay": {"allowed_roles": ["naysayer"]}})
        classification = _shipped()
        results, _stopped, code = asyncio.run(
            _REGISTER.apply(store, classification, _REGISTER.plan(classification))
        )
        assert code == _REGISTER.EXIT_READBACK_VIOLATION
        row = next(r for r in results if r["identity_name"] == "pr-gate-relay")
        assert [v["code"] for v in row["readback"]["violations"]] == [
            "machine_has_roles",
            "allowed_roles_differ_from_legitimate",
        ]

    def test_dry_run_makes_no_network_call(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def _boom(*_a: Any, **_k: Any) -> Any:
            raise AssertionError("dry run must not construct an MCP client")

        monkeypatch.setattr(_REGISTER, "StreamableHttpChatroomMcp", _boom)
        assert _REGISTER.main([]) == _REGISTER.EXIT_OK
        assert '"mode": "dry-run"' in capsys.readouterr().out


class TestFindingsStoreCheck:
    def test_hand_tampered_record_is_detected(self) -> None:
        # msg-1706 §4: a record written directly via upsert_identity, bypassing the guard.
        store = _FakeStore()
        store.records["pr-gate-relay"] = {
            "identity_name": "pr-gate-relay",
            "independence_class": MACHINE_INDEPENDENCE_CLASS,
            "allowed_roles": ["naysayer"],
        }
        store.records["naysayer-pr-review"] = {
            "identity_name": "naysayer-pr-review",
            "independence_class": MACHINE_INDEPENDENCE_CLASS,
            "allowed_roles": ["naysayer"],
        }
        rows, errors = asyncio.run(_FINDINGS._check_store(store, _shipped()))
        assert errors == []
        by_name = {r["identity_name"]: r for r in rows}
        assert by_name["pr-gate-relay"]["violations"][0]["code"] == ("machine_has_roles")
        assert "participant_marked_machine" in [
            v["code"] for v in by_name["naysayer-pr-review"]["violations"]
        ]
        # Unregistered identities are reported as such, not as consistent.
        assert by_name["orchestrator"]["status"] == "not_found"

    def test_check_store_record_found_clean(self) -> None:
        row = check_store_record(
            MACHINE,
            {
                "status": "found",
                "identity": {"independence_class": MACHINE_INDEPENDENCE_CLASS, "allowed_roles": []},
            },
        )
        assert row["violations"] == []
