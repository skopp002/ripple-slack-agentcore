# Ripple Code Walkthrough: User Identity to Google Drive ACL Enforcement

## Purpose

This document explains how Ripple authenticates a user through an OpenID Connect (OIDC) identity provider, invokes Amazon Bedrock AgentCore Runtime, extracts and verifies the user's identity, and accesses Google Drive so that the user sees only documents they are already authorized to access.

The design relies on two independent authorities:

1. **The OIDC identity provider** establishes who the user is.
2. **Google Drive** determines which documents that user may access.

Ripple does not download all company documents and implement its own access-control filter. It calls Google Drive using credentials that impersonate the authenticated user, and Google Drive applies its native access-control lists.

> **Terminology:** This project sometimes labels the Google path as “OBO” because it provides a no-per-user-consent experience. The implemented mechanism is specifically **Google Workspace domain-wide delegation with a delegated subject**, not an RFC 8693 token exchange.

## Architecture

![Ripple architecture and identity flows](infra/architecture-components.png)

The relevant Google Drive path is:

```text
User authenticates with OIDC provider
    -> OIDC provider issues a signed access token
    -> caller invokes AgentCore Runtime with the token
    -> AgentCore validates the bearer token
    -> Ripple reverifies the token used for delegation
    -> Ripple extracts a verified Workspace email
    -> Google service account impersonates that email
    -> Google issues a read-only Drive token for that user
    -> Google Drive applies the user's native permissions
    -> Ripple receives only documents that user may access
```

## Auth0 and Okta clarification

The current CLI in `client/login.py` authenticates directly against **Auth0** using Auth0's device authorization endpoints. If Okta is federated into Auth0, the user may sign in through Okta, but the token Ripple receives is issued by Auth0.

The runtime-side claim verification uses standard OIDC discovery and can support a conforming Okta authorization server when configured with its full issuer and discovery URL. A direct Okta deployment would also need an Okta-compatible login client because the current CLI endpoints are Auth0-specific.

## Required token claims

The OIDC access token used for Google delegation must include:

| Claim | Purpose |
|---|---|
| `iss` | Identifies the trusted token issuer. |
| `aud` | Confirms that the token was minted for the Ripple API. |
| `sub` | Stable identity-provider user identifier. |
| `email` | User's Google Workspace primary email address. |
| `email_verified` | Confirms that the issuer verified the email address. |
| `exp` | Prevents expired tokens from being accepted. |

Example claims:

```json
{
  "iss": "https://issuer.example.com/",
  "sub": "00u123456789",
  "aud": "ripple-api",
  "email": "alice@customer.example",
  "email_verified": true,
  "exp": 1785472686
}
```

The `email` value must be the user's Google Workspace **primary address**. Alias handling is not relied upon by this implementation.

## End-to-end walkthrough

### 1. The user starts OIDC device authorization

The CLI requests a device code from the configured Auth0 tenant:

```python
# client/login.py

def _device_code():
    r = requests.post(
        f"https://{DOMAIN}/oauth/device/code",
        data={
            "client_id": CLIENT_ID,
            "audience": AUDIENCE,
            "scope": SCOPE,
        },
        timeout=15,
    )
    r.raise_for_status()
    return r.json()
```

The requested scope normally includes:

```text
openid profile email offline_access
```

The identity provider returns a device code, user code, browser verification URL, and polling interval.

### 2. The user signs in

The CLI opens the verification URL and waits for approval:

```python
# client/login.py

dc = _device_code()
url = dc["verification_uri_complete"]

webbrowser.open(url)

tok = _poll_for_token(
    dc["device_code"],
    dc.get("interval", 5),
)
```

If Auth0 is federated with Okta, the browser redirects the user to Okta and returns to Auth0 after successful authentication.

The authorization server then returns a signed user access token.

### 3. The caller invokes AgentCore Runtime

The access token travels in exactly one place: the HTTP Authorization header. The JSON request body carries the question and nothing else.

```python
# client/ask.py

headers = {
    "Authorization": f"Bearer {access_token}",
    "Content-Type": "application/json",
    # Session id groups turns for the same user conversation (>=33 chars).
    "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session_id,
}

body = {"prompt": prompt}
params = {"qualifier": QUALIFIER}
r = requests.post(url, headers=headers, params=params, json=body, timeout=120)
```

The `Authorization` header is the single identity channel, and it has two consumers. The AgentCore custom JWT authorizer authenticates the caller at the runtime boundary, before the container runs. The Ripple agent then reads the same header string to derive the Google delegated subject, after performing its own verification of it (steps 5 and 6). Because there is only one channel, the identity AgentCore authenticated and the identity used for delegation cannot be two different values.

### 4. AgentCore validates the header token

The runtime is configured with a custom JWT authorizer:

```yaml
# infra/02-runtime.yaml

AuthorizerConfiguration:
  CustomJWTAuthorizer:
    DiscoveryUrl: !Sub 'https://${Auth0Domain}/.well-known/openid-configuration'
    AllowedAudience:
      - !Ref Auth0Audience
```

Before invoking the container, AgentCore validates the bearer token using the issuer's OIDC discovery document and JSON Web Key Set (JWKS). The validation covers the token signature, issuer, audience, and expiry.

If the header token is invalid, the request should be rejected before `agent.py` runs.

The same identity configuration is injected into the runtime for the delegated-source verification:

```yaml
# infra/02-runtime.yaml

EnvironmentVariables:
  IDP_ISSUER: !Sub 'https://${Auth0Domain}/'
  IDP_AUDIENCE: !Ref Auth0Audience
  ALLOWED_EMAIL_DOMAINS: !Ref AllowedEmailDomains
  TRUST_UNVERIFIED_EMAIL_CLAIM: !Ref TrustUnverifiedEmailClaim
  GOOGLE_SA_SECRET_ARN: !Ref GoogleSaSecretArn
```

### 5. The agent reads the caller's token from the Authorization header

The AgentCore entrypoint takes the question from the payload and the identity from the request context:

```python
# agent/agent.py

@app.entrypoint
async def invoke(payload, context):
    prompt = payload.get("prompt", "").strip()
    user_jwt = _caller_jwt(context)
    if not prompt:
        return {"error": "empty prompt"}

    user_sub = _user_sub(user_jwt)
```

`_caller_jwt()` is the only place a user token enters the agent:

```python
# agent/agent.py

def _caller_jwt(context) -> str:
    headers = getattr(context, "request_headers", None) or {}
    raw = headers.get("Authorization") or ""
    # Case-insensitive scheme, and tolerate the token being sent bare. Anything that is
    # not a Bearer credential is not one we can use.
    parts = raw.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return raw.strip() if len(parts) == 1 else ""
```

The runtime SDK supplies that header. `RequestContext` declares a `request_headers: Optional[Dict[str, str]]` field (`bedrock_agentcore/runtime/context.py`), and when the SDK collects forwardable headers it gives `Authorization` its own branch (`bedrock_agentcore/runtime/app.py`): the key is normalized to canonical casing regardless of the casing on the wire, and it is exempt from the runtime header allowlist that blocks `Proxy-Authorization`, `Cookie`, and the `X-Forwarded-*` family (`bedrock_agentcore/runtime/models.py`, `is_forwardable_header`). The populated context is attached to the request and passed to the entrypoint as its second argument.

The SDK exposes the raw header string only. It does not expose a parsed or verified claims object, so the agent verifies the token itself in step 6.

`request_headers` is `None` when no forwardable header arrived, so the `or {}` makes the helper fail closed: an absent header yields `""`, and `verified_claims()` refuses an empty token rather than producing a default subject.

The agent also decodes `sub` for response labeling:

```python
# agent/agent.py

def _user_sub(access_token: str) -> str:
    try:
        claims = jwt.decode(access_token, options={"verify_signature": False})
        return claims.get("sub") or claims.get("email") or "anonymous"
    except Exception:
        return "anonymous"
```

This unverified value is not used as the Google impersonation subject. It is suitable only for non-authoritative labeling or diagnostics.

### 6. Ripple cryptographically verifies the token used for delegation

For a delegated source, the agent calls `verified_email()`:

```python
# agent/agent.py

try:
    delegated_subject = verified_email(user_jwt)
except ClaimVerificationError as e:
    delegated_subject = None
```

`verified_email()` first calls `verified_claims()`:

```python
# agent/identity_claims.py

def verified_claims(token: str) -> dict:
    if not token:
        raise ClaimVerificationError("no token supplied")

    client, issuer = _client()
    signing_key = client.get_signing_key_from_jwt(token)

    return jwt.decode(
        token,
        signing_key.key,
        algorithms=["RS256"],
        issuer=issuer,
        audience=_AUDIENCE,
        options={"require": ["exp", "iss", "aud"]},
    )
```

This validation checks:

- Signature against a key published by the identity provider
- Explicit `RS256` algorithm allowlist
- Expected issuer
- Expected audience
- Expiration and applicable temporal claims

There is no fallback to an unverified decode for delegation.

### 7. Ripple discovers the issuer and signing keys

The verifier reads the OIDC discovery document instead of assuming a provider-specific JWKS path:

```python
# agent/identity_claims.py

def _discover() -> tuple[str, str]:
    url = (
        os.environ.get("IDP_DISCOVERY_URL")
        or _ISSUER.rstrip("/") + "/.well-known/openid-configuration"
    )

    with urllib.request.urlopen(url, timeout=_HTTP_TIMEOUT) as r:
        doc = json.loads(r.read())

    issuer = doc.get("issuer") or ""
    jwks_uri = doc.get("jwks_uri") or ""

    if issuer.rstrip("/") != _ISSUER.rstrip("/"):
        raise ClaimVerificationError(
            "discovery issuer does not match configured issuer"
        )

    return issuer, jwks_uri
```

