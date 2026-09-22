import asyncio
import sqlite3
from decimal import Decimal
from types import SimpleNamespace

import pytest

import bot as app


USER_ID = 10001
NETWORK = "USDT-ARB"
BTC_ADDRESS = "bc1qlegacyreconfirmation00000000000000000"


class FakeMessage:
    def __init__(self, text):
        self.text = text
        self.from_user = SimpleNamespace(id=USER_ID)
        self.answers = []

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))
        return SimpleNamespace(message_id=len(self.answers))


class FakeCallback:
    def __init__(self, plan_id, scheduled_at):
        self.data = f"dca_confirm:{plan_id}:{scheduled_at}"
        self.from_user = SimpleNamespace(id=USER_ID)
        self.message = SimpleNamespace(message_id=77)
        self.answers = []

    async def answer(self, text=None, **kwargs):
        self.answers.append((text, kwargs))


class FakeBot:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))
        if self.fail:
            raise RuntimeError("injected Telegram failure")
        return SimpleNamespace(message_id=len(self.sent))

    async def edit_message_text(self, **kwargs):
        return SimpleNamespace(message_id=kwargs.get("message_id"))


@pytest.fixture
def legacy_db(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy-reconfirm.sqlite3"
    monkeypatch.setattr(app, "DB_PATH", str(db_path))
    asyncio.run(app.init_db())

    async def limits(_network):
        return {"min": Decimal("10"), "max": Decimal("500"), "rate": None}

    monkeypatch.setattr(app, "get_fixedfloat_limits", limits)
    return db_path


def seed_plan(
    db_path,
    *,
    amount=25.125,
    amount_text=None,
    active=1,
    next_run=1_700_000_000,
    state="scheduled",
    skip_reason=None,
    active_order_id=None,
):
    with sqlite3.connect(db_path) as db:
        plan_id = db.execute(
            "INSERT INTO dca_plans("
            "user_id,from_asset,amount,amount_text,interval_hours,btc_address,next_run,"
            "active,deleted,execution_state,skip_reason,missed_count,last_tx_hash,"
            "active_order_id,active_order_token,active_order_address,active_order_amount,"
            "active_order_expires,confirmation_message_id,confirmation_expires_at,"
            "confirmation_scheduled_at) "
            "VALUES(?,?,?,?,?,?,?,?,0,?,?,7,'legacy-last-hash',?,?,?,?,?,?,?,?)",
            (
                USER_ID,
                NETWORK,
                amount,
                amount_text,
                24,
                BTC_ADDRESS,
                next_run,
                active,
                state,
                skip_reason,
                active_order_id,
                "active-token" if active_order_id else None,
                "0xactive" if active_order_id else None,
                "25.125" if active_order_id else None,
                1_800_000_000 if active_order_id else None,
                None,
                None,
                None,
            ),
        ).lastrowid
        db.commit()
    return int(plan_id)


def seed_transaction(db_path, plan_id, state):
    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT INTO sent_transactions("
            "user_id,plan_id,order_id,order_token,network_key,amount,deposit_address,state) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                USER_ID,
                plan_id,
                f"order-{state}",
                "token",
                NETWORK,
                25.125,
                "0xdeposit",
                state,
            ),
        )
        db.commit()


def plan_row(db_path, plan_id):
    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        return dict(
            db.execute("SELECT * FROM dca_plans WHERE id = ?", (plan_id,)).fetchone()
        )


def typed_plan_snapshot(db_path, plan_id, *, exclude=()):
    with sqlite3.connect(db_path) as db:
        columns = [
            row[1]
            for row in db.execute("PRAGMA table_info(dca_plans)")
            if row[1] not in set(exclude)
        ]
        selection = ", ".join(
            [f'"{column}"' for column in columns]
            + [f'typeof("{column}")' for column in columns]
        )
        values = db.execute(
            f"SELECT {selection} FROM dca_plans WHERE id = ?", (plan_id,)
        ).fetchone()
    return tuple(columns), tuple(values)


def assert_writer_lock_available(db_path):
    with sqlite3.connect(db_path, timeout=0.1) as db:
        db.execute("BEGIN IMMEDIATE")
        db.rollback()


