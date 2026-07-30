#!/usr/bin/env python3
"""Apply the project cost-allocation tag to resources CloudFormation cannot tag.

SCOPE SHRANK WITH THE CFN MIGRATION. This script existed because the starter
toolkit had no `tags` parameter, so everything it created (runtime, endpoint, ECR
repo, CodeBuild project, roles) needed tagging after the fact. The CloudFormation
templates now tag every resource they declare, at creation, natively.

What genuinely remains, and why each is undeclarable:
  - the RUNTIME LOG GROUP — its name embeds the runtime id CFN generates, and the
    service creates it on first invoke, so it cannot be declared up front;
  - the IDENTITY-CREATED SECRET holding the GitHub client secret, when
    ClientSecretSource=MANAGED (avoidable entirely: pass GithubClientSecretArn).

Everything else it touches is now belt-and-braces re-assertion, harmless because
every AWS tag API used here is an upsert. Idempotent by design: re-running is the
intended way to catch anything created out of band.

Resource discovery is by name/ARN derived from config, never by wildcard: this
AWS account is shared with unrelated AgentCore samples, and a broad sweep would
misattribute their cost to Ripple.

Usage:
    source dev.env
    python3 scripts/apply_tags.py [--dry-run]
"""
import os
import sys

import boto3
from botocore.exceptions import ClientError

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tags  # noqa: E402
from env_util import optional_env, require_env  # noqa: E402

REGION = require_env("AWS_REGION")
RUNTIME_ARN = require_env("RIPPLE_RUNTIME_ARN")
MEMORY_ID = require_env("AGENTCORE_MEMORY_ID")
GH_PROVIDER = require_env("GITHUB_CREDENTIAL_PROVIDER")

# The CodeBuild role is DECLARED by 01-foundation.yaml with a deterministic name,
# so it is derived rather than configured. AGENTCORE_CODEBUILD_ROLE_ARN is still
# honoured if set (a pre-CFN stack may use a hand-made role), but is no longer
# required — demanding it would break a clean CFN deploy that never defines it.
CODEBUILD_ROLE_ARN = optional_env("AGENTCORE_CODEBUILD_ROLE_ARN") or ""

# Agent name drives derived resource names (ECR repo, CodeBuild project, log
# groups). Must match the AgentName parameter given to both CFN stacks, or this
# script will look for resources that do not exist and report them as failures.
AGENT_NAME = optional_env("AGENTCORE_AGENT_NAME") or "ripple"

DRY_RUN = "--dry-run" in sys.argv

_ok, _skipped, _failed = [], [], []


def _report(label: str, arn: str, err: Exception | None = None) -> None:
    if err is None:
        _ok.append(label)
        print(f"  ✅ {label}")
        return
    code = err.response["Error"]["Code"] if isinstance(err, ClientError) else type(err).__name__
    # Service-managed resources reject caller tagging; that is not a failure of
    # this script, but it must be visible so the cost report gap is known.
    if code in ("AccessDeniedException", "AccessDenied", "InvalidRequestException",
                "ValidationException", "UnsupportedOperation"):
        _skipped.append(f"{label} ({code})")
        print(f"  ⚠️  {label} — not taggable by caller ({code})")
    else:
        _failed.append(f"{label} ({code})")
        print(f"  ❌ {label} — {code}: {err}")


def _tag(label: str, fn, **kwargs) -> None:
    arn = kwargs.get("resourceArn") or kwargs.get("SecretId") or kwargs.get("RoleName") or ""
    if DRY_RUN:
        print(f"  (dry-run) would tag {label} -> {arn}")
        return
    try:
        fn(**kwargs)
        _report(label, arn)
    except ClientError as e:
        _report(label, arn, e)


