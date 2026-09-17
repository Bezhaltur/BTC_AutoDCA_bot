import asyncio
import sqlite3
import threading

import pytest
from web3 import Web3

import bot as app


PLAN_ID = 7
USER_ID = 10001
ORDER_ID = "persisted-order"
NETWORK = "USDT-ARB"
APPROVE_HASH = "0x" + "ab" * 32


def _install_control_hooks(monkeypatch, hooks):
    original_execute = app.aiosqlite.Connection._execute
    submitted = []
    hooked = set()

    async def intercept(connection, function, *args, **kwargs):
        sql = " ".join(str(args[0]).split()).upper() if args else ""
        submitted.append(sql)
        hook = hooks.get(sql)
        if hook is not None and sql not in hooked:
            hooked.add(sql)

            def hooked_function(*worker_args, **worker_kwargs):
                return hook(function, worker_args, worker_kwargs)

            return await original_execute(
                connection, hooked_function, *args, **kwargs
            )
        return await original_execute(connection, function, *args, **kwargs)

    monkeypatch.setattr(app.aiosqlite.Connection, "_execute", intercept)
    return submitted


def _assert_writer_lock_released(db_path):
    with sqlite3.connect(db_path, timeout=0.1) as db:
        db.execute("BEGIN IMMEDIATE")
        db.rollback()


async def _wait_for_submission(submitted, statement):
    for _ in range(1000):
        if statement in submitted:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"statement was not submitted: {statement}")


def _seed(db_path, *, state, active=True, exact=False, approve_hash=None,
          transfer_hash=None, approve_raw=None, transfer_raw=None):
    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT INTO dca_plans (id,user_id,from_asset,amount,interval_hours,btc_address,"
            "next_run,active,deleted,execution_state,active_order_id,active_order_token,"
            "active_order_address,active_order_amount,active_order_expires) "
            "VALUES (?,?,?,?,?,?,?,?,?,'scheduled',?,?,?,?,?)",
            (
                PLAN_ID, USER_ID, NETWORK, 25.0, 24, "bc1persisted", 1_700_000_000,
                1, 0, ORDER_ID if active else None, "persisted-token" if active else None,
                "0xdeposit" if active else None, "25 USDT" if active else None,
                4_000_000_000 if active else None,
            ),
        )
        if state is not None:
            db.execute(
                "INSERT INTO sent_transactions (user_id,plan_id,order_id,order_token,network_key,"
                "approve_tx_hash,approve_raw_tx,transfer_tx_hash,transfer_raw_tx,amount,"
                "deposit_address,state,amount_units,token_decimals) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?, ?,?,?)",
                (
                    USER_ID, PLAN_ID, ORDER_ID, "persisted-token", NETWORK,
                    approve_hash, approve_raw, transfer_hash, transfer_raw, 25.0,
                    "0xdeposit", state, "25000000" if exact else None, 6 if exact else None,
                ),
            )
        db.commit()


def _snapshot(db_path):
    with sqlite3.connect(db_path) as db:
        return (
            db.execute("SELECT * FROM dca_plans WHERE id=?", (PLAN_ID,)).fetchone(),
            db.execute(
                "SELECT * FROM sent_transactions WHERE plan_id=? ORDER BY id", (PLAN_ID,)
            ).fetchall(),
        )


def _seed_scheduler_receipt(db_path):
    transfer_hash = "0x" + "67" * 32
    _seed(
        db_path,
        state="approve_confirmed",
        exact=True,
        transfer_hash=transfer_hash,
    )
    return transfer_hash


async def _run_scheduler_receipt_reconciliation(transfer_hash, schedule_anchor):
    result = await app.reconcile_existing_order(
        plan_id=PLAN_ID,
        existing_order_id=ORDER_ID,
        trigger="scheduler",
        schedule_anchor=schedule_anchor,
    )
    assert result.outcome == "confirmed"
    assert result.reason_code == "resume_confirmed"


@pytest.fixture
def reconciliation_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "reconciliation.sqlite3")
    monkeypatch.setattr(app, "DB_PATH", db_path)
    app._wallet_passwords.clear()
    asyncio.run(app.init_db())

    def forbidden_create(*_args, **_kwargs):
        raise AssertionError("existing-order reconciliation attempted FixedFloat create")

    monkeypatch.setattr(app, "create_fixedfloat_order", forbidden_create)
    return db_path


@pytest.mark.parametrize(
    ("trigger", "expected_schedule"),
    [
        ("manual", "unchanged"),
        ("scheduler", "advanced"),
        ("startup_recovery", "advanced"),
    ],
)
def test_same_approve_confirmed_row_uses_shared_core_for_all_triggers(
    reconciliation_db, monkeypatch, trigger, expected_schedule
):
    schedule_anchor = 1_800_001_000
    _seed(
        reconciliation_db,
        state="approve_confirmed",
        exact=True,
        approve_hash=APPROVE_HASH,
    )
    observed = []

    async def successful_resume(**kwargs):
        observed.append(kwargs)
        return "confirmed", APPROVE_HASH, "0x" + "cd" * 32, ""

    monkeypatch.setattr(app, "resume_transfer_after_approve", successful_resume)
    result = asyncio.run(
        app.reconcile_existing_order(
            plan_id=PLAN_ID,
            user_id=USER_ID,
            existing_order_id=ORDER_ID,
            trigger=trigger,
            schedule_anchor=schedule_anchor,
        )
    )

    assert result.outcome == "confirmed"
    assert result.schedule_effect == expected_schedule
    assert len(observed) == 1
    assert observed[0]["amount_units"] == 25_000_000
    assert observed[0]["token_decimals"] == 6
    with sqlite3.connect(reconciliation_db) as db:
        assert db.execute(
            "SELECT state,approve_tx_hash,transfer_tx_hash FROM sent_transactions"
        ).fetchone() == ("confirmed", APPROVE_HASH, "0x" + "cd" * 32)
        next_run = db.execute(
            "SELECT next_run FROM dca_plans WHERE id=?", (PLAN_ID,)
        ).fetchone()[0]
    assert next_run == (
        schedule_anchor + 24 * 3600
        if expected_schedule == "advanced"
        else 1_700_000_000
    )


