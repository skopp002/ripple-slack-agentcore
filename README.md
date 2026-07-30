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
the implementation is here. The parent repository is a Product Manager discovery
workspace (requirements, PR/FAQ, target-state diagrams) and is not required to
build or run any of this.

```
Auth0 device-code login  ->  user JWT
   -> AgentCore Runtime (CUSTOM_JWT authorizer validates it)
      -> AgentCore Identity mints a per-user token PER SOURCE (3-legged OAuth)
         -> Strands agent (Claude on Bedrock) + three tools that each fan out
            across every source this user has connected: search (content),
            read (one document's full text), list (what they can access)
            -> answer + [n] citations + HIGH/MEDIUM/LOW
               (+ a consent URL per source not yet connected)
```

## What's here

| Path | What it is |
|---|---|
| `agent/` | The agent that runs in the container: `agent.py` (entrypoint + the `SOURCES` table, the extension point, + the three tools), `github_tool.py` (`search_github`, `fetch_document`, `list_sources`) |
| `client/` | CLI test client: `login.py` (Auth0 device flow), `ask.py` (invoke the runtime), `consent.py` (completes 3-legged OAuth — owns the return URL both the runtime and the allowlist derive from) |
| `infra/` | CloudFormation templates, architecture docs, diagrams + their renderers |
| `scripts/` | Deploy path: `deploy.py` (orchestrator) → `cfn_build.py` (image) → `apply_tags.py`, plus `register_consent_url.py` (allowlists the consent return URL — CFN can't). `create_github_provider.py` is a standalone leftover — CloudFormation creates the provider now, so the Quickstart never calls it |
| `docs/` | External-account setup (Auth0, GitHub) and the multi-agent roadmap |
| `Dockerfile` | The container image. **Committed source**, not generated — `cfn_build.py` needs it |
| `requirements.txt` | Container dependencies only |
| `requirements-dev.txt` | Your machine: deploy + client + diagram rendering |
| `dev.env.example` | Config template. Copy to `dev.env` (gitignored) and fill in |

Not in git, by design: `dev.env` (your real config), `client/.token.json` (cached
JWT), `.venv*`, and `.bedrock_agentcore.yaml` (leftover state from the removed
starter toolkit).

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
   found". Walkthrough: [`docs/DATA-SOURCES-SETUP.md`](docs/DATA-SOURCES-SETUP.md).
5. **Python 3.12+**. Docker is *not* required — the image is built by CodeBuild,
   in AWS, on ARM64.
6. **A browser on this machine.** `client/login.py` runs the Auth0 device flow and
   opens a URL. Over a bare SSH session you must copy the URL out by hand.

## Quickstart

Empty AWS account to first answered question. Steps 1–8 and 10 are one-time; step 9
is the loop you actually use.

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

### 10. Activate the cost allocation tag (once per account)

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

[`infra/ARCHITECTURE.md`](infra/ARCHITECTURE.md) — three diagrams: components and
trust boundaries, deploy-time flow (every edge numbered), request-time flow. Also
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
  Activation in Billing is Quickstart step 10.
