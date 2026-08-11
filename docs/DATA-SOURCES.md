# Data Sources — onboarding every source

This is the single place to look for "how do I add source X". It covers all five
sources the solution is built around: **GitHub** and **Google Drive**, which are
implemented, and **Databricks Genie**, **Confluence** and **Slack**, which are target
state with no implementation in this repo.

**Every source shares one guarantee.** The source itself trims results to what the
signed-in user may read, because the source is called with a token that represents
*that user* — never a service account, and never filtering applied after the fact by
this code. GitHub returns the repos that user can see; Drive returns the files that
user can open. The two mechanisms that reach that guarantee differ, but the guarantee
does not.

**Adding a source is a row plus a credential.** Sources live in the `SOURCES` dict in
[`../agent/agent.py`](../agent/agent.py). Credential brokering, per-source consent
URLs, the response contract and the client's output all iterate over that dict, so a
new source touches neither `invoke()`, nor the response shape, nor the client. The
four-step recipe is at the end of this file.

---

## The five sources

| Source | Credential kind | Flow | Status | Who sets it up |
|---|---|---|---|---|
| **GitHub** | `VAULTED_OAUTH` | consent (`USER_FEDERATION`) | **Implemented** | Whoever deploys registers one OAuth App they own — no org admin. Then one consent click per end user, per source, once. |
| **Google Drive / Docs** | `DELEGATED_SUBJECT` | delegation, `M2` (no consent screen) | **Implemented** | Google Workspace **super-admin**, once (domain-wide delegation). End users do nothing. |
| **Databricks Genie** (MCP) | `VAULTED_OAUTH` | token exchange (`M3`) if fronted by an IdP-guarded MCP server; consent otherwise | Target state — **not implemented** | Platform/IdP admin (app registration + audience + AgentCore credential provider) |
| **Confluence** | `VAULTED_OAUTH` | consent by default; token exchange only if confirmed | Target state — **not implemented** | Atlassian admin for the OAuth app; then one consent click per end user |
| **Slack** (as a data source) | `VAULTED_OAUTH` | consent by default; token exchange only if confirmed | Target state — **not implemented** | Slack Workspace admin for the app; then one consent click per end user |

Credential kinds and flows above come from `SOURCES` and `_flow_for()` in
`agent/agent.py`. The three target-state rows exist there only as commented-out
placeholders — the "Flow" cell for those is what the mechanism *would* require, not a
configured value.

A source whose environment variables are absent is **skipped, not fatal**. Only if
*no* source is configured does the runtime refuse to start, because an agent with no
search source cannot answer anything under strict grounding.

---

## The flows

Two flows are *drawn* — consent and no-consent — but there are **three mechanisms**,
because the no-consent outcome is reached two incompatible ways. `M1` / `M2` / `M3` below
are the same labels the current component diagram badges every hop with.

**Consent (`M1`) — `USER_FEDERATION`.** Three-legged OAuth. The user clicks once, per
source, per lifetime; AgentCore Identity vaults a refresh token server-side and every
later question finds it without a consent screen. Works against any OAuth2 provider,
which is why it is the default. Diagram:
[`../infra/architecture-consent-flow.png`](../infra/architecture-consent-flow.png).

**No consent screen — two different mechanisms, and they are not interchangeable.** One
is a token exchange (`M3`), the other a delegated subject (`M2`). They share only the
*outcome*, which is why this document once called both "OBO" and why it no longer does:
that name is reserved for `ON_BEHALF_OF_TOKEN_EXCHANGE` — the exchange alone — in
`agent/agent.py`, so using it for Google Drive named a mechanism Drive cannot use.

- **RFC 8693 token exchange** — the user's IdP JWT is exchanged for a token at the
  resource. Requires both the IdP *and* the resource to support the exchange grant.
  No source in this repo runs this path today.
- **Delegated subject** (`DELEGATED_SUBJECT`) — no user token exists at all. The
  source impersonates the user *by name*, so the search callable receives the
  **verified email** instead of a token. Google Drive works this way. Because a
  delegated source can reach any user in the domain, its subject must come from
  `identity_claims.verified_email()` — signature, issuer and audience all checked.

