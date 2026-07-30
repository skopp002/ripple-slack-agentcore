# Data Source Setup — GitHub (no admin needed)

Goal: register ONE OAuth app **you own**, so AgentCore Identity can mint a
**per-user** GitHub token. Each user consents to their OWN account, so GitHub
returns only the repos/code/issues that user can see — that IS the permission trim,
enforced by GitHub, not by us. No corporate admin, no separate login.

Paste me the **non-secret** values (📋). Put **secrets** in your shell only (🔐).

I'll give you the real AgentCore Identity **callback URL** after I create the
credential provider — you'll come back and paste it into the app (Step marked ↩).

---

## GitHub OAuth App

1. <https://github.com> → your avatar → **Settings** → **Developer settings**
   (bottom of left nav) → **OAuth Apps** → **New OAuth App**.
2. Fill:
   - **Application name:** `Ripple Knowledge Assistant`
   - **Homepage URL:** `https://example.com` (placeholder is fine)
   - **Authorization callback URL:** `https://example.com/callback`
     (↩ I'll give you the real AgentCore callback URL to replace this.)
   - **Enable Device Flow:** leave **unchecked** — AgentCore uses the standard
     authorization-code (browser redirect) flow, not device flow.
3. **Register application.**
4. On the app page:
   - 📋 **Client ID** — DONE: `Ov23liOZOLgOrc2wo7CV`
   - Click **Generate a new client secret** → copy the value **once**.
     🔐 in your terminal (NOT chat):
     ```
     export GITHUB_CLIENT_SECRET='paste-value-here'
     ```
5. Scopes are requested at consent time (`repo read:org`) — nothing to configure here.

> `repo` lets Ripple search private repos you can access; `read:org` covers org
> repos. To keep it public-only, tell me and I'll drop to `public_repo`.

---

## Seed test content (makes the permission-trim demo visible)

Use two GitHub identities that can see DIFFERENT things:

- A **private repo** (e.g. `ripple-knowledge`) with a few markdown docs, e.g.
  `roadmap.md`, `compensation-fy26.md`, `onboarding.md`.
- Add your **second GitHub account** as a collaborator on this repo, but keep a
  SECOND private repo (e.g. `ripple-comp`) collaborator = account 1 only.
- Demo:
  - Account 1 asks about comp → **HIGH** with a citation to `ripple-comp`.
  - Account 2 asks the same → **LOW** / "not found" (it can't see that repo).

(If you only have one account, a simpler variant: one private repo you own vs. a
public repo — the trim is less dramatic but still demonstrates per-user access.)

---

## (after I set up AgentCore Identity) fix the callback URL  ↩
I'll create the GitHub OAuth2 credential provider in AgentCore Identity and give you
its **callback URL**. You'll then:
- GitHub OAuth App → **Authorization callback URL** → replace the placeholder → **Update application**.

---

## Summary of what to paste me (all non-secret)
- 📋 GitHub **Client ID** — DONE (`Ov23liOZOLgOrc2wo7CV`)
- 📋 the two test account GitHub logins (which repos each can see)

And in your shell only:
- 🔐 `export GITHUB_CLIENT_SECRET='...'`
