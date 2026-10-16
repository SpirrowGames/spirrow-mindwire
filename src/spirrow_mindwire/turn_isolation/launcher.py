"""The as-user launcher: run one command as ``[turn.isolation] user`` (unit 2a, S7).

It runs as the owner and stands in for the program it starts:
``mindwire-turn-launcher <program> [args...]``. The sequence is the one the design thread
settled (msg-7099, revised in msg-7104 / msg-7106 / msg-7108) with the spawn call replaced by
the human's decision B on PR #485 (``LogonUserW`` + ``CreateProcessAsUserW`` instead of
``CreateProcessWithLogonW``, so the child can inherit the stdio pipes). :func:`launch` is the
only place the sequence is spelled out:

0. Read both secrets itself, as the owner, from the owner's Credential Manager:
   ``mindwire/turn-user`` (the turn user's password) and ``mindwire/implementer-pat``. Neither
   ever arrives through argv, an environment variable, stdin or a log, and nothing outside
   sends them in. A missing one stops with ``turn user credential missing`` /
   ``implementer PAT credential missing``.
1. ``LogonUserW(..., LOGON32_LOGON_INTERACTIVE)`` — a missing "Allow log on locally" right
   fails here, before anything is built (msg-7106). The password is wiped as soon as this
   returns: the token is the only thing the spawn needs.
2. ``CreateEnvironmentBlock(token, bInherit=FALSE)`` — the turn user's own default
   environment (USERPROFILE, APPDATA, TEMP, localized system paths).
3. Overlay the allow-list: only the names declared for ``turn``; in 2a that is ``GH_TOKEN``.
   Nothing is read from the owner's environment.
4. Enable ``SeAssignPrimaryTokenPrivilege`` / ``SeIncreaseQuotaPrivilege`` in the launcher's
   own token (best effort), make inheritable duplicates of the launcher's stdin/stdout/stderr,
   then ``CreateProcessAsUserW(token, bInheritHandles=TRUE, CREATE_UNICODE_ENVIRONMENT |
   CREATE_SUSPENDED)`` with the explicit block (never ``NULL``), those duplicates through
   ``STARTF_USESTDHANDLES`` and ``lpDesktop = NULL``. A ``PROC_THREAD_ATTRIBUTE_HANDLE_LIST``
   limits inheritance to exactly those three duplicates. ``ERROR_PRIVILEGE_NOT_HELD`` stops
   with a reason naming the Local Security Policy right the owner account lacks.
5. Close the duplicates and the token, wipe the block.

Since the child is now a direct child of the launcher, it also starts inside whatever job holds
the launcher (``_sdk_job_hook``). Nested jobs are supported on Windows 8 and later.

Job object (msg-7104 objection 2): created at startup with ``KILL_ON_JOB_CLOSE`` and held only
here. The suspended child is assigned to it and resumed **only if the assignment succeeded**;
otherwise it is terminated and never runs. However the launcher dies, its job handle closes and
the OS kills the child and everything the child started.

Fail closed: a failure at any step starts nothing (or terminates what was started) and exits
:data:`~.errors.SECRETS_STOP_EXIT_CODE` with one ``turn-isolation: STOP <reason>`` line on
stderr. There is no fallback to a ``NULL`` or partial environment block.

Not wired yet (msg-7100 item 5): no adapter points ``cli_path`` here and ``claude_bg`` does not
route through it. That waits for the operator spike.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from .errors import (
    IMPLEMENTER_PAT_CREDENTIAL_MISSING,
    TURN_JOB_ASSIGNMENT_FAILED,
    TURN_USER_CREDENTIAL_MISSING,
    TurnIsolationStop,
)
from .win32 import (
    CREATE_SUSPENDED,
    CREATE_UNICODE_ENVIRONMENT,
    ERROR_ACCOUNT_DISABLED,
    ERROR_ACCOUNT_LOCKED_OUT,
    ERROR_LOGON_FAILURE,
    ERROR_LOGON_TYPE_NOT_GRANTED,
    ERROR_PRIVILEGE_NOT_HELD,
    LOGON32_LOGON_INTERACTIVE,
    SPAWN_PRIVILEGES,
    STARTF_USESTDHANDLES,
    CredentialReader,
    SecretBuffer,
    SpawnedProcess,
    Win32Api,
    Win32Error,
    default_api,
)

TURN_USER_CREDENTIAL = "mindwire/turn-user"
IMPLEMENTER_PAT_CREDENTIAL = "mindwire/implementer-pat"

# S5 names declared for the ``turn`` child, and their source in 2a. In 2b the manifest is the
# only source and the Credential Manager read goes away (the two are never used together).
TURN_ENV_FROM_CREDENTIAL: Mapping[str, str] = {"GH_TOKEN": IMPLEMENTER_PAT_CREDENTIAL}

SPAWN_CREATION_FLAGS = CREATE_UNICODE_ENVIRONMENT | CREATE_SUSPENDED
SPAWN_STARTUP_FLAGS = STARTF_USESTDHANDLES

_STEP_LOGON = "LogonUserW"
_STEP_ENV = "CreateEnvironmentBlock"
_STEP_SPAWN = "CreateProcessAsUserW"


def spawn_failure_reason(code: int, missing_privileges: Sequence[str]) -> str:
    """``CreateProcessAsUserW`` failed. 1314 names the owner-account rights that are missing."""
    if code == ERROR_PRIVILEGE_NOT_HELD:
        names = [n for n in SPAWN_PRIVILEGES if n in missing_privileges] or list(SPAWN_PRIVILEGES)
        rights = ", ".join(f"{SPAWN_PRIVILEGES[n]} ({n})" for n in names)
        return f"turn spawn failed: owner lacks privilege: {rights}"
    return f"turn spawn failed: {_STEP_SPAWN} ({code})"


_LOGON_REASONS = {
    ERROR_LOGON_TYPE_NOT_GRANTED: "logon right not granted (Allow log on locally)",
    ERROR_LOGON_FAILURE: "bad credential",
    ERROR_ACCOUNT_DISABLED: "account disabled/locked",
    ERROR_ACCOUNT_LOCKED_OUT: "account disabled/locked",
}


def logon_failure_reason(step: str, code: int) -> str:
    """msg-7106's mapping. 1385 / 1326 / 1331 / 1909 are named; anything else carries its code."""
    named = _LOGON_REASONS.get(code)
    if named is not None:
        return f"turn user logon failed: {named}"
    return f"turn user logon failed: {step} ({code})"


