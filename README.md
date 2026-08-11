# Ripple Knowledge Assistant — Solution

A working AgentCore agent that answers questions from the signed-in user's company
knowledge sources, with inline citations and a confidence band. Permission trimming
is enforced by each source itself, because the agent calls it with **that user's**
OAuth token — never a service account.

**GitHub is source #1, not the only one.** The target state adds Databricks Genie
(MCP), Confluence, Slack, and Google Drive. Adding a source is a row in the
`SOURCES` table in `agent/agent.py` plus its credential provider — the entrypoint,
the search tool, the response shape, and this client all iterate over that table.
See the comment above `SOURCES` for the four-step recipe.

This directory is **self-contained**: everything needed to deploy, run, and test
the implementation is here — CloudFormation templates, agent code, client, tests,
and docs. No other repository or checkout is required.

Two mechanisms reach that guarantee, and the solution implements both:

```
IdP device-code login  ->  user JWT
   -> AgentCore Runtime (CUSTOM_JWT authorizer validates it at the front door,
      and the agent re-verifies the signature itself before naming a user)
      -> Strands agent (Claude on Bedrock) + three tools that each fan out across
         every source available to this user: search (content), read (one
         document's full text), list (what they can access)
         |
         |-- DELEGATION FLOW, no consent screen ever (Google Drive):
         |     the verified `email` claim becomes the subject of a service-account
         |     assertion, and Google returns a token scoped to THAT ONE USER
         |
         `-- CONSENT FLOW, one click per user per source (GitHub):
               AgentCore Identity brokers the user's OWN OAuth token and vaults it
         |
         -> answer + [n] citations + HIGH/MEDIUM/LOW
            (+ a consent URL for any vaulted source not yet connected)
```

Either way the *source* does the trimming. See
[*The two flows, and what the user actually sees*](#the-two-flows-and-what-the-user-actually-sees).

## What's here

| Path | What it is |
|---|---|
| `agent/` | The agent that runs in the container: `agent.py` (entrypoint + the `SOURCES` table, the extension point, + the three tools), `github_tool.py` (`search_github`, `fetch_document`, `list_sources` — the consent flow), `gdrive_tool.py` (the same three over Drive — the delegation flow), `identity_claims.py` (re-verifies the JWT signature and gates which email may be impersonated; the delegation flow's whole safety argument), `gateway_tool.py` (calls a source through the tools Gateway instead of in-process, for a source set to `<KEY>_VIA=GATEWAY`) |
| `client/` | CLI test client: `login.py` (Auth0 device flow; `--copy` puts the JWT on the clipboard for the Harness playground), `ask.py` (invoke the runtime), `consent.py` (completes 3-legged OAuth — owns the return URL both the runtime and the allowlist derive from) |
| `infra/` | CloudFormation templates, architecture docs, diagrams + their renderers. `01-foundation` → `02-runtime` are the deploy; `03-gateways` and `04-harness` are opt-in and **additive** — `04` is a second, parallel managed agent, not a replacement for `02` |
| `scripts/` | Deploy path: `deploy.py` (orchestrator) → `cfn_build.py` (image) → `apply_tags.py`, plus `register_consent_url.py` (allowlists the consent return URL — CFN can't) and `verify_drive_delegation.py` (proves the Google admin grant works with no Ripple code in the path). `create_github_provider.py` is a standalone leftover — CloudFormation creates the provider now, so the Quickstart never calls it |
| `tests/` | `test_identity_claims.py` — the regression tests on the impersonation gates; `test_caller_identity.py` — that identity comes from the authenticated header and never from the body; `test_gateway_elicitation.py` — that a tools-Gateway consent prompt survives the anyio `ExceptionGroup` with its URL intact and is sorted as "the user must act", not "an operator must act"; `test_harness_prompt.py` — that the system prompt duplicated into `04-harness.yaml` still carries every grounding rule, and that the harness keeps no shell and no shared memory partition. Run each directly (`python3 tests/test_identity_claims.py`); no AWS or network needed, and no pytest |
| `docs/` | Setup guides: `SETUP.md` (Auth0 front-door identity), `DATA-SOURCES.md` (onboarding every source — GitHub, Google Drive, and the three target-state MCP sources), `GOOGLE-DRIVE-OKTA-SETUP.md` (the Drive deep dive), `HARNESS-ASSESSMENT.md` (why the managed harness can't be the primary agent yet — measured), `FUTURE-MULTI-AGENT.md` (roadmap) |
| `Dockerfile` | The container image. **Committed source**, not generated — `cfn_build.py` needs it |
| `requirements.txt` | Container dependencies only |
| `requirements-dev.txt` | Your machine: deploy + client + diagram rendering |
| `dev.env.example` | Config template. Copy to `dev.env` (gitignored) and fill in |

Not in git, by design: `dev.env` (your real config), `client/.token.json` (cached
JWT), `.venv*`, and `.bedrock_agentcore.yaml` (leftover state from the removed
starter toolkit).

## The two flows, and what the user actually sees

Both flows deliver the same guarantee — the source itself trims results to what the
signed-in user may read — but they reach it by different mechanisms, and they feel
completely different to use. That difference is the point of having both, so it is
worth being precise about it before setup.

`infra/architecture-components-v3.png` draws them side by side: the **delegation flow** in
green (steps `1a`–`14a`), the **consent flow** in orange (`1b`–`14b`, plus the
`★` arrow). Steps `1`–`5` are identical in both; they diverge at step `6`.

Each flow also has a sequence diagram to itself, which is where the shape of the
difference is easiest to see: `infra/architecture-delegation-flow.png` (17 edges, one
invocation, no consent screen on the canvas) and `infra/architecture-consent-flow.png`
(26 edges, two invocations, because the human step in the middle is one the runtime
will not hold a request open for). Those two diagrams number their own flow from `1`
independently of the components diagram and of each other.

| | Delegation flow (green, `…a`) | Consent flow (orange, `…b`) |
|---|---|---|
| Source in this repo | Google Drive / Docs | GitHub |
| What proves the identity | your IdP's signed JWT, re-verified inside the agent, `email` claim taken from it | the same JWT, exchanged for a token the target will accept |
| How the source is called | a service-account assertion with `sub` = that verified email — Google mints a token scoped to **that one user** | the user's **own** OAuth token, held in the AgentCore Identity vault |
| Consent screens | **none, ever** | one per user, per source, once |
| Human actions | **zero** | one — the `★` step, the only human action anywhere on the diagram |
| Who sets it up | a Workspace super-admin, once, for everyone | each user, for themselves |
| Fails when | the token has no verified `email`, or the address is not the Workspace **primary** address | the user declines, or the vault never received the `session_id` |

### End-user experience — delegation flow (Google Drive)

```
$ python3 client/login.py
  → browser opens once for the IdP device-code login
