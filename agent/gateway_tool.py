"""Tools Gateway client — reaches a source through AgentCore Gateway over MCP.

WHAT THIS CHANGES, AND WHAT IT DELIBERATELY DOES NOT.

The security property is unchanged: the call to the source still runs AS THE USER, and
the SOURCE still does the ACL trimming. What moves is *who holds the source token*.

    IN_PROCESS (github_tool.py, gdrive_tool.py)
        agent.py obtains a per-user GitHub token via AgentCore Identity, hands it to
        `search_github`, and this container holds source credentials in memory.

    GATEWAY (this module)
        agent.py hands the gateway THE USER'S OWN IdP JWT. The gateway authenticates
        that, then fetches the GitHub token itself from its target's credential provider
        and calls GitHub. **The agent never sees a source token at all.**

That is the point of routing a source this way, and it is worth being precise about the
size of the win: the vault already meant this container never saw a *refresh* token or a
client secret. Moving to the gateway removes the short-lived *access* token too, so a
compromise of this container yields no source credential of any kind.

WHERE THIS SITS IN THE ARCHITECTURE DIAGRAM (infra/architecture-components.png):
this is the "Tools AgentCore Gateway" tile — steps 7a/8a/10a/11a. Until
`infra/03-gateways.yaml` was written that tile was dashed and tagged TARGET; the tile is
real now, but note the arrows are still only taken by sources whose SOURCES row says
`via: GATEWAY`. Drive never takes them (see below).

⚠️ WHY GOOGLE DRIVE CAN NEVER ROUTE THROUGH HERE. Not a scheduling gap — a structural
one. The Gateway's `OAuthGrantType` enum is exactly {CLIENT_CREDENTIALS,
AUTHORIZATION_CODE, TOKEN_EXCHANGE}. M2 DELEGATION needs an assertion signed by a
*Google* service-account private key, which is not any of those and which a gateway could
not produce anyway — it does not hold Google's key material. So the tools gateway is
asymmetric by necessity: M1 CONSENT and M3 TOKEN EXCHANGE can live on it, M2 DELEGATION
stays in-process permanently. See `infra/03-gateways.yaml` and Addendum C.

THE CREDENTIAL ARGUMENT IS STILL FIRST, AND STILL PER-USER. Every callable here keeps
the `(credential, ...)` shape the in-process tools use, so `_build_agent`'s tool loop
does not branch on `via` — it passes whatever `tokens[key]` holds and the callable knows
what that is. For a gateway source that value is the user's IdP JWT rather than a source
token. Same uniform loop, different meaning per row, exactly as with DELEGATED_SUBJECT.
"""
import asyncio
import concurrent.futures
import json
import os
import threading

# The MCP client is imported lazily inside _call so that an image built before `mcp` was
# added to requirements.txt still serves the in-process sources instead of crash-looping
# at module scope. Same reasoning as gdrive_tool's guarded import in agent.py.

GATEWAY_URL = os.environ.get("RIPPLE_TOOLS_GATEWAY_URL") or ""

# Per-turn tool-name cache. The gateway PREFIXES each target's tool names (a target
# called `github` exposing `github_search_code` is advertised under a composed name), and
# the exact delimiter is a service detail we deliberately do not hardcode: a wrong guess
# fails as "tool not found" with no hint. We list the gateway's tools once and match by
# suffix instead, so the mapping is discovered rather than assumed.
_TOOL_NAMES: dict[str, str] = {}
_TOOL_NAMES_LOCK = threading.Lock()

# ⚠️ THE GATEWAY ASKS FOR ITS OWN CONSENT, AND IT IS A SECOND CONSENT.
#
# A vaulted token belongs to the workload that vaulted it. The runtime's workload
# identity holds the token `client/consent.py` completed, and the tools gateway's
# AUTHORIZATION_CODE target has a DIFFERENT credential provider under a DIFFERENT
# workload — so consenting to the in-process route grants the gateway route nothing. The
# same user must approve GitHub twice, once per route. That is not a bug to be fixed
# here; it follows from the win this module exists for (this container never sees a
# source token, so it cannot lend the gateway one) and it is the honest cost of flipping
# `via: GATEWAY` for an already-connected user.
#
# The gateway signals it via an MCP ELICITATION rather than an HTTP 401: JSON-RPC error
# code -32042, `data.elicitations[i].url` carrying an AgentCore authorize URL of exactly
# the shape `agent.py` already returns in `auth_required`. This code is load-bearing
# because the shape is not obvious from the error: `McpError.__str__` is only "This
# request requires more information.", so the URL is present but invisible in a log line,
# and re-raising the exception verbatim discards it. That is precisely what happened —
# the route reported `ExceptionGroup searching GitHub via the gateway`, which reads as
# broken plumbing and sent a debugging session after the MCP transport, when the true
# state was "reachable, authenticated, awaiting one click".
#
# Hence ElicitationRequired: a DISTINCT type, not a message prefix. `agent.py` must sort
# this into `auth_required` (user must act) rather than `source_errors` (an operator must
# act) — a distinction that file's comments already insist on — and sorting on substrings
# of an error message is how that distinction gets quietly lost again.
ELICITATION_CODE = -32042


