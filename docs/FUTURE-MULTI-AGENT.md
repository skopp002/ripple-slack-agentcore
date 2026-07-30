# Future Architecture — Multi-Agent + Shared (Episodic) Memory

Forward-looking design + TODOs for evolving Ripple from a single knowledge agent
into a **multi-agent system** that shares memory (notably **episodic** memory).
Nothing here is required for the MVP; it's the roadmap so today's build doesn't
box us in.

> Grounding: AgentCore Memory (SDK `MemorySessionManager`) already exposes the
> primitives this needs — **actors**, **sessions/events** (short-term), and
> **long-term memory records** organized by **namespace**, with
> `search_long_term_memories(...)` for semantic recall and strategy-based
> extraction. Shared episodic memory = a deliberately-scoped namespace that more
> than one agent is allowed to read (and, carefully, write).

---

## 1. Memory taxonomy (design this before scaling agents)

| Type | Scope | AgentCore mapping | Sharing rule |
|---|---|---|---|
| Short-term (working) | one session/turn | events / `get_last_k_turns` | never shared across users |
| Long-term semantic (facts/prefs) | per user | long-term records, namespace `user/{sub}/...` | private to the user |
| **Episodic** (past task episodes: goal→actions→outcome) | per user, cross-agent | long-term records, namespace `episodes/{sub}/...` | shared **read** across the user's agents |
| Org/shared knowledge (non-user-specific) | tenant | namespace `org/shared/...` | shared read; write gated |

**Golden rule (carry over from MVP):** memory is NEVER a permission oracle. A
shared episode may record "user asked about the roadmap and we cited SP-Roadmap",
but re-access always re-checks live source ACLs. Never store ACL-restricted
document *content* in a shared namespace.

### TODO — memory taxonomy
- [ ] Decide namespace convention now, e.g.
      `user/{sub}/facts`, `episodes/{sub}/{agent_or_shared}`, `org/shared/{topic}`.
- [ ] Add a `memory_scope` helper so every agent derives namespaces from the
      authenticated `sub` (prevents cross-user bleed by construction).
- [ ] Write a "what may be persisted" policy doc (no restricted content; store
      references/citations + outcomes, not payloads).

---

## 2. Episodic memory (the shared layer)

An **episode** = a structured record of one completed task:
`{goal, key_steps, tools_used, citations, outcome, satisfaction, ts}`.
Value: a second agent (e.g. a "report writer") can recall that the "knowledge
agent" already answered a related question for this user and reuse the grounded
result instead of re-retrieving.

### TODO — episodic memory
- [ ] Define the episode schema (JSON) and a single `write_episode()` used by all
      agents, tagged with `producer_agent`, `sub`, `namespace=episodes/{sub}/shared`.
- [ ] Choose an AgentCore Memory **extraction strategy** for episodes (summarization
      strategy over the session) vs. explicit `write_episode` calls. Start explicit
      (deterministic), add automatic extraction later.
- [ ] Implement `recall_episodes(sub, query)` via `search_long_term_memories`
      against the shared episodes namespace; inject top-k into the agent prompt as
      "prior related work (verify before reuse)".
- [ ] Add TTL / decay policy for episodes (align with the 90-day memory TTL).
- [ ] Add provenance: every reused episode must re-cite live sources, never quote
      the episode as if it were a source.

---

## 3. Multi-agent topology

Recommended starting shape: **supervisor / router → specialist agents**, each a
separate AgentCore Runtime behind the (existing) two-gateway pattern.

```
                       ┌──────────────► Knowledge Agent (Runtime)  ─┐
Auth0 JWT → Ingress GW → Supervisor/Router (Runtime) ─┼──► Report/Writer Agent      ├─► Tools GW → MCP tools
                       └──────────────► Action/Workflow Agent      ─┘
        shared read of episodes/{sub}/shared  ◄────────────────────┘
```

Key properties:
- The **same Auth0 user JWT** propagates to every agent (identity is uniform), so
  each agent's OBO tokens and memory namespaces are derived from one `sub`.
- Agents communicate either via the **supervisor** (A2A / orchestrator calls) or
  via **shared episodic memory** (loose coupling). Prefer shared memory for
  "what happened" and direct calls for "do this now".

### TODO — topology
- [ ] Pick coordination style: (a) supervisor orchestrator agent, or (b) AgentCore
      **Gateway with multiple Runtime targets** + a router. Start with (a).
- [ ] Define an agent registry (name, purpose, allowed tools, allowed namespaces).
- [ ] Enforce **least-privilege memory**: each agent's role limits which namespaces
      it can read/write (supervisor can read episodes; specialists write only their own).
- [ ] Add A2A/tracing correlation id so a multi-agent task is one trace in CloudWatch.

---

## 4. Concurrency & consistency (shared writes are the hazard)

- [ ] Decide write policy for shared namespaces: **single-writer per episode**
      (the producing agent) to avoid races; others read-only.
- [ ] Idempotency: episode ids derived from `(sub, task_id)` so retries don't dup.
- [ ] Conflict handling for org/shared facts: last-writer-wins vs. versioned; log
      writer identity for audit.
- [ ] Load test `search_long_term_memories` latency at expected fan-out; cache hot
      recalls per turn.

---

## 5. Security & privacy (multi-agent amplifies leakage risk)

- [ ] Re-affirm: shared memory stores references + outcomes, NOT restricted content.
- [ ] Namespace isolation test: agent for user A can never read `user/{B}/*` or
      `episodes/{B}/*` (add an automated cross-user assertion, like the MVP leakage test).
- [ ] Every agent independently re-checks source ACLs at retrieval time; a shared
      episode grants no data access.
- [ ] Extend the leakage-safety eval to multi-agent traces (a downstream agent must
      not surface something the user lost access to since the episode was written).

---

## 6. Observability & evals for multi-agent

- [ ] One correlated trace per user task spanning all agents (propagate trace id).
- [ ] Per-agent eval scores + a task-level (end-to-end) score.
- [ ] Add an eval: "did episode reuse improve latency/quality without changing the
      permission-correct answer set?"

---

## 7. Suggested sequencing (don't build it all at once)
1. Ship single knowledge agent (current MVP).
2. Add per-user long-term memory (facts/prefs) — private namespaces.
3. Introduce episodic memory for the single agent (write + recall, private).
4. Add a second specialist agent that **reads** the user's shared episodes (read-only).
5. Add a supervisor/router; formalize the agent registry + namespace ACLs.
6. Add shared org namespaces + concurrency controls.
7. Extend evals/observability to multi-agent, then optimization flywheel per agent.
