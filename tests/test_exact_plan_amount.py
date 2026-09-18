import asyncio
import sqlite3
from decimal import Decimal
from types import SimpleNamespace

import pytest

import bot as app


NETWORK = "USDT-ARB"
BTC_ADDRESS = "bc1qexactamountdestination000000000000000000"
USER_ID = 10001


class PlanMessage:
    def __init__(
        self, amount_text, *, network=NETWORK, interval=24, btc_address=BTC_ADDRESS
    ):
        self.text = (
            f"/setdca {network} {amount_text} {interval} {btc_address}"
        )
        self.from_user = SimpleNamespace(id=USER_ID)
        self.answers = []

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))
        return SimpleNamespace(message_id=len(self.answers))


def _init(db_path, monkeypatch):
    monkeypatch.setattr(app, "DB_PATH", str(db_path))
    asyncio.run(app.init_db())


def _configure_setdca(monkeypatch):
    monkeypatch.setattr(app, "validate_btc_address", lambda _address: True)

    async def exact_limits(_network):
        return {"min": Decimal("10"), "max": Decimal("500"), "rate": None}

    monkeypatch.setattr(app, "get_fixedfloat_limits", exact_limits)


def _seed_legacy_plan(
    db_path,
    *,
    network=NETWORK,
    interval=24,
    active_order_id=None,
    transaction_state=None,
):
    with sqlite3.connect(db_path) as db:
        plan_id = db.execute(
            "INSERT INTO dca_plans("
            "user_id,from_asset,amount,interval_hours,btc_address,next_run,"
            "active_order_id,active_order_token,execution_state) "
            "VALUES(?,?,?,?,?,?,?,?,'scheduled')",
            (
                USER_ID,
                network,
                10.0,
                interval,
                BTC_ADDRESS,
                1,
                active_order_id,
                "legacy-token" if active_order_id else None,
            ),
        ).lastrowid
        if transaction_state is not None:
            db.execute(
                "INSERT INTO sent_transactions("
                "user_id,plan_id,order_id,order_token,network_key,amount,"
                "deposit_address,state) VALUES(?,?,?,?,?,?,?,?)",
                (
                    USER_ID,
                    plan_id,
                    f"legacy-{transaction_state}",
                    "legacy-token",
                    network,
                    10.0,
                    "legacy-deposit",
                    transaction_state,
                ),
            )
        db.commit()
        return int(plan_id)


def _business_snapshot(db_path):
    with sqlite3.connect(db_path) as db:
        return (
            db.execute(
                "SELECT *, typeof(amount), typeof(amount_text) "
                "FROM dca_plans ORDER BY id"
            ).fetchall(),
            db.execute(
                "SELECT *, typeof(amount) FROM sent_transactions ORDER BY id"
            ).fetchall(),
        )


@pytest.mark.parametrize(
    ("source", "canonical"),
    [
        ("10", "10"),
        ("10.0", "10"),
        ("10.00", "10"),
        ("0.1", "0.1"),
        ("0.3", "0.3"),
        ("10.000000000000001", "10.000000000000001"),
    ],
)
def test_plan_amount_canonicalization_matrix(source, canonical):
    assert app.canonicalize_plan_amount(source) == canonical


@pytest.mark.parametrize(
    "source",
    [
        "1e1",
        "NaN",
        "Infinity",
        "-Infinity",
        "1E1",
        "1e+1",
        "10,00",
        "+10",
        "-10",
        "-0",
        "0",
        "0.000000000000000000",
        "00.10",
        "",
        "   ",
        " 10 ",
        "١٠",
        ".10",
        "10.",
        "10.0000000000000000001",
    ],
)
def test_plan_amount_rejects_noncanonical_or_unsupported_input(source):
    with pytest.raises((TypeError, ValueError)):
        app.canonicalize_plan_amount(source)