class ElicitationRequired(Exception):
    """The gateway needs this user to authorize its own copy of the source.

    Carries `url` so the caller can surface it as a consent URL. Never raised for
    transport, auth, or schema failures — those stay generic exceptions on purpose, so
    "the user must click something" and "something is misconfigured" cannot be confused.
    """

    def __init__(self, url: str, message: str = ""):
        super().__init__(message or "gateway requires user authorization")
        self.url = url


def _elicitation_url(exc: BaseException) -> str | None:
    """Return the authorize URL if `exc` is a gateway elicitation, else None.

    Defensive at every hop: an McpError whose `data` is not the expected shape must fall
    through as an ordinary error rather than raise a second, more confusing exception
    from inside the handler.
    """
    err = getattr(exc, "error", None)
    if err is None or getattr(err, "code", None) != ELICITATION_CODE:
        return None
    data = getattr(err, "data", None)
    if not isinstance(data, dict):
        return None
    for item in data.get("elicitations") or []:
        if isinstance(item, dict) and item.get("url"):
            return item["url"]
    return None

# One worker thread, one event loop. `_build_agent`'s tools are SYNCHRONOUS callables but
# `invoke()` is async, so there is already a running loop on the calling thread and
# asyncio.run() there would raise. Handing the coroutine to a separate thread is the
# reliable way to bridge that without making every tool async and reshaping the loop.
_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=4,
                                              thread_name_prefix="gw-mcp")


def _run(coro, timeout: int = 45):
    return _POOL.submit(asyncio.run, coro).result(timeout=timeout)


async def _resolve_names(session) -> dict[str, str]:
    """Map bare operationId -> the gateway's advertised (prefixed) tool name."""
    listed = await session.list_tools()
    out: dict[str, str] = {}
    for t in listed.tools:
        name = t.name
        out[name] = name
        # Suffix match on the last delimiter-free segment, so both `github___op` and a
        # plain `op` resolve. Longest-prefix collisions are not possible here because
        # operationIds in our OpenAPI schema are already globally unique.
        for delim in ("___", "__", "_._", ":"):
            if delim in name:
                out[name.rsplit(delim, 1)[-1]] = name
                break
    return out


async def _call_async(user_jwt: str, operation: str, args: dict) -> dict | list:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(
        url=GATEWAY_URL,
        headers={"Authorization": f"Bearer {user_jwt}"},
    ) as (read, write, _get_session_id):
        async with ClientSession(read, write) as session:
            await session.initialize()

            global _TOOL_NAMES
            if operation not in _TOOL_NAMES:
                names = await _resolve_names(session)
                with _TOOL_NAMES_LOCK:
                    _TOOL_NAMES.update(names)
            tool_name = _TOOL_NAMES.get(operation, operation)

            try:
                result = await session.call_tool(tool_name, args)
            except Exception as e:
                url = _elicitation_url(e)
                if url:
                    raise ElicitationRequired(url) from e
                raise
            if getattr(result, "isError", False):
                text = " ".join(getattr(c, "text", "") for c in (result.content or []))
                raise RuntimeError(f"gateway tool {tool_name} failed: {text[:300]}")

            # Prefer the structured payload when the gateway provides one; fall back to
            # parsing the text block, which is what an OpenAPI target returns.
            structured = getattr(result, "structuredContent", None)
            if structured:
                return structured
            for c in (result.content or []):
                text = getattr(c, "text", None)
                if text:
                    try:
                        return json.loads(text)
                    except json.JSONDecodeError:
                        return {"text": text}
            return {}


