import asyncio
import sqlite3
import threading
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

import bot as app


USER_ID = 10001
NETWORK = "USDT-ARB"
BTC_ADDRESS = "bc1qcreatewindow0000000000000000000000000"
DEPOSIT_ADDRESS = "0x1111111111111111111111111111111111111111"
NOW = 1_700_000_000


class SimulatedProcessCrash(BaseException):
    """Bypass cmd_execute's Exception handler like abrupt process loss."""


class FakeBot:
    def __init__(self):
        self.sent = []
        self.edited = []
        self._message_id = 100

    async def send_message(self, chat_id, text, **kwargs):
        self._message_id += 1
        self.sent.append((chat_id, text, kwargs))
        return SimpleNamespace(message_id=self._message_id)

    async def edit_message_text(self, **kwargs):
        self.edited.append(kwargs)
        return SimpleNamespace(message_id=kwargs["message_id"])


class ExecuteMessage:
    from_user = SimpleNamespace(id=USER_ID)
    text = "/execute"

    def __init__(self, fake_bot):
        self.fake_bot = fake_bot

    async def answer(self, text, **kwargs):
        return await self.fake_bot.send_message(USER_ID, text, **kwargs)


class FakeCallback:
    def __init__(self, data):
        self.data = data
        self.from_user = SimpleNamespace(id=USER_ID)
        self.message = SimpleNamespace(message_id=777)
        self.answers = []

    async def answer(self, text=None, **kwargs):
        self.answers.append((text, kwargs))


def _order_response(sequence):
    return {
        "id": f"external-order-{sequence}",
        "token": f"external-token-{sequence}",
        "from": {
            "code": "USDTARB",
            "amount": "25.000000",
            "address": DEPOSIT_ADDRESS,
        },
        "expires_at": NOW + 3600,
    }


def _plan_row(db_path, plan_id):
    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        return dict(
            db.execute("SELECT * FROM dca_plans WHERE id = ?", (plan_id,)).fetchone()
        )


def _transaction_rows(db_path, plan_id):
    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        return [
            dict(row)
            for row in db.execute(
                "SELECT * FROM sent_transactions WHERE plan_id = ? ORDER BY id",
                (plan_id,),
            ).fetchall()
        ]


@pytest.fixture
def create_window(tmp_path, monkeypatch):
    db_path = str(tmp_path / "create-window.sqlite3")
    fake_bot = FakeBot()
    external_creates = []

    monkeypatch.setattr(app, "DB_PATH", db_path)
    monkeypatch.setattr(app, "bot", fake_bot)
    monkeypatch.setattr(app.time, "time", lambda: NOW)
    app._wallet_passwords.clear()
    app._order_progress_messages.clear()

    def forbidden_network(*args, **kwargs):
        raise AssertionError("characterization test attempted real network access")

    monkeypatch.setattr(app.requests, "get", forbidden_network)
    monkeypatch.setattr(app.requests, "post", forbidden_network)
    monkeypatch.setattr(app, "ff_request", forbidden_network)

    async def fake_limits(network_key):
        assert network_key == NETWORK
        return {"min": 1.0, "max": 500.0}

    monkeypatch.setattr(app, "get_fixedfloat_limits", fake_limits)
    asyncio.run(app.init_db())

    with sqlite3.connect(db_path) as db:
        cur = db.execute(
            "INSERT INTO dca_plans ("
            "user_id, from_asset, amount, interval_hours, btc_address, next_run, "
            "active, deleted, execution_state"
            ") VALUES (?, ?, 25.0, 24, ?, ?, 1, 0, 'scheduled')",
            (USER_ID, NETWORK, BTC_ADDRESS, NOW),
        )
        plan_id = int(cur.lastrowid)
        db.commit()

    return SimpleNamespace(
        db_path=db_path,
        bot=fake_bot,
        message=ExecuteMessage(fake_bot),
        plan_id=plan_id,
        external_creates=external_creates,
    )