@pytest.mark.parametrize("trigger", ["manual", "scheduler", "startup_recovery"])
@pytest.mark.parametrize("state", ["tx_pending", "pending", "blocked"])
def test_persisted_raw_is_rebroadcast_byte_for_byte_without_new_transfer(
    reconciliation_db, monkeypatch, trigger, state
):
    raw_tx = Web3.to_hex(b"shared-persisted-transfer")
    tx_hash = Web3.keccak(b"shared-persisted-transfer").hex()
    _seed(
        reconciliation_db,
        state=state,
        exact=True,
        transfer_hash=tx_hash,
        transfer_raw=raw_tx,
    )
    rebroadcasts = []

    async def pending_status(network_key, candidate_hash):
        assert (network_key, candidate_hash) == (NETWORK, tx_hash)
        return "pending"

    async def capture_rebroadcast(network_key, candidate_raw, candidate_hash, action):
        rebroadcasts.append((network_key, candidate_raw, candidate_hash, action))

    async def forbidden_resume(**_kwargs):
        raise AssertionError("persisted raw path attempted to build another transaction")

    monkeypatch.setattr(app, "get_transfer_tx_status", pending_status)
    monkeypatch.setattr(app, "rebroadcast_persisted_erc20_transaction", capture_rebroadcast)
    monkeypatch.setattr(app, "resume_transfer_after_approve", forbidden_resume)

    result = asyncio.run(
        app.reconcile_existing_order(
            plan_id=PLAN_ID, existing_order_id=ORDER_ID, trigger=trigger
        )
    )
    if trigger == "startup_recovery":
        assert result.outcome == "tx_pending"
        assert rebroadcasts == [(NETWORK, raw_tx, tx_hash, "transfer")]
    else:
        assert result.outcome == "in_progress"
        assert rebroadcasts == []


@pytest.mark.parametrize("trigger", ["manual", "scheduler", "startup_recovery"])
def test_missing_exact_intent_fails_closed_without_real_backfill(
    reconciliation_db, monkeypatch, trigger
):
    _seed(
        reconciliation_db,
        state="approve_confirmed",
        exact=False,
        approve_hash=APPROVE_HASH,
    )

    async def forbidden_resume(**_kwargs):
        raise AssertionError("legacy REAL reached transfer preparation")

    monkeypatch.setattr(app, "resume_transfer_after_approve", forbidden_resume)
    result = asyncio.run(
        app.reconcile_existing_order(
            plan_id=PLAN_ID, existing_order_id=ORDER_ID, trigger=trigger
        )
    )

    assert result.outcome == "manual_review"
    assert result.reason_code == "missing_exact_intent"
    with sqlite3.connect(reconciliation_db) as db:
        state, error = db.execute(
            "SELECT state,error_message FROM sent_transactions"
        ).fetchone()
    assert state == "tx_pending"
    assert error == "INVALID_PAYMENT_AMOUNT:missing persisted exact payment intent"


@pytest.mark.parametrize("trigger", ["manual", "scheduler", "startup_recovery"])
@pytest.mark.parametrize("state", ["sent", "confirmed", "failed", "expired"])
def test_terminal_rows_are_exact_noops(reconciliation_db, state, trigger):
    _seed(reconciliation_db, state=state, exact=True)
    before = _snapshot(reconciliation_db)
    result = asyncio.run(
        app.reconcile_existing_order(
            plan_id=PLAN_ID, existing_order_id=ORDER_ID, trigger=trigger
        )
    )
    assert result.outcome == "terminal_noop"
    assert _snapshot(reconciliation_db) == before


@pytest.mark.parametrize("state", ["sent", "confirmed", "failed", "expired"])
@pytest.mark.parametrize("fixedfloat_status", ["done", "expired"])
def test_persisted_terminal_state_wins_over_provider_status_hint(
    reconciliation_db, state, fixedfloat_status
):
    _seed(reconciliation_db, state=state, exact=True)
    before = _snapshot(reconciliation_db)
    result = asyncio.run(
        app.reconcile_existing_order(
            plan_id=PLAN_ID,
            existing_order_id=ORDER_ID,
            fixedfloat_status=fixedfloat_status,
            trigger="scheduler",
        )
    )
    assert result.outcome == "terminal_noop"
    assert result.tx_state == state
    assert _snapshot(reconciliation_db) == before


def test_active_order_without_transaction_is_noop(reconciliation_db):
    _seed(reconciliation_db, state=None)
    before = _snapshot(reconciliation_db)
    result = asyncio.run(
        app.reconcile_existing_order(
            plan_id=PLAN_ID, existing_order_id=ORDER_ID, trigger="manual"
        )
    )
    assert result.outcome == "no_transaction"
    assert _snapshot(reconciliation_db) == before


