"""Guard the DUPLICATED system prompt and the harness template's shape.

    python3 tests/test_harness_prompt.py

WHY THIS EXISTS. Moving to a managed AgentCore Harness turns the system prompt from CODE
into CONFIGURATION: on the runtime it is `SYSTEM_PROMPT` in agent/agent.py, and on the
harness it is `SystemPrompt` in infra/04-harness.yaml. Both paths ship, so the same rules
now live in two files — and that duplication cannot be removed, because a CloudFormation
template cannot import a Python constant and the harness never runs our container.

The rules that are duplicated are the ones that keep the agent honest: cite everything,
answer only from retrieved content, never imply an unsearched source was empty, and stamp
a confidence band. Drift here does not fail anything. It degrades ONE path's grounding
discipline while the other stays correct, which is the hardest kind of regression to
notice: both agents answer, both look plausible, and only one is still citing properly.

So this does not compare the two prompts word for word — they legitimately differ (the
runtime's names our three in-process tools, which do not exist on the harness). It asserts
that every LOAD-BEARING RULE is present in both. Rewording is free; dropping a rule fails.

It also pins three template facts that are silent when wrong:
  - AllowedTools must be non-empty, or the harness grants `shell` and `file_operations`
    in every session. A document-reading agent does not need a shell, and a prompt
    injection carried inside a retrieved document does.
  - Memory.ActorId must NOT be set at the template level — that would pin every caller to
    one memory partition, i.e. one shared conversation history across all users.
  - There must be no EnvironmentArtifact pointing at our image, because it would imply
    agent.py runs on the harness. It does not; the harness overrides ENTRYPOINT and CMD.

WHERE THIS SITS IN THE ARCHITECTURE DIAGRAM (infra/architecture-components.png): nowhere,
like the other tests — nothing here is deployed. It guards the text inside the managed
harness tile, and the two properties on it (no shell, no shared memory partition) that are
security decisions rather than tuning.
"""
import re
import sys
from pathlib import Path

SOLUTION = Path(__file__).parents[1]
AGENT_PY = SOLUTION / "agent" / "agent.py"
HARNESS_YAML = SOLUTION / "infra" / "04-harness.yaml"

PASS: list[str] = []
FAIL: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    """`detail` explains a FAILURE, so it prints only on failure.

    Printed on PASS too, at first, which made every green line read as the warning it
    describes ("MISSING from ..." next to PASS). A test whose output has to be
    re-interpreted is a test that gets ignored.
    """
    (PASS if ok else FAIL).append(label)
    print(f"  {'PASS' if ok else 'FAIL'}  {label}"
          + (f"  — {detail}" if detail and not ok else ""))


def _agent_prompt() -> str:
    """Extract SYSTEM_PROMPT's text without importing agent.py.

    Imported, agent.py pulls in bedrock_agentcore, strands and boto3 and reads env vars
    at module scope — none of which is needed to read a string literal, and all of which
    would make this test fail for reasons unrelated to the prompt.
    """
    src = AGENT_PY.read_text()
    m = re.search(r'^SYSTEM_PROMPT\s*=\s*"""(.*?)"""', src, re.S | re.M)
    if not m:
        raise SystemExit("FATAL: could not find SYSTEM_PROMPT in agent/agent.py. If it "
                         "was renamed or moved, update this test — do not delete it.")
    return m.group(1)


def _harness_prompt() -> str:
    """Extract the SystemPrompt block literal from the YAML, without a YAML parser.

    Deliberately not `yaml.safe_load`: PyYAML is not in requirements.txt (nothing at
    runtime parses YAML), and the template contains CFN short tags like `!Ref` and `!If`
    that safe_load rejects outright. A block scalar is easy enough to slice.
    """
    src = HARNESS_YAML.read_text()
    m = re.search(r'^      SystemPrompt:\n        - Text: \|\n(.*?)(?=\n      \S)',
                  src, re.S | re.M)
    if not m:
        raise SystemExit("FATAL: could not find the SystemPrompt block in "
                         "infra/04-harness.yaml. If its indentation changed, update "
                         "this test — do not delete it.")
    return m.group(1)


