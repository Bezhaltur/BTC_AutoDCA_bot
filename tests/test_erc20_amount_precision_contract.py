"""Contract-first tests for exact FixedFloat -> ERC20 payment amounts.

These tests pin the exact amount contract and its restart/recovery behavior.

Persisted ``amount_units`` is decimal TEXT rather than SQLite INTEGER because
valid 18-decimal amounts (for example 500 USDT) exceed SQLite's signed int64.
"""

import asyncio
import sqlite3
from types import SimpleNamespace

import pytest
from web3 import Web3

import auto_send as sender
import bot as app
import erc20


def _amount_to_units(amount_text: str, token_decimals: int) -> int:
    converter = getattr(erc20, "decimal_amount_to_units", None)
    assert callable(converter), (
        "erc20.decimal_amount_to_units(amount_text: str, token_decimals: int) "
        "is the required exact conversion boundary"
    )
    return converter(amount_text, token_decimals)


def _has_sufficient_balance(balance_units: int, amount_units: int) -> bool:
    comparator = getattr(erc20, "has_sufficient_token_balance", None)
    assert callable(comparator), (
        "erc20.has_sufficient_token_balance(balance_units, amount_units) must "
        "compare integer units without converting either operand to float"
    )
    return comparator(balance_units, amount_units)


def _configure_exact_transfer_runtime(
    monkeypatch,
    *,
    db_path: str,
    order_id: str,
    balance_units: int = 16_000_002,
    token_decimals: int = 6,
):
    """Use production auto-send/transfer/persistence with an in-memory EVM boundary."""
    events = []
    raw_tx = b"signed-exact-transfer"

    class SignedTransaction:
        raw_transaction = raw_tx

    class FakeAccount:
        address = "0x2222222222222222222222222222222222222222"

        def sign_transaction(self, tx):
            events.append(("sign", tx["nonce"]))
            return SignedTransaction()

    class FakeEth:
        def get_transaction_count(self, _address, _block):
            return 9

        def send_raw_transaction(self, candidate_raw):
            events.append(("broadcast", bytes(candidate_raw)))
            return Web3.keccak(candidate_raw)

        def wait_for_transaction_receipt(self, _tx_hash, timeout):
            assert timeout == 120
            return SimpleNamespace(status=1, blockNumber=123)

    class FakeWeb3:
        eth = FakeEth()
        provider = SimpleNamespace(endpoint_uri="https://unused.invalid")

        @staticmethod
        def from_wei(value, _unit):
            return value / 10**18

        @staticmethod
        def is_connected():
            return True

    class TransferBuilder:
        def __init__(self, units):
            self.units = units

        def build_transaction(self, tx):
            with sqlite3.connect(db_path) as db:
                persisted = db.execute(
                    "SELECT amount_units, token_decimals FROM sent_transactions "
                    "WHERE order_id = ?",
                    (order_id,),
                ).fetchone()
            assert persisted == (str(self.units), token_decimals)
            events.append(("prepare", self.units))
            return dict(tx)

    class ContractFunctions:
        def transfer(self, _to_address, units):
            return TransferBuilder(units)

    contract = SimpleNamespace(functions=ContractFunctions())
    w3 = FakeWeb3()

    monkeypatch.setattr(erc20.Account, "from_key", lambda _key: FakeAccount())
    monkeypatch.setattr(sender, "load_keystore", lambda _user_id: {"crypto": {}})
    monkeypatch.setattr(
        sender, "decrypt_private_key", lambda _keystore, _password: "1" * 64
    )
    monkeypatch.setattr(sender, "get_web3_instance", lambda _network: w3)
    monkeypatch.setattr(
        sender, "get_network_config", lambda _network: {"native_token": "ETH"}
    )
    monkeypatch.setattr(sender, "get_usdt_token_decimals", lambda *_args: token_decimals)
    monkeypatch.setattr(sender, "get_usdt_balance_units", lambda *_args: balance_units)
    monkeypatch.setattr(sender, "get_native_balance", lambda *_args: 1.0)
    monkeypatch.setattr(sender, "estimate_gas_for_transfer", lambda *_args: 75_000)
    monkeypatch.setattr(sender, "build_gas_params", lambda *_args: {"gasPrice": 1})
    monkeypatch.setattr(erc20, "get_usdt_contract", lambda *_args: contract)
    monkeypatch.setattr(erc20, "estimate_gas_for_transfer", lambda *_args: 75_000)
    monkeypatch.setattr(erc20, "build_gas_params", lambda *_args: {"gasPrice": 1})
    monkeypatch.setattr(erc20, "get_network_config", lambda _network: {"chain_id": 42161})
    sender._SEND_LOCKS.clear()
    return events