$ python3 client/ask.py "what does the onboarding guide say about VPN access?"

Asking Ripple: what does the onboarding guide say about VPN access?
------------------------------------------------------------
The onboarding guide says VPN access is requested through … [1]
------------------------------------------------------------
Confidence: HIGH   Connected sources: ['gdrive']
```

That is the whole interaction. No consent page appears, nothing is vaulted, and the
second user gets a *different* answer from the same question because Drive trimmed
their result set — not because Ripple filtered anything. Run the same command as
each of the two test users and the difference in cited documents is the demo.

The user is never asked to authorize Drive because there is nothing for them to
authorize: the Workspace admin granted domain-wide delegation once, and each request
narrows that grant to a single impersonated subject taken from the verified token.
A user who has never signed in to Drive has no Drive, which surfaces as an empty
result rather than an error — see the troubleshooting table.

### End-user experience — consent flow (GitHub)

First question, per user, per source:

```
$ python3 client/ask.py "is there a testing guide for the parser?"
...
------------------------------------------------------------
1 source(s) not connected for this user — they were not searched.

=== connecting 'github'
  → browser opens on GitHub's "Authorize <your app>" screen   ← ★ the human step
  → you click Approve
  → the browser is redirected back to this client, which vaults the token
------------------------------------------------------------
Re-asking now that the new source(s) are connected...