# Each rule is (label, runtime pattern, harness pattern) — separate patterns because the
# two prompts word things differently on purpose. Matched case-insensitively against
# whitespace-collapsed text so reflowing a paragraph does not fail the test.
RULES = [
    ("cites every claim",
     r"cite (every|all)", r"cite (every|all)"),
    ("answers only from retrieved content",
     r"only from", r"only from"),
    ("must call a tool before answering",
     r"call a tool before", r"call a tool before"),
    ("never invents facts",
     r"never invent", r"never invent"),
    ("does not imply an unsearched source was empty",
     r"(do not|never).{0,80}(imply|conclude)", r"(do not|never).{0,80}(imply|conclude)"),
    ("declares a confidence band",
     r"confidence", r"confidence"),
    ("offers all three bands",
     r"HIGH.{0,40}MEDIUM.{0,40}LOW", r"HIGH.{0,40}MEDIUM.{0,40}LOW"),
    ("emits a Sources list",
     r"Sources:", r"Sources:"),
]


def main() -> int:
    agent = re.sub(r"\s+", " ", _agent_prompt())
    harness = re.sub(r"\s+", " ", _harness_prompt())
    yaml_src = HARNESS_YAML.read_text()

    print("\nsystem prompt rules present in BOTH prompts")
    for label, a_pat, h_pat in RULES:
        in_a = re.search(a_pat, agent, re.I) is not None
        in_h = re.search(h_pat, harness, re.I) is not None
        missing = [n for n, v in (("agent.py", in_a), ("04-harness.yaml", in_h)) if not v]
        check(label, in_a and in_h,
              "" if not missing else f"MISSING from {' and '.join(missing)}")

    # ⚠️ Harness-only, and the reason it is not symmetric is worth stating: on the
    # runtime, gateway_tool.py::_scope RAISES when it cannot build a `user:`/`org:`
    # qualifier, so an unscoped GitHub search is impossible regardless of the prompt. The
    # harness calls the gateway's tools directly with no such code in the path, so the
    # prompt is the ONLY thing standing between it and a search of all public GitHub
    # returning strangers' repositories as company documents. That defect was observed
    # live on the runtime before _scope was hardened; this path can reintroduce it.
    print("\nharness-only rules (no code enforces these on that path)")
    check("restricts code search to the caller's own accounts/orgs",
          re.search(r"(restrict|scope).{0,80}(own account|account or org)", harness,
                    re.I) is not None,
          "SCOPE DISCIPLINE paragraph missing — unscoped search searches all of GitHub")

    print("\ntemplate properties that are silent when wrong")
    # Only the resource body matters; the header comments discuss all of these at
    # length, so match on the property lines rather than anywhere in the file.
    body = yaml_src.split("Resources:", 1)[-1]
    check("AllowedTools is set and non-empty (no default shell/file_operations)",
          re.search(r"^      AllowedTools:\n\s+- ", body, re.M) is not None,
          "an unset AllowedTools grants shell + file_operations in every session")
    check("Memory.ActorId is NOT pinned in the template",
          re.search(r"^\s+ActorId:", body, re.M) is None,
          "a template-level ActorId shares ONE conversation history across all users")
    check("no EnvironmentArtifact (the harness would not run it anyway)",
          re.search(r"^      EnvironmentArtifact:", body, re.M) is None,
          "the harness overrides ENTRYPOINT/CMD, so agent.py would never execute")
    # TOKEN_EXCHANGE is valid in the API but absent from the CFN enum for this field, so
    # a template that OFFERS it fails at change-set time with a schema error.
    # Scoped to AllowedValues lines only: the header and the parameter Description
    # discuss TOKEN_EXCHANGE at length precisely so nobody adds it back, and a naive
    # substring search over the whole file flags that documentation as the defect.
    allowed_values = re.findall(r"^\s+AllowedValues:.*$", yaml_src, re.M)
    check("GrantType AllowedValues offers no TOKEN_EXCHANGE (not in the CFN enum)",
          not any("TOKEN_EXCHANGE" in line for line in allowed_values),
          "CFN's OAuthCredentialProvider.GrantType accepts only CLIENT_CREDENTIALS "
          "and AUTHORIZATION_CODE, so offering it fails at change-set time")

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    for f in FAIL:
        print(f"  FAILED: {f}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
