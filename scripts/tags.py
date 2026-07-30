"""Canonical cost-allocation tag for every Ripple-owned AWS resource.

One definition, three shapes — AWS tag APIs disagree on wire format:
  * map               {"key": "value"}           -> agentcore-control, logs
  * list of Key/Value [{"Key":..,"Value":..}]    -> ECR, IAM, Secrets Manager
  * list of key/value [{"key":..,"value":..}]    -> CodeBuild (lowercase, alone)

The key/value are committed constants (not in dev.env) on purpose: a cost
allocation tag only aggregates if it is byte-identical on every resource, so it
must not drift per-shell. Override is still possible via the environment for a
second project/stack sharing this code.
"""
import os

TAG_KEY = os.environ.get("PROJECT_TAG_KEY") or "project_name"
TAG_VALUE = os.environ.get("PROJECT_TAG_VALUE") or "ripple_slack_assistant"


def as_map() -> dict[str, str]:
    """agentcore-control (`tags`), CloudWatch Logs (`tags`)."""
    return {TAG_KEY: TAG_VALUE}


def as_upper_list() -> list[dict[str, str]]:
    """ECR (`tags`), IAM (`Tags`), Secrets Manager (`Tags`)."""
    return [{"Key": TAG_KEY, "Value": TAG_VALUE}]


def as_lower_list() -> list[dict[str, str]]:
    """CodeBuild (`tags`) — the one service using lowercase members."""
    return [{"key": TAG_KEY, "value": TAG_VALUE}]
