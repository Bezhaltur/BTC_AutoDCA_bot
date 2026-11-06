# Status snapshot

Verified on 2026-11-06. **Stage 27 Round 3 — BLOCKED.**

## GitHub, DEV and RUNTIME

- **GitHub main and DEV HEAD** were verified at
  `25a50ac10b23742e202b543db893b3056a92b038`, the merge of PR #26.
  [GitHub baseline](https://github.com/Bezhaltur/BTC_AutoDCA_bot/commit/25a50ac10b23742e202b543db893b3056a92b038)
- **DEV** contains uncommitted Stage 27 changes in `bot.py` and four tracked
  tests, plus the untracked `tests/test_generic_send_outcomes.py` and
  `tests/test_stage27_round3_safety.py`. The documentation branch excludes that
  financial diff and those tests.
- **RUNTIME** was observed at the same tracked SHA on disk in the read-only
  inventory. The inventory identified its LaunchAgent and its own SQLite.
  Disk SHA and process identity establish different facts. Loaded Python
  source bytes and end-to-end behavior of this release were not independently
  attested by that inventory.
- **Draft PR #27** is the workflow and guardrails PR. The published head
  `3ecb604b1daa0c82722b65a2341b399fc11f1875` has a green CI run. The corrected
  documentation diff is local and has no CI result of its own.
  [Draft PR #27](https://github.com/Bezhaltur/BTC_AutoDCA_bot/pull/27) ·
  [CI run](https://github.com/Bezhaltur/BTC_AutoDCA_bot/actions/runs/42255560264)

## Stage 27 evidence

### Retained pytest evidence

Both runs were recorded on 2026-11-06 (Europe/Moscow) using Python 3.9.6 from
the DEV virtual environment and isolated snapshots. The commands below are
from the recorded run, with `python` denoting that interpreter.

| Snapshot and working directory | Command | Result | Primary log |
| --- | --- | --- | --- |
| Archive of `25a50ac10b23742e202b543db893b3056a92b038` in `/tmp/dca-reconcile.DXLd6R` | `python -B -m pytest tests -q -p no:cacheprovider --tb=short` | **664 passed** | `/tmp/dca-reconcile.DXLd6R/pytest-head.log` |
| DEV Python sources and tests copied from the worktree at `25a50ac10b23742e202b543db893b3056a92b038`, including uncommitted Stage 27, in `/tmp/dca-reconcile.DXLd6R/current` | `python -B -m pytest -q -p no:cacheprovider --tb=short` | **894 passed, 26 failed** | `/tmp/dca-reconcile.DXLd6R/current/pytest-report.log` |

The clean archive's 43 tracked files were checked against Git blobs. The
DEV snapshot's 28 Python files match DEV byte for byte at corrective review.
Its manifest SHA-256 is
`5b41c2f42083f47dcb2bb7e6648182fb786e5d28de92c03902b2948361acb07e`.
The manifest uses sorted relative paths for root and test-tree Python files,
with each line formatted as `<file SHA-256>  <relative path>` followed by a newline.
These logs are local and temporary. The corrective documentation round
inspected the retained evidence without rerunning pytest.

### Static inventory and observed failures

Static inspection of the parameterization in
`tests/test_stage27_round3_safety.py` identifies 26 cases across four groups,
each covering `fresh` and `resume`. Separately, the retained DEV pytest log
records 26 failures and names all 26 of those cases.

| Race group | Static cases | Logged failures |
| --- | ---: | ---: |
| Stale attempt starts preparation | 10 | 10 |
| Ownership or completion changes after prepared COMMIT and before broadcast | 4 | 4 |
| Late success changes newer evidence or terminal state | 6 | 6 |
| Immediate revert uses evidence captured after RPC | 6 | 6 |

Ownership loss between an ambiguous first send and RPC retry is an additional
coverage gap outside the 26 cases. Review both application and transport
retries. The Round 3 architecture is proposed. Its implementation, full Python
3.11 verification and independent review remain open. The workflow CI uses
Python 3.11. [Safety criteria](ARCHITECTURE.md) · [CI definition](../../.github/workflows/ci.yml)

## Historical reports

The 2026-09-22 report about deployed and runtime-verified PR #24 and the
2026-10-14 report of Stage 27 review readiness are historical reports.
Use the verified evidence above for current status. Record CI, independent
review, merge, deployment and runtime verification separately.
