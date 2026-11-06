# AutoDCA agent operating rules

This repository handles real cryptocurrency transactions. Treat every code change as potentially financial-impacting.

## Workflow
1. Work from a narrowly scoped GitHub issue with acceptance criteria and explicit out-of-scope boundaries.
2. Use a feature branch and a draft pull request; never commit directly to `main`.
3. Preserve existing working-tree changes and avoid unrelated refactors.
4. Run `python -m pytest -q` and `python -m py_compile bot.py` before requesting review, or report exactly why they could not run.
5. Include changed files, testing evidence, remaining risks, and rollback notes in the PR.
6. Require a separate read-only reviewer, independent from the implementation agent. Address findings and repeat review after corrections.
7. A human maintainer must approve merge and any PROD deployment. No autonomous release or production transaction.

## Safety constraints
- Do not use real private keys, API credentials, wallet seeds, or production transaction endpoints in development, tests, CI, or agent sessions.
- Do not inspect or print secret values; do not add secrets to issues, PRs, logs, fixtures, or prompts.
- Do not broadcast transactions, send funds, create live exchange orders, or trigger production automation.
- Never weaken authorization, ownership checks, transaction-intent validation, idempotency, persisted evidence, crash recovery, or prepared-commit-to-broadcast protections.
- In particular, protect against evidence/ownership changes between prepared persistence and broadcast, late success after terminal transitions, duplicate transfers, and unsafe retries.
- Do not classify a financial-path change as safe only because tests pass. Require explicit behavioral evidence for fresh and resumed execution.
- Never self-approve, bypass required checks, change branch protection, or merge/ship your own implementation.
- Changes to `.github/workflows/`, permissions, dependency installation, or these agent rules require human security review.

## Completion criteria
Mark work READY FOR HUMAN REVIEW only after CI passes, an independent review has no unresolved blocking findings, and the PR explains whether any financial path or deployment behavior changed. Otherwise mark BLOCKED and list the missing evidence.