Diagram (the delegated-subject shape, which is the one that runs today):
[`../infra/architecture-delegation-flow.png`](../infra/architecture-delegation-flow.png).

**Which flow a source uses is configuration, not code.** `_flow_for()` reads
`<SOURCE>_AUTH_FLOW` and defaults to `USER_FEDERATION`; that is requirement **FR-34**
— a source must be migratable between paths with no code change.

**The failure mode is hard, not graceful.** The token exchange is not universally
available, and an unsupported grant is an error from the vendor's token endpoint rather
than a fallback to consent. Verified empirically: **GitHub returns
`{"error": "unsupported_grant_type"}` for the token-exchange grant, so GitHub cannot use
it** no matter how identity is arranged. Never set a source's flow to
`ON_BEHALF_OF_TOKEN_EXCHANGE` without first confirming against that vendor's token
endpoint that the exchange is accepted.

---

## GitHub — implemented, consent flow

Goal: register ONE OAuth app **you own**, so AgentCore Identity can mint a
**per-user** GitHub token. Each user consents to their OWN account, so GitHub
returns only the repos/code/issues that user can see — that IS the permission trim,
enforced by GitHub, not by this code. No corporate admin, no separate login.

This is the **consent flow** (orange, steps `1b`–`14b` plus `★` in
[`../infra/architecture-components.png`](../infra/architecture-components.png)). It is
the flow with a human in it: exactly one click, per user, per source, once.

Two kinds of value come out of this: **non-secret** ones (📋) go in `dev.env`, and the
**client secret** (🔐) goes to your shell and then straight into Secrets Manager,
never into a file in this repo.

One step has to happen **after** the first deploy: the app's *Authorization callback
URL* is issued by AgentCore Identity, so it does not exist until the credential
provider does. It is marked ↩ below.

### GitHub OAuth App

1. <https://github.com> → your avatar → **Settings** → **Developer settings**
   (bottom of left nav) → **OAuth Apps** → **New OAuth App**.
2. Fill:
   - **Application name:** `Ripple Knowledge Assistant`
   - **Homepage URL:** `https://example.com` (placeholder is fine)
   - **Authorization callback URL:** `https://example.com/callback`
     (↩ replaced with the real AgentCore callback URL after the first deploy.)
   - **Enable Device Flow:** leave **unchecked** — AgentCore uses the standard
     authorization-code (browser redirect) flow, not device flow.
3. **Register application.**
4. On the app page:
   - 📋 copy the **Client ID** → `GITHUB_CLIENT_ID` in `dev.env`. Not a secret.
   - Click **Generate a new client secret** → copy the value **once**; GitHub will
     not show it again.
     🔐 in your terminal, not in any file (the leading space keeps it out of shell
     history under `HIST_IGNORE_SPACE`):
     ```bash
      export GITHUB_CLIENT_SECRET='paste-value-here'
     ```
     Then create the Secrets Manager secret and put its **ARN** in `dev.env` as
     `GITHUB_CLIENT_SECRET_ARN` — exact commands, piped so the value never becomes an
     argv element, are in
     [`../infra/README.md` § *Creating `GithubClientSecretArn`*](../infra/README.md).
     CloudFormation then receives a pointer, so the secret never enters a template
     parameter or stack history.
5. Scopes are requested at consent time (`GITHUB_SCOPES`, default `repo read:org`) —
   nothing to configure on the app.

> `repo` lets Ripple search private repos the user can access; `read:org` covers org
> repos. For a public-only demo, set `GITHUB_SCOPES="public_repo"` in `dev.env` — the
> trim still works, it is just less striking.

### Seed test content (makes the permission-trim demo visible)

Without this the agent answers a correct but unconvincing "nothing found" for every
question, because STRICT GROUNDING means it will not answer from anything it did not
retrieve.

Use two GitHub identities that can see DIFFERENT things:

- A **private repo** (e.g. `ripple-knowledge`) with a few markdown docs, e.g.
  `roadmap.md`, `compensation-fy26.md`, `onboarding.md`.
- Add your **second GitHub account** as a collaborator on this repo, but keep a
  SECOND private repo (e.g. `ripple-comp`) collaborator = account 1 only.
- Demo:
  - Account 1 asks about comp → **HIGH** with a citation to `ripple-comp`.
  - Account 2 asks the same → **LOW** / "not found" (it cannot see that repo).

Put real sentences in the files, not just titles. GitHub code search matches
**content**, so a document that is only a filename matches nothing.

(With only one account, a simpler variant: one private repo you own vs. a public repo
— the trim is less dramatic but still demonstrates per-user access.)

### ↩ After the first deploy: fix the callback URL

CloudFormation creates the `OAuth2CredentialProvider`, and AgentCore Identity issues
it a callback URL containing a generated UUID. `scripts/deploy.py` prints that URL on
success; you can also read it back at any time:

```bash
aws cloudformation describe-stacks --stack-name ripple-runtime \
  --region "$AWS_REGION" \
  --query 'Stacks[0].Outputs[?OutputKey==`GithubCallbackUrl`].OutputValue' --output text
```

Then: GitHub OAuth App → **Authorization callback URL** → replace the placeholder →
**Update application**.

GitHub auth fails until this is done. If the provider is ever *replaced* — its `Name`
and `CredentialProviderVendor` are `createOnly` — a **new** URL is issued and this
step repeats. See
[`../infra/README.md` § *Replacing the credential provider*](../infra/README.md).

### What goes where

| Value | Destination | Secret? |
|---|---|---|
| 📋 GitHub **Client ID** | `GITHUB_CLIENT_ID` in `dev.env` | No |
| 🔐 GitHub **client secret** | your shell → Secrets Manager; only its ARN reaches `dev.env` as `GITHUB_CLIENT_SECRET_ARN` | **Yes** — never in a file here |
| 📋 the two test accounts' GitHub logins, and which repos each can see | nowhere in config — this is just what you need to know to read the demo | No |
| ↩ the AgentCore callback URL | pasted into the GitHub OAuth App | No |

`dev.env` is gitignored and holds non-secret configuration only.

---

## Google Drive — implemented, delegation flow (`M2`)

Drive is the source with **no consent screen at any step**. It is
`DELEGATED_SUBJECT`: there is no per-user Drive token to vault, because only Google
issues tokens Drive accepts. Instead a Google **service account** — created with **no
IAM roles**, since its power comes entirely from the Workspace grant — signs an
assertion whose subject is the user's verified `email` claim. Google returns a token
scoped to that one user, and Drive's own ACLs do the trimming.

What that setup consists of, in outline:

- A Google Cloud project with the Drive API enabled, and a service account in it with
  **no IAM roles**.
- A **JSON key** for that service account, stored in Secrets Manager; its ARN becomes
  `GOOGLE_SA_SECRET_ARN`, which is also the variable that switches the source on.
- **Domain-wide delegation** granted once by a Workspace super-admin, for
  `https://www.googleapis.com/auth/drive.readonly` and nothing more.
- `ALLOWED_EMAIL_DOMAINS` in `dev.env`, which bounds *who may be impersonated*. It
  pairs with `GOOGLE_SA_SECRET_ARN`: the ARN grants the capability, the domain list
  constrains it.
- The IdP's `email` claim must equal the Workspace **primary** address exactly, and
  must arrive in the **access** token alongside `email_verified`.

⚠️ The JSON key is a **long-lived, domain-wide** credential: it can impersonate any
user in the domain for the granted scopes, with no expiry and no revocation short of
deleting it. It goes to Secrets Manager only — never a file in this repo, never a
CloudFormation parameter — and losing it should be treated as a domain-wide
compromise. That blast radius is why the delegated path is fail-closed on verified
claims before it names a subject.

