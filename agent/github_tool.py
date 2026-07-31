"""GitHub retrieval tool — searches under the USER's own OAuth token.

The per-user GitHub token is obtained by AgentCore Identity (see agent.py's
@requires_access_token with USER_FEDERATION / 3-legged OAuth consent). Because the
token belongs to the signed-in user, GitHub returns ONLY repos/code that user can
access — this is the native permission trim, enforced by GitHub, not by us.

WHERE THIS SITS IN THE ARCHITECTURE DIAGRAM (infra/architecture-components.png):

This module is the far end of the CONSENT FLOW — steps 9b and 10b, against the `GH`
(GitHub MCP / API) tile, both tagged EXTENSION because this path works and is
deliberately not the one being demonstrated. It has no tile of its own; it is a module
inside the `RT` container, and the diagram carries its behaviour on those two arrows.
9b is every requests.get() below, made with the user's OWN OAuth token — this file
never sees how that token was obtained and never refreshes one, which is the whole
value of the vault sitting between it and GitHub. 10b is the trim, and as with the
Drive source there is no code here that performs it: GitHub decides, and `fetch_document`
reports a 404 as "not found or not permitted" precisely because GitHub conflates the two
and inventing a distinction would be a lie about who authorized what.

Nothing in this file performs 7b or 8b, and it cannot see the ★ either — by the time
`search_github` is called the consent has already happened, possibly several turns ago.
What it does have is the case the diagram gives no step to: `github_token` empty, which
is what a first-time user's turn actually looks like. Every entry point below returns an
`{"error": "GitHub not connected. Consent required."}` shape rather than raising, so the
turn still answers from whatever other source is live. On the diagram that is a green
1a->14a walk with an orange gap in it, which no single flow's numbering can express.

`_search_scope()` corresponds to no arrow at all, and that absence is worth naming here
because the diagram invites the wrong inference. 10b's sentence says GitHub returns "only
what this user's own grants allow", which is true and is a SECURITY property; it is not a
relevance property. /search/code with no qualifier searches all public code on GitHub,
correctly trimmed and completely useless — see that function's own comment. A reader who
took 10b as the full account of what comes back would omit the scoping and get an agent
that cites strangers' repositories while every check the diagram makes still holds.
"""
import requests

API = "https://api.github.com"
_HEADERS_BASE = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


def _headers(token: str) -> dict:
    return {**_HEADERS_BASE, "Authorization": f"Bearer {token}"}


# Per-token cache of the search scope. Keyed by the token so two users in one warm
# container can never read each other's scope — the same isolation rule as the
# agent's per-invocation Agent. Small and bounded: one entry per active user.
_SCOPE_CACHE: dict[str, str] = {}


def _search_scope(token: str) -> str:
    """Build the `user:`/`org:` qualifiers that confine a search to THIS user's world.

    WHY THIS IS NOT OPTIONAL. GitHub's /search/code is GLOBAL: with no qualifier it
    searches all public code on GitHub, and the user's own repositories are a rounding
    error against that. The observed symptom was every answer citing `awesome-flipperzero`,
    `FlagAI`, `learnxinyminutes-docs` and friends — real hits for the keywords, and
    completely useless as company knowledge.

    Note this is a RELEVANCE fix, not a security one. The permission trim was already
    correct: the token belongs to the user, so GitHub never returns private content they
    cannot read. Scoping stops PUBLIC noise from crowding out the content that matters.
    Both properties are needed — one without the other looks broken.
    """
    if token in _SCOPE_CACHE:
        return _SCOPE_CACHE[token]

    quals: list[str] = []
    try:
        r = requests.get(f"{API}/user", headers=_headers(token), timeout=15)
        if r.status_code == 200:
            login = (r.json() or {}).get("login")
            if login:
                quals.append(f"user:{login}")
    except requests.RequestException:
        pass

    # Org repos are the point of a company assistant — the onboarding guide lives in
    # an org repo far more often than in a personal one. Needs read:org, which the
    # provider already requests.
    try:
        r = requests.get(f"{API}/user/orgs", headers=_headers(token), timeout=15)
        if r.status_code == 200:
            for org in r.json() or []:
                if org.get("login"):
                    quals.append(f"org:{org['login']}")
    except requests.RequestException:
        pass

    scope = " ".join(quals)
    # Cache even an empty scope: if /user failed we do not want to retry it on every
    # search within the turn. An empty scope degrades to unscoped search, which is
    # what the caller had before — noisy, but not broken.
    _SCOPE_CACHE[token] = scope
    return scope


