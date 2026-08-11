"""Cryptographically verified claims from the caller's IdP token.

THIS MODULE IS A SECURITY BOUNDARY. Read the whole docstring before changing it.

WHY IT EXISTS. The rest of the agent gets the user's identity *ambiently*: the
runtime's CUSTOM_JWT authorizer validates the inbound JWT, and the platform injects
a WorkloadAccessToken that `@requires_access_token` forwards to AgentCore Identity.
Agent code never needs the raw token, and cannot forge an identity, because it never
asserts one.

The Google Drive source breaks that property, unavoidably. Domain-wide delegation
mints a token by putting a user's email in an assertion's `sub` claim, so THIS CODE
must name the user. The email therefore stops being a log field and becomes an
authorization decision — and the service account behind it can impersonate ANY user
in the Workspace domain.

Consequence: `jwt.decode(..., options={"verify_signature": False})`, which is fine
for logging a `sub`, is a DOMAIN-WIDE READ PRIMITIVE here.

WHERE THE TOKEN COMES FROM, AND WHY IT MATTERS THAT IT IS THE HEADER. `agent.py`
`_caller_jwt()` reads the `Authorization` header — the same value the runtime's
CUSTOM_JWT authorizer authenticated. Taking it from the request body instead would open
a second identity channel that the authorizer never sees: a caller could present a valid
header for themselves alongside a body token claiming `email: ceo@customer.com`, both
would verify, and the impersonation would follow the body. One channel means the two
cannot disagree.

That still does not make verification here optional, because the authorizer's guarantee
is narrower than this module's requirement. So every claim used for impersonation is
verified below: signature against the IdP's JWKS, plus issuer, audience and expiry — and
then the two gates further down that no authorizer performs. A token that fails any of
them leaves the source unavailable for that turn.

WHAT THIS MODULE DELIBERATELY DOES NOT DO. It never falls back to an unverified
decode. There is no `verify=False` path, no "best effort" mode, and no default
subject. If verification fails, the Drive source yields nothing — a demo that
silently reads the wrong user's files is worse than one that returns no answer.

WHAT A VERIFIED TOKEN DOES *NOT* PROVE, and why there are two more gates below.
Signature + iss + aud + exp prove the token is AUTHENTIC. They do not prove the
holder is entitled to the mailbox named in `email`. An issuer that permits
self-signup will happily sign a genuine token whose `email` is a colleague's
address; nothing is forged, and every check above passes. Two independent gates
close that, and both are required because neither is sufficient alone:

  1. `email_verified` must be TRUE, not merely "not False". Absent-is-accepted was
     the original behaviour and it is exactly the self-signup hole: an
     unconfirmed address arrives with the claim absent, not false.
  2. The domain must be on an explicit allowlist. Domain-wide delegation is granted
     per Workspace domain, so a subject outside it can never be legitimate — and
     pinning the domain bounds what an issuer compromise can reach to the one
     domain we already trusted.

Gate 2 alone would not stop a self-registered `ceo@ourdomain.com`; gate 1 alone
would not stop a second, differently-configured tenant of the same issuer. Hence
both.

WHERE THIS SITS IN THE ARCHITECTURE DIAGRAM (infra/architecture-components.png):

This module is the `RT`->`OKTA` edge, step 6a — the one the diagram's own comment calls
its load-bearing edge, and the only numbered step that belongs to the delegation flow
alone.
It has no tile: it is a module inside the Runtime container, and the diagram carries
what it DOES on that edge instead, because the load-bearing fact is not "a module
exists" but that the subject on the wire to Google was cryptographically verified. The
`_discover()`/`_client()` fetches below are what makes 6a a real hop to the IdP rather
than a local check; verified_email()'s return value is the `sub` step 8a signs into a
service-account assertion.

Why 6a exists only in flow A, and why that asymmetry is the point of numbering it: flow
B never names a user. GitHub is called under a token GitHub itself issued after the user
approved, so no claim of ours selects whose data is read and there is nothing here to
get wrong. The delegated path has no such backstop — the diagram draws 6a as the step
that makes impersonation safe, and that is exactly what a failure here withdraws.

TWO GATES THE DIAGRAM DOES NOT DRAW, and they are not cosmetic. `email_verified` and
ALLOWED_EMAIL_DOMAINS both fail closed here, and neither is visible as a step, a badge
or a NOTE bullet — the diagram's account of 6a stops at "re-verifies the JWT's signature
ITSELF", which is precisely the level of assurance the docstring above spends fifteen
lines explaining is NOT sufficient. A reader who takes the diagram as the specification
would build a 6a that authenticates the token and still hands over a colleague's Drive.

The failure mode is likewise undrawn: 6a raising does not produce an error arrow, it
produces a turn in which every delegated source is silently absent from
`connected_sources` while flow B's sources answer normally. The 11a-14a return lane is
walked in full, so a reader tracing the green path cannot tell from the diagram that
step 6a ever ran, let alone that it refused.
"""
import json
import os
import threading
import urllib.request

