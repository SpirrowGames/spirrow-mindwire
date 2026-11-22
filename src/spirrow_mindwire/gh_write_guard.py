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
``a_prime_heredoc`` (a lone ``gh`` write whose body arrives through one quoted heredoc and whose
other arguments pass the inline allowlist plus the stdin option) and
``a_prime_inline`` (a lone ``gh`` write whose arguments are all literal and on the allowlist)
are allowed; everything else is denied before the command runs.

L-p2: every other ``gh`` call (msg-9821 §1-§2 + msg-9823, approved by Einstein in msg-9824,
handed to implementation by the human post after msg-9850; Takahito's ruling msg-9797). A ``gh``
call outside the approved gh-write definition (:func:`gh_write`: ``pr``/``issue``
``create|comment|edit|review``; ``api`` with a write method or a field / ``--input``;
``pr``/``issue``/``release``/``gist`` with a body option; the four positional-file commands) is no
longer let through unjudged. It is judged by the ``<group> <sub>`` table :data:`L2_ALLOW` and, on
an allowed row, by the argument shapes of rule R-d (:func:`judge_table`). Anything not on the table
is denied (fail-closed): ``gh auth token`` and an ``auth status`` with arguments other than
``-a``/``--active`` as ``gh_auth_token``; ``variable`` and ``secret set|delete`` as
``gh_secret_value``; ``search`` and ``-S``/``--search`` as ``gh_search_text``; everything else —
including ``gh api`` outside the write definition, ``gh`` alone, ``--version`` and options before
the group — as ``gh_not_in_table``. ``auth setup-git`` is allowed as an explicit exception to rule
R-c (it changes git's credential helper) because the ``gh_auth_token`` deny text points to it
(msg-9797 §4) and it prints no token. Aliases are resolved first, from the gh 2.92.0 vocabulary.

How the two judgements combine (msg-9821 §1, msg-9823 Obj-27-1). Every ``gh`` node in the command
gets a verdict — a node inside the gh-write definition the L-p verdict of the whole command, any
other node the table's — and :func:`combine` folds them in node order: one deny denies the whole
command and the first deny names the class; an allow never ends the judgement early. A wrapper
shell (``bash -c``) whose argument contains a ``gh`` word, and a launcher (``env``, ``xargs``,
``time``, ...) with a ``gh`` argument, are denied as ``gh_not_in_table``: the table cannot see the
shape of the call inside.

What it does NOT do (ADR-2026-05-23-07 §8 decision 3, msg-9299 §6). This narrows the *shape* of
``gh`` writes so that the body of a write is a literal fixed in the command string. It does not
close the path to GitHub: ``git push``, another program going out through the proxy, and the
content of an allowed write itself are all untouched. It is not a guarantee that nothing leaves.
For L-p2 two different things remain (msg-9823 §2):

* narrower restrictions not taken this time — limiting ``-R`` and an ``owner/repo`` positional to
  the clone's ``origin``, and a branch to a ref that exists locally. Both need the hook to read
  git state at call time; they are a separate design, recorded as candidates.
* what an allowed operation carries by nature — the PR / run number or name needed to perform it
  is sent to GitHub as long as the operation is allowed, and would be even with the narrower
  restrictions above. R-d restricts the *form* of an argument (``owner/repo``, branch, tag, number
  shapes), not whether its value is safe: ``gh repo view <any name-shaped text>`` passes R-d and
  is sent.

Launch boundary (msg-9817 §0 as corrected by Obj-25-3). With the launch procedures examined in the
thread (direct launch, a preceding ``test``), a safe launch boundary has not been established:
diagnostics a program prints before it starts (a fatal interpreter error, for example) can reach
the session context. That stays open and recorded; this module does not address it.

Where it sits (ADR-2026-05-23-07 §7, msg-9301 §3). The roles' tool surfaces (``tools=``,
``mcp_servers``, session isolation) are unchanged; ``tests/test_role_tool_surface.py`` passes as
is. This guard is an extra runtime check on the shape of a Bash command *inside* that surface,
not a name denylist standing in for it.

Fail-closed (ADR-2026-05-23-07 §2.6, msg-9299 §5). An exception anywhere in the guard, a parse
failure, an unparsed gap in the tree, or a class outside the allowlist all deny.

Detection when the tree is unusable (PR-gate on #579, objection 4). If the command does not parse,
detection is *indeterminate*; it is never settled by the gh-write regex. An indeterminate command
is treated as a gh write — and so denied as ``ts_parse_fail`` — whenever its text, with quotes and
backslashes removed, contains ``gh`` / ``gh.exe`` as a word (``'gh'``, ``g"h"``, ``/usr/bin/gh``
all count). Only an unparsable command with no such word passes. The residual is the one the
parsed path has too: a ``gh`` whose name is built at run time (``$G``, ``$(...)``, ANSI-C quoting)
is an indirect call and is recognised in neither path.

Output discipline (C-8', msg-9297 §3). The deny reason is a fixed string chosen by
(class, command family). No part of the command, no parser diagnostic and no exception text is
ever put in the reason, logged, or written to stderr.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
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
#: Thread ``T-lp-allowlist-lacks-base-head-so-develop-flow-repos-cannot-open-prs`` (D1, approved
#: by Einstein after msg-9964): ``gh pr create`` — and only it — also takes the base and head
#: branch, each at most once, as a literal branch name (:func:`_is_branch_name`). Kept apart from
#: :data:`ALLOW_PI_OPTS` because ``-H`` means ``--header`` to ``gh api``, and because ``pr edit
#: --base`` (moving a PR's base) and ``issue`` are deliberately not covered. With either option
#: present the command may carry no positional argument (``gh pr create`` takes none).
ALLOW_PR_CREATE_REF_OPTS: dict[str, str] = {
    "-B": "base",
    "--base": "base",
    "-H": "head",
    "--head": "head",
}
_BRANCH_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")


def _is_branch_name(v: str | None) -> bool:
    """A plain branch name: no expansion, no ``owner:branch`` fork form, no ``..``/``//``, no
    trailing ``/`` or ``.lock``, and not starting with ``-``."""
    if v is None or not _BRANCH_NAME.fullmatch(v):
        return False
    return ".." not in v and "//" not in v and not v.endswith(("/", ".lock"))


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
#: Options gh resolves before the command path (cobra strips them while finding the
#: subcommand). Value-taking ones consume the next word; an unknown option is tried both ways.
GH_PRE_BOOL = frozenset({"-h", "--help", "--version"})
GH_PRE_VAL = frozenset({"-R", "--repo", "--hostname"})
#: Bound on the readings explored for unknown leading options; past it, detection says write.
_MAX_PATHS = 256
#: A ``gh`` word anywhere in a de-quoted text (the indeterminate-detection net, objection 4).
GH_TOKEN = re.compile(r"(?<![\w.-])gh(?:\.exe)?(?![\w.-])", re.IGNORECASE)
#: Unescaped characters that make an unquoted word expand (tilde, pathname, brace; objection 3).
WORD_EXPANDING = frozenset("*?[]{}~")
_MAX_WRAP_DEPTH = 3
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
def _command_paths(args: list[str]) -> list[tuple[int, ...]] | None:
    """Every command path (indices of the group and sub words in ``args`` = ``argv[1:]``) gh
    could resolve when options precede them (objection 1: ``gh --repo o/r pr comment ...``).
    ``None`` when the readings exceed :data:`_MAX_PATHS` (the caller treats that as a write).
    """
    out: set[tuple[int, ...]] = set()
    stack: list[tuple[int, tuple[int, ...]]] = [(0, ())]
    seen = 0
    while stack:
        seen += 1
        if seen > _MAX_PATHS:
            return None
        i, path = stack.pop()
        if len(path) == 2 or i >= len(args):
            out.add(path)
            continue
        t = args[i]
        if t.startswith("-") and t != "-":
            if t == "--" or "=" in t or t in GH_PRE_BOOL:
                stack.append((i + 1, path))
            elif t in GH_PRE_VAL:
                stack.append((i + 2, path))
            elif not t.startswith("--") and len(t) > 2 and t[:2] in GH_PRE_VAL:
                stack.append((i + 1, path))
            else:
                stack.extend(((i + 1, path), (i + 2, path)))
        else:
            stack.append((i + 1, (*path, i)))
    return sorted(out)


def gh_write(argv: list[str], legacy: bool = False) -> bool:
    """Whether ``argv`` (``argv[0] == "gh"``) is a gh write (msg-8645 §3 4 + msg-8646 §4 +
    msg-9287 §2). ``legacy`` drops the msg-9287 §2 widening (kept for measurement parity), and
    with it the leading-option normalisation, so the M-10f definition is reproduced as it was.

    Options before the group or the sub are normalised away first (objection 1): every command
    path gh could resolve is checked, the other words keep their order after it, and the call is
    a write if any of those readings is.
    """
    if legacy:
        return _gh_write_flat(argv, legacy)
    args = argv[1:]
    paths = _command_paths(args)
    if paths is None:
        return True
    for path in paths:
        head = [args[j] for j in path]
        tail = [a for j, a in enumerate(args) if j not in path]
        if _gh_write_flat([argv[0], *head, *tail]):
            return True
    return False


def _gh_write_flat(argv: list[str], legacy: bool = False) -> bool:
    """The definition on an argv whose group and sub are ``argv[1]`` / ``argv[2]``."""
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
        vals = _val_set(g, sub)
        return any(_is_body_opt(t, vals) for t in rest)
    return False


def _is_body_opt(t: str, vals: frozenset[str]) -> bool:
    """Whether the word ``t`` carries a body option (PR-gate on #583: attached short options).

    gh's flag parser accepts a short option's value attached to it (``-b"text"``) and short
    options bundled behind one dash (``-sb"text"``: ``-s`` then ``-b`` with its value), so the
    exact / ``--long=value`` test alone misses ``gh pr merge 1 --squash -b"$(cat f)"``. A
    single-dash word is read letter by letter the way gh reads it: a body letter anywhere before
    the first value-taking option makes it a body option; a value-taking option (``vals``, the
    arity table of this group and sub) ends the options, the rest of the word being its value.
    A letter in neither set is taken as a flag and the scan goes on, so an option missing from
    the arity table can only widen detection, never narrow it.
    """
    if t in BODY_OPTS_WRITE or t.split("=", 1)[0] in BODY_OPTS_WRITE:
        return True
    if not t.startswith("-") or t.startswith("--") or len(t) <= 2:
        return False
    for ch in t[1:]:
        opt = "-" + ch
        if opt in BODY_OPTS_WRITE:
            return True
        if opt in vals:
            return False
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


def _word_is_fixed(s: str) -> bool:
    """No unescaped tilde / glob / brace character in an unquoted word (objection 3)."""
    esc = False
    for ch in s:
        if esc:
            esc = False
        elif ch == "\\":
            esc = True
        elif ch in WORD_EXPANDING:
            return False
    return True


def literal(n: Node) -> bool:
    """Whether ``n`` is fixed in the command string. An unquoted word is not if Bash would expand
    it (tilde, pathname or brace expansion): ``--body ~`` sends the home directory."""
    t = n.type
    if t == "word":
        return not any(c.type in NONLITERAL for c in n.children) and _word_is_fixed(_text(n))
    if t in ("number", "raw_string"):
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


def maybe_gh(text: str) -> bool:
    """The indeterminate-detection net: ``gh`` as a word once quotes are gone, reading each
    backslash both as an escape (``g\\h``) and as a Windows path separator (``C:\\x\\gh.exe``)."""
    unquoted = re.sub(r"[\"']", "", text)
    return any(GH_TOKEN.search(unquoted.replace("\\", sep)) for sep in ("", "/"))


def _detect(text: str, depth: int = 0) -> bool | None:
    """Current-definition detection. ``None`` = indeterminate (the text does not parse)."""
    src = text.encode("utf-8", "replace")
    try:
        root = parse(src)
        if root.has_error or not gap_scan(root, src)[0]:
            return None
    except Exception:
        return None
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
            if gh_write(argv):
                return True
        elif base in WRAPPER_SHELLS:
            for a in argv[1:]:
                r = _detect(a, depth + 1) if depth < _MAX_WRAP_DEPTH else None
                if r is None:
                    r = maybe_gh(a)
                if r:
                    return True
    return False


def gh_write_kind(cmd: str) -> tuple[bool, bool]:
    """``(gh write under the current definition, gh write under the M-10f definition)``.

    The current definition never settles an unparsable command by the gh-write regex: it is a
    write whenever :func:`maybe_gh` finds a ``gh`` word in it (objection 4). The M-10f column
    keeps the measurement-time behaviour (lexical fallback) for parity only; nothing decides on
    it."""
    cur = _detect(cmd)
    if cur is None:
        cur = maybe_gh(cmd)
    src = cmd.encode("utf-8", "replace")
    try:
        root = parse(src)
        if not root.has_error and gap_scan(root, src)[0]:
            return cur, _tree_has_gh_write(root, True)
    except Exception:
        pass
    return cur, lex_has_gh_write(cmd, True)


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
    if group.startswith("-"):  # options before the command path have no allowed form (obj. 1)
        return "other", True, rec
    if group == "api":
        sub, rest = "", args[1:]
    else:
        if len(args) < 2 or not literal(args[1]):
            return "other", True, rec
        sub, rest = value(args[1]), args[2:]
        if sub.startswith("-"):
            return "other", True, rec
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
    heredoc_form = stdin and qh == 1 and not others and len(heredocs) == 1 and not dash_pos
    if stdin and not heredocs:
        return "stdin_no_heredoc", True, rec
    if not heredoc_form and (heredocs or others or stdin or dash_pos):
        return "other", True, rec
    # ---- 6 and 8 share one allowlist (objection 2). The quoted heredoc fixes the body only; the
    # rest of the command must be as literal and as listed as the inline form. The heredoc form
    # additionally permits its group's stdin option; a group with no inline allowlist (release)
    # has no heredoc form either. Anything not listed is 9 (fail-closed).
    allowed_cls = "a_prime_heredoc" if heredoc_form else "a_prime_inline"
    if any(c.type == "variable_assignment" for c in body.named_children):
        return "other", True, rec
    if nm != "gh" or not all(literal(a) for a in args):
        return "other", True, rec
    opts = [it for it in items if it[0] == "opt"]
    stdin_opts = FILE_OPTS.get(group, frozenset()) if heredoc_form else frozenset()
    if group in ("pr", "issue") and sub in ALLOW_PI_SUBS:
        ref_opts = [o for o in opts if o[1] in ALLOW_PR_CREATE_REF_OPTS]
        if ref_opts:
            if group != "pr" or sub != "create" or pos_vals:
                return "other", True, rec
            seen: set[str] = set()
            for o in ref_opts:
                kind = ALLOW_PR_CREATE_REF_OPTS[o[1]]
                lit, v, has = _optval(o)
                if kind in seen or not (has and lit and _is_branch_name(v)):
                    return "other", True, rec
                seen.add(kind)
        rest_opts = [o for o in opts if o[1] not in ALLOW_PR_CREATE_REF_OPTS]
        if all(o[1] in ALLOW_PI_OPTS | stdin_opts for o in rest_opts) and len(pos_vals) <= 1:
            return allowed_cls, True, rec
    elif (
        group == "api"
        and all(o[1] in ALLOW_API_OPTS | stdin_opts for o in opts)
        and len(pos_vals) == 1
    ):
        return allowed_cls, True, rec
    return "other", True, rec


def classify(cmd: str) -> str:
    return classify_ex(cmd)[0]


# ---------------------------------------------------------- L-p2: the out-of-definition table
#: L-p2 classes (msg-9819 §3, kept by msg-9821 §3). Every one of them denies.
L2_CLASSES: tuple[str, ...] = (
    "gh_auth_token",
    "gh_secret_value",
    "gh_search_text",
    "gh_not_in_table",
)

#: Built-in aliases of gh 2.92.0, copied from the ``alias`` lines of the M-10j vocabulary
#: ``gh_vocab_2.92.0.txt`` (sha256 ``be71a8761eb9…``, the canonical input named in msg-9817 §1;
#: user aliases excluded). Top-level group aliases first, then ``(group, sub)`` aliases.
GH_GROUP_ALIASES: dict[str, str] = {
    "agents": "agent-task",
    "at": "attestation",
    "cs": "codespace",
    "ext": "extension",
    "rs": "ruleset",
    "skills": "skill",
}
GH_SUB_ALIASES: dict[tuple[str, str], str] = {
    ("alias", "ls"): "list",
    ("cache", "ls"): "list",
    ("codespace", "ls"): "list",
    ("config", "ls"): "list",
    ("extension", "ls"): "list",
    ("gist", "ls"): "list",
    ("gist", "new"): "create",
    ("gpg-key", "ls"): "list",
    ("issue", "ls"): "list",
    ("issue", "new"): "create",
    ("label", "ls"): "list",
    ("org", "ls"): "list",
    ("pr", "co"): "checkout",
    ("pr", "ls"): "list",
    ("pr", "new"): "create",
    ("project", "ls"): "list",
    ("release", "ls"): "list",
    ("release", "new"): "create",
    ("repo", "ls"): "list",
    ("repo", "new"): "create",
    ("ruleset", "ls"): "list",
    ("run", "ls"): "list",
    ("secret", "ls"): "list",
    ("secret", "remove"): "delete",
    ("skill", "add"): "install",
    ("skill", "show"): "preview",
    ("ssh-key", "ls"): "list",
    ("variable", "ls"): "list",
    ("variable", "remove"): "delete",
    ("workflow", "ls"): "list",
}

#: msg-9821 §2: the allowed rows (R-a, R-b, and R-c's one exception ``auth setup-git``). A
#: ``(group, sub)`` not here is denied. ``auth status`` is judged on its own (:func:`_judge_auth`).
_PR_ALLOW = (
    "list",
    "view",
    "diff",
    "checks",
    "status",
    "checkout",
    "ready",
    "update-branch",
    "close",
    "reopen",
    "merge",
)
L2_ALLOW: frozenset[tuple[str, str]] = frozenset(
    {
        *(("pr", s) for s in _PR_ALLOW),
        *(("issue", s) for s in ("list", "view", "status", "close", "reopen")),
        *(("run", s) for s in ("list", "view", "watch", "download")),
        ("workflow", "list"),
        ("workflow", "view"),
        ("repo", "view"),
        ("repo", "clone"),
        *(("release", s) for s in ("list", "view", "download")),
        ("label", "list"),
        ("secret", "list"),
        ("cache", "list"),
        *(("ruleset", s) for s in ("list", "view", "check")),
        ("auth", "setup-git"),
    }
)

#: msg-9821 §2.1 (R-d): the argument shapes an allowed row accepts. The shapes restrict the *form*
#: of a value only; they do not judge whether the value is safe to send (msg-9823 §1).
_L2_NUMBER = re.compile(r"[0-9]+")
_L2_NAME = re.compile(r"[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)?")
_L2_OWNER_REPO = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+")
_L2_JSON_FIELDS = re.compile(r"[A-Za-z0-9_]+(?:,[A-Za-z0-9_]+)*")
_L2_STATES = frozenset({"open", "closed", "merged", "all"})


def _l2_number(v: str) -> bool:
    return _L2_NUMBER.fullmatch(v) is not None


def _l2_name(v: str) -> bool:
    return _L2_NAME.fullmatch(v) is not None


def _l2_owner_repo(v: str) -> bool:
    return _L2_OWNER_REPO.fullmatch(v) is not None


def _l2_json_fields(v: str) -> bool:
    return _L2_JSON_FIELDS.fullmatch(v) is not None


def _l2_state(v: str) -> bool:
    return v in _L2_STATES


def _l2_any(_v: str) -> bool:
    return True


#: Value-taking options every allowed row accepts, with the shape of their value.
#: ``--jq`` / ``--template`` only format the response locally, so any literal value is accepted.
_L2_COMMON_VAL: dict[str, Callable[[str], bool]] = {
    "-R": _l2_owner_repo,
    "--repo": _l2_owner_repo,
    "--json": _l2_json_fields,
    "--jq": _l2_any,
    "--template": _l2_any,
    "-L": _l2_number,
    "--limit": _l2_number,
}
#: ``list`` rows also take ``-s``/``--state`` with a fixed word.
_L2_STATE_VAL: dict[str, Callable[[str], bool]] = {"-s": _l2_state, "--state": _l2_state}
_L2_ARTIFACT_VAL: dict[str, Callable[[str], bool]] = {
    "-n": _l2_name,
    "--name": _l2_name,
    "-p": _l2_name,
    "--pattern": _l2_name,
}
#: Row-specific value-taking options (msg-9821 §2.1 "行ごとに足す option"). ``-D``/``--dir`` is
#: deliberately absent.
_L2_ROW_VAL: dict[tuple[str, str], dict[str, Callable[[str], bool]]] = {
    ("run", "download"): _L2_ARTIFACT_VAL,
    ("release", "download"): _L2_ARTIFACT_VAL,
}
#: Row-specific flags (no value).
_L2_ROW_FLAGS: dict[tuple[str, str], frozenset[str]] = {
    ("pr", "merge"): frozenset(
        {"--squash", "--merge", "--rebase", "-d", "--delete-branch", "--auto"}
    ),
    ("pr", "checks"): frozenset({"--watch", "--exit-status"}),
    ("run", "watch"): frozenset({"--watch", "--exit-status"}),
    ("run", "view"): frozenset({"--log-failed"}),
}
#: Options that send search text (msg-9819 §3 puts ``--search`` under ``gh_search_text``).
_L2_SEARCH_OPTS = frozenset({"-S", "--search"})
#: ``auth status`` is allowed bare or with exactly these; anything else may print the token.
_L2_AUTH_STATUS_OK = frozenset({"-a", "--active"})
#: Programs that run their arguments as a command (``env gh auth token``): a ``gh`` word among
#: their arguments is a gh call whose shape the table cannot see, so it is denied.
LAUNCHERS = frozenset(
    {
        "env",
        "command",
        "builtin",
        "exec",
        "time",
        "nice",
        "nohup",
        "xargs",
        "sudo",
        "timeout",
        "stdbuf",
        "setsid",
    }
)


def _is_gh_name(word: str) -> bool:
    return word.replace("\\", "/").rsplit("/", 1)[-1].lower() in GH_NAMES


def _row_args_ok(row: tuple[str, str], args: list[str]) -> str | None:
    """``None`` when every argument of an allowed row has an accepted shape (R-d), otherwise the
    denying class key. Nothing taken from ``args`` is ever returned."""
    vals = dict(_L2_COMMON_VAL)
    if row[1] == "list":
        vals.update(_L2_STATE_VAL)
    vals.update(_L2_ROW_VAL.get(row, {}))
    flags = _L2_ROW_FLAGS.get(row, frozenset())
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("-") and a != "-":
            key, att = a, None
            if a.startswith("--") and "=" in a:
                key, att = a.split("=", 1)
            if key in _L2_SEARCH_OPTS or (
                not a.startswith("--") and len(a) > 2 and a[:2] in _L2_SEARCH_OPTS
            ):
                return "gh_search_text"
            if key in flags and att is None:
                i += 1
                continue
            check = vals.get(key)
            if check is None:
                return "gh_not_in_table"
            if att is None:
                if i + 1 >= len(args):
                    return "gh_not_in_table"
                i += 1
                att = args[i]
            if not check(att):
                return "gh_not_in_table"
        elif not (_l2_number(a) or _l2_name(a)):
            return "gh_not_in_table"
        i += 1
    return None


def _judge_auth(sub: str, args: list[str]) -> str | None:
    if sub == "token":
        return "gh_auth_token"
    if sub == "status":
        return None if all(a in _L2_AUTH_STATUS_OK for a in args) else "gh_auth_token"
    if sub == "setup-git":
        return _row_args_ok(("auth", sub), args)
    return "gh_not_in_table"


def judge_table(argv: list[str]) -> str | None:
    """L-p2 verdict for one ``gh`` call outside the gh-write definition (msg-9821 §2).

    ``argv`` holds literal values with ``argv[0]`` the ``gh`` name. ``None`` = allowed; otherwise
    the denying class key. Options before the group or the sub, a missing group or sub, ``api``
    outside the write definition (Obj-26-1 cut) and anything else not on the table are
    ``gh_not_in_table`` (fail-closed).
    """
    if len(argv) < 2:
        return "gh_not_in_table"  # `gh` alone
    group = GH_GROUP_ALIASES.get(argv[1], argv[1])
    if group.startswith("-"):
        return "gh_not_in_table"  # `gh --version`, `gh -R o/r pr view 1`
    if group == "search":
        return "gh_search_text"
    if group == "variable":
        return "gh_secret_value"
    if len(argv) < 3 or argv[2].startswith("-"):
        return "gh_not_in_table"  # `gh pr`, `gh api /user`, `gh pr -R o/r view 1`
    sub = GH_SUB_ALIASES.get((group, argv[2]), argv[2])
    args = argv[3:]
    if group == "secret" and sub in ("set", "delete"):
        return "gh_secret_value"
    if group == "auth":
        return _judge_auth(sub, args)
    if (group, sub) not in L2_ALLOW:
        return "gh_not_in_table"
    return _row_args_ok((group, sub), args)


# ---------------------------------------------------------------------- combining the verdicts
@dataclass(frozen=True)
class Verdict:
    """One ``gh`` node's verdict. ``key`` is the denying class, ``None`` when allowed."""

    allowed: bool
    key: str | None = None


ALLOW = Verdict(True)


def combine(verdicts: Sequence[Verdict]) -> Verdict:
    """msg-9821 §1 step 3 / msg-9823 Obj-27-1: the whole command's verdict from every ``gh``
    node's verdict, in node order. Any deny denies the whole command and the first deny decides
    the class; an allow never ends the judgement early. No node at all is an allow."""
    for v in verdicts:
        if not v.allowed:
            return v if v.key is not None else Verdict(False, "guard_error")
    return ALLOW


_READ_METHODS = frozenset({"GET", "HEAD"})
_MERGE_SUBJECT_OPTS = frozenset({"-t", "--subject"})


def _write_overlay(argv: list[str]) -> str | None:
    """The L-p2 deny a node inside the gh-write definition still gets, whatever L-p says of it.

    Two shapes the L-p allowlist admits carry free text the L-p2 table rules out:

    * ``gh api`` with an explicit read method (``--method GET -f probe=...``): the fields become
      the query of a GET, so it is the ``api`` read that Obj-26-1 cut denies (msg-9821 §4);
    * ``gh pr merge`` with ``-t``/``--subject``: msg-9819 §2 / §4 deny it, as search-class text
      (msg-9819 §3).
    """
    if len(argv) < 2:
        return None
    if argv[1] == "api":
        for opt, val in _opt_words(argv[2:], _val_set("api", "")):
            if opt in ("-X", "--method") and val is not None and val.upper() in _READ_METHODS:
                return "gh_not_in_table"
        return None
    if argv[1:3] == ["pr", "merge"]:
        for opt, _ in _opt_words(argv[3:], _val_set("pr", "merge")):
            if opt in _MERGE_SUBJECT_OPTS:
                return "gh_search_text"
    return None


def _opt_words(words: list[str], vals: frozenset[str]) -> list[tuple[str, str | None]]:
    """``(option, value)`` pairs of ``words``, read with the option/value boundaries
    :func:`_parse_items` uses (PR-gate on #606): a value-taking option (``vals``) consumes the next
    word, so a value that looks like an option (``--body '--subject=x'``, ``-f '--method=GET'``)
    is never itself read as one. ``--opt=value`` and an attached short value (``-XGET``) are split;
    ``--`` ends the options. A flag (not in ``vals``) has the value ``None``."""
    out: list[tuple[str, str | None]] = []
    i = 0
    while i < len(words):
        v = words[i]
        if v == "--":
            break
        if not v.startswith("-") or v == "-":
            pass
        elif v.startswith("--") and "=" in v:
            k, a = v.split("=", 1)
            out.append((k, a))
        elif v in vals:
            out.append((v, words[i + 1] if i + 1 < len(words) else None))
            i += 1
        elif not v.startswith("--") and len(v) > 2 and v[:2] in vals:
            out.append((v[:2], v[2:]))
        else:
            out.append((v, None))
        i += 1
    return out


def _node_verdicts(cmd: str, write: bool, write_key: str | None) -> list[Verdict]:
    """Every ``gh`` node's verdict, in order. A node inside the gh-write definition carries the
    L-p verdict of the whole command (``write_key``; ``None`` = allowed); every other node is
    judged by the table. A gh write the tree cannot place (inside a wrapper shell) goes first."""
    old = Verdict(write_key is None, write_key)
    src = cmd.encode("utf-8", "replace")
    try:
        root: Node | None = parse(src)
        usable = root is not None and not root.has_error and gap_scan(root, src)[0]
    except Exception:
        root, usable = None, False
    if root is None or not usable:
        # indeterminate: deny on a `gh` word, as the L-p path does (objection 4)
        if write:
            return [old]
        return [Verdict(False, "gh_not_in_table")] if maybe_gh(cmd) else []
    out: list[Verdict] = []
    placed = False
    for n in walk(root):
        if n.type != "command":
            continue
        name = cmd_name(n)
        if name is None:
            continue
        arg_nodes = list(n.children_by_field_name("argument"))
        argv = [value(name)] + [value(a) for a in arg_nodes]
        if _is_gh_name(argv[0]):
            argv[0] = "gh"
            if gh_write(argv):
                out.append(old)
                placed = True
                over = _write_overlay(argv)
                if over is not None:
                    out.append(Verdict(False, over))
            elif not literal(name) or not all(literal(a) for a in arg_nodes):
                out.append(Verdict(False, "gh_not_in_table"))
            else:
                key = judge_table(argv)
                out.append(Verdict(key is None, key))
            continue
        base = argv[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
        if (base in WRAPPER_SHELLS and any(maybe_gh(a) for a in argv[1:])) or (
            base in LAUNCHERS and any(_is_gh_name(a) for a in argv[1:])
        ):
            out.append(Verdict(False, "gh_not_in_table"))
    if write and not placed:
        out.insert(0, old)
    return out


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
#: msg-9819 §3 (kept by msg-9821 §3): the L-p2 texts, one per class whatever the family. The
#: shared last two sentences ask for a placeholder command shape and forbid pasting the token or
#: the output (C-42, Obj-25-1).
_L2_REQUEST = (
    "To ask for this command to be added to the allowed table, post in the thread the command "
    "shape with every real value (host names, paths, tokens, IDs) replaced by a placeholder, why "
    "it is needed, and why no allowed form works. Do not paste the token or the command's output."
)
_L2_TEXTS: dict[str, str] = {
    "gh_auth_token": (
        "L-p denied this gh call [gh_auth_token]: printing a GitHub token into the session is "
        "not allowed. If you need it for git authentication, run `gh auth setup-git` once in its "
        "own Bash call and then use git as usual. If your use cannot be replaced that way, stop "
        "and raise it to a human in the thread (`NEXT: human`). " + _L2_REQUEST
    ),
    "gh_secret_value": (
        "L-p denied this gh call [gh_secret_value]: reading or changing repository variables "
        "and secrets is not allowed. " + _L2_REQUEST
    ),
    "gh_search_text": (
        "L-p denied this gh call [gh_search_text]: sending search text to GitHub is not "
        "allowed. " + _L2_REQUEST
    ),
    "gh_not_in_table": (
        "L-p denied this gh call [gh_not_in_table]: this gh call, or one of its arguments, is "
        "not in the allowed table. " + _L2_REQUEST
    ),
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
    """The fixed deny reason for ``(class key, family)`` — built only from constants. An L-p2
    class has one text whatever the family (msg-9819 §3)."""
    if key in _L2_TEXTS:
        return _L2_TEXTS[key]
    reason = _CLASS_REASON.get(key, _CLASS_REASON["other"])
    guide = _GUIDES.get(family, _GUIDE_UNKNOWN)
    return f"L-p denied this gh write [{key}]: {reason}. {guide}"


def decide(cmd: object) -> tuple[bool, str | None]:
    """``(allowed, deny_text_or_None)`` for one Bash ``command`` value. Never raises.

    msg-9821 §1: every ``gh`` node is judged — a node inside the gh-write definition by the nine
    L-p classes, any other by the L-p2 table — and :func:`combine` folds the verdicts; one deny
    denies the whole command.
    """
    try:
        if not isinstance(cmd, str):
            return False, deny_text(GUARD_ERROR_KEY, FAMILY_UNKNOWN)
        write = is_gh_write(cmd)
        write_key: str | None = None
        family = FAMILY_UNKNOWN
        if write:
            family = _family_of(cmd)
            try:
                cls = classify(cmd)
            except Exception:
                cls = "ts_parse_fail"
            write_key = None if cls in ALLOWED_CLASSES else cls
        try:
            verdicts = _node_verdicts(cmd, write, write_key)
        except Exception:
            verdicts = [Verdict(False, GUARD_ERROR_KEY)]
        v = combine(verdicts)
        if v.allowed:
            return True, None
        return False, deny_text(v.key or GUARD_ERROR_KEY, family)
    except Exception:
        return False, deny_text(GUARD_ERROR_KEY, FAMILY_UNKNOWN)


# --------------------------------------------------------------------------- P-1 prompt text
#: msg-10012 Obj-28-1: the placeholders a P-1 example template may carry, with the text the prompt
#: shows for each (C-42: placeholders only, never a real value) and the fixed synthetic value the
#: tests substitute before running the example through :func:`decide`.
P1_PLACEHOLDERS: dict[str, tuple[str, str]] = {
    "{n}": ("<n>", "1"),
    "{owner_repo}": ("owner/repo", "o/r"),
    "{branch}": ("<branch>", "feature-x"),
    "{text}": ("<text>", "foo"),
}

#: msg-10012 Obj-28-1 / Obj-28-2: one or more allowed examples per :data:`L2_ALLOW` row, as
#: ``(row, template)``. The prompt's (iv) list is built from these, and the tests run every one
#: of them through :func:`decide` and check that every row has at least one.
P1_EXAMPLES_ALLOW: tuple[tuple[tuple[str, str], str], ...] = (
    (("pr", "list"), "gh pr list -R {owner_repo} -s open"),
    (("pr", "view"), "gh pr view {n} -R {owner_repo}"),
    (("pr", "diff"), "gh pr diff {n} -R {owner_repo}"),
    (("pr", "checks"), "gh pr checks {n} -R {owner_repo}"),
    (("pr", "status"), "gh pr status -R {owner_repo}"),
    (("pr", "checkout"), "gh pr checkout {n} -R {owner_repo}"),
    (("pr", "ready"), "gh pr ready {n} -R {owner_repo}"),
    (("pr", "update-branch"), "gh pr update-branch {n} -R {owner_repo}"),
    (("pr", "close"), "gh pr close {n} -R {owner_repo}"),
    (("pr", "reopen"), "gh pr reopen {n} -R {owner_repo}"),
    (("pr", "merge"), "gh pr merge {n} -R {owner_repo} --squash"),
    (("issue", "list"), "gh issue list -R {owner_repo} -s open"),
    (("issue", "view"), "gh issue view {n} -R {owner_repo}"),
    (("issue", "status"), "gh issue status -R {owner_repo}"),
    (("issue", "close"), "gh issue close {n} -R {owner_repo}"),
    (("issue", "reopen"), "gh issue reopen {n} -R {owner_repo}"),
    (("run", "list"), "gh run list -R {owner_repo} -L 20"),
    (("run", "view"), "gh run view {n} -R {owner_repo} --log-failed"),
    (("run", "watch"), "gh run watch {n} -R {owner_repo} --exit-status"),
    (("run", "download"), "gh run download {n} -R {owner_repo}"),
    (("workflow", "list"), "gh workflow list -R {owner_repo}"),
    (("workflow", "view"), "gh workflow view {n} -R {owner_repo}"),
    (("repo", "view"), "gh repo view {owner_repo}"),
    (("repo", "clone"), "gh repo clone {owner_repo}"),
    (("release", "list"), "gh release list -R {owner_repo}"),
    (("release", "view"), "gh release view -R {owner_repo}"),
    (("release", "download"), "gh release download -R {owner_repo}"),
    (("label", "list"), "gh label list -R {owner_repo}"),
    (("secret", "list"), "gh secret list -R {owner_repo}"),
    (("cache", "list"), "gh cache list -R {owner_repo}"),
    (("ruleset", "list"), "gh ruleset list -R {owner_repo}"),
    (("ruleset", "view"), "gh ruleset view {n} -R {owner_repo}"),
    (("ruleset", "check"), "gh ruleset check {branch} -R {owner_repo}"),
    (("auth", "setup-git"), "gh auth setup-git"),
)

#: msg-10012 Obj-28-1: denied examples, as ``(template, expected class)``. The prompt shows them
#: as shapes not to use; the tests check that each is denied with that class.
P1_EXAMPLES_DENY: tuple[tuple[str, str], ...] = (
    ("gh pr list -R {owner_repo} --search {text}", "gh_search_text"),
    ("gh pr list -R {owner_repo} -S {text}", "gh_search_text"),
    ("gh search prs {text}", "gh_search_text"),
    ('bash -c "gh pr view {n}"', "gh_not_in_table"),
    ("env gh pr view {n}", "gh_not_in_table"),
    ("gh -R {owner_repo} pr view {n}", "gh_not_in_table"),
    ("gh --version", "gh_not_in_table"),
    ("gh api repos/{owner_repo}/pulls/{n}", "gh_not_in_table"),
    ("gh auth token", "gh_auth_token"),
    ("gh variable list -R {owner_repo}", "gh_secret_value"),
)


def p1_render(template: str, synthetic: bool = False) -> str:
    """``template`` with each placeholder replaced by its prompt text, or (``synthetic``) by the
    fixed synthetic value the tests run through :func:`decide`."""
    out = template
    for ph, (shown, synth) in P1_PLACEHOLDERS.items():
        out = out.replace(ph, synth if synthetic else shown)
    return out


def _p1_reads_block() -> str:
    """P-1 (iv): one line per :data:`L2_ALLOW` row with its allowed examples, then the denied
    examples — all built from constants (msg-10010 P-e, msg-10012 Obj-28-2)."""
    rows = []
    for row in sorted(L2_ALLOW):
        shapes = "; ".join(f"`{p1_render(t)}`" for r, t in P1_EXAMPLES_ALLOW if r == row)
        rows.append(f"- `{row[0]} {row[1]}`: {shapes}\n")
    denied = "; ".join(f"`{p1_render(t)}` [{k}]" for t, k in P1_EXAMPLES_DENY)
    return (
        "(iv) Every other `gh` call, reads included: only the group and subcommand rows below are "
        "allowed, and anything else is denied (a read through `gh api` included). The kind of "
        "argument and the place it goes are fixed per subcommand: use the shape of that row's "
        "example. `-S`/`--search` is never allowed. A `gh` run through a wrapper or launcher "
        "(`bash -c`, `env`, `xargs`, `command`, `time` and the like) is denied, and so is any "
        "option placed before the group or the subcommand. A row here is only what this guard "
        "admits, not a permission: every other rule you are given still applies.\n"
        + "".join(rows)
        + f"Denied shapes (with the class the guard answers): {denied}.\n"
        "If a call is denied, do what the deny text says; do not reshape the command to get it "
        "through.\n"
    )


#: P-1 (msg-9299 §1, msg-9301 §4; updated for L-p2 by msg-10010 P-a..P-e and msg-10012): the
#: guidance the implementer and proposer system prompts carry. Kept here, with the (iv) list built
#: from :data:`L2_ALLOW` and the example constants above, so the prompt and the guard it describes
#: cannot drift apart.
P1_PROMPT_BLOCK = """\
GH CALLS: every Bash call that runs `gh` passes a guard (L-p / L-p2) that judges \
every `gh` in the command and denies, before the command runs, any shape it does \
not list.
(i) `gh pr create|comment|edit|review|merge` and `gh issue create|comment|edit`: \
run that one command alone in its own Bash call — never chain it with `cd`, \
`git`, `|`, `;`, `&&` or a newline. Pass the body with `--body-file -` and a \
heredoc whose delimiter is quoted (`<<'EOF'`). Name the repository with \
`-R owner/repo` instead of `cd`; do `git push` and any other preparation in an \
earlier, separate Bash call. `gh pr create` also accepts `--base`/`--head` \
(a plain branch name only).
(ii) `gh api`: for a write only (`-X`/`--method` other than GET). Alone in its \
own Bash call, every value a literal `-f`/`-F` (no value starting with `@`), the \
owner/repo written into the endpoint. This is on the allowlist but unproven in \
practice. Do not use `gh api` to read: read with the subcommands in (iv).
(iii) `gh gist create|edit` and `gh release create|upload` are always denied and \
have no allowed form. If one is needed, stop and say why in the thread.
""" + _p1_reads_block()


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
    "L2_ALLOW",
    "L2_CLASSES",
    "P1_EXAMPLES_ALLOW",
    "P1_EXAMPLES_DENY",
    "P1_PLACEHOLDERS",
    "P1_PROMPT_BLOCK",
    "Verdict",
    "classify",
    "classify_ex",
    "combine",
    "decide",
    "deny_text",
    "gap_scan",
    "gh_write_guard_hooks",
    "gh_write_kind",
    "gh_write_pretooluse",
    "is_gh_write",
    "judge_table",
    "p1_render",
    "parse",
]