Full procedure — including the "Okta SSO into Workspace" vs. "domain-wide delegation"
distinction, seed content, and a standalone verification script that proves the grant
before any Ripple code depends on it:
[`GOOGLE-DRIVE-OKTA-SETUP.md`](GOOGLE-DRIVE-OKTA-SETUP.md).

---

## The three MCP sources — target state, not implemented

Databricks Genie, Confluence and Slack are Ripple's other sources, and Ripple's
current architecture already fans out to four MCP servers. **None of the
three is implemented here.** In this repo they exist as commented-out rows in
`SOURCES` and as design notes in
[`../infra/ARCHITECTURE.md`](../infra/ARCHITECTURE.md). There are no setup steps to
follow yet, and the notes below are not instructions — they record which credential
kind and flow each source would take, what has to exist first, and what is genuinely
unresolved.

Two facts apply to all three.

**An IdP-guarded MCP server is where the token-exchange code path would first actually
run.**
Register the MCP server as an app in the IdP with its own audience, and register it in
AgentCore as a `customOauth2ProviderConfig` with
`onBehalfOfTokenExchangeConfig.grantType = TOKEN_EXCHANGE`. That would be the first
source in this project where `ON_BEHALF_OF_TOKEN_EXCHANGE` executes at all, because
GitHub rejects the grant and AgentCore's built-in Google provider cannot express it —
`googleOauth2ProviderConfig` accepts only `clientId` / `clientSecret` /
`clientSecretConfig` / `clientSecretSource`, with no `onBehalfOfTokenExchangeConfig`
member. Nothing in the repo runs this today.

**Whether a given vendor accepts the exchange grant has to be confirmed against that
vendor.** There is no capability document to consult, the answer is per-vendor, and
the failure is a hard error from the token endpoint rather than a fallback to consent.
Confirm it before committing a source to the token-exchange path — not after.

### Databricks Genie

- **Credential kind:** `VAULTED_OAUTH`. Genie is reached over MCP under the user's own
  token, not by impersonating the user by name, so it is not `DELEGATED_SUBJECT`.
- **Flow:** the token exchange is the reason to prefer an MCP front door — an MCP server registered in
  the IdP with its own audience is exactly the shape described above. Absent that, the
  default `USER_FEDERATION` consent path applies.
- **Must exist first:** the MCP server itself and its IdP app registration; an
  AgentCore credential provider for it (a resource alongside `GithubProvider` in
  [`../infra/02-runtime.yaml`](../infra/02-runtime.yaml)); a `search_genie` callable in
  the agent; and an MCP client path in the runtime. The Tools Gateway itself is deployed
  (`../infra/03-gateways.yaml`) with the GitHub target live, so registering Genie as a
  second target is the incremental step rather than standing up new infrastructure.
- **Unresolved:** whether Databricks' token endpoint accepts the token-exchange grant,
  and therefore whether this source can be on the exchange at all. Also unresolved: what a Genie
  result maps onto in the `search` / `fetch` / `inventory` shape, since Genie answers
  questions over data rather than returning documents.

### Confluence

- **Credential kind:** `VAULTED_OAUTH` — Atlassian issues a per-user token, which
  AgentCore Identity would broker and vault.
- **Flow:** consent (`USER_FEDERATION`) unless and until Atlassian's token endpoint is
  confirmed to accept the exchange grant. The default is deliberate: consent works
  against any OAuth2 provider, whereas an unverified token-exchange assumption fails at the first
  question.
- **Must exist first:** an OAuth app on the Atlassian side, its credential provider
  resource, the callback URL pasted back into that app after the first deploy (the same
  ↩ step GitHub has), `CONFLUENCE_CREDENTIAL_PROVIDER` and `CONFLUENCE_SCOPES` injected
  into the runtime, and a `search_confluence` callable.
- **Unresolved:** the scope set, and whether Confluence search returns enough body text
  for a grounded, citable answer without a second `fetch` call per hit.

### Slack

- **Credential kind:** `VAULTED_OAUTH` — searched under the user's own Slack token, so
  Slack's own channel and DM membership does the trimming.