import jwt
from jwt import PyJWKClient

# Same values the runtime's CUSTOM_JWT authorizer is configured with, so this
# verification cannot be weaker than the front door's. Injected by
# infra/02-runtime.yaml from the SAME template parameters, rather than re-typed,
# precisely so the two cannot drift apart.
_ISSUER = os.environ.get("IDP_ISSUER") or ""
_AUDIENCE = os.environ.get("IDP_AUDIENCE") or ""

# Which email domains may be impersonated. REQUIRED — there is no permissive
# default, because the default would have to be "any domain the IdP will sign for",
# and that is the whole vulnerability. Comma-separated; matched case-insensitively
# on the part after the last '@'.
_ALLOWED_DOMAINS = tuple(
    d.strip().lower().lstrip("@")
    for d in (os.environ.get("ALLOWED_EMAIL_DOMAINS") or "").split(",")
    if d.strip()
)

# Escape hatch for an IdP that genuinely cannot emit `email_verified` — some
# directory-sourced deployments omit it, and the address is authoritative by
# provisioning there. Named for what it WEAKENS, not for what it enables, and it
# must be set deliberately: the safe reading of an absent claim is "unverified".
# Only set this where self-signup is impossible; with it on, the domain allowlist
# is the only thing standing between a self-registered account and someone else's
# Drive.
_EMAIL_VERIFIED_OPTIONAL = (
    os.environ.get("TRUST_UNVERIFIED_EMAIL_CLAIM", "").lower() in ("1", "true", "yes")
)

# One JWKS client per process, rebuilt when the discovery document is refetched.
#
# NOTE ON `cache_keys`: it is deliberately NOT set. It installs a plain `lru_cache`
# over `get_signing_key(kid)` with NO expiry (PyJWT 2.10.1, jwks_client.py), so a
# `kid` that has been ROTATED OUT — revoked because it was compromised — keeps
# verifying for the life of the process. Rotation IN works either way (a cache miss
# refreshes), so the untimed cache only ever helps the direction that does not
# matter and hurts the one that does. `cache_jwk_set` with `lifespan` gives the real
# benefit — one fetch per 10 min, not one per invocation — and it does expire.
_jwks_lock = threading.Lock()
_jwks_client: PyJWKClient | None = None
_discovered_issuer: str | None = None

# The discovery fetch and the JWKS fetch both block. `verified_email()` is called
# from an async entrypoint, so an unresponsive IdP would otherwise pin the event
# loop for PyJWT's 30s default.
_HTTP_TIMEOUT = 5


class ClaimVerificationError(Exception):
    """Verification failed. Carries a reason for logs, never for the end user.

    The message can quote token internals (issuer, audience mismatch), so callers
    must log it and surface something generic. It is an operator diagnostic.
    """


