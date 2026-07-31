"""Regression tests for agent/identity_claims.py — the impersonation subject.

WHY THESE ARE IN THE REPO rather than run once by hand. `verified_email()` decides
whose Google Drive gets read. If it ever accepts an unverified claim, the Drive
source stops being "scoped to the caller" and becomes a domain-wide read primitive
for anyone who can invoke the runtime (the service account holds domain-wide
delegation). That is a breach, not a bug, so the property needs a test that fails
loudly on a future refactor — not a note in a commit message.

WHAT MADE THIS TEST NECESSARY. An earlier version of this check appeared to pass
while proving nothing: the forged tokens were rejected by a JWKS *fetch* failure
(the issuer host did not exist), so no signature was ever verified. A test that
cannot distinguish "signature rejected" from "network down" is worse than no test,
because it reads as evidence. Hence the real RSA keypair below and the patched
local JWKS client: every rejection here is a genuine cryptographic rejection.

    python3 tests/test_identity_claims.py

WHERE THIS SITS IN THE ARCHITECTURE DIAGRAM (infra/architecture-components.png):

This is not a component and has no tile — nothing here is deployed and no arrow carries
it. It is the regression test for STEP 6a, the `AgentCore Runtime` -> `Okta (OIDC IdP)`
edge the diagram calls its load-bearing one: "the agent re-verifies the JWT's signature
ITSELF before naming a user". 6a is numbered for flow A alone, so this file guards the
green path only; flow B never names a user with a claim of ours, so nothing below applies
to it.

The edge's badge asserts one thing, a signature check. `verified_email()` is four gates,
and the tests here exist because a token can pass any subset and still be the wrong
subject for step 8a's assertion:

  - signature, against a key from the published JWKS — forged key, alg=none;
  - issuer and audience and expiry — another IdP, another application's token, expired,
    no `exp` at all, and a discovery document that names a different issuer or a
    plaintext jwks_uri, since discovery is itself a fetched input on the trust path;
  - `email_verified` is TRUE, not merely "not False" — the self-signup case, where an
    unconfirmed `ceo@` account yields a genuinely signed token with the claim ABSENT;
  - the domain is on an explicit allowlist, whole-domain and fail-closed — lookalikes,
    subdomains, an allowed domain hidden in the local part, and an empty allowlist,
    which must reject rather than mean "any domain".

The last two are why the diagram's one sentence under-describes 6a. Signature
verification proves a token is AUTHENTIC; it cannot prove its holder owns the mailbox
named in `email`, and 8a hands that email to a service account holding DOMAIN-WIDE
delegation. Every "AUTHENTIC tokens whose subject is not entitled" case below is forgery-
free and passes the check the badge names.
"""
import importlib
import json
import os
import sys
import time
from pathlib import Path

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

sys.path.insert(0, str(Path(__file__).parents[1] / "agent"))

ISSUER = "https://ripple-test.example.com/"
AUDIENCE = "https://ripple.test/api"
DOMAIN = "yourdomain.com"

# Two keypairs: one the "IdP" publishes, one an attacker holds. A token signed with
# the second must fail — that is the whole point of the module.
GOOD_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
EVIL_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
KID = "test-key-1"


class _StubJwks:
    """Stands in for PyJWKClient, returning the IdP's public key for any kid.

    Patching ONLY the key lookup is what makes these tests meaningful: the
    signature check itself runs for real, so a rejection can only come from
    cryptography — never from a failed network fetch.
    """

    def get_signing_key_from_jwt(self, token):
        class _K:
            key = GOOD_KEY.public_key()
        return _K()


