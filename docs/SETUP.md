# Ripple — Real Solution Setup Guide

This guide covers the **external accounts** that no code in this repo can create for
you — each one needs a human with an account somewhere. Everything on the
AWS/AgentCore side is automated by `scripts/deploy.py` and the two CloudFormation
templates.

Work through the steps, put the **non-secret** values (Client IDs, tenant IDs, URLs)
in `dev.env`, and keep secrets in your shell only.

> 🔐 **A client secret or API token never belongs in a file in this repo.** `dev.env`
> is for non-secret configuration; secrets are exported in your shell and, where the
> deploy path needs them, stored in Secrets Manager so that only an **ARN** reaches
> configuration.

> **Read Part 1 only.** Part 2 describes an Entra ID / SharePoint data source that was
> replaced by GitHub and is **not implemented**. It is kept as a record of the
> evaluated alternative. The sources that exist are GitHub
> and Google Drive — both covered in [`DATA-SOURCES.md`](DATA-SOURCES.md), which also
> covers the three target-state MCP sources. Drive's full procedure is
> [`GOOGLE-DRIVE-OKTA-SETUP.md`](GOOGLE-DRIVE-OKTA-SETUP.md).

---

## Part 1 — Auth0 / Okta (front-door user identity)

This is the identity layer both flows share: steps `1`–`5` of
`infra/architecture-components.png` are identical in the OBO and consent flows, and
they all happen here. Get this wrong and neither flow works.

Create two things in the tenant:

- an **API** whose identifier is the audience the runtime validates against — e.g.
  `ripple-slack-knowledge-assistant` → `AUTH0_AUDIENCE` in `dev.env`
- a **Native** application for the CLI — its Client ID → `AUTH0_CLI_CLIENT_ID`

Then the configuration below. *Device Code is off by default*, and a tenant that
looks correctly configured will still fail the login without it.

1. **Enable Device Code grant**
   - Applications → your native app → **Settings**
   - Bottom → **Advanced Settings** → **Grant Types**
   - Check ✅ **Device Code** (leave existing ones checked) → **Save**

2. **Authorize the app for the API + enable refresh tokens**
   - Same app → **APIs** tab → toggle your API ON
   - APIs → your API → **Settings** → **Allow Offline Access** ON → Save
   - (Recommended) App → Settings → Refresh Token Rotation: **Allow Refresh Token Rotation** ON

3. **Create 2 test users** (User Management → Users → Create User). These become the
   two people who will see *different* documents:
   - User 1 (e.g. `amir@yourdomain.com`)
   - User 2 (e.g. `dana@yourdomain.com`)

   > **Use a domain you actually own, not a `.test` / `.local` / `.example` domain.**
   > Auth0 is its own user store, so any string is accepted here — but the Google Drive
   > source impersonates users **by email address**, and Google will only accept a
   > domain you have verified ownership of. A reserved-for-testing TLD (RFC 2606) can
   > never be verified, and cannot be added to Google Workspace at all. Picking a
   > throwaway domain now means recreating both users later, because the Okta `email`
   > claim must equal the Workspace **primary** address exactly. See
   > `GOOGLE-DRIVE-OKTA-SETUP.md`.

   Create each user with **Email** set to the same address you created in Workspace,
   and tick **Email verified** (or verify it) — see step 4 below for why that is not
   optional.

4. **Put `email` and `email_verified` in the ACCESS token.** This is the step that
   makes the two identities line up, and the one most likely to be missed: Auth0 puts
   `email` in the *ID* token by default, but the runtime validates the **access**
   token, which by default carries only `sub`. Without this, every Drive request fails
   closed with *"the verified token carries no 'email' claim"*.

   Auth0 Dashboard → **Actions → Library → Build Custom** (trigger:
   *Login / Post Login*), then:

   ```javascript
   exports.onExecutePostLogin = async (event, api) => {
     if (event.authorization) {
       api.accessToken.setCustomClaim('email', event.user.email);
       api.accessToken.setCustomClaim('email_verified', event.user.email_verified);
     }
   };
   ```

   **Deploy**, then drag it into the **Login** flow (Actions → Flows → Login) and
   **Apply**. Both claims are required: `identity_claims.py` refuses to impersonate an
   address the issuer has not affirmed as verified, because an unconfirmed self-signup
   as `ceo@yourdomain.com` would otherwise be a domain-wide read.

   Verify what the token actually carries:

   ```bash
   python3 client/login.py --claims        # expect "email" AND "email_verified": true
   ```

**Record:** the two test-user emails. They must be byte-identical to the Google
Workspace primary addresses, since that string is the impersonation subject. Nothing
about them goes in `dev.env` — the runtime learns each user's email from their token,
never from configuration.

Confirm the identity layer is live — a `user_code` + `verification_uri` means the
device-code grant is working:

```bash
source dev.env
python3 client/login.py --claims
```

---

## Part 2 — Microsoft Entra ID + SharePoint — NOT IMPLEMENTED

