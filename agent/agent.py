"""Ripple Knowledge Assistant — AgentCore Runtime agent (harness entrypoint).

Architecture (real):
  user IdP JWT (Okta / Auth0, from the caller or an ingress front end)
    -> BedrockAgentCoreApp entrypoint (this file, on AgentCore Runtime)
    -> a per-user credential PER SOURCE, by one of two mechanisms:
         DELEGATED_SUBJECT  the source impersonates the user by VERIFIED EMAIL.
                            No consent screen, ever. Google Drive works this way.
                            THIS IS THE PRIMARY PATH.
         VAULTED_OAUTH      AgentCore Identity brokers a per-user OAuth token,
                            vaulted + refreshed. One-time consent per user per
                            source. GitHub works this way — an EXTENSION, kept
                            because not every vendor can be reached without it.
    -> Strands agent (Claude on Bedrock) with three tools that each fan out across
       every source this user has available
    -> answer with inline citations + High/Medium/Low confidence band
  Per-user memory via AgentCore Memory, partitioned by the IdP 'sub'.

DATA SOURCES: Google Drive/Docs and GitHub are implemented; Databricks Genie (MCP),
Confluence and Slack are the target state. Everything here iterates over the SOURCES
table below, so adding one is a table entry plus its credential wiring — not a change
to invoke(), the response shape, or the client. See the comment on SOURCES.

Permission trim: the call to each source runs AS THE USER, so each source returns ONLY
what that user can access. The trim is enforced BY THE SOURCE, not by us, and that must
stay true for every source added — never a service account's own view, never our own
filtering. A user who has no credential for a source simply gets no hits from it.

⚠️ THE ONE INVARIANT THAT MUST NOT BREAK. A delegated source can reach ANY user in the
domain; only the subject we pass narrows it to one. That subject must always come from
identity_claims.verified_email() (signature + issuer + audience verified), never from
the unverified `_user_sub()` decode and never from anything the caller supplies
directly. Get this wrong and the per-user trim silently becomes a domain-wide read
while every log line still looks correct.

STATE MODEL: STATELESS PER INVOCATION.  <-- deliberate; see `invoke()` below
Every call to the entrypoint builds a fresh Agent and answers from the prompt
alone. Nothing is carried between turns: no conversation history, no cached tool
results, no in-process dict keyed by user or session. Two consequences worth being
explicit about, because both are easy to regress:
  - Any runtime instance can serve any turn. There is no affinity to preserve, so
    scaling out needs no sticky routing and a cold start loses nothing.
  - "What did I just ask?" does not work. That is a real product gap, not an
    oversight — see the STATEFUL section in `invoke()` for how to close it.
This matches the MCP 2026-07-28 model, which removes protocol-level sessions and
makes each tool call self-contained (state becomes the application's job, carried
in explicit parameters). See infra/ARCHITECTURE.md § "State model".

Deployed by CloudFormation: `python3 scripts/deploy.py` (or the equivalent
`aws cloudformation deploy` calls in infra/README.md). The starter toolkit is no
longer used anywhere in this repo — note that the `bedrock_agentcore` imports
below are the SDK, a different package that is NOT deprecated.

WHERE THIS SITS IN THE ARCHITECTURE DIAGRAM (infra/architecture-components.png):

This file IS the `RT` (AgentCore Runtime) tile. identity_claims.py, gdrive_tool.py and
github_tool.py get no tiles of their own on purpose — they are modules inside this
container, and drawing them as peers of managed services would imply they are
separately deployable. On the prefix both flows share, this file performs 5a/5b: the
diagram puts front-door JWT validation on the `IGW` tile, but that tile is TARGET, so
today the Runtime's own CUSTOM_JWT authorizer IS the front door and the `IGW`->`RT`
arrow is an alias for the invocations endpoint this file is served on. `invoke()` then
performs 6a by calling verified_email() before any delegated source is touched, 7a/6b
by dispatching the tools (the `RT`->`TGW` arrow, drawn through the TARGET tools gateway;
the calls are in-process), and 11a/11b plus 12a/12b on the return lane.

7b IS NOT WHAT THIS FILE DOES, and the mismatch is not a detail. The diagram numbers
flow B's next step as an RFC 8693 exchange of the user's Okta JWT "with no user
interaction", then numbers 8b as the vault having no token and redirecting to consent —
which cannot both happen. The code takes the second: GitHub's row in SOURCES resolves to
USER_FEDERATION, because GitHub answers `unsupported_grant_type` to the exchange grant
and no arrangement of identity changes that. The combination 7b then 8b describes is one
`_capture` below treats as a POLICY failure and deliberately refuses to show a user
(FR-5a), so a reader tracing flow B as numbered is tracing a sequence this file classes
as a bug. The exchange edge is real but belongs to the `MCP` tile, which is TARGET.

Two things the diagram draws around this file that the code does not do, so do not
read them as descriptions of `invoke()`. The `RT`->`MEM` "read / write events" arrow is
solid and untagged, but MEMORY_ID is provisioned and unused and every turn is
stateless — nothing here reads or writes AgentCore Memory, and no actorId is ever set.
And 8b is drawn as the vault redirecting the user's browser; what actually happens is
that `_capture` catches the URL and `result["auth_required"]` hands it back to the
caller, because NoWaitTokenPoller refuses to hold the request open. The consequence for
reading the diagram is that flow B's 1b->14b cannot be walked inside one invocation: a
first-time user gets the entire return lane WITH a consent URL attached, and 9b happens
on their next question, after the client has completed the session out of band.

The numbering also makes the two flows look like alternatives, and in this file they
are not. `search_company_documents` fans out over every row of ENABLED and merges the
hits, so a single question can run 8a-10a and 9b-10b in the same turn; the `credential`
kind in SOURCES, not the diagram's per-source arrows, is what decides which mechanism
each row uses. Finally, the diagram has no edge into `CloudWatch` at all, which hides a
lane this file deliberately relies on: when claim verification fails the caller gets a
generic refusal and the REASON goes only to the log, so the print() calls below are the
only place that failure is diagnosable.
"""
import asyncio
import json
import os
import sys
import time

# Ensure sibling modules import whether launched as `python agent.py`,
# `python -m agent.agent` (container CMD), or from another CWD.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import boto3
import jwt
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from bedrock_agentcore.identity.auth import requires_access_token
from bedrock_agentcore.services.identity import TokenPoller
from strands import Agent, tool
from strands.models import BedrockModel

import gateway_tool
from github_tool import fetch_document, list_sources, search_github
from identity_claims import ClaimVerificationError, verified_email

