"""Register the consent return URL on every WorkloadIdentity that redirects a browser.

Up to THREE of them, because a vaulted OAuth token belongs to THE WORKLOAD IDENTITY THAT
VAULTED IT — so the same human may approve the same GitHub scopes three times, and a
consent completed on one route grants the other two nothing:
  1. the runtime's         — the in-process route (always);
  2. the tools gateway's   — a source routed `via: GATEWAY`, whose target runs its own
                             consent against its own credential provider (if deployed);
  3. the harness's         — the managed agent from 04-harness.yaml (if deployed).
Three consents is the model, not a bug to work around here. What this script prevents is
the worse version, where the redirect has nowhere to land.

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

WHERE THIS SITS IN THE ARCHITECTURE DIAGRAM (infra/architecture-components.png): it
configures the WorkloadIdentity that the "AgentCore Runtime" tile owns implicitly, and
it exists entirely for the sake of step 8b — the "AgentCore Identity token vault" tile
redirecting the user's browser to GitHub's consent screen. That is the one arrow on the
canvas whose round trip comes back through a URL our own code has to serve, which is
what this allowlist governs.

Not a request-path step, and it has no tile of its own: a workload identity is a
property of the runtime, not a component beside it. But get it wrong and the ★ human
action — the only place on this diagram where a person acts — is wasted, because the
failure lands at the LAST hop, after the user has already read the consent screen and
clicked Approve. Nothing before 8b notices, and nothing in the delegation flow depends on this
at all: the Google Drive path (8a-10a) never redirects a browser anywhere, so it works
whether this script has been run or not.

Run after every deploy that creates or replaces the runtime:
    python3 scripts/register_consent_url.py
"""
import sys
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parents[1] / "client"))
from env_util import optional_env, require_env  # noqa: E402
from consent import CONSENT_RETURN_URL  # noqa: E402  (single source of truth)

REGION = require_env("AWS_REGION")
RUNTIME_ARN = require_env("RIPPLE_RUNTIME_ARN")
# Only used to build the harness workload-identity PREFIX. Kept optional and defaulted
# the same way deploy.py does it, so this script does not start failing on setups that
# never deploy a harness.
AGENT_NAME = optional_env("AGENTCORE_AGENT_NAME") or "ripple"


def _register(cp, workload_name: str, required: bool) -> int:
    """Allowlist CONSENT_RETURN_URL on one workload identity. 0 ok, 2 failed."""
    try:
        current = cp.get_workload_identity(name=workload_name)
    except ClientError as e:
        if not required:
            # The tools gateway is opt-in (DEPLOY_GATEWAYS), so its absence is the
            # normal case, not an error.
            print(f"skipped '{workload_name}': not present ({e.response['Error']['Code']})")
            return 0
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


def main() -> int:
    # The workload identity the runtime uses is named after the runtime id, which is
    # the last ARN segment (e.g. .../runtime/ripple-FRpnoXBbiR -> ripple-FRpnoXBbiR).
    workload_name = RUNTIME_ARN.rsplit("/", 1)[-1]
    cp = boto3.client("bedrock-agentcore-control", region_name=REGION)

    rc = _register(cp, workload_name, required=True)

    # ⚠️ THE TOOLS GATEWAY NEEDS THIS TOO, AND IT IS A SEPARATE WORKLOAD IDENTITY.
    # A gateway target on AUTHORIZATION_CODE runs its own consent — a SECOND consent for
    # the same source, because a vaulted token belongs to the workload that vaulted it.
    # That consent redirects to the same loopback URL and is refused (or, today, silently
    # lands nowhere) unless it is allowlisted here as well. Missing this is invisible
    # until the moment after a user clicks Approve, and then presents as the browser
    # showing "This site can't be reached" with no error anywhere in AWS — observed.
    try:
        gateways = cp.list_gateways().get("items", [])
    except ClientError as e:
        print(f"could not list gateways ({e.response['Error']['Code']}); "
              "skipping the gateway workload identity")
        return rc
    for gw in gateways:
        name = gw.get("name") or ""
        gid = gw.get("gatewayId") or gw.get("gatewayIdentifier") or ""
        # Tools gateways only. The INGRESS gateway is on JWT_PASSTHROUGH: it fetches no
        # outbound token, so it never runs a consent and never redirects anywhere.
        if name.startswith("ripple-tools") and gid:
            rc = _register(cp, gid, required=False) or rc

    # ⚠️ AND SO DOES THE HARNESS — A THIRD ONE. 04-harness.yaml's managed agent drives
    # GitHub consent itself, so it redirects a browser and fails the same invisible way.
    #
    # ITS NAME CANNOT BE DERIVED, which is why this is discovery and not an f-string: the
    # service appends a suffix, so `ripple_harness` becomes `harness_ripple_harness-<rand>`
    # (the `harness_` prefix is literal and the harness name follows — hence the doubled
    # word in the observed `harness_harness_0tfk9-dc9myp9RQD`).
    try:
        identities = cp.list_workload_identities().get("workloadIdentities", [])
    except ClientError as e:
        print(f"could not list workload identities ({e.response['Error']['Code']}); "
              "skipping the harness workload identity")
        return rc
    prefix = f"harness_{AGENT_NAME}"
    matches = [w["name"] for w in identities
               if (w.get("name") or "").startswith(prefix)]
    if not matches:
        # Normal when DEPLOY_HARNESS is off.
        print(f"skipped harness: no workload identity starting '{prefix}'")
    elif len(matches) > 1:
        # A replaced harness leaves its workload identity behind, so extra matches are
        # ORPHANS. Registering on a stale one is harmless but useless; say so once.
        print(f"  note: {len(matches)} match '{prefix}' — a replaced harness leaves its "
              "workload identity behind, so the older ones are orphans")
    for name in matches:
        rc = _register(cp, name, required=False) or rc
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
