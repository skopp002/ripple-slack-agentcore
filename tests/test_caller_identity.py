"""Regression tests for agent/agent.py `_caller_jwt()` — where identity enters.

WHY THESE ARE IN THE REPO. `_caller_jwt()` is the single point at which the caller's
identity reaches the agent, and everything downstream trusts its output: the string it
returns becomes the impersonation subject Google Drive is read as (via
`verified_email()`), and the `sub` every log line is attributed to. Two properties have
to hold, and neither is visible in a code review of the one-line call site:

  1. IT READS THE HEADER, NOT THE BODY. The `Authorization` header is the value the
     runtime's CUSTOM_JWT authorizer authenticated. A payload field is not — a caller
     can set it to anything. If both were accepted, a caller could pair a valid header
     for themselves with a valid token naming a colleague and the impersonation would
     follow the colleague's, with both tokens verifying and every log line looking
     normal. There is no body identity channel to disagree with the header, and this
     test exists so that stays true.

  2. IT FAILS CLOSED. An absent header, an absent `request_headers` dict, or a
     non-Bearer scheme must all yield "" so that `verified_claims()` refuses. The
     dangerous failure is not an exception — it is a plausible-looking default subject.

WHAT MAKES THE ABSENT CASES REAL RATHER THAN PEDANTIC. `request_headers` is None
whenever no forwardable header arrived (bedrock_agentcore/runtime/app.py only sets it
`if request_headers:`), so `context.request_headers["Authorization"]` is a live
AttributeError on a legitimate request, not a hypothetical. And a SigV4-authenticated
caller has no user JWT on this header at all — the SDK expects
X-Amzn-Bedrock-AgentCore-Runtime-User-Id instead — so "" must mean "no delegated source
this turn", never "impersonate the default".

    python3 tests/test_caller_identity.py

WHERE THIS SITS IN THE ARCHITECTURE DIAGRAMS. This guards the tail of edge 7 and the
head of edge 8 on infra/architecture-delegation-flow.png: the SDK exposing the authenticated
header on `context`, and the agent reading the JWT from it. The diagram draws edge 8 as
one arrow to the IdP, which is the verification; the read that supplies its input is
inside the runtime and has no arrow. tests/test_identity_claims.py covers the
verification itself — this file covers only what is handed to it.

Nothing here is deployed and no tile carries it.
"""
import re
import sys
import types
from pathlib import Path

AGENT_PY = Path(__file__).parents[1] / "agent" / "agent.py"

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))


def _load_caller_jwt():
    """`_caller_jwt` extracted from agent.py, without importing the whole module.

    agent.py reads required environment variables and constructs the SOURCES table at
    import time, and importing it would pull in strands, boto3 and the AgentCore SDK.
    None of that is under test here, and stubbing it would make the test depend on the
    shape of the stubs rather than on the function. So the source text of this one
    function is compiled on its own.

    The regex is anchored on the two real definitions surrounding it, so a rename or a
    move raises here rather than silently testing nothing.
    """
    src = AGENT_PY.read_text()
    m = re.search(r"\ndef _caller_jwt\(context\) -> str:\n.*?(?=\ndef _user_sub\()",
                  src, re.S)
    if not m:
        raise SystemExit(
            "FATAL: could not find `def _caller_jwt(context) -> str:` followed by "
            "`def _user_sub(` in agent/agent.py. If the function was renamed or moved, "
            "update this test — do not delete it: it is the only check that identity "
            "comes from the authenticated header."
        )
    mod = types.ModuleType("_extracted")
    exec(compile(m.group(0), str(AGENT_PY), "exec"), mod.__dict__)
    return mod._caller_jwt


class Ctx:
    """A request context carrying whatever headers a test wants — or none."""

    def __init__(self, headers):
        self.request_headers = headers


