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
         |-- OBO FLOW, no consent screen ever (Google Drive):
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
| `agent/` | The agent that runs in the container: `agent.py` (entrypoint + the `SOURCES` table, the extension point, + the three tools), `github_tool.py` (`search_github`, `fetch_document`, `list_sources` — the consent flow), `gdrive_tool.py` (the same three over Drive — the OBO flow), `identity_claims.py` (re-verifies the JWT signature and gates which email may be impersonated; the OBO flow's whole safety argument) |
| `client/` | CLI test client: `login.py` (Auth0 device flow), `ask.py` (invoke the runtime), `consent.py` (completes 3-legged OAuth — owns the return URL both the runtime and the allowlist derive from) |
| `infra/` | CloudFormation templates, architecture docs, diagrams + their renderers |
| `scripts/` | Deploy path: `deploy.py` (orchestrator) → `cfn_build.py` (image) → `apply_tags.py`, plus `register_consent_url.py` (allowlists the consent return URL — CFN can't) and `verify_drive_delegation.py` (proves the Google admin grant works with no Ripple code in the path). `create_github_provider.py` is a standalone leftover — CloudFormation creates the provider now, so the Quickstart never calls it |
| `tests/` | `test_identity_claims.py` — the regression tests on the impersonation gates. Run with `python3 tests/test_identity_claims.py`; no AWS or network needed |
| `docs/` | Setup guides: `SETUP.md` (Auth0 front-door identity), `DATA-SOURCES.md` (onboarding every source — GitHub, Google Drive, and the three target-state MCP sources), `GOOGLE-DRIVE-OKTA-SETUP.md` (the Drive deep dive), `FUTURE-MULTI-AGENT.md` (roadmap) |
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

`infra/architecture-components.png` draws them side by side: the **OBO flow** in
green (steps `1a`–`14a`), the **consent flow** in orange (`1b`–`14b`, plus the
`★` arrow). Steps `1`–`5` are identical in both; they diverge at step `6`.

Each flow also has a sequence diagram to itself, which is where the shape of the
difference is easiest to see: `infra/architecture-obo-flow.png` (17 edges, one
invocation, no consent screen on the canvas) and `infra/architecture-consent-flow.png`
(26 edges, two invocations, because the human step in the middle is one the runtime
will not hold a request open for). Those two diagrams number their own flow from `1`
independently of the components diagram and of each other.

| | OBO flow (green, `…a`) | Consent flow (orange, `…b`) |
|---|---|---|
| Source in this repo | Google Drive / Docs | GitHub |
| What proves the identity | your IdP's signed JWT, re-verified inside the agent, `email` claim taken from it | the same JWT, exchanged for a token the target will accept |
| How the source is called | a service-account assertion with `sub` = that verified email — Google mints a token scoped to **that one user** | the user's **own** OAuth token, held in the AgentCore Identity vault |
| Consent screens | **none, ever** | one per user, per source, once |
| Human actions | **zero** | one — the `★` step, the only human action anywhere on the diagram |
| Who sets it up | a Workspace super-admin, once, for everyone | each user, for themselves |
| Fails when | the token has no verified `email`, or the address is not the Workspace **primary** address | the user declines, or the vault never received the `session_id` |

### End-user experience — OBO flow (Google Drive)

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
and the OBO flow has no equivalent at any step.

### With both sources enabled

Both are searched on every question and results are merged, each citation carrying
its source. A user who has approved GitHub but whose Drive is empty gets a grounded
answer from GitHub alone and exit code `0` — a partial answer from the connected
sources is a real answer. Exit `3` means *nothing* was searched.

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
step 10 adds Google Drive and with it the **OBO flow**, and is optional but is the
only way to demonstrate the no-consent path.

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

### 10. Optional — turn on Google Drive (the OBO flow)

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

`Connected sources: ['gdrive', ...]` and no consent page means the OBO flow is live.
If Drive appears under *"could not be reached"* instead, `ask.py` prints the
delegated-source advice — the cause is the claim or the grant, not anything a user
can authorize away.

### 11. Activate the cost allocation tag (once per account)

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

[`infra/ARCHITECTURE.md`](infra/ARCHITECTURE.md) — four diagrams: components and trust
boundaries, then one sequence diagram per flow with every edge numbered
(`infra/architecture-deploy-flow.png`, `infra/architecture-obo-flow.png`,
`infra/architecture-consent-flow.png`).

`infra/architecture-components.png` is the one to read first. It draws **both flows
at once** — green `1a`–`14a` for OBO, orange `1b`–`14b` plus `★` for consent — with a
step-by-step table per flow derived from the same edge list that draws the arrows, so
a badge and its description cannot disagree. Solid tiles are deployed; dashed ones
are target state. The three flow PNGs then draw one flow each, in sequence form, and
number their own flow independently: a step number from one of them means nothing
without naming which diagram it came from. Every source file carries a
`WHERE THIS SITS IN THE ARCHITECTURE DIAGRAM` comment naming the components and
numbered steps it implements, so you can grep from either direction:

```bash
grep -rn "WHERE THIS SITS IN THE ARCHITECTURE DIAGRAM" agent client scripts infra tests
```

`ARCHITECTURE.md` also
documents the **state model**: the solution is stateless at every layer, which is
deliberate, and the file explains exactly what to change to make it stateful.

Regenerate the PNGs (needs `matplotlib`, run from this directory):

```bash
python3 infra/render_components.py
python3 infra/render_diagrams.py
```

Both refuse to emit a PNG that fails their self-checks.

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
  Activation in Billing is Quickstart step 11.