def test_persisted_fixedfloat_success_uses_shared_completion_rule(reconciliation_db):
    _seed(
        reconciliation_db,
        state="tx_pending",
        exact=True,
        transfer_hash="0x" + "45" * 32,
    )
    result = asyncio.run(
        app.reconcile_existing_order(
            plan_id=PLAN_ID,
            user_id=USER_ID,
            existing_order_id=ORDER_ID,
            fixedfloat_status="done",
            trigger="manual",
        )
    )

    assert result.outcome == "order_completed"
    with sqlite3.connect(reconciliation_db) as db:
        assert db.execute(
            "SELECT active_order_id FROM dca_plans WHERE id=?", (PLAN_ID,)
        ).fetchone() == (None,)
        assert db.execute("SELECT state FROM sent_transactions").fetchone() == ("confirmed",)
        assert db.execute(
            "SELECT order_id FROM completed_orders"
        ).fetchone() == (ORDER_ID,)


def test_persisted_fixedfloat_failure_uses_evidence_aware_release(reconciliation_db):
    _seed(reconciliation_db, state=None)
    result = asyncio.run(
        app.reconcile_existing_order(
            plan_id=PLAN_ID,
            user_id=USER_ID,
            existing_order_id=ORDER_ID,
            fixedfloat_status="expired",
            trigger="scheduler",
        )
    )

    assert result.outcome == "order_failed"
    with sqlite3.connect(reconciliation_db) as db:
        assert db.execute(
            "SELECT active_order_id FROM dca_plans WHERE id=?", (PLAN_ID,)
        ).fetchone() == (None,)
        assert db.execute("SELECT COUNT(*) FROM sent_transactions").fetchone() == (0,)


def test_fixedfloat_failure_cannot_clear_unresolved_sending_gate(reconciliation_db):
    _seed(reconciliation_db, state="sending", exact=True)
    before = _snapshot(reconciliation_db)
    result = asyncio.run(
        app.reconcile_existing_order(
            plan_id=PLAN_ID,
            existing_order_id=ORDER_ID,
            fixedfloat_status="expired",
            trigger="scheduler",
        )
    )

    assert result.outcome == "manual_review"
    assert result.reason_code == "active_gate_retained"
    assert _snapshot(reconciliation_db) == before


def test_tokenless_legacy_active_order_without_transaction_is_noop(reconciliation_db):
    _seed(reconciliation_db, state=None)
    with sqlite3.connect(reconciliation_db) as db:
        db.execute(
            "UPDATE dca_plans SET active_order_token=NULL WHERE id=?", (PLAN_ID,)
        )
        db.commit()
    before = _snapshot(reconciliation_db)
    result = asyncio.run(
        app.reconcile_existing_order(
            plan_id=PLAN_ID,
            existing_order_id=ORDER_ID,
            trigger="scheduler",
        )
    )
    assert result.outcome == "no_transaction"
    assert _snapshot(reconciliation_db) == before


@pytest.mark.parametrize("trigger", ["manual", "scheduler", "startup_recovery"])
def test_confirmed_transfer_receipt_uses_persisted_hash(
    reconciliation_db, monkeypatch, trigger
):
    tx_hash = "0x" + "12" * 32
    _seed(
        reconciliation_db,
        state="tx_pending",
        exact=True,
        transfer_hash=tx_hash,
    )

    async def confirmed_status(network_key, candidate_hash):
        assert (network_key, candidate_hash) == (NETWORK, tx_hash)
        return "confirmed"

    monkeypatch.setattr(app, "get_transfer_tx_status", confirmed_status)
    result = asyncio.run(
        app.reconcile_existing_order(
            plan_id=PLAN_ID, existing_order_id=ORDER_ID, trigger=trigger
        )
    )
    if trigger == "startup_recovery":
        assert result.outcome == "confirmed"
        assert result.schedule_effect == "advanced"
        with sqlite3.connect(reconciliation_db) as db:
            assert db.execute("SELECT state FROM sent_transactions").fetchone()[0] == "confirmed"
    else:
        assert result.outcome == "in_progress"
        with sqlite3.connect(reconciliation_db) as db:
            assert db.execute("SELECT state FROM sent_transactions").fetchone()[0] == "tx_pending"


@pytest.mark.parametrize("trigger", ["manual", "scheduler", "startup_recovery"])
def test_sending_without_exact_intent_never_uses_legacy_real(
    reconciliation_db, monkeypatch, trigger
):
    _seed(reconciliation_db, state="sending", exact=False)

    async def forbidden_resume(**_kwargs):
        raise AssertionError("sending row without exact intent reached transfer preparation")

    monkeypatch.setattr(app, "resume_transfer_after_approve", forbidden_resume)
    result = asyncio.run(
        app.reconcile_existing_order(
            plan_id=PLAN_ID, existing_order_id=ORDER_ID, trigger=trigger
        )
    )
    if trigger == "startup_recovery":
        assert result.outcome == "manual_review"
        assert result.reason_code == "missing_exact_intent"
        with sqlite3.connect(reconciliation_db) as db:
            assert db.execute(
                "SELECT error_message FROM sent_transactions"
            ).fetchone() == (
                "INVALID_PAYMENT_AMOUNT:missing persisted exact payment intent; "
                "manual review required",
            )
    else:
        assert result.outcome == "in_progress"


@pytest.mark.parametrize("trigger", ["manual", "scheduler"])
@pytest.mark.parametrize(
    "state", ["sending", "transfering", "tx_pending", "pending", "blocked"]
)
def test_interactive_wait_states_preserve_exact_database_snapshot(
    reconciliation_db, trigger, state
):
    _seed(reconciliation_db, state=state, exact=True)
    before = _snapshot(reconciliation_db)
    result = asyncio.run(
        app.reconcile_existing_order(
            plan_id=PLAN_ID,
            existing_order_id=ORDER_ID,
            trigger=trigger,
            schedule_anchor=1_800_001_500,
        )
    )

    assert result.outcome == "in_progress"
    assert result.reason_code == "existing_execution_in_progress"
    assert _snapshot(reconciliation_db) == before


