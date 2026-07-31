#!/usr/bin/env python3
"""Build the agent container and push it to ECR, then print the image tag.

This exists because CloudFormation cannot build a container image, and
AWS::BedrockAgentCore::Runtime requires a ContainerUri that already exists. So:

    01-foundation.yaml  ->  cfn_build.py  ->  02-runtime.yaml (ImageTag=<tag>)

Replaces the starter toolkit's Runtime.launch(): same CodeBuild -> ECR path, but
driven against the CFN-managed project so nothing is created outside the stacks.

WHERE THIS SITS IN THE ARCHITECTURE DIAGRAM (infra/architecture-components.png):
this script DRIVES the build plane zone and appears on it only as motion, never as a
tile. It walks the three unnumbered arrows in that green box left to right — it writes
source.zip into the S3 build source, starts the CodeBuild project, and CodeBuild
pushes the arm64 image into the ECR repository whose tag 02-runtime.yaml then pins so
the "container image" arrow into the AgentCore Runtime tile points at something that
exists.

It carries no step number because the build plane carries none: those three arrows
are deploy-time infrastructure, drawn thin and labelled inline precisely because they
are not hops in either request flow. This script reads no user identity, touches no
token vault, and creates nothing — every AWS resource it uses was declared by
01-foundation.yaml and is looked up through that stack's outputs.

The narrow INCLUDE list below is what keeps it that way. The zip carries the
Dockerfile, requirements.txt and agent/ and nothing else, so dev.env cannot travel
into a build log or an image layer — the credentials the diagram routes through the
Secrets Manager tile stay reachable only by ARN at runtime.

Usage:
    source dev.env
    python3 scripts/cfn_build.py [--foundation-stack ripple-foundation]
"""
import argparse
import io
import os
import sys
import time
import zipfile

import boto3

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from env_util import require_env  # noqa: E402

SOLUTION_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Everything the container build needs. Kept explicit rather than zipping the
# whole tree: .venv/, .venv-deploy/ and dev.env must never enter the artifact.
INCLUDE_FILES = ["Dockerfile", "requirements.txt"]
INCLUDE_DIRS = ["agent"]
EXCLUDE_SUFFIXES = (".pyc", ".pyo")
EXCLUDE_DIR_NAMES = {"__pycache__"}


def _stack_output(cfn, stack: str, key: str) -> str:
    outs = cfn.describe_stacks(StackName=stack)["Stacks"][0].get("Outputs", [])
    for o in outs:
        if o["OutputKey"] == key:
            return o["OutputValue"]
    raise SystemExit(f"Stack '{stack}' has no output '{key}'. Deploy 01-foundation.yaml first.")


def _build_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in INCLUDE_FILES:
            p = os.path.join(SOLUTION_DIR, f)
            if not os.path.exists(p):
                raise SystemExit(f"Missing required build file: {f}")
            z.write(p, f)
        for d in INCLUDE_DIRS:
            root_dir = os.path.join(SOLUTION_DIR, d)
            if not os.path.isdir(root_dir):
                raise SystemExit(f"Missing required build dir: {d}/")
            for root, dirs, files in os.walk(root_dir):
                dirs[:] = [x for x in dirs if x not in EXCLUDE_DIR_NAMES]
                for fn in files:
                    if fn.endswith(EXCLUDE_SUFFIXES):
                        continue
                    full = os.path.join(root, fn)
                    z.write(full, os.path.relpath(full, SOLUTION_DIR))
    return buf.getvalue()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--foundation-stack", default=os.environ.get("FOUNDATION_STACK", "ripple-foundation"))
    ap.add_argument("--agent-name", default=os.environ.get("AGENTCORE_AGENT_NAME", "ripple"))
    args = ap.parse_args()

    region = require_env("AWS_REGION")
    cfn = boto3.client("cloudformation", region_name=region)
    s3 = boto3.client("s3", region_name=region)
    cb = boto3.client("codebuild", region_name=region)

    bucket = _stack_output(cfn, args.foundation_stack, "SourceBucketName")
    project = _stack_output(cfn, args.foundation_stack, "CodeBuildProjectName")
    repo_uri = _stack_output(cfn, args.foundation_stack, "EcrRepositoryUri")

    # Timestamp tag; the ECR repo is IMMUTABLE so tags must never be reused.
    image_tag = time.strftime("%Y%m%d-%H%M%S", time.gmtime())

    key = f"{args.agent_name}/source.zip"
    print(f"Packaging source -> s3://{bucket}/{key}")
    s3.put_object(Bucket=bucket, Key=key, Body=_build_zip())

    print(f"Starting build {project} (IMAGE_TAG={image_tag})")
    build_id = cb.start_build(
        projectName=project,
        environmentVariablesOverride=[
            {"name": "IMAGE_TAG", "value": image_tag, "type": "PLAINTEXT"},
            {"name": "ECR_REPO_URI", "value": repo_uri, "type": "PLAINTEXT"},
        ],
    )["build"]["id"]

    print(f"Build {build_id} — waiting...")
    phase = None
    while True:
        b = cb.batch_get_builds(ids=[build_id])["builds"][0]
        if b.get("currentPhase") != phase:
            phase = b.get("currentPhase")
            print(f"  phase: {phase}")
        if b["buildComplete"]:
            status = b["buildStatus"]
            break
        time.sleep(5)

    if status != "SUCCEEDED":
        print(f"\nBuild FAILED ({status}). Logs:")
        logs = b.get("logs", {})
        print("  " + (logs.get("deepLink") or "no log link available"))
        return 1

    print(f"\nBuild SUCCEEDED.\n  image: {repo_uri}:{image_tag}\n")
    print("Next — deploy the runtime with this tag:")
    print(f"  ImageTag={image_tag}")
    # Machine-readable last line for scripting.
    print(f"IMAGE_TAG={image_tag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
