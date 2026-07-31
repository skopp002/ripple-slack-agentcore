"""Ripple CLI — ask the deployed AgentCore Runtime a question.

Flow:
  1. Auth0 device-flow login (login.py) -> real user JWT.
  2. POST the prompt + JWT to the AgentCore Runtime invocations endpoint.
     The runtime's CUSTOM_JWT authorizer validates the Auth0 token; the agent
     then uses AgentCore Identity to mint the user's GitHub token and answers
     with inline citations + a High/Medium/Low confidence band.

The user JWT travels in the Authorization: Bearer header and nowhere else. That is the
value the CUSTOM_JWT authorizer authenticates, and it is also where the agent reads the
caller's identity from, so there is exactly one identity channel per request.

Usage:
    python client/ask.py "What is our 2026 roadmap?"
    python client/ask.py --force "..."      # force re-login first

WHERE THIS SITS IN THE ARCHITECTURE DIAGRAM (infra/architecture-components.png):

This file is the other half of the `CLI client` tile, and it is where a reader following
the badges enters and leaves the system. It receives 1a/1b (the question, as argv rather
than a chat box — no source is named and no permission is requested, which is what makes
the two flows indistinguishable at this point), performs 4a/4b, receives 13a/13b, and
prints 14a/14b.

4a/4b and 13a/13b are drawn against the `Ingress AgentCore Gateway`, which is dashed
TARGET. Today invoke() POSTs straight at the runtime's own data-plane invocations
endpoint, so the hop this file really makes is CLI -> AgentCore Runtime, an arrow the
diagram does not draw at all, and step 5a/5b — the front door validating the JWT and
forwarding — is performed by the runtime's own CUSTOM_JWT authorizer rather than by
anything on the diagram's 5a/5b edge. The NOTE block says so in prose; the badges do
not, so do not read 4a->5a as two hops when tracing against this code.

The Bearer header set at 4a/4b is the ONLY identity this request carries — the body holds
the question and nothing else. Step 6a therefore re-verifies the very token the authorizer
already validated at 5a, which is not redundant: the authorizer proves the token is
authentic, while 6a additionally requires `email_verified` to be TRUE and the address to
be inside the domain allowlist before that address may become an impersonation subject.
Neither of those two gates is drawn on the canvas.

THE CONSENT FLOW RUNS THROUGH HERE IN A WAY THE DIAGRAM DOES NOT SHOW. 8b is drawn as
the vault redirecting the user's browser, but by the time consent is needed this
invocation has already returned — the runtime never holds the request open for a human
(see agent.py's NoWaitTokenPoller), so the authorization URL travels back to this
process in `auth_required` over 11b/12b/13b and it is _render() below that opens the
browser on it. On the canvas that makes the CLI tile look absent from flow B between 4b
and 13b, when in fact this file is the only thing that turns a returned URL into the ★
step happening at all. After consent completes it re-invokes: a second full 4b->13b pass
the numbering shows once, because the user asked a question, not for a browser tab.

Only VAULTED sources can appear in `auth_required`; a DELEGATED source (Google Drive,
flow A) has no consent step anywhere, which is why nothing in the block below can ever
open a browser for the green path.
"""
import argparse
import json
import sys
import urllib.parse
import uuid
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from login import login  # noqa: E402
from consent import complete_consent  # noqa: E402
from env_util import require_env  # noqa: E402

REGION = require_env("AWS_REGION")
RUNTIME_ARN = require_env("RIPPLE_RUNTIME_ARN")
QUALIFIER = require_env("RIPPLE_RUNTIME_QUALIFIER")