This matters for Okta because its signing-key URL and issuer formatting can differ from Auth0's. The discovery document supplies the authoritative issuer and `jwks_uri`.

### 8. Ripple extracts the verified Workspace email

After verifying the token, Ripple extracts and type-checks the email:

```python
# agent/identity_claims.py

def verified_email(token: str) -> str:
    claims = verified_claims(token)
    email = claims.get("email")

    if email is not None and not isinstance(email, str):
        raise ClaimVerificationError(
            "'email' claim is not a string"
        )

    email = (email or "").strip()

    if not email:
        raise ClaimVerificationError(
            "verified token carries no email claim"
        )

    if "@" not in email:
        raise ClaimVerificationError(
            "email claim is not an address"
        )
```

Ripple applies two additional gates because a validly signed token proves authenticity but does not, by itself, establish entitlement to the mailbox named in `email`.

#### Gate 1: require an affirmed, verified email

```python
# agent/identity_claims.py

if claims.get("email_verified") is not True:
    if not _EMAIL_VERIFIED_OPTIONAL:
        raise ClaimVerificationError(
            "token does not affirm the email as verified"
        )

    if claims.get("email_verified") is False:
        raise ClaimVerificationError(
            "issuer reports the email as unverified"
        )
```

Default behavior:

| `email_verified` state | Result |
|---|---|
| `true` | Accepted |
| `false` | Rejected |
| Missing | Rejected unless an explicit, carefully controlled override is enabled |

An explicit `false` is always rejected.

#### Gate 2: require an allowed Workspace domain

```python
# agent/identity_claims.py

domain = email.rsplit("@", 1)[1].lower()

if domain not in _ALLOWED_DOMAINS:
    raise ClaimVerificationError(
        "email domain is not in ALLOWED_EMAIL_DOMAINS"
    )
```

The allowlist is loaded from runtime configuration:

```python
_ALLOWED_DOMAINS = tuple(
    d.strip().lower().lstrip("@")
    for d in (
        os.environ.get("ALLOWED_EMAIL_DOMAINS") or ""
    ).split(",")
    if d.strip()
)
```

For example:

```text
ALLOWED_EMAIL_DOMAINS=customer.example
```

accepts `alice@customer.example` and rejects identities outside that Workspace domain.

### 9. The verified email becomes the delegated source credential

The agent stores the verified email in the per-source credential map:

```python
# agent/agent.py

for key, src in ENABLED.items():
    if src.get("credential") == _CRED_DELEGATED:
        if delegated_subject:
            tokens[key] = delegated_subject
        continue
```

For Google Drive, the value is a verified email rather than a bearer token:

```python
tokens["gdrive"] = "alice@customer.example"
```

The generic search tool passes that value to the Drive adapter:

```python
found = src["search"](token, query)
```

The Drive implementation names this argument `subject` to make its role explicit:

```python
# agent/gdrive_tool.py

def search_drive(subject: str, query: str, top: int = 8):
    drive = _drive(subject)
```

### 10. Ripple loads the Google service-account key

The Google service-account JSON is retrieved from AWS Secrets Manager:

```python
# agent/gdrive_tool.py

def _service_account_key() -> dict:
    arn = os.environ.get("GOOGLE_SA_SECRET_ARN") or ""
    if not arn:
        raise DriveConfigError(
            "GOOGLE_SA_SECRET_ARN is not set"
        )

    sm = boto3.client("secretsmanager", region_name=region or None)
    raw = sm.get_secret_value(SecretId=arn)["SecretString"]
    key = json.loads(raw)
    return key
```

The key is cached in process memory and is not written to the runtime filesystem. IAM access is scoped to the configured secret ARN in `infra/02-runtime.yaml`.

This key is highly sensitive because the associated service account can impersonate users covered by the Workspace domain-wide delegation grant.

### 11. Ripple creates read-only credentials for the verified user

The Google scope is read-only:

```python
# agent/gdrive_tool.py

SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly"
]
```

The critical operation is `with_subject(subject)`:

```python
# agent/gdrive_tool.py

def _credentials(subject: str):
    if not subject or "@" not in subject:
        raise DriveConfigError(
            "subject must be a verified user email"
        )

    return (
        service_account.Credentials
        .from_service_account_info(
            _service_account_key(),
            scopes=SCOPES,
        )
        .with_subject(subject)
    )
```

Conceptually, the Google authentication library signs a service-account assertion containing:

```json
{
  "iss": "ripple-service-account@project.iam.gserviceaccount.com",
  "sub": "alice@customer.example",
  "scope": "https://www.googleapis.com/auth/drive.readonly",
  "aud": "https://oauth2.googleapis.com/token"
}
```

Google verifies:

1. The service-account signature.
2. Domain-wide delegation is enabled.
3. The Workspace administrator authorized `drive.readonly` for the service account's numeric client ID.
4. The delegated subject is a valid Workspace user.

Google then issues a Drive access token representing the delegated user.

### 12. Ripple creates a user-scoped Drive API client

```python
# agent/gdrive_tool.py

def _drive(subject: str):
    return build(
        "drive",
        "v3",
        credentials=_credentials(subject),
        cache_discovery=False,
    )
```

Every API request made through this client runs as the delegated Workspace user.

### 13. Google applies the user's native ACLs during search

```python
# agent/gdrive_tool.py

def search_drive(subject: str, query: str, top: int = 8):
    drive = _drive(subject)

    q = (
        f"fullText contains '{_escape(query.strip())}' "
        "and trashed = false"
    )

    response = drive.files().list(
        q=q,
        pageSize=top,
        includeItemsFromAllDrives=True,
        supportsAllDrives=True,
        fields=(
            "incompleteSearch,"
            "files(id,name,mimeType,webViewLink,"
            "modifiedTime,owners(emailAddress))"
        ),
    ).execute()
```

Google evaluates the request using the delegated user's identity. Results can include:

- Files owned by the user
- Files shared directly with the user
- Files available through the user's groups
- Shared-drive content the user may access
- Domain-visible content the user may access

Files outside the user's permissions are not returned.

### 14. Document reads use the same user-scoped client

```python
# agent/gdrive_tool.py

def fetch_drive_document(subject: str, ref: str, max_chars: int = 20000):
    drive = _drive(subject)

    metadata = drive.files().get(
        fileId=ref,
        supportsAllDrives=True,
        fields="id,name,mimeType,webViewLink,modifiedTime",
    ).execute()

    body = _read_text(
        drive,
        ref,
        metadata.get("mimeType", ""),
        max_chars,
    )
```

Knowing or constructing another file's ID does not bypass authorization. If the delegated user cannot access that file, Google rejects the metadata or content request.

## Where permission trimming occurs

Permission trimming is performed by Google Drive, not by Ripple:

```text
Drive API request
    credentials = Google token for alice@customer.example
```

Google applies Alice's existing Drive permissions. Ripple does not maintain a duplicate ACL database and does not attempt to reproduce Google's authorization decisions.

The effective security chain is:

```text
Verified OIDC JWT
    -> verified and domain-allowed Workspace email
    -> service-account assertion with sub=email
    -> Google access token representing that user
    -> Google Drive native ACL evaluation
    -> only documents accessible to that user
```

## Fail-closed behavior

The delegated Drive source is unavailable when any required identity or delegation check fails:

- Missing token
- Invalid signature
- Incorrect issuer
- Incorrect audience
- Expired token
- Missing or malformed email
- Unverified email
- Email outside `ALLOWED_EMAIL_DOMAINS`
- Invalid Workspace primary address
- Missing domain-wide delegation
- Missing `drive.readonly` grant
- Missing or invalid service-account secret

The code does not fall back to:

- An unverified email
- A caller-supplied plain email string
- A default Workspace user
- The service account's own Drive view

This fail-closed behavior is essential because domain-wide delegation can authorize the service account to impersonate any user within the configured Workspace domain.

## Ripple deployment context

This repository is a validation implementation for Ripple, not a complete deployment inside Ripple's enterprise environment.

Ripple already has the two identity relationships the production design needs:

- Ripple employees sign in to enterprise Slack using Ripple-managed identities.
- Ripple employees access Google Workspace using identities governed by Ripple's Okta tenant.

The repository author does not control Ripple's Slack Enterprise Grid, Okta tenant, Google Workspace domain, or Google Workspace administrator settings. The supplied CLI therefore replaces the unavailable Slack ingress during validation:

| Validation component | Ripple production equivalent |
|---|---|
| `client/login.py` | Ripple Okta user authentication or account-linking flow |
| `client/ask.py` | Slack event/command receiver invoking AgentCore Runtime |
| Auth0 test tenant | Ripple's Okta authorization server |
| Test Google Workspace configuration | Ripple-managed Google Workspace and domain-wide delegation |
| Terminal output | Slack thread or direct-message response |

The CLI proves the runtime, identity-verification, delegated-subject, and Drive ACL behavior without claiming to implement Ripple's enterprise Slack integration.

### Slack is the user interface, not automatically an identity token source

An employee's ability to sign in to Slack through Okta does **not** mean a Slack event contains an Okta access token. A Slack request normally provides:

- A Slack request signature
- Slack enterprise and workspace identifiers
- A Slack user identifier
- Event or command data