- **Flow:** consent (`USER_FEDERATION`), same reasoning as Confluence; token exchange only on
  confirmation against Slack's token endpoint.
- **Must exist first:** a Slack app in the workspace, its credential provider resource
  and callback URL, scopes injected, and a `search_slack` callable.
- **Unresolved:** the scope set, and whether the exchange grant is available.

> **Slack appears in the target state twice, and they are different things.** As the
> **front door** it replaces the CLI — IdP SSO → Slack → Events API → a receiver that
> maps the Slack user to their IdP identity → the agent; that is described in
> [`SETUP.md`](SETUP.md) Part 3 and changes no agent code. As a **data source** it is a
> row in `SOURCES` like any other. Neither is implemented, and building one does not
> give you the other.

---

## Adding a source

The recipe, from the comment above `SOURCES` in
[`../agent/agent.py`](../agent/agent.py):

1. **Pick its credential kind.** `DELEGATED_SUBJECT` if the vendor can be reached by
   impersonating a user with no consent; `VAULTED_OAUTH` otherwise.
2. **Provision what that kind needs.** For `VAULTED_OAUTH`: create its OAuth2
   credential provider as a resource in `infra/02-runtime.yaml`, paste the callback URL
   it issues into that vendor's OAuth app — `Name` is `createOnly`, so a rename
   reissues the URL — and inject `<KEY>_CREDENTIAL_PROVIDER` and `<KEY>_SCOPES`. For
   `DELEGATED_SUBJECT`: inject whatever the impersonation needs (for Drive, a Secrets
   Manager ARN) and name that variable in `enabled_by`.
3. **Add the row.** A `search` callable is required. Two more are optional: `fetch`
   (one document's full text, for `read_company_document`) and `inventory` (what this
   user can see, for `list_available_sources`). A source missing either is simply not
   offered to that tool; neither is needed to make the source useful.
4. **Nothing else.** Credential brokering, per-source consent URLs, the response
   contract and the client's output all iterate over the dict.

Two consequences worth knowing.

**Four sources is where a Tools Gateway starts to pay for itself.** The Gateway is
deployed already (`infra/03-gateways.yaml`, MCP, `supportedVersions` pinned, GitHub
target live), but routing through it is opt-in per source via `<SOURCE>_VIA=GATEWAY`, and
in-process brokering stays the default. Around the fourth source the argument flips and
the fan-out is what you want by default rather than the exception — see
[`../infra/ARCHITECTURE.md`](../infra/ARCHITECTURE.md). However the tools are routed,
per-user permission trimming must stay enforced by each source via that user's own
token: never a service account, never filtering of our own.

**A source with absent environment variables is skipped, not fatal.** So one template
deploys a GitHub-only stack and a multi-source stack with no code change and no
template fork.

### The recipe above does not apply to the managed Harness

If the source also has to be reachable from `infra/04-harness.yaml`'s
`AWS::BedrockAgentCore::Harness` (opt-in; README step 12), none of steps 1–4 help. A harness
overrides the container's `ENTRYPOINT`, so `agent/agent.py` never runs and the `SOURCES` dict
is never read there. A harness tool is one of exactly five kinds — `remote_mcp`,
`agentcore_gateway`, `agentcore_browser`, `agentcore_code_interpreter`, `inline_function` —
so the source must be reached through the tools Gateway (or its own MCP endpoint) and declared
in the template's `Tools` list *and* in `AllowedTools`.

That is also why **`DELEGATED_SUBJECT` sources cannot be served on the harness at all.** A
gateway tool's outbound auth is a union of exactly `{AwsIam | None | Oauth}`, none of which is
"an assertion we sign ourselves", and a harness has no `RequestHeaderConfiguration`, so nothing
in the tool path can read the caller's `Authorization` header to learn *which* user to
impersonate. For Drive that turns a per-user read into a domain-wide one, so the template
carries no Drive tool. `VAULTED_OAUTH` sources are the ones that port — and each brings a
**third consent**, because the harness is its own workload identity.
