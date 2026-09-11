import asyncio
import sqlite3
import threading

import pytest

import bot as app


def install_worker_statement_hooks(monkeypatch, worker_hooks):
    original_execute = app.aiosqlite.Connection._execute
    submitted = []
    hooked = set()

    async def intercept_execute(connection, function, *args, **kwargs):
        normalized_sql = " ".join(str(args[0]).split()).upper() if args else ""
        submitted.append(normalized_sql)
        worker_hook = worker_hooks.get(normalized_sql)
        if worker_hook is not None and normalized_sql not in hooked:
            hooked.add(normalized_sql)

            def hooked_function(*worker_args, **worker_kwargs):
                return worker_hook(function, worker_args, worker_kwargs)

            return await original_execute(
                connection, hooked_function, *args, **kwargs
            )
        return await original_execute(connection, function, *args, **kwargs)

    monkeypatch.setattr(app.aiosqlite.Connection, "_execute", intercept_execute)
    return submitted


def assert_write_lock_released(db_path):
    with sqlite3.connect(db_path, timeout=0.1) as db:
        db.execute("BEGIN IMMEDIATE")
        db.rollback()


def cancellation_with_commit_diagnostic(propagated_cancellation):
    """Return the cancellation instance carrying scheduler diagnostics.

    Python 3.9 synthesizes a new CancelledError when an already-cancelled Task
    is awaited, retaining the explicitly raised instance as its context.
    """
    candidates = (
        propagated_cancellation,
        propagated_cancellation.__context__,
    )
    for cancellation in candidates:
        if cancellation is not None and hasattr(
            cancellation, "scheduler_transaction_commit_error"
        ):
            return cancellation
    raise AssertionError("scheduler COMMIT diagnostic is absent from cancellation chain")


def initialize_db(db_path, monkeypatch):
    monkeypatch.setattr(app, "DB_PATH", str(db_path))
    asyncio.run(app.init_db())


async def wait_until_submitted(submitted, statement):
    for _ in range(1000):
        if statement in submitted:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"statement was not submitted: {statement}")


def seed_scheduler_sent_order(db_path, now):
    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT INTO dca_plans "
            "(id, user_id, from_asset, amount, interval_hours, btc_address, "
            "next_run, active, active_order_id, active_order_token, active_order_expires) "
            "VALUES (7, 10001, 'USDT-ARB', 25, 24, 'bc1test', ?, 1, "
            "'scheduler-order', 'scheduler-token', ?)",
            (now, now + 3600),
        )
        db.execute(
            "INSERT INTO sent_transactions "
            "(user_id, plan_id, order_id, order_token, network_key, amount, "
            "deposit_address, state, sent_at) "
            "VALUES (10001, 7, 'scheduler-order', 'scheduler-token', "
            "'USDT-ARB', 25, '0xdeposit', 'sent', ?)",
            (now,),
        )