@pytest.mark.parametrize(
    ("amount_text", "token_decimals", "expected_units"),
    [
        ("16.000002", 6, 16_000_002),
        ("16.000002", 18, 16_000_002_000_000_000_000),
    ],
)
def test_decimal_string_converts_to_exact_token_units(
    amount_text, token_decimals, expected_units
):
    assert _amount_to_units(amount_text, token_decimals) == expected_units


@pytest.mark.parametrize(
    ("amount_text", "token_decimals"),
    [
        ("16.0000001", 6),
        ("0.0000000000000000001", 18),
    ],
)
def test_excess_token_precision_is_rejected(amount_text, token_decimals):
    with pytest.raises(ValueError, match="precision|representable|decimals"):
        _amount_to_units(amount_text, token_decimals)


@pytest.mark.parametrize(
    "amount_text",
    ["NaN", "Infinity", "-Infinity", "-1", "0", "0.000000"],
)
def test_non_finite_or_non_positive_amount_is_rejected(amount_text):
    with pytest.raises(ValueError, match="finite|positive|amount"):
        _amount_to_units(amount_text, 6)


def test_binary_float_is_not_an_accepted_amount_boundary():
    with pytest.raises((TypeError, ValueError), match="string|float|amount"):
        _amount_to_units(16.000002, 6)


def test_balance_comparison_uses_exact_integer_units():
    required = 16_000_002
    assert _has_sufficient_balance(required, required) is True
    assert _has_sufficient_balance(required - 1, required) is False


def test_transfer_build_uses_pre_resolved_integer_units_without_reconversion(
    monkeypatch
):
    transfer_calls = []

    class TransferBuilder:
        def build_transaction(self, tx):
            return dict(tx)

    class ContractFunctions:
        def transfer(self, to_address, amount_units):
            transfer_calls.append((to_address, amount_units))
            return TransferBuilder()

    contract = SimpleNamespace(functions=ContractFunctions())
    w3 = SimpleNamespace(
        eth=SimpleNamespace(get_transaction_count=lambda *_args: 1),
        from_wei=lambda value, _unit: value / 10**18,
    )

    monkeypatch.setattr(
        erc20.Account,
        "from_key",
        lambda _key: SimpleNamespace(
            address="0x2222222222222222222222222222222222222222"
        ),
    )
    monkeypatch.setattr(erc20, "get_usdt_contract", lambda *_args: contract)
    monkeypatch.setattr(
        erc20, "estimate_gas_for_transfer", lambda *_args, **_kwargs: 75_000
    )
    monkeypatch.setattr(erc20, "build_gas_params", lambda *_args: {"gasPrice": 1})

    result = erc20.transfer_usdt(
        w3,
        "USDT-ARB",
        "0x" + "1" * 64,
        "0x1111111111111111111111111111111111111111",
        amount_units=16_000_002,
        dry_run=True,
    )

    assert result is None
    assert transfer_calls == [
        (Web3.to_checksum_address("0x1111111111111111111111111111111111111111"), 16_000_002)
    ]


@pytest.mark.parametrize(
    "invalid_amount", ["not-a-number", "NaN", "Infinity", 16.0, True]
)
def test_invalid_fixedfloat_amount_fails_before_rpc_or_broadcast(
    monkeypatch, invalid_amount
):
    rpc_calls = []
    transfer_calls = []

    monkeypatch.setattr(
        sender,
        "load_keystore",
        lambda _user_id: {"crypto": {}},
    )
    monkeypatch.setattr(
        sender,
        "decrypt_private_key",
        lambda _keystore, _password: "1" * 64,
    )
    monkeypatch.setattr(
        sender,
        "get_web3_instance",
        lambda *_args: rpc_calls.append(_args),
    )
    monkeypatch.setattr(
        sender,
        "transfer_usdt",
        lambda *_args, **_kwargs: transfer_calls.append((_args, _kwargs)),
    )

    result = asyncio.run(
        sender.auto_send_usdt(
            network_key="USDT-ARB",
            user_id=10001,
            wallet_password="test-password",
            deposit_address="0x1111111111111111111111111111111111111111",
            required_amount=invalid_amount,
            btc_address="bc1unused",
            order_id="invalid-fixedfloat-amount",
            persist_prepared_tx=lambda *_args: None,
        )
    )

    assert result[:3] == (False, None, None)
    assert result[3].startswith("INVALID_PAYMENT_AMOUNT:")
    assert rpc_calls == []
    assert transfer_calls == []


