# Data Source Setup — Google Drive / Docs, with Okta as the identity provider

Goal: Ripple answers from Drive and Docs content, **trimmed to what the calling user
can see**, with **no consent screen ever shown to that user**.

This is the **OBO flow** — green, steps `1a`–`14a` on
`infra/architecture-components.png`. It is the flow with **no human action at any
step**, which is the whole reason it exists alongside the GitHub consent flow.

Values marked 📋 are non-secret and end up in `dev.env`. Values marked 🔐 are
credentials: they go to Secrets Manager, and only an **ARN** reaches configuration.

---

## Read this first: "Okta as the IdP for Drive" is TWO separate things

Conflating them is the single most common way this setup fails. They are independent
and only one of them is required to make Ripple work.

| | What it controls | Needed for Ripple? |
|---|---|---|
| **A. Okta SSO into Google Workspace** (SAML) | Where the user types their password when they open Drive **in a browser** | **No** — but it's what makes the demo story "one identity" |
| **B. Domain-wide delegation** (service account) | Whether an application can call the **Drive API as a user** | **Yes — this is the load-bearing part** |

**Why A is not enough on its own.** Setting up Okta SSO does not make Okta an issuer
of Drive tokens. Drive is guarded by Google, and only `oauth2.googleapis.com` issues
tokens Drive will accept. Google's `jwt-bearer` grant requires the assertion be signed
by a **Google service-account private key** (`iss` = the service account); an
Okta-signed assertion is rejected. Workforce Identity Federation *does* accept Okta via
RFC 8693, but it returns a Google **Cloud** token for IAM-governed resources — Workspace
APIs like Drive and Gmail are out of scope.

AgentCore's own API agrees: `googleOauth2ProviderConfig` accepts only `clientId` /
`clientSecret` / `clientSecretConfig` / `clientSecretSource` — there is no
`onBehalfOfTokenExchangeConfig` on it, unlike `customOauth2ProviderConfig`. The built-in
Google provider is consent-only by construction.

**So identity propagates in two hops, by two different mechanisms:**

```
Okta JWT  --(validated by CUSTOM_JWT: signature, iss, aud)-->  email claim
   |
   +--(service-account assertion with sub=<that email>)-->  Google
                                                              |
                                    Drive token scoped to THAT user  -->  Drive ACLs trim
```

Hop 1 is cryptographic. Hop 2 is admin-delegated impersonation. No single credential
spans both authorities, because Okta and Google are each authoritative over different
things: Okta over who the user is, Google over what that user may read in Drive. The
two hops are therefore two mechanisms, and describing them as one would misstate where
each authorization decision is actually made.

---

## "Do I need a developer account and a client ID?"

**No to both — and reaching for a client ID is the most likely wrong turn here.**

| | Needed? | Why |
|---|---|---|
| A Google **developer account** | **No** | There is no such thing to register for. A Google Cloud project is created from the console with the account you already have. |
| An **OAuth client ID + secret** | **No** | That is the *consent* path — it exists to show a user an "allow this app" screen. This design has no consent screen, so it has no OAuth client. |
| A **service account** + JSON key | **Yes** | This is the credential. It authenticates as itself and impersonates users by name. |
| A **numeric client ID** | **Yes — but a different one** | Every service account has a numeric ID (~21 digits) used *only* to identify it in the delegation grant. It is not an OAuth client ID and has no secret. |

The confusing part is that Google calls both things a "client ID". They are not
interchangeable:

- **OAuth client ID** — looks like `1234-abc.apps.googleusercontent.com`, comes with a
  client *secret*, used with a redirect URI and a consent screen. **Not used here.**
- **Service-account client ID** — a bare number like `109876543210987654321`, has no
  secret, and is what you paste into *Manage Domain Wide Delegation*. **This one.**

If you find yourself on the **OAuth consent screen** configuration page, or being asked
for a redirect URI or an authorized JavaScript origin, you are on the consent path —
back out. Nothing in this setup needs those. (The consent path is the *extension* in
this project, and only GitHub uses it.)

