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


def get_secret(name: str) -> str | None:
    if name in _CACHE:
        return _CACHE[name]
    value = os.environ.get(name, "").strip()
    if not value and shutil.which("tokens"):
        try:
            out = subprocess.run(
                ["tokens", "show", name, "--value-only"],
                capture_output=True, text=True, timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            out = None
        if out is not None and out.returncode == 0:
            value = out.stdout.strip()
    if not value:
        return None
    _CACHE[name] = value
    return value


def require_secret(name: str) -> str:
    value = get_secret(name)
    if not value:
        raise SecretUnavailable(
            f"{name} is not set: export it, or store it with `tokens set {name} ...`"
        )
    return value
