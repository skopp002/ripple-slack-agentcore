"""Strict environment-variable access. No baked-in defaults.

If a required variable is not set, raise a clear usage error telling the caller
to `source dev.env` (or export it). Keeps deployment config out of the code.

WHERE THIS SITS IN THE ARCHITECTURE DIAGRAM (infra/architecture-components.png):
nowhere — it creates no tile, configures no component and performs no step in either
flow. It is a two-function helper shared by the deploy scripts and by the CLI client,
which on the diagram is the "CLI client" tile outside the AWS account.

Its one architectural property is the refusal to default. Every value that names a
trust relationship on this canvas arrives through here: the IdP domain and audience
that the runtime's authorizer and its in-agent JWT re-verification (step 6a) are both
derived from, the runtime ARN the CLI invokes at step 4a, and the secret ARNs behind
the Secrets Manager tile. A baked-in default for any of those is not a convenience,
it is a silent substitution of one identity provider or one credential for another —
and the failure would surface as a working system pointed at the wrong tenant rather
than as an error. Hence a usage message instead of a fallback.
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