def test_transfer_claim_contention_is_reported_without_resume(
    reconciliation_db, monkeypatch
):
    _seed(
        reconciliation_db,
        state="approve_confirmed",
        exact=True,
        approve_hash=APPROVE_HASH,
    )

    async def claim_lost(*_args, **_kwargs):
        return False

    async def forbidden_resume(**_kwargs):
        raise AssertionError("lost claim reached transfer resume")

    monkeypatch.setattr(app, "claim_transfer_after_approve", claim_lost)
    monkeypatch.setattr(app, "resume_transfer_after_approve", forbidden_resume)
    result = asyncio.run(
        app.reconcile_existing_order(
            plan_id=PLAN_ID, existing_order_id=ORDER_ID, trigger="scheduler"
        )
    )
    assert result.outcome == "claim_contended"
    assert result.reason_code == "transfer_claim_not_acquired"


def test_real_concurrent_reconciliation_claim_has_exactly_one_transfer_owner(
    reconciliation_db, monkeypatch
):
    _seed(
        reconciliation_db,
        state="approve_confirmed",
        exact=True,
        approve_hash=APPROVE_HASH,
    )
    resume_calls = []

    async def scenario():
        resume_started = asyncio.Event()
        release_resume = asyncio.Event()

        async def delayed_resume(**kwargs):
            resume_calls.append(kwargs)
            resume_started.set()
            await release_resume.wait()
            return "confirmed", APPROVE_HASH, "0x" + "89" * 32, ""

        monkeypatch.setattr(app, "resume_transfer_after_approve", delayed_resume)
        first = asyncio.create_task(
            app.reconcile_existing_order(
                plan_id=PLAN_ID,
                existing_order_id=ORDER_ID,
                trigger="scheduler",
                schedule_anchor=1_800_002_000,
            )
        )
        second = asyncio.create_task(
            app.reconcile_existing_order(
                plan_id=PLAN_ID,
                existing_order_id=ORDER_ID,
                trigger="scheduler",
                schedule_anchor=1_800_002_000,
            )
        )
        await asyncio.wait_for(resume_started.wait(), timeout=5)
        await asyncio.sleep(0)
        release_resume.set()
        return await asyncio.gather(first, second)

    results = asyncio.run(scenario())

    assert len(resume_calls) == 1
    assert sorted(result.outcome for result in results) in (
        ["claim_contended", "confirmed"],
        ["confirmed", "in_progress"],
    )
    with sqlite3.connect(reconciliation_db) as db:
        assert db.execute(
            "SELECT state,transfer_tx_hash FROM sent_transactions"
        ).fetchone() == ("confirmed", "0x" + "89" * 32)


def test_startup_transfering_exact_intent_without_raw_resumes_once(
    reconciliation_db, monkeypatch
):
    _seed(reconciliation_db, state="transfering", exact=True)
    app._wallet_passwords[USER_ID] = "test-password"
    observed = []

    async def successful_resume(**kwargs):
        observed.append(kwargs)
        return "confirmed", None, "0x" + "34" * 32, ""

    monkeypatch.setattr(app, "resume_transfer_after_approve", successful_resume)
    result = asyncio.run(
        app.reconcile_existing_order(
            plan_id=PLAN_ID, existing_order_id=ORDER_ID, trigger="startup_recovery"
        )
    )

    assert result.outcome == "confirmed"
    assert len(observed) == 1
    assert observed[0]["amount_units"] == 25_000_000
    assert observed[0]["token_decimals"] == 6


def test_startup_missing_hash_without_wallet_preserves_state_fail_closed(
    reconciliation_db, monkeypatch
):
    _seed(reconciliation_db, state="sending", exact=True)

    async def forbidden_resume(**_kwargs):
        raise AssertionError("locked wallet reached transfer preparation")

    monkeypatch.setattr(app, "resume_transfer_after_approve", forbidden_resume)
    result = asyncio.run(
        app.reconcile_existing_order(
            plan_id=PLAN_ID, existing_order_id=ORDER_ID, trigger="startup_recovery"
        )
    )

    assert result.outcome == "manual_review"
    assert result.reason_code == "wallet_unlock_unavailable"
    with sqlite3.connect(reconciliation_db) as db:
        state, error = db.execute(
            "SELECT state,error_message FROM sent_transactions"
        ).fetchone()
    assert state == "sending"
    assert error == "ERC20 recovery blocked: wallet unlock or plan context unavailable"


def test_planless_confirmed_approve_is_not_mistaken_for_completed_payment(
    reconciliation_db, monkeypatch
):
    with sqlite3.connect(reconciliation_db) as db:
        db.execute(
            "INSERT INTO sent_transactions (user_id,plan_id,order_id,network_key,"
            "approve_tx_hash,amount,deposit_address,state,amount_units,token_decimals) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                USER_ID, None, "legacy-planless", NETWORK, APPROVE_HASH,
                1.0, "0xlegacy", "tx_pending", "1", 6,
            ),
        )
        db.commit()

    async def confirmed_status(network_key, tx_hash):
        assert (network_key, tx_hash) == (NETWORK, APPROVE_HASH)
        return "confirmed"

    monkeypatch.setattr(app, "get_transfer_tx_status", confirmed_status)
    asyncio.run(app.recovery_scan_pending_transactions())

    with sqlite3.connect(reconciliation_db) as db:
        assert db.execute(
            "SELECT state,approve_tx_hash,transfer_tx_hash FROM sent_transactions "
            "WHERE order_id='legacy-planless'"
        ).fetchone() == ("tx_pending", APPROVE_HASH, None)


def _in_progress_result(trigger):
    return app.ReconciliationResult(
        "in_progress",
        PLAN_ID,
        ORDER_ID,
        "sending",
        "existing_execution_in_progress",
        trigger != "startup_recovery",
        "unchanged",
    )