def _call(user_jwt: str, operation: str, **args) -> dict | list:
    if not GATEWAY_URL:
        raise RuntimeError("RIPPLE_TOOLS_GATEWAY_URL is not set")
    if not user_jwt:
        raise RuntimeError("no caller JWT: cannot authenticate to the tools gateway")
    try:
        return _run(_call_async(user_jwt, operation, args))
    except BaseException as e:
        # ⚠️ RE-RAISE THE UNWRAPPED ElicitationRequired. `streamablehttp_client` is an
        # anyio task-group context manager, so an exception raised inside it leaves as an
        # ExceptionGroup — `except ElicitationRequired` at the call site would NOT match,
        # and the consent URL would be lost exactly as before. Unwrapping here keeps that
        # transport detail from leaking into every caller.
        found = _find_elicitation(e)
        if found is not None:
            raise found from None
        raise


def _find_elicitation(exc: BaseException, depth: int = 0):
    """Find an ElicitationRequired anywhere in an exception's group/cause tree."""
    if depth > 6:  # cycles are possible through __cause__; bound the walk.
        return None
    if isinstance(exc, ElicitationRequired):
        return exc
    for inner in getattr(exc, "exceptions", None) or []:
        found = _find_elicitation(inner, depth + 1)
        if found is not None:
            return found
    for chained in (exc.__cause__, exc.__context__):
        if chained is not None:
            found = _find_elicitation(chained, depth + 1)
            if found is not None:
                return found
    return None


def is_configured() -> bool:
    """Whether gateway routing is available on this runtime.

    Absent RIPPLE_TOOLS_GATEWAY_URL, a `via: GATEWAY` row falls back to in-process so
    the agent still runs locally with no gateway deployed. That fallback is why this is
    a predicate rather than an assertion at import.
    """
    return bool(GATEWAY_URL)


# ---------------------------------------------------------------------------
# GitHub over the gateway.
# ---------------------------------------------------------------------------
# ⚠️ THE RELEVANCE SCOPING IS PRESERVED HERE ON PURPOSE, and this is the one thing in
# this module that is easy to get wrong.
#
# github_tool._search_scope() prefixes every query with `user:`/`org:` qualifiers.
# Without them GitHub's /search/code is GLOBAL and every answer cited strangers'
# repositories — `awesome-flipperzero`, `FlagAI`, `learnxinyminutes-docs` (observed, not
# hypothetical). That is a RELEVANCE property, and no permission check substitutes for
# it: the results were correctly trimmed AND useless.
#
# A gateway target is ONE call per tool — it cannot chain "list my orgs, then search".
# So a naive single "search GitHub" target would silently drop the scoping and regress
# answer quality in a way no auth test would catch. Resolution: the gateway exposes the
# primitives (see GithubTarget's OpenAPI schema in 03-gateways.yaml) and the COMPOSITION
# stays here, mirroring _search_scope's logic over MCP instead of over `requests`.
#
# ⚠️ AND THE WARNING ABOVE CAME TRUE ON THIS ROUTE, which is why an empty scope is now
# an ERROR here rather than a degradation. github_tool caches an empty scope and searches
# unscoped, on the reasoning that "noisy but still answers" beats failing — a defensible
# call there, because /user failing means GitHub itself is unreachable and the search will
# fail next anyway. Here the empty scope had a DIFFERENT cause: the schema simply had no
# `/user` operation, so every search for an org-less account was global and permanently
# so. Degrading silently is what let that ship. A route that cannot scope must say so.
_SCOPE_CACHE: dict[str, str] = {}


def _scope(user_jwt: str) -> str:
    """Same qualifiers as github_tool._search_scope, sourced over MCP.

    Cached per JWT, not per user id: two users in one warm container must never share a
    scope. Same isolation rule as the in-process cache it mirrors.
    """
    if user_jwt in _SCOPE_CACHE:
        return _SCOPE_CACHE[user_jwt]
    quals: list[str] = []
    # ⚠️ `user:` FIRST, and it is the half that was missing. _search_scope asks GET /user
    # for the login before listing orgs, and dropping that here produced an EMPTY scope
    # for any account in no orgs — i.e. a global search over public GitHub. See the
    # `/user` note in 03-gateways.yaml GithubTarget: the schema had no such operation, so
    # the qualifier its own summary demands was unobtainable. Ordered the same way as the
    # in-process version so the two routes build byte-identical scopes.
    try:
        me = _call(user_jwt, "github_get_user")
        if isinstance(me, dict) and me.get("login"):
            quals.append(f"user:{me['login']}")
    except ElicitationRequired:
        raise
    except Exception:
        pass
    try:
        orgs = _call(user_jwt, "github_list_orgs")
        if isinstance(orgs, list):
            for org in orgs:
                if isinstance(org, dict) and org.get("login"):
                    quals.append(f"org:{org['login']}")
    except ElicitationRequired:
        # NOT swallowed like other failures. This is the FIRST gateway call of the turn,
        # so it is where an unconsented user is discovered — and degrading to "unscoped
        # search" here would guarantee the search call raises the same elicitation two
        # lines later, having thrown away the URL and cached an empty scope for the rest
        # of the turn. Propagate so search_github_via_gateway reports one consent URL.
        raise
    except Exception:
        # An empty scope degrades to unscoped search — noisy, but it still answers.
        # Cached anyway so one failure does not retry on every search in the turn.
        pass
    scope = " ".join(quals)
    if not scope:
        # NOT cached: unlike the in-process tool, an empty scope here is a failure to be
        # retried, not a state to settle into. Raising keeps the ONE property this whole
        # module's ⚠️ is about — that moving a source onto the gateway must not quietly
        # cost relevance — enforced rather than merely documented.
        raise RuntimeError(
            "refusing to search GitHub unscoped: could not determine this user's login "
            "or orgs over the gateway. An unscoped /search/code is global public GitHub, "
            "which returns strangers' repositories (see this module's scoping note)."
        )
    _SCOPE_CACHE[user_jwt] = scope
    return scope


