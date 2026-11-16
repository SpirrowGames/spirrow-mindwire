"""The ``jev`` object of one material PUT, for the head the composer read (2e-2b-2).

Spec: T-decider-conductor-hook, Bohr msg-8616 (endorsed Einstein, no objections), against the
interface magickit#109 delivered (operator msg-8577 §2). The sweep wrapper's
``Push-DecisionMaterial`` calls this once per PUT it is about to send, with the head the composer
actually read (``extras.head_msg_id_read``), and puts the printed ``jev`` into the PUT body when
it is not ``null``.

Why a lookup at push time and not a field on ``parked_humans.py``'s rows: the material PUT is sent
from inside the sweep loop, right after the Conductor run that parked the thread, and the
parked-humans probe runs later, for the digest. Its rows do not exist yet when the PUT goes out.
The annotation itself does: the Conductor writes it when it escalates, before it exits.

Contract:

    scripts/jev_material.py --annotations <path> --project <p> --thread-id <t> --head-msg-id <m>

    stdout, one ASCII JSON line, exit 0:
        {"jev": {"should_ask_human": 0.12, "rule": "R3", "model_version": "jev/tierc-v4"}}
        {"jev": null}            -- no annotation for exactly this head, or no file at all
    exit 1, stderr names why     -- the file exists but cannot be read

The mapping is :func:`spirrow_mindwire.decider.annotation.jev_material`; this file only joins on
``(project, thread_id, head_msg_id)``, last line per key wins, as ``parked_humans.py`` does.

**Reader of the payload.** The sweep wrapper, which forwards it unchanged to magickit's decision
page (read there by Takahito). Nothing here posts to a chatroom thread, so no chatroom fallback
surface is involved. A failure is fail-open on the wrapper side: the PUT goes out without ``jev``
and the push log line says ``jev=absent`` with the reason.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from spirrow_mindwire.decider.annotation import jev_material, read_annotations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--annotations", type=Path, required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--thread-id", required=True)
    parser.add_argument("--head-msg-id", required=True)
    args = parser.parse_args(argv)

    try:
        found, skipped = read_annotations(args.annotations)
    except (OSError, UnicodeDecodeError) as exc:
        print(f"jev_material: annotations unreadable: {exc}", file=sys.stderr)
        return 1
    if skipped:
        print(f"jev_material: {skipped} malformed annotation line(s) skipped", file=sys.stderr)
    ann = found.get((args.project, args.thread_id, args.head_msg_id))
    # ASCII-only stdout (D-33): the rule text can be Japanese (NO_RULE_TEXT), and the wrapper
    # reads the stream as UTF-8 whatever the console code page is.
    print(json.dumps({"jev": jev_material(ann)}, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
