# Next-session handoff

Start in DEV. Read [AGENTS.md](../../AGENTS.md) and the context files. Check
HEAD, remotes and working-tree drift before using the recorded evidence.
Preserve local Stage 27 edits and keep RUNTIME outside this work.

**Stage 27 Round 3 is BLOCKED.** [STATUS](STATUS.md) records the reproduced
failures and the separate retry coverage gap. [ARCHITECTURE](ARCHITECTURE.md)
records the proposed guarantees. This context is prepared for Draft PR #27,
whose published head covers workflow guardrails. Financial implementation is
tracked separately.

## Next action

Repeat independent read-only review of the corrected documentation diff.
Review of this documentation grants no authorization for financial
implementation. Stage 27 Round 3 remains BLOCKED and any implementation
requires separate, explicit authorization. [Stage 27 backlog](BACKLOG.md)

## Local review candidate

The temporary worktree is `/private/tmp/autodca-pr27-context.7D8hXV/worktree`.
Its base is the published Draft PR #27 head
`3ecb604b1daa0c82722b65a2341b399fc11f1875`. Verify the base and inspect the
complete candidate diff, including the seven new context files:

```sh
git -C /private/tmp/autodca-pr27-context.7D8hXV/worktree rev-parse HEAD
git -C /private/tmp/autodca-pr27-context.7D8hXV/worktree diff --name-status 3ecb604b1daa0c82722b65a2341b399fc11f1875 --
git -C /private/tmp/autodca-pr27-context.7D8hXV/worktree diff 3ecb604b1daa0c82722b65a2341b399fc11f1875 --
```

This temporary path may disappear. If unavailable, ask for the exact candidate
diff before reviewing it.

After each authorized stage, update [STATUS](STATUS.md) with evidence and
this handoff with one next action. Record changed decisions in
[DECISIONS](DECISIONS.md).
