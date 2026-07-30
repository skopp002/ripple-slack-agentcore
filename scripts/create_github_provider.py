#!/usr/bin/env python3
"""Create (or show) the GitHub OAuth2 credential provider in AgentCore Identity.

Run this in the shell where GITHUB_CLIENT_SECRET is exported. The secret is read
from the environment and sent directly to the AgentCore control-plane API — it is
never printed, logged, or written to disk.

Usage:
    export GITHUB_CLIENT_SECRET='...'        # already done by you
    python3 scripts/create_github_provider.py
"""
import os
import sys

import boto3

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tags  # noqa: E402
from env_util import require_env  # noqa: E402

REGION = require_env("AWS_REGION")
NAME = require_env("GITHUB_CREDENTIAL_PROVIDER")
CLIENT_ID = require_env("GITHUB_CLIENT_ID")
CLIENT_SECRET = require_env("GITHUB_CLIENT_SECRET")  # shell only; never in dev.env


def main() -> int:
    c = boto3.client("bedrock-agentcore-control", region_name=REGION)

    # If it already exists, just show it (idempotent-ish; delete first to recreate).
    existing = {
        p.get("name")
        for p in c.list_oauth2_credential_providers().get("credentialProviders", [])
    }
    if NAME in existing:
        info = c.get_oauth2_credential_provider(name=NAME)
        print(f"Provider '{NAME}' already exists.")
        # Re-assert the cost tag: the provider may predate tagging.
        arn = info.get("credentialProviderArn") or (
            info.get("credentialProvider") or {}
        ).get("credentialProviderArn")
        if arn:
            c.tag_resource(resourceArn=arn, tags=tags.as_map())
            print(f"Tagged {tags.TAG_KEY}={tags.TAG_VALUE}")
        _print_callback(info)
        return 0

    resp = c.create_oauth2_credential_provider(
        name=NAME,
        credentialProviderVendor="GithubOauth2",
        oauth2ProviderConfigInput={
            "githubOauth2ProviderConfig": {
                "clientId": CLIENT_ID,
                "clientSecret": CLIENT_SECRET,
            }
        },
        tags=tags.as_map(),
    )
    print(f"Created GitHub credential provider '{NAME}' "
          f"tagged {tags.TAG_KEY}={tags.TAG_VALUE}.")
    _print_callback(resp)
    return 0


def _print_callback(info: dict) -> None:
    """Surface the OAuth callback/redirect URL to paste into the GitHub OAuth App."""
    # Field name varies by API version; probe the likely spots.
    for key in ("oauth2ProviderConfigOutput", "credentialProvider", "clientInformation"):
        blob = info.get(key)
        if isinstance(blob, dict):
            for k, v in blob.items():
                if "redirect" in k.lower() or "callback" in k.lower():
                    print(f"\n>>> Callback URL to paste into the GitHub OAuth App:\n    {v}")
                    return
    print(
        "\n(No callback URL field found in the response — run:\n"
        f"   aws bedrock-agentcore-control get-oauth2-credential-provider --name {NAME} --region {REGION}\n"
        " and look for the redirect/callback URI.)"
    )
    # Also dump top-level keys to help locate it.
    print("Response keys:", list(info.keys()))


if __name__ == "__main__":
    raise SystemExit(main())