@pytest.mark.parametrize("source", ["9.999999999999999999", "500.000000000000000001"])
def test_setdca_rejects_values_outside_exact_boundaries(tmp_path, monkeypatch, source):
    db_path = tmp_path / "boundary.sqlite3"
    _init(db_path, monkeypatch)
    _configure_setdca(monkeypatch)
    message = PlanMessage(source)

    asyncio.run(app.cmd_setdca(message))

    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM dca_plans").fetchone()[0] == 0


@pytest.mark.parametrize(
    "source",
    [
        "1e1", "1E1", "1e+1", "NaN", "Infinity", "-Infinity", "10,00",
        "", "   ", "0", "-10", "-0", "00.10", "١٠", "501",
        "10.0000000000000000001",
    ],
)
def test_invalid_plan_amount_never_reaches_limits_or_persistence(
    tmp_path, monkeypatch, source
):
    db_path = tmp_path / "invalid-input.sqlite3"
    _init(db_path, monkeypatch)
    monkeypatch.setattr(app, "validate_btc_address", lambda _address: True)

    async def unexpected_limits(_network):
        pytest.fail("invalid amount reached the external limits lookup")

    monkeypatch.setattr(app, "get_fixedfloat_limits", unexpected_limits)
    asyncio.run(app.cmd_setdca(PlanMessage(source)))

    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM dca_plans").fetchone()[0] == 0


def test_internal_exact_scale_accepts_18_places_and_rejects_19():
    assert app.canonicalize_plan_amount("10.000000000000000001") == (
        "10.000000000000000001"
    )
    assert app.canonicalize_plan_amount("0.000000000000000001") == (
        "0.000000000000000001"
    )
    with pytest.raises(ValueError):
        app.canonicalize_plan_amount("10.0000000000000000001")


def test_setdca_accepts_exact_minimum_and_maximum_boundaries(tmp_path, monkeypatch):
    db_path = tmp_path / "exact-boundaries.sqlite3"
    _init(db_path, monkeypatch)
    _configure_setdca(monkeypatch)

    asyncio.run(app.cmd_setdca(PlanMessage("10")))
    asyncio.run(app.cmd_setdca(PlanMessage("500")))

    with sqlite3.connect(db_path) as db:
        assert db.execute(
            "SELECT amount_text FROM dca_plans ORDER BY id"
        ).fetchall() == [("10",), ("500",)]