> **Do not follow this part.** SharePoint was evaluated as source #1 and replaced by
> GitHub, which needs no corporate admin to demonstrate the same per-user trim. No
> code in this repo calls Microsoft Graph, and no configuration variable below is
> read by anything. It is kept because the mechanism is a close analogue of the
> consent flow that *is* implemented, and because "why not SharePoint" is a fair
> question to ask of a knowledge assistant.
>
> The implemented sources are **GitHub** (consent flow) and **Google Drive** (OBO
> flow), both in [`DATA-SOURCES.md`](DATA-SOURCES.md); Drive's full procedure is
> [`GOOGLE-DRIVE-OKTA-SETUP.md`](GOOGLE-DRIVE-OKTA-SETUP.md).
> `MS_CLIENT_SECRET` and the tenant IDs below are not referenced anywhere.

**Where this sits in the architecture diagram
(`../infra/architecture-components.png`):** Part 1 sets up the `OKTA` tile, which is
the only component both flows depend on identically — it is what makes steps `2`/`3`
(device-code login, signed JWT), `5` (front-door validation against JWKS) and `6a`
(the agent's own re-verification) possible. Part 3's Slack front door is the dashed
`SLACK` tile. Part 2 corresponds to no tile at all, which is the point of the notice
on it.

Real SharePoint retrieval requires a **Microsoft-issued** Graph token, so this would
register one Entra application. AgentCore Identity would use it to mint a **per-user**
Graph token (3-legged OAuth), so retrieval is trimmed to what each user can actually
open — architecturally the same consent flow GitHub uses today.

### 2.1 Register the app
1. https://entra.microsoft.com → **Identity → Applications → App registrations → New registration**
2. Name: `ripple-sharepoint`
3. Supported account types: **Accounts in this organizational directory only** (single tenant)
4. **Redirect URI:** platform **Web**, value:
   `https://<will-provide-after-agentcore-identity-setup>` — leave blank for now; I'll
   give you the exact AgentCore Identity callback URL to paste back.
5. Register.

Record the **Directory (tenant) ID** and **Application (client) ID** (both non-secret)
— which is what a SharePoint source would have been configured with.

### 2.2 Delegated Graph permissions (permission-trimming depends on these)
1. App → **API permissions → Add a permission → Microsoft Graph → Delegated permissions**
2. Add: **`Files.Read.All`**, **`Sites.Read.All`**, **`User.Read`**, **`offline_access`**, **`openid`**, **`profile`**
3. Click **Grant admin consent for <tenant>** (needs admin; you have it).

> These are *delegated* (act-as-the-user) scopes — the token is scoped to the signed-in
> user, so Graph returns only files that user can access. That is the native ACL trim.

### 2.3 Client secret
1. App → **Certificates & secrets → New client secret** → copy the **Value** immediately.
2. In your terminal (NOT chat):
   ```
   export MS_CLIENT_SECRET='paste-the-secret-value-here'
   ```

### 2.4 SharePoint site + test documents
1. Use (or create) a SharePoint site, e.g. `https://<tenant>.sharepoint.com/sites/ripple-knowledge`
2. Upload ~3–4 documents. **Share them to DIFFERENT users** so trimming is visible:
   - Doc A ("2026 Product Roadmap") → share with **both** test users
   - Doc B ("Compensation Planning FY26") → share with **only User 2**
   - Doc C ("Onboarding Guide") → share with **both**
3. Record the site hostname (`<tenant>.sharepoint.com`) and site path
   (`/sites/ripple-knowledge`) — which is where this would have been wired in.

---

## Part 3 — Slack (deferred — production front door)

The production design is **IdP SSO → Slack**, with Slack as the interface rather than
the CLI. It is drawn on `infra/architecture-components.png` as a dashed tile, and it
is deferred for two reasons: it is off the critical path for proving the core
guarantee, and custom SAML SSO into Slack needs a **Business+/Enterprise Grid** plan.
No agent code changes when it is added — Slack replaces the CLI at steps `1`–`4` and
`13`–`14`, and everything between is untouched.

Target front-door flow:
```
User → (IdP SSO) → Slack → Events API → API Gateway → Receiver Lambda
     → map Slack user_id → IdP identity → Ingress AgentCore Gateway → agent
```

Note that Slack appears in the target state **twice** and they are different things:
as the front door above, and as a *data source* to be searched under the user's own
Slack token — which would be another consent-flow source alongside GitHub.

---

## What the code and templates create (no manual step)
- AgentCore **Runtime** agent (Strands + Bedrock) using the AgentCore harness —
  `infra/02-runtime.yaml`
- AgentCore **Memory** (created and injected, deliberately unused; see
  `infra/ARCHITECTURE.md` on the state model)
- AgentCore **Identity** credential provider for GitHub (per-user OAuth, the consent
  flow) — also `infra/02-runtime.yaml`
- The **build plane**: S3 source bucket, CodeBuild project, ECR repository —
  `infra/01-foundation.yaml`
- CLI **client** (device-flow login → sends user JWT → prints cited,
  confidence-scored answer) — `client/`

Two AgentCore **Gateways** (ingress Runtime target; tools MCP target) are drawn as
target state and are **not** deployed. They become worthwhile at roughly four
sources; today the runtime is called directly.