def run_scheduler_iteration(monkeypatch):
    real_sleep = asyncio.sleep

    async def stop_after_iteration(delay):
        if delay == 60:
            raise asyncio.CancelledError
        await real_sleep(0)

    monkeypatch.setattr(app.asyncio, "sleep", stop_after_iteration)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(app.dca_scheduler())


def test_scheduler_auto_pauses_once_and_notifies_after_commit(
    legacy_db, monkeypatch
):
    now = 1_700_100_000
    plan_id = seed_plan(legacy_db, next_run=now - 30)
    fake_bot = FakeBot()
    monkeypatch.setattr(app, "bot", fake_bot)
    monkeypatch.setattr(app.time, "time", lambda: now)

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("legacy auto-pause reached fresh execution")

    monkeypatch.setattr(app, "create_dca_confirmation_request", forbidden)
    monkeypatch.setattr(app, "create_fixedfloat_order", forbidden)
    monkeypatch.setattr(app, "auto_send_usdt", forbidden)

    original_send = fake_bot.send_message

    async def assert_committed_before_send(chat_id, text, **kwargs):
        row = plan_row(legacy_db, plan_id)
        assert row["active"] == 0
        assert row["skip_reason"] == app.EXACT_AMOUNT_RECONFIRMATION_REASON
        return await original_send(chat_id, text, **kwargs)

    fake_bot.send_message = assert_committed_before_send
    run_scheduler_iteration(monkeypatch)

    first = plan_row(legacy_db, plan_id)
    assert first["active"] == 0
    assert first["amount_text"] is None
    assert first["skip_reason"] == app.EXACT_AMOUNT_RECONFIRMATION_REASON
    assert first["missed_count"] == 7
    assert len(fake_bot.sent) == 1
    assert "Покупка не выполнялась" in fake_bot.sent[0][1]

    run_scheduler_iteration(monkeypatch)
    assert plan_row(legacy_db, plan_id) == first
    assert len(fake_bot.sent) == 1


def test_scheduler_notification_failure_keeps_durable_pause(legacy_db, monkeypatch):
    now = 1_700_100_000
    plan_id = seed_plan(legacy_db, next_run=now - 1)
    fake_bot = FakeBot(fail=True)
    monkeypatch.setattr(app, "bot", fake_bot)
    monkeypatch.setattr(app.time, "time", lambda: now)

    run_scheduler_iteration(monkeypatch)
    row = plan_row(legacy_db, plan_id)
    assert row["active"] == 0
    assert row["skip_reason"] == app.EXACT_AMOUNT_RECONFIRMATION_REASON
    assert len(fake_bot.sent) == 1

    fake_bot.fail = False
    run_scheduler_iteration(monkeypatch)
    assert len(fake_bot.sent) == 1


@pytest.mark.parametrize(
    ("active", "reason", "next_run", "expected_active", "expected_next", "expected_reason"),
    [
        (
            0,
            app.EXACT_AMOUNT_RECONFIRMATION_REASON,
            1_600_000_000,
            1,
            1_700_000_000 + 24 * 3600,
            None,
        ),
        (0, "manual_pause", 1_600_000_000, 0, 1_600_000_000, "manual_pause"),
        (1, None, 1_800_000_000, 1, 1_800_000_000, None),
    ],
)
def test_reconfirm_updates_in_place_and_preserves_unrelated_typed_state(
    legacy_db,
    monkeypatch,
    active,
    reason,
    next_run,
    expected_active,
    expected_next,
    expected_reason,
):
    now = 1_700_000_000
    monkeypatch.setattr(app.time, "time", lambda: now)
    plan_id = seed_plan(
        legacy_db, active=active, skip_reason=reason, next_run=next_run
    )
    excluded = {"amount", "amount_text", "active", "next_run", "skip_reason"}
    before = typed_plan_snapshot(legacy_db, plan_id, exclude=excluded)

    message = FakeMessage("/reconfirm_1 25.5000")
    asyncio.run(app.cmd_reconfirm(message))

    row = plan_row(legacy_db, plan_id)
    assert row["id"] == plan_id
    assert row["amount_text"] == "25.5"
    assert row["amount"] == 25.5
    assert row["active"] == expected_active
    assert row["next_run"] == expected_next
    assert row["skip_reason"] == expected_reason
    assert row["missed_count"] == 7
    assert typed_plan_snapshot(legacy_db, plan_id, exclude=excluded) == before
    with sqlite3.connect(legacy_db) as db:
        assert db.execute("SELECT COUNT(*) FROM dca_plans").fetchone()[0] == 1