def _install_successful_create(monkeypatch, harness):
    def create_order(network_key, amount, btc_address):
        assert (network_key, amount, btc_address) == (NETWORK, 25.0, BTC_ADDRESS)
        harness.external_creates.append(len(harness.external_creates) + 1)
        return _order_response(len(harness.external_creates))

    monkeypatch.setattr(app, "create_fixedfloat_order", create_order)


def _seed_wallet(harness):
    with sqlite3.connect(harness.db_path) as db:
        db.execute(
            "INSERT INTO wallets (user_id, wallet_address) VALUES (?, ?)",
            (USER_ID, "0x2222222222222222222222222222222222222222"),
        )
        db.commit()
    app._wallet_passwords[USER_ID] = "test-password"


def test_crash_before_creating_order_commit_leaves_only_recoverable_claim(
    create_window, monkeypatch
):
    production_mark_started = app.mark_plan_order_creation_started

    async def crash_before_durable_gate(*args, **kwargs):
        raise SimulatedProcessCrash("before creating_order commit")

    monkeypatch.setattr(app, "mark_plan_order_creation_started", crash_before_durable_gate)
    with pytest.raises(SimulatedProcessCrash):
        asyncio.run(app.cmd_execute(create_window.message))

    assert create_window.external_creates == []
    assert _plan_row(create_window.db_path, create_window.plan_id)["execution_state"] == "claiming"
    assert _transaction_rows(create_window.db_path, create_window.plan_id) == []

    asyncio.run(app.recover_stale_plan_claims())
    monkeypatch.setattr(app, "mark_plan_order_creation_started", production_mark_started)
    _install_successful_create(monkeypatch, create_window)
    asyncio.run(app.cmd_execute(create_window.message))

    assert create_window.external_creates == [1]
    assert _plan_row(create_window.db_path, create_window.plan_id)["active_order_id"] == "external-order-1"


def test_successful_external_create_lost_before_persistence_cannot_be_duplicated(
    create_window, monkeypatch
):
    _install_successful_create(monkeypatch, create_window)

    def crash_after_response(*args, **kwargs):
        raise SimulatedProcessCrash("after create response, before local persistence")

    monkeypatch.setattr(app, "extract_order_expires_at", crash_after_response)
    with pytest.raises(SimulatedProcessCrash):
        asyncio.run(app.cmd_execute(create_window.message))

    plan = _plan_row(create_window.db_path, create_window.plan_id)
    assert create_window.external_creates == [1]
    assert plan["execution_state"] == "creating_order"
    assert plan["active_order_id"] is None
    assert plan["active_order_token"] is None
    assert _transaction_rows(create_window.db_path, create_window.plan_id) == []

    asyncio.run(app.recover_stale_plan_claims())
    monkeypatch.setattr(app, "extract_order_expires_at", lambda data: data["expires_at"])
    asyncio.run(app.cmd_execute(create_window.message))

    assert create_window.external_creates == [1]
    plan = _plan_row(create_window.db_path, create_window.plan_id)
    assert plan["execution_state"] == "creating_order"
    assert plan["active_order_id"] is None
    assert plan["active_order_token"] is None


def test_creating_order_committed_before_external_call_blocks_restart_retry(
    create_window, monkeypatch
):
    create_attempts = []

    def crash_before_request(*args, **kwargs):
        create_attempts.append("entered")
        assert _plan_row(create_window.db_path, create_window.plan_id)["execution_state"] == "creating_order"
        raise SimulatedProcessCrash("before actual request")

    monkeypatch.setattr(app, "create_fixedfloat_order", crash_before_request)
    with pytest.raises(SimulatedProcessCrash):
        asyncio.run(app.cmd_execute(create_window.message))

    asyncio.run(app.recover_stale_plan_claims())
    asyncio.run(app.cmd_execute(create_window.message))
    assert create_attempts == ["entered"]
    assert _plan_row(create_window.db_path, create_window.plan_id)["execution_state"] == "creating_order"