def test_excess_precision_stops_before_balance_persistence_gas_or_transfer(monkeypatch):
    events = []
    monkeypatch.setattr(sender, "load_keystore", lambda _user_id: {"crypto": {}})
    monkeypatch.setattr(
        sender, "decrypt_private_key", lambda _keystore, _password: "1" * 64
    )
    monkeypatch.setattr(sender, "get_web3_instance", lambda _network: SimpleNamespace())
    monkeypatch.setattr(sender, "get_network_config", lambda _network: {"native_token": "ETH"})
    monkeypatch.setattr(
        sender,
        "get_usdt_token_decimals",
        lambda *_args: events.append("decimals") or 6,
    )
    monkeypatch.setattr(
        sender,
        "get_usdt_balance_units",
        lambda *_args: (_ for _ in ()).throw(AssertionError("balanceOf must not run")),
    )
    monkeypatch.setattr(
        sender,
        "estimate_gas_for_transfer",
        lambda *_args: (_ for _ in ()).throw(AssertionError("gas must not run")),
    )
    monkeypatch.setattr(
        sender,
        "transfer_usdt",
        lambda *_args: (_ for _ in ()).throw(AssertionError("transfer must not run")),
    )

    result = asyncio.run(
        sender.auto_send_usdt(
            network_key="USDT-ARB",
            user_id=10001,
            wallet_password="test-password",
            deposit_address="0x1111111111111111111111111111111111111111",
            required_amount="16.0000001",
            btc_address="bc1unused",
            order_id="excess-precision",
            persist_prepared_tx=lambda *_args: None,
            persist_payment_intent=lambda *_args: (_ for _ in ()).throw(
                AssertionError("intent must not persist")
            ),
        )
    )

    assert result[:3] == (False, None, None)
    assert result[3].startswith("INVALID_PAYMENT_AMOUNT:")
    assert events == ["decimals"]


def test_persisted_decimals_mismatch_stops_before_balance_or_transfer(monkeypatch):
    monkeypatch.setattr(sender, "load_keystore", lambda _user_id: {"crypto": {}})
    monkeypatch.setattr(
        sender, "decrypt_private_key", lambda _keystore, _password: "1" * 64
    )
    monkeypatch.setattr(sender, "get_web3_instance", lambda _network: SimpleNamespace())
    monkeypatch.setattr(sender, "get_network_config", lambda _network: {"native_token": "ETH"})
    monkeypatch.setattr(sender, "get_usdt_token_decimals", lambda *_args: 18)
    monkeypatch.setattr(
        sender,
        "get_usdt_balance_units",
        lambda *_args: (_ for _ in ()).throw(AssertionError("balanceOf must not run")),
    )
    monkeypatch.setattr(
        sender,
        "transfer_usdt",
        lambda *_args: (_ for _ in ()).throw(AssertionError("transfer must not run")),
    )

    result = asyncio.run(
        sender.auto_send_usdt(
            network_key="USDT-ARB",
            user_id=10001,
            wallet_password="test-password",
            deposit_address="0x1111111111111111111111111111111111111111",
            required_amount=16.000002,
            btc_address="bc1unused",
            order_id="decimals-mismatch",
            amount_units=16_000_002,
            token_decimals=6,
            persist_prepared_tx=lambda *_args: None,
            persist_payment_intent=lambda *_args: (_ for _ in ()).throw(
                AssertionError("intent must not persist")
            ),
        )
    )

    assert result[:3] == (False, None, None)
    assert result[3].startswith("INVALID_PAYMENT_AMOUNT:")


