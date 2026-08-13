"""Credential-guarded reduction of a base-URL string to its ``host:port``.

Extracted from :mod:`spirrow_mindwire.source_marker` by P-2 (msg-953 §3),
where it was ``_route_authority``. **The extraction is not cosmetic.** Two
different lines now print a ``route=`` field derived from the same operator-
supplied env value:

- ``<!-- source: … route=… -->`` — a re-statement of the configuration
  (:mod:`spirrow_mindwire.source_marker`)
- ``<!-- attest: … route=… -->`` — where the preflight probe was actually sent
  (:mod:`spirrow_mindwire.naysayer.preflight`)

A base URL can carry an inline credential, and both lines land in a durable,
widely-readable chatroom post. A second, hand-rolled reducer on the attestation
side would be a second place for the leak PR #142 closed to reappear — and it
would reappear silently, because the two lines usually print the same value and
nobody diffs them. One reducer, one guard, both callers.

It also keeps the marker builder out of the adapter import graph (msg-834 §2
(c)). ``adapters/naysayer_sdk.py`` needs the preflight, the preflight needs this
reducer; had the reducer stayed in ``source_marker``, importing it would have
dragged the marker builder in through the back door.
"""

from __future__ import annotations

# Rendered when the base URL was set but its userinfo boundary could not be
# resolved (see :func:`route_authority`). A reader greps this token to find
# posts made under a malformed — possibly credential-bearing — base URL.
ROUTE_REDACTED = "redacted"


def route_authority(raw: str) -> str | None:
    """Reduce a base-URL string to its ``host:port`` authority.

    Returns ``None`` when the userinfo boundary cannot be resolved — see the
    fail-closed rule below. ``None`` is a third outcome, distinct from the
    empty string (which means the value reduced to no authority at all).

    Hand-rolled rather than :func:`urllib.parse.urlsplit` because the input is
    an operator-supplied env value, not a guaranteed-well-formed URL, and
    ``urlsplit`` mis-parses the most likely malformed case: ``urlsplit`` on a
    schemeless ``"100.79.84.62:8110"`` yields an empty ``netloc`` (a scheme
    must start with a letter, so the whole string lands in ``path``) and we
    would print nothing at all for a value that was in fact configured. The
    reader needs to see what was set, including when it was set wrong.

    The reductions, in order:

    1. drop everything up to and including ``://`` (the scheme)
    2. split at the first ``/``, ``?`` or ``#`` into authority + tail
       (path / query / fragment)
    3. **fail closed if the discarded tail still contains a ``@``**
    4. drop everything up to and including the last ``@`` (userinfo)

    Steps 3-4 are a **credential guard**, not cosmetics: a base URL of the
    form ``https://user:token@host/`` would otherwise copy a live secret into
    a durable chatroom post.

    **Why step 3 exists (PR #142 Tier B blocking).** Step 4 alone is only
    sound when the operator wrote a well-formed URL. A password containing a
    raw ``/``, ``?`` or ``#`` — a common env-var misconfiguration — moves the
    userinfo ``@`` past step 2's cut, so the cut lands *inside the credential*
    and step 4 then finds no ``@`` to remove. Measured on the pre-fix code:
    ``https://admin:p/assword@api.internal:8443/`` rendered ``admin:p``, i.e.
    the guard printed the secret it exists to suppress.

    Step 3 detects exactly that class, and does so by construction rather than
    by heuristic: userinfo is delimited by an ``@`` that necessarily follows
    the *whole* credential, so if step 2's cut ever lands inside the userinfo,
    that delimiting ``@`` is necessarily still in the discarded tail. "A ``@``
    survives in the tail" therefore covers every ordering that can leak.

    The converse does not hold — a genuine ``@`` in a path (
    ``https://host:8443/path@v2``) produces the same string shape as a
    password containing ``/``, and nothing in the string distinguishes them.
    That case is redacted as well. Losing the route display for an unusual but
    valid URL is the cheaper error: msg-957, "if the parser cannot safely
    disambiguate the userinfo boundary, it must fail closed or redact
    aggressively, not silently print the secret."
    """
    value = raw.strip()
    scheme_sep = value.find("://")
    if scheme_sep != -1:
        value = value[scheme_sep + 3 :]
    tail = ""
    for sep in ("/", "?", "#"):
        idx = value.find(sep)
        if idx != -1:
            tail += value[idx:]
            value = value[:idx]
    if "@" in tail:
        # The cut may have landed inside a credential. Unresolvable → redact.
        return None
    at = value.rfind("@")
    if at == -1:
        return value
    value = value[at + 1 :]
    # Userinfo consumed the entire value (e.g. "https://user:token@"). Do not
    # return "" — the caller renders that as ``route=empty``, which the legend
    # reserves as the fingerprint of a MISSING base URL. A credential-bearing
    # value must not forge that fingerprint.
    return value or None


__all__ = ["ROUTE_REDACTED", "route_authority"]