def test_ambiguous_create_error_retains_gate_and_blocks_second_create(
    create_window, monkeypatch
):
    attempts = []

    def ambiguous_create(*args, **kwargs):
        attempts.append("request")
        raise TimeoutError("FixedFloat response timeout")

    monkeypatch.setattr(app, "create_fixedfloat_order", ambiguous_create)
    asyncio.run(app.cmd_execute(create_window.message))
    assert attempts == ["request"]
    assert _plan_row(create_window.db_path, create_window.plan_id)["execution_state"] == "creating_order"

    asyncio.run(app.recover_stale_plan_claims())
    asyncio.run(app.cmd_execute(create_window.message))
    assert attempts == ["request"]


def test_transport_error_retains_gate_and_blocks_second_create(
    create_window, monkeypatch
):
    attempts = []

    def transport_error(*args, **kwargs):
        attempts.append("request")
        raise ConnectionError("connection reset after request write")

    monkeypatch.setattr(app, "create_fixedfloat_order", transport_error)
    asyncio.run(app.cmd_execute(create_window.message))

    assert attempts == ["request"]
    assert _plan_row(create_window.db_path, create_window.plan_id)["execution_state"] == "creating_order"

    asyncio.run(app.cmd_execute(create_window.message))
    assert attempts == ["request"]
    assert _plan_row(create_window.db_path, create_window.plan_id)["execution_state"] == "creating_order"

    asyncio.run(app.recover_stale_plan_claims())
    asyncio.run(app.cmd_execute(create_window.message))
    assert attempts == ["request"]


def test_cancellation_during_create_retains_gate_and_blocks_restart_retry(
    create_window, monkeypatch
):
    request_started = threading.Event()
    release_request = threading.Event()
    attempts = []

    def delayed_create(*args, **kwargs):
        attempts.append("request")
        request_started.set()
        assert release_request.wait(timeout=5)
        return _order_response(1)

    monkeypatch.setattr(app, "create_fixedfloat_order", delayed_create)

    async def scenario():
        task = asyncio.create_task(app.cmd_execute(create_window.message))
        await asyncio.to_thread(request_started.wait, 5)
        task.cancel()
        release_request.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert attempts == ["request"]
    assert _plan_row(create_window.db_path, create_window.plan_id)["execution_state"] == "creating_order"

    asyncio.run(app.recover_stale_plan_claims())
    asyncio.run(app.cmd_execute(create_window.message))
    assert attempts == ["request"]


@pytest.mark.parametrize("response", [None, {}, {"token": "token-without-id"}])
def test_invalid_attempted_create_response_retains_gate(
    create_window, monkeypatch, response
):
    attempts = []

    def invalid_create(*args, **kwargs):
        attempts.append("request")
        return response

    monkeypatch.setattr(app, "create_fixedfloat_order", invalid_create)
    asyncio.run(app.cmd_execute(create_window.message))
    asyncio.run(app.recover_stale_plan_claims())
    asyncio.run(app.cmd_execute(create_window.message))

    assert attempts == ["request"]
    plan = _plan_row(create_window.db_path, create_window.plan_id)
    assert plan["execution_state"] == "creating_order"
    assert plan["active_order_id"] is None


def test_crash_during_active_order_commit_leaves_no_partial_gate(
    create_window, monkeypatch
):
    _install_successful_create(monkeypatch, create_window)
    production_open_db = app.open_db
    injected = False

    class CommitCrashProxy:
        def __init__(self, db):
            self._db = db
            self.active_gate_update = False

        def __getattr__(self, name):
            return getattr(self._db, name)

        def execute(self, sql, parameters=None):
            if "UPDATE dca_plans SET active_order_id" in sql:
                self.active_gate_update = True
            if parameters is None:
                return self._db.execute(sql)
            return self._db.execute(sql, parameters)

        async def commit(self):
            nonlocal injected
            if self.active_gate_update and not injected:
                injected = True
                raise SimulatedProcessCrash("during active-order commit")
            return await self._db.commit()

    @asynccontextmanager
    async def crash_open_db():
        async with production_open_db() as db:
            yield CommitCrashProxy(db)

    monkeypatch.setattr(app, "open_db", crash_open_db)
    with pytest.raises(SimulatedProcessCrash):
        asyncio.run(app.cmd_execute(create_window.message))

    plan = _plan_row(create_window.db_path, create_window.plan_id)
    assert create_window.external_creates == [1]
    assert injected is True
    assert plan["execution_state"] == "creating_order"
    assert plan["active_order_id"] is None
    assert plan["active_order_token"] is None
    assert plan["active_order_address"] is None
    assert plan["active_order_amount"] is None
    assert plan["active_order_expires"] is None