def test_scheduler_commit_failure_rolls_back_before_nested_work(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "scheduler-commit-cleanup.sqlite3"
    initialize_db(db_path, monkeypatch)
    now = 1_700_000_000
    seed_scheduler_sent_order(db_path, now)

    monkeypatch.setattr(app.time, "time", lambda: now)
    async def display_number(*_args, **_kwargs):
        return 1

    monkeypatch.setattr(app, "get_plan_display_number", display_number)

    async def active_status(*_args, **_kwargs):
        return "NEW"

    monkeypatch.setattr(app, "get_fixedfloat_order_status_with_retry", active_status)
    events = []

    def fail_first_commit(_function, _args, _kwargs):
        events.append("commit-error")
        raise sqlite3.OperationalError("injected scheduler COMMIT failure")

    submitted = install_worker_statement_hooks(
        monkeypatch, {"COMMIT;": fail_first_commit}
    )

    async def nested_telegram(*_args, **_kwargs):
        with sqlite3.connect(db_path, timeout=0.1) as probe:
            probe.execute("BEGIN IMMEDIATE")
            probe.rollback()
        events.append("nested-after-cleanup")

    original_sleep = asyncio.sleep

    async def stop_after_iteration(_seconds):
        if _seconds == 60:
            raise asyncio.CancelledError
        await original_sleep(_seconds)

    monkeypatch.setattr(app.bot, "send_message", nested_telegram)
    monkeypatch.setattr(app.asyncio, "sleep", stop_after_iteration)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(app.dca_scheduler())

    assert events == ["commit-error", "nested-after-cleanup"]
    assert submitted.count("ROLLBACK;") == 1
    with sqlite3.connect(db_path) as db:
        assert db.execute(
            "SELECT active_order_id FROM dca_plans WHERE id = 7"
        ).fetchone()[0] == "scheduler-order"
    assert_write_lock_released(db_path)


def test_scheduler_rollback_failure_stops_nested_business_work(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "scheduler-cleanup-fail-closed.sqlite3"
    initialize_db(db_path, monkeypatch)
    now = 1_700_000_000
    seed_scheduler_sent_order(db_path, now)
    monkeypatch.setattr(app.time, "time", lambda: now)

    async def display_number(*_args, **_kwargs):
        return 1

    async def active_status(*_args, **_kwargs):
        return "NEW"

    monkeypatch.setattr(app, "get_plan_display_number", display_number)
    monkeypatch.setattr(app, "get_fixedfloat_order_status_with_retry", active_status)

    def fail_commit(_function, _args, _kwargs):
        raise sqlite3.OperationalError("primary scheduler COMMIT failure")

    def fail_rollback(_function, _args, _kwargs):
        raise sqlite3.OperationalError("secondary scheduler ROLLBACK failure")

    submitted = install_worker_statement_hooks(
        monkeypatch, {"COMMIT;": fail_commit, "ROLLBACK;": fail_rollback}
    )
    nested_calls = []

    async def forbidden_nested(*_args, **_kwargs):
        nested_calls.append(True)

    original_sleep = asyncio.sleep

    async def stop_after_iteration(seconds):
        if seconds == 60:
            raise asyncio.CancelledError
        await original_sleep(seconds)

    monkeypatch.setattr(app.bot, "send_message", forbidden_nested)
    monkeypatch.setattr(app.asyncio, "sleep", stop_after_iteration)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(app.dca_scheduler())

    assert nested_calls == []
    assert submitted.count("ROLLBACK;") == 1
    with sqlite3.connect(db_path) as db:
        assert db.execute(
            "SELECT active_order_id FROM dca_plans WHERE id = 7"
        ).fetchone()[0] == "scheduler-order"
    assert_write_lock_released(db_path)


def test_commit_error_with_inactive_transaction_does_not_rollback(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "scheduler-committed-error.sqlite3"
    initialize_db(db_path, monkeypatch)

    def commit_then_raise(function, args, kwargs):
        function(*args, **kwargs)
        raise sqlite3.OperationalError("error after real COMMIT")

    submitted = install_worker_statement_hooks(
        monkeypatch, {"COMMIT;": commit_then_raise}
    )

    async def scenario():
        async with app.open_db() as db:
            await db.execute("INSERT INTO wallets (user_id, wallet_address) VALUES (1, '0x1')")
            with pytest.raises(sqlite3.OperationalError, match="after real COMMIT"):
                await app._commit_scheduler_transaction(db)
            assert db.in_transaction is False

    asyncio.run(scenario())

    assert "ROLLBACK;" not in submitted
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM wallets").fetchone()[0] == 1
    assert_write_lock_released(db_path)


def test_scheduler_rollback_error_keeps_commit_error_primary(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "scheduler-rollback-error.sqlite3"
    initialize_db(db_path, monkeypatch)

    def fail_commit(_function, _args, _kwargs):
        raise sqlite3.OperationalError("primary scheduler COMMIT failure")

    def fail_rollback(_function, _args, _kwargs):
        raise sqlite3.OperationalError("secondary scheduler ROLLBACK failure")

    submitted = install_worker_statement_hooks(
        monkeypatch, {"COMMIT;": fail_commit, "ROLLBACK;": fail_rollback}
    )

    async def scenario():
        async with app.open_db() as db:
            await db.execute("INSERT INTO wallets (user_id, wallet_address) VALUES (2, '0x2')")
            with pytest.raises(
                sqlite3.OperationalError, match="primary scheduler COMMIT failure"
            ) as raised:
                await app._commit_scheduler_transaction(db)
            assert isinstance(raised.value.__cause__, sqlite3.OperationalError)
            assert "secondary scheduler ROLLBACK failure" in str(raised.value.__cause__)
            assert raised.value.scheduler_transaction_rollback_error is raised.value.__cause__
            raise raised.value

    with pytest.raises(sqlite3.OperationalError, match="primary scheduler COMMIT failure"):
        asyncio.run(scenario())

    assert submitted.count("ROLLBACK;") == 1
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM wallets").fetchone()[0] == 0
    assert_write_lock_released(db_path)


def test_scheduler_running_commit_cancellation_waits_for_acknowledgement(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "scheduler-cancelled-commit.sqlite3"
    initialize_db(db_path, monkeypatch)
    commit_running = threading.Event()
    release_commit = threading.Event()

    def delay_commit(function, args, kwargs):
        commit_running.set()
        if not release_commit.wait(timeout=5):
            raise RuntimeError("timed out waiting to release scheduler COMMIT")
        return function(*args, **kwargs)

    submitted = install_worker_statement_hooks(monkeypatch, {"COMMIT;": delay_commit})

    async def scenario():
        async with app.open_db() as db:
            await db.execute("INSERT INTO wallets (user_id, wallet_address) VALUES (3, '0x3')")
            commit_task = asyncio.create_task(app._commit_scheduler_transaction(db))
            assert await asyncio.to_thread(commit_running.wait, 5)
            commit_task.cancel()
            await asyncio.sleep(0)
            release_commit.set()
            with pytest.raises(asyncio.CancelledError):
                await commit_task
            assert db.in_transaction is False

    asyncio.run(scenario())

    assert submitted.count("COMMIT;") == 1
    assert "ROLLBACK;" not in submitted
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM wallets").fetchone()[0] == 1
    assert_write_lock_released(db_path)


def test_scheduler_true_queued_commit_cancellation_waits_for_worker_result(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "scheduler-true-queued-commit.sqlite3"
    initialize_db(db_path, monkeypatch)
    blocker_started = threading.Event()
    release_blocker = threading.Event()
    commit_started = threading.Event()
    blocker_sql = "SELECT 1 /* COMMIT QUEUE BLOCKER */"

    def block_worker(function, args, kwargs):
        blocker_started.set()
        if not release_blocker.wait(timeout=5):
            raise RuntimeError("timed out waiting to release COMMIT queue blocker")
        return function(*args, **kwargs)

    def observe_commit(function, args, kwargs):
        commit_started.set()
        return function(*args, **kwargs)

    submitted = install_worker_statement_hooks(
        monkeypatch,
        {blocker_sql: block_worker, "COMMIT;": observe_commit},
    )

    async def scenario():
        async with app.open_db() as db:
            await db.execute(
                "INSERT INTO wallets (user_id, wallet_address) VALUES (30, '0x30')"
            )
            blocker_task = asyncio.create_task(db.execute(blocker_sql))
            assert await asyncio.to_thread(blocker_started.wait, 5)

            commit_task = asyncio.create_task(app._commit_scheduler_transaction(db))
            await wait_until_submitted(submitted, "COMMIT;")
            assert commit_started.is_set() is False

            commit_task.cancel()
            await asyncio.sleep(0)
            assert commit_started.is_set() is False
            release_blocker.set()

            blocker_cursor = await blocker_task
            await blocker_cursor.close()
            with pytest.raises(asyncio.CancelledError):
                await commit_task
            assert commit_started.is_set() is True
            assert db.in_transaction is False

    asyncio.run(scenario())

    assert submitted.index(blocker_sql) < submitted.index("COMMIT;")
    assert submitted.count("COMMIT;") == 1
    assert "ROLLBACK;" not in submitted
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM wallets").fetchone()[0] == 1
    assert_write_lock_released(db_path)


def test_scheduler_pending_cancellation_before_commit_rolls_back(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "scheduler-cancel-before-commit.sqlite3"
    initialize_db(db_path, monkeypatch)
    submitted = install_worker_statement_hooks(monkeypatch, {})

    async def scenario():
        async with app.open_db() as db:
            await db.execute("INSERT INTO wallets (user_id, wallet_address) VALUES (4, '0x4')")
            asyncio.current_task().cancel()
            with pytest.raises(asyncio.CancelledError):
                await app._commit_scheduler_transaction(db)
            assert db.in_transaction is False

    asyncio.run(scenario())

    assert "COMMIT;" not in submitted
    assert submitted.count("ROLLBACK;") == 1
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM wallets").fetchone()[0] == 0
    assert_write_lock_released(db_path)


def test_scheduler_rollback_cancellation_is_acknowledged(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "scheduler-cancelled-rollback.sqlite3"
    initialize_db(db_path, monkeypatch)
    rollback_running = threading.Event()
    release_rollback = threading.Event()

    def fail_commit(_function, _args, _kwargs):
        raise sqlite3.OperationalError("primary scheduler COMMIT failure")

    def delay_rollback(function, args, kwargs):
        rollback_running.set()
        if not release_rollback.wait(timeout=5):
            raise RuntimeError("timed out waiting to release scheduler ROLLBACK")
        return function(*args, **kwargs)

    submitted = install_worker_statement_hooks(
        monkeypatch, {"COMMIT;": fail_commit, "ROLLBACK;": delay_rollback}
    )

    async def transaction_task():
        async with app.open_db() as db:
            await db.execute("INSERT INTO wallets (user_id, wallet_address) VALUES (5, '0x5')")
            try:
                await app._commit_scheduler_transaction(db)
            except sqlite3.OperationalError as error:
                assert isinstance(
                    error.scheduler_transaction_rollback_cancellation,
                    asyncio.CancelledError,
                )
                assert db.in_transaction is False
                raise

    async def scenario():
        task = asyncio.create_task(transaction_task())
        assert await asyncio.to_thread(rollback_running.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        release_rollback.set()
        with pytest.raises(
            sqlite3.OperationalError, match="primary scheduler COMMIT failure"
        ):
            await task

    asyncio.run(scenario())

    assert submitted.count("ROLLBACK;") == 1
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM wallets").fetchone()[0] == 0
    assert_write_lock_released(db_path)


def test_scheduler_true_queued_rollback_cancellation_waits_for_worker_result(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "scheduler-true-queued-rollback.sqlite3"
    initialize_db(db_path, monkeypatch)
    commit_started = threading.Event()
    release_commit_error = threading.Event()
    blocker_started = threading.Event()
    release_blocker = threading.Event()
    rollback_started = threading.Event()
    blocker_sql = "SELECT 1 /* ROLLBACK QUEUE BLOCKER */"

    def delayed_commit_error(_function, _args, _kwargs):
        commit_started.set()
        if not release_commit_error.wait(timeout=5):
            raise RuntimeError("timed out waiting to fail scheduler COMMIT")
        raise sqlite3.OperationalError("primary queued scheduler COMMIT failure")

    def block_worker(function, args, kwargs):
        blocker_started.set()
        if not release_blocker.wait(timeout=5):
            raise RuntimeError("timed out waiting to release ROLLBACK queue blocker")
        return function(*args, **kwargs)

    def observe_rollback(function, args, kwargs):
        rollback_started.set()
        return function(*args, **kwargs)

    submitted = install_worker_statement_hooks(
        monkeypatch,
        {
            "COMMIT;": delayed_commit_error,
            blocker_sql: block_worker,
            "ROLLBACK;": observe_rollback,
        },
    )
    transaction_db = None

    async def transaction_task():
        nonlocal transaction_db
        async with app.open_db() as db:
            transaction_db = db
            await db.execute(
                "INSERT INTO wallets (user_id, wallet_address) VALUES (50, '0x50')"
            )
            try:
                await app._commit_scheduler_transaction(db)
            except sqlite3.OperationalError as error:
                assert isinstance(
                    error.scheduler_transaction_rollback_cancellation,
                    asyncio.CancelledError,
                )
                assert db.in_transaction is False
                raise

    async def scenario():
        transaction = asyncio.create_task(transaction_task())
        assert await asyncio.to_thread(commit_started.wait, 5)
        assert transaction_db is not None
        blocker_task = asyncio.create_task(transaction_db.execute(blocker_sql))
        await wait_until_submitted(submitted, blocker_sql)
        release_commit_error.set()
        assert await asyncio.to_thread(blocker_started.wait, 5)
        await wait_until_submitted(submitted, "ROLLBACK;")
        assert rollback_started.is_set() is False

        transaction.cancel()
        await asyncio.sleep(0)
        assert rollback_started.is_set() is False
        release_blocker.set()

        blocker_cursor = await blocker_task
        await blocker_cursor.close()

        with pytest.raises(
            sqlite3.OperationalError, match="primary queued scheduler COMMIT failure"
        ):
            await transaction
        assert rollback_started.is_set() is True

    asyncio.run(scenario())

    assert submitted.index("COMMIT;") < submitted.index(blocker_sql)
    assert submitted.index(blocker_sql) < submitted.index("ROLLBACK;")
    assert submitted.count("ROLLBACK;") == 1
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM wallets").fetchone()[0] == 0
    assert_write_lock_released(db_path)


def test_scheduler_running_commit_error_after_cancellation_stops_business_work(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "scheduler-running-cancelled-commit-error.sqlite3"
    initialize_db(db_path, monkeypatch)
    now = 1_700_000_000
    seed_scheduler_sent_order(db_path, now)
    commit_started = threading.Event()
    release_commit_error = threading.Event()

    monkeypatch.setattr(app.time, "time", lambda: now)

    async def display_number(*_args, **_kwargs):
        return 1

    async def active_status(*_args, **_kwargs):
        return "NEW"

    monkeypatch.setattr(app, "get_plan_display_number", display_number)
    monkeypatch.setattr(app, "get_fixedfloat_order_status_with_retry", active_status)

    commit_error = sqlite3.OperationalError(
        "cancelled running scheduler COMMIT failure"
    )

    def delayed_commit_error(_function, _args, _kwargs):
        commit_started.set()
        if not release_commit_error.wait(timeout=5):
            raise RuntimeError("timed out waiting to fail running scheduler COMMIT")
        raise commit_error

    submitted = install_worker_statement_hooks(
        monkeypatch, {"COMMIT;": delayed_commit_error}
    )
    forbidden_business_calls = []

    async def forbidden_business(*_args, **_kwargs):
        forbidden_business_calls.append(True)

    monkeypatch.setattr(app, "release_plan_claim", forbidden_business)
    monkeypatch.setattr(app, "update_order_progress_message", forbidden_business)
    monkeypatch.setattr(app.bot, "send_message", forbidden_business)

    original_sleep = asyncio.sleep

    async def stop_after_iteration(seconds):
        if seconds == 60:
            raise asyncio.CancelledError
        await original_sleep(seconds)

    monkeypatch.setattr(app.asyncio, "sleep", stop_after_iteration)

    async def scenario():
        scheduler_task = asyncio.create_task(app.dca_scheduler())
        assert await asyncio.to_thread(commit_started.wait, 5)
        scheduler_task.cancel()
        await original_sleep(0)
        release_commit_error.set()
        with pytest.raises(asyncio.CancelledError) as raised:
            await scheduler_task
        return raised.value

    cancellation = asyncio.run(scenario())

    diagnostic_cancellation = cancellation_with_commit_diagnostic(cancellation)
    assert diagnostic_cancellation.scheduler_transaction_commit_error is commit_error
    assert diagnostic_cancellation.__cause__ is commit_error
    assert submitted.count("ROLLBACK;") == 1
    assert forbidden_business_calls == []
    with sqlite3.connect(db_path) as db:
        assert db.execute(
            "SELECT active_order_id FROM dca_plans WHERE id = 7"
        ).fetchone()[0] == "scheduler-order"
    assert_write_lock_released(db_path)


def test_scheduler_true_queued_commit_error_after_cancellation_is_acknowledged(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "scheduler-queued-cancelled-commit-error.sqlite3"
    initialize_db(db_path, monkeypatch)
    blocker_started = threading.Event()
    release_blocker = threading.Event()
    commit_started = threading.Event()
    blocker_sql = "SELECT 1 /* COMBINED COMMIT ERROR QUEUE BLOCKER */"
    commit_error = sqlite3.OperationalError(
        "cancelled queued scheduler COMMIT failure"
    )

    def block_worker(function, args, kwargs):
        blocker_started.set()
        if not release_blocker.wait(timeout=5):
            raise RuntimeError("timed out waiting to release combined queue blocker")
        return function(*args, **kwargs)

    def fail_commit_after_start(_function, _args, _kwargs):
        commit_started.set()
        raise commit_error

    submitted = install_worker_statement_hooks(
        monkeypatch,
        {blocker_sql: block_worker, "COMMIT;": fail_commit_after_start},
    )
    business_calls = []

    async def scenario():
        async with app.open_db() as db:
            await db.execute(
                "INSERT INTO wallets (user_id, wallet_address) VALUES (60, '0x60')"
            )
            blocker_task = asyncio.create_task(db.execute(blocker_sql))
            assert await asyncio.to_thread(blocker_started.wait, 5)

            async def transaction_task():
                await app._commit_scheduler_transaction(db)
                business_calls.append(True)

            transaction = asyncio.create_task(transaction_task())
            await wait_until_submitted(submitted, "COMMIT;")
            assert commit_started.is_set() is False
            transaction.cancel()
            await asyncio.sleep(0)
            assert commit_started.is_set() is False
            release_blocker.set()

            blocker_cursor = await blocker_task
            await blocker_cursor.close()
            with pytest.raises(asyncio.CancelledError) as raised:
                await transaction
            assert commit_started.is_set() is True
            return raised.value

    cancellation = asyncio.run(scenario())

    diagnostic_cancellation = cancellation_with_commit_diagnostic(cancellation)
    assert diagnostic_cancellation.scheduler_transaction_commit_error is commit_error
    assert diagnostic_cancellation.__cause__ is commit_error
    assert submitted.index(blocker_sql) < submitted.index("COMMIT;")
    assert submitted.count("ROLLBACK;") == 1
    assert business_calls == []
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM wallets").fetchone()[0] == 0
    assert_write_lock_released(db_path)


def test_scheduler_cancelled_commit_and_rollback_errors_keep_all_diagnostics(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "scheduler-cancelled-commit-rollback-errors.sqlite3"
    initialize_db(db_path, monkeypatch)
    commit_started = threading.Event()
    release_commit_error = threading.Event()
    commit_error = sqlite3.OperationalError(
        "cancelled scheduler primary COMMIT failure"
    )
    rollback_error = sqlite3.OperationalError(
        "cancelled scheduler secondary ROLLBACK failure"
    )

    def delayed_commit_error(_function, _args, _kwargs):
        commit_started.set()
        if not release_commit_error.wait(timeout=5):
            raise RuntimeError("timed out waiting to fail combined COMMIT")
        raise commit_error

    def fail_rollback(_function, _args, _kwargs):
        raise rollback_error

    submitted = install_worker_statement_hooks(
        monkeypatch,
        {"COMMIT;": delayed_commit_error, "ROLLBACK;": fail_rollback},
    )
    business_calls = []

    async def transaction_task():
        async with app.open_db() as db:
            await db.execute(
                "INSERT INTO wallets (user_id, wallet_address) VALUES (70, '0x70')"
            )
            await app._commit_scheduler_transaction(db)
            business_calls.append(True)

    async def scenario():
        transaction = asyncio.create_task(transaction_task())
        assert await asyncio.to_thread(commit_started.wait, 5)
        transaction.cancel()
        await asyncio.sleep(0)
        release_commit_error.set()
        with pytest.raises(asyncio.CancelledError) as raised:
            await transaction
        return raised.value

    cancellation = asyncio.run(scenario())

    diagnostic_cancellation = cancellation_with_commit_diagnostic(cancellation)
    assert diagnostic_cancellation.scheduler_transaction_commit_error is commit_error
    assert diagnostic_cancellation.scheduler_transaction_rollback_error is rollback_error
    assert diagnostic_cancellation.__cause__ is commit_error
    assert commit_error.scheduler_transaction_rollback_error is rollback_error
    assert commit_error.__cause__ is rollback_error
    assert submitted.count("ROLLBACK;") == 1
    assert business_calls == []
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM wallets").fetchone()[0] == 0
    assert_write_lock_released(db_path)
