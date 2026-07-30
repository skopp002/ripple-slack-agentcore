# Ripple — Real Solution Setup Guide

This guide covers the **external accounts** I cannot create for you. Everything on
the AWS/AgentCore side I automate. Do these steps, paste me the **non-secret**
values (Client IDs, tenant IDs, URLs), and export secrets in your shell only.

> 🔐 **Never paste a client secret or API token into chat.** For any secret, I'll
> tell you the exact `export VAR=...` command to run in your terminal.

---

## Part 1 — Auth0 (front-door user identity) — ALMOST DONE

You already created:
- API `ripple-slack-knowledge-assistant` (audience) ✅
- Native app `ripple-slack-knowledge-assistant-cli`, Client ID
  `wp2SXV9fA9YlABBzlxtYh5OyBKbfwf3D` ✅

**Two toggles remain** (my live test showed Device Code is currently disabled):

1. **Enable Device Code grant**
   - Applications → `ripple-slack-knowledge-assistant-cli` → **Settings**
   - Bottom → **Advanced Settings** → **Grant Types**
   - Check ✅ **Device Code** (leave existing ones checked) → **Save**

2. **Authorize the app for the API + enable refresh tokens**
   - Same app → **APIs** tab → toggle **`ripple-slack-knowledge-assistant`** ON
   - APIs → `ripple-slack-knowledge-assistant` → **Settings** → **Allow Offline Access** ON → Save
   - (Recommended) App → Settings → Refresh Token Rotation: **Allow Refresh Token Rotation** ON

3. **Create 2 test users** (User Management → Users → Create User). These become the
   two people who will see *different* SharePoint documents:
   - User 1 (e.g. `amir@ripple.test`)
   - User 2 (e.g. `dana@ripple.test`)

**Paste me:** the two test-user emails. (I already have domain, audience, client ID.)

When done, I'll re-run the device-code test — a `user_code` + `verification_uri`
means the identity layer is live.

---

## Part 2 — Microsoft Entra ID + SharePoint (the data source)

Real SharePoint retrieval requires a **Microsoft-issued** Graph token, so we register
one Entra application. AgentCore Identity will use it to mint a **per-user** Graph
token (3-legged OAuth), so retrieval is trimmed to what each user can actually open.

### 2.1 Register the app
1. https://entra.microsoft.com → **Identity → Applications → App registrations → New registration**
2. Name: `ripple-sharepoint`
3. Supported account types: **Accounts in this organizational directory only** (single tenant)
4. **Redirect URI:** platform **Web**, value:
   `https://<will-provide-after-agentcore-identity-setup>` — leave blank for now; I'll
   give you the exact AgentCore Identity callback URL to paste back.
5. Register.

**Paste me:** **Directory (tenant) ID** and **Application (client) ID** (both non-secret).

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
3. **Paste me:** site hostname (`<tenant>.sharepoint.com`) and site path (`/sites/ripple-knowledge`).

---

## Part 3 — Slack (deferred — production front door)

Your production design: **Auth0 SSO → Slack**, Slack as the interface (Okta-SSO.png route).
We defer wiring it because (a) it's off the critical path for proving the core guarantee,
and (b) custom SAML SSO into Slack needs a **Business+/Enterprise Grid** plan (test
workspace is Free/Pro). No agent code changes when we add it later.

Documented target front-door flow:
```
User → (Auth0 SSO) → Slack → Events API → API Gateway → Receiver Lambda
     → map Slack user_id → Auth0 identity → Ingress AgentCore Gateway → agent
```

---

## What I build (no action from you)
- AgentCore **Runtime** agent (Strands + Bedrock Opus 4.8) using the AgentCore harness
- AgentCore **Memory** (per-user, Auth0-identity partitioned)
- AgentCore **Identity** credential provider for Microsoft (per-user Graph OBO)
- SharePoint **MCP** tool (calls Graph under the user's token)
- Two AgentCore **Gateways** (ingress Runtime target; tools MCP target)
- CLI **client** (device-flow login → sends user JWT → prints cited, confidence-scored answer)