def invoke(prompt: str, access_token: str, session_id: str) -> dict:
    # Data-plane HTTPS endpoint for CUSTOM_JWT runtimes: the ARN is URL-encoded
    # into the path and the user JWT is the bearer credential.
    arn_enc = urllib.parse.quote(RUNTIME_ARN, safe="")
    url = (
        f"https://bedrock-agentcore.{REGION}.amazonaws.com"
        f"/runtimes/{arn_enc}/invocations"
    )
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        # Session id groups turns for the same user conversation (>=33 chars).
        "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session_id,
    }
    # The body carries the QUESTION only. The user's identity travels in the
    # Authorization header above, which is the value the CUSTOM_JWT authorizer
    # authenticates — the agent reads it from there (agent.py `_caller_jwt`). Sending the
    # token in the body as well would create a second, unauthenticated identity channel
    # that could name a different user than the header does.
    body = {"prompt": prompt}
    params = {"qualifier": QUALIFIER}
    r = requests.post(url, headers=headers, params=params, json=body, timeout=120)
    if r.status_code != 200:
        raise RuntimeError(f"invoke failed [{r.status_code}]: {r.text[:500]}")
    try:
        return r.json()
    except ValueError:
        return {"raw": r.text}


def _render(resp: dict, args, prompt: str, access_token: str,
            session_id: str, allow_consent: bool = True) -> int:
    """Print one response and return the process exit code.

    Factored out of main() so the post-consent re-ask can reuse it verbatim.
    `allow_consent=False` on that second pass makes the recursion strictly
    one-deep: if a source is STILL unconnected after we just vaulted its token,
    looping would only re-open the browser forever.
    """
    if args.json:
        print(json.dumps(resp, indent=2))
        return 0

    print(resp.get("answer", resp))
    if "confidence" in resp:
        print("-" * 60)
        print(f"Confidence: {resp['confidence']}   "
              f"Connected sources: {resp.get('connected_sources', [])}")

    # First run per user, per source: not connected yet, so that source could not be
    # searched. Rather than leaving the user to guess why the answer is LOW confidence
    # with no sources, drive the one-time consent to completion. A dict keyed by
    # source, because the target state has four (Databricks Genie, Confluence,
    # Slack, Google Drive) and a user may need to authorize several.
    # Only VAULTED sources can ever appear here. A DELEGATED source (Google Drive) has
    # no consent step at all, so it is absent from `auth_required` by construction —
    # the agent reports its kind in `source_flows` rather than inventing a consent URL
    # for a path that has none. See agent.py's `source_flows` comment.
    pending = resp.get("auth_required") or {}
    if pending:
        print("-" * 60)
        print(f"{len(pending)} source(s) not connected for this user — "
              "they were not searched.")
        # Drive each consent to completion rather than printing the URL and hoping.
        # Printing alone is not enough: AgentCore redirects to our return URL with a
        # `session_id`, and the token is only vaulted once we call
        # CompleteResourceTokenAuth with it (see client/consent.py). A user who just
        # opens the URL sees the vendor's approval screen succeed and still ends up
        # with nothing in the vault.
        if args.no_consent or not allow_consent:
            print("Open each once to authorize, then re-run the same question:\n")
            for source, url in sorted(pending.items()):
                print(f"  {source}:\n    {url}\n")
            if args.no_consent:
                print("NOTE: --no-consent only prints these. Opening one by hand "
                      "does NOT finish the flow —\nthe redirect must reach this "
                      "client to be completed. Re-run without --no-consent.")
        else:
            failed = {}
            for source, url in sorted(pending.items()):
                print(f"\n=== connecting '{source}'")
                try:
                    complete_consent(url, access_token, REGION,
                                     open_browser=not args.print_url)
                except Exception as e:
                    failed[source] = f"{type(e).__name__}: {e}"
                    print(f"  could not complete: {type(e).__name__}: {e}")
            if len(failed) < len(pending):
                # At least one source was connected, so the same question can now be
                # answered with grounding. Re-asking here is the whole point: the
                # user typed a question, not a request to authorize.
                print("\n" + "-" * 60)
                print("Re-asking now that the new source(s) are connected...\n")
                return _render(invoke(prompt, access_token, session_id),
                               args, prompt, access_token, session_id,
                               allow_consent=False)
        # Non-zero only if NOTHING was searched. With several sources, a partial
        # answer from the connected ones is still a real answer, so a script should
        # not treat that as failure — but a fully ungrounded one is not a success.
        if not resp.get("connected_sources"):
            return 3

    # Sources that broke BEFORE a consent URL could be issued. Distinct from the
    # block above: there is nothing the user can click, so this is our bug or a
    # misconfiguration, and printing the exception beats leaving someone to infer it
    # from a LOW-confidence answer.
    broken = resp.get("source_errors") or {}
    if broken:
        flows = resp.get("source_flows") or {}
        print("-" * 60)
        print(f"{len(broken)} source(s) could not be reached — not a consent issue:")
        for source, err in sorted(broken.items()):
            print(f"  {source}: {err}")
        print("\nThis is a configuration or plumbing fault, not something the user "
              "can authorize away. Check the runtime logs.")
        # A DELEGATED source needs different advice from a vaulted one: there is no
        # consent to complete and no token to refresh, so the cause is either the
        # admin-side delegation grant or a missing claim in the caller's token. Saying
        # "check the runtime logs" alone sends someone hunting in the wrong place.
        delegated = [s for s in broken if flows.get(s) == "DELEGATED_SUBJECT"]
        if delegated:
            print(f"\n{', '.join(sorted(delegated))} use(s) admin-delegated "
                  "impersonation (no user consent exists to fix). Likely causes:")
            print("  - the caller's token carries no verified 'email' claim")
            print("  - the email is not the Workspace PRIMARY address (an alias "
                  "fails rather than resolving)")
            print("  - the domain-wide delegation grant is missing, or its scope "
                  "list does not match")
            print("  Verify the admin side independently:")
            print("    python3 scripts/verify_drive_delegation.py <user-email>")
        if not resp.get("connected_sources"):
            return 4
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("prompt", nargs="+", help="the question to ask Ripple")
    ap.add_argument("--force", action="store_true", help="force Auth0 re-login")
    ap.add_argument("--json", action="store_true", help="print raw JSON response")
    # webbrowser.open() targets whatever profile the OS considers default, which is
    # the wrong one if you keep work identities in separate browser profiles. This
    # still runs the loopback server and completes the flow — it just lets you paste
    # the URL where you want it.
    ap.add_argument("--print-url", action="store_true",
                    help="print the consent URL instead of opening a browser "
                         "(use when the right identity is in another profile)")
    ap.add_argument("--no-consent", action="store_true",
                    help="print consent URLs instead of completing them in a "
                         "browser (for headless runs; leaves sources unconnected)")
    args = ap.parse_args()

    prompt = " ".join(args.prompt)
    tok = login(force=args.force)
    access_token = tok["access_token"]

    # STATELESS: a brand-new session id every run, so no two invocations are ever
    # grouped. That is honest about the current behaviour — the agent keeps no
    # history (see agent/agent.py invoke()), so a stable id would only imply a
    # continuity that does not exist. The runtime uses this for observability
    # grouping and requires >=33 chars.
    #
    # STATEFUL: to hold one conversation across several `ask.py` runs, persist the
    # id instead of minting it, e.g. write it beside the cached token:
    #     p = Path(__file__).parent / ".session.json"
    #     session_id = json.loads(p.read_text())["id"] if p.exists() else new_id
    # and add a --new-session flag to rotate it deliberately. Do that ONLY together
    # with the server-side memory work: on its own it changes nothing, because the
    # agent never reads the id.
    session_id = f"ripple-{uuid.uuid4().hex}"

    print(f"\nAsking Ripple: {prompt}\n" + "-" * 60)
    resp = invoke(prompt, access_token, session_id)
    return _render(resp, args, prompt, access_token, session_id)


if __name__ == "__main__":
    raise SystemExit(main())