Yes — `docs/testing.md` describes … [1]
------------------------------------------------------------
Confidence: HIGH   Connected sources: ['github']
```

`ask.py` drives that to completion and then re-asks the original question by itself,
because the user typed a question, not a request to authorize. Every later run is
silent:

```
$ python3 client/ask.py "how do I set up the parser locally?"
Run `pip install -e .` and … [1]
------------------------------------------------------------
Confidence: HIGH   Connected sources: ['github']
```

So it is genuinely one click per user per source — but it *is* a click, by a person,
and the delegation flow has no equivalent at any step.

### With both sources enabled

Both are searched on every question and results are merged, each citation carrying
its source. A user who has approved GitHub but whose Drive is empty gets a grounded
answer from GitHub alone and exit code `0` — a partial answer from the connected
sources is a real answer. Exit `3` means *nothing* was searched.

## Okta Cross-App Access (XAA) vs. AgentCore token exchange (M3)

A recurring question is whether Okta **Cross-App Access (XAA / ID-JAG)** replaces the
per-source consent (M1) with a single "auth once, reach everything" flow, and if so why
this solution routes token exchange (**M3**) through **AgentCore Identity** rather than
driving XAA directly. The short answer: the two are not interchangeable, and M3 today is
a *deliberately unused* path because the exchange it needs is not possible as of now on
either side. The essentials:

**XAA is a two-leg protocol; AgentCore performs one hop.** Verified against the IETF WG
draft `draft-ietf-oauth-identity-assertion-authz-grant-04` (not yet an RFC; the
`…:token-type:id-jag` URN is not yet IANA-registered):

| | Leg 1 — at the **IdP** (Okta) | Leg 2 — at the **RP's own AS** |
|---|---|---|
| `grant_type` | `token-exchange` (RFC 8693) | `jwt-bearer` (RFC 7523) |
| sends | `subject_token` = user's id_token | `assertion` = **the ID-JAG** |
| `requested_token_type` | **`…:token-type:id-jag`** ← the crux | — |
| returns | the ID-JAG | the RP's access token |

The ID-JAG is the **output of leg 1** and the **input to leg 2**. AgentCore Identity's
`onBehalfOfTokenExchangeConfig` offers two `grantType` values (`TOKEN_EXCHANGE`,
`JWT_AUTHORIZATION_GRANT`) — but they are two dialects for **one** hop, selected per
credential provider, not legs 1 and 2.

**M3 is not available today, and it is blocked by a THREE-link chain — every link must
hold for a given source, and today none of the three does for any source we care about.**
These are separate gaps on separate parties; closing one or two is not enough:

| # | Link | Whose | Today |
|---|---|---|---|
| 1 | **Ripple's Okta issues an ID-JAG** — XAA must be GA, enabled on **Ripple's** Okta tenant, and the target registered as a relying party | Ripple's Okta | Unconfirmed. XAA is early and the public Okta XAA docs were not reachable during evaluation. (The tenant in `dev.env` today is an Auth0 dev tenant, which cannot mint an ID-JAG at all.) |
| 2 | **AgentCore Identity requests the ID-JAG** | AWS (AgentCore) | **Not possible as of now.** No `requested_token_type` field anywhere in the API (not on the provider config, not on `GetResourceOauth2Token`), so it cannot ask for an ID-JAG, and `customParameters` is documented as unable to override standard OAuth parameters. Confirmed on the wire — injecting `requested_token_type` against an endpoint that validates token types before client auth changed nothing. |
| 3 | **The target MCP server accepts the ID-JAG** — each source's own authorization server must support the XAA / ID-JAG relying-party side | each source vendor | Per source: **Slack — no** (its AS speaks `authorization_code` only); **Confluence / Atlassian — unconfirmed** (its endpoint routes token-exchange grants but rejects standard subject-token types today); **Databricks Genie — untested** (no environment yet). |

*(Underlying all three: ID-JAG is still an IETF **draft**, not an RFC, and the
`…:token-type:id-jag` URN is not yet IANA-registered.)*

The links are **independent.** If AWS shipped an ID-JAG-aware grant tomorrow (link 2),
M3 still would not work until Ripple's Okta issues the ID-JAG (link 1) **and** the
specific target accepts it (link 3). Link 3 is per-source: **Databricks Genie, Slack MCP
and Confluence MCP each have to support XAA on their own authorization server** — Ripple's
Okta and AgentCore being ready does nothing for a source whose vendor does not accept an
ID-JAG. The three links share only the missing artifact — the ID-JAG — which link 1 must
**issue**, link 2 must **request**, and link 3 must **accept**.

**Why we still route M3 through AgentCore Identity rather than hand-rolling XAA.** The
alternative — implementing leg 1 ourselves — is only ~20 lines of `POST`, but it is the
wrong ~20 lines: it means this solution holds the Okta client credential and owns ID-JAG
signature / `typ` / `aud` validation forever. That is a standing security liability, so a
hand-rolled exchange is treated as a defect, not an optimisation. Delegating the exchange
to AgentCore Identity keeps that credential and that validation out of our code; the cost
is that we can only do what its API exposes — hence M3 is drawn but unused until AgentCore
adds `requested_token_type` (link 2) and Ripple's Okta tenant has XAA enabled with the
target registered as a relying party (link 1).

**What this means per connector, today.** XAA only pays off where the target's own
authorization server supports the ID-JAG relying-party side (link 3) — realistically an
Okta-guarded MCP server, or Databricks Genie if fronted the same way (untested). For the
named third-party connectors it does **not** help regardless of Ripple's Okta roadmap:
Slack advertises `authorization_code` only, and Google's `jwt-bearer` requires the
assertion be signed with a **Google** service-account key (which is why Drive is M2
delegation, not M3). Those are vendor rules about what their own authorization servers
accept, so **M1 consent is the plan of record — not a stopgap — for Slack and Confluence.**

## Prerequisites

Things this code cannot create for you. Each needs a human with an account
somewhere, so do these before touching the CLI:

1. **AWS account** + credentials that can create CloudFormation, IAM, ECR,
   CodeBuild, Secrets Manager and AgentCore resources.
2. **Bedrock model access** enabled in your region for the model you put in
   `BEDROCK_MODEL_ID`. On a fresh account this is off, and the failure surfaces
   late — the deploy succeeds and the *first question* fails with
   `AccessDeniedException` from the container. Enable it in the Bedrock console
   under *Model access* (for a cross-region inference profile like
   `us.anthropic.claude-opus-4-8`, access is needed in every region the profile
   routes to).
3. **Auth0 tenant** — an API (the audience) plus a **native** app with the
   *Device Code* grant enabled and *Allow Offline Access* on, plus at least one
   test user in the database connection. Two users make the permission-trim demo
   visible. Walkthrough: [`docs/SETUP.md`](docs/SETUP.md) **Part 1 only** — Part 2
   of that file describes an Entra ID / SharePoint data source that was replaced
   by GitHub and is not implemented. Ignore it.
4. **GitHub OAuth App** that *you* own — that is what makes consent per-user with
   no org admin involved. Also seed a couple of repos so different users see
   different content, or every answer is a correct but unconvincing "nothing
   found". Walkthrough: [`docs/DATA-SOURCES.md`](docs/DATA-SOURCES.md) § *GitHub*.
   *Optional second source:* Google Drive / Docs reached with **no consent screen at
   all**, via service-account impersonation driven by the validated IdP `email` claim.
   Needs **a domain you own**, Google Workspace on it, and **super-admin** — there is
   no way around any of the three, because domain-wide delegation is a Workspace
   feature and Workspace requires a DNS-verified domain. Quickstart step 10 is the
   plumbing summary; the walkthrough (with a standalone verification script that
   proves the grant before any agent code depends on it) is
   [`docs/GOOGLE-DRIVE-OKTA-SETUP.md`](docs/GOOGLE-DRIVE-OKTA-SETUP.md).
5. **Python 3.12+**. Docker is *not* required — the image is built by CodeBuild,
   in AWS, on ARM64.
6. **A browser on this machine.** `client/login.py` runs the Auth0 device flow and
   opens a URL. Over a bare SSH session you must copy the URL out by hand.

## Quickstart

Empty AWS account to first answered question. Every step except 9 is one-time; step 9
is the loop you actually use. Steps 1–9 give you the **consent flow** over GitHub;
step 10 adds Google Drive and with it the **delegation flow**, and is optional but is the
only way to demonstrate the no-consent path. Step 11 deploys the two Gateways, which are
additive and off by default — nothing above needs them.

### 1. Install

```bash
cd solution
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
```

`requirements.txt` is the *container's* dependencies — do not install it here.

### 2. Point at an AWS account

```bash
aws configure                 # or export AWS_PROFILE / AWS_ACCESS_KEY_ID ...
aws sts get-caller-identity   # must succeed before anything else will
```

### 3. Fill in config (first pass)

```bash
cp dev.env.example dev.env    # dev.env is gitignored
```

Fill everything **except** three values, which are outputs of step 7 — so this file
gets filled in two passes:

| Leave as placeholder for now | Why |
|---|---|
| `GITHUB_CLIENT_SECRET_ARN` | created in step 4 |
| `RIPPLE_RUNTIME_ARN`, `RIPPLE_RUNTIME_QUALIFIER`, `AGENTCORE_MEMORY_ID` | printed by step 7 |

No secret goes in this file, and none is needed in your shell either: the deploy
path reads only the Secrets Manager **ARN**. (`GITHUB_CLIENT_SECRET` is read by
exactly one script, `scripts/create_github_provider.py`, which the Quickstart does
not use — CloudFormation creates the credential provider now.)

```bash
source dev.env
```

### 4. Put the GitHub client secret in Secrets Manager

Create the secret, then put its ARN in `dev.env` and re-`source`:

```bash
# see infra/README.md for the exact piped commands
export GITHUB_CLIENT_SECRET_ARN=arn:aws:secretsmanager:...
```

CloudFormation gets an ARN, never the value, so the secret stays out of template
parameters and stack history. Exact commands — the value is piped end to end and
never becomes an argv element or a file:
[`infra/README.md` § *Creating `GithubClientSecretArn`*](infra/README.md).

If you skip this, the stack falls back to `ClientSecretSource=MANAGED` and needs
the secret passed some other way. `deploy.py` warns when the ARN is unset.

### 5. Check the plan

```bash
python3 scripts/deploy.py --dry-run
```

Prints every stack, every parameter, and the preflight result — creating nothing. A
missing config value exits `2` here rather than halfway through a deploy.

### 6. Clear any name collisions

That dry run ends in a **preflight** that looks for resource names which already
exist *outside* a CloudFormation stack. Do not wave this through: CloudFormation
cannot adopt a pre-existing resource — it fails `... already exists` and rolls back
**after** having created some of the others.

Under `--dry-run` preflight only *reports* collisions and keeps going, so read its
output. On a real run it exits `2` before creating anything.

Two ways out, both documented in
[`infra/README.md` § *Pre-deploy name collisions*](infra/README.md):

- **Deploy alongside** — `export AGENTCORE_AGENT_NAME=ripple2`. Non-destructive.
- **Delete the old resources** — clean cutover, takes the existing runtime down.
  Migrate the OAuth provider's managed secret out *first* if you don't want to
  regenerate the GitHub client secret.

Two gotchas worth knowing before you delete anything:

- **Memory names collide case-insensitively.** `CreateMemory` rejected
  `rippleKnowledgeMemory` against an existing `RippleKnowledgeMemory` — a rename
  that differs only in case is not a fix, and this one cost a rollback.
- **A runtime's DEFAULT endpoint cannot be deleted on its own**
  (`ConflictException`). Delete the runtime; the endpoint goes with it.

`--skip-preflight` exists, but skipping it is how you get a mid-deploy rollback.

### 7. Deploy

```bash
python3 scripts/deploy.py     # preflight -> foundation -> build image -> runtime -> tags
```

Expect several minutes — the CodeBuild ARM64 image build dominates.

On success it prints:
- Three `export` lines (`RIPPLE_RUNTIME_ARN`, `RIPPLE_RUNTIME_QUALIFIER`,
  `AGENTCORE_MEMORY_ID`) — **paste them into `dev.env`**, replacing the
  placeholders from step 3.
- The GitHub **callback URL** (step 8).

The last stage tags the two resources the templates can't declare (the runtime log
group, whose name embeds the generated runtime id, and the Identity-managed secret
under MANAGED). If tagging fails the **deploy still succeeded** — re-run
`python3 scripts/apply_tags.py` rather than redeploying.

### 8. Paste the callback URL into GitHub

Put that URL in the OAuth App's *Authorization callback URL* field. GitHub auth
fails until you do. The provider's `Name` is `createOnly`, so if the provider is
ever replaced the URL changes and this step repeats — see
[`infra/README.md` § *Replacing the credential provider*](infra/README.md).

### 9. Log in and ask

```bash
source dev.env                # now with the runtime ARN, qualifier and memory id
python3 scripts/register_consent_url.py   # once per runtime; see note below
python3 client/login.py       # Auth0 device flow; caches the JWT in client/.token.json
python3 client/ask.py "what does the onboarding guide say about VPN access?"
```

The first `ask.py` run **per user, per source** opens a consent page for each source
that user has not connected yet, waits for the approval, vaults the token, and then
re-asks your question automatically. Later runs reuse the vaulted token silently, so
this is genuinely one click per user per source.

- `--print-url` prints the consent URL instead of opening a browser — use it when the
  right identity lives in a different browser profile.
- `--no-consent` only prints the URLs and connects nothing (headless/CI).
- Exits `3` only when *nothing* was searched; a partial answer from the connected
  sources is a real answer and exits `0`.

**Do not skip `register_consent_url.py`,** and do not point the return URL at a
third-party page. Consent has a final step that is easy to miss: AgentCore redirects
the browser to the return URL with a `?session_id=...`, and the token is vaulted only
when the client calls `CompleteResourceTokenAuth` with it (`client/consent.py`). Send
that redirect somewhere we don't control and the `session_id` is discarded — GitHub
will list the app as authorized while the vault stays empty and every answer comes
back ungrounded. The URL is registered on the runtime's WorkloadIdentity, which
CloudFormation cannot manage because the runtime creates it implicitly.

Ask a question your seeded repos can actually answer. Under STRICT GROUNDING the
agent answers only from retrieved documents, so an unseeded account correctly
returns a LOW-confidence "nothing found" — which reads like a bug and isn't one.

Three kinds of question work, because there are three tools:

| Ask | Tool | Example |
|---|---|---|
| What do I have access to? | `list_available_sources` | "which repositories can I see?" |
| Which document mentions X? | `search_company_documents` | "is there a testing guide for the parser?" |
| What does it actually say? | `read_company_document` | "how do I set up the parser locally?" |

The third only works because search hits carry a `ref` the agent can read in full;
code search alone returns matching fragments, which is often too little to answer
from. Questions that need a field GitHub does not expose — "who *created* this repo"
— still cannot be answered, and the agent will say so rather than guess ownership.

If it fails:

| Symptom | Cause |
|---|---|
| `HTTP 403 OAuth authorization failed: Failed to parse token` | Expired or wrong JWT — `python3 client/login.py --force`. The authorizer rejects it *before* the container runs, so this is not an agent bug. |
| `AccessDeniedException` mentioning Bedrock | Model access not enabled — prerequisite 2. |
| Answer arrives, `connected_sources` empty, consent URL shown | Normal on a first run — approve the page that opens. If the URL 404s at GitHub, step 8 was missed. |
| Consent approved, yet `connected_sources` stays empty forever | The `session_id` redirect never reached the client, so nothing was vaulted. Run `scripts/register_consent_url.py`, and check the runtime's `OAUTH_CALLBACK_URL` matches `CONSENT_RETURN_URL` in `client/consent.py`. |
| Answers cite unrelated public repos | Prerequisite 4 — no company content seeded, *and* check the runtime is at the version that scopes search to `user:`/`org:`. GitHub's `/search/code` is global by default, so unscoped queries return the whole public index. |
| Answer names the right file but says it cannot describe the contents | The runtime predates `read_company_document`. Code search returns matching fragments only; without the read tool a strictly-grounded agent has nothing to quote and lands at LOW. |
| `ValidationException` on the qualifier | `RIPPLE_RUNTIME_QUALIFIER` must be the `EndpointName` output (`live`), not `DEFAULT`. |

Drive-specific, once step 10 is done — all of these report under *"could not be
reached"* rather than as a consent problem, because a delegated source has no consent
step to fail:

| Symptom | Cause |
|---|---|
| `the verified token carries no 'email' claim` | The IdP is putting `email` in the **ID** token only; the runtime validates the **access** token. Fix with the Post-Login Action in [`docs/SETUP.md`](docs/SETUP.md) step 4, then confirm with `python3 client/login.py --claims`. |
| `unauthorized_client` from Google | The delegation grant has the service account's **email** where its **numeric** client ID belongs, or the authorized scope list does not match `drive.readonly` exactly. Also appears for an **alias** instead of the primary address. |
| `Client is unauthorized to retrieve access tokens using this method` | The grant has not propagated yet, or was added in a different Workspace domain. |
| Drive connects, answers nothing, no error | That user has never signed in to Drive, so there is no Drive to read — an empty list, not a failure. Sign in as them once. Or the file bodies have no matching text: `search_drive` uses Drive's `fullText contains`, so a document that is only a *title* matches nothing. |
| `deploy.py` exits 2 about `ALLOWED_EMAIL_DOMAINS` | The two Drive variables are a pair; see step 10. Working as intended. |
| Confident "not found" that seems wrong | Check for `incompleteSearch` in the result. Drive sets it when some drives could not be searched, which makes a partial result look complete. |

### 10. Optional — turn on Google Drive (the delegation flow)

Everything above deploys the GitHub source and the consent flow. Drive is the second
source and the **only** one that demonstrates the no-consent path, so it is worth
enabling even though it needs an admin. It is off until you set two variables, and
turning it on is a redeploy of the runtime stack — no code change.

**This section is the plumbing summary.** The click-by-click walkthrough, with the
exact console paths and the failure mode at each step, is
[`docs/GOOGLE-DRIVE-OKTA-SETUP.md`](docs/GOOGLE-DRIVE-OKTA-SETUP.md).

#### What you must have, and why each one is non-negotiable

| Prerequisite | Why | If you don't have it |
|---|---|---|
| A **domain you own** | Google verifies domain ownership by DNS before admitting it to Workspace. `.test` / `.local` / `.example` / `.invalid` are RFC 2606 reserved: unregisterable, unverifiable, and rejected outright. | Register one (~$10–15/yr). There is no workaround — this is the one hard blocker. |
| **Google Workspace** on that domain, and **super-admin** on it | Domain-wide delegation is a Workspace feature and does not exist for consumer `@gmail.com` accounts. | A Business Starter trial is enough for a demo. |
| A **Google Cloud project** | It holds nothing but the service account. | Any project; create a throwaway. |
| **Two Workspace users**, each signed in once | Two identities are what make the ACL trim visible. A user who has never signed in has no Drive, which reads as an empty answer rather than an error. | One user still works; the demo is just far less convincing. |
| Your IdP putting **`email` and `email_verified` in the ACCESS token** | The runtime validates the access token, and the impersonation subject comes from its `email` claim. | See step 4 of [`docs/SETUP.md`](docs/SETUP.md) — this is the single most commonly missed step. |

The IdP users' `email` claims must equal the Workspace **primary** addresses
byte-for-byte. Aliases fail with `unauthorized_client` rather than resolving, so
create the Workspace users first and the IdP users to match.

#### The five setup steps

1. **Enable the Google Drive API** in the Cloud project (Docs API too, if you want
   structured Docs content).
2. **Create a service account** and grant it **no Cloud IAM roles** — its power comes
   entirely from the Workspace delegation grant, so roles only widen blast radius.
   Copy its **numeric client ID** (~21 digits, not its email address).
3. **Create its JSON key and pipe it straight into Secrets Manager.** The key never
   belongs in `dev.env`, the repo, or a chat window: it is a non-expiring,
   domain-wide credential, and treating its loss as anything less than a
   domain-wide compromise would be wrong. The `create-secret` command prints the
   ARN — that ARN is what configuration carries.
4. **Grant domain-wide delegation** as super-admin (Admin console → *Security → API
   controls → Domain-wide delegation*) to the **numeric** client ID, for exactly
   `https://www.googleapis.com/auth/drive.readonly`. The grant is per-scope and
   exact-match; pasting the service account's *email* into the client-ID field is the
   usual cause of `unauthorized_client`.