def test_one_unit_less_balance_stops_production_auto_send(monkeypatch):
    transfer_calls = []

    class FakeWeb3:
        eth = SimpleNamespace()

        @staticmethod
        def from_wei(value, _unit):
            return value / 10**18

    monkeypatch.setattr(sender, "load_keystore", lambda _user_id: {"crypto": {}})
    monkeypatch.setattr(
        sender, "decrypt_private_key", lambda _keystore, _password: "1" * 64
    )
    monkeypatch.setattr(sender, "get_web3_instance", lambda _network: FakeWeb3())
    monkeypatch.setattr(sender, "get_network_config", lambda _network: {"native_token": "ETH"})
    monkeypatch.setattr(sender, "get_usdt_token_decimals", lambda *_args: 6)
    monkeypatch.setattr(sender, "get_usdt_balance_units", lambda *_args: 16_000_001)
    monkeypatch.setattr(sender, "get_native_balance", lambda *_args: 1.0)
    monkeypatch.setattr(
        sender, "transfer_usdt", lambda *_args: transfer_calls.append(_args)
    )

    result = asyncio.run(
        sender.auto_send_usdt(
            network_key="USDT-ARB",
            user_id=10001,
            wallet_password="test-password",
            deposit_address="0x1111111111111111111111111111111111111111",
            required_amount="16.000002",
            btc_address="bc1unused",
            order_id="one-unit-short",
        )
    )

    assert result[:3] == (False, None, None)
    assert result[3].startswith("Insufficient USDT balance")
    assert transfer_calls == []


def test_production_persister_precedes_transfer_preparation(tmp_path, monkeypatch):
    db_path = str(tmp_path / "persist-before-prepare.sqlite3")
    monkeypatch.setattr(app, "DB_PATH", db_path)
    asyncio.run(app.init_db())
    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT INTO sent_transactions "
            "(user_id, plan_id, order_id, network_key, amount, deposit_address, state) "
            "VALUES (?, ?, ?, ?, ?, ?, 'transfering')",
            (10001, 7, "persist-before-prepare", "USDT-ARB", 16.000002, "0xdeposit"),
        )
        db.commit()

    events = _configure_exact_transfer_runtime(
        monkeypatch, db_path=db_path, order_id="persist-before-prepare"
    )
    result = asyncio.run(
        sender.auto_send_usdt(
            network_key="USDT-ARB",
            user_id=10001,
            wallet_password="test-password",
            deposit_address="0x1111111111111111111111111111111111111111",
            required_amount="16.000002",
            btc_address="bc1unused",
            order_id="persist-before-prepare",
            persist_prepared_tx=app.make_prepared_tx_persister(
                7, "persist-before-prepare"
            ),
            persist_payment_intent=app.make_payment_intent_persister(
                7, "persist-before-prepare"
            ),
        )
    )

    assert result[0] is True
    assert events == [
        ("prepare", 16_000_002),
        ("sign", 9),
        ("broadcast", b"signed-exact-transfer"),
    ]
    with sqlite3.connect(db_path) as db:
        row = db.execute(
            "SELECT amount_units, token_decimals, transfer_nonce, transfer_raw_tx "
            "FROM (SELECT amount_units, token_decimals, transfer_tx_nonce AS transfer_nonce, "
            "transfer_raw_tx FROM sent_transactions WHERE order_id = 'persist-before-prepare')"
        ).fetchone()
    assert row == ("16000002", 6, 9, Web3.to_hex(b"signed-exact-transfer"))


def test_invalid_amount_units_raise_in_gas_estimator_before_fallback(monkeypatch):
    monkeypatch.setattr(
        erc20,
        "get_usdt_contract",
        lambda *_args: (_ for _ in ()).throw(AssertionError("RPC must not run")),
    )
    with pytest.raises(ValueError, match="positive integer"):
        erc20.estimate_gas_for_transfer(
            SimpleNamespace(), "USDT-ARB", "0xfrom", "0xto", 0
        )


def test_valid_amount_units_keep_gas_estimation_fallback(monkeypatch):
    monkeypatch.setattr(
        erc20,
        "get_usdt_contract",
        lambda *_args: (_ for _ in ()).throw(ConnectionError("RPC unavailable")),
    )
    assert erc20.estimate_gas_for_transfer(
        SimpleNamespace(), "USDT-ARB", "0xfrom", "0xto", 1
    ) == erc20.GAS_FALLBACK_TRANSFER