def test_active_order_persistence_operational_error_retains_gate(
    create_window, monkeypatch
):
    _install_successful_create(monkeypatch, create_window)
    production_open_db = app.open_db
    injected = False

    class PersistenceErrorProxy:
        def __init__(self, db):
            self._db = db

        def __getattr__(self, name):
            return getattr(self._db, name)

        def execute(self, sql, parameters=None):
            nonlocal injected
            if "UPDATE dca_plans SET active_order_id" in sql:
                injected = True
                raise sqlite3.OperationalError("injected active-order persistence failure")
            if parameters is None:
                return self._db.execute(sql)
            return self._db.execute(sql, parameters)

    @asynccontextmanager
    async def failing_open_db():
        async with production_open_db() as db:
            yield PersistenceErrorProxy(db)

    monkeypatch.setattr(app, "open_db", failing_open_db)
    asyncio.run(app.cmd_execute(create_window.message))
    monkeypatch.setattr(app, "open_db", production_open_db)

    plan = _plan_row(create_window.db_path, create_window.plan_id)
    assert injected is True
    assert create_window.external_creates == [1]
    assert plan["execution_state"] == "creating_order"
    assert plan["active_order_id"] is None

    asyncio.run(app.cmd_execute(create_window.message))
    assert create_window.external_creates == [1]


@pytest.mark.parametrize(
    ("source_state", "expected"),
    [
        ("scheduled", True),
        ("skipped", True),
        ("expired", True),
        ("claiming", False),
        ("creating_order", False),
        ("confirming", False),
        ("awaiting_confirmation", False),
    ],
)
def test_skip_missed_cycle_uses_scheduler_owned_state_cas(
    create_window, source_state, expected
):
    with sqlite3.connect(create_window.db_path) as db:
        db.execute(
            "UPDATE dca_plans SET execution_state = ? WHERE id = ?",
            (source_state, create_window.plan_id),
        )
        db.commit()

    applied = asyncio.run(
        app.skip_missed_dca_cycle(
            plan_id=create_window.plan_id,
            user_id=USER_ID,
            scheduled_time=NOW,
            interval_hours=24,
        )
    )

    assert applied is expected
    row = _plan_row(create_window.db_path, create_window.plan_id)
    assert row["execution_state"] == ("skipped" if expected else source_state)


def test_skip_missed_cycle_rejects_active_order_and_inflight_evidence(create_window):
    with sqlite3.connect(create_window.db_path) as db:
        db.execute(
            "UPDATE dca_plans SET active_order_id = 'active-order' WHERE id = ?",
            (create_window.plan_id,),
        )
        db.commit()
    assert asyncio.run(
        app.skip_missed_dca_cycle(
            plan_id=create_window.plan_id,
            user_id=USER_ID,
            scheduled_time=NOW,
            interval_hours=24,
        )
    ) is False
    assert _plan_row(create_window.db_path, create_window.plan_id)["execution_state"] == "scheduled"

    with sqlite3.connect(create_window.db_path) as db:
        db.execute(
            "UPDATE dca_plans SET active_order_id = NULL WHERE id = ?",
            (create_window.plan_id,),
        )
        db.execute(
            "INSERT INTO sent_transactions "
            "(user_id, plan_id, order_id, network_key, amount, deposit_address, state) "
            "VALUES (?, ?, 'inflight-order', ?, 25.0, ?, 'pending')",
            (USER_ID, create_window.plan_id, NETWORK, DEPOSIT_ADDRESS),
        )
        db.commit()
    assert asyncio.run(
        app.skip_missed_dca_cycle(
            plan_id=create_window.plan_id,
            user_id=USER_ID,
            scheduled_time=NOW,
            interval_hours=24,
        )
    ) is False
    assert _plan_row(create_window.db_path, create_window.plan_id)["execution_state"] == "scheduled"


