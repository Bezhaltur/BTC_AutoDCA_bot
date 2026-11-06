# Backlog

## P0 — Stage 27 Round 3 safety closure

**BLOCKED.** The initial reconciliation and the 26 existing failing cases are
recorded in [STATUS](STATUS.md). After implementation is authorized:

1. Close all four reproduced race groups in `fresh` and `resume`.
2. Add a deterministic case where ownership changes after an ambiguous first
   POST and before retry. Assert no second POST and no additional nonce lookup
   or signing. Cover hidden transport retries and a control case that retries
   the identical committed bytes while ownership remains valid.
3. Prove the guard under concurrent SQLite writers, cancellation and restart.
   Check recovery evidence and the time the guard holds under slow RPC.
4. Pass all 26 existing cases, the added regressions and the full Python 3.11
   suite. Obtain independent review on the exact diff and record release
   evidence through [WORKFLOW](WORKFLOW.md).

## Documentation automation — planned

Add a **required context CI check for pull requests** as a separately approved
automation change. The check should validate the eight expected paths,
relative links and evidence-backed STATUS/HANDOFF updates for completed stages.
The check and its required status are not implemented. Any workflow or
required-check setting change follows the approvals in [AGENTS.md](../../AGENTS.md).

## Later work

- Reconcile any remaining findings against their owning issue and current code.
- Revisit Telegram management and UI after the execution safety gates close.
  Strategy scheduling research is a separate product task.