Those values prove that Slack sent the event after the receiver validates the Slack signature. They do not, by themselves, provide the per-user Okta JWT expected by the current AgentCore custom JWT authorizer.

The Ripple integration must therefore establish a trusted bridge from the Slack user to an Okta principal. It must not simply accept an email address from the Slack message payload and use it as a Google delegated subject.

## Ripple target architecture

```text
Ripple employee
    -> sends an app mention, command, or direct message in enterprise Slack
    -> Slack sends a signed event to the Ripple Slack receiver
    -> receiver validates Slack signature, timestamp, enterprise, and event ID
    -> receiver resolves Slack enterprise/user ID to a linked Okta subject
    -> receiver obtains or refreshes a per-user Okta access token
    -> receiver invokes AgentCore with that user's Okta JWT
    -> AgentCore validates the JWT using Ripple Okta OIDC discovery and JWKS
    -> agent derives a verified Ripple Workspace primary email
    -> Google service account uses domain-wide delegation with sub=<verified email>
    -> Google issues a read-only Drive token representing that employee
    -> Google Drive applies the employee's existing Drive ACLs
    -> agent returns a grounded answer with citations
    -> receiver posts the answer to the originating Slack thread
```

The existing `agent/identity_claims.py` and `agent/gdrive_tool.py` path remains useful in this architecture. Ripple primarily needs to replace the CLI ingress, configure its enterprise identity systems, and confirm that the employee's `Authorization` header reaches the runtime intact across the Slack receiver and any ingress Gateway (see "Identity binding").

## Work Ripple must complete

### 1. Slack platform team: create the enterprise Slack application

Ripple's Slack administrators must create and install a Slack application in the approved enterprise/workspace scope.

The app should support the interaction model Ripple chooses, for example:

- App mentions
- Slash commands
- Direct messages
- Message shortcuts

The Slack team must configure only the scopes required by that interaction model. Typical examples may include `app_mentions:read`, `chat:write`, and—only if needed for identity reconciliation—`users:read` or `users:read.email`.

The app's signing secret and bot token must be stored in a managed secret store such as AWS Secrets Manager. They must not be placed in source code, CloudFormation plaintext parameters, logs, or Slack messages.

The receiver must validate every Slack request before processing it:

1. Recompute and compare the `X-Slack-Signature` using the signing secret.
2. Reject stale `X-Slack-Request-Timestamp` values to prevent replay.
3. Restrict requests to Ripple's expected Slack `enterprise_id` and approved workspace IDs.
4. Deduplicate retries using Slack's `event_id` or command identity.
5. Acknowledge Slack within its response deadline and process the AgentCore request asynchronously.

A common production pattern is:

```text
Slack Events API
    -> API Gateway or load balancer
    -> lightweight receiver Lambda/service
    -> SQS/EventBridge queue
    -> asynchronous worker
    -> AgentCore Runtime
    -> chat.postMessage/chat.update to the originating thread
```

The receiver and asynchronous worker are not implemented in this repository.

### 2. Okta identity team: expose a Ripple agent audience and user claims

Ripple's Okta administrators must configure an authorization server and application appropriate for the Slack-to-AgentCore identity bridge.

The resulting user access token must contain:

- A stable `sub`
- Ripple's expected issuer
- The AgentCore/Ripple API audience
- The employee's Google Workspace primary `email`
- An affirmative `email_verified` claim, or an explicitly reviewed directory-only alternative
- Suitable token expiration and revocation behavior

Ripple must provide the runtime with the full Okta issuer or discovery URL. An Okta custom authorization server commonly uses an issuer shaped like:

```text
https://<ripple-okta-domain>/oauth2/<authorization-server-id>
```

The OIDC discovery URL and JWKS location must come from that issuer's discovery document rather than being inferred from an Auth0 URL pattern.

The current CloudFormation parameter names—`Auth0Domain`, `Auth0Audience`, and `AUTH0_*`—should be generalized to OIDC/Okta names before production use. `client/login.py` can remain as a test harness, but it is not the production Slack authentication path.

### 3. Identity/platform team: bind each Slack user to an Okta subject

The preferred integration is a one-time account-linking flow:

1. The receiver identifies the employee by stable Slack keys such as `enterprise_id`, `team_id`, and `user_id`.
2. If no trusted link exists, the app sends the employee a short-lived account-link URL.
3. The employee signs in through Ripple Okta using Authorization Code with PKCE.
4. The callback validates state, nonce, issuer, audience, and token signature.
5. The service records a mapping from the stable Slack identity to the verified Okta `sub`.
6. The service obtains per-user Okta access tokens according to Ripple's approved token-refresh or token-exchange policy.