def main() -> int:
    sts = boto3.client("sts", region_name=REGION)
    account = sts.get_caller_identity()["Account"]

    print(f"Tagging {tags.TAG_KEY}={tags.TAG_VALUE}")
    print(f"account={account} region={REGION}\n")

    acp = boto3.client("bedrock-agentcore-control", region_name=REGION)
    ecr = boto3.client("ecr", region_name=REGION)
    cb = boto3.client("codebuild", region_name=REGION)
    iam = boto3.client("iam", region_name=REGION)
    logs = boto3.client("logs", region_name=REGION)
    sm = boto3.client("secretsmanager", region_name=REGION)

    # ---- AgentCore control plane (tags = flat map) ----
    print("AgentCore:")
    runtime_id = RUNTIME_ARN.rsplit("/", 1)[-1]
    _tag("runtime", acp.tag_resource, resourceArn=RUNTIME_ARN, tags=tags.as_map())

    # Runtime endpoints are separately billable/taggable resources.
    try:
        endpoints = acp.list_agent_runtime_endpoints(
            agentRuntimeId=runtime_id
        ).get("runtimeEndpoints", [])
    except ClientError as e:
        endpoints = []
        _report("runtime endpoints (list)", RUNTIME_ARN, e)
    for ep in endpoints:
        ep_arn = ep.get("agentRuntimeEndpointArn")
        if ep_arn:
            _tag(f"runtime endpoint {ep.get('name')}", acp.tag_resource,
                 resourceArn=ep_arn, tags=tags.as_map())

    memory_arn = f"arn:aws:bedrock-agentcore:{REGION}:{account}:memory/{MEMORY_ID}"
    _tag("memory", acp.tag_resource, resourceArn=memory_arn, tags=tags.as_map())

    provider_arn = (
        f"arn:aws:bedrock-agentcore:{REGION}:{account}"
        f":token-vault/default/oauth2credentialprovider/{GH_PROVIDER}"
    )
    _tag("github oauth2 credential provider", acp.tag_resource,
         resourceArn=provider_arn, tags=tags.as_map())

    # ---- Build/registry ----
    print("\nBuild + registry:")
    ecr_repo = f"bedrock-agentcore-{AGENT_NAME}"
    ecr_arn = f"arn:aws:ecr:{REGION}:{account}:repository/{ecr_repo}"
    _tag(f"ecr repository {ecr_repo}", ecr.tag_resource,
         resourceArn=ecr_arn, tags=tags.as_upper_list())

    # CodeBuild has no TagResource; tags go through UpdateProject, which REPLACES
    # the whole tag set — so read existing tags and merge instead of clobbering.
    cb_project = f"bedrock-agentcore-{AGENT_NAME}-builder"
    if DRY_RUN:
        print(f"  (dry-run) would tag codebuild project -> {cb_project}")
    else:
        try:
            projects = cb.batch_get_projects(names=[cb_project]).get("projects", [])
            existing = projects[0].get("tags", []) if projects else []
            merged = [t for t in existing if t.get("key") != tags.TAG_KEY]
            merged += tags.as_lower_list()
            cb.update_project(name=cb_project, tags=merged)
            _report(f"codebuild project {cb_project}", cb_project)
        except ClientError as e:
            _report(f"codebuild project {cb_project}", cb_project, e)

    # ---- IAM (Tags = [{Key,Value}]) ----
    # Both roles are now DECLARED and tagged by the templates
    # (Ripple<Agent>CodeBuildRole in 01-foundation, Ripple<Agent>RuntimeRole in
    # 02-runtime), so this is re-assertion only. It still matters for a stack
    # deployed before those tags existed.
    #
    # The old shared AgentCoreRuntimeRole is deliberately absent: it is used by
    # unrelated agents in this account (kb_agent, researcher_agent,
    # web_research_agent), which is exactly why CFN creates a project-scoped role
    # instead — a shared role can never carry a per-project cost tag.
    print("\nIAM:")
    # Derived from the template's naming convention when not explicitly set.
    cb_role = (CODEBUILD_ROLE_ARN.rsplit("/", 1)[-1] if CODEBUILD_ROLE_ARN
               else f"Ripple{AGENT_NAME}CodeBuildRole")
    if DRY_RUN:
        print(f"  (dry-run) would tag iam role -> {cb_role}")
    else:
        try:
            iam.tag_role(RoleName=cb_role, Tags=tags.as_upper_list())
            _report(f"iam role {cb_role}", cb_role)
        except ClientError as e:
            _report(f"iam role {cb_role}", cb_role, e)

    # ---- CloudWatch Logs (tags = flat map) ----
    print("\nCloudWatch Logs:")
    for ep in endpoints or [{"name": "DEFAULT"}]:
        lg = f"/aws/bedrock-agentcore/runtimes/{runtime_id}-{ep.get('name')}"
        _tag(f"log group {lg}", logs.tag_resource,
             resourceArn=f"arn:aws:logs:{REGION}:{account}:log-group:{lg}",
             tags=tags.as_map())
    cb_lg = f"/aws/codebuild/{cb_project}"
    _tag(f"log group {cb_lg}", logs.tag_resource,
         resourceArn=f"arn:aws:logs:{REGION}:{account}:log-group:{cb_lg}",
         tags=tags.as_map())

    # ---- Secrets Manager (service-managed; best effort) ----
    # AgentCore Identity creates this secret to hold the GitHub client secret.
    # It is owned by the service, so tagging may be refused — attempt and report.
    print("\nSecrets Manager:")
    prefix = f"bedrock-agentcore-identity!default/oauth2/{GH_PROVIDER}"
    found = False
    try:
        for page in sm.get_paginator("list_secrets").paginate():
            for s in page.get("SecretList", []):
                if s["Name"].startswith(prefix):
                    found = True
                    _tag(f"secret {s['Name']}", sm.tag_resource,
                         SecretId=s["ARN"], Tags=tags.as_upper_list())
    except ClientError as e:
        _report("secrets (list)", prefix, e)
    if not found:
        print(f"  – no secret matching {prefix}*")

    # ---- Resolved by the CFN migration ----
    # Both entries here used to be permanent gaps in the cost report, because the
    # toolkit created shared, account-level resources. The templates replaced each
    # with a project-scoped equivalent that is tagged at creation:
    print("\nNo longer cost-attribution gaps (CFN owns project-scoped versions):")
    print(f"  – s3://ripple-agentcore-build-sources-{account}-{REGION} "
          "— DEDICATED bucket declared in 01-foundation.yaml, replacing the "
          "toolkit's account-shared bedrock-agentcore-codebuild-sources-* bucket")
    print(f"  – iam role Ripple{AGENT_NAME}RuntimeRole — project-scoped, declared "
          "in 02-runtime.yaml, replacing the shared AgentCoreRuntimeRole")

    print(f"\nSummary: {len(_ok)} tagged, {len(_skipped)} skipped, {len(_failed)} failed")
    if _failed:
        print("Failed: " + ", ".join(_failed))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