def _load_module(issuer=ISSUER, audience=AUDIENCE, stub_jwks=True,
                 domains=DOMAIN, trust_unverified=""):
    """(Re)import identity_claims with a given configuration in the environment.

    The module reads IDP_ISSUER / IDP_AUDIENCE / ALLOWED_EMAIL_DOMAINS /
    TRUST_UNVERIFIED_EMAIL_CLAIM at import time — they are runtime configuration,
    not per-call arguments — so the env must be set first and the module reloaded
    whenever it changes.

    `stub_jwks=False` leaves the real client unset so `_client()` must run discovery;
    that is how the "no issuer" case is forced to raise instead of quietly reusing a
    cached client from a previous test.

    `_discovered_issuer` is set alongside the stub client because the two are
    produced together by `_client()`. Leaving it None would make the stubbed path
    fall back to IDP_ISSUER, so a test could pass against a value the real code
    never uses.
    """
    os.environ["IDP_ISSUER"] = issuer
    os.environ["IDP_AUDIENCE"] = audience
    os.environ["ALLOWED_EMAIL_DOMAINS"] = domains
    os.environ["TRUST_UNVERIFIED_EMAIL_CLAIM"] = trust_unverified
    import identity_claims
    importlib.reload(identity_claims)
    if stub_jwks:
        identity_claims._jwks_client = _StubJwks()
        identity_claims._discovered_issuer = issuer
    else:
        identity_claims._jwks_client = None
    return identity_claims


def _token(key=GOOD_KEY, alg="RS256", **overrides):
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "auth0|user-one",
        "email": "user-one@yourdomain.com",
        # Present and TRUE by default, because that is now the only accepted state:
        # an absent claim is treated as unverified. See the email_verified tests.
        "email_verified": True,
        "iat": now,
        "exp": now + 300,
    }
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not _ABSENT}
    if alg == "none":
        return jwt.encode(claims, key=None, algorithm=None,
                          headers={"alg": "none", "kid": KID})
    return jwt.encode(claims, key, algorithm=alg, headers={"kid": KID})


class _Absent:
    pass


_ABSENT = _Absent()

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))


def expect_reject(ic, name: str, token: str) -> None:
    """Assert the token is rejected, AND that the reason is not a fetch failure.

    The second half is the lesson from the false-pass: a PyJWKClient connection
    error would also raise ClaimVerificationError, so a test that only asserts
    "raised" would go green with verification effectively switched off.
    """
    try:
        got = ic.verified_email(token)
    except ic.ClaimVerificationError as e:
        msg = str(e)
        if "PyJWKClient" in msg or "Connection" in msg or "Unable to find" in msg:
            check(name, False, f"rejected for the WRONG reason: {msg}")
        else:
            check(name, True, msg.split(":")[0])
    else:
        check(name, False, f"ACCEPTED and returned {got!r}")


