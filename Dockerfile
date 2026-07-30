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