@pytest.mark.parametrize(
    ("source_state", "expected"),
    [
        ("scheduled", True),
        ("confirming", True),
        ("skipped", True),
        ("expired", True),
        ("claiming", False),
        ("creating_order", False),
        ("awaiting_confirmation", False),
    ],
)
def test_claim_plan_execution_has_explicit_source_state_allowlist(
    create_window, source_state, expected
):
    with sqlite3.connect(create_window.db_path) as db:
        db.execute(
            "UPDATE dca_plans SET execution_state = ? WHERE id = ?",
            (source_state, create_window.plan_id),
        )
        db.commit()

    assert asyncio.run(
        app.claim_plan_execution(create_window.plan_id, USER_ID)
    ) is expected
    row = _plan_row(create_window.db_path, create_window.plan_id)
    assert row["execution_state"] == ("claiming" if expected else source_state)


def test_stale_scheduler_skip_cannot_erase_concurrent_creating_order(
    create_window, monkeypatch
):
    create_started = threading.Event()
    release_create = threading.Event()

    def delayed_create(*args, **kwargs):
        create_window.external_creates.append(1)
        assert _plan_row(create_window.db_path, create_window.plan_id)["execution_state"] == "creating_order"
        create_started.set()
        assert release_create.wait(timeout=5)
        return _order_response(1)

    monkeypatch.setattr(app, "create_fixedfloat_order", delayed_create)

    async def scenario():
        scheduler_snapshot_read = asyncio.Event()
        allow_scheduler_skip = asyncio.Event()

        async def stale_scheduler():
            async with app.open_db() as db:
                async with db.execute(
                    "SELECT execution_state FROM dca_plans WHERE id = ?",
                    (create_window.plan_id,),
                ) as cur:
                    snapshot = await cur.fetchone()
            assert snapshot == ("scheduled",)
            scheduler_snapshot_read.set()
            await allow_scheduler_skip.wait()
            return await app.skip_missed_dca_cycle(
                plan_id=create_window.plan_id,
                user_id=USER_ID,
                scheduled_time=NOW,
                interval_hours=24,
            )

        scheduler_task = asyncio.create_task(stale_scheduler())
        await scheduler_snapshot_read.wait()
        execute_task = asyncio.create_task(app.cmd_execute(create_window.message))
        assert await asyncio.to_thread(create_started.wait, 5)
        allow_scheduler_skip.set()
        assert await scheduler_task is False
        assert _plan_row(create_window.db_path, create_window.plan_id)["execution_state"] == "creating_order"
        release_create.set()
        await execute_task

    asyncio.run(scenario())

    plan = _plan_row(create_window.db_path, create_window.plan_id)
    assert create_window.external_creates == [1]
    assert plan["execution_state"] == "scheduled"
    assert plan["active_order_id"] == "external-order-1"
    assert plan["active_order_token"] == "external-token-1"

    asyncio.run(app.recover_stale_plan_claims())
    asyncio.run(app.cmd_execute(create_window.message))
    assert create_window.external_creates == [1]