@pytest.mark.parametrize(
    "amount_text",
    ["1e1", "NaN", "Infinity", "10.0000000000000000001", "0", "-10", "9"],
)
def test_invalid_reconfirmation_input_never_mutates(
    legacy_db, monkeypatch, amount_text
):
    plan_id = seed_plan(legacy_db, active=0)
    before = typed_plan_snapshot(legacy_db, plan_id)
    message = FakeMessage(f"/reconfirm_1 {amount_text}")
    asyncio.run(app.cmd_reconfirm(message))
    assert typed_plan_snapshot(legacy_db, plan_id) == before


def test_provider_limit_rejection_does_not_mutate(legacy_db, monkeypatch):
    plan_id = seed_plan(legacy_db, active=0)
    before = typed_plan_snapshot(legacy_db, plan_id)

    async def narrow_limits(_network):
        return {"min": Decimal("31"), "max": Decimal("500"), "rate": None}

    monkeypatch.setattr(app, "get_fixedfloat_limits", narrow_limits)
    asyncio.run(app.cmd_reconfirm(FakeMessage("/reconfirm_1 30")))
    assert typed_plan_snapshot(legacy_db, plan_id) == before


@pytest.mark.parametrize(
    "state",
    ["awaiting_confirmation", "confirming", "claiming", "creating_order"],
)
def test_live_execution_states_block_reconfirmation(legacy_db, monkeypatch, state):
    plan_id = seed_plan(legacy_db, active=0, state=state)
    before = typed_plan_snapshot(legacy_db, plan_id)
    asyncio.run(app.cmd_reconfirm(FakeMessage("/reconfirm_1 30")))
    assert typed_plan_snapshot(legacy_db, plan_id) == before


def test_active_order_blocks_reconfirmation_without_clearing_gate(
    legacy_db, monkeypatch
):
    plan_id = seed_plan(legacy_db, active=0, active_order_id="active-order")
    before = typed_plan_snapshot(legacy_db, plan_id)
    asyncio.run(app.cmd_reconfirm(FakeMessage("/reconfirm_1 30")))
    assert typed_plan_snapshot(legacy_db, plan_id) == before


@pytest.mark.parametrize("state", ["sending", "transfering", "approve_confirmed", "tx_pending", "pending", "blocked"])
def test_nonterminal_transaction_evidence_blocks_reconfirmation(
    legacy_db, monkeypatch, state
):
    plan_id = seed_plan(legacy_db, active=0)
    seed_transaction(legacy_db, plan_id, state)
    asyncio.run(app.cmd_reconfirm(FakeMessage("/reconfirm_1 30")))
    assert plan_row(legacy_db, plan_id)["amount_text"] is None


@pytest.mark.parametrize("state", ["sent", "confirmed", "failed", "expired"])
def test_terminal_history_including_sent_permits_reconfirmation(
    legacy_db, monkeypatch, state
):
    plan_id = seed_plan(legacy_db, active=0)
    seed_transaction(legacy_db, plan_id, state)
    asyncio.run(app.cmd_reconfirm(FakeMessage("/reconfirm_1 30")))
    assert plan_row(legacy_db, plan_id)["amount_text"] == "30"


def test_reconfirmation_preserves_terminal_transaction_evidence_byte_for_byte(
    legacy_db, monkeypatch
):
    plan_id = seed_plan(legacy_db, active=0)
    seed_transaction(legacy_db, plan_id, "sent")
    with sqlite3.connect(legacy_db) as db:
        db.execute(
            "UPDATE sent_transactions SET approve_tx_hash=?, approve_tx_nonce=?, "
            "approve_raw_tx=?, transfer_tx_hash=?, transfer_tx_nonce=?, transfer_raw_tx=?, "
            "amount_units=?, token_decimals=?, error_message=? WHERE plan_id=?",
            (
                "0xapprove",
                7,
                "0xrawapprove00",
                "0xtransfer",
                8,
                "0xrawtransfer00",
                "30125000",
                6,
                "historical diagnostic",
                plan_id,
            ),
        )
        columns = [row[1] for row in db.execute("PRAGMA table_info(sent_transactions)")]
        selection = ", ".join(
            [f'"{column}"' for column in columns]
            + [f'typeof("{column}")' for column in columns]
        )
        before = db.execute(
            f"SELECT {selection} FROM sent_transactions WHERE plan_id=?", (plan_id,)
        ).fetchone()
        db.commit()

    asyncio.run(app.cmd_reconfirm(FakeMessage("/reconfirm_1 30.125")))

    with sqlite3.connect(legacy_db) as db:
        after = db.execute(
            f"SELECT {selection} FROM sent_transactions WHERE plan_id=?", (plan_id,)
        ).fetchone()
    assert after == before