def _discover() -> tuple[str, str]:
    """(issuer, jwks_uri) read from the IdP's OIDC discovery document.

    WHY NOT BUILD THE URL FROM THE ISSUER. That is what this module used to do
    (`<issuer>/.well-known/jwks.json`) and it is an Auth0-shaped guess, not a
    standard. Okta — the IdP the Drive setup doc is written for end to end — puts
    its keys at `<issuer>/v1/keys` and emits an `iss` with no trailing slash, so
    the guess failed twice over: the JWKS fetch 404s AND the exact-string issuer
    comparison mismatches. The runtime's CUSTOM_JWT authorizer has always used
    discovery (`DiscoveryUrl` in infra/02-runtime.yaml); this now reads the same
    document, so "the agent's check is the same check as the front door's" is a
    fact about the code rather than a coincidence of Auth0's URL layout.

    The document's own `issuer` is authoritative for the `iss` comparison, but it is
    cross-checked against IDP_ISSUER first — otherwise pointing this at an
    attacker's discovery document would make it trust the attacker's keys and their
    issuer name in one move.
    """
    if not _ISSUER:
        raise ClaimVerificationError(
            "IDP_ISSUER is not set on this runtime, so the caller's token cannot be "
            "verified. Refusing to impersonate on the strength of an unverified claim."
        )
    url = (os.environ.get("IDP_DISCOVERY_URL")
           or _ISSUER.rstrip("/") + "/.well-known/openid-configuration")
    if not url.startswith("https://"):
        raise ClaimVerificationError(f"discovery URL is not https: {url!r}")
    try:
        with urllib.request.urlopen(url, timeout=_HTTP_TIMEOUT) as r:
            doc = json.loads(r.read())
    except Exception as e:
        raise ClaimVerificationError(
            f"could not read the IdP discovery document at {url}: "
            f"{type(e).__name__}: {e}"
        ) from e

    issuer, jwks_uri = doc.get("issuer") or "", doc.get("jwks_uri") or ""
    if not issuer or not jwks_uri:
        raise ClaimVerificationError(
            f"discovery document at {url} lacks 'issuer' or 'jwks_uri'"
        )
    # Trailing slash only: Auth0 emits one, Okta does not, and the difference is
    # cosmetic. Any other divergence means IDP_ISSUER and the discovery document
    # describe different IdPs, which is a misconfiguration worth failing on.
    if issuer.rstrip("/") != _ISSUER.rstrip("/"):
        raise ClaimVerificationError(
            f"discovery document issuer {issuer!r} does not match IDP_ISSUER "
            f"{_ISSUER!r}; these must be the same IdP"
        )
    if not jwks_uri.startswith("https://"):
        raise ClaimVerificationError(f"jwks_uri is not https: {jwks_uri!r}")
    return issuer, jwks_uri


def _client() -> tuple[PyJWKClient, str]:
    """(JWKS client, authoritative issuer string). Built once, then reused."""
    global _jwks_client, _discovered_issuer
    with _jwks_lock:
        if _jwks_client is None:
            _discovered_issuer, jwks_uri = _discover()
            _jwks_client = PyJWKClient(
                jwks_uri, cache_jwk_set=True, lifespan=600, timeout=_HTTP_TIMEOUT
            )
        return _jwks_client, _discovered_issuer or _ISSUER


def verified_claims(token: str) -> dict:
    """Verify `token` and return its claims. Raises on ANY doubt.

    Checks, all of them mandatory:
      - signature, against a key fetched from the issuer's published JWKS by `kid`
      - `iss` matches the issuer named in the IdP's discovery document
      - `aud` contains IDP_AUDIENCE
      - `exp` / `nbf` (PyJWT enforces these by default; stated to make the
        expiry check explicit rather than incidental)

    What the audience check DOES buy: `aud` names the API, so this rejects tokens
    minted for a different API by the same issuer. What it does NOT buy, despite
    an earlier comment here claiming otherwise: it does not isolate applications.
    Any client in the tenant authorized for this same API mints tokens accepted
    here. That is the same boundary the runtime's authorizer enforces, so it is not
    a divergence — but do not read `aud` as a per-application check.

    There is intentionally no way to skip a check. If you find yourself wanting one
    for local testing, point IDP_ISSUER at a local IdP instead of weakening this.
    """
    if not token:
        raise ClaimVerificationError("no token supplied")
    if not _AUDIENCE:
        raise ClaimVerificationError(
            "IDP_AUDIENCE is not set on this runtime; refusing to verify without an "
            "audience check, because a token minted for a DIFFERENT API by the same "
            "issuer would otherwise be accepted here."
        )
    try:
        client, issuer = _client()
        signing_key = client.get_signing_key_from_jwt(token)
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],   # allowlist: never read `alg` from the token
            issuer=issuer,
            audience=_AUDIENCE,
            options={"require": ["exp", "iss", "aud"]},
        )
    except ClaimVerificationError:
        raise
    except Exception as e:
        raise ClaimVerificationError(f"{type(e).__name__}: {e}") from e


