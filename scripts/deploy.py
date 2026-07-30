#!/usr/bin/env python3
"""Deploy Ripple end to end via CloudFormation. One command, no copy-paste.

    source dev.env
    python3 scripts/deploy.py                    # full deploy
    python3 scripts/deploy.py --image-only       # rebuild + roll the image only
    python3 scripts/deploy.py --dry-run          # show what would run

REPLACES THE STARTER TOOLKIT. The previous version of this file called
`bedrock_agentcore_starter_toolkit.Runtime.configure/launch`; that package is
slated for deprecation, and it created resources (runtime, endpoint, ECR repo,
CodeBuild project) outside any stack, so they could not be declared, drift-checked
or deleted as a unit — and had to be tagged after the fact by apply_tags.py.

Everything is now CloudFormation:

    01-foundation.yaml   S3 + ECR + CodeBuild        (build plane)
    cfn_build.py         source.zip -> CodeBuild -> ECR image   <- not declarative:
                         CFN cannot build a container image, and
                         AWS::BedrockAgentCore::Runtime needs a ContainerUri that
                         already exists. This is the ONE imperative step, and it
                         creates nothing — it only puts an image in a declared repo.
    02-runtime.yaml      Memory + Identity provider + role + Runtime + Endpoint

This script only sequences those three and threads the image tag between them.
It creates no AWS resource itself, so `aws cloudformation deploy` by hand (see
infra/README.md) remains a fully supported equivalent — nothing here is required
magic.

NOT deprecated and still used: the `bedrock-agentcore` SDK, which the agent
imports at runtime (BedrockAgentCoreApp, @requires_access_token). Different
package from the starter toolkit; see requirements.txt.
"""
import argparse
import os
import subprocess
import sys

import boto3
from botocore.exceptions import ClientError

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from env_util import optional_env, require_env  # noqa: E402
import tags  # noqa: E402

# The consent return URL is owned by the CLIENT (it runs the loopback server that
# receives ?session_id=... and calls CompleteResourceTokenAuth), so it is imported
# from there rather than duplicated. It MUST be passed explicitly on every deploy:
# `aws cloudformation deploy` reuses the stored parameter value for anything not in
# --parameter-overrides, so a changed template Default is silently ignored on an
# existing stack. That bit us once — the runtime kept serving the old URL while the
# template said otherwise.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "client"))
from consent import CONSENT_RETURN_URL  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SOLUTION = os.path.dirname(HERE)
INFRA = os.path.join(SOLUTION, "infra")

FOUNDATION_STACK = optional_env("FOUNDATION_STACK") or "ripple-foundation"
RUNTIME_STACK = optional_env("RUNTIME_STACK") or "ripple-runtime"
AGENT_NAME = optional_env("AGENTCORE_AGENT_NAME") or "ripple"


def run(cmd: list[str], dry: bool, label: str) -> str:
    print(f"\n=== {label}")
    if dry:
        print("  (dry-run) " + " ".join(cmd))
        return ""
    # Streamed, not captured: a CFN deploy or CodeBuild wait can run minutes and
    # silence is indistinguishable from a hang.
    proc = subprocess.run(cmd, cwd=SOLUTION, text=True)
    if proc.returncode != 0:
        raise SystemExit(f"FAILED: {label} (exit {proc.returncode})")
    return ""


def deploy_stack(stack: str, template: str, params: dict, dry: bool) -> None:
    cmd = [
        "aws", "cloudformation", "deploy",
        "--stack-name", stack,
        "--template-file", os.path.join(INFRA, template),
        "--capabilities", "CAPABILITY_NAMED_IAM",
        "--region", REGION,
        # Stack-level tags cover resources added later; the templates also tag
        # each resource explicitly so an accidental stack-tag change cannot strip
        # the cost attribution. Belt and braces, deliberately.
        "--tags", f"{tags.TAG_KEY}={tags.TAG_VALUE}",
        "--no-fail-on-empty-changeset",
    ]
    if params:
        cmd.append("--parameter-overrides")
        cmd += [f"{k}={v}" for k, v in params.items()]
    run(cmd, dry, f"stack {stack} ({template})")