def test_new_plan_stores_canonical_text_and_duplicate_numeric_forms_match(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "canonical-plan.sqlite3"
    _init(db_path, monkeypatch)
    _configure_setdca(monkeypatch)

    for source in ("10", "10.0", "10.00"):
        asyncio.run(app.cmd_setdca(PlanMessage(source)))

    with sqlite3.connect(db_path) as db:
        rows = db.execute(
            "SELECT amount, typeof(amount), amount_text, typeof(amount_text) "
            "FROM dca_plans ORDER BY id"
        ).fetchall()
    assert rows == [(10.0, "real", "10", "text")]


def test_legacy_active_order_sibling_blocks_new_exact_plan(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy-active-order.sqlite3"
    _init(db_path, monkeypatch)
    _configure_setdca(monkeypatch)
    _seed_legacy_plan(db_path, active_order_id="legacy-active")
    before = _business_snapshot(db_path)

    message = PlanMessage("10")
    asyncio.run(app.cmd_setdca(message))

    assert _business_snapshot(db_path) == before
    assert any("незавершённый ордер или перевод" in text for text, _ in message.answers)


@pytest.mark.parametrize(
    "live_state",
    ["sending", "transfering", "approve_confirmed", "tx_pending", "pending", "blocked"],
)
def test_legacy_live_transaction_sibling_blocks_new_exact_plan(
    tmp_path, monkeypatch, live_state
):
    db_path = tmp_path / f"legacy-live-{live_state}.sqlite3"
    _init(db_path, monkeypatch)
    _configure_setdca(monkeypatch)
    _seed_legacy_plan(db_path, transaction_state=live_state)
    before = _business_snapshot(db_path)

    asyncio.run(app.cmd_setdca(PlanMessage("10")))

    assert _business_snapshot(db_path) == before


@pytest.mark.parametrize("terminal_state", ["sent", "confirmed", "failed", "expired"])
def test_legacy_terminal_history_does_not_block_new_exact_plan(
    tmp_path, monkeypatch, terminal_state
):
    db_path = tmp_path / f"legacy-terminal-{terminal_state}.sqlite3"
    _init(db_path, monkeypatch)
    _configure_setdca(monkeypatch)
    legacy_id = _seed_legacy_plan(db_path, transaction_state=terminal_state)

    asyncio.run(app.cmd_setdca(PlanMessage("10")))

    with sqlite3.connect(db_path) as db:
        rows = db.execute(
            "SELECT id,amount_text FROM dca_plans ORDER BY id"
        ).fetchall()
    assert rows == [(legacy_id, None), (legacy_id + 1, "10")]


def test_legacy_real_without_unresolved_evidence_is_not_inferred_equal(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "legacy-no-evidence.sqlite3"
    _init(db_path, monkeypatch)
    _configure_setdca(monkeypatch)
    legacy_id = _seed_legacy_plan(db_path)

    asyncio.run(app.cmd_setdca(PlanMessage("10.00")))

    with sqlite3.connect(db_path) as db:
        rows = db.execute(
            "SELECT id,amount,typeof(amount),amount_text,typeof(amount_text) "
            "FROM dca_plans ORDER BY id"
        ).fetchall()
    assert rows == [
        (legacy_id, 10.0, "real", None, "null"),
        (legacy_id + 1, 10.0, "real", "10", "text"),
    ]


def test_legacy_unresolved_different_plan_slot_does_not_overblock(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "legacy-different-slot.sqlite3"
    _init(db_path, monkeypatch)
    _configure_setdca(monkeypatch)
    legacy_id = _seed_legacy_plan(
        db_path, interval=12, active_order_id="other-slot-order"
    )

    asyncio.run(app.cmd_setdca(PlanMessage("10", interval=24)))

    with sqlite3.connect(db_path) as db:
        rows = db.execute(
            "SELECT id,interval_hours,amount_text,active_order_id "
            "FROM dca_plans ORDER BY id"
        ).fetchall()
    assert rows == [
        (legacy_id, 12, None, "other-slot-order"),
        (legacy_id + 1, 24, "10", None),
    ]


def test_v2_to_v3_preserves_real_and_leaves_exact_amount_null(tmp_path, monkeypatch):
    db_path = tmp_path / "v2-to-v3.sqlite3"
    _init(db_path, monkeypatch)
    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT INTO dca_plans(user_id,from_asset,amount,interval_hours,btc_address) "
            "VALUES(?,?,?,?,?)",
            (USER_ID, NETWORK, 10.000000000000002, 24, BTC_ADDRESS),
        )
        before = db.execute(
            "SELECT id,amount,typeof(amount) FROM dca_plans ORDER BY id"
        ).fetchall()
        db.execute("ALTER TABLE dca_plans DROP COLUMN amount_text")
        db.execute("PRAGMA user_version = 2")
        db.commit()

    _init(db_path, monkeypatch)
    with sqlite3.connect(db_path) as db:
        after = db.execute(
            "SELECT id,amount,typeof(amount),amount_text,typeof(amount_text) "
            "FROM dca_plans ORDER BY id"
        ).fetchall()
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert [row[:3] for row in after] == before
    assert all(row[3:] == (None, "null") for row in after)

    snapshot = after
    _init(db_path, monkeypatch)
    with sqlite3.connect(db_path) as db:
        assert db.execute(
            "SELECT id,amount,typeof(amount),amount_text,typeof(amount_text) "
            "FROM dca_plans ORDER BY id"
        ).fetchall() == snapshot


def test_v2_to_v3_failure_after_alter_rolls_back_exactly(tmp_path, monkeypatch):
    db_path = tmp_path / "v2-to-v3-rollback.sqlite3"
    _init(db_path, monkeypatch)
    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT INTO dca_plans(user_id,from_asset,amount,interval_hours,btc_address) "
            "VALUES(?,?,?,?,?)",
            (USER_ID, NETWORK, 10.000000000000002, 24, BTC_ADDRESS),
        )
        db.execute("ALTER TABLE dca_plans DROP COLUMN amount_text")
        db.execute("PRAGMA user_version = 2")
        db.commit()
        before = (
            db.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='dca_plans'"
            ).fetchone()[0],
            db.execute(
                "SELECT id,amount,typeof(amount) FROM dca_plans ORDER BY id"
            ).fetchall(),
        )

    real_assert_v3 = app._assert_v3_schema

    async def fail_after_alter(_db):
        raise RuntimeError("injected after additive DDL")

    monkeypatch.setattr(app, "_assert_v3_schema", fail_after_alter)
    with pytest.raises(RuntimeError, match="injected after additive DDL"):
        _init(db_path, monkeypatch)

    with sqlite3.connect(db_path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert "amount_text" not in {
            row[1] for row in db.execute("PRAGMA table_info(dca_plans)")
        }
        assert (
            db.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='dca_plans'"
            ).fetchone()[0],
            db.execute(
                "SELECT id,amount,typeof(amount) FROM dca_plans ORDER BY id"
            ).fetchall(),
        ) == before

    monkeypatch.setattr(app, "_assert_v3_schema", real_assert_v3)
    _init(db_path, monkeypatch)
    with sqlite3.connect(db_path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3


def test_stamped_v3_without_amount_text_fails_closed(tmp_path, monkeypatch):
    db_path = tmp_path / "v3-missing-exact-column.sqlite3"
    _init(db_path, monkeypatch)
    with sqlite3.connect(db_path) as db:
        db.execute("ALTER TABLE dca_plans DROP COLUMN amount_text")
        db.commit()
        before = (
            db.execute("PRAGMA user_version").fetchone()[0],
            db.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
            ).fetchall(),
        )

    with pytest.raises(RuntimeError, match="version 3 requires exact plan amount"):
        _init(db_path, monkeypatch)

    with sqlite3.connect(db_path) as db:
        after = (
            db.execute("PRAGMA user_version").fetchone()[0],
            db.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
            ).fetchall(),
        )
    assert after == before


