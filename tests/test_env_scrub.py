"""I-4 (T-gate-records-submitted-review-id msg-9923): ``scrubbed_secret_env``."""

from __future__ import annotations

import pytest

from spirrow_mindwire.adapters._env_scrub import scrubbed_secret_env


def test_blanks_every_mindwire_token_name_and_nothing_else() -> None:
    environ = {
        "MINDWIRE_NAYSAYER_GITHUB_TOKEN": "a",
        "MINDWIRE_GITHUB_TOKEN": "b",
        "mindwire_stall_ledger_github_token": "c",
        "MINDWIRE_LEXORA_URL": "http://x",
        "GH_TOKEN": "d",
        "PATH": "p",
    }
    assert scrubbed_secret_env(environ) == {
        "MINDWIRE_NAYSAYER_GITHUB_TOKEN": "",
        "MINDWIRE_GITHUB_TOKEN": "",
        "mindwire_stall_ledger_github_token": "",
    }


def test_keep_is_case_insensitive_and_exact() -> None:
    environ = {"MINDWIRE_GITHUB_TOKEN": "b", "MINDWIRE_NAYSAYER_GITHUB_TOKEN": "a"}
    assert scrubbed_secret_env(environ, keep=["mindwire_github_token"]) == {
        "MINDWIRE_NAYSAYER_GITHUB_TOKEN": ""
    }


def test_defaults_to_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINDWIRE_PROBE_TOKEN", "v")
    assert scrubbed_secret_env()["MINDWIRE_PROBE_TOKEN"] == ""


def test_values_are_never_copied() -> None:
    assert set(scrubbed_secret_env({"MINDWIRE_A_TOKEN": "secret"}).values()) == {""}