def test_restart_uses_persisted_units_without_reading_legacy_real(monkeypatch):
    events = []

    class FakeWeb3:
        eth = SimpleNamespace()

        @staticmethod
        def from_wei(value, _unit):
            return value / 10**18

    monkeypatch.setattr(sender, "load_keystore", lambda _user_id: {"crypto": {}})
    monkeypatch.setattr(
        sender, "decrypt_private_key", lambda _keystore, _password: "1" * 64
    )
    monkeypatch.setattr(sender, "get_web3_instance", lambda _network: FakeWeb3())
    monkeypatch.setattr(
        sender, "get_network_config", lambda _network: {"native_token": "ETH"}
    )
    monkeypatch.setattr(
        sender, "get_usdt_balance_units", lambda *_args: 16_000_002
    )
    monkeypatch.setattr(sender, "get_usdt_token_decimals", lambda *_args: 6)
    monkeypatch.setattr(sender, "get_native_balance", lambda *_args: 1.0)
    monkeypatch.setattr(sender, "estimate_gas_for_transfer", lambda *_args: 75_000)
    monkeypatch.setattr(sender, "build_gas_params", lambda *_args: {"gasPrice": 1})

    def capture_transfer(*args):
        events.append(("transfer", args[4]))
        return None

    monkeypatch.setattr(sender, "transfer_usdt", capture_transfer)
    sender._SEND_LOCKS.clear()

    result = asyncio.run(
        sender.auto_send_usdt(
            network_key="USDT-ARB",
            user_id=10001,
            wallet_password="test-password",
            deposit_address="0x1111111111111111111111111111111111111111",
            # SQLite returns the unchanged legacy REAL as a float. It is not an
            # input to exact recovery once the exact intent pair exists.
            required_amount=16.000002,
            btc_address="bc1unused",
            order_id="exact-restart-order",
            dry_run=True,
            amount_units=16_000_002,
            token_decimals=6,
            persist_payment_intent=lambda units, decimals: events.append(
                ("persist", units, decimals)
            ),
        )
    )

    assert result == (True, None, None, "DRY RUN: Would transfer USDT")
    assert events == [
        ("persist", 16_000_002, 6),
        ("transfer", 16_000_002),
    ]


def test_restart_persists_same_integer_amount_as_decimal_text(tmp_path, monkeypatch):
    db_path = str(tmp_path / "exact-amount-restart.sqlite3")
    monkeypatch.setattr(app, "DB_PATH", db_path)
    asyncio.run(app.init_db())

    with sqlite3.connect(db_path) as db:
        columns = {
            row[1]: row[2].upper()
            for row in db.execute("PRAGMA table_info(sent_transactions)")
        }
        assert columns.get("amount_units") == "TEXT"
        assert columns.get("token_decimals") == "INTEGER"
        db.execute(
            "INSERT INTO sent_transactions "
            "(user_id, plan_id, order_id, network_key, amount, amount_units, "
            "token_decimals, deposit_address, state) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'transfering')",
            (
                10001,
                7,
                "exact-restart-order",
                "USDT-BSC",
                16.000002,
                "16000002000000000000",
                18,
                "0xdeposit",
            ),
        )
        db.commit()

    with sqlite3.connect(db_path) as reopened:
        row = reopened.execute(
            "SELECT amount_units, typeof(amount_units), token_decimals "
            "FROM sent_transactions WHERE order_id = 'exact-restart-order'"
        ).fetchone()

    assert row == ("16000002000000000000", "text", 18)