5. **Prove it before deploying anything.** This script talks to Google directly with
   no Ripple code in the path, so a failure here is unambiguously an admin-side
   problem:

   ```bash
   python3 scripts/verify_drive_delegation.py amir@yourdomain.com
   python3 scripts/verify_drive_delegation.py dana@yourdomain.com
   ```

#### Plumbing it into the agent

Two variables, and they are a **pair**:

```bash
export GOOGLE_SA_SECRET_ARN=arn:aws:secretsmanager:us-west-2:<account>:secret:ripple/google-drive-sa-xxxxxx
export ALLOWED_EMAIL_DOMAINS=yourdomain.com     # comma-separated for several
```

Both already sit in `dev.env` and `dev.env.example`, commented out. Uncomment, fill
in, `source dev.env`, then:

```bash
python3 scripts/deploy.py --image-only    # or a full deploy; both pass the new params
```

Setting *one* of them is refused, not tolerated. `deploy.py` exits `2` with an
explanation before building anything, and `02-runtime.yaml` asserts it again
(`Rules: DriveRequiresDomainAllowlist`) so a hand-rolled `aws cloudformation deploy`
cannot slip past. The reason is not tidiness: `ALLOWED_EMAIL_DOMAINS` bounds **who
may be impersonated**, and with it empty the acceptable-subject set becomes every
address your IdP will sign for — which turns a per-user ACL trim into a domain-wide
read. `agent/identity_claims.py` fails closed on an empty allowlist and
`tests/test_identity_claims.py` guards exactly that, but the value still has to be
right.

