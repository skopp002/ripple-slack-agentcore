"""Regression tests for agent/gateway_tool.py's elicitation handling.

WHY THIS FILE EXISTS. When a GATEWAY-routed source needs the user's authorization, the
tools gateway does not answer 401. It answers a JSON-RPC error with code -32042 and the
consent URL buried in `data.elicitations[i].url`. Three things then have to hold, and all
three failed at once when this route was first exercised against the live gateway:

  1. THE URL SURVIVES. `McpError.__str__` is only "This request requires more
     information." — the URL is present on the object and invisible in every log line. It
     was dropped, and the route reported `ExceptionGroup searching GitHub via the
     gateway`, which reads as broken plumbing. A debugging session went after the MCP
     transport while the true state was "reachable, authenticated, awaiting one click".

  2. IT ARRIVES UNWRAPPED. `streamablehttp_client` is an anyio task-group context
     manager, so anything raised inside it leaves as an ExceptionGroup. A plain
     `except ElicitationRequired` at the call site does NOT match one, so the unwrapping
     in `_call` is what makes the type usable — and a refactor that moves the raise
     inside or outside the group boundary can silently undo it.

  3. IT STAYS A DISTINCT TYPE. agent.py must sort "the user must click something" into
     `auth_required` and "an operator must fix something" into `source_errors`. That
     distinction is the difference between telling a user to authorize and telling them
     to file a ticket. Sorting on substrings of an error message is how it gets lost.

WHAT IS NOT TESTED HERE: whether the gateway is reachable, and whether consent completes.
Both need the network and a human; this file is offline and imports no AWS SDK.

    python3 tests/test_gateway_elicitation.py

WHERE THIS SITS IN THE ARCHITECTURE DIAGRAMS. It guards the "Tools AgentCore Gateway"
tile's return leg — the response to 7a that says "not yet authorized". The diagrams draw
7a/8a as a call and its answer; the consent detour off that answer has no arrow, and is a
SECOND consent distinct from the ★ drawn in the consent flow (the runtime's vault and the
gateway target's are different workloads, so one click does not serve both).

Nothing here is deployed and no tile carries it.
"""
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "agent"))

import gateway_tool as g  # noqa: E402  (path must be set first)

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))


def _mcp_error(code, data):
    """An McpError lookalike: `.error` with `.code` and `.data`.

    Built as a stub rather than imported from `mcp` so the test runs without that
    dependency installed — the same reason gateway_tool imports it lazily. What is under
    test is our reading of the shape, not the library's rendering of it.
    """
    err = types.SimpleNamespace(code=code, message="This request requires more information.", data=data)
    exc = Exception("This request requires more information.")
    exc.error = err
    return exc


URL = "https://bedrock-agentcore.us-west-2.amazonaws.com/identities/oauth2/authorize?request_uri=urn%3Aabc"


