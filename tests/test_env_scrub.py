"""I-4 / I-4b (T-gate-records-submitted-review-id msg-9923, msg-9953): ``scrubbed_secret_env``."""

from __future__ import annotations

import pytest

from spirrow_mindwire.adapters._env_scrub import SECRET_ENV_NAME_SUFFIXES, scrubbed_secret_env
from spirrow_mindwire.adapters.implementer import IMPLEMENTER_KEPT_TOKEN_ENV


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


# --------------------------------------------------------------------------- #
# I-4b (msg-9953): every MINDWIRE_* secret suffix, not only _TOKEN
# --------------------------------------------------------------------------- #


def test_secret_suffix_set_is_pinned() -> None:
    """Changing which names count as secrets must be a reviewed change to this test."""
    assert SECRET_ENV_NAME_SUFFIXES == ("TOKEN", "SECRET", "KEY", "WEBHOOK", "PASSWORD")


@pytest.mark.parametrize(
    "name",
    [
        "MINDWIRE_X_TOKEN",
        "MINDWIRE_X_SECRET",
        "MINDWIRE_MCP_API_KEY",
        "MINDWIRE_NOTIFY_DISCORD_WEBHOOK",
        "MINDWIRE_X_PASSWORD",
        "mindwire_notify_discord_webhook",
    ],
)
def test_each_secret_suffix_is_blanked(name: str) -> None:
    assert scrubbed_secret_env({name: "secret"}) == {name: ""}


@pytest.mark.parametrize(
    "name",
    [
        "PATH",
        "GH_TOKEN",
        "DISCORD_WEBHOOK",
        "MINDWIRE_LEXORA_URL",
        "MINDWIRE_KEYRING_PATH",  # suffix must END the name
        "MINDWIRE_X_TOKENS",
        "MINDWIRE_MONKEY",  # "KEY" only after an underscore
        "MINDWIRE_TOKEN",  # no middle segment
    ],
)
def test_non_secret_names_are_left_alone(name: str) -> None:
    assert scrubbed_secret_env({name: "v"}) == {}


def test_implementer_exception_keeps_only_the_author_token() -> None:
    environ = {
        "MINDWIRE_GITHUB_TOKEN": "author",
        "MINDWIRE_NAYSAYER_GITHUB_TOKEN": "ops",
        "MINDWIRE_NOTIFY_DISCORD_WEBHOOK": "https://hook",
    }
    assert scrubbed_secret_env(environ, keep=IMPLEMENTER_KEPT_TOKEN_ENV) == {
        "MINDWIRE_NAYSAYER_GITHUB_TOKEN": "",
        "MINDWIRE_NOTIFY_DISCORD_WEBHOOK": "",
    }
    # Every other turn (no keep) blanks the same author token.
    assert scrubbed_secret_env(environ)["MINDWIRE_GITHUB_TOKEN"] == ""
