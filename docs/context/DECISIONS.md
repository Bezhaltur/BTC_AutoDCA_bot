# Decisions and open questions

## Verified working boundaries — 2026-11-06

- DEV owns development and tests. RUNTIME owns release execution and live data.
  GitHub `origin/main` is the merged-code source. See [PROJECT](PROJECT.md)
  for the paths and [STATUS](STATUS.md) for the verified snapshot.
- Persisted confirmation and restart recovery exist in main and have
  regression coverage. [Confirmation tests](../../tests/test_confirmation_flow.py)
- [AGENTS.md](../../AGENTS.md) owns mandatory operating policy.
  [WORKFLOW](WORKFLOW.md) describes how to apply the review and release gates.

## Proposed and open

- Round 3 ownership protection is a design proposal. The persisted attempt,
  SQLite write serialization and conditional receipt update still need
  implementation, contention checks and independent review.
  [Acceptance criteria](ARCHITECTURE.md)
- A required context check on pull requests is planned. Its rules and rollout
  are open. [Automation backlog](BACKLOG.md)

Record new decisions with date, decision, rationale, evidence, owner and status.
Keep changing implementation and verification results in [STATUS](STATUS.md).