def test_recovery_resumes_unsigned_exact_intent_with_persisted_units(
    tmp_path, monkeypatch
):
    db_path = str(tmp_path / "unsigned-exact-recovery.sqlite3")
    monkeypatch.setattr(app, "DB_PATH", db_path)
    asyncio.run(app.init_db())

    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT INTO dca_plans "
            "(id, user_id, from_asset, amount, interval_hours, btc_address, "
            "next_run, active, deleted, execution_state, active_order_id, "
            "active_order_address, active_order_amount, active_order_expires) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 1, 0, 'scheduled', ?, ?, ?, ?)",
            (
                7,
                10001,
                "USDT-ARB",
                16.000002,
                24,
                "bc1unused",
                1_700_000_000,
                "unsigned-exact-order",
                "0x1111111111111111111111111111111111111111",
                "16.000002 USDT",
                4_000_000_000,
            ),
        )
        db.execute(
            "INSERT INTO sent_transactions "
            "(user_id, plan_id, order_id, network_key, amount, amount_units, "
            "token_decimals, deposit_address, state) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'transfering')",
            (
                10001,
                7,
                "unsigned-exact-order",
                "USDT-ARB",
                16.000002,
                "16000002",
                6,
                "0x1111111111111111111111111111111111111111",
            ),
        )
        db.commit()

    monkeypatch.setitem(app._wallet_passwords, 10001, "test-password")
    events = _configure_exact_transfer_runtime(
        monkeypatch, db_path=db_path, order_id="unsigned-exact-order"
    )

    asyncio.run(app.recovery_scan_pending_transactions())

    assert events == [
        ("prepare", 16_000_002),
        ("sign", 9),
        ("broadcast", b"signed-exact-transfer"),
    ]
    with sqlite3.connect(db_path) as db:
        row = db.execute(
            "SELECT state, amount_units, token_decimals, transfer_raw_tx "
            "FROM sent_transactions "
            "WHERE order_id = 'unsigned-exact-order'"
        ).fetchone()
        active_order_id = db.execute(
            "SELECT active_order_id FROM dca_plans WHERE id = 7"
        ).fetchone()[0]
    assert row == (
        "confirmed",
        "16000002",
        6,
        Web3.to_hex(b"signed-exact-transfer"),
    )
    assert active_order_id == "unsigned-exact-order"


def test_recovery_persisted_decimals_mismatch_never_signs_or_broadcasts(
    tmp_path, monkeypatch
):
    db_path = str(tmp_path / "recovery-decimals-mismatch.sqlite3")
    monkeypatch.setattr(app, "DB_PATH", db_path)
    asyncio.run(app.init_db())

    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT INTO dca_plans "
            "(id, user_id, from_asset, amount, interval_hours, btc_address, next_run, "
            "active, deleted, execution_state, active_order_id, active_order_address, "
            "active_order_amount, active_order_expires) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 1, 0, 'scheduled', ?, ?, ?, ?)",
            (
                7,
                10001,
                "USDT-ARB",
                16.000002,
                24,
                "bc1unused",
                1_700_000_000,
                "mismatched-decimals-order",
                "0x1111111111111111111111111111111111111111",
                "16.000002 USDT",
                4_000_000_000,
            ),
        )
        db.execute(
            "INSERT INTO sent_transactions "
            "(user_id, plan_id, order_id, network_key, amount, amount_units, token_decimals, "
            "deposit_address, state) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'transfering')",
            (
                10001,
                7,
                "mismatched-decimals-order",
                "USDT-ARB",
                16.000002,
                "16000002",
                6,
                "0x1111111111111111111111111111111111111111",
            ),
        )

    monkeypatch.setitem(app._wallet_passwords, 10001, "test-password")
    monkeypatch.setattr(sender, "load_keystore", lambda _user_id: {"crypto": {}})
    monkeypatch.setattr(
        sender, "decrypt_private_key", lambda _keystore, _password: "1" * 64
    )
    monkeypatch.setattr(sender, "get_web3_instance", lambda _network: SimpleNamespace())
    monkeypatch.setattr(
        sender, "get_network_config", lambda _network: {"native_token": "ETH"}
    )
    monkeypatch.setattr(sender, "get_usdt_token_decimals", lambda *_args: 18)
    monkeypatch.setattr(
        sender,
        "get_usdt_balance_units",
        lambda *_args: (_ for _ in ()).throw(AssertionError("balanceOf must not run")),
    )
    monkeypatch.setattr(
        sender,
        "transfer_usdt",
        lambda *_args: (_ for _ in ()).throw(AssertionError("sign/broadcast must not run")),
    )

    asyncio.run(app.recovery_scan_pending_transactions())

    with sqlite3.connect(db_path) as db:
        row = db.execute(
            "SELECT state, amount_units, token_decimals, transfer_tx_hash, transfer_raw_tx, error_message "
            "FROM sent_transactions WHERE order_id = 'mismatched-decimals-order'"
        ).fetchone()
        active_order_id = db.execute(
            "SELECT active_order_id FROM dca_plans WHERE id = 7"
        ).fetchone()[0]
    assert row[:5] == ("failed", "16000002", 6, None, None)
    assert row[5].startswith("INVALID_PAYMENT_AMOUNT:")
    assert active_order_id == "mismatched-decimals-order"


