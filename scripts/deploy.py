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
    03-gateways.yaml     Ingress Gateway + Tools Gateway + targets   <- OPT-IN,
                         via DEPLOY_GATEWAYS=true. Off by default because the
                         runtime is directly invocable without it and the default
                         route for every source is still in-process, so a gateway
                         that nothing uses is cost and surface with no benefit.
    04-harness.yaml      MANAGED AgentCore Harness (AWS runs the agent loop),
                         fully stack-managed: it also declares the Auth0 M2M
                         credential provider the harness uses for outbound gateway
                         auth, so nothing is created or patched outside the stack.
                         <- OPT-IN, via DEPLOY_HARNESS=true, and it needs
                         DEPLOY_GATEWAYS too because its only tool is the tools
                         gateway.

⚠️ DO NOT USE 04 (the harness) AS THE DEPLOYED AGENT FOR NOW — use the runtime route.
It is a deployed EXPERIMENT: InvokeHarness lets the caller override the system prompt and
tool allowlist, and it cannot serve Google Drive. docs/HARNESS-ASSESSMENT.md has the
measured evidence.

⚠️ THE STACKS ARE CUMULATIVE, NOT EITHER/OR. DEPLOY_HARNESS=true does NOT swap the harness
in for the runtime — 01 and 02 always deploy, 03 deploys if DEPLOY_GATEWAYS=true, and 04 is
ADDED on top (and requires 03). Turning the harness on only appends a parallel agent; it
never skips or replaces 02.

⚠️ 04 IS A SECOND, PARALLEL AGENT — NOT A REPLACEMENT FOR 02. It is easy to read
"use the harness to manage the agent" as "02 goes away", and that would silently
delete the only path that serves Google Drive. A Harness OVERRIDES the container's
ENTRYPOINT/CMD, so agent/agent.py never runs on it: no in-process tools, and no
`Authorization` header to read (Harness has no RequestHeaderConfiguration), which is
exactly what the Drive path needs to pin impersonation to a verified email. So both
stacks coexist deliberately, and this script never destroys 02 on behalf of 04. See
infra/04-harness.yaml's header for the full accounting of what does and does not
migrate.

THE GATEWAY STACK NEEDS TWO PASSES, and the second is not optional if you want the
gateway to be more than decorative. 03-gateways.yaml imports the runtime ARN from
02-runtime.yaml, and 02-runtime.yaml's AllowedWorkloadConfiguration needs the ingress
gateway's ARN to stop callers bypassing the gateway. That is circular, so this script
deploys 03, reads its IngressGatewayArn, and re-deploys 02 with it. Between those two
points the bypass is open; the script says so rather than leaving it implicit.

This script only sequences those stacks and threads the image tag between them.
It creates no AWS resource itself, so `aws cloudformation deploy` by hand (see
infra/README.md) remains a fully supported equivalent — nothing here is required
magic.

NOT deprecated and still used: the `bedrock-agentcore` SDK, which the agent
imports at runtime (BedrockAgentCoreApp, @requires_access_token). Different
package from the starter toolkit; see requirements.txt.

WHERE THIS SITS IN THE ARCHITECTURE DIAGRAM (infra/architecture-components.png):
nowhere on it, deliberately — this script has no tile and performs no numbered step.
It creates the diagram by running the two templates that declare it: the build plane
zone (S3, CodeBuild, ECR) from 01-foundation.yaml, then the AgentCore tiles (Runtime,
Memory, the GitHub credential provider behind the token vault, and the execution
role) from 02-runtime.yaml, threading the image tag cfn_build.py produced between
them. Nothing here is on the request path; run it and then never again until the code
changes, and every numbered step still runs without it.

Two of its responsibilities are visible on the canvas even so. The GithubCallbackUrl
it prints belongs to the "GitHub OAuth App" tile in the top-right consent zone — that
URL is what makes the ★ human action land somewhere real, and it is reissued whenever
the credential provider is replaced, so a stale one breaks consent with no error at
deploy time. And ConsentReturnUrl, imported from the client rather than duplicated
here, is the return leg of step 8b: pass a value that our own code does not serve and
the vendor still shows the app as authorized while nothing is ever vaulted.