def build_image(dry: bool) -> str:
    """Run cfn_build.py and return the image tag it produced."""
    print("\n=== build image (CodeBuild -> ECR)")
    script = os.path.join(HERE, "cfn_build.py")
    if dry:
        print(f"  (dry-run) {sys.executable} {script}")
        return "DRYRUN-TAG"
    # Captured here (unlike deploy_stack) because the tag must be parsed out;
    # cfn_build.py prints its own phase progress, echoed below as it arrives.
    proc = subprocess.Popen(
        [sys.executable, script, "--foundation-stack", FOUNDATION_STACK,
         "--agent-name", AGENT_NAME],
        cwd=SOLUTION, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    tag = ""
    for line in proc.stdout:
        print(line, end="")
        if line.startswith("IMAGE_TAG="):
            tag = line.strip().split("=", 1)[1]
    if proc.wait() != 0:
        raise SystemExit("FAILED: image build")
    if not tag:
        raise SystemExit("Build succeeded but printed no IMAGE_TAG= line.")
    return tag


def preflight(dry: bool) -> None:
    """Fail fast on names that already exist OUTSIDE any stack.

    Why this is needed: the resources were originally created imperatively (by the
    starter toolkit and by hand), so several of the fixed names the templates
    declare are already taken in this account. CloudFormation will not adopt a
    pre-existing resource — it reports `... already exists` and rolls the stack
    back, mid-deploy, after some resources were created. Checking up front turns an
    opaque partial failure into an actionable list.

    Only FIXED names are checked. Anything the service names itself (the runtime
    id, the memory id suffix) cannot collide.
    """
    print("\n=== preflight: checking for pre-existing names")
    account = boto3.client("sts", region_name=REGION).get_caller_identity()["Account"]
    cfn = boto3.client("cloudformation", region_name=REGION)

    # A name inside a stack we are about to update is not a conflict — it is ours.
    managed: set[str] = set()
    for stack in (FOUNDATION_STACK, RUNTIME_STACK):
        try:
            for p in cfn.get_paginator("list_stack_resources").paginate(StackName=stack):
                for r in p["StackResourceSummaries"]:
                    if r.get("PhysicalResourceId"):
                        managed.add(r["PhysicalResourceId"])
        except ClientError:
            pass  # stack does not exist yet — nothing of ours is managed

    def _exists(fn, **kw) -> bool:
        try:
            fn(**kw)
            return True
        except ClientError:
            return False

    ecr = boto3.client("ecr", region_name=REGION)
    cb = boto3.client("codebuild", region_name=REGION)
    iam = boto3.client("iam", region_name=REGION)
    logs = boto3.client("logs", region_name=REGION)
    acp = boto3.client("bedrock-agentcore-control", region_name=REGION)
    s3 = boto3.client("s3", region_name=REGION)

    repo = f"bedrock-agentcore-{AGENT_NAME}"
    project = f"bedrock-agentcore-{AGENT_NAME}-builder"
    bucket = f"ripple-agentcore-build-sources-{account}-{REGION}"

    checks: list[tuple[str, str, bool]] = [
        ("01-foundation", f"s3 bucket {bucket}",
         _exists(s3.head_bucket, Bucket=bucket)),
        ("01-foundation", f"ecr repository {repo}",
         _exists(ecr.describe_repositories, repositoryNames=[repo])),
        ("01-foundation", f"codebuild project {project}",
         bool(cb.batch_get_projects(names=[project]).get("projects"))),
        ("01-foundation", f"iam role Ripple{AGENT_NAME}CodeBuildRole",
         _exists(iam.get_role, RoleName=f"Ripple{AGENT_NAME}CodeBuildRole")),
        ("01-foundation", f"log group /aws/codebuild/{project}",
         any(g["logGroupName"] == f"/aws/codebuild/{project}"
             for g in logs.describe_log_groups(
                 logGroupNamePrefix=f"/aws/codebuild/{project}").get("logGroups", []))),
        ("02-runtime", f"iam role Ripple{AGENT_NAME}RuntimeRole",
         _exists(iam.get_role, RoleName=f"Ripple{AGENT_NAME}RuntimeRole")),
        # Memory ids are `<Name>-<random>`, so match on the prefix before the
        # suffix — and CASE-INSENSITIVELY: CreateMemory rejected the template's
        # `rippleKnowledgeMemory` against an existing `RippleKnowledgeMemory`,
        # which cost a stack rollback. The service folds case; this check must too.
        ("02-runtime", f"memory named {AGENT_NAME}KnowledgeMemory",
         any(m.get("id", "").rsplit("-", 1)[0].lower()
             == f"{AGENT_NAME}KnowledgeMemory".lower()
             for m in acp.list_memories().get("memories", []))),
        ("02-runtime", f"agent runtime named '{AGENT_NAME}'",
         any(r.get("agentRuntimeName") == AGENT_NAME
             for r in acp.list_agent_runtimes().get("agentRuntimes", []))),
        ("02-runtime", f"oauth2 credential provider {AGENT_NAME}-github",
         any(p.get("name") == f"{AGENT_NAME}-github"
             for p in acp.list_oauth2_credential_providers()
             .get("credentialProviders", []))),
    ]

    conflicts = []
    managed_lc = [m.lower() for m in managed]
    for stack_label, what, exists in checks:
        # `managed` holds physical ids; match loosely and case-insensitively,
        # because an id may be an ARN, a bare name, or a name with a
        # service-generated suffix (Memory) depending on the resource type.
        name = what.rsplit(" ", 1)[-1].strip("'").lower()
        if exists and not any(name in m for m in managed_lc):
            conflicts.append((stack_label, what))
            print(f"  ⚠️  EXISTS outside a stack: {what}  ({stack_label})")
        elif exists:
            print(f"  ✅ {what} — already managed by the stack")
        else:
            print(f"  ✅ {what} — free")

    if not conflicts:
        print("  no conflicts.")
        return

    print(f"\n  {len(conflicts)} name(s) already exist outside CloudFormation.")
    print("  CFN cannot adopt them: the stack will fail with 'already exists' and")
    print("  roll back. Resolve each, then re-run. Options per resource:")
    print("    - delete it (see infra/README.md § 'Pre-deploy name collisions'), or")
    print("    - pick a different AGENTCORE_AGENT_NAME so every name is fresh.")
    print("\n  Note the ECR repo and CodeBuild project already carry the correct")
    print("  cost tag — they are OUR earlier toolkit-created resources, not another")
    print("  project's. Deleting them is safe; the image is rebuilt either way.")
    if dry:
        print("\n  (dry-run) continuing anyway.")
        return
    raise SystemExit(2)


def stack_outputs(stack: str) -> dict:
    cfn = boto3.client("cloudformation", region_name=REGION)
    try:
        outs = cfn.describe_stacks(StackName=stack)["Stacks"][0].get("Outputs", [])
    except ClientError as e:
        raise SystemExit(f"Cannot read outputs of '{stack}': {e}")
    return {o["OutputKey"]: o["OutputValue"] for o in outs}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--image-only", action="store_true",
                    help="rebuild the image and update the runtime; skip the "
                         "foundation stack (ImageTag is not create-only, so the "
                         "runtime updates in place)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the commands without running them")
    ap.add_argument("--skip-preflight", action="store_true",
                    help="skip the pre-existing-name check (it only prevents an "
                         "'already exists' rollback mid-deploy; skip if you know "
                         "the conflicts are yours and intend to fail)")
    ap.add_argument("--skip-tags", action="store_true",
                    help="skip apply_tags.py (the two service-created resources "
                         "CFN cannot declare stay untagged)")
    args = ap.parse_args()

    global REGION
    REGION = require_env("AWS_REGION")

    # Read every required value up front: a missing var should fail before we
    # start creating infrastructure, not halfway through.
    auth0_domain = require_env("AUTH0_DOMAIN")
    auth0_audience = require_env("AUTH0_AUDIENCE")
    model_id = require_env("BEDROCK_MODEL_ID")
    github_client_id = require_env("GITHUB_CLIENT_ID")
    github_scopes = require_env("GITHUB_SCOPES")
    # Preferred path: the client secret lives in Secrets Manager and only its ARN
    # is passed, so the secret never enters the template or CloudFormation
    # history. See infra/README.md § "The GitHub client secret".
    secret_arn = optional_env("GITHUB_CLIENT_SECRET_ARN") or ""

    print(f"Region {REGION} · agent '{AGENT_NAME}' · "
          f"stacks '{FOUNDATION_STACK}' + '{RUNTIME_STACK}'")
    if not secret_arn:
        print("\n  NOTE: GITHUB_CLIENT_SECRET_ARN is not set. The runtime stack "
              "will\n  need GithubClientSecret passed some other way "
              "(ClientSecretSource=MANAGED).\n  Prefer the ARN route: "
              "infra/README.md § 'The GitHub client secret'.")

    # --image-only updates an existing stack, so pre-existing names are expected.
    if not args.image_only and not args.skip_preflight:
        preflight(args.dry_run)

    if not args.image_only:
        deploy_stack(FOUNDATION_STACK, "01-foundation.yaml",
                     {"AgentName": AGENT_NAME,
                      "ProjectTagValue": tags.TAG_VALUE},
                     args.dry_run)

    image_tag = build_image(args.dry_run)

    runtime_params = {
        "FoundationStackName": FOUNDATION_STACK,
        "AgentName": AGENT_NAME,
        "ImageTag": image_tag,
        "ProjectTagValue": tags.TAG_VALUE,
        "Auth0Domain": auth0_domain,
        "Auth0Audience": auth0_audience,
        "BedrockModelId": model_id,
        "GithubClientId": github_client_id,
        "GithubScopes": github_scopes,
        "ConsentReturnUrl": CONSENT_RETURN_URL,
    }
    if secret_arn:
        runtime_params["GithubClientSecretArn"] = secret_arn
    deploy_stack(RUNTIME_STACK, "02-runtime.yaml", runtime_params, args.dry_run)

    if args.dry_run:
        print("\n(dry-run) would then print stack outputs and apply tags.")
        return 0

    out = stack_outputs(RUNTIME_STACK)
    print("\n=== outputs — put these in dev.env")
    print(f"  export RIPPLE_RUNTIME_ARN={out.get('RuntimeArn', '?')}")
    print(f"  export RIPPLE_RUNTIME_QUALIFIER={out.get('EndpointName', '?')}")
    print(f"  export AGENTCORE_MEMORY_ID={out.get('MemoryId', '?')}")

    # The callback URL changes whenever the credential provider is REPLACED, and
    # a stale one breaks user auth with no error at deploy time. Always surfaced.
    callback = out.get("GithubCallbackUrl")
    if callback:
        print("\n=== ACTION: GitHub OAuth App callback URL")
        print(f"  {callback}")
        print("  Paste into the OAuth App's 'Authorization callback URL' if it "
              "changed.\n  It is reissued whenever the credential provider is "
              "replaced — see infra/README.md.")

    if not args.skip_tags:
        # Two resources cannot be declared: the runtime log group (its name embeds
        # the generated runtime id) and, under MANAGED, the Identity-created
        # secret. Everything else is tagged by the templates themselves.
        print(f"\n=== tag service-created resources ({tags.TAG_KEY}={tags.TAG_VALUE})")
        env = dict(os.environ)
        if out.get("RuntimeArn"):
            env["RIPPLE_RUNTIME_ARN"] = out["RuntimeArn"]
        if out.get("MemoryId"):
            env["AGENTCORE_MEMORY_ID"] = out["MemoryId"]
        env["AGENTCORE_AGENT_NAME"] = AGENT_NAME
        rc = subprocess.call([sys.executable, os.path.join(HERE, "apply_tags.py")],
                             env=env, cwd=SOLUTION)
        if rc != 0:
            print("WARNING: some resources could not be tagged. The DEPLOY "
                  "SUCCEEDED; re-run scripts/apply_tags.py to retry.")

    print("\nDone. Test with:  python3 client/ask.py \"What is our 2026 roadmap?\"")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