def test_unsigned_legacy_real_without_exact_intent_is_fail_closed(
    tmp_path, monkeypatch
):
    db_path = str(tmp_path / "unsigned-legacy-recovery.sqlite3")
    monkeypatch.setattr(app, "DB_PATH", db_path)
    asyncio.run(app.init_db())

    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT INTO dca_plans "
            "(id, user_id, from_asset, amount, interval_hours, btc_address, "
            "next_run, active, deleted, execution_state, active_order_id, "
            "active_order_address, active_order_amount, active_order_expires) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 1, 0, 'scheduled', ?, ?, ?, ?)",
            (
                7,
                10001,
                "USDT-ARB",
                16.000002,
                24,
                "bc1unused",
                1_700_000_000,
                "unsigned-legacy-order",
                "0x1111111111111111111111111111111111111111",
                "16.000002 USDT",
                4_000_000_000,
            ),
        )
        db.execute(
            "INSERT INTO sent_transactions "
            "(user_id, plan_id, order_id, network_key, amount, deposit_address, state) "
            "VALUES (?, ?, ?, ?, ?, ?, 'transfering')",
            (
                10001,
                7,
                "unsigned-legacy-order",
                "USDT-ARB",
                16.000002,
                "0x1111111111111111111111111111111111111111",
            ),
        )
        db.commit()

    async def forbidden_resume(**_kwargs):
        raise AssertionError("legacy REAL must not be used to prepare a transfer")

    monkeypatch.setattr(app, "resume_transfer_after_approve", forbidden_resume)
    asyncio.run(app.recovery_scan_pending_transactions())

    with sqlite3.connect(db_path) as db:
        row = db.execute(
            "SELECT state, amount_units, token_decimals, error_message "
            "FROM sent_transactions WHERE order_id = 'unsigned-legacy-order'"
        ).fetchone()
        active_order_id = db.execute(
            "SELECT active_order_id FROM dca_plans WHERE id = 7"
        ).fetchone()[0]
    assert row[:3] == ("transfering", None, None)
    assert row[3].startswith("INVALID_PAYMENT_AMOUNT:")
    assert active_order_id == "unsigned-legacy-order"


def test_init_db_does_not_rewrite_legacy_real_amount(tmp_path, monkeypatch):
    db_path = str(tmp_path / "legacy-real-preserved.sqlite3")
    monkeypatch.setattr(app, "DB_PATH", db_path)
    asyncio.run(app.init_db())

    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT INTO sent_transactions "
            "(user_id, plan_id, order_id, network_key, amount, deposit_address, state) "
            "VALUES (?, ?, ?, ?, ?, ?, 'failed')",
            (10001, 7, "legacy-real", "USDT-ARB", 16.000002, "0xdeposit"),
        )
        before = db.execute(
            "SELECT amount, typeof(amount) FROM sent_transactions "
            "WHERE order_id = 'legacy-real'"
        ).fetchone()
        db.commit()

    asyncio.run(app.init_db())

    with sqlite3.connect(db_path) as db:
        after = db.execute(
            "SELECT amount, typeof(amount) FROM sent_transactions "
            "WHERE order_id = 'legacy-real'"
        ).fetchone()

    assert after == before