def main() -> int:
    ic = _load_module()

    print("\nAccepted — a correctly signed token:")
    try:
        email = ic.verified_email(_token())
        check("valid token returns the email", email == "user-one@yourdomain.com", email)
    except Exception as e:
        check("valid token returns the email", False, f"{type(e).__name__}: {e}")

    # The escape hatch, and ONLY with it deliberately on. A directory-sourced IdP may
    # be unable to emit the claim; the address is authoritative by provisioning there.
    lax = _load_module(trust_unverified="true")
    try:
        email = lax.verified_email(_token(email_verified=_ABSENT))
        check("email_verified absent accepted ONLY with TRUST_UNVERIFIED_EMAIL_CLAIM",
              email == "user-one@yourdomain.com", email)
    except Exception as e:
        check("email_verified absent accepted ONLY with TRUST_UNVERIFIED_EMAIL_CLAIM",
              False, f"{type(e).__name__}: {e}")
    # Even the escape hatch must not override an EXPLICIT false — that is a statement
    # by the issuer, not a gap in it.
    expect_reject(lax, "explicit email_verified=False rejected even with the hatch on",
                  _token(email_verified=False))

    print("\nRejected — forgeries and mismatches:")
    ic = _load_module()
    # THE load-bearing case: a valid-looking token for a privileged user, signed
    # with a key the IdP never published.
    expect_reject(ic, "wrong signing key claiming ceo@corp.com",
                  _token(key=EVIL_KEY, email="ceo@corp.com"))
    expect_reject(ic, "alg=none (unsigned)", _token(alg="none"))
    expect_reject(ic, "audience for another application",
                  _token(aud="https://someone-elses.api"))
    expect_reject(ic, "issuer is a different IdP",
                  _token(iss="https://attacker.example.com/"))
    expect_reject(ic, "expired", _token(exp=int(time.time()) - 60))
    expect_reject(ic, "no exp claim at all", _token(exp=_ABSENT))
    expect_reject(ic, "no email claim", _token(email=_ABSENT))
    expect_reject(ic, "email_verified is False",
                  _token(email_verified=False))
    expect_reject(ic, "email is not an address", _token(email="user-one"))
    expect_reject(ic, "empty token", "")
    # A signature-VERIFIED token can still carry a malformed claim. This must be a
    # ClaimVerificationError, not an AttributeError from `.strip()` — an
    # AttributeError escapes agent.py's handler and fails the whole turn, taking
    # sources unrelated to Drive down with it.
    expect_reject(ic, "email claim is a dict, not a string",
                  _token(email={"address": "user-one@yourdomain.com"}))

    print("\nRejected — AUTHENTIC tokens whose subject is not entitled:")
    # THE FINDING THAT PROMPTED THESE. Nothing below is forged. Every token here is
    # correctly signed by the real key with the right iss/aud/exp — the attack is that
    # an issuer permitting self-signup will sign a token for an address its holder
    # does not own. Signature verification cannot see that, so these two gates exist.
    #
    # The self-signup case: an account registered as ceo@ourdomain.com and never
    # confirmed. The claim arrives ABSENT, not false, which is precisely why
    # "is not True" and not "is False" is the correct test in the module.
    expect_reject(ic, "unconfirmed self-signup as ceo@ (email_verified ABSENT)",
                  _token(email="ceo@yourdomain.com", email_verified=_ABSENT))
    # The other-domain case: the same issuer serving another tenant. The delegation
    # grant is per Workspace domain, so this subject could never be legitimate.
    expect_reject(ic, "verified address outside the allowed domain",
                  _token(email="ceo@some-other-company.com"))
    # Substring matching would let `evil-yourdomain.com` or `yourdomain.com.evil.io`
    # through. The module splits on the LAST '@' and compares whole domains.
    for sneaky in ("ceo@evil-yourdomain.com", "ceo@yourdomain.com.evil.io",
                   "ceo@sub.yourdomain.com"):
        expect_reject(ic, f"lookalike domain {sneaky}", _token(email=sneaky))
    # An address whose LOCAL part contains the allowed domain, e.g.
    # "user@yourdomain.com"@evil.io — splitting on the FIRST '@' would read the
    # domain as "yourdomain.com" and accept it.
    expect_reject(ic, "allowed domain hidden in the local part",
                  _token(email='"user@yourdomain.com"@evil.io'))
    # Case and whitespace must not defeat the allowlist either.
    try:
        email = ic.verified_email(_token(email="  User-One@YOURDOMAIN.com  "))
        check("allowlist is case-insensitive and trims", "@" in email, email)
    except Exception as e:
        check("allowlist is case-insensitive and trims", False,
              f"{type(e).__name__}: {e}")

    print("\nRejected — an empty allowlist must fail CLOSED, not open:")
    # The dangerous default. "No allowlist configured" must never mean "any domain",
    # because the accepted set would be every address the IdP will sign for.
    expect_reject(_load_module(domains=""),
                  "ALLOWED_EMAIL_DOMAINS unset rejects even a valid token", _token())

    print("\nDiscovery document — a new network input, so a new trust decision:")
    # WHY THIS IS TESTED. The module used to GUESS the JWKS URL from the issuer
    # (`<issuer>/.well-known/jwks.json`), which is Auth0's layout, not a standard —
    # against Okta it 404s and the issuer string mismatches, so verification failed
    # where the runtime's authorizer succeeded. Reading discovery fixes that but adds
    # a fetched document to the trust path: whatever it names becomes the issuer we
    # compare `iss` against, and the keys we verify signatures with. If it were
    # trusted unconditionally, pointing IDP_DISCOVERY_URL at an attacker's document
    # would hand over both in one move. Hence the cross-check against IDP_ISSUER.
    def _with_discovery(doc, discovery_url=None):
        """Reload the module with urlopen stubbed to return `doc`, and run _client()."""
        import identity_claims as m
        os.environ["IDP_ISSUER"] = ISSUER
        os.environ["IDP_AUDIENCE"] = AUDIENCE
        os.environ["ALLOWED_EMAIL_DOMAINS"] = DOMAIN
        if discovery_url:
            os.environ["IDP_DISCOVERY_URL"] = discovery_url
        else:
            os.environ.pop("IDP_DISCOVERY_URL", None)
        importlib.reload(m)

        class _Resp:
            def read(self):
                return json.dumps(doc).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        m.urllib.request.urlopen = lambda url, timeout=None: _Resp()
        return m

    okta_doc = {"issuer": "https://ripple-test.example.com",   # NO trailing slash
                "jwks_uri": "https://ripple-test.example.com/oauth2/v1/keys"}
    m = _with_discovery(okta_doc)
    try:
        _, issuer = m._client()
        # Okta's own issuer string is used for the `iss` comparison, not the
        # trailing-slash form from IDP_ISSUER. Getting this backwards is exactly the
        # bug that made the agent reject every Okta token.
        check("Okta-style doc (no trailing slash) accepted, its issuer used",
              issuer == okta_doc["issuer"], issuer)
        check("jwks_uri comes from the document, not from a guessed path",
              m._jwks_client.uri == okta_doc["jwks_uri"], m._jwks_client.uri)
    except Exception as e:
        check("Okta-style doc accepted", False, f"{type(e).__name__}: {e}")

    for label, doc in (
        ("a DIFFERENT issuer", {"issuer": "https://attacker.example.com",
                                "jwks_uri": "https://attacker.example.com/keys"}),
        ("no jwks_uri", {"issuer": ISSUER}),
        ("a plaintext jwks_uri", {"issuer": ISSUER,
                                  "jwks_uri": "http://ripple-test.example.com/keys"}),
    ):
        m = _with_discovery(doc)
        try:
            m._client()
        except m.ClaimVerificationError as e:
            check(f"discovery doc with {label} rejected", True, str(e)[:70])
        else:
            check(f"discovery doc with {label} rejected", False, "ACCEPTED")

    m = _with_discovery(okta_doc, discovery_url="http://not-https.example.com/.wk")
    try:
        m._client()
    except m.ClaimVerificationError as e:
        check("plaintext IDP_DISCOVERY_URL rejected", "not https" in str(e), str(e)[:70])
    else:
        check("plaintext IDP_DISCOVERY_URL rejected", False, "ACCEPTED")

    print("\nRejected — misconfiguration must fail CLOSED:")
    # A runtime deployed without these must not fall through to an unverified
    # decode. Both are checked because they fail at different points: no issuer
    # means no JWKS URL to build; no audience means the signature could still
    # verify while the token was minted for a different application entirely.
    # No issuer: there is no JWKS URL to build, so no key can be fetched. The real
    # client must be absent here (stub_jwks=False) or a cached one would mask it.
    # No audience: the signature could verify perfectly while the token was minted
    # for a DIFFERENT application by the same issuer, so the stub stays in place and
    # the check has to come from the audience guard itself.
    #
    # Loaded LAZILY (thunks, not values). `importlib.reload` mutates the one module
    # object, so building both modules up front would leave BOTH names bound to the
    # last configuration — and the issuer case would silently assert the audience
    # error instead. It passed that way once; the reason printed alongside each PASS
    # is what exposed it.
    for var, load in (("IDP_ISSUER", lambda: _load_module(issuer="", stub_jwks=False)),
                      ("IDP_AUDIENCE", lambda: _load_module(audience=""))):
        mod = load()
        try:
            got = mod.verified_email(_token())
        except mod.ClaimVerificationError as e:
            reason = str(e).split(";")[0][:60]
            # Assert the reason names the variable under test, not merely that it
            # raised — that is the difference between a test and a coin flip.
            check(f"{var} unset raises", var in str(e), reason)
        else:
            check(f"{var} unset raises", False, f"ACCEPTED and returned {got!r}")

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    for f in FAIL:
        print(f"  FAILED: {f}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