What the variables reach, end to end:

| | Path |
|---|---|
| `GOOGLE_SA_SECRET_ARN` | `dev.env` → `deploy.py` → `02-runtime.yaml` parameter `GoogleSaSecretArn` → the runtime's `GOOGLE_SA_SECRET_ARN` env var **and** a `secretsmanager:GetSecretValue` statement scoped to that one ARN → `agent/gdrive_tool.py` reads the key on first use |
| `ALLOWED_EMAIL_DOMAINS` | `dev.env` → `deploy.py` → parameter `AllowedEmailDomains` → runtime env var → `agent/identity_claims.py` gate 2 |

Non-empty `GOOGLE_SA_SECRET_ARN` is the switch itself: `agent/agent.py` binds the
Drive callables into the `SOURCES` table only when it is set, so an empty string
means Drive is simply absent and the GitHub path deploys exactly as before. The
Google import is guarded, so an older image built before those libraries were pinned
degrades to GitHub-only with a startup warning instead of crash-looping.

#### Confirming it worked

```bash
python3 client/login.py --claims     # must show "email" AND "email_verified": true
python3 client/ask.py "what does the onboarding guide say about VPN access?"
```

`Connected sources: ['gdrive', ...]` and no consent page means the delegation flow is live.
If Drive appears under *"could not be reached"* instead, `ask.py` prints the
delegated-source advice — the cause is the claim or the grant, not anything a user
can authorize away.

### 11. Optional — deploy the two Gateways

`infra/03-gateways.yaml` is a third stack, off by default and not needed for anything
above. It creates:

- an **ingress Gateway** — an HTTP front door with an `Http.AgentcoreRuntime` target in
  front of the same runtime. Its outbound credential is `JWT_PASSTHROUGH`, so it forwards
  the caller's `Authorization` header rather than fetching a token of its own.
