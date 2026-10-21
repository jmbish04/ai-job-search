#!/usr/bin/env python3
"""Resolve a credential by name: environment variable first, then the `tokens` CLI.

Shared by tools/core_resumes.py and tools/guardian.py. The value is never
printed, logged or written to disk - callers put it in an Authorization header
and nowhere else.

A missing credential is an error, never a fallback to another credential:
require_secret() raises with the name it looked for, so a wrong guess fails
loudly instead of silently sending the wrong key.
"""

import os
import shutil
import subprocess

_CACHE: dict[str, str] = {}


class SecretUnavailable(RuntimeError):
    pass


# Why the last lookup came back empty, by name. require_secret() reports it, so
# a lookup that failed is never reported as a credential that does not exist.
_LAST_ERROR: dict[str, str] = {}


def _from_cli(name: str) -> str:
    """One `tokens show` call. Returns the value, or "" with the reason recorded."""
    if not shutil.which("tokens"):
        _LAST_ERROR[name] = "the tokens CLI is not on PATH"
        return ""
    try:
        out = subprocess.run(
            ["tokens", "show", name, "--value-only"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _LAST_ERROR[name] = f"`tokens show` failed: {exc}"
        return ""
    value = out.stdout.strip() if out.returncode == 0 else ""
    if not value:
        # stderr only: stdout is where the value goes, and this text is printed.
        # The last line of a traceback is the cause (measured: `TimeoutError:
        # timed out` when the key store link is slow).
        lines = [ln for ln in out.stderr.splitlines() if ln.strip()]
        detail = lines[-1].strip()[:200] if lines else "no stderr (stdout not shown: it can hold the key)"
        _LAST_ERROR[name] = f"`tokens show` exited {out.returncode}: {detail}"
    return value


def get_secret(name: str) -> str | None:
    if name in _CACHE:
        return _CACHE[name]
    _LAST_ERROR.pop(name, None)
    value = os.environ.get(name, "").strip()
    if not value:
        # The CLI reaches the key store over the network and can miss once;
        # measured 2026-10-21: one empty lookup, the next three fine.
        value = _from_cli(name) or _from_cli(name)
    if not value:
        return None
    _LAST_ERROR.pop(name, None)
    _CACHE[name] = value
    return value


def require_secret(name: str) -> str:
    value = get_secret(name)
    if not value:
        reason = _LAST_ERROR.get(name)
        if reason:
            raise SecretUnavailable(f"{name} lookup failed after 2 tries: {reason}")
        raise SecretUnavailable(
            f"{name} is not set: export it, or store it with `tokens set {name} ...`"
        )
    return value