What you do need an account for: **a Google Cloud project** (any Google account can
create one, no developer program, no fee) and **super-admin on a Workspace domain** so
you can authorize the delegation. The second is the real gate — see below.

---

## What you must have before starting

- [ ] A **Google Workspace** domain (not a personal `@gmail.com` account) and
      **super-admin** on it. Domain-wide delegation does not exist for consumer
      accounts — this is a hard blocker, not a preference.
- [ ] A **Google Cloud project** (any; it only holds the service account).
- [ ] Your Okta org, already issuing the JWT Ripple validates today.
- [ ] Two Workspace users, so the ACL trim is visible (see *Seed test content*).

If you do not have a Workspace domain, a trial on a domain you own is enough — the
delegation mechanism is identical.

### Part A0. Getting a Workspace domain, if you do not have one

> **The domain must be one you own and can prove you own.** Google requires DNS
> verification before it will let a domain into Workspace, so reserved-for-testing
> names — `.test`, `.local`, `.example`, `.invalid` (RFC 2606) — cannot be used at all.
> They resolve nowhere, cannot be registered, and cannot be verified. This is the one
> prerequisite with no workaround: domain-wide delegation is a Workspace feature, and
> Workspace requires a verified domain.

1. **Register a real domain** if you have none — any registrar, roughly $10–15/year.
   Google Domains has closed; Squarespace Domains, Cloudflare, and Namecheap all work.
   Anything you control DNS for is fine (`ripple-demo.com`, `example-corp.io`, a
   subdomain of a domain you already own).
