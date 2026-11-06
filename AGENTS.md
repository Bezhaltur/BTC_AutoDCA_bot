# AutoDCA agent operating rules

This repository handles real cryptocurrency transactions. Treat every code change as potentially financial-impacting.

## Start in DEV

Confirm the repository, branch, HEAD, remotes and working-tree status before editing.
Read [PROJECT](docs/context/PROJECT.md), [STATUS](docs/context/STATUS.md),
[ARCHITECTURE](docs/context/ARCHITECTURE.md), [WORKFLOW](docs/context/WORKFLOW.md),
[BACKLOG](docs/context/BACKLOG.md), [DECISIONS](docs/context/DECISIONS.md) and
[HANDOFF](docs/context/HANDOFF.md). If a context file is unavailable, record the gap.
Start new sessions with read-only reconciliation. Distinguish current evidence,
historical reports and unknowns. Check GitHub for merged state and use separately
authorized runtime evidence for deployed state. Answer in Russian unless asked
otherwise. Keep paths, test names and commit hashes exact.

DEV is the development and test checkout. Use an isolated test environment with
a separate SQLite database, fake services, test-only credentials and a mocked or
dedicated isolated keyring. Switching checkouts leaves the macOS Keychain shared.
Agent sessions and tests must have no access to the production wallet or its
Keychain entries. Keep RUNTIME configuration, SQLite and WAL files, logs,
keystores and running processes outside DEV work. Never use RUNTIME as a source
of test data. Do not use the README Quick Start to launch the bot autonomously.

## Authorization boundaries

A read-only task stays read-only. It grants no permission to write files or
change Git state. Feature branches and draft pull requests describe the workflow
for authorized changes. Those conventions grant no permission to write or publish.
Obtain separate, explicit authorization before any commit, push, PR creation,
PR update, PR publication, merge or deploy. Documentation review grants no
authorization for financial implementation. That work requires its own explicit
authorization.

## Workflow
1. Work from a narrowly scoped GitHub issue with acceptance criteria and explicit out-of-scope boundaries.
2. Use a feature branch and a draft pull request; never commit directly to `main`.
3. Preserve existing working-tree changes and avoid unrelated refactors. Obtain explicit permission before any operation that would discard existing work.
4. Run `python -m pytest -q` and `python -m py_compile bot.py` before requesting review, or report exactly why they could not run.
5. Include changed files, testing evidence, remaining risks, and rollback notes in the PR.
6. Require a separate read-only reviewer, independent from the implementation agent, to review the exact candidate diff. Address findings and repeat review on the revised diff.
7. A human maintainer must approve merge and separately approve any PROD deployment. No autonomous release or production transaction. Production operations stay under human control.
8. At the end of each authorized stage, update `STATUS.md` with evidence and `HANDOFF.md` with the next action. Record new or changed decisions in `DECISIONS.md`.

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
Mark work READY FOR HUMAN REVIEW only after CI passes on the exact PR head, an independent review has no unresolved blocking findings, and the PR explains whether any financial path or deployment behavior changed. Otherwise mark BLOCKED and list the missing evidence. See [WORKFLOW](docs/context/WORKFLOW.md) for the release sequence.
