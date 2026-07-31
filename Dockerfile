# Ripple agent container — OWNED BY THIS REPO, not generated.
#
# This file was previously produced by the bedrock-agentcore-starter-toolkit and
# gitignored as a build artifact. That was a latent break: scripts/cfn_build.py
# requires a Dockerfile in the build zip, so a fresh clone had nothing to build.
# The toolkit is slated for deprecation, so this is now a committed source file —
# edit it deliberately and review it in diffs.
#
# MUST produce linux/arm64: AgentCore Runtime rejects amd64 images. CodeBuild
# guarantees it by running on ARM_CONTAINER (infra/01-foundation.yaml); if you
# build by hand, pass --platform linux/arm64.
#
# WHERE THIS SITS IN THE ARCHITECTURE DIAGRAM (infra/architecture-components.png):
# this file is the CONTENTS of the "AgentCore Runtime" tile, and it is consumed by
# the build plane rather than being a tile of its own. CodeBuild reads it out of the
# source.zip, pushes the resulting image to ECR, and the unnumbered "container image"
# arrow from ECR to the runtime is where that image becomes the tile the request path
# talks to. So this file has no step number and cannot have one: it is built once at
# deploy time and then every numbered step the runtime performs (5a/5b, 6a, 7a/6b,
# 12a/12b) is performed by the process this CMD starts.
#
# The COPY of agent/ is why several things on the diagram are captions rather than
# tiles. identity_claims.py — which does the JWT re-verification of step 6a — and
# gdrive_tool.py, which signs the service-account assertion of step 8a, ship inside
# this image as modules, not as deployable components. Drawing them as peers of
# Bedrock or the token vault would assert they can be deployed or scaled
# independently; they cannot, they are files in this container. The same is true of
# the in-process tool dispatch that stands in for the TARGET "Tools AgentCore
# Gateway" tile today: the reason that tile is dashed is that its work currently
# happens inside this image.
#
# Nothing here reaches outside the runtime tile at build time. The allowlist COPY
# below (agent/ only, never `COPY . .`) is what keeps dev.env — and therefore every
# credential the diagram routes through the Secrets Manager tile — out of the image
# in the first place.

FROM public.ecr.aws/docker/library/python:3.12-slim-bookworm

# uv copied from its own published image rather than curl'd at build time: no
# network fetch in the build, and the version is pinned instead of drifting.
# Public ECR for the Python base keeps the pull inside AWS (no Docker Hub rate
# limit from CodeBuild); uv has no ECR mirror, so it comes from ghcr.
COPY --from=ghcr.io/astral-sh/uv:0.12.0 /uv /usr/local/bin/uv

WORKDIR /app

ENV UV_SYSTEM_PYTHON=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_NO_PROGRESS=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
# NOT set here, deliberately:
#   AWS_REGION / AWS_DEFAULT_REGION — the toolkit's Dockerfile baked in
#     us-west-2, which silently pins the image to one region and overrides what
#     the runtime injects. Region arrives from the runtime environment instead.
#   DOCKER_CONTAINER — the toolkit set it twice; nothing in this agent reads it.

# Dependencies before source, so editing agent code reuses this cached layer.
COPY requirements.txt .
RUN uv pip install --no-cache -r requirements.txt

# Non-root, created before COPY so app files land with the right owner.
RUN useradd --create-home --uid 1000 bedrock_agentcore

# Copy ONLY the agent package. The toolkit did `COPY . .` and leaned on
# .dockerignore to subtract secrets — an allowlist cannot leak dev.env by
# forgetting an ignore rule. scripts/cfn_build.py zips the same narrow set.
COPY --chown=bedrock_agentcore:bedrock_agentcore agent/ ./agent/

USER bedrock_agentcore

# AgentCore Runtime addresses the container on 8080. The toolkit also EXPOSEd
# 9000 and 8000, which were never served — dropped, since an unused EXPOSE only
# misleads the next reader.
EXPOSE 8080

# BedrockAgentCoreApp.run() binds 0.0.0.0:8080 and serves the entrypoint.
CMD ["python", "-m", "agent.agent"]