# ---- config (from injected runtime env; no baked-in defaults) --------------
def _require(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(
            f"required environment variable '{name}' is not set on the runtime"
        )
    return val


MODEL_ID = _require("BEDROCK_MODEL_ID")

# NOT _require: AWS_REGION is the one value we do not set ourselves. The CFN
# template deliberately omits it (infra/02-runtime.yaml) because AgentCore Runtime
# is expected to inject it, and setting a reserved variable can be rejected. But
# `_require` at import time turns that expectation into a crash loop if it is ever
# untrue — the container would die before serving one request, which is a bad way
# to discover an environment detail. Fall back through the standard chain instead;
# boto3 resolves the same order, so an explicitly-passed region always matches what
# the SDK would have used anyway.
REGION = (
    os.environ.get("AWS_REGION")
    or os.environ.get("AWS_DEFAULT_REGION")
    or boto3.session.Session().region_name
)
if not REGION:
    raise RuntimeError(
        "cannot determine AWS region: none of AWS_REGION, AWS_DEFAULT_REGION, or "
        "the boto3 session default is set on this runtime"
    )

# Where AgentCore sends the USER'S BROWSER as the LAST hop of consent. Two URLs are
# easy to confuse here, and swapping them silently breaks the final hop:
#
#   provider callback   .../identities/oauth2/callback/<uuid>  — receives GITHUB's
#                       redirect, carrying ?code=&state=. Goes in the vendor's OAuth
#                       app. NOT this value.
#   return url (this)   a callback into OUR OWN application. AgentCore appends
#                       ?session_id=<urn:ietf:params:oauth:request_uri:...> and the
#                       token is vaulted ONLY when the app then calls
#                       CompleteResourceTokenAuth with it (see client/consent.py).
#
# IT IS NOT A PASSIVE LANDING PAGE. Believing that cost a long debugging session: point
# it at a third party (GitHub's authorized-apps page renders perfectly) and the
# session_id is silently discarded — the vendor lists the app as authorized,
# GetResourceOauth2Token keeps issuing fresh consent URLs, and sessionStatus stays
# IN_PROGRESS forever. Every symptom reads "user never consented" when consent was in
# fact given and never *completed*.
#
# Setting this to the provider callback instead re-enters that endpoint with no
# code/state and fails: "2 validation errors detected: Value at 'authorizationCode'
# failed to satisfy constraint: Member must not be null; Value at 'state' ...".
#
# Injected so it can differ per environment (a real web front end would use its own
# "you're connected" page) and so it stays in sync with the WorkloadIdentity's
# allowedResourceOauth2ReturnUrls allowlist.
CONSENT_RETURN_URL = os.environ.get("OAUTH_CALLBACK_URL") or None

# Seconds to wait for consent inside one invocation. ZERO on purpose — see the
# class below. Overridable only as an escape hatch; the default should stay 0.
CONSENT_WAIT_SECONDS = float(os.environ.get("CONSENT_WAIT_SECONDS") or 0)
CONSENT_POLL_INTERVAL_SECONDS = 1.0


class NoWaitTokenPoller(TokenPoller):
    """Return control immediately instead of polling for consent.

    WHY NOT JUST WAIT. The SDK's default poller blocks for up to 600 seconds
    (DEFAULT_POLLING_TIMEOUT_SECONDS) waiting for a human to approve consent. Every
    part of that is wrong for a request/response agent, and the failures compound:

      - `on_auth_url` fires IMMEDIATELY, but the agent cannot RETURN the URL until
        the decorated call unblocks. With the default poller the consent URL
        therefore exists ONLY in the runtime's logs — `auth_required`, and the client
        code that prints it, are unreachable. The user is told "no sources
        connected" while their consent link sits in CloudWatch.
      - The caller gives up first regardless (client/ask.py uses a 120s HTTP
        timeout), so the extra minutes buy nothing but a hung request.
      - The authorization `request_uri` is single-use and SHORT-LIVED. Holding it
        server-side while nobody can see it means it has usually expired by the time
        a human finds it — exactly the `{"message":"Request invalid or expired"}`
        that made this concrete.

    WHY ZERO RATHER THAN A SHORT WAIT. A poller is only ever reached when the
    service returned an authorization URL, which means no token is vaulted for this
    user yet. The user has not seen the URL at that instant — we are still holding
    the response that carries it — so they cannot possibly have consented. Any wait
    at all is pure added latency on a request that is guaranteed to fail. If a token
    IS vaulted, `get_token` returns it directly and this class is never constructed.

    THIS IS THE EVENT-BASED DESIGN, not a workaround for the lack of one. Consent
    completion genuinely is event-driven — just not on this call path: the user
    approves, the vendor redirects to the AgentCore-hosted callback, and Identity
    completes the session and vaults the token server-side. There is no push or
    subscribe surface to await that from inside an invocation, and there does not
    need to be: the next question finds the vaulted token and never sees a consent
    URL again. One extra round trip, once per user per source, in exchange for a
    request that always returns promptly.
    """

    def __init__(self, auth_url: str, polling_func):
        self.auth_url = auth_url
        self.polling_func = polling_func

    async def poll_for_token(self) -> str:
        deadline = time.time() + CONSENT_WAIT_SECONDS
        # With CONSENT_WAIT_SECONDS = 0 this body never runs; kept so the escape
        # hatch actually works if someone sets it deliberately.
        while time.time() < deadline:
            await asyncio.sleep(CONSENT_POLL_INTERVAL_SECONDS)
            token = self.polling_func()
            if token is not None:
                return token
        # Not an error — this is the EXPECTED path for a first-time user. Worded for
        # whoever reads it in the logs.
        raise asyncio.TimeoutError(
            "no vaulted token for this user yet; returning the consent URL to the "
            "caller instead of holding the request open (see NoWaitTokenPoller)"
        )


# ---- data sources ----------------------------------------------------------
# ONE ENTRY PER SOURCE. GitHub is source #1, not the only one: the target state is
# Databricks Genie (MCP), Confluence, Slack, and Google Drive as well — the
# customer's current architecture already fans out to four MCP servers. Adding one
# here must not require touching invoke(), the response shape, or the client.
#
# To add a source:
#   1. Pick its `credential` kind (see below). DELEGATED_SUBJECT if the vendor can be
#      reached by impersonating a user (no consent); VAULTED_OAUTH otherwise.
#   2. For VAULTED_OAUTH: create its OAuth2 credential provider (a resource in
#      infra/02-runtime.yaml), paste the callback URL it issues into that vendor's
#      OAuth app (`Name` is createOnly, so a rename reissues the URL), and inject
#      <KEY>_CREDENTIAL_PROVIDER and <KEY>_SCOPES.
#      For DELEGATED_SUBJECT: inject whatever the impersonation needs (for Drive, a
#      Secrets Manager ARN) and name that variable in `enabled_by`.
#   3. Add a row below with the search callable, plus two optional ones: `fetch`
#      (a document's full text, for read_company_document) and `inventory` (what this
#      user can see, for list_available_sources). A source missing either is simply
#      not offered to that tool — neither is required to make the source useful.
#   4. Nothing else. Credential brokering, per-source consent URLs, the response
#      contract, and the client's output all iterate over this dict.
#
# A source is SKIPPED, not fatal, when its env vars are absent — so one template
# can deploy a GitHub-only stack and a four-source stack without code changes.
#
# ---- TOKEN PATH (`flow`) --------------------------------------------------------
# Per source, and resolved from CONFIG rather than hardcoded, which is requirement
# FR-34: a source must be migratable between paths with no code change. Two values:
#
#   USER_FEDERATION              3-legged OAuth. The user consents ONCE per source,
#                                and AgentCore Identity vaults a refresh token.
#   ON_BEHALF_OF_TOKEN_EXCHANGE  RFC 8693 token exchange. NO consent screen ever:
#                                the user's IdP JWT is exchanged for a token at the
#                                resource. Requires the IdP and the RESOURCE to
#                                support it (see the readiness note below).
#
# THE AGENT DOES NOT CARE WHICH. Both end at the identical guarantee — the API call
# runs as the user, so native ACLs decide visibility. What differs is who consents
# and who holds long-lived credentials. Keeping the choice in this table is what lets
# `invoke()` stay one loop over `ENABLED`.
#
# ⚠️ TOKEN EXCHANGE IS NOT UNIVERSALLY AVAILABLE, and the failure is not graceful — it
# is a hard error from the vendor's token endpoint. Verified empirically: GitHub returns
# `{"error": "unsupported_grant_type"}` for the token-exchange grant, so GitHub CANNOT
# use it no matter how identity is arranged. Do not set `flow` to
# ON_BEHALF_OF_TOKEN_EXCHANGE for a source without first confirming against that
# vendor's token endpoint that it accepts the exchange.
#
# ⚠️ DO NOT CALL THIS "OBO" — the word is retired in this repo, and this constant is
# why. It was `_FLOW_OBO`, which read as the generic "on behalf of the user" and so got
# applied to the GOOGLE DRIVE path in an early diagram. Drive is DELEGATED_SUBJECT below
# — a Google-signed service-account assertion, a different mechanism with a different
# blast radius, and one that can NEVER become a token exchange because Google requires
# the assertion be signed with a key we hold. Two mechanisms under one name meant
# reviewers could not tell which arrow was which. The full M1/M2/M3 vocabulary is in
# infra/render_components_v3.py.
_FLOW_USER_FEDERATION = "USER_FEDERATION"
_FLOW_TOKEN_EXCHANGE = "ON_BEHALF_OF_TOKEN_EXCHANGE"


def _flow_for(key: str, default: str = _FLOW_USER_FEDERATION) -> str:
    """Resolve a source's token path from `<KEY>_AUTH_FLOW`, defaulting to consent.

    Defaults to USER_FEDERATION deliberately: it works against any OAuth2 provider,
    whereas the token exchange silently requires vendor support. A wrong default that
    always works is better than one that fails at the first question with
    `unsupported_grant_type`.
    """
    val = (os.environ.get(f"{key.upper()}_AUTH_FLOW") or "").strip().upper()
    if not val:
        return default
    if val not in (_FLOW_USER_FEDERATION, _FLOW_TOKEN_EXCHANGE):
        raise RuntimeError(
            f"{key.upper()}_AUTH_FLOW must be {_FLOW_USER_FEDERATION} or "
            f"{_FLOW_TOKEN_EXCHANGE}, got {val!r}"
        )
    return val


# ---- CREDENTIAL KIND (`credential`) ---------------------------------------------
# HOW a source's per-user credential is obtained. Orthogonal to `flow`, which only
# describes the OAuth variant used by the vaulted path.
#
#   VAULTED_OAUTH   AgentCore Identity brokers a per-user token (consent or token
#                   exchange) and the search callable receives that TOKEN.
#   DELEGATED_SUBJECT
#                   No user token exists. The source impersonates the user by NAME,
#                   so the callable receives the VERIFIED EMAIL instead. Google Drive
#                   works this way, because Drive is guarded by Google and Okta cannot
#                   mint a token Drive accepts (see gdrive_tool's module docstring).
#
# Both preserve the invariant that makes this system safe: the call to the source runs
# as the user, so the SOURCE does the ACL trimming. What differs is what proves the
# identity — a token the user granted, versus a claim we cryptographically verified.
#
# ⚠️ DELEGATED_SUBJECT REQUIRES VERIFIED CLAIMS. A delegated source can reach ANY user
# in the domain, so its subject must come from identity_claims.verified_email() —
# signature, issuer and audience checked. The unverified `_user_sub()` decode below is
# fine for logging and MUST NEVER be used as a subject.
_CRED_VAULTED = "VAULTED_OAUTH"
_CRED_DELEGATED = "DELEGATED_SUBJECT"


# ---- ROUTE (`via`) --------------------------------------------------------------
# WHERE a source's call is made from, and therefore WHO HOLDS THE SOURCE TOKEN. A third
# axis, orthogonal to both `credential` and `flow` — which is why it is its own key
# rather than another value of either.
#
#   IN_PROCESS  This container calls the vendor's API directly. agent.py obtains the
#               per-user credential and passes it to the source's callable, so a source
#               access token lives in this process's memory for the turn.
#   GATEWAY     The call goes to the Tools Gateway over MCP, carrying the USER'S OWN IdP
#               JWT. The gateway authenticates that, then fetches the source token
#               itself from its target's credential provider. **This container never
#               sees a source token.**
#
# Both keep the invariant that matters: the call runs as the user and the SOURCE trims.
# What GATEWAY adds is that a compromise of this container yields no source credential.
#
# ⚠️ M2 DELEGATION CAN NEVER BE `GATEWAY`. This is structural, not a backlog item. The
# Gateway's OAuthGrantType enum is exactly {CLIENT_CREDENTIALS, AUTHORIZATION_CODE,
# TOKEN_EXCHANGE}; M2 needs an assertion signed with a *Google* service-account key,
# which is none of those and which a gateway could not mint regardless — it does not hold
# Google's key material. So `gdrive` stays IN_PROCESS permanently and the tools gateway
# is asymmetric by necessity. Documented, not hidden. (See infra/03-gateways.yaml.)
#
# ⚠️ AND `GATEWAY` IS NOT STRICTLY BETTER TODAY — GitHub defaults to IN_PROCESS. The
# gateway route loses GitHub's text-match fragments, because an OpenAPI target cannot
# vary the Accept header per call; hits arrive as metadata with no quotable text, and
# under STRICT GROUNDING that lands as LOW confidence rather than as an error. Reachable
# and wired, worse answers. Flip a source with <KEY>_VIA=GATEWAY once that is fixed.
_VIA_IN_PROCESS = "IN_PROCESS"
_VIA_GATEWAY = "GATEWAY"


def _via_for(key: str, default: str = _VIA_IN_PROCESS) -> str:
    """Resolve a source's route from `<KEY>_VIA`, defaulting to in-process.

    Same config-not-code rule as `_flow_for` (FR-34): a source moves between routes by
    configuration. Falls back to IN_PROCESS when the gateway is not deployed, so the
    agent still runs with no gateway at all rather than failing every search.
    """
    val = (os.environ.get(f"{key.upper()}_VIA") or "").strip().upper()
    if not val:
        return default
    if val not in (_VIA_IN_PROCESS, _VIA_GATEWAY):
        raise RuntimeError(
            f"{key.upper()}_VIA must be {_VIA_IN_PROCESS} or {_VIA_GATEWAY}, "
            f"got {val!r}"
        )
    if val == _VIA_GATEWAY and not gateway_tool.is_configured():
        print(f"WARNING: source '{key}' requests {_VIA_GATEWAY} but "
              f"RIPPLE_TOOLS_GATEWAY_URL is unset; falling back to "
              f"{_VIA_IN_PROCESS}.", flush=True)
        return _VIA_IN_PROCESS
    return val


SOURCES: dict[str, dict] = {
    "github": {
        "credential": _CRED_VAULTED,
        "via": _via_for("github"),
        "provider": os.environ.get("GITHUB_CREDENTIAL_PROVIDER"),
        "scopes": (os.environ.get("GITHUB_SCOPES") or "").split(),
        # GitHub does NOT support the token-exchange grant (tested: it returns
        # unsupported_grant_type), so this stays on the consent path. The env var can
        # still override it — for a future GitHub Enterprise / proxy that does.
        "flow": _flow_for("github"),
        "search": search_github,
        "fetch": fetch_document,
        "inventory": list_sources,
        "label": "GitHub",
    },
    # Google Drive / Docs. THE PRIMARY DEMO SOURCE: no consent screen ever appears,
    # and the per-user trim is still enforced by Drive itself. Enabled by setting
    # GOOGLE_SA_SECRET_ARN; absent, the row is skipped like any other.
    "gdrive": {
        "credential": _CRED_DELEGATED,
        # HARDCODED, not `_via_for("gdrive")`, and that asymmetry is the point: there is
        # no configuration under which M2 DELEGATION can traverse the gateway (see the
        # ROUTE note above). Making it settable would advertise a route that cannot work.
        "via": _VIA_IN_PROCESS,
        # No credential provider and no scopes: there is no vaulted OAuth token to
        # broker. Google scopes are granted ONCE by a Workspace admin in the
        # domain-wide delegation grant, not requested per user per call.
        "provider": None,
        "scopes": [],
        "enabled_by": "GOOGLE_SA_SECRET_ARN",
        "search": None,      # bound below, only if the dependency imports
        "fetch": None,
        "inventory": None,
        "label": "Google Drive",
    },
    # "confluence":      {... "search": search_confluence},   # Atlassian
    # "slack":           {... "search": search_slack},         # source, not the front door
    # "databricks_genie":{... "search": search_genie},         # MCP-native
    #
    # NOTE for the multi-source build: four sources is the point at which a Tools
    # Gateway (AgentCore Gateway, MCP fan-out) stops being optional — see
    # infra/ARCHITECTURE.md. Per-user permission trimming must stay enforced BY EACH
    # SOURCE via that user's own token; never a service account, never our own
    # filtering, no matter how the tools are routed.
}

# Bind the Drive callables only if the source is switched on AND its dependencies are
# present. The google-api-python-client import is deliberately guarded: an image built
# before those packages were added to requirements.txt must still serve GitHub rather
# than crash-loop on an ImportError at module scope. A configured-but-unimportable
# source is a loud startup message, not a dead runtime.
if SOURCES["gdrive"]["enabled_by"] and os.environ.get(SOURCES["gdrive"]["enabled_by"]):
    try:
        from gdrive_tool import (fetch_drive_document, list_drive_sources,
                                 search_drive)
        SOURCES["gdrive"].update({"search": search_drive,
                                  "fetch": fetch_drive_document,
                                  "inventory": list_drive_sources})
    except ImportError as e:
        print(f"WARNING: GOOGLE_SA_SECRET_ARN is set but the Google client libraries "
              f"are missing ({e}); the Drive source is DISABLED. Rebuild the image "
              f"with requirements.txt current.", flush=True)

# Rebind any GATEWAY-routed source's callables to the gateway client. Done here rather
# than inline in the table so the table states INTENT (`via`) and this states the single
# consequence of it — one place to look when a route behaves differently.
#
# ⚠️ ONLY `search` MOVES. `fetch` and `inventory` stay on their in-process
# implementations even for a gateway-routed source, because the gateway's GitHub target
# exposes only the two operations the scope composition needs (see infra/03-gateways.yaml).
# So a source is PARTLY routed, and the two halves need DIFFERENT credentials in the same
# turn: gateway `search` needs the user's IdP JWT, in-process `fetch` needs the vaulted
# source token. `_credential_for()` below resolves that per call, and the vaulted token is
# still fetched for a gateway-routed source precisely because fetch/inventory need it.
for _key, _src in SOURCES.items():
    if _src.get("via") == _VIA_GATEWAY and _key == "github":
        _src["search"] = gateway_tool.search_github_via_gateway
        print(f"source '{_key}' routes search through the Tools Gateway "
              f"({_VIA_GATEWAY}); this container will not hold its source token for "
              f"search.", flush=True)


def _is_enabled(src: dict) -> bool:
    """Whether a source is configured on this runtime.

    Two different tests, because the two credential kinds need different things:
    a vaulted source needs a credential provider and scopes; a delegated source needs
    its enabling env var and a bound search callable (the latter fails if the client
    libraries are absent, per the guarded import above).
    """
    if src.get("credential") == _CRED_DELEGATED:
        return bool(os.environ.get(src.get("enabled_by") or "") and src.get("search"))
    return bool(src.get("provider") and src.get("scopes"))


# Sources actually configured on this runtime. Enforced non-empty: an agent with no
# search source cannot answer anything under STRICT GROUNDING, so failing at import
# is better than serving confident-looking LOW-confidence answers forever.
ENABLED = {k: v for k, v in SOURCES.items() if _is_enabled(v)}
if not ENABLED:
    raise RuntimeError(
        "no data source is configured: set <SOURCE>_CREDENTIAL_PROVIDER and "
        "<SOURCE>_SCOPES, or a delegated source's enabling variable "
        "(e.g. GOOGLE_SA_SECRET_ARN), for at least one of " + ", ".join(SOURCES)
    )

# PROVISIONED BUT UNUSED — the switch from stateless to stateful.
# infra/02-runtime.yaml always creates the Memory resource and injects this, so
# turning on multi-turn memory needs no infrastructure change: read this id and
# follow the STATEFUL recipe in invoke()'s docstring. `.get` not `_require`
# precisely so the agent stays runnable stateless if the resource is absent.
MEMORY_ID = os.environ.get("AGENTCORE_MEMORY_ID")

# --- FUTURE: multi-agent + shared episodic memory (see docs/FUTURE-MULTI-AGENT.md) ---
# When we grow past one agent, derive every namespace from the authenticated `sub`
# so cross-user bleed is impossible by construction. Sketch:
#   def _ns(kind, sub):  # kind in {"facts","episodes"}
#       return f"user/{sub}/facts" if kind == "facts" else f"episodes/{sub}/shared"
# TODO(memory): create AgentCore Memory resource; store per-user facts in user/{sub}/facts.
# TODO(episodic): write_episode({goal,steps,tools,citations,outcome,ts}) to episodes/{sub}/shared.
# TODO(episodic): recall_episodes(sub, query) via search_long_term_memories -> inject as
#                 "prior related work (verify before reuse)"; NEVER treat an episode as a source.
# TODO(multiagent): a downstream specialist agent may READ episodes/{sub}/shared but must
#                   re-check live source ACLs on every retrieval. Shared memory = references +
#                   outcomes only, never ACL-restricted document content.

app = BedrockAgentCoreApp()

SYSTEM_PROMPT = """You are Ripple, an enterprise knowledge assistant.

TOOLS — pick by what the question is about:
- search_company_documents: which document says something. Content search.
- read_company_document: the full text of one hit. Search returns only the matching
  fragments, so READ THE PROMISING HITS before saying information is missing — a
  fragment is a pointer, not the document. Pass a result's "ref" verbatim.
- list_available_sources: what the user can access or owns (repositories, shared
  drives, documents, spaces). Content search cannot answer inventory questions, so do
  not substitute it. Each item carries a "kind" — describe items using their own kind
  rather than calling everything a repository or a file.

Typical shape of a good answer: search, read the two or three most promising hits,
then answer from their text.

STRICT GROUNDING:
- Answer ONLY from what these tools returned.
- Always call a tool before answering a factual question.
- Cite every substantive claim with inline markers [1], [2] that map to the Sources list.
- The tools see ONLY what this user is permitted to see. Never guess about documents
  you could not retrieve, and never mention titles you did not retrieve.
- Each result carries a "source" field. When results come from more than one source,
  make clear which source each cited claim came from.
- If a document comes back with "truncated": true, you read a prefix — do not conclude
  it fails to mention something. A result may also carry a "note" explaining a limit of
  what could be extracted (a spreadsheet's first sheet only, a format whose text could
  not be read); respect it the same way and say so rather than treating silence as
  absence.
- If nothing relevant is returned, say so plainly. If a result carries an "error"
  field, that source could not be searched — say which one, and do not imply its
  content was checked and found empty. Never invent facts.

CONFIDENCE (exactly one band at the end):
- HIGH: answer directly & fully supported by retrieved sources.
- MEDIUM: partial support or light inference.
- LOW: little/no relevant retrieved support (say what is missing). Do not settle for
  LOW because a search snippet was thin — read the document first.

OUTPUT FORMAT:
<answer with inline [n] citations>

Sources:
[1] <title> — <url>

Confidence: HIGH | MEDIUM | LOW — <one short reason>"""


def _caller_jwt(context) -> str:
    """The caller's JWT, taken from the HEADER the authorizer validated.

    THE ONLY PLACE THE USER'S TOKEN ENTERS THE AGENT, and it must stay that way. The
    request body carries the QUESTION; the header carries WHO IS ASKING. Accepting an
    identity from the body as well would mean the value the CUSTOM_JWT authorizer
    authenticated and the value that selects whose Drive is read are two different
    strings with nothing forcing them to match — a caller could pair a valid token for
    themselves in the header with a valid token for a colleague in the body, and both
    would verify while the impersonation followed the body. Reading only the header
    makes that divergence unrepresentable instead of something to check for.

    HOW THE HEADER GETS HERE. The runtime SDK gives `Authorization` its own branch when
    it builds the request context (bedrock_agentcore/runtime/app.py), exempt from the
    RESTRICTED_HEADERS allowlist that blocks Proxy-Authorization, Cookie and the
    X-Forwarded-* family, and normalises the key to canonical casing regardless of what
    the wire used. `context.request_headers["Authorization"]` is a supported read.

    FAILS CLOSED, DELIBERATELY. `request_headers` is None when no forwardable header
    arrived, so the `or {}` matters: an absent header yields "" and lets
    verified_claims() refuse, rather than producing a default subject. "" is not
    itself an error — a SigV4-authenticated caller legitimately has no user JWT here
    (the SDK expects X-Amzn-Bedrock-AgentCore-Runtime-User-Id instead) and simply finds
    every delegated source unavailable for the turn.

    THIS DOES NOT REPLACE VERIFICATION. The header proves only that the platform let the
    request through. identity_claims.verified_email() still checks signature, issuer,
    audience, `email_verified` and the domain allowlist — two of those five are things
    no authorizer checks (see identity_claims.py).
    """
    headers = getattr(context, "request_headers", None) or {}
    raw = headers.get("Authorization") or ""
    # Case-insensitive scheme, and tolerate the token being sent bare. Anything that is
    # not a Bearer credential is not one we can use.
    parts = raw.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return raw.strip() if len(parts) == 1 else ""


def _user_sub(access_token: str) -> str:
    """The stable user id, for LOGGING AND LABELLING ONLY — unverified decode.

    Never an authorization input. `verify_signature: False` means an expired or
    malformed token still yields a plausible-looking `sub`, which is fine for a log line
    and wrong for a decision. verified_email() is the only source of an impersonation
    subject. The input is the authenticated header rather than anything the caller can
    set independently, which is what keeps an unverified decode acceptable at all here.
    """
    try:
        claims = jwt.decode(access_token, options={"verify_signature": False})
        return claims.get("sub") or claims.get("email") or "anonymous"
    except Exception:
        return "anonymous"


def _build_agent(tokens: dict[str, str], user_jwt: str = "",
                 gateway_consent: dict[str, str] | None = None) -> Agent:
    """Build the turn's agent.

    `tokens` maps source key -> that source's PER-USER CREDENTIAL, which is one of two
    things depending on the source's `credential` kind:
      VAULTED_OAUTH      an OAuth access token belonging to this user
      DELEGATED_SUBJECT  this user's VERIFIED email, which the source impersonates
    Absent or empty means unavailable, so that source is not searched.

    `user_jwt` is the caller's own IdP token, needed for GATEWAY-routed calls: the
    gateway authenticates the USER and fetches the source token itself, so the credential
    it wants is the JWT rather than anything in `tokens`.

    `gateway_consent` is an OUT-parameter: the tools below write `source key -> consent
    URL` into it when a GATEWAY-routed source turns out to need this user's authorization.
    It exists because that fact is only discoverable DURING the turn — the pre-flight loop
    in `invoke()` cannot find it, since a gateway source has no token to fetch here (the
    gateway holds it), so its consent state is unknown until a tool actually calls out.
    A mutable dict rather than a return value because the caller is the model's tool loop,
    which has no channel back to `invoke()`. Same role `auth_urls` plays for the
    in-process path, and it feeds the same `auth_required` key in the response.

    The tools below still do not branch on the credential KIND — each source's callables
    know what they receive. What they do consult is `_credential_for()`, because a
    partly-routed source needs a different credential for `search` than for `fetch`
    within one turn.
    """

    def _credential_for(key: str, operation: str) -> str:
        """The credential this source's `operation` callable expects.

        Routing is PER OPERATION, not per source, because only `search` moves onto the
        gateway (see the rebinding loop above). Getting this wrong is silent rather than
        loud — handing a gateway callable a GitHub token produces a 401 from the
        gateway's authorizer, which reads as "the gateway is broken" — so the choice is
        made in one place against the same `via` flag that did the rebinding.
        """
        src = ENABLED.get(key) or {}
        if src.get("via") == _VIA_GATEWAY and operation == "search":
            return user_jwt
        return tokens.get(key) or ""

    @tool
    def search_company_documents(query: str) -> str:
        """Search the user's connected company knowledge sources for content
        relevant to the query. Returns only items the current user is authorized
        to read.

        Args:
            query: keywords or a natural-language question
        """
        # Every connected source is searched and the hits are merged. One source
        # failing must not lose the others' results, so each is wrapped
        # individually — a Confluence outage should degrade the answer, not void it.
        hits: list = []
        for key, src in ENABLED.items():
            token = _credential_for(key, "search")
            if not token:
                continue
            try:
                found = src["search"](token, query)
                # Stamp provenance so the model can attribute correctly when more
                # than one source is live. Only fills it in if the tool did not.
                for h in found:
                    h.setdefault("source", key)
                    # Namespace the fetch handle here rather than in each source's
                    # tool, so a new source cannot forget to do it and collide with
                    # another's ref format.
                    if h.get("ref"):
                        h["ref"] = f"{key}::{h['ref']}"
                hits.extend(found)
            except gateway_tool.ElicitationRequired as e:
                # The gateway needs its OWN consent for this user (a second consent —
                # see gateway_tool.ELICITATION_CODE). Record the URL for `auth_required`
                # so the client can drive it, and tell the MODEL that the source is
                # unconnected rather than broken: the previous generic error made it
                # write "the connector returned an error… it likely needs attention from
                # whoever manages the integration", pointing the user at an operator for
                # something only the user can do.
                if gateway_consent is not None:
                    gateway_consent[key] = e.url
                # Deliberately NOT the URL itself. The model would print it, and a
                # hand-opened URL does not complete the flow (client/consent.py) — the
                # client has the return-URL machinery, the answer text does not.
                hits.append({"source": key, "error": (
                    "not connected: this source needs a one-time authorization from "
                    "you. The client will prompt for it — no action is needed from an "
                    "administrator.")})
            except Exception as e:  # surface auth/permission errors cleanly
                hits.append({"source": key, "error": str(e)[:200]})
        return json.dumps(hits)

    @tool
    def read_company_document(ref: str) -> str:
        """Read the FULL text of one document returned by search_company_documents.

        Search returns only the fragments that matched, which is often too little to
        answer with. Call this on a promising hit before concluding that information
        is missing.

        Args:
            ref: the exact "ref" value from a search result. Do not construct one.
        """
        # `ref` is prefixed with its source key so a hit can be routed home without
        # the model tracking which source produced it. Sources are searched in
        # parallel and the model sees one merged list, so an unprefixed ref would be
        # ambiguous the moment a second source is enabled.
        key, _, native = ref.partition("::")
        if not native:  # unprefixed: only unambiguous while exactly one source is live
            fetchable = [k for k, s in ENABLED.items()
                         if s.get("fetch") and _credential_for(k, "fetch")]
            if len(fetchable) != 1:
                return json.dumps({"error": (
                    f"ambiguous ref {ref!r}: prefix it with a source key "
                    f"({', '.join(fetchable) or 'none available'})")})
            key, native = fetchable[0], ref
        src = ENABLED.get(key)
        if not src or not src.get("fetch"):
            return json.dumps({"error": f"source '{key}' cannot read documents"})
        token = _credential_for(key, "fetch")
        if not token:
            return json.dumps({"error": f"source '{key}' is not connected"})
        try:
            doc = src["fetch"](token, native)
        except Exception as e:
            return json.dumps({"source": key, "error": str(e)[:200]})
        if isinstance(doc, dict):
            doc.setdefault("source", key)
        return json.dumps(doc)

    @tool
    def list_available_sources() -> str:
        """List the repositories, spaces or drives this user can access, with
        descriptions. Use for questions about WHAT the user has access to or owns
        ("which repositories can I see?", "list my projects") — questions about
        inventory rather than document content, which search cannot answer.

        `owned_by_user` marks items this user owns, and `kind` says what each item is
        (repository, shared drive, google doc, ...). Neither GitHub nor Drive exposes
        who CREATED an item, so report ownership as ownership; do not claim authorship.
        """
        items: list = []
        for key, src in ENABLED.items():
            token = _credential_for(key, "inventory")
            if not token or not src.get("inventory"):
                continue
            try:
                found = src["inventory"](token)
                for h in found:
                    h.setdefault("source", key)
                items.extend(found)
            except Exception as e:
                items.append({"source": key, "error": str(e)[:200]})
        return json.dumps(items)

    return Agent(
        model=BedrockModel(model_id=MODEL_ID, region_name=REGION),
        system_prompt=SYSTEM_PROMPT,
        tools=[search_company_documents, read_company_document,
               list_available_sources],
    )


@app.entrypoint
async def invoke(payload, context):
    """AgentCore Runtime entrypoint — STATELESS. One invocation, one answer.

    payload: {"prompt": "..."}
    identity: the caller's JWT, read from the `Authorization` header via `context` —
      never from the payload. The body carries the QUESTION; the header carries WHO IS
      ASKING, and only one of those is authenticated. See `_caller_jwt()`.

    DO NOT RENAME THE `context` PARAMETER. The SDK decides whether to pass the request
    context by inspecting this signature for a second parameter literally named
    "context" (`_takes_context` in bedrock_agentcore/runtime/app.py). Any other name and
    the handler is called with the payload alone — so the failure mode of a rename is a
    TypeError on a missing argument if you are lucky, and every delegated source
    silently unavailable if the parameter has a default. Neither says "you renamed the
    thing that carries the user's identity".

    WHAT "STATELESS" MEANS HERE
    Everything this function needs arrives in `payload`. It reads no prior turn,
    writes no turn for a successor to read, and keeps no module-level mutable
    state. The client does send a session id header
    (X-Amzn-Bedrock-AgentCore-Runtime-Session-Id, see client/ask.py) which the
    platform uses to group turns for observability and billing — but this code
    never reads it, so it does not influence the answer. Grouping is not memory.

    WHY STATELESS
    Horizontal scale for free (no affinity, no shared session store, no sticky
    load balancing), and no cross-user bleed is possible by construction: there is
    no shared container for one user's data to leak into another's turn. This is
    the same direction MCP 2026-07-28 took when it deleted protocol sessions.

    ------------------------------------------------------------------------
    HOW TO MAKE THIS STATEFUL (multi-turn conversation)
    The infrastructure is already provisioned: AGENTCORE_MEMORY_ID points at an
    AgentCore Memory resource created by infra/02-runtime.yaml. It is deliberately
    unused today. To turn it on:

      1. Read the session id, and make it the memory partition key. It must come
         from `context`, NOT from `payload` — a client-supplied conversation id is
         a cross-user read primitive, since anyone could pass someone else's.
             session_id = getattr(context, "session_id", None)
      2. Scope every memory call by BOTH `user_sub` and `session_id`. `user_sub`
         is the security boundary (it comes from the validated JWT); session_id
         only separates one conversation from another within that user.
             actor_id = user_sub                  # authorization boundary
             namespace = f"user/{user_sub}/session/{session_id}"
      3. Load prior turns before building the agent and pass them in as
         `messages=`, rather than concatenating them into the prompt string —
         Strands keeps roles intact that way, and role confusion is what lets a
         retrieved document impersonate a user instruction.
      4. Write this turn after answering (user message + assistant answer).
      5. Bound it explicitly: a turn cap or token budget. The Memory resource's
         EventExpiryDuration (MemoryExpiryDays, default 90) caps RETENTION, not
         context size, so it will not save you here. Unbounded history grows cost
         per turn forever and eventually pushes the system prompt out of the window.

    What you give up, and must then handle:
      - Affinity. Turn N+1 must see turn N's write, so the write has to be
        durable before the response returns, or a retry on another instance reads
        stale history.
      - Grounding. Retrieved history is NOT a citable source. Keep it out of the
        Sources list, or the confidence band starts vouching for the model's own
        earlier claims — a self-confirming loop.
      - Deletion. Stored conversation is user data. GDPR/DSAR deletion becomes a
        real code path, which is why this is off until someone asks for it.
    ------------------------------------------------------------------------
    """
    prompt = payload.get("prompt", "").strip()
    # From the HEADER, not the payload. This one line is the identity binding: the value
    # that names the user is the same value the authorizer authenticated.
    user_jwt = _caller_jwt(context)
    if not prompt:
        return {"error": "empty prompt"}

    user_sub = _user_sub(user_jwt)

    # Per-user token PER SOURCE, brokered by AgentCore Identity. Each source's `flow`
    # decides HOW (consent vs token exchange); this loop is identical either way.
    # Sources are independent: a user connected to GitHub but not Confluence gets
    # GitHub results, not an error.
    #
    # ---- HOW THE USER'S IDENTITY REACHES THE EXCHANGE -----------------------------
    # Nothing here passes `user_jwt` to Identity explicitly, and that is correct — it
    # would be the wrong design, because a token the agent chooses is a token the agent
    # could forge. The propagation is ambient and platform-enforced:
    #
    #   1. The caller presents the user's IdP JWT as `Authorization: Bearer`.
    #   2. The runtime's CUSTOM_JWT authorizer VALIDATES it (signature, iss, aud) via
    #      JWKS. An invalid token never reaches this container at all.
    #   3. The platform then injects a `WorkloadAccessToken` request header, which the
    #      SDK stores in a contextvar (runtime/app.py `_build_request_context`).
    #   4. `@requires_access_token` reads that contextvar and sends it as
    #      `workloadIdentityToken` on GetResourceOauth2Token.
    #
    # So the user identity Identity acts on is the one the AUTHORIZER verified, not one
    # this code asserts. That is exactly the property you want from a brokered exchange:
    # the agent cannot request a token for a user it cannot prove. `user_jwt` is used only
    # to read `sub` for logging/partitioning — never as a credential.
    #
    # THE DELEGATED PATH SHARES THAT PROPERTY BY A DIFFERENT ROUTE. `user_jwt` comes from
    # the `Authorization` header (see `_caller_jwt`) — the same string the authorizer
    # validated. So the vaulted path derives the user from the WorkloadAccessToken
    # contextvar and the delegated path from the header, and neither reads anything a
    # caller can vary independently of what was authenticated.
    #
    # CONSEQUENCE FOR A SLACK FRONT END: the receiver must forward a real per-user IdP
    # JWT. A bot/service token would make every user share one identity and silently
    # collapse the permission trim, and SIGV4 inbound auth requires the
    # X-Amzn-Bedrock-AgentCore-Runtime-User-Id header instead (see the SDK's error text
    # in identity/auth.py `_get_workload_access_token`).
    tokens: dict[str, str] = {}
    auth_urls: dict[str, str] = {}
    errors: dict[str, str] = {}   # source key -> why its token could not be obtained

    # DELEGATED sources first: no Identity round trip, no consent, nothing to vault.
    # The "credential" is the user's own verified email, and the source impersonates
    # them by name. This is the path that delivers the desired outcome — zero user
    # interaction with a per-user ACL trim still enforced by the source.
    #
    # VERIFICATION IS MANDATORY HERE even though the token arrives on the authenticated
    # header, because two of the five gates in verified_email() are things NO authorizer
    # checks: `email_verified` must be TRUE (an issuer permitting self-signup will
    # genuinely sign a token whose `email` is a colleague's address), and the domain must
    # sit inside the allowlist the delegation grant actually covers. Signature/iss/aud are
    # re-checked too — "it reached this container, therefore it was validated" is an
    # undocumented platform invariant, and confirming it costs one cached JWKS lookup per
    # ten minutes.
    #
    # WHY THE SUBJECT COMES FROM THE HEADER AND NOWHERE ELSE. Taking it from the request
    # body instead would let a caller pair a valid header for themselves with a body token
    # naming someone else: the authorizer checks one string, the impersonation follows the
    # other, and both verify. There is no body field here to disagree with the header.
    # `_user_sub()` decodes without checking the signature, which is acceptable for a log
    # field and would not be for a subject.
    delegated_subject: str | None = None
    if any(s.get("credential") == _CRED_DELEGATED for s in ENABLED.values()):
        try:
            delegated_subject = verified_email(user_jwt)
        except ClaimVerificationError as e:
            # Every delegated source is unavailable this turn. Deliberately NOT
            # degraded to an unverified fallback: answering from the wrong user's
            # documents is far worse than not answering.
            # GENERIC message to the caller; the REASON goes only to the log.
            # identity_claims' errors quote token internals — the expected `iss`, the
            # expected `aud`, which `kid` values the JWKS carries. Returning those
            # verbatim (which this did) hands an unprivileged caller a tuning oracle
            # for forging the very claim that selects whose Drive is read. The
            # operator gets the detail from CloudWatch; the caller gets "no".
            for key, src in ENABLED.items():
                if src.get("credential") == _CRED_DELEGATED:
                    errors[key] = (
                        "cannot verify the caller's identity, so no user can be "
                        "impersonated. See the runtime logs for the reason.")
            print(f"ERROR: claim verification failed; delegated sources disabled "
                  f"for this turn: {e}", flush=True)

    for key, src in ENABLED.items():
        if src.get("credential") == _CRED_DELEGATED:
            if delegated_subject:
                # The subject IS the credential for this source kind. Stored in the
                # same dict so _build_agent's tools stay one uniform loop.
                tokens[key] = delegated_subject
            continue

        flow = src.get("flow") or _FLOW_USER_FEDERATION

        # USER_FEDERATION is a 3-legged flow: the FIRST time a given user asks,
        # there is no vaulted token yet and AgentCore Identity produces a consent
        # URL that this user must visit once. Capturing it is not optional — the SDK
        # hands it to `on_auth_url` and NOWHERE else, so without this callback the
        # URL is discarded and the user can never connect. Every answer then comes
        # back LOW confidence with no sources, which looks like a broken agent
        # rather than a missing consent.
        #
        # On the token-exchange path this callback should NEVER fire. If it does, the
        # exchange silently degraded to interactive consent — which defeats the entire
        # point of choosing it — so it is recorded as an ERROR rather than shown to the
        # user. FR-5a: that is a POLICY failure, not a consent gap, and must never be
        # handled by prompting a user to consent to something they cannot self-grant.
        def _capture(url: str, _k: str = key, _flow: str = flow) -> None:
            if _flow == _FLOW_TOKEN_EXCHANGE:
                errors[_k] = (
                    "ON_BEHALF_OF_TOKEN_EXCHANGE unexpectedly returned an interactive "
                    "consent URL, which means the exchange did not succeed. Treating as a "
                    "policy failure, not a consent prompt (FR-5a). Check that this "
                    "resource accepts the RFC 8693 grant and that the user's IdP "
                    "token has the required scopes."
                )
                print(f"ERROR: source '{_k}' is configured for {_FLOW_TOKEN_EXCHANGE} but the "
                      f"service returned a consent URL; not surfacing it to the user.",
                      flush=True)
                return
            auth_urls[_k] = url          # _k default-binds the loop variable

        @requires_access_token(
            provider_name=src["provider"],
            scopes=src["scopes"],
            # Per-source, from config — NOT hardcoded. See `_flow_for` and FR-34.
            auth_flow=flow,
            into="access_token",
            on_auth_url=_capture,
            # Where the vendor redirects after the user approves consent. The SDK
            # normally lifts this from the inbound `OAuth2CallbackUrl` request header
            # (runtime/app.py -> BedrockAgentCoreContext), which a browser-based front
            # end sets to its own return page. A CLI caller has no such page and does
            # not send the header, so callback_url resolves to None and the service
            # rejects the call outright: "You must provide a ResourceOauth2ReturnUrl
            # to proceed with this flow".
            #
            # Passing it explicitly makes the agent work for BOTH callers: the SDK
            # prefers a user-provided value and only falls back to the header, so a
            # future web front end still overrides this by sending the header.
            # It must be the AgentCore-hosted callback that the credential provider
            # already registered — the same URL pasted into the vendor's OAuth app.
            # Any other value is rejected as unregistered.
            callback_url=CONSENT_RETURN_URL,
            # Do not block this request waiting for a human to click consent. Without
            # this the SDK polls for 600s and the consent URL never escapes the logs.
            token_poller=NoWaitTokenPoller(
                CONSENT_RETURN_URL or "",
                lambda: None,   # never consulted: CONSENT_WAIT_SECONDS is 0
            ),
        )
        async def _fetch(access_token: str = "", _k: str = key):
            tokens[_k] = access_token or ""

        try:
            await _fetch()
        except Exception as e:
            # Not consented / unavailable / consent poll timed out. We still answer
            # the turn using whatever other sources are connected. Deliberately not
            # re-raised: a knowledge question should degrade to "I could not search
            # X" rather than fail the whole invocation.
            #
            # But it MUST be logged. This used to be a bare `except: pass`, which
            # made the single most failure-prone step in the system — a 3-legged
            # OAuth handshake against a third party — completely silent. The symptom
            # (LOW confidence, no sources, no consent URL) is identical for "user
            # hasn't consented yet", "provider name is wrong", "scopes rejected",
            # and "no workload identity in this request", so without the exception
            # text there is nothing to debug. Never re-silence this.
            errors[key] = f"{type(e).__name__}: {e}"
            print(f"ERROR: token fetch failed for source '{key}': "
                  f"{type(e).__name__}: {e}", flush=True)

    # A GATEWAY-routed source is connected when the CALLER is authenticated, because the
    # gateway holds the source credential rather than this process. Reporting it by
    # `tokens` alone would understate what the turn can actually reach — and would show a
    # source as disconnected while it was answering.
    connected = sorted(
        k for k, v in ENABLED.items()
        if tokens.get(k) or (v.get("via") == _VIA_GATEWAY and user_jwt)
    )
    # A FRESH Agent per invocation — this line is what makes the turn stateless.
    # Do not hoist it to module scope to "save time": a module-level Agent would
    # accumulate conversation across DIFFERENT USERS sharing a warm container,
    # which is a cross-user data leak, not a cache. Building it is cheap; the
    # tool closure also has to re-bind this turn's per-source tokens anyway.
    # Filled DURING the turn by the tools, not before it — see _build_agent's docstring.
    gateway_consent: dict[str, str] = {}
    agent = _build_agent(tokens, user_jwt, gateway_consent)
    # Stateful variant: agent(prompt) becomes agent(prompt, messages=history) —
    # see the STATEFUL block in this function's docstring.
    answer = str(agent(prompt)).strip()

    band = "LOW"
    for line in answer.splitlines():
        if line.strip().upper().startswith("CONFIDENCE:"):
            up = line.upper()
            band = "HIGH" if "HIGH" in up else "MEDIUM" if "MEDIUM" in up else "LOW"
            break

    result = {
        "user": user_sub,
        "answer": answer,
        "confidence": band,
        # A gateway source is counted optimistically above (the caller is authenticated,
        # so it SHOULD be reachable). If the turn then discovered it needs consent, that
        # optimism was wrong and has to be withdrawn — otherwise the response says
        # `connected_sources: ['github']` while the answer says GitHub could not be
        # searched, and `ask.py` exits 0 on a fully ungrounded answer because its
        # "nothing was searched" test reads this list. Observed exactly so.
        "connected_sources": [k for k in connected if k not in gateway_consent],
    }
    # Consent URLs for sources this user has NOT connected yet, keyed by source.
    # A DICT, not a single `github_auth_url` string, deliberately: with four sources
    # a user may need to authorize several, and a scalar key would have to be a
    # breaking wire-format change later. Absent entirely once everything is vaulted,
    # so a client can treat its presence as "action required from this user".
    pending = {k: u for k, u in auth_urls.items() if not tokens.get(k)}
    # A GATEWAY-routed source's consent URL comes from the gateway itself, mid-turn, and
    # merges into the SAME key — a client should not need to know which route a source is
    # on to know that its user must click something. `gateway_consent` wins on collision
    # because it is the route actually in use for that source this turn.
    pending.update(gateway_consent)
    if pending:
        result["auth_required"] = pending

    # Sources that failed WITHOUT producing a consent URL. This is a different
    # condition from `auth_required` and must not be collapsed into it: a consent URL
    # means "the user needs to act", while an entry here means "the handshake broke
    # before a URL could even be issued" — a misconfigured provider name, rejected
    # scopes, or a missing workload identity. Both previously looked identical to the
    # caller (LOW confidence, no sources), which is what made this hard to diagnose.
    unavailable = {k: e for k, e in errors.items() if not tokens.get(k) and k not in pending}
    if unavailable:
        result["source_errors"] = unavailable

    # Which token path each source used, so a caller can tell "the user must click
    # something" apart from "an admin must fix a grant" WITHOUT parsing error strings.
    # A token-exchange source can never appear in `auth_required` (see `_capture`), so a
    # Slack front end can show a consent button for USER_FEDERATION sources and route
    # exchange failures to an operator instead of prompting a user who cannot self-grant.
    #
    # A DELEGATED source reports its credential kind rather than an OAuth flow, because
    # calling it USER_FEDERATION would be a lie that matters: a client would render a
    # "connect your account" button for a source that never needs one. It can never
    # appear in `auth_required` either — there is no consent URL in that path at all.
    result["source_flows"] = {
        k: (_CRED_DELEGATED if v.get("credential") == _CRED_DELEGATED
            else (v.get("flow") or _FLOW_USER_FEDERATION))
        for k, v in ENABLED.items()
    }

    # WHERE each source was called from. A SEPARATE key rather than a new value inside
    # `source_flows`, deliberately: route and flow are orthogonal (a GATEWAY source still
    # has an OAuth flow, it is just executed elsewhere), and folding them together would
    # both lose information and break any client already switching on `source_flows`.
    #
    # Useful to a caller for one concrete reason: it says who held the source token this
    # turn, which is the difference between a source token having been present in this
    # container's memory and not. That is an audit-relevant fact and is not derivable from
    # anything else in the response.
    result["source_routes"] = {
        k: (v.get("via") or _VIA_IN_PROCESS) for k, v in ENABLED.items()
    }
    return result


if __name__ == "__main__":
    app.run()