def main() -> int:
    print("\n-- _elicitation_url: recognising the shape")

    check("extracts the url from a -32042 error",
          g._elicitation_url(_mcp_error(g.ELICITATION_CODE, {
              "elicitations": [{"mode": "url", "url": URL, "elicitationId": "x"}]})) == URL)

    # The code is the discriminator. Another error carrying a `url` in its data must not
    # be misread as a consent prompt: promoting a transport failure to "click here"
    # sends the user to authorize something that was never the problem.
    check("ignores a non-elicitation error that happens to carry a url",
          g._elicitation_url(_mcp_error(-32603, {"elicitations": [{"url": URL}]})) is None)

    check("returns None for an ordinary exception (no .error at all)",
          g._elicitation_url(RuntimeError("connection reset")) is None)

    # Defensive shapes: a handler that raises while explaining a failure replaces a
    # diagnosable error with a confusing one.
    for label, data in [("data is None", None),
                        ("data is not a dict", "nope"),
                        ("no elicitations key", {"other": 1}),
                        ("elicitations empty", {"elicitations": []}),
                        ("elicitation without a url", {"elicitations": [{"mode": "url"}]}),
                        ("elicitation not a dict", {"elicitations": ["nope"]})]:
        ok = True
        try:
            ok = g._elicitation_url(_mcp_error(g.ELICITATION_CODE, data)) is None
        except Exception as e:
            ok = False
            label += f" (raised {type(e).__name__})"
        check(f"tolerates malformed payload: {label}", ok)

    # First entry wins, deterministically. The gateway may offer several; picking one
    # arbitrarily would make the consent prompt vary between identical runs.
    check("takes the first elicitation with a url",
          g._elicitation_url(_mcp_error(g.ELICITATION_CODE, {"elicitations": [
              {"mode": "url"}, {"url": URL}, {"url": "https://second"}]})) == URL)

    print("\n-- _find_elicitation: surviving the anyio task group")

    e = g.ElicitationRequired(URL)
    check("finds a bare ElicitationRequired", g._find_elicitation(e) is e)

    # THE CASE THAT ACTUALLY BIT. `streamablehttp_client` wraps it one or more levels deep.
    grouped = ExceptionGroup("unhandled errors in a TaskGroup", [e])
    check("finds it inside an ExceptionGroup (the shape anyio produces)",
          g._find_elicitation(grouped) is e)
    check("finds it inside a NESTED ExceptionGroup",
          g._find_elicitation(ExceptionGroup("outer", [ExceptionGroup("inner", [e])])) is e)
    check("finds it among sibling exceptions",
          g._find_elicitation(ExceptionGroup("g", [RuntimeError("x"), e])) is e)

    wrapped = RuntimeError("wrapper")
    wrapped.__cause__ = e
    check("finds it through __cause__", g._find_elicitation(wrapped) is e)

    check("returns None when there is none to find",
          g._find_elicitation(ExceptionGroup("g", [RuntimeError("x"), ValueError("y")])) is None)

    # A cause cycle is constructible and would hang an unbounded walk. The depth bound
    # is the reason this returns at all.
    a, b = RuntimeError("a"), RuntimeError("b")
    a.__cause__, b.__cause__ = b, a
    ok = True
    try:
        g._find_elicitation(a)
    except RecursionError:
        ok = False
    check("terminates on a cyclic cause chain", ok)

    print("\n-- the type carries what the caller needs")
    check("ElicitationRequired exposes .url", g.ElicitationRequired(URL).url == URL)
    # If it stopped being an Exception subclass, `except Exception` fallbacks elsewhere
    # would stop covering it and the failure would escape the tool loop entirely.
    check("ElicitationRequired is an Exception", issubclass(g.ElicitationRequired, Exception))

    print("\n-- callers keep the distinction (source-text checks)")
    src = (Path(__file__).parents[1] / "agent" / "gateway_tool.py").read_text()

    # _scope() swallows every other failure by design (an empty scope still answers). If
    # it swallowed this one, the search call two lines later would raise the same
    # elicitation with the URL already discarded and an empty scope cached for the turn.
    check("_scope re-raises ElicitationRequired rather than swallowing it",
          "except ElicitationRequired:\n        # NOT swallowed" in src)

    # The hit-list return shape has no field for "the user must authorize"; returning a
    # {"error": ...} here is what hid the consent URL from agent.py in the first place.
    check("search_github_via_gateway lets ElicitationRequired propagate",
          "    except ElicitationRequired:\n        raise" in src)

    agent_src = (Path(__file__).parents[1] / "agent" / "agent.py").read_text()
    check("agent.py catches it before its generic `except Exception`",
          agent_src.index("except gateway_tool.ElicitationRequired")
          < agent_src.index("except Exception as e:  # surface auth/permission errors"))
    check("agent.py merges gateway consent into `auth_required`",
          "pending.update(gateway_consent)" in agent_src)
    # Reporting a source as connected while it is awaiting consent makes a fully
    # ungrounded answer exit 0 in ask.py, whose "nothing was searched" test reads this list.
    check("agent.py drops an awaiting-consent source from `connected_sources`",
          "[k for k in connected if k not in gateway_consent]" in agent_src)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    for f in FAIL:
        print(f"  FAILED: {f}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
