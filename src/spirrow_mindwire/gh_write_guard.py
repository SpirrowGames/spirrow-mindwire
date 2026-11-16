"""L-p: the implementer's PreToolUse guard on ``gh`` writes issued through Bash.

Thread ``T-real-infra-values-egress-from-agent-context``. The design is the approved chain
msg-8648 §1 (classification order) + msg-8650 §1-§3 (allowlist) + msg-9289 §1/§2/§4 +
msg-9292 §1 + msg-9297 §3 / msg-9299 §1, §5 / msg-9301 (approved by Einstein, last in the
post following msg-9301). **This module is the normative copy** of the allowlist, the
classification order, the ``-`` recovery rule and the fixed deny texts (msg-9299 §5); the thread
text and the measurement script used for M-10f / M-10g (``m10g_run2.py``, sha256 prefix
``b52ca86b63e0``, never committed) only point here.

What it does. A Bash call whose command contains a ``gh`` write (:func:`is_gh_write`) is
classified into exactly one of nine classes (:data:`CLASSES`, first match wins). Only
``a_prime_heredoc`` (a lone ``gh`` write whose body arrives through one quoted heredoc) and
``a_prime_inline`` (a lone ``gh`` write whose arguments are all literal and on the allowlist)
are allowed; everything else is denied before the command runs. A Bash call with no ``gh`` write
passes untouched.

What it does NOT do (ADR-2026-05-23-07 §8 decision 3, msg-9299 §6). This narrows the *shape* of
``gh`` writes so that the body of a write is a literal fixed in the command string. It does not
close the path to GitHub: ``git push``, another program going out through the proxy, and the
content of an allowed write itself are all untouched. It is not a guarantee that nothing leaves.

Where it sits (ADR-2026-05-23-07 §7, msg-9301 §3). The roles' tool surfaces (``tools=``,
``mcp_servers``, session isolation) are unchanged; ``tests/test_role_tool_surface.py`` passes as
is. This guard is an extra runtime check on the shape of a Bash command *inside* that surface,
not a name denylist standing in for it.

Fail-closed (ADR-2026-05-23-07 §2.6, msg-9299 §5). An exception anywhere in the guard, a parse
failure, an unparsed gap in the tree, or a class outside the allowlist all deny.

Output discipline (C-8', msg-9297 §3). The deny reason is a fixed string chosen by
(class, command family). No part of the command, no parser diagnostic and no exception text is
ever put in the reason, logged, or written to stderr.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Iterator
from typing import Any

from claude_agent_sdk import HookContext, HookInput, HookJSONOutput, HookMatcher
from tree_sitter import Language, Node, Parser

# ------------------------------------------------------------------------------------ classes
CLASSES: tuple[str, ...] = (
    "ts_parse_fail",
    "compound",
    "heredoc_unquoted",
    "body_file_path",
    "body_expansion",
    "a_prime_heredoc",
    "stdin_no_heredoc",
    "a_prime_inline",
    "other",
)
ALLOWED_CLASSES: frozenset[str] = frozenset({"a_prime_heredoc", "a_prime_inline"})

# ------------------------------------------------------------------- what counts as a gh write
GH_NAMES = ("gh", "gh.exe")
READONLY_SUBS = frozenset({"view", "list", "status", "checks", "diff", "download"})
BODY_OPTS_WRITE = frozenset(
    {
        "-b",
        "--body",
        "--body-file",
        "-t",
        "--title",
        "-c",
        "--comment",
        "--subject",
        "-n",
        "--notes",
        "--notes-file",
    }
)
API_WRITE_FIELD = frozenset({"-f", "-F", "--field", "--raw-field", "--input"})
WRITE_METHODS = frozenset({"POST", "PATCH", "PUT", "DELETE"})
#: msg-8650 §2 / msg-9287 §2 / msg-9289 §1: gh writes whatever their options, and always class 4.
POSFILE_CMDS: frozenset[tuple[str, str]] = frozenset(
    {("gist", "create"), ("gist", "edit"), ("release", "create"), ("release", "upload")}
)

# ------------------------------------------------------------------------ option arity tables
PR_ISSUE_VAL = frozenset(
    {
        "-t",
        "--title",
        "-b",
        "--body",
        "-F",
        "--body-file",
        "-c",
        "--comment",
        "--subject",
        "-R",
        "--repo",
        "--add-label",
        "--remove-label",
        "-B",
        "--base",
        "-H",
        "--head",
        "-a",
        "--assignee",
        "-l",
        "--label",
        "-m",
        "--milestone",
        "-p",
        "--project",
        "-r",
        "--reviewer",
        "-T",
        "--template",
        "--add-assignee",
        "--remove-assignee",
        "--add-reviewer",
        "--remove-reviewer",
        "--add-project",
        "--remove-project",
        "--match-head-commit",
        "-A",
        "--author-email",
    }
)
SUB_FLAGS: dict[str, frozenset[str]] = {
    "review": frozenset({"-c", "--comment", "-a", "--approve", "-r", "--request-changes"}),
    "merge": frozenset(
        {"-m", "--merge", "-r", "--rebase", "-s", "--squash", "-d", "--delete-branch"}
    ),
}
API_VAL = frozenset(
    {
        "-X",
        "--method",
        "-f",
        "--raw-field",
        "-F",
        "--field",
        "-H",
        "--header",
        "--input",
        "-p",
        "--preview",
        "-q",
        "--jq",
        "-t",
        "--template",
        "--hostname",
        "--cache",
    }
)
RELEASE_VAL = frozenset(
    {
        "-n",
        "--notes",
        "-F",
        "--notes-file",
        "-t",
        "--title",
        "--target",
        "--discussion-category",
        "--notes-start-tag",
        "-R",
        "--repo",
    }
)
GIST_VAL = frozenset(
    {"-d", "--desc", "-f", "--filename", "-a", "--add", "-r", "--remove", "-R", "--repo"}
)

#: msg-8648 §2: options that read a file (``-`` / ``@-`` = stdin).
FILE_OPTS: dict[str, frozenset[str]] = {
    "pr": frozenset({"--body-file", "-F"}),
    "issue": frozenset({"--body-file", "-F"}),
    "release": frozenset({"-F", "--notes-file"}),
    "api": frozenset({"--input"}),
}
API_AT_FIELDS = frozenset({"-F", "--field", "--raw-field"})
#: msg-8648 §1 row 5: body-bearing arguments (aliases of the listed forms included).
BODY_ARGS = frozenset(
    {
        "--title",
        "-t",
        "-b",
        "--body",
        "-c",
        "--comment",
        "--subject",
        "-n",
        "--notes",
        "-f",
        "-F",
        "--field",
        "--raw-field",
    }
)

# -------------------------------------------------------------------- msg-8650 §3 allowlist
ALLOW_PI_SUBS = frozenset({"create", "comment", "edit", "review", "close", "merge"})
ALLOW_PI_OPTS = frozenset(
    {
        "-t",
        "--title",
        "-b",
        "--body",
        "-c",
        "--comment",
        "--subject",
        "--add-label",
        "--remove-label",
        "--approve",
        "--request-changes",
        "-R",
        "--repo",
    }
)
ALLOW_API_OPTS = frozenset(
    {"-X", "--method", "-f", "--raw-field", "-F", "--field", "-H", "--header"}
)

# ------------------------------------------------------------------------- tree vocabulary
NONLITERAL = frozenset(
    {
        "simple_expansion",
        "expansion",
        "command_substitution",
        "process_substitution",
        "arithmetic_expansion",
    }
)
STRUCT = frozenset({"command_substitution", "process_substitution", "subshell"})
GAP_CHECKED = frozenset({"program", "redirected_statement", "command", "list", "pipeline"})
WRAPPER_SHELLS = frozenset(
    {
        "pwsh",
        "pwsh.exe",
        "powershell",
        "powershell.exe",
        "bash",
        "sh",
        "bash.exe",
        "sh.exe",
        "cmd",
        "cmd.exe",
    }
)
LEX_GH = re.compile(r"(?<![\w./\\-])gh(?:\.exe)?[ \t]+(?:pr|issue|release|gist|api)\b")
LEX_STOP = re.compile(r"&&|\|\||[;|\n)`]")

_PARSER: Parser | None = None


def _parser() -> Parser:
    global _PARSER
    if _PARSER is None:
        import tree_sitter_bash

        _PARSER = Parser(Language(tree_sitter_bash.language()))
    return _PARSER


def parse(src: bytes) -> Node:
    """The tree-sitter-bash root node of ``src``."""
    return _parser().parse(src).root_node


# ----------------------------------------------------------------------------- gh-write test
def gh_write(argv: list[str], legacy: bool = False) -> bool:
    """Whether ``argv`` (``argv[0] == "gh"``) is a gh write (msg-8645 §3 4 + msg-8646 §4 +
    msg-9287 §2). ``legacy`` drops the msg-9287 §2 widening (kept for measurement parity)."""
    if len(argv) < 2:
        return False
    g = argv[1]
    sub = argv[2] if len(argv) > 2 else ""
    rest = argv[2:]
    if g in ("pr", "issue") and sub in ("create", "comment", "edit", "review"):
        return True
    if g == "api":
        for i, t in enumerate(rest):
            if (
                t in ("-X", "--method")
                and i + 1 < len(rest)
                and rest[i + 1].upper() in WRITE_METHODS
            ):
                return True
            if t.startswith("--method=") and t.split("=", 1)[1].upper() in WRITE_METHODS:
                return True
            if t.startswith("-X") and not t.startswith("--") and t[2:].upper() in WRITE_METHODS:
                return True
            if t in API_WRITE_FIELD or t.split("=", 1)[0] in ("--field", "--raw-field", "--input"):
                return True
            if t[:2] in ("-f", "-F") and len(t) > 2 and not t.startswith("--"):
                return True
        return False
    if not legacy and (g, sub) in POSFILE_CMDS:
        return True
    if g in ("pr", "issue", "release", "gist") and sub not in READONLY_SUBS:
        return any(t in BODY_OPTS_WRITE or t.split("=", 1)[0] in BODY_OPTS_WRITE for t in rest)
    return False


def _lex_split(s: str) -> list[str]:
    try:
        return shlex.split(s, posix=True)
    except ValueError:
        return s.split()


def lex_has_gh_write(text: str, legacy: bool = False) -> bool:
    """Lexical fallback used when the tree is unusable (and inside wrapper-shell strings)."""
    for m in LEX_GH.finditer(text):
        seg = text[m.start() :]
        stop = LEX_STOP.search(seg)
        if stop:
            seg = seg[: stop.start()]
        toks = _lex_split(seg)
        if toks:
            toks[0] = "gh"
        if gh_write(toks, legacy):
            return True
    return False


# ------------------------------------------------------------------------------ tree helpers
def _text(n: Node) -> str:
    return (n.text or b"").decode("utf-8", "replace")


def literal(n: Node) -> bool:
    t = n.type
    if t in ("word", "number", "raw_string"):
        return not any(c.type in NONLITERAL for c in n.children)
    if t == "string":
        return all(c.type in ('"', "string_content") for c in n.children)
    if t == "concatenation":
        return all(literal(c) for c in n.children)
    return False


def value(n: Node) -> str:
    t = n.type
    s = _text(n)
    if t == "raw_string":
        return s[1:-1]
    if t == "string":
        return s[1:-1].replace('\\"', '"')
    if t == "concatenation":
        return "".join(value(c) for c in n.children)
    if t == "word":
        return re.sub(r"\\(.)", r"\1", s)
    return s


def cmd_name(cmd: Node) -> Node | None:
    n = cmd.child_by_field_name("name")
    if n is not None and n.type == "command_name" and n.named_child_count == 1:
        n = n.named_children[0]
    return n


def walk(n: Node) -> Iterator[Node]:
    yield n
    for c in n.children:
        yield from walk(c)


def _ends_with_command(node: Node | None) -> bool:
    """msg-9291 §3: the gap sits right after the last argument of a simple command — follow the
    last child of a list / pipeline down until a command is reached."""
    while node is not None and node.type != "command" and node.child_count:
        node = node.children[-1]
    return node is not None and node.type == "command"


def gap_scan(root: Node, src: bytes) -> tuple[bool, list[Node]]:
    """msg-9289 §4. Two separate rules:

    * gap check (adds denials): any non-whitespace byte not covered by a child of a statement,
      command, list or pipeline node is a parse failure;
    * ``-`` recovery (an exception that turns a denial back into an allow): tree-sitter-bash
      0.25.1 drops a bare ``-`` right before ``<<``; exactly that one byte, sitting between the
      last argument of a simple command and a heredoc redirect, is recovered as a trailing
      argument. Its reason is the canonical form's real use, not safety.

    Returns ``(ok, recovered_statements)``.
    """
    dash: list[Node] = []
    for n in walk(root):
        if n.type not in GAP_CHECKED:
            continue
        pos = n.start_byte
        prev: Node | None = None
        kids: list[Node | None] = [*n.children, None]
        for c in kids:
            end = c.start_byte if c is not None else n.end_byte
            gap = src[pos:end].replace(b"\\\r\n", b"").replace(b"\\\n", b"")
            if gap.strip():
                if (
                    gap.strip() == b"-"
                    and n.type == "redirected_statement"
                    and c is not None
                    and c.type == "heredoc_redirect"
                    and _ends_with_command(prev)
                ):
                    dash.append(n)
                else:
                    return False, []
            if c is not None:
                pos = max(pos, c.end_byte)
                prev = c
    return True, dash


def _tree_has_gh_write(root: Node, legacy: bool = False) -> bool:
    for n in walk(root):
        if n.type != "command":
            continue
        name = cmd_name(n)
        if name is None:
            continue
        argv = [value(name)] + [value(a) for a in n.children_by_field_name("argument")]
        base = argv[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
        if base in GH_NAMES:
            argv[0] = "gh"
            if gh_write(argv, legacy):
                return True
        elif base in WRAPPER_SHELLS and any(lex_has_gh_write(a, legacy) for a in argv[1:]):
            return True
    return False


def gh_write_kind(cmd: str) -> tuple[bool, bool]:
    """``(gh write under the current definition, gh write under the M-10f definition)``."""
    src = cmd.encode("utf-8", "replace")
    try:
        root = parse(src)
        if not root.has_error and gap_scan(root, src)[0]:
            return _tree_has_gh_write(root), _tree_has_gh_write(root, True)
    except Exception:
        pass
    return lex_has_gh_write(cmd), lex_has_gh_write(cmd, True)


def is_gh_write(cmd: str) -> bool:
    return gh_write_kind(cmd)[0]


# ------------------------------------------------------------------------------ arg parsing
#: ('opt', name, value_node | None, attached_value | None) or ('pos', node, None, None)
Item = tuple[str, Any, Node | None, str | None]


def _val_set(group: str, sub: str) -> frozenset[str]:
    if group in ("pr", "issue"):
        return PR_ISSUE_VAL - SUB_FLAGS.get(sub, frozenset())
    return {"api": API_VAL, "release": RELEASE_VAL, "gist": GIST_VAL}.get(group, frozenset())


def _parse_items(nodes: list[Node], group: str, sub: str) -> list[Item]:
    vals = _val_set(group, sub)
    items: list[Item] = []
    i = 0
    only_pos = False
    while i < len(nodes):
        n = nodes[i]
        v = value(n) if literal(n) else None
        if only_pos or v is None or not v.startswith("-") or v == "-":
            items.append(("pos", n, None, None))
        elif v == "--":
            only_pos = True
        elif v.startswith("--") and "=" in v:
            k, a = v.split("=", 1)
            items.append(("opt", k, None, a))
        elif v in vals:
            nxt = nodes[i + 1] if i + 1 < len(nodes) else None
            items.append(("opt", v, nxt, None))
            i += 1
        elif not v.startswith("--") and len(v) > 2 and v[:2] in vals:
            items.append(("opt", v[:2], None, v[2:]))
        else:
            items.append(("opt", v, None, None))
        i += 1
    return items


def _optval(item: Item) -> tuple[bool, str | None, bool]:
    """``(is_literal, value_or_None, has_value)`` for an option item."""
    _, _, vn, att = item
    if att is not None:
        return True, att, True
    if vn is None:
        return True, None, False
    lit = literal(vn)
    return lit, value(vn) if lit else None, True


# ------------------------------------------------------------------------------- classifier
def classify_ex(cmd: str) -> tuple[str, bool, bool]:
    """``(class, tree_ok, dash_recovered)`` per the msg-8648 §1 order as revised (first match)."""
    src = cmd.encode("utf-8", "replace")
    root = parse(src)
    if root.has_error:
        return "ts_parse_fail", False, False
    ok, dash = gap_scan(root, src)
    if not ok:
        return "ts_parse_fail", False, False
    rec = bool(dash)
    top = [c for c in root.named_children if c.type != "comment"]
    if len(top) != 1:
        return "compound", True, rec
    node = top[0]
    redirs: list[Node]
    body: Node | None
    if node.type == "redirected_statement":
        body = node.child_by_field_name("body")
        redirs = [
            c for c in node.named_children if c.type != "command" and c.type.endswith("_redirect")
        ]
    elif node.type == "command":
        body, redirs = node, []
    else:
        return "compound", True, rec
    if body is None or body.type != "command":
        return "compound", True, rec
    if any(n.type in STRUCT or n.type == "&" for n in walk(root)):
        return "compound", True, rec
    redirs += [c for c in body.named_children if c.type.endswith("_redirect")]
    heredocs = [r for r in redirs if r.type == "heredoc_redirect"]
    others = [r for r in redirs if r.type != "heredoc_redirect"]
    qh = 0
    for h in heredocs:
        start = next((c for c in h.children if c.type == "heredoc_start"), None)
        st = _text(start) if start is not None else ""
        if st[:1] in ("'", '"') or "\\" in st:
            qh += 1
        else:
            return "heredoc_unquoted", True, rec
    name = cmd_name(body)
    args = list(body.children_by_field_name("argument"))
    if name is None or not literal(name):
        return "other", True, rec
    nm = value(name)
    if nm.replace("\\", "/").rsplit("/", 1)[-1].lower() not in GH_NAMES or not args:
        return "other", True, rec
    if not literal(args[0]):
        return "other", True, rec
    group = value(args[0])
    if group == "api":
        sub, rest = "", args[1:]
    else:
        if len(args) < 2 or not literal(args[1]):
            return "other", True, rec
        sub, rest = value(args[1]), args[2:]
    # ---- 4 (ii), msg-9289 §1: positional-file commands are class 4 whatever their arguments,
    # and the '-' recovery does not apply to them (msg-9289 §4, third condition).
    if (group, sub) in POSFILE_CMDS:
        return "body_file_path", True, False
    items = _parse_items(rest, group, sub)
    dash_pos = rec  # the recovered '-' is the trailing argument
    # ---- 4 (i) and stdin detection
    stdin = False
    fileread = False
    for it in items:
        if it[0] != "opt":
            continue
        k = it[1]
        lit, v, has = _optval(it)
        if k in FILE_OPTS.get(group, frozenset()):
            if not has:
                if dash_pos:  # `--body-file -` whose '-' tree-sitter dropped
                    stdin = True
                    dash_pos = False
                continue
            if lit and v == "-":
                stdin = True
            else:
                fileread = True
        elif group == "api" and k in API_AT_FIELDS and has and lit and v is not None:
            fv = v.split("=", 1)[1] if "=" in v else ""
            if fv == "@-":
                stdin = True
            elif fv.startswith("@"):
                fileread = True
    pos_vals: list[str | None] = [
        value(it[1]) if literal(it[1]) else None for it in items if it[0] == "pos"
    ]
    if dash_pos:
        pos_vals.append("-")
    if fileread:
        return "body_file_path", True, rec
    # ---- 5
    for it in items:
        if it[0] == "opt" and it[1] in BODY_ARGS:
            lit, _, has = _optval(it)
            if has and not lit:
                return "body_expansion", True, rec
    # ---- 6 / 7
    if stdin and qh == 1 and not others and len(heredocs) == 1:
        return "a_prime_heredoc", True, rec
    if stdin and not heredocs:
        return "stdin_no_heredoc", True, rec
    # ---- 8 (allowlist, fail-closed: anything not listed is 9)
    if heredocs or others or stdin or dash_pos:
        return "other", True, rec
    if any(c.type == "variable_assignment" for c in body.named_children):
        return "other", True, rec
    if nm != "gh" or not all(literal(a) for a in args):
        return "other", True, rec
    opts = [it for it in items if it[0] == "opt"]
    if group in ("pr", "issue") and sub in ALLOW_PI_SUBS:
        if all(o[1] in ALLOW_PI_OPTS for o in opts) and len(pos_vals) <= 1:
            return "a_prime_inline", True, rec
    elif group == "api" and all(o[1] in ALLOW_API_OPTS for o in opts) and len(pos_vals) == 1:
        return "a_prime_inline", True, rec
    return "other", True, rec


def classify(cmd: str) -> str:
    return classify_ex(cmd)[0]


# ------------------------------------------------------------------------- deny texts (fixed)
#: P-1 (msg-9299 §1): the three command families the guidance is written for.
FAMILY_PR_ISSUE = "pr_issue"
FAMILY_API = "api"
FAMILY_UNSUPPORTED = "gist_release"
FAMILY_UNKNOWN = "unknown"

_GUIDE_PR_ISSUE = (
    "Run the gh pr/issue write alone in its own Bash call, pass the body with "
    "`--body-file -` and a quoted heredoc (`<<'EOF'`), and name the repository with "
    "`-R owner/repo` instead of `cd`."
)
_GUIDE_API = (
    "Run the gh api write alone in its own Bash call with literal `-f`/`-F` values (no value "
    "starting with `@`) and the owner/repo written into the endpoint."
)
_GUIDE_UNSUPPORTED = (
    "gh gist create/edit and gh release create/upload are always denied; there is no allowed "
    "form. Stop and explain in the thread why it is needed."
)
_GUIDE_UNKNOWN = (
    "Run the gh write alone in its own Bash call in the allowed form "
    "(`gh pr|issue ... --body-file - <<'EOF'`)."
)
_GUIDES: dict[str, str] = {
    FAMILY_PR_ISSUE: _GUIDE_PR_ISSUE,
    FAMILY_API: _GUIDE_API,
    FAMILY_UNSUPPORTED: _GUIDE_UNSUPPORTED,
    FAMILY_UNKNOWN: _GUIDE_UNKNOWN,
}
_CLASS_REASON: dict[str, str] = {
    "ts_parse_fail": "the command could not be parsed completely",
    "compound": "the gh write is combined with other commands (`cd`, `git`, `|`, `;`, `&&`, "
    "newline, subshell or background)",
    "heredoc_unquoted": "the heredoc delimiter is unquoted, so the body is expanded",
    "body_file_path": "the body is read from a file",
    "body_expansion": "a body argument contains a variable or command expansion",
    "stdin_no_heredoc": "the body is read from stdin without a quoted heredoc",
    "other": "the gh write is not in the allowed form",
    "guard_error": "the guard could not decide",
}
#: The deny text for an unexpected failure inside the guard (fail-closed).
GUARD_ERROR_KEY = "guard_error"


def _family_of(cmd: str) -> str:
    """The command family for the fixed guidance. Never echoes anything from ``cmd``."""
    try:
        for m in LEX_GH.finditer(cmd):
            toks = _lex_split(cmd[m.start() :].split("\n", 1)[0])
            if len(toks) >= 3 and (toks[1], toks[2]) in POSFILE_CMDS:
                return FAMILY_UNSUPPORTED
            if len(toks) >= 2 and toks[1] in ("pr", "issue"):
                return FAMILY_PR_ISSUE
            if len(toks) >= 2 and toks[1] == "api":
                return FAMILY_API
    except Exception:
        return FAMILY_UNKNOWN
    return FAMILY_UNKNOWN


def deny_text(key: str, family: str) -> str:
    """The fixed deny reason for ``(class key, family)`` — built only from constants."""
    reason = _CLASS_REASON.get(key, _CLASS_REASON["other"])
    guide = _GUIDES.get(family, _GUIDE_UNKNOWN)
    return f"L-p denied this gh write [{key}]: {reason}. {guide}"


def decide(cmd: object) -> tuple[bool, str | None]:
    """``(allowed, deny_text_or_None)`` for one Bash ``command`` value. Never raises."""
    try:
        if not isinstance(cmd, str):
            return False, deny_text(GUARD_ERROR_KEY, FAMILY_UNKNOWN)
        if not is_gh_write(cmd):
            return True, None
        family = _family_of(cmd)
        try:
            cls = classify(cmd)
        except Exception:
            cls = "ts_parse_fail"
        if cls in ALLOWED_CLASSES:
            return True, None
        return False, deny_text(cls, family)
    except Exception:
        return False, deny_text(GUARD_ERROR_KEY, FAMILY_UNKNOWN)


# --------------------------------------------------------------------------- P-1 prompt text
#: P-1 (msg-9299 §1, with the msg-9301 §4 / Einstein's CLI-check wording): the guidance the
#: implementer and proposer system prompts carry. Kept here so the prompt and the guard it
#: describes cannot drift apart.
P1_PROMPT_BLOCK = """\
GH WRITES: every Bash call that writes through `gh` passes a guard (L-p) that \
denies any other shape before the command runs.
(i) `gh pr create|comment|edit|review|merge` and `gh issue create|comment|edit`: \
run that one command alone in its own Bash call — never chain it with `cd`, \
`git`, `|`, `;`, `&&` or a newline. Pass the body with `--body-file -` and a \
heredoc whose delimiter is quoted (`<<'EOF'`). Name the repository with \
`-R owner/repo` instead of `cd`; do `git push` and any other preparation in an \
earlier, separate Bash call.
(ii) `gh api`: alone in its own Bash call, every value a literal `-f`/`-F` (no \
value starting with `@`), the owner/repo written into the endpoint. This is on \
the allowlist but unproven in practice: a read-only GET with `-f` was checked \
against the CLI; `-F` and writes have not been.
(iii) `gh gist create|edit` and `gh release create|upload` are always denied and \
have no allowed form. If one is needed, stop and say why in the thread.
"""


# ------------------------------------------------------------------------------- SDK hook
def _deny_output(reason: str) -> HookJSONOutput:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


async def gh_write_pretooluse(
    input_data: HookInput, _tool_use_id: str | None, _context: HookContext
) -> HookJSONOutput:
    """PreToolUse callback for the Bash tool. ``{}`` = no decision (allowed); otherwise deny.

    The reader of the deny reason is the model in the same session, through the SDK hook return
    value. It is not written to the chatroom or anywhere else (ADR-2026-05-21-06 I1).
    """
    try:
        data: Any = input_data
        if not isinstance(data, dict) or data.get("tool_name") != "Bash":
            return {}
        tool_input = data.get("tool_input")
        cmd = tool_input.get("command") if isinstance(tool_input, dict) else None
        allowed, reason = decide(cmd)
        if allowed:
            return {}
        return _deny_output(reason or deny_text(GUARD_ERROR_KEY, FAMILY_UNKNOWN))
    except Exception:
        return _deny_output(deny_text(GUARD_ERROR_KEY, FAMILY_UNKNOWN))


def gh_write_guard_hooks() -> dict[Any, list[HookMatcher]]:
    """The ``ClaudeAgentOptions.hooks`` entry that attaches L-p to the Bash tool."""
    return {"PreToolUse": [HookMatcher(matcher="Bash", hooks=[gh_write_pretooluse])]}


__all__ = [
    "ALLOWED_CLASSES",
    "CLASSES",
    "P1_PROMPT_BLOCK",
    "classify",
    "classify_ex",
    "decide",
    "deny_text",
    "gap_scan",
    "gh_write_guard_hooks",
    "gh_write_kind",
    "gh_write_pretooluse",
    "is_gh_write",
    "parse",
]
