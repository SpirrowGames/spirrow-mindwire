"""The registered shadow questions version cannot drift from the live one (DECIDED 2d-15).

T-decider-conductor-hook msg-5753 item 4: ``b224540`` raised the live question frame to
``tierc-v3`` while the pre-registration and the exporter still counted ``tierc-v2``, so every
production row would have been excluded and the export would have been silently empty. This
test ties the three places together:

* ``decider/questions.py`` ``TIERC_ESCALATION_QUESTIONS_VERSION`` — what the live hook logs;
* ``scripts/export_shadow_eval_set.py`` ``REGISTERED_QUESTIONS_VERSION`` — what is counted;
* ``eval/tierc/shadow-prereg.md`` — the title, §1 condition 3 and the §2 row.

Changing the question frame (and so the version) turns the gate red in that PR, which forces an
explicit decision on the registration before any data is counted.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from typing import Any

from spirrow_mindwire.decider.questions import TIERC_ESCALATION_QUESTIONS_VERSION

ROOT = Path(__file__).resolve().parent.parent
PREREG = ROOT / "eval" / "tierc" / "shadow-prereg.md"


def _exporter() -> Any:
    spec = importlib.util.spec_from_file_location(
        "export_shadow_eval_set_regver", ROOT / "scripts" / "export_shadow_eval_set.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses resolve their module through sys.modules
    spec.loader.exec_module(mod)
    return mod


def prereg_versions(text: str) -> dict[str, str]:
    """The version the pre-registration names in its title, §1 condition 3 and §2 row."""
    patterns = {
        "title": r"^# Tier-C production shadow — pre-registration \((?P<v>[^)]+)\)$",
        "condition_3": r'^3\. \*\*Questions version:\*\* `questions_version == "(?P<v>[^"]+)"`',
        "registered_values": r"^\| Questions version \| `(?P<v>[^`]+)` \|",
    }
    out: dict[str, str] = {}
    for where, pattern in patterns.items():
        found = re.findall(pattern, text, flags=re.MULTILINE)
        assert len(found) == 1, f"shadow-prereg.md: expected one {where} line, found {found}"
        out[where] = found[0]
    return out


def test_registered_version_matches_the_live_version() -> None:
    registered = _exporter().REGISTERED_QUESTIONS_VERSION
    prereg = prereg_versions(PREREG.read_text(encoding="utf-8"))
    assert registered == TIERC_ESCALATION_QUESTIONS_VERSION, (
        "the live question frame's version changed: decide the shadow registration explicitly "
        "(eval/tierc/shadow-prereg.md, the exporter) before any data is counted"
    )
    assert prereg == dict.fromkeys(prereg, registered)
