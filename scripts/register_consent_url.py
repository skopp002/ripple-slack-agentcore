"""Register the consent return URL on the runtime's WorkloadIdentity.

WHY THIS IS A SEPARATE SCRIPT. `AWS::BedrockAgentCore::Runtime` creates its own
WorkloadIdentity implicitly, and that resource is NOT declared in the template — so
CloudFormation cannot set its `allowedResourceOauth2ReturnUrls`. There is no
`WorkloadIdentity` property on the runtime to set either. The allowlist therefore
has to be applied out-of-band, after the runtime exists.

An EMPTY allowlist currently behaves permissively (any return URL is accepted),
which is exactly why this is easy to skip and then get bitten by later: the day the
service tightens that default, every consent breaks at once with a validation error
that names a URL nobody remembers configuring. Registering it explicitly costs one
API call and removes that trapdoor.

The URL registered here MUST match, byte for byte:
  - `client/consent.py` CONSENT_RETURN_URL (where the loopback server listens), and
  - the runtime's OAUTH_CALLBACK_URL env var (ConsentReturnUrl in 02-runtime.yaml),
    which is what the agent passes as `callback_url`.
A mismatch fails at the last hop of consent, after the user has already approved.

Run after every deploy that creates or replaces the runtime:
    python3 scripts/register_consent_url.py
"""
import sys
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parents[1] / "client"))
from env_util import require_env  # noqa: E402
from consent import CONSENT_RETURN_URL  # noqa: E402  (single source of truth)

REGION = require_env("AWS_REGION")
RUNTIME_ARN = require_env("RIPPLE_RUNTIME_ARN")


def main() -> int:
    # The workload identity the runtime uses is named after the runtime id, which is
    # the last ARN segment (e.g. .../runtime/ripple-FRpnoXBbiR -> ripple-FRpnoXBbiR).
    workload_name = RUNTIME_ARN.rsplit("/", 1)[-1]
    cp = boto3.client("bedrock-agentcore-control", region_name=REGION)

    try:
        current = cp.get_workload_identity(name=workload_name)
    except ClientError as e:
        print(f"could not read workload identity '{workload_name}': "
              f"{e.response['Error']['Code']}: {e.response['Error']['Message']}")
        print("Is RIPPLE_RUNTIME_ARN correct, and has the runtime finished creating?")
        return 2

    existing = current.get("allowedResourceOauth2ReturnUrls") or []
    if CONSENT_RETURN_URL in existing:
        print(f"already registered on '{workload_name}':\n  {CONSENT_RETURN_URL}")
        return 0

    # Additive, not replacing: a real web front end will have its own return page
    # registered here, and clobbering it would break that caller's consent.
    updated = sorted({*existing, CONSENT_RETURN_URL})
    resp = cp.update_workload_identity(
        name=workload_name,
        allowedResourceOauth2ReturnUrls=updated,
    )
    print(f"registered on '{workload_name}':")
    for url in resp.get("allowedResourceOauth2ReturnUrls", updated):
        print(f"  {url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
