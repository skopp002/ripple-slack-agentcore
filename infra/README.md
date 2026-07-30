# Ripple — CloudFormation Infrastructure

Declarative replacement for the `bedrock-agentcore-starter-toolkit` deploy path,
which is slated for deprecation and no longer used anywhere in this repo.
`scripts/deploy.py` now sequences these templates instead of calling the toolkit.
Every resource carries the cost allocation tag
`project_name=ripple_slack_assistant` **natively at creation** — no retrofit pass.

> **Architecture diagrams:** see [`ARCHITECTURE.md`](ARCHITECTURE.md) — three views.
>
> | PNG | What it shows | Regenerate |
> |---|---|---|
> | [`architecture-components.png`](architecture-components.png) | the components and trust boundaries, in official AWS icons — comparable side by side with the customer's own current-state diagram | `python3 infra/render_components.py` |
> | [`architecture-a-deploy.png`](architecture-a-deploy.png) | deploy-time flow, every edge numbered | `python3 infra/render_diagrams.py` |
> | [`architecture-b-request.png`](architecture-b-request.png) | request-time flow, every edge numbered | `python3 infra/render_diagrams.py` |
>
> Run from `solution/`. Both renderers are matplotlib-only — no Node, no Docker,
> nothing leaves the machine — and refuse to emit a PNG that fails their checks.

## Coverage: is everything available in CloudFormation?

Yes, for this solution. Verified present in `us-west-2` (all `FULLY_MUTABLE`,
all with `tagOnCreate: true`):

| Need | CFN type | Where |
|---|---|---|
| Runtime | `AWS::BedrockAgentCore::Runtime` | `02-runtime.yaml` |
| Runtime endpoint | `AWS::BedrockAgentCore::RuntimeEndpoint` | `02-runtime.yaml` |
| Memory | `AWS::BedrockAgentCore::Memory` | `02-runtime.yaml` |
| GitHub OAuth provider | `AWS::BedrockAgentCore::OAuth2CredentialProvider` | `02-runtime.yaml` |
| Runtime role | `AWS::IAM::Role` | `02-runtime.yaml` |
| ECR repo | `AWS::ECR::Repository` | `01-foundation.yaml` |
| CodeBuild + role | `AWS::CodeBuild::Project`, `AWS::IAM::Role` | `01-foundation.yaml` |
| Build source bucket | `AWS::S3::Bucket` | `01-foundation.yaml` |
| CodeBuild log group | `AWS::Logs::LogGroup` | `01-foundation.yaml` |

Also available if ever needed: `Gateway`, `GatewayTarget`, `WorkloadIdentity`,
`ApiKeyCredentialProvider`, `Browser`, `CodeInterpreter`, `TokenVault`.

### The one true gap

**CloudFormation cannot build a container image.** `AWS::BedrockAgentCore::Runtime`
requires a `ContainerUri` that already exists in ECR. So deployment is two-stage
with a build in between — `scripts/cfn_build.py` fills that slot:

```
01-foundation.yaml  →  scripts/cfn_build.py  →  02-runtime.yaml (ImageTag=…)
   (build plane)          (source→ECR)              (the agent)
```

This is inherent to CloudFormation, not a gap in AgentCore's coverage.

### Not created by CloudFormation

- **Runtime log group** — the service creates
  `/aws/bedrock-agentcore/runtimes/<runtime-id>-<endpoint>` on first invoke. Its
  name embeds the generated runtime id, so it cannot be declared up front. Run
  `scripts/apply_tags.py` after the first invoke to tag it.
- **Identity secret** — AgentCore Identity creates the Secrets Manager secret
  holding the GitHub client secret when `ClientSecretSource=MANAGED`. Also tagged
  by `apply_tags.py`. Avoid entirely by using `GithubClientSecretArn` (see below).

## Pre-deploy name collisions

**Read this before the first deploy into an account that already ran the toolkit.**

CloudFormation **cannot adopt a pre-existing resource.** If a fixed name the
template declares is already taken, the stack fails with `... already exists` and
rolls back — *after* creating some resources. So `deploy.py` runs a preflight check
first and refuses to start (exit 2) if any fixed name is taken outside a stack:

```
=== preflight: checking for pre-existing names
  ✅ s3 bucket ripple-agentcore-build-sources-<account>-us-west-2 — free
  ⚠️  EXISTS outside a stack: ecr repository bedrock-agentcore-ripple
  ...
```

It only checks **fixed** names. The runtime id and the memory id suffix are
service-generated and cannot collide. A name already inside one of the two stacks
is reported as *already managed*, not a conflict — that is just an update.

### Two ways to resolve

**Option A — a fresh name prefix (non-destructive, recommended for a trial).**
Every colliding name derives from `AgentName`, so changing it sidesteps all of them
at once and leaves the toolkit-deployed runtime running:

```bash
export AGENTCORE_AGENT_NAME=ripple2   # also pass AgentName=ripple2 if deploying by hand
```

Costs a second ECR repo and CodeBuild project. Note `apply_tags.py` derives names
the same way, so it must see the same value.

**Option B — delete the toolkit-created resources (clean cutover).**
This **deletes the live `ripple` runtime**; it will not answer requests until the
stack recreates it. Do it when you are ready to cut over, not to try things out.

```bash
# The runtime, and the endpoint under it
aws bedrock-agentcore-control delete-agent-runtime \
  --agent-runtime-id ripple-bd2HGJEprG --region "$AWS_REGION"

# Build plane. --force also removes the images; they are rebuilt on next deploy.
aws ecr delete-repository --repository-name bedrock-agentcore-ripple \
  --force --region "$AWS_REGION"
aws codebuild delete-project --name bedrock-agentcore-ripple-builder \
  --region "$AWS_REGION"
aws logs delete-log-group \
  --log-group-name /aws/codebuild/bedrock-agentcore-ripple-builder \
  --region "$AWS_REGION"
```

The ECR repo and CodeBuild project already carry
`project_name=ripple_slack_assistant` — they are this project's own earlier
resources, not another team's. Verify before deleting anything:

```bash
aws ecr list-tags-for-resource --region "$AWS_REGION" \
  --resource-arn arn:aws:ecr:$AWS_REGION:<account-id>:repository/bedrock-agentcore-ripple
```

> ⚠️ **The credential provider is the expensive one to delete.** Replacing
> `ripple-github` issues a **new callback URL**, so GitHub auth breaks until the
> new URL is pasted into the OAuth App (see the section below). Under Option A
> the template creates `ripple2-github`, which is a new provider and therefore
> *also* a new callback URL. Either way, budget for that manual step. Keeping the
> existing provider and importing it is not possible — `Name` is `createOnly`.

`--skip-preflight` bypasses the check. `--image-only` skips it automatically, since
updating an existing stack is supposed to find its own names.

## Deploy

```bash
source dev.env
python3 scripts/deploy.py            # preflight -> foundation -> build -> runtime -> tags
python3 scripts/deploy.py --dry-run  # print the commands without running them
python3 scripts/deploy.py --image-only   # code change only: rebuild + roll image
```

`deploy.py` just sequences the steps below and threads the image tag between them
— it creates nothing itself and calls no deprecated toolkit. Do the one-time
GitHub client secret setup first (§ "The GitHub client secret") and export
`GITHUB_CLIENT_SECRET_ARN`, so the secret never enters the template.

Afterwards it prints the three `export` lines for `dev.env` and the GitHub
callback URL to check.

### The same thing by hand

Equally supported — `deploy.py` is convenience, not a required wrapper:

```bash
source dev.env

# 0. ONE-TIME: create the GitHub client secret in Secrets Manager.
#    Full walkthrough under "The GitHub client secret" below. Skip if it exists.

# 1. Build plane
aws cloudformation deploy \
  --stack-name ripple-foundation \
  --template-file infra/01-foundation.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --region "$AWS_REGION" \
  --tags project_name=ripple_slack_assistant

# 2. Build + push the image; prints IMAGE_TAG=<tag> on the last line
python3 scripts/cfn_build.py

# 3. The agent
aws cloudformation deploy \
  --stack-name ripple-runtime \
  --template-file infra/02-runtime.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --region "$AWS_REGION" \
  --tags project_name=ripple_slack_assistant \
  --parameter-overrides \
      ImageTag=<tag-from-step-2> \
      Auth0Domain="$AUTH0_DOMAIN" \
      Auth0Audience="$AUTH0_AUDIENCE" \
      BedrockModelId="$BEDROCK_MODEL_ID" \
      GithubClientId="$GITHUB_CLIENT_ID" \
      GithubClientSecretArn=<secret-arn-from-step-0>

# 4. Read outputs into dev.env (RuntimeArn, EndpointName, MemoryId)
aws cloudformation describe-stacks --stack-name ripple-runtime \
  --region "$AWS_REGION" --query 'Stacks[0].Outputs' --output table

# 5. Paste the GithubCallbackUrl output into the GitHub OAuth App (see warning below)

# 6. Tag the two service-created resources CFN cannot declare
python3 scripts/apply_tags.py
```

`--tags` on the stack propagates to every resource that supports tagging, *in
addition to* the explicit per-resource tags. Belt and braces: stack-level tags
cover resources added later, explicit tags survive a stack-tag change.

### Redeploying a new image

Steps 2 and 3 only. `ImageTag` is not create-only, so the runtime updates in
place — no replacement, and the GitHub callback URL is unaffected.

## The GitHub client secret

Two options; the first is strongly preferred.

### Creating `GithubClientSecretArn` (EXTERNAL) — recommended

You create the secret once, outside CloudFormation; the template only references
it. The secret value never enters the template, the parameter list, or stack
history, and rotating it later needs no redeploy.

**Step 1 — get the client secret from GitHub.** GitHub → *Settings* →
*Developer settings* → *OAuth Apps* → your app (client ID `Ov23liOZOLgOrc2wo7CV`) →
**Generate a new client secret**. It is shown **once**; copy it immediately.

**Step 2 — put it in your shell, not in a file.** Note the leading space, which
keeps it out of shell history in `zsh`/`bash` (`HIST_IGNORE_SPACE`):

```bash
 export GITHUB_CLIENT_SECRET='paste-the-value-here'
```

Never put this in `dev.env` — that file is for non-secret config only.

**Step 3 — create the secret.** The value must be **JSON**, because the template
references a specific key inside it (`JsonKey` is required by the schema). Built
with `--secret-string` from the env var so the literal never appears in your
command line or history:

```bash
aws secretsmanager create-secret \
  --name ripple/github-oauth \
  --description "GitHub OAuth App client secret for the Ripple assistant" \
  --secret-string "{\"client_secret\":\"$GITHUB_CLIENT_SECRET\"}" \
  --tags Key=project_name,Value=ripple_slack_assistant \
  --region "$AWS_REGION" \
  --query ARN --output text
```

That prints the ARN — this is your `GithubClientSecretArn`:

```
arn:aws:secretsmanager:us-west-2:<account-id>:secret:ripple/github-oauth-AbCdEf
```

The random 6-character suffix is added by AWS. Always pass the **full ARN**, not
the bare name.

**Step 4 — verify the shape without printing the secret.** Confirms the JSON
parses and the key name matches:

```bash
aws secretsmanager get-secret-value \
  --secret-id ripple/github-oauth \
  --region "$AWS_REGION" \
  --query SecretString --output text | python3 -c 'import json,sys; print("keys:", list(json.load(sys.stdin)))'
# expected ->  keys: ['client_secret']
```

**Step 5 — pass it to the stack.** `client_secret` matches the
`GithubClientSecretJsonKey` default, so that parameter can be omitted:

```bash
--parameter-overrides GithubClientSecretArn=<the-ARN-from-step-3>
```

**Rotating the secret later** — update the value in place; the ARN and the
credential provider are unchanged, so no redeploy and **no new callback URL**:

```bash
 export GITHUB_CLIENT_SECRET='the-new-value'
aws secretsmanager put-secret-value \
  --secret-id ripple/github-oauth \
  --secret-string "{\"client_secret\":\"$GITHUB_CLIENT_SECRET\"}" \
  --region "$AWS_REGION"
```