def test_valid_response_atomically_exits_gate_and_preserves_payment_pipeline(
    create_window, monkeypatch
):
    _install_successful_create(monkeypatch, create_window)
    _seed_wallet(create_window)
    observed = []

    async def successful_payment(**kwargs):
        row = _transaction_rows(create_window.db_path, create_window.plan_id)[0]
        plan = _plan_row(create_window.db_path, create_window.plan_id)
        observed.append((plan["execution_state"], plan["active_order_id"], row["state"]))
        kwargs["persist_payment_intent"](25_000_000, 6)
        kwargs["persist_prepared_tx"](
            "transfer", "0x" + "cd" * 32, 21, "0x02f86c" + "22" * 32
        )
        return True, None, "0x" + "cd" * 32, ""

    monkeypatch.setattr(app, "auto_send_usdt", successful_payment)
    asyncio.run(app.cmd_execute(create_window.message))

    plan = _plan_row(create_window.db_path, create_window.plan_id)
    row = _transaction_rows(create_window.db_path, create_window.plan_id)[0]
    assert observed == [("scheduled", "external-order-1", "transfering")]
    assert plan["execution_state"] == "scheduled"
    assert plan["active_order_id"] == "external-order-1"
    assert plan["active_order_token"] == "external-token-1"
    assert (row["state"], row["amount_units"], row["token_decimals"]) == (
        "sent",
        "25000000",
        6,
    )
    assert row["transfer_raw_tx"] is not None


def test_stale_recovery_releases_claiming_but_never_creating_order(create_window):
    with sqlite3.connect(create_window.db_path) as db:
        db.execute(
            "UPDATE dca_plans SET execution_state = 'claiming', active_order_id = NULL WHERE id = ?",
            (create_window.plan_id,),
        )
        db.commit()
    asyncio.run(app.recover_stale_plan_claims())
    assert _plan_row(create_window.db_path, create_window.plan_id)["execution_state"] == "scheduled"

    with sqlite3.connect(create_window.db_path) as db:
        db.execute(
            "UPDATE dca_plans SET execution_state = 'creating_order' WHERE id = ?",
            (create_window.plan_id,),
        )
        db.commit()
    asyncio.run(app.recover_stale_plan_claims())
    assert _plan_row(create_window.db_path, create_window.plan_id)["execution_state"] == "creating_order"


