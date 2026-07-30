"""Ripple Knowledge Assistant — AgentCore Runtime agent (harness entrypoint).

Architecture (real):
  Auth0 user JWT (from ingress gateway / caller)
    -> BedrockAgentCoreApp entrypoint (this file, on AgentCore Runtime)
    -> AgentCore Identity mints a per-user token PER SOURCE via 3-legged OAuth
       consent (USER_FEDERATION), vaulted + refreshed.
    -> Strands agent (Claude Opus 4.8 on Bedrock) with one search tool that fans
       out across every source this user has connected
    -> answer with inline citations + High/Medium/Low confidence band
  Per-user memory via AgentCore Memory, partitioned by the Auth0 'sub'.

DATA SOURCES: GitHub is implemented; Databricks Genie (MCP), Confluence, Slack, and
Google Drive are the target state. Everything here iterates over the SOURCES table
below, so adding one is a table entry plus its credential provider — not a change to
invoke(), the response shape, or the client. See the comment on SOURCES.

Permission trim: each token belongs to the signed-in user, so each source returns
ONLY what that user can access. The trim is enforced BY THE SOURCE, not by us, and
that must stay true for every source added — never a service account, never our own
filtering. A user who hasn't connected a source simply gets no hits from it.

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

from github_tool import fetch_document, list_sources, search_github

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

# Where AgentCore sends the USER'S BROWSER once it has vaulted the token, i.e. a
# landing page. Two URLs are easy to confuse here, and swapping them silently breaks
# the final hop of consent:
#
#   provider callback   .../identities/oauth2/callback/<uuid>  — receives GITHUB's
#                       redirect, carrying ?code=&state=. Goes in the vendor's OAuth
#                       app. NOT this value.
#   return url (this)   where the user lands AFTER the token is vaulted. Consent is
#                       already finished; nothing is appended to it.
#
# Setting this to the provider callback re-enters that endpoint with no code/state and
# fails: "2 validation errors detected: Value at 'authorizationCode' failed to satisfy
# constraint: Member must not be null; Value at 'state' ...".
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
#   1. Create its OAuth2 credential provider (a resource in infra/02-runtime.yaml),
#      and paste the callback URL it issues into that vendor's OAuth app. `Name` is
#      createOnly, so a rename reissues the URL.
#   2. Inject <KEY>_CREDENTIAL_PROVIDER and <KEY>_SCOPES as runtime env vars.
#   3. Add a row below with the search callable, plus two optional ones: `fetch`
#      (a document's full text, for read_company_document) and `inventory` (what this
#      user can see, for list_available_sources). A source missing either is simply
#      not offered to that tool — neither is required to make the source useful.
#   4. Nothing else. Token brokering, per-source consent URLs, the response
#      contract, and the client's output all iterate over this dict.
#
# A source is SKIPPED, not fatal, when its env vars are absent — so one template
# can deploy a GitHub-only stack and a four-source stack without code changes.
SOURCES: dict[str, dict] = {
    "github": {
        "provider": os.environ.get("GITHUB_CREDENTIAL_PROVIDER"),
        "scopes": (os.environ.get("GITHUB_SCOPES") or "").split(),
        "search": search_github,
        "fetch": fetch_document,
        "inventory": list_sources,
        "label": "GitHub",
    },
    # "confluence":      {... "search": search_confluence},   # Atlassian
    # "gdrive":          {... "search": search_gdrive},
    # "slack":           {... "search": search_slack},         # source, not the front door
    # "databricks_genie":{... "search": search_genie},         # MCP-native
    #
    # NOTE for the multi-source build: four sources is the point at which a Tools
    # Gateway (AgentCore Gateway, MCP fan-out) stops being optional — see
    # infra/ARCHITECTURE.md. Per-user permission trimming must stay enforced BY EACH
    # SOURCE via that user's own token; never a service account, never our own
    # filtering, no matter how the tools are routed.
}

# Sources actually configured on this runtime. Enforced non-empty: an agent with no
# search source cannot answer anything under STRICT GROUNDING, so failing at import
# is better than serving confident-looking LOW-confidence answers forever.
ENABLED = {k: v for k, v in SOURCES.items() if v["provider"] and v["scopes"]}
if not ENABLED:
    raise RuntimeError(
        "no data source is configured: set <SOURCE>_CREDENTIAL_PROVIDER and "
        "<SOURCE>_SCOPES for at least one of " + ", ".join(SOURCES)
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
- list_available_sources: what the user can access or owns (repositories, spaces,
  drives). Content search cannot answer inventory questions, so do not substitute it.

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
  it fails to mention something.
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


def _user_sub(access_token: str) -> str:
    """Extract the stable user id from the inbound Auth0 JWT (unverified decode;
    signature is validated at the gateway/authorizer boundary)."""
    try:
        claims = jwt.decode(access_token, options={"verify_signature": False})
        return claims.get("sub") or claims.get("email") or "anonymous"
    except Exception:
        return "anonymous"


def _build_agent(tokens: dict[str, str]) -> Agent:
    """Build the turn's agent. `tokens` maps source key -> that user's access token
    (absent or empty = not connected, so that source is not searched)."""

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
            token = tokens.get(key)
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
                         if s.get("fetch") and tokens.get(k)]
            if len(fetchable) != 1:
                return json.dumps({"error": (
                    f"ambiguous ref {ref!r}: prefix it with a source key "
                    f"({', '.join(fetchable) or 'none available'})")})
            key, native = fetchable[0], ref
        src = ENABLED.get(key)
        if not src or not src.get("fetch"):
            return json.dumps({"error": f"source '{key}' cannot read documents"})
        token = tokens.get(key)
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

        `owned_by_user` marks items this user owns. GitHub does not expose who
        created a repository, so report ownership as ownership; do not claim
        authorship.
        """
        items: list = []
        for key, src in ENABLED.items():
            token = tokens.get(key)
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

    payload: {"prompt": "...", "access_token": "<auth0 user JWT>"}
      (In production the ingress gateway passes the JWT through; access_token in
       the payload lets the CLI client drive the same path directly.)

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
    user_jwt = payload.get("access_token", "")
    if not prompt:
        return {"error": "empty prompt"}

    user_sub = _user_sub(user_jwt)

    # Per-user token PER SOURCE, brokered by AgentCore Identity via 3-legged OAuth
    # consent (vaulted + refreshed). Sources are independent: a user connected to
    # GitHub but not Confluence gets GitHub results, not an error.
    tokens: dict[str, str] = {}
    auth_urls: dict[str, str] = {}
    errors: dict[str, str] = {}   # source key -> why its token could not be obtained

    for key, src in ENABLED.items():
        # USER_FEDERATION is a 3-legged flow: the FIRST time a given user asks,
        # there is no vaulted token yet and AgentCore Identity produces a consent
        # URL that this user must visit once. Capturing it is not optional — the SDK
        # hands it to `on_auth_url` and NOWHERE else, so without this callback the
        # URL is discarded and the user can never connect. Every answer then comes
        # back LOW confidence with no sources, which looks like a broken agent
        # rather than a missing consent.
        def _capture(url: str, _k: str = key) -> None:
            auth_urls[_k] = url          # _k default-binds the loop variable

        @requires_access_token(
            provider_name=src["provider"],
            scopes=src["scopes"],
            auth_flow="USER_FEDERATION",
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

    connected = sorted(k for k, v in tokens.items() if v)
    # A FRESH Agent per invocation — this line is what makes the turn stateless.
    # Do not hoist it to module scope to "save time": a module-level Agent would
    # accumulate conversation across DIFFERENT USERS sharing a warm container,
    # which is a cross-user data leak, not a cache. Building it is cheap; the
    # tool closure also has to re-bind this turn's per-source tokens anyway.
    agent = _build_agent(tokens)
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
        "connected_sources": connected,
    }
    # Consent URLs for sources this user has NOT connected yet, keyed by source.
    # A DICT, not a single `github_auth_url` string, deliberately: with four sources
    # a user may need to authorize several, and a scalar key would have to be a
    # breaking wire-format change later. Absent entirely once everything is vaulted,
    # so a client can treat its presence as "action required from this user".
    pending = {k: u for k, u in auth_urls.items() if not tokens.get(k)}
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
    return result


if __name__ == "__main__":
    app.run()