def test_existing_signed_transfer_intent_is_never_recomputed_from_real(
    tmp_path, monkeypatch
):
    db_path = str(tmp_path / "signed-intent-wins.sqlite3")
    monkeypatch.setattr(app, "DB_PATH", db_path)
    asyncio.run(app.init_db())

    raw_tx = Web3.to_hex(b"already-signed-exact-transfer")
    tx_hash = Web3.keccak(b"already-signed-exact-transfer").hex()
    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT INTO sent_transactions "
            "(user_id, plan_id, order_id, network_key, transfer_tx_hash, "
            "transfer_tx_nonce, transfer_raw_tx, amount, deposit_address, state) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'tx_pending')",
            (
                10001,
                7,
                "signed-intent-order",
                "USDT-ARB",
                None,
                23,
                raw_tx,
                16.000002,
                "0xdeposit",
            ),
        )
        db.commit()

    async def pending_status(_network_key, candidate_hash):
        assert candidate_hash == tx_hash
        return "pending"

    rebroadcasts = []

    async def capture_rebroadcast(network_key, candidate_raw, candidate_hash, action):
        rebroadcasts.append((network_key, candidate_raw, candidate_hash, action))

    def forbidden_recalculation(*_args, **_kwargs):
        raise AssertionError("signed transfer intent must not recalculate amount")

    async def forbidden_auto_send(*_args, **_kwargs):
        raise AssertionError("signed transfer intent must not build a new transaction")

    monkeypatch.setattr(app, "get_transfer_tx_status", pending_status)
    monkeypatch.setattr(
        app, "rebroadcast_persisted_erc20_transaction", capture_rebroadcast
    )
    monkeypatch.setattr(app, "auto_send_usdt", forbidden_auto_send)
    monkeypatch.setattr(
        erc20,
        "decimal_amount_to_units",
        forbidden_recalculation,
        raising=False,
    )

    asyncio.run(app.recovery_scan_pending_transactions())

    assert rebroadcasts == [
        ("USDT-ARB", raw_tx, tx_hash, "transfer")
    ]


def test_legacy_integral_real_without_exact_units_fails_closed_after_approve(
    tmp_path, monkeypatch
):
    db_path = str(tmp_path / "legacy-fractional-real.sqlite3")
    monkeypatch.setattr(app, "DB_PATH", db_path)
    asyncio.run(app.init_db())

    approve_raw = Web3.to_hex(b"legacy-fractional-approve")
    approve_hash = Web3.keccak(b"legacy-fractional-approve").hex()
    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT INTO dca_plans "
            "(id, user_id, from_asset, amount, interval_hours, btc_address, "
            "next_run, active, deleted, execution_state, active_order_id, "
            "active_order_token, active_order_address, active_order_amount, "
            "active_order_expires) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 1, 0, 'scheduled', ?, ?, ?, ?, ?)",
            (
                7,
                10001,
                "USDT-ARB",
                16.0,
                24,
                "bc1unused",
                1_700_000_000,
                "legacy-fractional-order",
                "order-token",
                "0x1111111111111111111111111111111111111111",
                "16.000000000000000001 USDT",
                4_000_000_000,
            ),
        )
        db.execute(
            "INSERT INTO sent_transactions "
            "(user_id, plan_id, order_id, order_token, network_key, "
            "approve_tx_hash, approve_tx_nonce, approve_raw_tx, amount, "
            "deposit_address, state, error_message) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'tx_pending', ?)",
            (
                10001,
                7,
                "legacy-fractional-order",
                "order-token",
                "USDT-ARB",
                approve_hash,
                8,
                approve_raw,
                16.0,
                "0x1111111111111111111111111111111111111111",
                f"APPROVE_TX_PENDING:{approve_hash}",
            ),
        )
        db.commit()

    async def confirmed_status(_network_key, candidate_hash):
        assert candidate_hash == approve_hash
        return "confirmed"

    def forbidden_rpc(*_args, **_kwargs):
        raise AssertionError("unconfirmed legacy amount must not reach RPC")

    monkeypatch.setitem(app._wallet_passwords, 10001, "test-password")
    monkeypatch.setattr(app, "get_transfer_tx_status", confirmed_status)
    monkeypatch.setattr(sender, "get_web3_instance", forbidden_rpc)

    asyncio.run(app.recovery_scan_pending_transactions())

    with sqlite3.connect(db_path) as db:
        tx_row = db.execute(
            "SELECT state, approve_tx_hash, approve_raw_tx, transfer_tx_hash, "
            "transfer_raw_tx, amount_units, token_decimals, error_message "
            "FROM sent_transactions "
            "WHERE order_id = 'legacy-fractional-order'"
        ).fetchone()
        active_order_id = db.execute(
            "SELECT active_order_id FROM dca_plans WHERE id = 7"
        ).fetchone()[0]

    assert tx_row[:7] == (
        "tx_pending", approve_hash, approve_raw, None, None, None, None
    )
    assert tx_row[7].startswith("INVALID_PAYMENT_AMOUNT:")
    assert active_order_id == "legacy-fractional-order"
