"""The PR-review output-token budget (human decision B on msg-7130).

PR #485 @ 290a0e1 ran the reviewer out of its 32000-token output cap (msg-7129,
``finish_reason=length``), which the gate turns into a fail-closed REQUEST_CHANGES with no
findings. These tests pin the raised default and the ``[naysayer_gating] review_max_tokens``
override that the composition root threads into the driver.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from spirrow_mindwire.config import NaysayerGatingConfig
from spirrow_mindwire.loop_runner import build_pr_review_driver
from spirrow_mindwire.naysayer import pr_review
from spirrow_mindwire.naysayer.pr_review import NaysayerPrReviewDriver


class _NoLexora:
    async def complete(self, **_: object) -> object:  # pragma: no cover - never called
        raise AssertionError("not called")


def test_default_review_budget_is_raised() -> None:
    assert pr_review.DEFAULT_REVIEW_MAX_TOKENS == 65536


def test_driver_uses_raised_default() -> None:
    driver = NaysayerPrReviewDriver(lexora=_NoLexora(), github=object())  # type: ignore[arg-type]
    assert driver._max_tokens == 65536


def test_gating_config_leaves_budget_unset_by_default() -> None:
    assert NaysayerGatingConfig().review_max_tokens is None


def test_gating_config_rejects_non_positive_budget() -> None:
    with pytest.raises(ValidationError):
        NaysayerGatingConfig(review_max_tokens=0)


def test_composition_root_keeps_driver_default_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def fake_driver(**kwargs: object) -> object:
        seen.update(kwargs)
        return object()

    monkeypatch.setattr("spirrow_mindwire.loop_runner.NaysayerPrReviewDriver", fake_driver)
    build_pr_review_driver(NaysayerGatingConfig())
    assert "max_tokens" not in seen  # the driver's own default applies


def test_composition_root_threads_configured_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_driver(**kwargs: object) -> object:
        seen.update(kwargs)
        return object()

    monkeypatch.setattr("spirrow_mindwire.loop_runner.NaysayerPrReviewDriver", fake_driver)
    build_pr_review_driver(NaysayerGatingConfig(review_max_tokens=48000))
    assert seen["max_tokens"] == 48000


def test_budget_is_settable_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from spirrow_mindwire.config import load_settings

    monkeypatch.setenv("MINDWIRE_NAYSAYER_GATING__REVIEW_MAX_TOKENS", "60000")
    settings = load_settings(None)
    assert settings.naysayer_gating.review_max_tokens == 60000