def search_github(github_token: str, query: str, top: int = 5) -> list[dict]:
    """Search code + issues/PRs the user can access for `query`.

    Returns a list of {source, title, url, snippet, repo} for items THIS user is
    permitted to see. Blends code hits (docs/README/source) and issue/PR hits.
    """
    if not github_token:
        return [{"error": "GitHub not connected. Consent required."}]

    out: list[dict] = []
    headers = _headers(github_token)

    # Confine both searches to the user's own + org repos. Without this, /search/code
    # and /search/issues span all of public GitHub and real hits are buried.
    scope = _search_scope(github_token)
    scoped_query = f"{query} {scope}".strip() if scope else query

    # 1) Code search (finds markdown docs, READMEs, source files).
    #
    # The text-match Accept header is what makes these results ANSWERABLE. Without it
    # GitHub returns metadata only — name, path, url — and a strictly-grounded model
    # can see that a file called TESTING_GUIDE.md exists while having no text to quote,
    # so it correctly refuses and every answer lands at LOW confidence. That looked
    # like a retrieval bug and was really an empty `snippet`.
    try:
        r = requests.get(
            f"{API}/search/code",
            headers={**headers, "Accept": "application/vnd.github.text-match+json"},
            params={"q": scoped_query, "per_page": top},
            timeout=20,
        )
        if r.status_code == 200:
            for it in r.json().get("items", []):
                repo = (it.get("repository") or {}).get("full_name", "")
                path = it.get("path", "")
                frags = [
                    m.get("fragment", "").strip()
                    for m in (it.get("text_matches") or [])
                    if m.get("fragment")
                ]
                out.append(
                    {
                        "source": "github-code",
                        "title": it.get("name", "file"),
                        "url": it.get("html_url", ""),
                        "repo": repo,
                        "path": path,
                        # The matching fragments, not the path. Falls back to the
                        # locator only when GitHub returned no fragment at all.
                        "snippet": ("\n...\n".join(frags))[:1200] or f"{repo}/{path}",
                        # Opaque handle the model passes back to read_company_document
                        # to get the whole file. Kept a single string on purpose: a
                        # multi-field reference invites the model to assemble one.
                        "ref": f"{repo}:{path}",
                    }
                )
    except requests.RequestException:
        pass

    # 2) Issues + PRs (design discussions, decisions, tickets).
    try:
        r = requests.get(
            f"{API}/search/issues",
            headers=headers,
            params={"q": scoped_query, "per_page": top},
            timeout=20,
        )
        if r.status_code == 200:
            for it in r.json().get("items", []):
                out.append(
                    {
                        "source": "github-issue",
                        "title": it.get("title", ""),
                        "url": it.get("html_url", ""),
                        "repo": (it.get("repository_url", "").split("/repos/")[-1]),
                        "snippet": (it.get("body") or "")[:200],
                    }
                )
    except requests.RequestException:
        pass

    return out[:top]


def list_sources(github_token: str, top: int = 100) -> list[dict]:
    """Inventory the repositories this user can see, newest activity first.

    WHY A SEPARATE TOOL. Search answers "which document says X"; it cannot answer
    "what do I have access to", because /search/code matches CONTENT and a repo with
    no matching text is invisible to it. Asked to list their own repositories, the
    agent had nothing to call and correctly said so — a true answer to a question the
    user reasonably expected to work. Inventory is metadata, so it needs a metadata
    endpoint.

    Also the natural first question against a new deployment, and the cheapest way for
    someone to see that permission trimming is real: the list is exactly this user's.
    """
    if not github_token:
        return [{"error": "GitHub not connected. Consent required."}]

    out: list[dict] = []
    try:
        # affiliation covers own + org + collaborator repos; GitHub applies the trim.
        r = requests.get(
            f"{API}/user/repos",
            headers=_headers(github_token),
            params={
                "per_page": min(top, 100),
                "sort": "updated",
                "affiliation": "owner,collaborator,organization_member",
            },
            timeout=20,
        )
        if r.status_code != 200:
            return [{"error": f"HTTP {r.status_code} listing repositories"}]
        me = ""
        try:
            u = requests.get(f"{API}/user", headers=_headers(github_token), timeout=15)
            if u.status_code == 200:
                me = ((u.json() or {}).get("login") or "").lower()
        except requests.RequestException:
            pass
        for repo in r.json() or []:
            owner = ((repo.get("owner") or {}).get("login") or "")
            out.append(
                {
                    "source": "github-repo",
                    "title": repo.get("full_name", ""),
                    "url": repo.get("html_url", ""),
                    "repo": repo.get("full_name", ""),
                    "private": bool(repo.get("private")),
                    # "created by me" is not a field GitHub exposes; ownership is the
                    # closest true answer, so it is labelled as ownership, not
                    # authorship, and the model is told the difference.
                    "owned_by_user": owner.lower() == me and bool(me),
                    "owner": owner,
                    "description": (repo.get("description") or "")[:200],
                    "updated_at": repo.get("updated_at", ""),
                }
            )
    except requests.RequestException as e:
        return [{"error": f"{type(e).__name__} listing repositories"}]
    return out


def fetch_document(github_token: str, ref: str, max_chars: int = 20000) -> dict:
    """Fetch one document's full text by the `ref` handle a search hit carried.

    `ref` is "<owner>/<repo>:<path>" — the exact string search_github emitted, which
    keeps the model out of the business of assembling API paths.

    STILL PERMISSION-TRIMMED. This is the user's own token on the ordinary contents
    endpoint, so GitHub 404s anything they cannot read. Fetching by handle does not
    widen access beyond what search already returned to this user.
    """
    if not github_token:
        return {"error": "GitHub not connected. Consent required."}
    repo_full_name, _, path = ref.partition(":")
    if not repo_full_name or not path:
        return {"error": f"malformed ref {ref!r}; expected 'owner/repo:path'"}

    headers = {**_headers(github_token), "Accept": "application/vnd.github.raw+json"}
    try:
        r = requests.get(
            f"{API}/repos/{repo_full_name}/contents/{path}",
            headers=headers,
            timeout=30,
        )
    except requests.RequestException as e:
        return {"error": f"{type(e).__name__} fetching {ref}"}

    if r.status_code == 404:
        # Genuinely indistinguishable from "no permission" — GitHub deliberately
        # conflates the two, and reporting it as missing is the safe reading.
        return {"error": f"not found or not permitted: {ref}"}
    if r.status_code != 200:
        return {"error": f"HTTP {r.status_code} fetching {ref}"}

    text = r.text
    # Truncation is REPORTED, not silent: a model told the text is complete will
    # answer "the guide does not mention X" from a fragment.
    return {
        "source": "github-code",
        "title": path.rsplit("/", 1)[-1],
        "url": f"https://github.com/{repo_full_name}/blob/HEAD/{path}",
        "repo": repo_full_name,
        "path": path,
        "text": text[:max_chars],
        "truncated": len(text) > max_chars,
    }