def test_manual_existing_order_routes_through_shared_core_once(
    reconciliation_db, monkeypatch
):
    _seed(reconciliation_db, state="sending", exact=True)
    with sqlite3.connect(reconciliation_db) as db:
        db.execute(
            "UPDATE dca_plans SET active_order_expires=? WHERE id=?",
            (1_600_000_000, PLAN_ID),
        )
        db.commit()
    monkeypatch.setattr(app.time, "time", lambda: 1_700_000_100)
    calls = []

    async def active_status(*_args):
        return "NEW"

    async def capture_reconciliation(**kwargs):
        calls.append(kwargs)
        return _in_progress_result("manual")

    async def progress(*_args, **_kwargs):
        return None

    async def answer(*_args, **_kwargs):
        return None

    monkeypatch.setattr(app, "get_fixedfloat_order_status_with_retry", active_status)
    monkeypatch.setattr(app, "reconcile_existing_order", capture_reconciliation)
    monkeypatch.setattr(app, "update_order_progress_message", progress)
    message = type(
        "ExecuteMessage",
        (),
        {
            "from_user": type("User", (), {"id": USER_ID})(),
            "text": "/execute",
            "answer": answer,
        },
    )()

    asyncio.run(app.cmd_execute(message))

    assert len(calls) == 1
    assert calls[0]["trigger"] == "manual"


def test_scheduler_live_order_routes_through_shared_core_once(
    reconciliation_db, monkeypatch
):
    _seed(reconciliation_db, state="sending", exact=True)
    now = 1_700_000_100
    monkeypatch.setattr(app.time, "time", lambda: now)
    calls = []

    async def display_number(*_args):
        return 1

    async def active_status(*_args):
        return "NEW"

    async def capture_reconciliation(**kwargs):
        calls.append(kwargs)
        return _in_progress_result("scheduler")

    original_sleep = asyncio.sleep

    async def stop_after_iteration(seconds):
        if seconds == 60:
            raise asyncio.CancelledError
        await original_sleep(seconds)

    monkeypatch.setattr(app, "get_plan_display_number", display_number)
    monkeypatch.setattr(app, "get_fixedfloat_order_status_with_retry", active_status)
    monkeypatch.setattr(app, "reconcile_existing_order", capture_reconciliation)
    monkeypatch.setattr(app.asyncio, "sleep", stop_after_iteration)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(app.dca_scheduler())

    assert len(calls) == 1
    assert calls[0]["trigger"] == "scheduler"
    assert calls[0]["schedule_anchor"] == now


def test_startup_plan_owned_row_routes_through_shared_core_once(
    reconciliation_db, monkeypatch
):
    _seed(reconciliation_db, state="sending", exact=True)
    calls = []

    async def capture_reconciliation(**kwargs):
        calls.append(kwargs)
        return _in_progress_result("startup_recovery")

    monkeypatch.setattr(app, "reconcile_existing_order", capture_reconciliation)
    asyncio.run(app.recovery_scan_pending_transactions())

    assert len(calls) == 1
    assert calls[0]["trigger"] == "startup_recovery"
    assert isinstance(calls[0]["schedule_anchor"], int)


def test_cancellation_before_receipt_result_leaves_persisted_state_unchanged(
    reconciliation_db, monkeypatch
):
    tx_hash = "0x" + "ef" * 32
    _seed(
        reconciliation_db,
        state="tx_pending",
        exact=True,
        transfer_hash=tx_hash,
    )
    before = _snapshot(reconciliation_db)

    async def cancelled_status(*_args, **_kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(app, "get_transfer_tx_status", cancelled_status)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            app.reconcile_existing_order(
                plan_id=PLAN_ID, existing_order_id=ORDER_ID, trigger="startup_recovery"
            )
        )
    assert _snapshot(reconciliation_db) == before


def test_scheduler_core_true_queued_commit_cancellation_is_acknowledged(
    reconciliation_db, monkeypatch
):
    transfer_hash = _seed_scheduler_receipt(reconciliation_db)
    schedule_anchor = 1_800_000_000
    blocker_started = threading.Event()
    release_blocker = threading.Event()
    commit_enqueued = threading.Event()
    commit_started = threading.Event()
    submitted = []
    original_execute = app.aiosqlite.Connection._execute
    commit_hooked = False

    async def confirmed_status(network_key, candidate_hash):
        assert (network_key, candidate_hash) == (NETWORK, transfer_hash)
        return "confirmed"

    async def intercept(connection, function, *args, **kwargs):
        nonlocal commit_hooked
        sql = " ".join(str(args[0]).split()).upper() if args else ""
        submitted.append(sql)
        if sql == "COMMIT;" and not commit_hooked:
            commit_hooked = True

            def block_worker():
                blocker_started.set()
                if not release_blocker.wait(timeout=5):
                    raise RuntimeError("timed out waiting to release core COMMIT blocker")

            blocker_task = asyncio.create_task(original_execute(connection, block_worker))
            assert await asyncio.to_thread(blocker_started.wait, 5)

            def observe_commit(*worker_args, **worker_kwargs):
                commit_started.set()
                return function(*worker_args, **worker_kwargs)

            commit_enqueued.set()
            try:
                return await original_execute(
                    connection, observe_commit, *args, **kwargs
                )
            finally:
                await blocker_task
        return await original_execute(connection, function, *args, **kwargs)

    monkeypatch.setattr(app, "get_transfer_tx_status", confirmed_status)
    monkeypatch.setattr(app.aiosqlite.Connection, "_execute", intercept)

    async def scenario():
        task = asyncio.create_task(
            _run_scheduler_receipt_reconciliation(transfer_hash, schedule_anchor)
        )
        assert await asyncio.to_thread(commit_enqueued.wait, 5)
        assert commit_started.is_set() is False
        task.cancel()
        await asyncio.sleep(0)
        assert commit_started.is_set() is False
        release_blocker.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert commit_started.is_set() is True

    asyncio.run(scenario())

    assert "ROLLBACK;" not in submitted
    with sqlite3.connect(reconciliation_db) as db:
        assert db.execute("SELECT state FROM sent_transactions").fetchone() == ("confirmed",)
        assert db.execute("SELECT next_run FROM dca_plans").fetchone() == (
            schedule_anchor + 24 * 3600,
        )
    _assert_writer_lock_released(reconciliation_db)