def verified_email(token: str) -> str:
    """The caller's email, verified — the ONLY acceptable impersonation subject.

    Returns a non-empty address or raises. Never returns a default, because a
    default subject in an impersonation flow reads someone else's documents.

    Use the user's Workspace PRIMARY address. Google documents `sub` only as "the
    email address of the user for which the application is requesting delegated
    access" and never states how an alias is treated, so an alias is relying on
    unspecified behaviour — see docs/GOOGLE-DRIVE-OKTA-SETUP.md.
    """
    claims = verified_claims(token)
    email = claims.get("email")
    # Type-check before touching it. A signature-VERIFIED token whose `email` is a
    # dict or a number is truthy and has no `.strip()`; an AttributeError here is not
    # a ClaimVerificationError, so it would escape the caller's handler and fail the
    # whole turn, taking unrelated sources down with it.
    if email is not None and not isinstance(email, str):
        raise ClaimVerificationError(
            f"'email' claim is not a string but {type(email).__name__}"
        )
    email = (email or "").strip()
    if not email:
        # Common and worth naming precisely: many authorization servers omit `email`
        # from ACCESS tokens unless the claim is added explicitly. Without it there
        # is no verified subject, so the source must stay dark.
        raise ClaimVerificationError(
            "the verified token carries no 'email' claim. Add it to the "
            "authorization server's access-token claims; without a verified email "
            "there is no subject to impersonate, and an unverified one would be a "
            "domain-wide read."
        )
    if "@" not in email:
        raise ClaimVerificationError(f"'email' claim is not an address: {email!r}")

    # GATE 1: the issuer must AFFIRM the address, not merely decline to deny it.
    #
    # This used to be `is False`, which accepted an absent claim. That is the
    # self-signup hole, and the repo's own setup docs made absent the EXPECTED state:
    # they tell the operator to add `email` to the access-token claims and say
    # nothing about `email_verified`. So an attacker who self-registers as
    # `ceo@ourdomain.com` and never confirms the address gets a genuine,
    # correctly-signed token, and the old check saw None rather than False and waved
    # it through — reading the real CEO's Drive with nothing forged. The logs looked
    # normal, because `result["user"]` shows the attacker's own `sub`.
    #
    # The safe reading of an absent claim is "not verified". TRUST_UNVERIFIED_EMAIL_
    # CLAIM exists for directory-sourced IdPs that genuinely cannot emit it, and is
    # opt-in precisely so that reading has to be argued for, not inherited.
    if claims.get("email_verified") is not True:
        if not _EMAIL_VERIFIED_OPTIONAL:
            state = ("absent" if "email_verified" not in claims
                     else repr(claims.get("email_verified")))
            raise ClaimVerificationError(
                f"the token does not affirm email {email!r} as verified "
                f"(email_verified is {state}); refusing to impersonate it. If this "
                "IdP cannot emit the claim and self-signup is impossible, set "
                "TRUST_UNVERIFIED_EMAIL_CLAIM=true deliberately."
            )
        if claims.get("email_verified") is False:
            # Even with the escape hatch on, an EXPLICIT false is a statement by the
            # issuer, not a gap in it. Never override that.
            raise ClaimVerificationError(
                f"the issuer reports email {email!r} as UNVERIFIED; refusing to "
                "impersonate it"
            )

    # GATE 2: the domain must be one we hold a delegation grant for.
    #
    # Independent of gate 1 and not redundant with it. The delegation grant is
    # per-Workspace-domain, so a subject outside the allowlist could never be a
    # legitimate impersonation target — and pinning it bounds an issuer compromise
    # to the domain we already trusted, instead of to every address the IdP will
    # sign. Required, with no permissive default: the only possible default is "any
    # domain", which is the vulnerability.
    if not _ALLOWED_DOMAINS:
        raise ClaimVerificationError(
            "ALLOWED_EMAIL_DOMAINS is not set on this runtime. Refusing to "
            "impersonate any address, because without an allowlist the accepted set "
            "is every address this issuer will sign for."
        )
    domain = email.rsplit("@", 1)[1].lower()
    if domain not in _ALLOWED_DOMAINS:
        raise ClaimVerificationError(
            f"email domain {domain!r} is not in ALLOWED_EMAIL_DOMAINS; the "
            "domain-wide delegation grant does not cover it"
        )
    return email