def main() -> int:
    caller_jwt = _load_caller_jwt()

    print("\nAccepted — the token is returned from the Authorization header:")
    for name, ctx, want in [
        ("canonical `Bearer <jwt>`", Ctx({"Authorization": "Bearer abc.def.ghi"}),
         "abc.def.ghi"),
        # HTTP/2 lowercases header names and HTTP/1.1 may not; the SDK normalises the
        # key, and the SCHEME is case-insensitive per RFC 7235. A caller sending
        # `bearer` is conformant, and rejecting it would drop identity for a request
        # that is not malformed.
        ("lowercase `bearer` scheme", Ctx({"Authorization": "bearer abc"}), "abc"),
        ("bare token, no scheme", Ctx({"Authorization": "abc"}), "abc"),
        ("surrounding whitespace stripped",
         Ctx({"Authorization": "Bearer   abc  "}), "abc"),
    ]:
        got = caller_jwt(ctx)
        check(name, got == want, repr(got) if got == want else
              f"got {got!r}, expected {want!r}")

    print("\nFails closed — every case must yield the empty string, never a default:")
    for name, ctx in [
        # The live one: app.py sets request_headers only `if request_headers:`, so a
        # request with no forwardable headers arrives with None here.
        ("request_headers is None", Ctx(None)),
        ("no Authorization header at all", Ctx({"X-Other": "value"})),
        ("Authorization present but empty", Ctx({"Authorization": ""})),
        ("Authorization is whitespace only", Ctx({"Authorization": "   "})),
        # A context object without the attribute — a different SDK version, or a
        # locally-invoked handler. Must not raise; must not guess.
        ("context has no request_headers attribute", object()),
        # Not a Bearer credential. Accepting the second field of an arbitrary scheme
        # would feed a non-JWT into the verifier and rely on it to notice.
        ("Basic auth is not accepted", Ctx({"Authorization": "Basic dXNlcjpwYXNz"})),
    ]:
        got = caller_jwt(ctx)
        check(name, got == "", "" if got == "" else f"RETURNED {got!r} — must be ''")

    print("\nThe body is not an identity channel:")
    # Not a call into _caller_jwt: the property is that no payload field is consulted
    # anywhere. Asserted against the source text of invoke(), because the failure this
    # guards against is someone re-adding the read, not the function misbehaving.
    src = AGENT_PY.read_text()
    body_reads = re.findall(r"payload(?:\.get\(|\[)\s*[\"']access_token[\"']", src)
    check("agent.py reads no access_token from the payload", not body_reads,
          "no payload identity field" if not body_reads
          else f"FOUND {len(body_reads)}: {body_reads}")

    m = re.search(r"\n    user_jwt = (.+)", src)
    check("invoke() sources user_jwt from _caller_jwt(context)",
          bool(m) and m.group(1).strip() == "_caller_jwt(context)",
          m.group(1).strip() if m else "no `user_jwt =` assignment found")

    ask_py = (AGENT_PY.parents[1] / "client" / "ask.py").read_text()
    m = re.search(r"\n    body = (\{[^}]*\})", ask_py)
    check("client/ask.py sends no token in the request body",
          bool(m) and "access_token" not in m.group(1),
          m.group(1) if m else "no `body = {...}` assignment found")

    # The SDK passes the request context only if the entrypoint's SECOND parameter is
    # literally named "context" (`_takes_context`, bedrock_agentcore/runtime/app.py).
    # A rename is not a syntax error and not a type error — it silently stops identity
    # arriving, and the symptom is every delegated source unavailable with no message
    # naming the cause.
    m = re.search(r"async def invoke\(([^)]*)\)", src)
    params = [p.strip() for p in m.group(1).split(",")] if m else []
    check("entrypoint's 2nd parameter is named `context` (the SDK matches by name)",
          len(params) >= 2 and params[1] == "context",
          ", ".join(params) if params else "no `async def invoke(...)` found")

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    for f in FAIL:
        print(f"  FAILED: {f}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