def test_manual_and_scheduler_do_not_cross_creating_order_gate(
    create_window, monkeypatch
):
    with sqlite3.connect(create_window.db_path) as db:
        db.execute(
            "UPDATE dca_plans SET execution_state = 'creating_order' WHERE id = ?",
            (create_window.plan_id,),
        )
        db.commit()
    create_calls = []

    def forbidden_create(*args, **kwargs):
        create_calls.append("create")
        raise AssertionError("creating_order reached FixedFloat create")

    async def forbidden_confirmation(*args, **kwargs):
        raise AssertionError("creating_order reached confirmation creation")

    async def stop_after_iteration(*args, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(app, "create_fixedfloat_order", forbidden_create)
    monkeypatch.setattr(app, "create_dca_confirmation_request", forbidden_confirmation)
    asyncio.run(app.cmd_execute(create_window.message))
    monkeypatch.setattr(app.asyncio, "sleep", stop_after_iteration)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(app.dca_scheduler())

    assert create_calls == []
    assert _plan_row(create_window.db_path, create_window.plan_id)["execution_state"] == "creating_order"


def test_confirmation_finally_cannot_clear_concurrent_creating_order(
    create_window, monkeypatch
):
    scheduled_at = NOW
    with sqlite3.connect(create_window.db_path) as db:
        db.execute(
            "UPDATE dca_plans SET execution_state = 'awaiting_confirmation', "
            "confirmation_message_id = 777, confirmation_expires_at = ?, "
            "confirmation_scheduled_at = ? WHERE id = ?",
            (NOW + 600, scheduled_at, create_window.plan_id),
        )
        db.commit()

    async def transition_after_confirmation_cas(*args, **kwargs):
        with sqlite3.connect(create_window.db_path) as db:
            db.execute(
                "UPDATE dca_plans SET execution_state = 'creating_order' WHERE id = ?",
                (create_window.plan_id,),
            )
            db.commit()

    def forbidden_create(*args, **kwargs):
        raise AssertionError("confirmation crossed creating_order gate")

    monkeypatch.setattr(app, "edit_confirmation_message", transition_after_confirmation_cas)
    monkeypatch.setattr(app, "create_fixedfloat_order", forbidden_create)
    callback = FakeCallback(f"dca_confirm:{create_window.plan_id}:{scheduled_at}")
    asyncio.run(app.cb_dca_confirm(callback))

    assert _plan_row(create_window.db_path, create_window.plan_id)["execution_state"] == "creating_order"


def test_committed_active_gate_without_payment_row_blocks_all_new_create_paths(
    create_window, monkeypatch
):
    _install_successful_create(monkeypatch, create_window)

    def crash_before_payment_row(amount):
        raise SimulatedProcessCrash("after active gate, before sent_transactions")

    monkeypatch.setattr(app, "require_exact_fixedfloat_amount", crash_before_payment_row)
    with pytest.raises(SimulatedProcessCrash):
        asyncio.run(app.cmd_execute(create_window.message))

    plan = _plan_row(create_window.db_path, create_window.plan_id)
    assert plan["active_order_id"] == "external-order-1"
    assert plan["active_order_token"] == "external-token-1"
    assert plan["execution_state"] == "scheduled"
    assert _transaction_rows(create_window.db_path, create_window.plan_id) == []

    # Startup recovery has no payment row to reconstruct, and stale-claim cleanup
    # cannot clear a persisted active gate.
    asyncio.run(app.recovery_scan_pending_transactions())
    asyncio.run(app.recover_stale_plan_claims())
    monkeypatch.setattr(app, "require_exact_fixedfloat_amount", app.validate_decimal_amount_text)
    asyncio.run(app.cmd_execute(create_window.message))

    async def pending_status(*args, **kwargs):
        return "pending"

    async def forbidden_confirmation(*args, **kwargs):
        raise AssertionError("scheduler attempted a new execution")

    async def stop_scheduler(*args, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(app, "get_fixedfloat_order_status_with_retry", pending_status)
    monkeypatch.setattr(app, "create_dca_confirmation_request", forbidden_confirmation)
    monkeypatch.setattr(app.asyncio, "sleep", stop_scheduler)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(app.dca_scheduler())

    # The monitor also observes the existing gate and never owns order creation.
    monitor_sleeps = 0

    async def run_one_monitor_iteration(*args, **kwargs):
        nonlocal monitor_sleeps
        monitor_sleeps += 1
        if monitor_sleeps > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(app.asyncio, "sleep", run_one_monitor_iteration)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(app.order_monitor())

    assert create_window.external_creates == [1]
    assert _transaction_rows(create_window.db_path, create_window.plan_id) == []
    assert _plan_row(create_window.db_path, create_window.plan_id)["active_order_id"] == "external-order-1"


def test_crash_after_payment_row_before_transfer_claim_is_fail_closed_on_restart(
    create_window, monkeypatch
):
    _install_successful_create(monkeypatch, create_window)
    _seed_wallet(create_window)

    async def crash_before_transfer_claim(*args, **kwargs):
        raise SimulatedProcessCrash("after sent row, before transfer claim")

    monkeypatch.setattr(app, "claim_auto_send_execution", crash_before_transfer_claim)
    with pytest.raises(SimulatedProcessCrash):
        asyncio.run(app.cmd_execute(create_window.message))

    row = _transaction_rows(create_window.db_path, create_window.plan_id)[0]
    assert row["state"] == "sending"
    assert row["amount_units"] is None
    assert row["transfer_raw_tx"] is None

    asyncio.run(app.recovery_scan_pending_transactions())
    row = _transaction_rows(create_window.db_path, create_window.plan_id)[0]
    assert row["state"] == "sending"
    assert "missing persisted exact payment intent" in row["error_message"]
    asyncio.run(app.cmd_execute(create_window.message))
    assert create_window.external_creates == [1]


def test_crash_after_transfer_claim_before_raw_uses_persisted_exact_intent_on_restart(
    create_window, monkeypatch
):
    _install_successful_create(monkeypatch, create_window)
    _seed_wallet(create_window)

    async def crash_before_raw_persistence(**kwargs):
        kwargs["persist_payment_intent"](25_000_000, 6)
        raise SimulatedProcessCrash("after transfer claim, before signed raw persistence")

    monkeypatch.setattr(app, "auto_send_usdt", crash_before_raw_persistence)
    with pytest.raises(SimulatedProcessCrash):
        asyncio.run(app.cmd_execute(create_window.message))

    row = _transaction_rows(create_window.db_path, create_window.plan_id)[0]
    assert (row["state"], row["amount_units"], row["token_decimals"]) == (
        "transfering",
        "25000000",
        6,
    )
    assert row["transfer_raw_tx"] is None

    resume_calls = []

    async def characterize_resume(**kwargs):
        resume_calls.append(kwargs)
        return "blocked", None, None, "characterized before-raw restart"

    monkeypatch.setattr(app, "resume_transfer_after_approve", characterize_resume)
    asyncio.run(app.recovery_scan_pending_transactions())

    assert len(resume_calls) == 1
    assert resume_calls[0]["amount_units"] == 25_000_000
    assert resume_calls[0]["token_decimals"] == 6
    assert _transaction_rows(create_window.db_path, create_window.plan_id)[0]["state"] == "blocked"


def test_crash_after_raw_persistence_rebroadcasts_identical_bytes_on_restart(
    create_window, monkeypatch
):
    _install_successful_create(monkeypatch, create_window)
    _seed_wallet(create_window)
    tx_hash = "0x" + "ab" * 32
    raw_tx = "0x02f86c0180843b9aca0082520894" + "11" * 20 + "8080c0"

    async def crash_after_raw_persistence(**kwargs):
        kwargs["persist_payment_intent"](25_000_000, 6)
        kwargs["persist_prepared_tx"]("transfer", tx_hash, 17, raw_tx)
        raise SimulatedProcessCrash("after signed raw persistence")

    monkeypatch.setattr(app, "auto_send_usdt", crash_after_raw_persistence)
    with pytest.raises(SimulatedProcessCrash):
        asyncio.run(app.cmd_execute(create_window.message))

    row = _transaction_rows(create_window.db_path, create_window.plan_id)[0]
    assert (row["transfer_tx_hash"], row["transfer_tx_nonce"], row["transfer_raw_tx"]) == (
        tx_hash,
        17,
        raw_tx,
    )

    async def pending_status(network_key, persisted_hash):
        assert (network_key, persisted_hash) == (NETWORK, tx_hash)
        return "pending"

    rebroadcasts = []

    async def capture_rebroadcast(network_key, persisted_raw, persisted_hash, action):
        rebroadcasts.append((network_key, persisted_raw, persisted_hash, action))
        return persisted_hash

    monkeypatch.setattr(app, "get_transfer_tx_status", pending_status)
    monkeypatch.setattr(app, "rebroadcast_persisted_erc20_transaction", capture_rebroadcast)
    asyncio.run(app.recovery_scan_pending_transactions())

    assert rebroadcasts == [(NETWORK, raw_tx, tx_hash, "transfer")]
    row = _transaction_rows(create_window.db_path, create_window.plan_id)[0]
    assert row["state"] == "tx_pending"
    assert row["transfer_raw_tx"] == raw_tx
    assert row["transfer_tx_nonce"] == 17


def test_fixedfloat_create_request_has_no_established_idempotency_identifier(monkeypatch):
    captured = []

    def capture_request(method, params):
        captured.append((method, dict(params)))
        return _order_response(1)

    monkeypatch.setattr(app, "ff_request", capture_request)
    result = app.create_fixedfloat_order(NETWORK, 25.0, BTC_ADDRESS)

    assert result["id"] == "external-order-1"
    assert captured == [
        (
            "create",
            {
                "type": "fixed",
                "fromCcy": app.get_fixedfloat_symbol(NETWORK),
                "toCcy": "BTC",
                "direction": "from",
                "amount": 25.0,
                "toAddress": BTC_ADDRESS,
            },
        )
    ]