Notes:
- Use a **different secret name per environment** (e.g. `ripple/github-oauth-prod`);
  one secret shared across stacks reintroduces the ambiguous-ownership problem
  described under *Differences from the toolkit-deployed stack*.
- If the secret is encrypted with a **customer-managed KMS key**, its key policy
  must allow `kms:Decrypt` for `bedrock-agentcore.amazonaws.com`. The default
  `aws/secretsmanager` key needs no extra setup.
- Deleting the stack does **not** delete this secret — it is outside CFN by design.
  Remove it explicitly with `delete-secret` when decommissioning.

### `GithubClientSecret` (MANAGED) — fallback

Passed as a `NoEcho` parameter; AgentCore Identity then creates and owns its own
Secrets Manager secret. `NoEcho` masks console display, but the value is still
submitted to CloudFormation. Use only if you cannot pre-create a secret.

## ⚠️ Replacing the credential provider changes the GitHub callback URL

Verified empirically: each `OAuth2CredentialProvider` is issued a **new** callback
UUID.

```
existing: …/identities/oauth2/callback/b09c9a15-e507-401b-8b9d-ea7adfdb7a06
new one:  …/identities/oauth2/callback/6c7057ff-1188-4258-86a8-447324b52a21
```

`Name` and `CredentialProviderVendor` are **createOnly** — changing either
replaces the resource and invalidates the URL. GitHub auth stays broken until the
new URL is pasted into the GitHub OAuth App's *Authorization callback URL*.

After any deploy that touched the provider:

```bash
aws cloudformation describe-stacks --stack-name ripple-runtime \
  --region "$AWS_REGION" \
  --query 'Stacks[0].Outputs[?OutputKey==`GithubCallbackUrl`].OutputValue' --output text
```

## Tag shapes differ between AgentCore resources

Not a typo in the templates — the schemas genuinely disagree:

| Resource | `Tags` shape |
|---|---|
| `Runtime`, `RuntimeEndpoint`, `Memory` | **map** — `{project_name: value}` |
| `OAuth2CredentialProvider` | **list** — `[{Key: …, Value: …}]` |
| `IAM::Role`, `ECR`, `S3`, `CodeBuild`, `Logs` | **list** — `[{Key: …, Value: …}]` |

`ProtocolConfiguration` likewise differs from boto3: a bare string (`HTTP`) in
CFN, a nested object (`{serverProtocol: HTTP}`) in the API.

## Differences from the toolkit-deployed stack

The `bedrock-agentcore-starter-toolkit` is **slated for deprecation and is no
longer used anywhere in this repo.** The table below is now a record of what the
migration changed, not a live choice between two paths.

What replaced what:

| Toolkit thing | Now |
|---|---|
| `Runtime.configure()` / `Runtime.launch()` | `infra/01-foundation.yaml` + `infra/02-runtime.yaml` |
| toolkit-generated `Dockerfile` (gitignored) | committed, owned `Dockerfile` |
| `bedrock-agentcore-starter-toolkit` in `requirements.txt` | removed — it shipped a deploy tool into every image |
| `.bedrock_agentcore.yaml` deploy state | CloudFormation stack state |
| pre-existing hand-made IAM roles via env vars | roles declared by the templates |

Still used, and **not** deprecated: the `bedrock-agentcore` **SDK**, which the
agent imports at runtime (`BedrockAgentCoreApp`, `@requires_access_token`). It is
a separate package from the starter toolkit and unaffected by its deprecation.

One imperative step remains, and cannot be removed: CloudFormation cannot build a
container image, and `AWS::BedrockAgentCore::Runtime` requires a `ContainerUri`
that already exists. `scripts/cfn_build.py` fills that gap — but it *creates* no
resources, it only puts an image into a repo the stack declares.

Intentional improvements from the migration, not drift:

| | Toolkit | CloudFormation |
|---|---|---|
| Cost tags | retrofitted by `apply_tags.py` | native at creation |
| Runtime role | `AgentCoreRuntimeRole`, shared with unrelated agents | `Ripple<name>RuntimeRole`, project-scoped |
| Memory IAM scope | `memory/*` | this stack's memory ARN only |
| Source bucket | account-shared `bedrock-agentcore-codebuild-sources-*` | dedicated, taggable, TLS-only, versioned |
| Buildspec | literal image tag baked in per build | parameterised via `IMAGE_TAG` |
| ECR | mutable tags, unbounded | immutable tags, scan-on-push, keep last 10 |
| Endpoint | `DEFAULT` | `live` |
| Container definition | generated + gitignored, so a fresh clone couldn't build | committed source, allowlist `COPY`, no baked-in region |
| Deploy state | `.bedrock_agentcore.yaml` (machine-local, absolute paths) | stack state in AWS |

The old shared `AgentCoreRuntimeRole` and account-shared source bucket are
deliberately left alone — they belong to other agents too.

### Removing the toolkit entirely

Nothing imports it any more, so it can go:

```bash
pip uninstall bedrock-agentcore-starter-toolkit    # in .venv and .venv-deploy
rm solution/.bedrock_agentcore.yaml                # machine-local deploy state
```

Do **not** uninstall `bedrock-agentcore` — that is the SDK the agent needs.

`.bedrock_agentcore.yaml` is still gitignored and safe to delete: it holds one
machine's absolute paths, an account id, and a runtime id from a single toolkit
deploy. Nothing in the CloudFormation path reads it.

### Why the shared role couldn't be tagged, and why CFN fixes it

This is the one place where CloudFormation buys a genuine correctness improvement
rather than just tidier code, so it's worth spelling out.

**The limitation.** A cost allocation tag answers "which project should this cost
be billed to?" — so it only makes sense on a resource owned by exactly one
project. `AgentCoreRuntimeRole` was created by hand and then reused, and is
currently the execution role for **four** runtimes (verified via
`get-agent-runtime` on every runtime in the account):

```
web_research_agent-xjnEcPANov
ripple-bd2HGJEprG          ← ours
researcher_agent-2TWu6XAvet
kb_agent-bfXISq6FeG
```

Tagging it `project_name=ripple_slack_assistant` would assert that the other
three agents' activity belongs to Ripple. That's not a cosmetic inaccuracy —
it corrupts the cost report the tag exists to produce. So `apply_tags.py`
deliberately skips it and prints why. The same reasoning applies to the
account-shared CodeBuild source bucket: S3 tags are bucket-level, and Ripple only
owns the `ripple/` prefix within it.

**The root cause** is that resources were created imperatively, one CLI call at a
time, with reuse as the path of least resistance. Nothing recorded which project
owned what, so ownership became ambiguous — and an ambiguously-owned resource
cannot be truthfully tagged.

**Why CFN removes the limitation.** A stack *is* an ownership boundary. Each
template declares its own `Ripple<name>RuntimeRole`, so the role has exactly one
owner by construction, and the tag is simply true. Ownership stops being a
convention someone has to remember and becomes a property of the deployment.

The same reasoning tightens IAM. The hand-made role grants Memory data-plane
access across `memory/*`, which today resolves to five memories — four owned by
unrelated projects:

```
CustomerSupportMemory-p6Qj6a3iiw
RippleKnowledgeMemory-fQgBR2CbaB   ← ours
TravelAgent_STM_20250720201351-YJ07p665j3
TravelAgent_STM_20250720202838-jFVvlH6wx9
TravelAgent_STM_20251021100704-C6cKWs4Gpb
```

The wildcard was necessary imperatively: the role had to be written before the
memory existed, so its ARN wasn't yet known. In CFN, `!GetAtt Memory.MemoryArn`
resolves during deploy — the dependency graph supplies the exact ARN, so
least-privilege costs nothing to express. Given that this solution's core
guarantee is per-user permission trimming, a runtime that could read four other
projects' conversation stores is worth closing on its own merits.

The general pattern: **cost attribution and least-privilege both require knowing
who owns a resource, and imperative creation doesn't record that. Declarative
creation does.**

## Validation

Templates are checked against the **live CloudFormation registry schemas**
(`describe-type`), which is stricter than `validate-template` alone — this is how
the `ProtocolConfiguration` shape mismatch was caught before any deploy. All 11
resources across both templates conform.
