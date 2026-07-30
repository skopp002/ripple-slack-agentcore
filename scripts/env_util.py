"""Strict environment-variable access. No baked-in defaults.

If a required variable is not set, raise a clear usage error telling the caller
to `source dev.env` (or export it). Keeps deployment config out of the code.
"""
import os
import sys


class ConfigError(SystemExit):
    """Exit with a usage-style message when required config is missing."""


def require_env(name: str) -> str:
    val = os.environ.get(name)
    if val is None or val == "":
        sys.stderr.write(
            f"\nConfig error: required environment variable '{name}' is not set.\n"
            f"Fix: copy dev.env.example -> dev.env, fill it, then `source dev.env`\n"
            f"(secrets like GITHUB_CLIENT_SECRET go in your shell only).\n\n"
        )
        raise ConfigError(2)
    return val


def optional_env(name: str) -> str | None:
    v = os.environ.get(name)
    return v if v else None