def test_v2_to_v3_cancellation_after_alter_rolls_back(tmp_path, monkeypatch):
    db_path = tmp_path / "v2-to-v3-cancel.sqlite3"
    _init(db_path, monkeypatch)
    with sqlite3.connect(db_path) as db:
        db.execute("ALTER TABLE dca_plans DROP COLUMN amount_text")
        db.execute("PRAGMA user_version = 2")
        db.commit()

    real_assert_v3 = app._assert_v3_schema

    async def cancel_after_target_assertion(db):
        await real_assert_v3(db)
        task = asyncio.current_task()
        assert task is not None
        task.cancel()
        await asyncio.sleep(0)

    monkeypatch.setattr(app, "_assert_v3_schema", cancel_after_target_assertion)
    with pytest.raises(asyncio.CancelledError):
        _init(db_path, monkeypatch)

    with sqlite3.connect(db_path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert "amount_text" not in {
            row[1] for row in db.execute("PRAGMA table_info(dca_plans)")
        }
        db.execute("BEGIN IMMEDIATE")
        db.rollback()

    monkeypatch.setattr(app, "_assert_v3_schema", real_assert_v3)
    _init(db_path, monkeypatch)


def test_legacy_plan_cannot_reach_fixedfloat_create(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy-plan.sqlite3"
    _init(db_path, monkeypatch)
    with sqlite3.connect(db_path) as db:
        plan_id = db.execute(
            "INSERT INTO dca_plans(user_id,from_asset,amount,interval_hours,btc_address) "
            "VALUES(?,?,?,?,?)",
            (USER_ID, NETWORK, 25.0, 24, BTC_ADDRESS),
        ).lastrowid
        db.commit()

    monkeypatch.setattr(
        app,
        "create_fixedfloat_order",
        lambda *_args: pytest.fail("legacy REAL reached FixedFloat create"),
    )
    result = asyncio.run(
        app.execute_new_order(
            app.NewOrderExecutionRequest(plan_id=plan_id, user_id=USER_ID, trigger="manual")
        )
    )
    assert result.outcome == "legacy_amount_unverified"
    assert result.external_create_attempted is False


def test_fixedfloat_http_body_contains_exact_unquoted_decimal(monkeypatch):
    captured = {}

    class Response:
        status_code = 200
        text = "ok"

        def raise_for_status(self):
            return None

        def json(self, **_kwargs):
            return {"code": 0, "data": {"id": "exact-order"}}

    def fake_post(url, data, headers, timeout):
        captured.update(url=url, data=data, headers=headers, timeout=timeout)
        return Response()

    monkeypatch.setattr(app, "FF_API_KEY", "key")
    monkeypatch.setattr(app, "FF_API_SECRET", "secret")
    monkeypatch.setattr(app, "MOCK_FIXEDFLOAT", False)
    monkeypatch.setattr(app.requests, "post", fake_post)

    app.create_fixedfloat_order(NETWORK, "10.000000000000001", BTC_ADDRESS)

    body = captured["data"].decode("utf-8")
    assert '"amount":10.000000000000001' in body
    assert '"amount":"10.000000000000001"' not in body
    assert "10.000000000000002" not in body


def test_fixedfloat_limits_remain_exact_decimals(monkeypatch):
    async def response(_method, _params):
        return {
            "from": {"min": "10.000000000000001", "max": "500"},
            "to": {"amount": "0.001"},
        }

    monkeypatch.setattr(app, "ff_request_async", response)
    app._fixedfloat_limits_cache.clear()
    limits = asyncio.run(app.get_fixedfloat_limits(NETWORK))
    assert limits["min"] == Decimal("10.000000000000001")
    assert limits["max"] == Decimal("500")
    assert limits["rate"] == Decimal("50000")


@pytest.mark.parametrize(
    ("time_left", "expected"),
    [
        (123, 1123),
        (Decimal("123.0"), 1123),
        (Decimal("123.5"), 1123),
        ("123.5", 1000),
        ("invalid", 1000),
        (Decimal("NaN"), 1000),
        (Decimal("Infinity"), 1000),
        (Decimal("-1"), 1000),
    ],
)
def test_order_time_left_decimal_compatibility(time_left, expected):
    assert app.extract_order_expires_at(
        {"time": {"left": time_left}}, fallback_now=1000
    ) == expected


def test_raw_fixedfloat_json_fractional_time_left_is_decimal_and_supported(
    monkeypatch,
):
    captured = {}
    response = app.requests.Response()
    response.status_code = 200
    response._content = (
        b'{"code":0,"data":{"status":"NEW","time":{"left":123.5}}}'
    )

    def fake_post(url, data, headers, timeout):
        captured.update(url=url, data=data, headers=headers, timeout=timeout)
        return response

    monkeypatch.setattr(app, "FF_API_KEY", "key")
    monkeypatch.setattr(app, "FF_API_SECRET", "secret")
    monkeypatch.setattr(app, "MOCK_FIXEDFLOAT", False)
    monkeypatch.setattr(app.requests, "post", fake_post)

    data = app.ff_request("order", {"id": "order", "token": "token"})

    assert data["time"]["left"] == Decimal("123.5")
    assert app.extract_order_expires_at(data, fallback_now=1000) == 1123
    assert captured["data"] == b'{"id":"order","token":"token"}'