def test_duplicate_exact_sibling_blocks_in_place_reconfirmation(
    legacy_db, monkeypatch
):
    legacy_id = seed_plan(legacy_db, active=0)
    seed_plan(legacy_db, amount=30.0, amount_text="30", active=1)
    asyncio.run(app.cmd_reconfirm(FakeMessage("/reconfirm_1 30.0")))
    assert plan_row(legacy_db, legacy_id)["amount_text"] is None


def test_concurrent_reconfirmations_serialize_duplicate_guard(
    legacy_db, monkeypatch
):
    first_id = seed_plan(legacy_db, active=0)
    second_id = seed_plan(legacy_db, active=0)

    async def scenario():
        return await asyncio.gather(
            app._reconfirm_legacy_plan(
                plan_id=first_id,
                user_id=USER_ID,
                expected_network=NETWORK,
                canonical_amount="30",
                reconfirmed_at=1_700_000_000,
            ),
            app._reconfirm_legacy_plan(
                plan_id=second_id,
                user_id=USER_ID,
                expected_network=NETWORK,
                canonical_amount="30",
                reconfirmed_at=1_700_000_000,
            ),
        )

    outcomes = {result.outcome for result in asyncio.run(scenario())}
    assert outcomes == {"reconfirmed", "duplicate"}
    with sqlite3.connect(legacy_db) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM dca_plans WHERE amount_text = '30'"
        ).fetchone()[0] == 1


def test_scheduler_pause_guard_preserves_active_financial_evidence(
    legacy_db, monkeypatch
):
    active_order_id = seed_plan(
        legacy_db, active=1, active_order_id="still-active"
    )
    unresolved_id = seed_plan(legacy_db, active=1)
    seed_transaction(legacy_db, unresolved_id, "tx_pending")

    async def scenario():
        return (
            await app._auto_pause_legacy_plan_for_reconfirmation(
                active_order_id, USER_ID
            ),
            await app._auto_pause_legacy_plan_for_reconfirmation(
                unresolved_id, USER_ID
            ),
        )

    assert asyncio.run(scenario()) == (False, False)
    assert plan_row(legacy_db, active_order_id)["active"] == 1
    assert plan_row(legacy_db, unresolved_id)["active"] == 1


def test_scheduler_pause_guard_treats_historical_sent_as_terminal(
    legacy_db, monkeypatch
):
    plan_id = seed_plan(legacy_db, active=1)
    seed_transaction(legacy_db, plan_id, "sent")
    assert asyncio.run(
        app._auto_pause_legacy_plan_for_reconfirmation(plan_id, USER_ID)
    ) is True
    row = plan_row(legacy_db, plan_id)
    assert row["active"] == 0
    assert row["skip_reason"] == app.EXACT_AMOUNT_RECONFIRMATION_REASON


def test_individual_and_bulk_resume_never_activate_exactless_plans(
    legacy_db, monkeypatch
):
    legacy_id = seed_plan(legacy_db, active=0)
    exact_id = seed_plan(legacy_db, amount=40.0, amount_text="40", active=0)

    single = FakeMessage("/resume_1")
    asyncio.run(app.cmd_resume(single))
    assert plan_row(legacy_db, legacy_id)["active"] == 0
    assert "reconfirm_1" in single.answers[-1][0]

    bulk = FakeMessage("/resume")
    asyncio.run(app.cmd_resume(bulk))
    assert plan_row(legacy_db, legacy_id)["active"] == 0
    assert plan_row(legacy_db, exact_id)["active"] == 1
    assert "пропущено: 1" in bulk.answers[-1][0]