def search_github_via_gateway(user_jwt: str, query: str, top: int = 5) -> list[dict]:
    """Search GitHub code through the tools gateway, as the calling user.

    Returns the same hit shape as github_tool.search_github, so nothing downstream —
    provenance stamping, `ref` namespacing, the citation format — needs to know which
    route produced a hit.
    """
    if not user_jwt:
        return [{"error": "no caller identity; cannot search GitHub via the gateway"}]

    # Both calls below can raise ElicitationRequired, and it must reach the caller as a
    # CONSENT URL rather than an error string — see ELICITATION_CODE above. Letting it
    # propagate out of this function is deliberate: the hit-list return shape has no field
    # for "the user must authorize", and inventing one (`[{"consent_url": ...}]`) would put
    # a URL where agent.py's provenance stamping expects a document.
    try:
        scope = _scope(user_jwt)
        scoped = f"{query} {scope}".strip() if scope else query
        res = _call(user_jwt, "github_search_code", q=scoped, per_page=top)
    except ElicitationRequired:
        raise
    except Exception as e:
        # `type(e).__name__` alone was too little to debug on: an ExceptionGroup wrapping
        # an elicitation printed as "ExceptionGroup searching GitHub via the gateway",
        # which named the wrapper and hid the cause. Include the message.
        return [{"error": f"{type(e).__name__} searching GitHub via the gateway: {e}"}]

    items = (res or {}).get("items", []) if isinstance(res, dict) else []
    out: list[dict] = []
    for it in items:
        repo = (it.get("repository") or {}).get("full_name", "")
        path = it.get("path", "")
        out.append({
            "source": "github-code",
            "title": it.get("name", "file"),
            "url": it.get("html_url", ""),
            "repo": repo,
            "path": path,
            # ⚠️ NO text-match FRAGMENTS ON THIS ROUTE, and that is a real regression to
            # be honest about rather than paper over. The in-process tool sends
            # `Accept: application/vnd.github.text-match+json` and gets the matching
            # snippet; a gateway OpenAPI target does not let us vary the Accept header
            # per call, so hits come back as metadata only. Under STRICT GROUNDING a
            # model with a locator and no text has nothing to quote from the hit itself.
            #
            # MEASURED, HOWEVER, AT HIGH CONFIDENCE ON THE DEPLOYED ROUTE — worth stating
            # because the pessimistic reading above is what this comment used to assert
            # flatly. Routing is PER OPERATION (agent.py `_credential_for`): only `search`
            # moves here, and `read_company_document` stays in-process, so the model
            # recovers the missing text by reading a promising hit in full. The snippet
            # loss costs a round trip, not the answer. It DOES degrade to LOW for a user
            # who consented to the gateway but not in-process, because then there is no
            # credential for `fetch` and the locator is all there is.
            #
            # GitHub still defaults to IN_PROCESS: this route needs a SECOND consent from
            # every user (see ELICITATION_CODE) and costs that round trip, for a benefit —
            # the container never holds a source token for search — that is real but does
            # not pay for itself while `fetch` still needs one. Documented in
            # CODE-WALKTHROUGH.md rather than left
            # for someone to rediscover as "the gateway broke search".
            "snippet": f"{repo}/{path}",
            "ref": f"{repo}:{path}",
        })
    return out[:top]
