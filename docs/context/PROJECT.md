# Project brief

AutoDCA is a Telegram bot for recurring BTC purchases funded with USDT through
FixedFloat. Plans use intervals of 12, 24, 168 or 720 hours. Arbitrum, BSC and
Polygon are configured in source. Live provider availability is a separate check.
The execution scope includes scheduling, persisted confirmations, FixedFloat
order creation, manual funding, automatic transfers and recovery.
[Plan intervals](../../bot.py#L8335) · [Network configuration](../../networks.py)

## Repository and deployment roles

| Surface | Role |
| --- | --- |
| DEV, `~/Projects/Bitcoin-AutoDCA-Bot` | Development, tests and project documentation. Its working tree may contain uncommitted work. |
| [GitHub main](https://github.com/Bezhaltur/BTC_AutoDCA_bot/tree/main) | The merged-code baseline. DEV and RUNTIME use `Bezhaltur/BTC_AutoDCA_bot` as `origin`. The older clone URL in the README is historical. |
| RUNTIME, `~/Development/Auto_DCA_bot-main` | Separately operated release checkout. Its local configuration, live SQLite, logs, keystores and process belong to the running instance. |

DEV tests use isolated SQLite, fake services, test-only credentials and a
mocked or dedicated isolated keyring. The two checkouts currently use the same
Keychain service namespace in code. Switching checkouts leaves the macOS
Keychain shared. Agent sessions and tests must have no access to the production
wallet or its Keychain entries. [Operating rules](../../AGENTS.md) ·
[Current evidence](STATUS.md)

## Product direction

Close execution, recovery and financial-safety blockers before expanding the
Telegram interface. Product ideas are tracked separately in [BACKLOG](BACKLOG.md).