def test_explicit_pause_converts_auto_pause_into_manual_pause(
    legacy_db, monkeypatch
):
    next_run = 1_600_000_000
    plan_id = seed_plan(
        legacy_db,
        active=0,
        next_run=next_run,
        skip_reason=app.EXACT_AMOUNT_RECONFIRMATION_REASON,
    )
    asyncio.run(app.cmd_pause(FakeMessage("/pause_1")))
    paused = plan_row(legacy_db, plan_id)
    assert paused["active"] == 0
    assert paused["skip_reason"] is None

    asyncio.run(app.cmd_reconfirm(FakeMessage("/reconfirm_1 30")))
    reconfirmed = plan_row(legacy_db, plan_id)
    assert reconfirmed["amount_text"] == "30"
    assert reconfirmed["active"] == 0
    assert reconfirmed["next_run"] == next_run


def test_stale_legacy_confirmation_fails_before_confirming_or_execution(
    legacy_db, monkeypatch
):
    scheduled_at = 1_700_000_000
    plan_id = seed_plan(
        legacy_db,
        state="awaiting_confirmation",
        next_run=scheduled_at,
    )
    with sqlite3.connect(legacy_db) as db:
        db.execute(
            "UPDATE dca_plans SET confirmation_message_id=77, "
            "confirmation_scheduled_at=?, confirmation_expires_at=? WHERE id=?",
            (scheduled_at, scheduled_at + 600, plan_id),
        )
        db.commit()
    monkeypatch.setattr(app.time, "time", lambda: scheduled_at + 10)

    async def forbidden_execute(*_args, **_kwargs):
        raise AssertionError("stale legacy confirmation reached execution")

    monkeypatch.setattr(app, "cmd_execute", forbidden_execute)
    callback = FakeCallback(plan_id, scheduled_at)
    asyncio.run(app.cb_dca_confirm(callback))
    row = plan_row(legacy_db, plan_id)
    assert row["execution_state"] == "awaiting_confirmation"
    assert row["confirmation_message_id"] == 77
    assert callback.answers[-1][1].get("show_alert") is True


def test_status_marks_legacy_reference_and_exact_amount_separately(
    legacy_db, monkeypatch
):
    seed_plan(legacy_db, active=0)
    seed_plan(legacy_db, amount=30.125, amount_text="30.125", active=0)
    captured = []

    async def capture_status(_user_id, text, **_kwargs):
        captured.append(text)

    monkeypatch.setattr(app, "update_status_message", capture_status)
    asyncio.run(app.cmd_status(FakeMessage("/status")))
    assert "legacy-справка; точная сумма не подтверждена" in captured[0]
    assert "/reconfirm_1 EXACT_AMOUNT" in captured[0]
    assert "30.125 USDT" in captured[0]


def test_cancellation_inside_reconfirmation_rolls_back_and_releases_lock(
    legacy_db, monkeypatch
):
    plan_id = seed_plan(legacy_db, active=0)

    async def cancel_with_transaction_open(_db, _plan_id):
        raise asyncio.CancelledError

    monkeypatch.setattr(
        app, "_plan_has_unresolved_transaction_evidence", cancel_with_transaction_open
    )
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            app._reconfirm_legacy_plan(
                plan_id=plan_id,
                user_id=USER_ID,
                expected_network=NETWORK,
                canonical_amount="30",
                reconfirmed_at=1_700_000_000,
            )
        )
    assert plan_row(legacy_db, plan_id)["amount_text"] is None
    assert_writer_lock_available(legacy_db)


def test_failure_inside_reconfirmation_rolls_back_and_releases_lock(
    legacy_db, monkeypatch
):
    plan_id = seed_plan(legacy_db, active=0)

    async def fail_with_transaction_open(_db, _plan_id):
        raise sqlite3.OperationalError("injected evidence lookup failure")

    monkeypatch.setattr(
        app, "_plan_has_unresolved_transaction_evidence", fail_with_transaction_open
    )
    with pytest.raises(sqlite3.OperationalError, match="evidence lookup failure"):
        asyncio.run(
            app._reconfirm_legacy_plan(
                plan_id=plan_id,
                user_id=USER_ID,
                expected_network=NETWORK,
                canonical_amount="30",
                reconfirmed_at=1_700_000_000,
            )
        )
    assert plan_row(legacy_db, plan_id)["amount_text"] is None
    assert_writer_lock_available(legacy_db)
