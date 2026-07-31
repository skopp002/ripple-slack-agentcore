"""Complete a 3-legged OAuth consent for one source, from the CLI.

WHY THIS FILE EXISTS — the step that is easy to miss entirely.

`resourceOauth2ReturnUrl` is NOT a decorative "you're connected" page. After the
vendor redirects to the AgentCore-hosted callback and the code is exchanged,
AgentCore sends the user's browser to the return URL with a **`session_id`** query
parameter, and the application MUST then call:

    complete_resource_token_auth(sessionUri=<that session_id>, userIdentifier=...)

Only that call vaults the token. Skip it and the authorization session stays
`IN_PROGRESS` forever: the vendor shows the app as authorized, `GetResourceOauth2Token`
keeps handing back a fresh authorization URL, and nothing ever appears in the vault.
Every symptom points at consent "not being given" when in fact consent was given and
never *completed*. This cost a long debugging session — the giveaway was pointing the
return URL at a third-party page (GitHub's authorized-apps list), which renders
perfectly and silently discards the `session_id`.

So the return URL has to be something WE control. A CLI has no web front end, hence
the tiny loopback server below: it exists only to read one query parameter, complete
the session, and shut down. A real web app would do the same thing in a route handler
and would not need any of this.

Two constraints on the return URL, both enforced elsewhere:
  - it must be registered in the runtime's WorkloadIdentity
    `allowedResourceOauth2ReturnUrls` (scripts/register_consent_url.py), and
  - it must match what the agent passes as `callback_url`
    (OAUTH_CALLBACK_URL on the runtime, from the ConsentReturnUrl CFN parameter),
so all three are derived from CONSENT_PORT/CONSENT_PATH here to keep them in sync.

WHERE THIS SITS IN THE ARCHITECTURE DIAGRAM (infra/architecture-components.png):

This file belongs to the `CLI client` tile — a third module the tile's caption does not
name — and it exists ENTIRELY IN THE CONSENT FLOW. It has no counterpart in flow A and
never runs for it, because a DELEGATED source has no consent to complete.

Its position is best described as the return leg of 8b that the diagram does not draw.
8b is the vault redirecting a browser to GitHub's consent screen and ★ is the user
clicking Approve; what happens next is that GitHub redirects to the AgentCore-hosted
provider callback, the code is exchanged, and AgentCore then sends the browser to
CONSENT_RETURN_URL below with a `session_id`. So this loopback server sits strictly
AFTER ★ and completes 8b's round trip — the round trip whose existence is precisely why
flow_numbering() refused to let ★ be numbered 9b.

The consequence is an arrow that should exist on the canvas and does not: the
complete_resource_token_auth() call at the bottom of this file is a CLI -> AgentCore
Identity token vault hop, and it is the hop that puts the token in the vault. The
diagram goes 8b (vault redirects) straight to 9b ("GitHub is called under the user's OWN
OAuth token, vaulted after they approved"), which reads as though approval alone vaults
it. It does not. Without the call below the session stays IN_PROGRESS forever, nothing is
ever vaulted, and 9b can never happen — the failure this whole docstring is about. A
reader tracing flow B on the PNG cannot see the one step that makes 9b's precondition
true.

Second thing the canvas cannot show: ★ is drawn USER -> GitHub OAuth App, deliberately,
to point at a human's attention. In this code the browser is opened by the CLI
(webbrowser.open below, or the URL is printed for another profile), so the machine hop
underneath ★ is CLI -> GitHub. That is a documented choice in the diagram, not an error
in it, but it is why the CLI appears to do nothing during the one part of flow B it is
in fact driving end to end.
"""
import http.server
import threading
import urllib.parse
import webbrowser

import boto3

# Fixed, because the value is baked into two places that cannot negotiate at
# runtime: the WorkloadIdentity allowlist and the runtime's OAUTH_CALLBACK_URL.
# Changing it means re-running scripts/register_consent_url.py and redeploying.
CONSENT_PORT = 8799
CONSENT_PATH = "/consent"
CONSENT_RETURN_URL = f"http://localhost:{CONSENT_PORT}{CONSENT_PATH}"

_PAGE = b"""<!doctype html><meta charset=utf-8>
<title>Connected</title>
<body style="font-family:system-ui;max-width:34em;margin:4em auto">
<h2>Source connected</h2>
<p>Your token has been vaulted. You can close this tab and re-run your question.</p>
</body>"""

_FAIL = b"""<!doctype html><meta charset=utf-8>
<title>Consent failed</title>
<body style="font-family:system-ui;max-width:34em;margin:4em auto">
<h2>Consent did not complete</h2>
<p>See the terminal for the error.</p>
</body>"""


class _Handler(http.server.BaseHTTPRequestHandler):
    """Single-purpose handler: capture `session_id`, then let the server die."""

    session_id = None
    error = None

    def do_GET(self):  # noqa: N802 (stdlib naming)
        parts = urllib.parse.urlsplit(self.path)
        if parts.path != CONSENT_PATH:
            # Browsers ask for /favicon.ico unprompted; answering 404 keeps it out
            # of the way instead of being mistaken for the real redirect.
            self.send_error(404)
            return
        qs = urllib.parse.parse_qs(parts.query)
        # AgentCore names it `session_id`; the API parameter is `sessionUri`. Same
        # value, two spellings — accept both rather than depend on one.
        got = (qs.get("session_id") or qs.get("sessionUri") or [None])[0]
        if got:
            _Handler.session_id = got
        else:
            _Handler.error = f"return URL hit with no session_id: {parts.query!r}"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(_PAGE if got else _FAIL)

    def log_message(self, *_args):
        # The default handler logs every request to stderr, which interleaves with
        # the CLI's own output for no benefit.
        pass


def complete_consent(auth_url: str, user_jwt: str, region: str,
                     timeout: float = 300.0, open_browser: bool = True) -> None:
    """Drive one consent to completion, or raise with why it did not.

    Blocks until the browser round-trip finishes (or `timeout`), because there is
    nothing useful to do concurrently: the user has to click.
    """
    _Handler.session_id = None
    _Handler.error = None

    # Bind BEFORE opening the browser. Opening first is a race the user can win on
    # a fast machine, and the redirect would hit a closed port.
    server = http.server.HTTPServer(("127.0.0.1", CONSENT_PORT), _Handler)
    server.timeout = timeout
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    print(f"\nOpening the consent page. Approve access, then return here.")
    print(f"  {auth_url}\n")
    if open_browser:
        webbrowser.open(auth_url)
    else:
        print("  (open the URL above manually)")

    thread.join(timeout)
    server.server_close()

    if _Handler.error:
        raise RuntimeError(_Handler.error)
    if not _Handler.session_id:
        raise TimeoutError(
            f"no redirect to {CONSENT_RETURN_URL} within {timeout:.0f}s. Either the "
            "consent page was not completed, or the URL is not registered in the "
            "runtime's WorkloadIdentity allowlist "
            "(run scripts/register_consent_url.py)."
        )

    # THE STEP THAT ACTUALLY VAULTS THE TOKEN. Identify the user by the same Auth0
    # JWT the runtime authenticated, so the token is vaulted against that identity
    # and no other — this is what keeps per-user permission trimming honest.
    boto3.client("bedrock-agentcore", region_name=region).complete_resource_token_auth(
        userIdentifier={"userToken": user_jwt},
        sessionUri=_Handler.session_id,
    )
    print("Consent completed — token vaulted.")