The Google and GitHub credentials it threads through are ARNs only. The Secrets
Manager tile holds the values; this script never sees them, which is why
GOOGLE_SA_SECRET_ARN being empty is the switch that removes the entire Google Drive
row (steps 8a-10a) from what the deployed system can do.
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
GATEWAY_STACK = optional_env("GATEWAY_STACK") or "ripple-gateways"
HARNESS_STACK = optional_env("HARNESS_STACK") or "ripple-harness"
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
    for stack in (FOUNDATION_STACK, RUNTIME_STACK, GATEWAY_STACK):
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
        # 03-gateways.yaml's fixed names. Checked unconditionally even though the stack
        # is opt-in: a conflict reported on a run that skips the stack is noise the
        # summary already labels by stack, whereas a MISSED conflict is a mid-deploy
        # rollback. Gateway names are fixed (`<agent>-ingress`, `<agent>-tools`); the
        # gateway ids and target ids are service-generated and cannot collide.
        ("03-gateways", f"iam role Ripple{AGENT_NAME}GatewayRole",
         _exists(iam.get_role, RoleName=f"Ripple{AGENT_NAME}GatewayRole")),
        ("03-gateways", f"gateway named {AGENT_NAME}-ingress",
         any(g.get("name") == f"{AGENT_NAME}-ingress"
             for g in acp.list_gateways().get("items", []))),
        ("03-gateways", f"gateway named {AGENT_NAME}-tools",
         any(g.get("name") == f"{AGENT_NAME}-tools"
             for g in acp.list_gateways().get("items", []))),
        # 04-harness.yaml. Underscore, not hyphen: a harness name matches
        # ^[a-zA-Z][a-zA-Z0-9_]{0,39}$ and rejects the `-` every other resource here uses.
        # ⚠️ THIS IS THE ONE THAT USUALLY FIRES: a hand-made `<agent>_harness` created
        # outside any stack (via the toolkit) cannot be ADOPTED by CloudFormation — this
        # stack would try to CREATE a same-named harness and roll back. Delete the
        # unmanaged harness first, then deploy; the stack owns the canonical name.
        ("04-harness", f"iam role Ripple{AGENT_NAME}HarnessRole",
         _exists(iam.get_role, RoleName=f"Ripple{AGENT_NAME}HarnessRole")),
        # ⚠️ Also catches the runtime a PREVIOUS harness provisioned for itself. A
        # harness owns a runtime named `harness_<harnessName>`, which is how a
        # harness-shaped runtime and log group appear in the account with nobody having
        # created one. Deleting the harness deletes it; deleting that runtime by hand
        # leaves the harness pointing at nothing.
        ("04-harness", f"harness named {AGENT_NAME}_harness",
         any(h.get("harnessName") == f"{AGENT_NAME}_harness"
             for h in acp.list_harnesses().get("harnesses", []))),
        # The stack-managed Auth0 M2M provider the harness uses for outbound gateway auth.
        ("04-harness", f"oauth2 credential provider {AGENT_NAME}-gw-auth0",
         any(p.get("name") == f"{AGENT_NAME}-gw-auth0"
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
    # Optional: the template defaults to USER_FEDERATION. Passed explicitly when set,
    # because `aws cloudformation deploy` reuses the STORED value for any parameter
    # absent from --parameter-overrides — the same trap that once shipped a stale
    # consent URL.
    github_auth_flow = optional_env("GITHUB_AUTH_FLOW") or ""
    # Preferred path: the client secret lives in Secrets Manager and only its ARN
    # is passed, so the secret never enters the template or CloudFormation
    # history. See infra/README.md § "The GitHub client secret".
    secret_arn = optional_env("GITHUB_CLIENT_SECRET_ARN") or ""
    # Setting this enables the Google Drive source (the no-consent path). Passed
    # explicitly and ALWAYS — including when empty — because `aws cloudformation
    # deploy` reuses the stored value for any parameter absent from
    # --parameter-overrides. Omitting it when empty would make an
    # already-Drive-enabled stack keep its old ARN, so "unset the var to turn Drive
    # off" would silently not work.
    google_sa_arn = optional_env("GOOGLE_SA_SECRET_ARN") or ""
    # Bounds WHO can be impersonated. Required whenever Drive is on — the template
    # asserts it too (Rules: DriveRequiresDomainAllowlist), but failing here is
    # kinder: no image build, no changeset, and the message can name the env var.
    allowed_domains = optional_env("ALLOWED_EMAIL_DOMAINS") or ""
    # OPT-IN. The gateways are real resources with their own cost and attack surface, and
    # nothing requires them: the runtime is directly invocable and every source's default
    # route is in-process. Deploying them by default would create infrastructure most
    # runs do not use.
    deploy_gateways = (optional_env("DEPLOY_GATEWAYS") or "").lower() in (
        "1", "true", "yes")
    # OPT-IN, and ADDITIVE. Turning this on does not turn 02 off — see the module
    # docstring. The harness is a separate agent with its own front door, its own
    # workload identity, and no Google Drive.
    deploy_harness = (optional_env("DEPLOY_HARNESS") or "").lower() in (
        "1", "true", "yes")
    if deploy_harness:
        # Not an error — the experiment is deliberately runnable — but the harness must not
        # be mistaken for the deploy path. See docs/HARNESS-ASSESSMENT.md.
        print("\n🛑 NOTE: DEPLOY_HARNESS=true deploys the EXPERIMENTAL managed harness.\n"
              "  Do not use it as the deployed agent for now — the runtime route (02) is the\n"
              "  supported path. InvokeHarness lets the caller override the system prompt and\n"
              "  tool allowlist, and the harness cannot serve Google Drive. The runtime stack\n"
              "  below is deployed regardless; this only ADDS a parallel agent.\n",
              file=sys.stderr)
    # Where a `via: GATEWAY` source sends its calls. Always passed to the runtime stack
    # (empty included) for the same stored-value reason as GOOGLE_SA_SECRET_ARN. When the
    # gateway stack is deployed in this run, the freshly-read URL overrides whatever is in
    # the environment — the deployed gateway is the authoritative answer.
    tools_gateway_url = optional_env("RIPPLE_TOOLS_GATEWAY_URL") or ""
    github_via = optional_env("GITHUB_VIA") or ""
    # How the HARNESS authenticates ITSELF to the tools gateway (04-harness.yaml). The
    # gateway is CUSTOM_JWT inbound, so AWS_IAM and NONE both 401 at tool load; OAUTH is the
    # only value that loads the tool, and the template hard-wires it. The Auth0 M2M provider
    # is a STACK RESOURCE, so deploy passes only the client id and the Secrets Manager ARN of
    # its secret — never the secret itself, and never a hand-created provider ARN. The
    # provider ARN is resolved inside the template via !GetAtt, which also orders the
    # dependency for CloudFormation.
    m2m_client_id = optional_env("AUTH0_M2M_CLIENT_ID") or ""
    m2m_secret_arn = optional_env("AUTH0_M2M_CLIENT_SECRET_ARN") or ""
    m2m_secret_json_key = optional_env("AUTH0_M2M_CLIENT_SECRET_JSON_KEY") or ""
    gateway_oauth_scopes = optional_env("GATEWAY_OAUTH_SCOPES") or ""
    if github_via.upper() == "GATEWAY" and not (deploy_gateways or tools_gateway_url):
        print("\nERROR: GITHUB_VIA=GATEWAY but no tools gateway is available.\n"
              "  Either set DEPLOY_GATEWAYS=true to create one, or set "
              "RIPPLE_TOOLS_GATEWAY_URL\n  to an existing gateway. (The agent would "
              "otherwise fall back to in-process at\n  startup with a warning, which "
              "works but is not what you asked for.)", file=sys.stderr)
        return 2
    if deploy_harness and not deploy_gateways:
        print("\nERROR: DEPLOY_HARNESS=true but DEPLOY_GATEWAYS is not set.\n"
              "  The harness's ONLY tool is the tools gateway (it cannot have "
              "in-process tools —\n  a Harness overrides the container entrypoint, so "
              "agent.py never runs). Without the\n  gateway it would deploy cleanly and "
              "then answer every question with no sources at\n  all, which reads as a "
              "model problem rather than a missing stack.\n"
              "  Set DEPLOY_GATEWAYS=true.", file=sys.stderr)
        return 2
    if deploy_harness and not m2m_client_id:
        print("\nERROR: DEPLOY_HARNESS=true but AUTH0_M2M_CLIENT_ID is not set.\n"
              "  The harness reaches the CUSTOM_JWT tools gateway with an Auth0 "
              "client_credentials\n  token; AWS_IAM and NONE both 401 at tool load, so "
              "OAUTH is not optional.\n  The stack declares the Auth0 credential provider "
              "as a resource, but it still needs the\n  M2M app's client id (and its "
              "secret in Secrets Manager — AUTH0_M2M_CLIENT_SECRET_ARN).\n"
              "  Set AUTH0_M2M_CLIENT_ID in dev.env.", file=sys.stderr)
        return 2
    if deploy_harness and not m2m_secret_arn:
        # Not fatal — the template's MANAGED fallback exists — but passing the secret as
        # a CloudFormation parameter stores it in the stack, so we refuse to do it
        # silently. Point at the ARN route and stop.
        print("\nERROR: DEPLOY_HARNESS=true but AUTH0_M2M_CLIENT_SECRET_ARN is not set.\n"
              "  Store the M2M client secret in Secrets Manager and pass its ARN, exactly "
              "like\n  GITHUB_CLIENT_SECRET_ARN. deploy does NOT accept the raw secret as a "
              "parameter\n  (it would be stored in the CloudFormation stack).\n"
              "  See infra/README.md § 'The harness M2M client secret'.", file=sys.stderr)
        return 2
    if google_sa_arn and not allowed_domains:
        print("\nERROR: GOOGLE_SA_SECRET_ARN is set (Drive enabled) but "
              "ALLOWED_EMAIL_DOMAINS is not.\n"
              "  The Drive source impersonates users by email. With no allowlist the "
              "acceptable\n  subjects are every address your IdP will sign for, not "
              "just your Workspace users.\n"
              "  Set it in dev.env, e.g.  ALLOWED_EMAIL_DOMAINS=yourdomain.com",
              file=sys.stderr)
        return 2

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
        # Empty string is meaningful (= Drive disabled), so this is never conditional.
        "GoogleSaSecretArn": google_sa_arn,
        # Always passed for the same reason as GoogleSaSecretArn: `deploy` reuses the
        # STORED value for any omitted parameter, so narrowing the allowlist by
        # unsetting the var has to actually narrow it.
        "AllowedEmailDomains": allowed_domains,
        # Always passed, empty included — same stored-value reason as the two above.
        "ToolsGatewayUrl": tools_gateway_url,
        "GithubVia": github_via,
    }
    trust_unverified = (optional_env("TRUST_UNVERIFIED_EMAIL_CLAIM") or "").lower()
    if trust_unverified in ("1", "true", "yes"):
        # Loud, because it re-opens the hole the allowlist only partly covers: an
        # absent `email_verified` becomes acceptable, so a self-registered address in
        # an allowed domain is an impersonation subject.
        print("\n  WARNING: TRUST_UNVERIFIED_EMAIL_CLAIM is on. Tokens with NO "
              "`email_verified` claim\n  will be accepted as impersonation subjects. "
              "Only valid if your IdP cannot emit\n  the claim AND self-signup is "
              "impossible.")
        runtime_params["TrustUnverifiedEmailClaim"] = "true"
    if github_auth_flow:
        runtime_params["GithubAuthFlow"] = github_auth_flow
    if secret_arn:
        runtime_params["GithubClientSecretArn"] = secret_arn
    deploy_stack(RUNTIME_STACK, "02-runtime.yaml", runtime_params, args.dry_run)

    # ---- 03-gateways.yaml, plus the second pass over 02 -------------------------
    # Opt-in, and skipped under --image-only (rolling an image does not change either
    # gateway; re-running this would only re-assert the same configuration).
    if deploy_gateways and not args.image_only:
        deploy_stack(GATEWAY_STACK, "03-gateways.yaml",
                     {"RuntimeStackName": RUNTIME_STACK,
                      "AgentName": AGENT_NAME,
                      "ProjectTagValue": tags.TAG_VALUE,
                      "Auth0Domain": auth0_domain,
                      "Auth0Audience": auth0_audience,
                      "GithubScopes": github_scopes,
                      # Same value 02 gets, from the same import. The gateway's GitHub
                      # target needs it because its grant type is AUTHORIZATION_CODE,
                      # and it must MATCH the runtime's: two different return URLs
                      # would mean consent granted on one route never completes on
                      # the other.
                      "ConsentReturnUrl": CONSENT_RETURN_URL,
                      # Same ARN 02 got, and for a reason that is invisible from this
                      # stack: 02 runs ClientSecretSource=EXTERNAL, so AgentCore reads
                      # the OAuth client secret AS THE CALLER. The gateway role needs
                      # its own read grant or the first GitHub tool call dies with
                      # AccessDenied. Passed from the SAME variable as the runtime
                      # stack's, so the two grants cannot name different secrets.
                      "GithubClientSecretArn": secret_arn},
                     args.dry_run)

        if not args.dry_run:
            gw = stack_outputs(GATEWAY_STACK)
            ingress_arn = gw.get("IngressGatewayArn") or ""
            tools_url = gw.get("ToolsGatewayUrl") or ""

            # SECOND PASS, for the TOOLS gateway URL only: without the gateway's real
            # URL a GATEWAY-routed source falls back to in-process, which works and is
            # not what was asked for.
            #
            # ⚠️ IngressGatewayArn is READ ABOVE AND DELIBERATELY NOT PASSED. It was,
            # originally — this pass existed to close the gateway bypass by feeding
            # 02's AllowedWorkloadConfiguration. Passing it TODAY makes the runtime 401
            # on EVERY path, the gateway's included, because the ingress target is on
            # JWT_PASSTHROUGH and a passthrough target never mints the WAT that stamps
            # the workload identity chain that field introspects. Adding it back here on
            # its own is the single easiest way to take this deployment down.
            #
            # It is a TWO-part change, not a broken feature: an otherwise-identical
            # gateway on OAUTH outbound does pass the check (proven), so flipping the
            # ingress target's credential type is the prerequisite, and the reason that
            # has not been done is a claim trade-off (M2 needs `email` in the token).
            # Evidence and correct test method: 03-gateways.yaml, IngressGateway comment,
            # mitigation (2). UNTIL THEN THE GATEWAY BYPASS IS OPEN, knowingly.
            if tools_url:
                print("\n=== second pass over the runtime stack")
                print("  Required because 02 and 03 depend on each other: 03 imports "
                      "the runtime ARN, and\n  02 needs the tools gateway's URL. Only "
                      "the URL is passed back — the ingress gateway\n  ARN is read and "
                      "not passed; see the comment above for the two-part change that\n"
                      "  would close the direct-invoke bypass.")
                if tools_url:
                    # The deployed gateway wins over whatever the environment said.
                    runtime_params["ToolsGatewayUrl"] = tools_url
                deploy_stack(RUNTIME_STACK, "02-runtime.yaml", runtime_params, False)

    # ---- 04-harness.yaml --------------------------------------------------------
    # Last, because it imports the tools gateway ARN from 03. Skipped under
    # --image-only: the harness does not run our image at all, so a new image tag is
    # not a reason to touch it. That is the clearest single symptom of what a Harness
    # is — `--image-only` is a no-op for it.
    #
    # FULLY STACK-MANAGED: it declares the Auth0 M2M credential provider and the harness
    # together, so there is nothing to create or patch by hand. OAUTH outbound is not
    # optional here (it is the only value that loads the tool), so the M2M client id is
    # required whenever the harness is deployed.
    if deploy_harness and not args.image_only:
        harness_params = {
            "GatewayStackName": GATEWAY_STACK,
            "AgentName": AGENT_NAME,
            "ProjectTagValue": tags.TAG_VALUE,
            "BedrockModelId": model_id,
            "Auth0Domain": auth0_domain,
            "Auth0Audience": auth0_audience,
            # The Auth0 M2M app authorized for Auth0Audience. Not the public CLI client.
            "Auth0M2mClientId": m2m_client_id,
            # Scopes stay EMPTY by default — client_credentials is scoped by audience,
            # and GitHub's scopes are the wrong vocabulary for Auth0. Passed as a stored
            # value so an override actually takes effect.
            "GatewayOauthScopes": gateway_oauth_scopes,
        }
        # PREFERRED path: the secret lives in Secrets Manager and the template reads it
        # (ClientSecretSource=EXTERNAL). Only the ARN is passed — never the secret, and
        # never as a CloudFormation parameter value. Without it the template falls back to
        # MANAGED, which needs the secret passed some other way; we do not do that here.
        if m2m_secret_arn:
            harness_params["Auth0M2mClientSecretArn"] = m2m_secret_arn
            if m2m_secret_json_key:
                harness_params["Auth0M2mClientSecretJsonKey"] = m2m_secret_json_key
        # Closes FR-18. The runtime provisions Memory and never reads it; the harness
        # takes an ARN and AWS does the reading and writing. Passed only when the
        # runtime stack actually produced one — an empty MemoryArn makes the template
        # set Disabled:{} explicitly rather than leave memory unconfigured.
        if not args.dry_run:
            mem_arn = stack_outputs(RUNTIME_STACK).get("MemoryArn") or ""
            if mem_arn:
                harness_params["MemoryArn"] = mem_arn
            else:
                print("\n  NOTE: runtime stack exports no MemoryArn, so the harness "
                      "deploys with memory\n  DISABLED (sessions still work; they just "
                      "do not persist across invocations).")
        deploy_stack(HARNESS_STACK, "04-harness.yaml", harness_params, args.dry_run)

    if args.dry_run:
        print("\n(dry-run) would then print stack outputs and apply tags.")
        return 0

    out = stack_outputs(RUNTIME_STACK)
    print("\n=== outputs — put these in dev.env")
    print(f"  export RIPPLE_RUNTIME_ARN={out.get('RuntimeArn', '?')}")
    print(f"  export RIPPLE_RUNTIME_QUALIFIER={out.get('EndpointName', '?')}")
    print(f"  export AGENTCORE_MEMORY_ID={out.get('MemoryId', '?')}")
    if deploy_gateways and not args.image_only:
        gw_out = stack_outputs(GATEWAY_STACK)
        print(f"  export RIPPLE_TOOLS_GATEWAY_URL="
              f"{gw_out.get('ToolsGatewayUrl', '?')}")
        print(f"\n  ingress gateway: {gw_out.get('IngressGatewayUrl', '?')}"
              f"/{AGENT_NAME}-runtime/invocations")
        print("  Takes the same {\"prompt\": ...} body as the runtime — it is an HTTP "
              "front door, not\n  an MCP server (only the TOOLS gateway speaks MCP).")
        print("\n  NOTE: this is a SECOND front door, not a replacement. The runtime "
              "stays directly\n  invocable. The control that would stop that "
              "(AllowedWorkloadConfiguration) cannot be\n  enabled while the ingress "
              "target uses JWT_PASSTHROUGH — a passthrough target never\n  stamps the "
              "workload chain, so it would 401 every path. Closeable by moving that\n"
              "  target to OAUTH outbound; see infra/03-gateways.yaml, IngressGateway "
              "comment,\n  mitigation (2).")

    if deploy_harness and not args.image_only:
        h_out = stack_outputs(HARNESS_STACK)
        print(f"\n  export RIPPLE_HARNESS_ARN={h_out.get('HarnessArn', '?')}")
        print("\n  The harness is a SEPARATE agent, not a replacement for the runtime "
              "above. Invoke it\n  with InvokeHarness and an `Authorization: Bearer` "
              "header — SigV4 deploys and runs\n  but does NOT propagate per-user "
              "identity, which silently collapses every user's\n  GitHub access to one "
              "shared credential.")
        print("  ⚠️  It serves GitHub only. Google Drive stays on the runtime: a "
              "harness cannot read\n  the caller's token, and a Drive tool that cannot "
              "learn who is asking cannot narrow a\n  domain-wide service account to "
              "one user. See infra/04-harness.yaml § M2.")
        print(f"  It also provisioned its own runtime "
              f"({h_out.get('ProvisionedRuntimeArn', '?')}) —\n  that is where the extra "
              "harness_* runtime and log group come from. Do not manage it\n  directly.")
        print("\n=== ACTION: register the consent return URL for the HARNESS")
        print("  python3 scripts/register_consent_url.py")
        print(f"  The harness is a THIRD workload identity (harness_{AGENT_NAME}_"
              "harness), so users who\n  already consented on the runtime or gateway "
              "path have NOT consented here. Until it\n  is registered the browser "
              "shows \"This site can't be reached\" AFTER the user clicks\n  Approve, "
              "and nothing is logged anywhere in AWS.")

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