2. **Start Workspace**: [workspace.google.com](https://workspace.google.com) → *Get
   started*. Business Starter is enough — a 14-day trial covers a demo. You will be
   asked for the domain from step 1 and for a first admin user; that account becomes
   **super-admin**, which §B4 needs.
3. **Verify the domain** by adding the `TXT` record Google gives you at your registrar's
   DNS. Propagation is usually minutes. Nothing else in this guide works until this
   completes.
4. **Add the two test users**: Admin console → **Directory → Users → Add new user**.
   Create both, e.g. `amir@yourdomain.com` and `dana@yourdomain.com`. Sign in as each
   once to accept the terms and initialize their Drive — a user who has never signed in
   has no Drive to read, which shows up later as an empty file list rather than an
   error.
5. **Record the two primary addresses.** The `email` claim in the Okta token must equal
   the Workspace **primary** address exactly. Aliases do not work: Google resolves the
   impersonation `sub` against the primary address, so an alias yields
   `unauthorized_client` even when everything else is correct.

The domain you register here is what goes in `dev.env` as `ALLOWED_EMAIL_DOMAINS`, and
the same domain must be the one your Okta users' `email` claims are on.

---

## Part B (do this first — it's what Ripple actually needs)

### B1. Enable the APIs

Google Cloud console → **APIs & Services → Library** → enable:

- **Google Drive API** — required.
- **Google Docs API** — only if you want structured Docs content (see *Google Docs*
  below; Drive export alone often suffices). Enabling it needs no extra delegation
  scope: `documents.get` accepts `drive.readonly`.

### B2. Create the service account

1. **IAM & Admin → Service Accounts → Create service account**.
2. Name it e.g. `ripple-drive-reader`. Click **Done**.
3. **Grant it no IAM roles.** It needs none — its power comes from the Workspace
   delegation grant, not from Google Cloud IAM. Adding project roles only widens
   blast radius for no benefit.
4. Open the service account → **Show advanced settings** → copy the numeric
   **Client ID** (a ~21-digit number).
   📋 keep this Client ID to hand — B4 needs it, and it is not a secret.

### B3. Create the key and put it in Secrets Manager

> **If "Create new key" is greyed out or errors, this is expected — not a bug.** The
> `iam.disableServiceAccountKeyCreation` org policy constraint **blocks key creation,
> and is enforced by default for any Google Cloud organization created on or after
> 2024-05-03**. To proceed, a policy administrator must set a **project-level exception**
> for the project holding this service account (IAM & Admin → **Organization policies**
> → that constraint → *Manage policy* → add a rule for this project that does not
> enforce it). Scope the exception to the one project; the org-wide default should stay.
>
> Google's reason for the default is the same reason step 3 below exists: a downloadable
> key is a **long-lived credential** with no expiry and no revocation short of deleting
> it, and here it is a *domain-wide* one. That is why the key goes straight into Secrets
> Manager and the local copy is shredded — you are accepting a risk Google turns off by
> default, so handle it accordingly.

1. Service account → **Keys → Add key → Create new key → JSON → Create**.
2. 🔐 The file downloads once. Put it straight into Secrets Manager — never into
   `dev.env`, the repo, or chat:

   ```bash
   aws secretsmanager create-secret \
     --name ripple/google-drive-sa \
     --secret-string "file://$HOME/Downloads/<the-key>.json" \
     --region "$AWS_REGION" \
     --tags Key=project_name,Value=ripple_slack_assistant
   ```

3. Then **delete the local file** — it is a domain-wide credential sitting in
   `~/Downloads`:

   ```bash
   rm -P "$HOME/Downloads/<the-key>.json"
   ```

4. 📋 the command prints the secret's **ARN** — that is what goes in `dev.env` as
   `GOOGLE_SA_SECRET_ARN`. An ARN is a pointer, not a credential.

This mirrors how `GITHUB_CLIENT_SECRET_ARN` works: only the ARN reaches
CloudFormation, so the credential never enters a template or stack history.

### B4. Grant domain-wide delegation (super-admin, once)

Google **Admin** console → **Security → Access and data control → API controls** →
**Manage Domain Wide Delegation** → **Add new**:

- **Client ID:** the numeric ID from B2 — *not* the service account email. Pasting the
  email is the most common mistake here and fails with `unauthorized_client`.
- **OAuth scopes:** the field is literally labeled *"OAuth scopes (comma-delimited)"*.
  One scope is all Ripple needs, so there is nothing to delimit:

  ```
  https://www.googleapis.com/auth/drive.readonly
  ```

  `drive.readonly` alone covers Drive search, `files.export`, **and** the Docs API's
  `documents.get` — so there is no second scope to add. If you ever do add one,
  **the separator is a comma, on one line**, e.g.
  `https://www.googleapis.com/auth/drive.readonly,https://www.googleapis.com/auth/documents.readonly`
  — a newline or a space is not a delimiter here, and the resulting grant silently
  fails to match at runtime as `unauthorized_client`.

  > Two places, two delimiters — a reliable trap. The Admin console field is
  > **comma**-delimited; the `scope` claim *inside the signed JWT* is
  > **space**-delimited (Google's own docs: "Note that the list of scopes in the
  > `scope` claim needs to be separated by spaces, not commas"). The client library
  > builds the JWT from `SCOPES`, so you only handle the comma one by hand.

- **Authorize.**

Changes are usually immediate but can take up to 24 hours to propagate.

> **"`drive.readonly` is a *restricted* scope" — yes, and it costs you nothing here.**
> Google does classify it restricted. The verification and CASA-assessment burden that
> normally implies attaches to the **consent** path: apps showing a consent screen to
> users outside your org. Domain-wide delegation shows no consent screen at all, and an
> app used only within your own Workspace organization is exempt from verification. So
> "restricted" is accurate and, for this design, inert.

> ⚠️ **This is an admin-level credential.** That service account can now impersonate
> **any** user in the domain for the granted scopes. Treat loss of the key as a
> domain-wide compromise. Three consequences, all of them already enforced in the
> implementation:
> - scopes stay minimal and **read-only** — `drive.readonly`, never `drive`;
> - the key lives only in Secrets Manager, read by the runtime role and nothing else;
> - the impersonation `sub` is taken **only** from the validated Okta token claim, and
>   the code asserts that rather than trusting a caller-supplied value. If `sub` can
>   ever be influenced by user input, the ACL trim silently becomes a domain-wide read.
>   That is the one bug in this design that is a breach rather than an error.

### B5. Verify delegation works — before any Ripple code exists

This is the step that de-risks everything else. It proves hop 2 independently:

```bash
export GOOGLE_SA_SECRET_ARN='arn:aws:secretsmanager:...:secret:ripple/google-drive-sa-xxxxxx'
python3 scripts/verify_drive_delegation.py user-one@yourdomain.com
python3 scripts/verify_drive_delegation.py user-two@yourdomain.com
```

Each run lists the files **that user** can see. Two users returning different file
lists **is** the ACL trim, demonstrated with no Ripple, no Okta, and no consent screen.

If it fails, the error names the cause — see *Troubleshooting*.

---

## Part A (optional for function, valuable for the demo)

Okta SSO into Workspace is what lets you say "the same Okta user, one login, all the
way through to Drive." Functionally, Ripple only needs the **email claim in the Okta
token to equal the user's Workspace primary address**. If your Okta users already carry
their corporate email, hop 2 works today without any Workspace federation.

To set it up:

1. **Okta Admin → Applications → Browse App Catalog → Google Workspace → Add.**
2. **Sign On** tab: SAML. The wizard shows the exact **IdP entity ID**, **sign-in page
   URL**, and **X.509 certificate** to use — take the values from the wizard rather
   than from any doc, including this one, since they are org-specific.
3. **Provisioning** tab (optional): enable it so Okta users exist in Workspace
   automatically. Not required if the accounts already exist.
4. Google **Admin** console → **Security → Authentication → SSO with third-party IdP**
   → create an SSO profile with the values from step 2 → assign it to the users or org
   units in scope.

   Google's form asks for **five** values, not the three the Okta wizard leads with:
   IdP entity ID, **sign-in page URL**, **sign-out page URL**, **change-password URL**,
   and the verification certificate. The last two are easy to miss — Okta surfaces them
   less prominently, and getting change-password wrong sends users to Google's password
   form for an account whose password Google does not hold.
5. Keep one super-admin **excluded** from SSO. If the SAML config is wrong you will
   otherwise lock yourself out of the console that fixes it.

### The claim that actually matters

Whether or not you do Part A, verify this:

> the `email` claim in the Okta access token **equals** the user's Google Workspace
> **primary** address.

**Use the primary address.** Google documents the `sub` field only as "the email address
of the user for which the application is requesting delegated access", and documents
`invalid_grant`'s cause only as "the user doesn't exist" — so what happens with an
**alias** is *undocumented in both directions*. Evidence cuts both ways: an alias is not
a sign-in-able account, yet the Directory API accepts aliases as a `userKey`. Do not
build on either guess. Send the primary and the behaviour is specified; send an alias and
you are relying on something Google has never committed to.

Check the Okta token and the Workspace user record agree exactly:

```bash
python3 client/login.py
python3 -c "import json,base64,pathlib; \
  t=json.loads(pathlib.Path('client/.token.json').read_text())['access_token']; \
  p=t.split('.')[1]; p+='='*(-len(p)%4); \
  print(json.loads(base64.urlsafe_b64decode(p)))"
```

If `email` is absent, add it to the Okta authorization server's access-token claims —
Ripple has nothing to impersonate with otherwise. If it's present but is an alias, map
the claim to the primary in Okta (or fix the Workspace primary) rather than testing
whether the alias happens to work today.

---

## Google Docs specifically

A Google Doc is not a file with bytes — it is a Drive object with a
`application/vnd.google-apps.document` MIME type, so a plain `files.get` download
returns nothing useful. Two ways to read the text, both covered by the same delegation:

| Approach | Scope | When |
|---|---|---|
| **Drive export** — `files.export(fileId, mimeType='text/plain')` | `drive.readonly` | Default. Gives the body text, which is what a grounded answer needs. |
| **Docs API** — `documents.get` | `drive.readonly` also works (`documents.readonly` not required) | Only when you need structure (headings, tables) rather than prose. |

Start with Drive export — one API, one scope, and the text is directly quotable, which
is what STRICT GROUNDING requires. `verify_drive_delegation.py --export <fileId>`
exercises exactly this path.

Note that `documents.get` accepts `drive.readonly` in addition to `documents.readonly`,
so **enabling the Docs API needs no change to the B4 delegation grant** — enable the API
in the Cloud project and the existing scope covers it.

**`files.export` is capped at 10 MB of exported content.** This is a hard failure, not a
truncation: export fetches the *whole* document before any client-side `max_chars` trim
applies, so an oversize Doc returns nothing rather than a readable prefix. The Drive tool
degrades that to a reported `note` on the result, which the agent is instructed to
surface — so a huge document reads as "could not be extracted", never as "exists and is
silent on your question". Google's escape hatch for larger exports is `files.download`
with a long-running operation; Ripple does not use it, since a Doc big enough to hit
10 MB of plain text is not a document a grounded answer should be quoting wholesale.

---

## Seed test content (makes the permission-trim demo visible)

Mirroring the GitHub setup in [`DATA-SOURCES.md`](DATA-SOURCES.md), use two Workspace users who can
see different things. The examples below use **amir** and **dana** on your verified
domain (`amir@yourdomain.com`, `dana@yourdomain.com`); substitute your own two primary
addresses.

### Two rules that decide whether the demo works

**1. Create the files as the users, in a browser — not with a script.** The service
account is granted `drive.readonly` only (§B4), so it *cannot* create or share
anything, by design: this component must never be able to modify a customer's Drive.
Sign in as each user and create their files in the Drive UI.

**2. Put real sentences in the body.** `search_drive()` queries
`fullText contains '<term>'`, which matches document **content**, not just titles. A
Doc named `Compensation FY26` with an empty body will not be found by a question about
compensation. Each file below therefore has suggested body text — a couple of specific
sentences with a number or a name in them is plenty, and specific beats plausible,
because the answer is graded on whether it can be cited.

### The four files

Create these as Google **Docs** (Docs export cleanly to text, which is what the tool
reads):

| # | Create as | Title | Share with | Body should contain |
|---|---|---|---|---|
| 1 | user one | `Ripple Onboarding Guide` | **both** users | "New engineers get a laptop on day one. The VPN is set up by IT during week one. Ask #ripple-help for access requests." |
| 2 | user one | `Compensation FY26` | **user one only** — do not share | "The FY26 engineering salary band for L5 is 180,000 to 210,000 USD. Equity refreshes annually in March." |
| 3 | user two | `Support Runbook` | **user two only** — do not share | "Sev-1 pages the on-call within five minutes. Escalate to the platform team after thirty minutes with no ack." |
| 4 | user one | `2026 Product Roadmap` | **both** users | "Q1 2026 ships the Slack front door. Q2 2026 adds Confluence as a source. Databricks Genie is planned for Q3 2026." |

Files 1 and 4 are the shared baseline — both users must get the *same* answer from
them, which is what shows the trim is a permission boundary and not randomness. Files 2
and 3 are the asymmetric pair, and having one private file **per user** (rather than one
overall) matters: it rules out "user two just sees less of everything" and shows each
user seeing exactly their own slice.

### What to check

Run the verifier for both users **before** deploying anything — no Okta, no agent in
the path:

```bash
export GOOGLE_SA_SECRET_ARN=<the arn from B3>
python3 scripts/verify_drive_delegation.py user-one@yourdomain.com
python3 scripts/verify_drive_delegation.py user-two@yourdomain.com
```

Expected, and this **is** the ACL trim demonstrated end to end:

- user one's listing contains `Compensation FY26`; user two's does **not**.
- user two's listing contains `Support Runbook`; user one's does **not**.
- both listings contain `Ripple Onboarding Guide` and `2026 Product Roadmap`.

If both users see identical lists, sharing is wider than intended — open file 2 in
Drive and confirm it is not shared with the domain or "anyone with the link".

Then, once deployed, the same asymmetry through the agent:

- user one asks about the L5 salary band → **HIGH**, cited to `Compensation FY26`.
- user two asks the same question → **LOW** / not found. Drive never returned the file,
  so there is nothing to quote — the trim happens at Google, before the model sees
  anything.

Neither user sees a consent screen at any point. That contrast against the GitHub
source — which *does* prompt once — is the clearest way to show why the path is
per-source configuration.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `unauthorized_client` | The **service account email** was pasted into the delegation Client ID field instead of the **numeric** client ID. Also appears if the scope requested at runtime is not in the authorized list — the grant is per-scope, exact-match. |
| `Client is unauthorized to retrieve access tokens using this method` | Delegation not yet propagated (wait), or the grant was added in the wrong Workspace domain. |
| `invalid_grant: Invalid email or User ID` | `sub` is not a real Workspace user. Google documents only "the user doesn't exist" here. If the value is an **alias**, retry with the primary address — alias handling is undocumented and not something to depend on either way. |
| `403 insufficientFilePermissions` | Delegation is fine but the scope is too narrow for the call — e.g. exporting a Doc with only a metadata scope. (This is Drive's documented reason string; the shorter `insufficientPermissions` also appears on some Google surfaces, so the code matches both.) |
| **Create new key** is unavailable in B3 | `iam.disableServiceAccountKeyCreation` is enforced — the default for orgs created on/after 2024-05-03. Needs a project-level org-policy exception; see B3. |
| A large Google Doc returns no text | `files.export` caps exported content at 10 MB and fails whole rather than truncating. The tool reports this as a `note`; it is not a permission problem. |
| Confident "not found" that seems wrong | Check for `incompleteSearch` in the search result. Drive sets it when some drives could not be searched, which makes a partial result look complete. `verify_drive_delegation.py` now prints it too. |
| Delegation works, but Ripple finds nothing | The `email` claim is missing from the Okta token, so there is nothing to impersonate. Check with the decode snippet above. |
| API returns `accessNotConfigured` | The Drive (or Docs) API is not enabled in the Cloud project from B1. |

---

## Summary — what you should have when this is done

All non-secret:

- 📋 the service account **numeric Client ID** — used in the B4 delegation grant
- 📋 the Secrets Manager **ARN** from B3 — becomes `GOOGLE_SA_SECRET_ARN`
- 📋 your Workspace **domain** — becomes `ALLOWED_EMAIL_DOMAINS`
- 📋 the two test users' **primary** email addresses — not configuration; each user's
      email reaches the runtime in their own token, never from a file
- 📋 a passing `verify_drive_delegation.py` for both users, which proves hop 2 works
      before any agent code depends on it

Nothing goes in an environment variable as a secret: the service-account key goes
from the browser download straight into Secrets Manager, and the local copy is
shredded.

### Where those land in `dev.env`

Two of them are configuration, and they are a **pair** — the runtime stack refuses to
deploy with one set and the other empty (`02-runtime.yaml` → `Rules:
DriveRequiresDomainAllowlist`). Until both are filled, Drive is off and the GitHub path
deploys exactly as it does today. Both are already present, commented out, in
`dev.env` (and in `dev.env.example` as a template):

```bash
export GOOGLE_SA_SECRET_ARN=arn:aws:secretsmanager:us-west-2:...:secret:ripple/google-drive-sa-xxxxxx
export ALLOWED_EMAIL_DOMAINS=yourdomain.com     # comma-separated for several
```

| Value | Where it comes from | Secret? |
|---|---|---|
| `GOOGLE_SA_SECRET_ARN` | printed by the `aws secretsmanager create-secret` command in **B3** | No — an ARN is a pointer, not the credential |
| `ALLOWED_EMAIL_DOMAINS` | your Workspace domain, the same one the **B4** delegation grant covers | No |

`ALLOWED_EMAIL_DOMAINS` is not cosmetic: it bounds **who can be impersonated**. With it
unset, the set of acceptable subjects is every address your IdP will sign for, so the
per-user ACL trim becomes a domain-wide read. `identity_claims.py` fails closed on an
empty allowlist and `tests/test_identity_claims.py` has a regression test for exactly
that, but the value still has to be right: it must match your Workspace domain, and
each user's Okta `email` claim must equal their Workspace **primary** address.

The service-account **JSON key** is not in this list, and must not be: it goes straight
from the browser download into Secrets Manager (B3) and the local copy is shredded. It
is domain-wide and does not expire.