@dataclass(frozen=True)
class TurnUser:
    user: str
    domain: str

    @classmethod
    def parse(cls, configured: str) -> TurnUser:
        """``DOMAIN\\user`` or a bare local ``user`` (domain ``.``)."""
        domain, sep, user = configured.partition("\\")
        if not sep:
            return cls(user=configured, domain=".")
        return cls(user=user, domain=domain or ".")


def read_secret(api: CredentialReader, target: str, missing_reason: str) -> SecretBuffer:
    """Read a generic credential from the owner's Credential Manager or stop the tick."""
    try:
        secret = api.cred_read(target)
    except Win32Error as exc:
        raise TurnIsolationStop(f"{missing_reason}: {target} ({exc.code})") from None
    if secret is None or not secret.reveal_length():
        if secret is not None:
            secret.wipe()
        raise TurnIsolationStop(f"{missing_reason}: {target}")
    return secret


def build_turn_environment(
    default_env: Mapping[str, str], overlay: Mapping[str, str]
) -> dict[str, str]:
    """The turn user's default environment plus the allow-list. Nothing from ``os.environ``."""
    env = dict(default_env)
    env.update(overlay)
    return env


def launch(
    argv: Sequence[str],
    turn_user: TurnUser,
    *,
    api: Win32Api,
    current_directory: str,
) -> int:
    """Run ``argv`` as the turn user and return its exit code. Raises :class:`TurnIsolationStop`."""
    if not argv:
        raise TurnIsolationStop("turn launcher: no program to run")
    password = read_secret(api, TURN_USER_CREDENTIAL, TURN_USER_CREDENTIAL_MISSING)
    job = 0
    token = 0
    block: SecretBuffer | None = None
    stdio: tuple[int, int, int] = (0, 0, 0)
    spawned: SpawnedProcess | None = None
    try:
        overlay: dict[str, str] = {}
        for name, target in TURN_ENV_FROM_CREDENTIAL.items():
            secret = read_secret(api, target, IMPLEMENTER_PAT_CREDENTIAL_MISSING)
            overlay[name] = secret.reveal()
            secret.wipe()
        try:
            job = api.create_kill_on_close_job()
        except Win32Error as exc:
            raise TurnIsolationStop(f"turn job creation failed ({exc.code})") from None
        try:
            token = api.logon_user(
                turn_user.user, turn_user.domain, password, LOGON32_LOGON_INTERACTIVE
            )
        except Win32Error as exc:
            raise TurnIsolationStop(logon_failure_reason(_STEP_LOGON, exc.code)) from None
        finally:
            password.wipe()  # the token is all the spawn needs
        try:
            default_env = api.create_environment_block(token)
        except Win32Error as exc:
            raise TurnIsolationStop(logon_failure_reason(_STEP_ENV, exc.code)) from None
        block = SecretBuffer.environment_block(build_turn_environment(default_env, overlay))
        overlay.clear()
        missing_privileges = api.enable_privileges(list(SPAWN_PRIVILEGES))
        try:
            stdio = api.inheritable_std_handles()
        except Win32Error as exc:
            raise TurnIsolationStop(f"turn stdio handle duplication failed ({exc.code})") from None
        try:
            spawned = api.create_process_as_user(
                token=token,
                command_line=subprocess.list2cmdline(list(argv)),
                creation_flags=SPAWN_CREATION_FLAGS,
                environment=block,
                current_directory=current_directory,
                startup_flags=SPAWN_STARTUP_FLAGS,
                desktop=None,
                std_handles=stdio,
                inherit_handles=stdio,
            )
        except Win32Error as exc:
            raise TurnIsolationStop(spawn_failure_reason(exc.code, missing_privileges)) from None
    finally:
        # Step 5, on every path: the secrets are dead the moment the spawn call returns, and the
        # child holds its own copies of the stdio duplicates.
        for handle in stdio:
            api.close_handle(handle)
        api.close_handle(token)
        if block is not None:
            block.wipe()
        password.wipe()
        if spawned is None:
            api.close_handle(job)
    try:
        api.assign_process_to_job(job, spawned.process)
    except Win32Error:
        api.terminate_process(spawned.process, 1)
        _close_spawn(api, spawned)
        api.close_handle(job)
        raise TurnIsolationStop(TURN_JOB_ASSIGNMENT_FAILED) from None
    try:
        api.resume_thread(spawned.thread)
        return api.wait_exit_code(spawned.process)
    except Win32Error as exc:
        api.terminate_process(spawned.process, 1)
        raise TurnIsolationStop(f"turn launcher: {exc.api} failed ({exc.code})") from None
    finally:
        _close_spawn(api, spawned)
        api.close_handle(job)  # KILL_ON_JOB_CLOSE takes any grandchild still running with it


def _close_spawn(api: Win32Api, spawned: SpawnedProcess) -> None:
    api.close_handle(spawned.thread)
    api.close_handle(spawned.process)


def main(
    argv: Sequence[str] | None = None,
    *,
    api_factory: Callable[[], Win32Api] = default_api,
    configured_user: Callable[[], str | None] | None = None,
) -> int:
    """``mindwire-turn-launcher <program> [args...]``; returns the child's exit code."""
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        user = (configured_user or _configured_user)()
        if not user:
            raise TurnIsolationStop("turn launcher: [turn.isolation] user is not set")
        return launch(args, TurnUser.parse(user), api=api_factory(), current_directory=os.getcwd())
    except TurnIsolationStop as stop:
        print(stop.stop_line(), file=sys.stderr, flush=True)
        return stop.exit_code


def _configured_user() -> str | None:
    from ..config import load_settings

    return load_settings().turn.isolation.user


def entrypoint() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    entrypoint()
