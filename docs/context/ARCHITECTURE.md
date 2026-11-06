# Architecture and safety invariants

## Verified baseline

The merged baseline has SQLite-backed plans and persisted confirmations with
restart recovery. It uses a durable gate before FixedFloat create, shared
fresh-order execution and existing-order reconciliation, periodic receipt
checks and proof-based handling of definitive reverts.
[Bot entrypoint and execution code](../../bot.py) ·
[Confirmation regressions](../../tests/test_confirmation_flow.py) ·
[Revert regressions](../../tests/test_new_transfer_reverts.py)

Startup recovery in main can take two paths with different effects:

- It can resume a transfer from persisted exact payment intent when the saved
  state and recovery checks permit it. This path may continue the transfer
  flow, including after a confirmed approval and a successful transfer claim.
  [Resume from persisted intent](../../bot.py#L2848-L2920)
- If the on-chain lookup returns pending and saved raw transaction bytes are
  available, recovery can rebroadcast those bytes. This occurs in the plan
  reconciliation path and can also occur for historical rows without a plan.
  [Plan-bound rebroadcast](../../bot.py#L2970-L2981) ·
  [Historical rebroadcast](../../bot.py#L3075-L3079)

These paths describe main. The proposed Stage 27 restrictions on restart
and rebroadcast still require implementation and verification.

## Required Round 3 behavior

These are acceptance criteria for the proposed correction. See [STATUS](STATUS.md)
for the evidence and current blockers.

1. Bind the authorized attempt to persisted ownership and payment evidence,
   including the order and token, user, recipient, funding account, network and exact
   amount. Loss of that right prevents nonce lookup, build and signing.
2. Commit the signed bytes, hash and nonce before broadcast. Protect the
   ownership check through the actual RPC POST, including the interval
   after prepared COMMIT. A writer that commits a conflicting change first
   must prevent the stale attempt from sending.
3. Allow at most one effective transfer per authorized intent. A permitted
   retry revalidates ownership for each actual RPC POST and uses the same
   committed bytes and nonce. Transport retries must respect the same rule.
4. Apply success or revert only when the pre-RPC snapshot still matches.
   A stale result cannot change transfer evidence, state or schedule, release
   the active-order gate or authorize a notification.
5. Preserve evidence and the active-order gate on ambiguous send. Recover
   through receipt observation or explicit manual resolution. Restart must not
   authorize a new transfer or rebroadcast an interrupted Stage 27 attempt.
   SQLite cannot retract a request already submitted to an RPC node.
6. Apply the same guarantees to fresh and resumed execution. Prove both using
   deterministic races, isolated SQLite, fake signing and fake RPC. Include
   worker cancellation, process restart and bounded contention when a
   concurrent writer or a slow RPC delays progress.

The architecture proposal uses a persisted attempt identity, SQLite write
serialization at the protected boundaries and conditional application of
receipts. Its safety and latency still need implementation evidence.
[Open work](BACKLOG.md) · [Decision status](DECISIONS.md)