- a **tools Gateway** — an MCP server with the GitHub source as a target on per-user
  `OAUTH`, `supportedVersions` pinned to `['2025-11-25', '2026-07-28']`.

```bash
export DEPLOY_GATEWAYS=true
python3 scripts/deploy.py
```

It prints one more `export` line (`RIPPLE_TOOLS_GATEWAY_URL`) for `dev.env` and the
ingress URL, which takes the same `{"prompt": ...}` body as the runtime — only the
*tools* Gateway speaks MCP.

Both are **additive**. The ingress Gateway is a second door, not a replacement: the
runtime stays directly invocable and `client/ask.py` keeps calling it directly. Sources
stay in-process until you move one, per source:

```bash
export GITHUB_VIA=GATEWAY     # then redeploy the runtime stack
```

`deploy.py` refuses `GITHUB_VIA=GATEWAY` with no Gateway available, rather than letting
the agent fall back to in-process at startup with only a warning.

⚠️ **Moving a source onto the tools Gateway asks the user to consent a SECOND time.**
A vaulted token belongs to the workload that vaulted it, and the Gateway target's
credential provider is a different workload from the runtime's — so a user who has
already connected GitHub in-process must approve it again for the Gateway route. The
Gateway signals this with an MCP *elicitation* (JSON-RPC `-32042`) carrying an authorize
URL, which Ripple surfaces in the same `auth_required` field as any other consent, so a
client does not need to know which route a source is on. The loopback return URL must be
allowlisted on the *Gateway's* workload identity too, or the redirect after approval lands
on nothing — the browser shows "This site can't be reached" and no error appears anywhere
in AWS. `scripts/register_consent_url.py` now does both; re-run it after deploying the
Gateways:

```bash
python3 scripts/register_consent_url.py   # runtime + tools Gateway, idempotent
```

Verified end to end on this route: consent completes, and search returns hits scoped to
the caller's own repositories. Two defects were found by running it and are fixed —
the elicitation URL used to be discarded and reported as a transport error, and the
Gateway's OpenAPI target had no `/user` operation, so an account in no organizations
searched *all public GitHub*. `agent/gateway_tool.py` now refuses to search unscoped
rather than degrade quietly.

The known cost of this route is answer quality, not correctness: an OpenAPI target cannot
vary the `Accept` header per call, so hits arrive without GitHub's text-match snippets.
In practice the deployed route still answered at HIGH confidence, because routing is **per
operation** — only `search` moves to the Gateway, while `read_company_document` stays
in-process, so the model recovers the text it needs by reading a promising hit in full.
That recovery depends on the in-process credential also being present: a user who
consented *only* to the Gateway has search but no `fetch`, and those answers do degrade to
LOW. `IN_PROCESS` remains the default for that reason and because it needs one consent
rather than two.

⚠️ **Deploying the ingress Gateway does not close the direct path, and cannot today.**
The runtime parameter that would (`AllowedWorkloadConfiguration`, pinning the runtime to
one caller) is deliberately left unset, because a `JWT_PASSTHROUGH` target never mints a
Workload Access Token and so never stamps the workload identity chain that parameter
introspects — setting it while the target is on passthrough returns `401` on *every*
path, the Gateway's included. Closing the bypass means moving the ingress target to
`OAUTH` outbound **and** setting the parameter: one change, not two. The evidence, the
trade-off, and the correct way to test it are in `infra/03-gateways.yaml`
(`IngressGateway` comment, mitigation 2).

Identity **does** survive the hop, and this is verified: the same user JWT sent to the
Gateway URL and to the runtime directly resolves to the same named `sub`, which is what
per-user Drive and GitHub reads depend on.

⚠️ That verification only passes because the runtime sets
`RequestHeaderConfiguration.RequestHeaderAllowlist: ['Authorization']`. The allowlist
defaults to forwarding **nothing**, so without it `Authorization` reaches no container on
either path and the agent answers as `"user": "anonymous"` with no connected sources — a
`200`, not an error, which is what makes it easy to misread as an unconsented user. The
template sets it; if you build your own runtime, set it too.

### 12. Optional — deploy the managed AgentCore Harness (EXPERIMENTAL — don't use for now)

> 🛑 **Do not use the harness route for deployment yet. Deploy via the runtime route
> (steps 1–11, `02-runtime.yaml`) instead.** The harness is a deployed experiment, not the
> production path: a caller can override its guardrails at invoke time (see the assessment
> below) and it cannot serve Google Drive. The steps here are for reproducing that experiment,
> not for shipping.

`infra/04-harness.yaml` is a fourth stack, off by default. It deploys the same product as
a **managed agent**: `AWS::BedrockAgentCore::Harness`, where the model, system prompt,
tools, memory and iteration limits are *configuration* and **AWS runs the agent loop**.

```bash
export DEPLOY_GATEWAYS=true    # required: the harness's only tool is the tools Gateway
export DEPLOY_HARNESS=true
python3 scripts/deploy.py
python3 scripts/register_consent_url.py   # now covers a THIRD workload identity
```

> **Full assessment: [`docs/HARNESS-ASSESSMENT.md`](docs/HARNESS-ASSESSMENT.md)** — why the
> managed harness cannot yet be the primary agent, from live probes. Headline: on the harness
> the system prompt and tool allowlist are *per-request defaults* rather than server-enforced
> limits, so they cannot be relied on behind an untrusted front door the way the runtime's
> baked-in guardrails can. That, plus the M2/M3 gaps below, is why `02-runtime.yaml` stays
> primary. (The precise invoke-time behaviour was characterised separately and is not
> reproduced here.)

⚠️ **A Harness is not the SDK harness in our container, and it does not run `agent/agent.py`.**
Two different things share the name. `bedrock_agentcore.runtime.BedrockAgentCoreApp` — what
`agent.py` uses, and what step 7 deploys — is a server *inside* our image; our Python is
the agent. `AWS::BedrockAgentCore::Harness` is a managed AWS resource with its own API and
console page. On a Harness the container is an **environment, not an application**: AWS
overrides its `ENTRYPOINT` and `CMD`, so our startup command never executes. `@app.entrypoint`
is never called and `@tool`-decorated Python is never registered. A harness tool can only be
one of five things — `remote_mcp`, `agentcore_gateway`, `agentcore_browser`,
`agentcore_code_interpreter`, or `inline_function` (which runs in the *caller*).

It also **provisions its own runtime underneath**. That is where the extra `harness_*`
runtime and log group in the account come from; don't manage it directly.

**This stack is additive, and the runtime stack must stay.** It is not a migration you can
finish by deleting stack 2:

| | Runtime (stack 2) | Harness (stack 4) |
|---|---|---|
| Agent loop | our Strands loop in `agent.py` | AWS's, service-side |
| GitHub — consent flow | ✅ in-process, or via Gateway | ✅ declarative (`OutboundAuth.Oauth`) |
| Google Drive — delegation flow | ✅ | ❌ **cannot be served** |
| Model / prompt change | rebuild + redeploy image | `UpdateHarness` |
| Conversation memory | provisioned, never read | ✅ read and written by AWS |
| Iteration / token ceiling | none | `MaxIterations`, `MaxTokens` |
| `--image-only` | rolls the image | no-op |

⚠️ **The harness cannot serve Google Drive, and that is a security stop rather than a
missing feature.** Two independent blockers. Its gateway outbound auth is a union of
exactly `{AwsIam | None | Oauth}`, and Drive's mechanism is none of those — it is a JWT
assertion signed with a *Google service account* key carrying the user's verified email in
`sub`. And a Harness has **no `RequestHeaderConfiguration`**, so nothing in the tool path
can read the caller's `Authorization` header; a Gateway Lambda target doesn't help either,
as its context carries only gateway/target/tool ids and no caller. The service-account key
holds domain-wide delegation, so the *only* thing narrowing it to one person is the subject
we pass, which must come from a signature-verified claim. A Drive tool that cannot learn
who is asking cannot narrow anything — it would turn a per-user read into a domain-wide one
while every log line still looked correct. So the template ships **no** Drive tool rather
than a degraded one. The two survivable designs, and why each moves a trust boundary, are
in `infra/04-harness.yaml` § M2.

**Testing it in the console playground.** *AgentCore → Harness → Harness playground* needs a
JWT in **Configs → Authentication → JWT token** — being signed into the console is not
enough, because the harness has a `CustomJWTAuthorizer` and refuses any session without a
bearer token from *our* IdP:

```bash
python3 client/login.py --copy    # copies the token, prints only its remaining lifetime
```

It deliberately does not echo the token: that value is a bearer credential, so anything
holding it can act as you until it expires — keep it out of scrollback, tickets and
screenshots. Use `--print-token` if you need it on stdout anyway.

**Status: deployed and invoked. The managed loop works; the Gateway tool does not yet.**
Verified on `ripple_harness-5tCOqF76a2`: it reaches the model, honours the system prompt
verbatim (`Sources:` block, `Confidence: LOW`, and a refusal to guess at a roadmap it could
not retrieve), and writes to Memory. But loading its one tool fails:

```
runtimeClientError ... Failed to load tool 'ripple-tools' (type=agentcore_gateway):
Failed to start MCP client ... Client error '401 Unauthorized' for url
https://ripple-tools-xaeszuty3h.gateway.bedrock-agentcore.us-west-2.amazonaws.com/mcp
```

The tools Gateway is on `CUSTOM_JWT` inbound, so it accepts only a bearer JWT from its
configured issuer — **both** `AWS_IAM` (SigV4) and `NONE` are refused, tested. `OAUTH` is
the matching value and is wired in the template, but it needs a credential provider for the
*Gateway's* issuer (an Auth0 `client_credentials` client), and this project has only a
public CLI client with no secret. Creating one is an Auth0-side action, so `AWS_IAM` remains
the default as the least-surprising no-credential value — read it as *not yet wired*, not
*verified*. Until then the harness answers every question at LOW confidence with no sources,
and **does not surface the auth failure to the user**.

⚠️ **A green stack proves nothing about whether the agent can answer.** Two of the three
defects above were invisible at deploy time and only appeared inside the event stream on the
first `InvokeHarness`. The other was `Temperature: 0.2` — accepted by the CFN schema, then
rejected by the model with `` `temperature` is deprecated for this model `` on every
invocation. Model parameters are validated by the *model* at invoke time, not by
CloudFormation. Anything added to `Model` must be tested with a real call.

⚠️ **Invoke it with a Bearer JWT, never SigV4.** SigV4 is refused outright here
(`AccessDeniedException: This harness requires OAuth Bearer token authentication`) because
of the authorizer below — but on a harness *without* one, SigV4 succeeds and silently stops
propagating per-user identity downstream, collapsing every user's access onto one shared
credential. That is why `AuthorizerConfiguration.CustomJWTAuthorizer` is load-bearing here
in a way it isn't on the runtime. Note that boto3 needs the SigV4 signer *disabled*
(`Config(signature_version=UNSIGNED)`) before an `Authorization` header survives — adding
the header alone leaves SigV4 in place and the call is rejected.

⚠️ **A third consent.** The harness is its own workload identity — the real one here is
`harness_ripple_harness-ejdAHhBoDB` — so a user who already connected GitHub on the runtime
*or* the Gateway route has **not** consented here, the same rule as step 11 applying a third
time. Note the doubled word: the `harness_` prefix is literal and the harness name follows,
and the service appends a random suffix, so the name **cannot be derived** and
`register_consent_url.py` discovers it by prefix instead. Skip that step and the redirect
after Approve lands on nothing, with no error anywhere in AWS.

It also provisioned its own runtime, `harness_ripple_harness-ejdAHhBoDB` — same name as the
workload identity. That is the answer to "where did this extra `harness_*` runtime and log
group come from"; don't manage it directly.

Two more things worth knowing before choosing this route. `AllowedTools` is set explicitly
because a harness otherwise grants `shell` **and** `file_operations` in every session — a
document-reading agent needs neither, and a prompt injection carried inside a retrieved
document does. And `Memory.ActorId` is deliberately *not* set in the template: pinning it
would give every caller one shared conversation history. Per-user partitioning comes from
the `actorId` argument on `InvokeHarness`, which is **caller-controlled** — a request field,
not a claim — so the caller must derive it from the verified token `sub`, or a caller can
read another user's history. That is the header-vs-body trap `_caller_jwt()` exists to make
unrepresentable, reappearing one layer up.

What this route gives up, beyond Drive: `identity_claims.verified_email()`'s five checks
(nothing re-verifies anything; the platform authorizer is the only gate), the per-source
fan-out and merge that makes "which source said this" precise, the refusal that hides a
POLICY failure from the user, and — concretely — the hard error in `gateway_tool.py::_scope`
that makes an unscoped GitHub search impossible. On the harness the model calls the
Gateway's tools directly, so only the **system prompt** stands between it and a search of
all public GitHub. That defect was found by running the Gateway route and fixed in code;
this path can reintroduce it. `tests/test_harness_prompt.py` exists for that reason — the
prompt is duplicated in `agent.py` and in the template, and it fails when a load-bearing
rule is dropped from either.

