# Working agreement

[AGENTS.md](../../AGENTS.md) defines permissions and mandatory gates. Work in DEV.
Read-only tasks stay read-only. Git writes, PR actions and deployment require
the separate authorizations defined there. RUNTIME verification and production
operations require separate scope.

## Review and release sequence

1. Scope one change and state measurable acceptance criteria. Capture Git
   identity, existing changes and the exact baseline.
2. Gather read-only evidence. Record verified findings, hypotheses and unknowns
   separately. Implement an authorized change in isolation from unrelated work.
3. Report the file list, test commands and results, behavior changes, remaining
   risks and rollback constraints. For financial work include fresh and resume,
   prepared COMMIT to broadcast, RPC retry, late receipts and restart.
4. An authorized draft PR can collect CI evidence. Before marking it
   `READY FOR HUMAN REVIEW`, require green CI on the exact head and an
   independent read-only review of that diff with no unresolved blockers.
   Repeat review after relevant changes.
5. A human maintainer approves merge. Any PROD rollout requires separate human
   approval. Record merge, deployment and runtime verification as separate
   events with their own evidence.
6. At the end of each authorized stage, update [STATUS](STATUS.md) with the
   outcome and evidence and [HANDOFF](HANDOFF.md) with one next action.
   Update [DECISIONS](DECISIONS.md) when a decision is added or changes.

## Context validation

A mandatory context CI check on pull requests is planned. The current CI
runs pytest and compiles `bot.py`. Until the context check exists, review
document completeness, links and consistency manually and report any gaps.
[CI definition](../../.github/workflows/ci.yml) · [Automation backlog](BACKLOG.md)