The mapping and any refresh-token material must be encrypted, access-controlled, auditable, and revocable. Suitable storage could include a narrowly scoped DynamoDB table plus KMS encryption and a managed token vault, depending on Ripple's platform standards.

Important constraints:

- Do not mint a shared service identity for all Slack users.
- Do not treat the Slack bot token as the employee's identity.
- Do not trust an email typed into a command.
- Do not assume `users.info` email alone is equivalent to an Okta access token.
- Do not use OAuth client credentials if that collapses all employees into one runtime identity.
- If Ripple Okta supports an approved token-exchange or delegation mechanism, validate its exact subject, audience, and resource semantics before using it instead of account linking.

The stable authorization boundary should be the Okta `sub`. Email is needed for Google Workspace delegation, but it should be taken from a verified token belonging to that same `sub`.

### 4. Google Workspace team: authorize read-only domain-wide delegation

A Ripple Google Workspace super-administrator must configure the Google side. The repository author cannot perform these steps outside Ripple's domain.

Ripple must:

1. Create a Google Cloud project owned by Ripple.
2. Enable the Google Drive API.
3. Create a dedicated service account for this workload.
4. Enable domain-wide delegation on that service account.
5. Record the service account's **numeric OAuth client ID**.
6. In Google Admin Console, authorize that numeric client ID for only:

   ```text
   https://www.googleapis.com/auth/drive.readonly
   ```

7. Store the service-account JSON key in Ripple's AWS Secrets Manager account, or use an approved keyless alternative if supported by the final architecture.
8. Configure `GOOGLE_SA_SECRET_ARN` with that secret's ARN.
9. Configure `ALLOWED_EMAIL_DOMAINS` with only the Ripple Google Workspace domains covered by the delegation grant.
10. Establish key rotation, revocation, access logging, and incident-response ownership.

Ripple must confirm that the `email` claim emitted by Okta is byte-for-byte the employee's Google Workspace primary address. If Okta emits aliases or a different canonical domain, the identity team must normalize this through an authoritative directory mapping rather than relying on undocumented Google alias behavior.

### 5. AWS platform team: deploy the production ingress and runtime configuration

Ripple's AWS team must deploy or extend the following components:

- Public HTTPS endpoint for Slack Events API or commands
- Slack request-verification receiver
- Asynchronous queue and worker
- Secure Slack token/signing-secret storage
- Slack-to-Okta account-link mapping and token storage
- AgentCore foundation and runtime stacks
- Google service-account secret
- CloudWatch logging, alarms, tracing, and security audit controls
- Network and egress controls appropriate for Slack, Okta, Google, and Bedrock endpoints

The AgentCore runtime must be configured against Ripple's Okta issuer and audience rather than the validation Auth0 tenant. The allowed email domains must be Ripple-controlled Google Workspace domains.

The runtime should continue to use a custom JWT authorizer only if the Slack worker can present a real per-user Okta JWT. If Ripple instead chooses a service-authenticated receiver, the runtime identity contract must be redesigned so that a trusted, signed user identity reaches the agent without allowing the receiver or caller to substitute another employee.

### 6. Application team: replace the CLI boundary without weakening identity

The production Slack receiver replaces these validation components:

| Existing validation code | Production replacement |
|---|---|
| `client/login.py` | Okta account-linking/login callback or approved token-exchange flow |
| `client/ask.py` | Slack receiver/worker AgentCore invocation client |
| Terminal session ID | Slack thread/conversation correlation plus a generated AgentCore session ID |
| Terminal output | Slack message posted to the originating channel/thread |

The receiver should pass the Slack context separately from the authorization identity. Example non-authoritative context includes:

```json
{
  "enterprise_id": "E...",
  "team_id": "T...",
  "channel_id": "C...",
  "thread_ts": "...",
  "slack_user_id": "U..."
}
```

These fields help route the answer but must not choose the Google delegated subject. The trusted Okta identity must choose the subject.

The receiver must preserve the single-identity-channel property described under "Identity binding": the employee's Okta JWT belongs on the `Authorization` header of the AgentCore invocation, and the request body carries the question and non-authoritative Slack routing context only. If the chosen ingress cannot present a per-user JWT on that header — a SigV4-authenticated receiver, or a Gateway hop that substitutes its own credential — the delegated subject must be re-established by a mechanism the caller cannot vary, and the agent must not read an identity from the body to compensate.

### 7. Security team: review the cross-system trust chain

Ripple security should explicitly approve the complete chain:

```text
Slack signature
    -> allowed Ripple enterprise/workspace
    -> linked Slack user ID
    -> verified Okta subject and token
    -> verified Ripple Workspace primary email
    -> Google delegated subject
    -> Drive read-only ACL enforcement
```

The review should include:

- Slack request forgery and replay protection
- Account-link takeover and CSRF protection
- Okta token audience, issuer, lifetime, refresh, revocation, and group policy
- Deprovisioned-user handling across Slack, Okta, and Google
- Domain-wide delegation blast radius
- Service-account key storage and rotation
- Cross-user data leakage tests
- Prompt-injection controls for retrieved documents
- Logging redaction for tokens, document content, queries, and Slack messages
- Data retention and deletion requirements
- Incident-response procedures for Slack, Okta, Google, or AWS credential compromise

## Recommended rollout sequence

### Phase 0: existing CLI validation

Use the current CLI to validate:

- AgentCore invocation
- OIDC/JWT claim verification
- Google domain-wide delegation mechanics
- User-specific Drive ACL behavior
- Grounded answers and citations

This phase is a technical proof and does not validate Ripple Slack administration or Ripple Google Workspace configuration.

### Phase 1: Ripple identity and Google sandbox

With Ripple administrators:

1. Configure a non-production Ripple Okta application/authorization server.
2. Emit the required access-token claims.
3. Configure a dedicated non-production Google service account and read-only delegation grant.
4. Validate with at least two Ripple test employees who have deliberately different Drive access.
5. Confirm that aliases, contractors, secondary domains, and deactivated users fail or map according to policy.

The CLI can still be used in this phase if it is adapted to Ripple Okta. This isolates identity and Google setup from Slack application work.

### Phase 2: Slack pilot

1. Install the Slack app in an approved test workspace or restricted enterprise scope.
2. Deploy request verification, asynchronous processing, and account linking.
3. Pilot with a small employee group.
4. Compare Slack results with direct Drive access for each pilot user.
5. Confirm that no shared bot or service identity is used for Drive.

### Phase 3: production hardening

Before broad rollout:

- Confirm that the employee's `Authorization` header reaches the runtime intact through the production ingress, or establish an equivalent subject derivation for it.
- Complete threat modeling and penetration testing.
- Add operational alarms and audit dashboards.
- Establish service-account key rotation or migrate to an approved keyless design.
- Add rate limits, concurrency controls, and Slack retry deduplication.
- Define user support, access revocation, and incident-response runbooks.
- Verify all customer-facing documentation reflects the implemented production architecture.

## Production acceptance criteria

Ripple should not enable broad employee access until all of the following tests pass.

### Identity and isolation

- Two employees asking the same question receive results constrained to their respective Drive permissions.
- An employee cannot retrieve a known file ID they cannot open directly in Drive.
- A request whose `Authorization` header names employee A is answered as A regardless of any additional identity fields placed in the request body; the body cannot select the delegated subject.
- A request with no `Authorization` header, or with an unverifiable token, yields no delegated source rather than a default subject.
- An unlinked Slack user cannot silently fall back to a shared or default identity.
- A user outside the configured Ripple Workspace domains is rejected.
- Missing, false, or malformed `email_verified` claims fail according to approved policy.
- A deactivated Okta or Google user loses access within the defined revocation window.

### Slack ingress

- Invalid Slack signatures are rejected.
- Stale request timestamps and duplicate event IDs are rejected or deduplicated.
- Events from unapproved Slack enterprises/workspaces are rejected.
- The receiver acknowledges within Slack's deadline and finishes work asynchronously.
- Answers are returned only to the originating approved channel, direct message, or thread.

### Google delegation

- The delegation grant contains only `drive.readonly`.
- Okta email claims match Google Workspace primary addresses.
- Shared drives are included only when the employee has access.
- Service-account key access is limited to the runtime role and approved operators.
- Key rotation and revocation have been exercised in a non-production environment.

### Observability and privacy

- Logs identify request, Slack user, Okta subject, and source outcome using non-secret identifiers.
- Access tokens, refresh tokens, Slack secrets, Google private keys, and document content are redacted.
- Source failures are distinguishable from valid empty search results.
- Security and operational teams can audit which user identity was delegated for each request without logging bearer credentials.

## Responsibility matrix

| Team | Primary responsibilities |
|---|---|
| Slack administrators | Create/install app, approve scopes, restrict enterprise/workspace access, manage Slack app governance. |
| Okta identity team | Authorization server, OIDC app, claims, account-link policy, token lifecycle, deprovisioning. |
| Google Workspace administrators | Service account, Drive API, domain-wide delegation, read-only scope, Workspace domain and primary-email alignment. |
| AWS/platform team | Slack ingress, queues/workers, secrets, AgentCore deployment, runtime authorizer, monitoring, network controls. |
| Application team | Slack adapter, account-link integration, per-user runtime invocation, answer routing, identity binding. |
| Security/privacy team | Threat model, delegation approval, secret handling, audit requirements, retention, incident response. |

## Slack frontend versus Slack knowledge source

This integration uses Slack as the **user interface**. It does not automatically make Slack a searchable enterprise knowledge source.

Adding Slack messages as a retrieval source would be a separate capability requiring:

- Slack search/history scopes
- Per-user or policy-approved authorization semantics
- Channel and enterprise ACL preservation
- Retention and legal-review controls
- A new source adapter or MCP integration

That work is outside the current Google Drive delegated-access flow and should not be conflated with receiving questions from Slack.

## Identity binding

The identity used for Google delegation is the identity AgentCore authenticated, because they are the same string. `_caller_jwt(context)` reads the `Authorization` header — the value the custom JWT authorizer validated before the container ran — and the request body carries no identity of any kind.

This closes a concrete impersonation failure mode. If the agent accepted a token from the request body, a custom caller could present a valid token for employee A in the `Authorization` header alongside a valid token for employee B in the body. AgentCore would authenticate A; the agent would verify B's token successfully, since nothing about it is forged; and the Drive impersonation would follow the body, reading B's files under A's authenticated session. Signature verification alone does not detect this, because both tokens are authentic — only equality with the authenticated principal would, and the two values have nothing forcing them to agree. Reading the header alone makes that divergence unrepresentable rather than something to check for.

The binding does not make in-agent verification optional. Two of the five gates `verified_email()` applies — the affirmative `email_verified` claim and the `ALLOWED_EMAIL_DOMAINS` allowlist — have no counterpart at the authorizer, so the authorizer's guarantee is narrower than what domain-wide delegation requires. See steps 6 through 9.

Two questions remain open. Each depends on how a given deployment's front door authenticates to the runtime, so each must be verified for that deployment before it carries production traffic.

**Inbound SigV4 authentication.** With SigV4 inbound authentication instead of `CUSTOM_JWT`, there is no user JWT on `Authorization` at all; the SDK expects the caller to supply `X-Amzn-Bedrock-AgentCore-Runtime-User-Id` instead, and says so in the error text of `_get_workload_access_token` in the SDK's `identity/auth.py` (see also the note in `agent/agent.py`). `_caller_jwt()` returns `""` in that configuration, so every delegated source is unavailable for the turn. That is fail-closed and therefore safe, but it means a SigV4 front door needs a different subject derivation before delegated sources work at all.

**Traversal of an ingress Gateway.** Whether the original caller's `Authorization` header survives a hop through an AgentCore Gateway, or is replaced by the Gateway's own credential, is unverified: no Gateway is deployed in this repository, so the question has not arisen. It becomes load-bearing in the target architecture, where the Slack front door routes this way — if the Gateway substitutes its own credential, the agent would see the Gateway's identity rather than the employee's, and the delegated subject would have to be established by another mechanism.

## Security properties

The design provides:

1. **Authenticated user identity** through OIDC and AgentCore JWT validation, with the delegated identity bound to it by construction: both are the same `Authorization` header value.
2. **Cryptographically verified delegation claims** through in-agent JWT verification.
3. **Verified email ownership** through `email_verified`.
4. **Workspace boundary enforcement** through `ALLOWED_EMAIL_DOMAINS`.
5. **Read-only Google access** through `drive.readonly`.
6. **Native source authorization** through Google Drive ACLs.
7. **No application-side ACL replication**, reducing authorization drift.
8. **Fail-closed behavior** when verification or delegation fails.

## Relevant source files

| File | Responsibility |
|---|---|
| `client/login.py` | Auth0 device authorization and token acquisition. |
| `client/ask.py` | AgentCore Runtime invocation with the user's JWT. |
| `infra/02-runtime.yaml` | Custom JWT authorizer, identity configuration, service-account secret access, and runtime environment. |
| `agent/agent.py` | Runtime entrypoint, delegated-source selection, and verified subject propagation. |
| `agent/identity_claims.py` | OIDC discovery, JWKS verification, email verification, and domain allowlisting. |
| `agent/gdrive_tool.py` | Google domain-wide delegation, user impersonation, Drive search, and document retrieval. |
| `docs/GOOGLE-DRIVE-OKTA-SETUP.md` | Google Workspace and delegated-service-account setup. |
| `docs/DATA-SOURCES.md` | Source onboarding for all five sources: credential kind, flow, and status per source, plus the recipe for adding one. |

## Summary

Ripple's Google Drive integration uses a verified OIDC identity to select a Google Workspace delegated subject. The Google service account signs an assertion containing that subject and a read-only Drive scope. Google then issues a token representing the user and applies the user's existing Drive permissions to every search and document read.

The Google delegation and native ACL-trimming model is sound, and the identity used for delegation is bound to the identity AgentCore authenticated because both are the same `Authorization` header value. Two deployment-specific questions remain to be settled before a production front door: a SigV4-authenticated ingress presents no user JWT on `Authorization`, so it needs a different subject derivation; and whether the caller's `Authorization` header survives a hop through an ingress Gateway is unverified, which matters for the Slack front door in the target architecture.