def test_scheduler_core_running_commit_cancellation_is_acknowledged(
    reconciliation_db, monkeypatch
):
    transfer_hash = _seed_scheduler_receipt(reconciliation_db)
    schedule_anchor = 1_800_000_100
    commit_started = threading.Event()
    release_commit = threading.Event()

    async def confirmed_status(*_args):
        return "confirmed"

    def delayed_commit(function, args, kwargs):
        commit_started.set()
        if not release_commit.wait(timeout=5):
            raise RuntimeError("timed out waiting to release core COMMIT")
        return function(*args, **kwargs)

    submitted = _install_control_hooks(
        monkeypatch, {"COMMIT;": delayed_commit}
    )
    monkeypatch.setattr(app, "get_transfer_tx_status", confirmed_status)

    async def scenario():
        task = asyncio.create_task(
            _run_scheduler_receipt_reconciliation(transfer_hash, schedule_anchor)
        )
        assert await asyncio.to_thread(commit_started.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        release_commit.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    assert submitted.count("COMMIT;") == 1
    assert "ROLLBACK;" not in submitted
    with sqlite3.connect(reconciliation_db) as db:
        assert db.execute("SELECT state FROM sent_transactions").fetchone() == ("confirmed",)
    _assert_writer_lock_released(reconciliation_db)


def test_scheduler_core_commit_error_rolls_back_and_releases_lock(
    reconciliation_db, monkeypatch
):
    transfer_hash = _seed_scheduler_receipt(reconciliation_db)

    async def confirmed_status(*_args):
        return "confirmed"

    def fail_commit(_function, _args, _kwargs):
        raise sqlite3.OperationalError("core scheduler COMMIT failure")

    submitted = _install_control_hooks(monkeypatch, {"COMMIT;": fail_commit})
    monkeypatch.setattr(app, "get_transfer_tx_status", confirmed_status)

    with pytest.raises(sqlite3.OperationalError, match="core scheduler COMMIT failure"):
        asyncio.run(
            _run_scheduler_receipt_reconciliation(transfer_hash, 1_800_000_200)
        )

    assert submitted.count("ROLLBACK;") == 1
    with sqlite3.connect(reconciliation_db) as db:
        assert db.execute("SELECT state FROM sent_transactions").fetchone() == (
            "approve_confirmed",
        )
    _assert_writer_lock_released(reconciliation_db)


def test_scheduler_core_cancelled_commit_error_keeps_diagnostic(
    reconciliation_db, monkeypatch
):
    transfer_hash = _seed_scheduler_receipt(reconciliation_db)
    commit_started = threading.Event()
    release_commit_error = threading.Event()
    commit_error = sqlite3.OperationalError("cancelled core scheduler COMMIT failure")

    async def confirmed_status(*_args):
        return "confirmed"

    def delayed_commit_error(_function, _args, _kwargs):
        commit_started.set()
        if not release_commit_error.wait(timeout=5):
            raise RuntimeError("timed out waiting to fail core COMMIT")
        raise commit_error

    submitted = _install_control_hooks(
        monkeypatch, {"COMMIT;": delayed_commit_error}
    )
    monkeypatch.setattr(app, "get_transfer_tx_status", confirmed_status)

    async def scenario():
        task = asyncio.create_task(
            _run_scheduler_receipt_reconciliation(transfer_hash, 1_800_000_300)
        )
        assert await asyncio.to_thread(commit_started.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        release_commit_error.set()
        with pytest.raises(asyncio.CancelledError) as raised:
            await task
        return raised.value

    cancellation = asyncio.run(scenario())
    diagnostic = cancellation
    if not hasattr(diagnostic, "scheduler_transaction_commit_error"):
        diagnostic = diagnostic.__context__
    assert diagnostic.scheduler_transaction_commit_error is commit_error
    assert submitted.count("ROLLBACK;") == 1
    with sqlite3.connect(reconciliation_db) as db:
        assert db.execute("SELECT state FROM sent_transactions").fetchone() == (
            "approve_confirmed",
        )
    _assert_writer_lock_released(reconciliation_db)


def test_scheduler_core_rollback_error_is_fail_closed(
    reconciliation_db, monkeypatch
):
    transfer_hash = _seed_scheduler_receipt(reconciliation_db)
    commit_error = sqlite3.OperationalError("primary core COMMIT failure")
    rollback_error = sqlite3.OperationalError("secondary core ROLLBACK failure")

    async def confirmed_status(*_args):
        return "confirmed"

    def fail_commit(_function, _args, _kwargs):
        raise commit_error

    def fail_rollback(_function, _args, _kwargs):
        raise rollback_error

    submitted = _install_control_hooks(
        monkeypatch, {"COMMIT;": fail_commit, "ROLLBACK;": fail_rollback}
    )
    monkeypatch.setattr(app, "get_transfer_tx_status", confirmed_status)

    with pytest.raises(sqlite3.OperationalError, match="primary core COMMIT failure") as raised:
        asyncio.run(
            _run_scheduler_receipt_reconciliation(transfer_hash, 1_800_000_400)
        )

    assert raised.value.scheduler_transaction_rollback_error is rollback_error
    assert raised.value.__cause__ is rollback_error
    assert submitted.count("ROLLBACK;") == 1
    with sqlite3.connect(reconciliation_db) as db:
        assert db.execute("SELECT state FROM sent_transactions").fetchone() == (
            "approve_confirmed",
        )
    _assert_writer_lock_released(reconciliation_db)


@pytest.mark.parametrize("state", ["sending", "transfering", "approve_confirmed", "tx_pending", "pending", "blocked", "sent", "confirmed", "failed", "expired"])
def test_scheduler_routes_every_persisted_state_through_shared_core_once(
    reconciliation_db, monkeypatch, state
):
    _seed(reconciliation_db, state=state, exact=True)
    before = _snapshot(reconciliation_db)
    now = 1_700_000_100
    calls = []

    async def display_number(*_args):
        return 1

    async def capture_reconciliation(**kwargs):
        calls.append(kwargs)
        return app.ReconciliationResult(
            "terminal_noop" if state in {"sent", "confirmed", "failed", "expired"} else "in_progress",
            PLAN_ID,
            ORDER_ID,
            state,
            f"terminal_{state}" if state in {"sent", "confirmed", "failed", "expired"} else "existing_execution_in_progress",
            False,
            "unchanged",
        )

    async def forbidden_status(*_args):
        if state in {"sent", "confirmed", "failed", "expired"}:
            raise AssertionError("terminal state reached FixedFloat status lookup")
        return "NEW"

    async def forbidden_mark(*_args, **_kwargs):
        raise AssertionError("legacy terminal reconciliation branch executed")

    original_sleep = asyncio.sleep

    async def stop_after_iteration(seconds):
        if seconds == 60:
            raise asyncio.CancelledError
        await original_sleep(seconds)

    monkeypatch.setattr(app.time, "time", lambda: now)
    monkeypatch.setattr(app, "get_plan_display_number", display_number)
    monkeypatch.setattr(app, "get_fixedfloat_order_status_with_retry", forbidden_status)
    monkeypatch.setattr(app, "reconcile_existing_order", capture_reconciliation)
    monkeypatch.setattr(app, "mark_order_completed", forbidden_mark)
    monkeypatch.setattr(app, "mark_order_failed", forbidden_mark)
    monkeypatch.setattr(app.asyncio, "sleep", stop_after_iteration)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(app.dca_scheduler())

    assert len(calls) == 1
    assert calls[0]["trigger"] == "scheduler"
    assert calls[0]["existing_order_id"] == ORDER_ID
    assert _snapshot(reconciliation_db) == before


def test_startup_anchor_is_captured_after_recovery_query(
    reconciliation_db, monkeypatch
):
    transfer_hash = "0x" + "77" * 32
    _seed(
        reconciliation_db,
        state="tx_pending",
        exact=True,
        transfer_hash=transfer_hash,
    )
    recovery_query_submitted = threading.Event()
    original_execute = app.aiosqlite.Connection._execute
    anchor = 1_800_003_000

    async def intercept(connection, function, *args, **kwargs):
        sql = " ".join(str(args[0]).split()).upper() if args else ""
        if sql.startswith("SELECT ID, PLAN_ID, ORDER_ID, NETWORK_KEY"):
            recovery_query_submitted.set()
        return await original_execute(connection, function, *args, **kwargs)

    def capture_time():
        assert recovery_query_submitted.is_set()
        return anchor

    async def confirmed_status(*_args):
        return "confirmed"

    monkeypatch.setattr(app.aiosqlite.Connection, "_execute", intercept)
    monkeypatch.setattr(app.time, "time", capture_time)
    monkeypatch.setattr(app, "get_transfer_tx_status", confirmed_status)

    asyncio.run(app.recovery_scan_pending_transactions())

    with sqlite3.connect(reconciliation_db) as db:
        assert db.execute("SELECT next_run FROM dca_plans").fetchone() == (
            anchor + 24 * 3600,
        )


def test_planless_no_hash_validates_exact_intent_before_context_error(
    reconciliation_db,
):
    with sqlite3.connect(reconciliation_db) as db:
        db.execute(
            "INSERT INTO sent_transactions (user_id,plan_id,order_id,network_key,amount,"
            "deposit_address,state,amount_units,token_decimals) VALUES (?,?,?,?,?,?,?,?,?)",
            (USER_ID, None, "legacy-no-exact", NETWORK, 1.25, "0xlegacy", "sending", None, None),
        )
        db.commit()

    asyncio.run(app.recovery_scan_pending_transactions())

    with sqlite3.connect(reconciliation_db) as db:
        assert db.execute(
            "SELECT error_message FROM sent_transactions WHERE order_id='legacy-no-exact'"
        ).fetchone() == (
            "INVALID_PAYMENT_AMOUNT:missing persisted exact payment intent; manual review required",
        )


@pytest.mark.parametrize("trigger", ["manual", "scheduler"])
def test_interactive_onchain_revert_preserves_exact_baseline_error(
    reconciliation_db, monkeypatch, trigger
):
    transfer_raw = Web3.to_hex(b"reverted-transfer")
    transfer_hash = Web3.keccak(b"reverted-transfer").hex()
    _seed(
        reconciliation_db,
        state="approve_confirmed",
        exact=True,
        transfer_hash=transfer_hash,
        transfer_raw=transfer_raw,
    )
    with sqlite3.connect(reconciliation_db) as db:
        db.execute("UPDATE sent_transactions SET transfer_tx_nonce=29")
        db.commit()

    async def failed_status(*_args):
        return "failed"

    monkeypatch.setattr(app, "get_transfer_tx_status", failed_status)
    result = asyncio.run(
        app.reconcile_existing_order(
            plan_id=PLAN_ID,
            existing_order_id=ORDER_ID,
            trigger=trigger,
        )
    )

    assert result.outcome == "failed"
    with sqlite3.connect(reconciliation_db) as db:
        assert db.execute(
            "SELECT state,error_message FROM sent_transactions"
        ).fetchone() == ("failed", "Transfer tx reverted on-chain")


@pytest.mark.parametrize("helper_path", ["completed", "failed", "claim"])
def test_scheduler_reused_helper_true_queued_commit_cancellation_is_acknowledged(
    reconciliation_db, monkeypatch, helper_path
):
    if helper_path == "completed":
        _seed(
            reconciliation_db,
            state="tx_pending",
            exact=True,
            transfer_hash="0x" + "81" * 32,
        )
    elif helper_path == "failed":
        _seed(reconciliation_db, state=None)
    else:
        _seed(
            reconciliation_db,
            state="approve_confirmed",
            exact=True,
            approve_hash=APPROVE_HASH,
        )

    blocker_started = threading.Event()
    release_blocker = threading.Event()
    commit_enqueued = threading.Event()
    commit_started = threading.Event()
    submitted = []
    original_execute = app.aiosqlite.Connection._execute
    commit_hooked = False

    async def intercept(connection, function, *args, **kwargs):
        nonlocal commit_hooked
        sql = " ".join(str(args[0]).split()).upper() if args else ""
        submitted.append(sql)
        if sql == "COMMIT;" and not commit_hooked:
            commit_hooked = True

            def block_worker():
                blocker_started.set()
                if not release_blocker.wait(timeout=5):
                    raise RuntimeError("timed out waiting for helper COMMIT blocker")

            blocker_task = asyncio.create_task(original_execute(connection, block_worker))
            assert await asyncio.to_thread(blocker_started.wait, 5)

            def observe_commit(*worker_args, **worker_kwargs):
                commit_started.set()
                return function(*worker_args, **worker_kwargs)

            commit_enqueued.set()
            try:
                return await original_execute(connection, observe_commit, *args, **kwargs)
            finally:
                await blocker_task
        return await original_execute(connection, function, *args, **kwargs)

    async def forbidden_resume(**_kwargs):
        raise AssertionError("cancelled claim continued into transfer resume")

    monkeypatch.setattr(app.aiosqlite.Connection, "_execute", intercept)
    monkeypatch.setattr(app, "resume_transfer_after_approve", forbidden_resume)

    async def run_core():
        kwargs = {
            "plan_id": PLAN_ID,
            "existing_order_id": ORDER_ID,
            "trigger": "scheduler",
        }
        if helper_path == "completed":
            kwargs["fixedfloat_status"] = "done"
        elif helper_path == "failed":
            kwargs["fixedfloat_status"] = "expired"
        return await app.reconcile_existing_order(**kwargs)

    async def scenario():
        task = asyncio.create_task(run_core())
        assert await asyncio.to_thread(commit_enqueued.wait, 5)
        assert commit_started.is_set() is False
        task.cancel()
        await asyncio.sleep(0)
        assert commit_started.is_set() is False
        release_blocker.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert commit_started.is_set() is True

    asyncio.run(scenario())

    assert submitted.count("COMMIT;") == 1
    assert "ROLLBACK;" not in submitted
    with sqlite3.connect(reconciliation_db) as db:
        active_order_id = db.execute(
            "SELECT active_order_id FROM dca_plans WHERE id=?", (PLAN_ID,)
        ).fetchone()[0]
        if helper_path == "completed":
            assert active_order_id is None
            assert db.execute("SELECT state FROM sent_transactions").fetchone() == ("confirmed",)
        elif helper_path == "failed":
            assert active_order_id is None
        else:
            assert active_order_id == ORDER_ID
            assert db.execute("SELECT state FROM sent_transactions").fetchone() == ("transfering",)
    _assert_writer_lock_released(reconciliation_db)


def test_scheduler_mark_completed_commit_error_with_cancellation_rolls_back(
    reconciliation_db, monkeypatch
):
    _seed(
        reconciliation_db,
        state="tx_pending",
        exact=True,
        transfer_hash="0x" + "82" * 32,
    )
    before = _snapshot(reconciliation_db)
    commit_started = threading.Event()
    release_commit_error = threading.Event()
    commit_error = sqlite3.OperationalError("mark completed COMMIT failure")

    def delayed_commit_error(_function, _args, _kwargs):
        commit_started.set()
        if not release_commit_error.wait(timeout=5):
            raise RuntimeError("timed out waiting to fail helper COMMIT")
        raise commit_error

    submitted = _install_control_hooks(
        monkeypatch, {"COMMIT;": delayed_commit_error}
    )

    async def scenario():
        task = asyncio.create_task(
            app.reconcile_existing_order(
                plan_id=PLAN_ID,
                existing_order_id=ORDER_ID,
                fixedfloat_status="done",
                trigger="scheduler",
            )
        )
        assert await asyncio.to_thread(commit_started.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        release_commit_error.set()
        with pytest.raises(asyncio.CancelledError) as raised:
            await task
        return raised.value

    cancellation = asyncio.run(scenario())
    diagnostic = cancellation
    if not hasattr(diagnostic, "scheduler_transaction_commit_error"):
        diagnostic = diagnostic.__context__
    assert diagnostic.scheduler_transaction_commit_error is commit_error
    assert submitted.count("ROLLBACK;") == 1
    assert _snapshot(reconciliation_db) == before
    _assert_writer_lock_released(reconciliation_db)