Also: `M3 TOKEN EXCHANGE` **cannot be expressed in CloudFormation** on a harness. The API
accepts a `TOKEN_EXCHANGE` grant type; the CFN schema for the same field accepts only
`CLIENT_CREDENTIALS` and `AUTHORIZATION_CODE`. It needs `create-harness`/`update-harness`
over the API until that closes.

### 13. Activate the cost allocation tag (once per account)

```bash
aws ce update-cost-allocation-tags-status --region us-east-1 \
    --cost-allocation-tags-status TagKey=project_name,Status=Active
```

Every resource already carries `project_name=ripple_slack_assistant` natively, but
the tag does not appear in Cost Explorer until it is activated — always in
`us-east-1`, regardless of where the stack lives. Takes up to 24h and is **not
retroactive**, so do it on deploy day or the first day's spend is unattributable.

### Redeploying

Code change only, no infrastructure change:

```bash
python3 scripts/deploy.py --image-only   # rebuild the image, roll the runtime
```

This skips preflight by design — the names it checks are supposed to exist now.

Two things to know when changing deploy-time config:

- **A changed template `Default` does nothing on an existing stack.**
  `aws cloudformation deploy` reuses the stored value for any parameter absent from
  `--parameter-overrides`, so `deploy.py` must pass it explicitly. This silently
  shipped a stale consent URL once.
- **Re-run `scripts/register_consent_url.py` if the runtime was replaced** — its
  WorkloadIdentity is created implicitly and cannot be declared in the template.

### Tearing down

Order matters. The runtime stack imports the foundation stack's ECR repository URI,
so foundation cannot be deleted while that export is in use:

```bash
aws cloudformation delete-stack --stack-name ripple-runtime
aws cloudformation wait stack-delete-complete --stack-name ripple-runtime

# ECR has no Retain policy, so CFN deletes it — but that FAILS while images
# remain, taking the whole foundation delete to DELETE_FAILED. Empty it first.
aws ecr delete-repository --repository-name bedrock-agentcore-ripple --force

aws cloudformation delete-stack --stack-name ripple-foundation
```

Three things deliberately survive, and cost money until removed by hand:

- the **S3 build-source bucket** — `DeletionPolicy: Retain`, so an accidental stack
  delete cannot destroy uploaded sources. Empty it, then delete it.
- the **Secrets Manager secret** from step 4 — `aws secretsmanager delete-secret`.
- the **runtime CloudWatch log group** — never declared, because its name embeds
  the generated runtime id.

Deploying by hand without `deploy.py`, and everything above in more depth:
[`infra/README.md`](infra/README.md).

## Architecture

[`infra/ARCHITECTURE.md`](infra/ARCHITECTURE.md) — the component view plus one sequence
diagram per flow with every edge numbered (`infra/architecture-deploy-flow.png`,
`infra/architecture-delegation-flow.png`, `infra/architecture-consent-flow.png`).

[`infra/architecture-components-v3.png`](infra/architecture-components-v3.png) is the
current component view. It draws **both flows at once** — green `1a`–`14a` for delegation,
orange `1b`–`14b` plus `★` for consent — with a step-by-step table per flow derived from
the same edge list that draws the arrows, so a badge and its description cannot disagree.
It also badges every last hop with the mechanism it uses (`M1` / `M2` / `M3`). Dashed
tiles do not exist yet; solid ones do, and a solid tile tagged **OPT-IN** — both Gateways —
is deployed but off the traced path until configured. The three flow PNGs then draw one
flow each, in sequence form, and number their own flow independently: a step number from
one of them means nothing without naming which diagram it came from. Every source file
carries a `WHERE THIS SITS IN THE ARCHITECTURE DIAGRAM` comment naming the components and
numbered steps it implements, so you can grep from either direction:

```bash
grep -rn "WHERE THIS SITS IN THE ARCHITECTURE DIAGRAM" agent client scripts infra tests
```

`ARCHITECTURE.md` also
documents the **state model**: the solution is stateless at every layer, which is
deliberate, and the file explains exactly what to change to make it stateful.

Regenerate the PNGs (needs `matplotlib`, run from this directory):

```bash
python3 infra/render_components_v3.py   # the current component diagram
python3 infra/render_diagrams.py        # the three flow diagrams
```

Both refuse to emit a PNG that fails their self-checks.

### Earlier implementations

The current component diagram is `infra/architecture-components-v3.png` (rendered by
`infra/render_components_v3.py`). Two superseded drawings are kept only as a record of
what the diagram used to claim — not as alternatives, and nothing above depends on them:

- `architecture-components.png` (v1) labels the Google Drive path **"OBO"**, a name this
  repo has since retired: in `agent/agent.py` that word means the *token exchange* (M3),
  while Drive is *delegation* (M2). Read v1's green arrows as M2. It also predates the
  second Gateway and the five-source picture.
- `architecture-components-v2.png` (v2) is an intermediate view without the M1/M2/M3 hop
  badges.

`render_components.py` and `render_components_v2.py` still run and emit these historical
PNGs — use them only to reproduce a past diagram, never to update the current one.
`infra/README.md` has the full version table.

## Notes for reviewers

- **No baked-in defaults.** Every config value is read via `scripts/env_util.py`
  `require_env()`, which exits 2 with a usable message if unset. A missing value
  fails before anything is created, not halfway through a deploy.
- **Secrets never enter a file.** `GITHUB_CLIENT_SECRET` lives in your shell only;
  `dev.env` holds non-secret config. The preferred path passes a Secrets Manager
  **ARN** so the value never reaches a CloudFormation parameter or stack history.
- **The starter toolkit is gone.** `bedrock-agentcore-starter-toolkit` is slated
  for deprecation and nothing here imports it. Note that `bedrock-agentcore` (what
  `agent/` imports) is the **SDK** — a different package, not deprecated.
- **One imperative step is irreducible.** CloudFormation cannot build a container
  image, and `AWS::BedrockAgentCore::Runtime` requires a `ContainerUri` that
  already exists. `scripts/cfn_build.py` fills that slot and creates no resources.
- **Cost attribution.** Every resource carries `project_name=ripple_slack_assistant`
  natively at creation, including the two IAM roles — which the toolkit's shared
  `AgentCoreRuntimeRole` could not, because it was shared across four agents.
  Activation in Billing is Quickstart step 13.
