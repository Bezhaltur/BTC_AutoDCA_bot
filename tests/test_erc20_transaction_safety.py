import asyncio
import sqlite3
from types import SimpleNamespace

import pytest
from web3 import Web3

import auto_send as sender
import bot as app
import erc20


class FakeSignedTransaction:
    def __init__(self, raw_transaction):
        self.rawTransaction = raw_transaction


class FakeAccount:
    def __init__(self, raw_transaction=b"signed-transfer"):
        self.raw_transaction = raw_transaction
        self.signed = []

    def sign_transaction(self, tx):
        self.signed.append(dict(tx))
        return FakeSignedTransaction(self.raw_transaction)


class AmbiguousEth:
    def __init__(self):
        self.nonces = [17, 18]
        self.nonce_reads = 0
        self.broadcasts = []

    def get_transaction_count(self, _address, _block):
        nonce = self.nonces[self.nonce_reads]
        self.nonce_reads += 1
        return nonce

    def send_raw_transaction(self, raw_transaction):
        self.broadcasts.append(bytes(raw_transaction))
        if len(self.broadcasts) == 1:
            raise TimeoutError("RPC accepted request but response timed out")
        return Web3.keccak(raw_transaction)


def make_fake_web3(eth):
    return SimpleNamespace(
        eth=eth,
        provider=SimpleNamespace(endpoint_uri="https://unused.invalid"),
        is_connected=lambda: True,
    )


def test_ambiguous_broadcast_retries_identical_signed_bytes_and_fixed_nonce(monkeypatch):
    eth = AmbiguousEth()
    w3 = make_fake_web3(eth)
    account = FakeAccount()
    events = []

    monkeypatch.setattr(erc20.time, "sleep", lambda _seconds: None)

    def build_tx():
        events.append("build")
        return {
            "nonce": eth.get_transaction_count("0xsender", "pending"),
            "to": "0xrecipient",
            "value": 0,
        }

    def persist(action, tx_hash, nonce, raw_tx):
        events.append(("persist", action, tx_hash, nonce, raw_tx))

    tx_hash, tx = erc20.send_transaction_with_retry(
        w3,
        account,
        build_tx,
        network_key="USDT-ARB",
        action_name="transfer",
        persist_prepared_tx=persist,
    )

    assert tx["nonce"] == 17
    assert eth.nonce_reads == 1
    assert len(account.signed) == 1
    assert eth.broadcasts == [b"signed-transfer", b"signed-transfer"]
    assert tx_hash == Web3.keccak(b"signed-transfer").hex()
    assert events[0] == "build"
    assert events[1][0] == "persist"


def test_broadcast_fails_closed_without_persistence_callback():
    eth = AmbiguousEth()
    account = FakeAccount()

    with pytest.raises(RuntimeError, match="persistence is required"):
        erc20.send_transaction_with_retry(
            make_fake_web3(eth),
            account,
            lambda: {"nonce": 17},
            network_key="USDT-ARB",
            action_name="transfer",
        )

    assert account.signed == []
    assert eth.broadcasts == []


def test_nonce_too_low_never_builds_or_sends_with_a_new_nonce():
    class NonceTooLowEth(AmbiguousEth):
        def send_raw_transaction(self, raw_transaction):
            self.broadcasts.append(bytes(raw_transaction))
            raise ValueError({"message": "nonce too low"})

    eth = NonceTooLowEth()
    account = FakeAccount()

    def build_tx():
        return {"nonce": eth.get_transaction_count("0xsender", "pending")}

    try:
        erc20.send_transaction_with_retry(
            make_fake_web3(eth),
            account,
            build_tx,
            network_key="USDT-ARB",
            action_name="transfer",
            persist_prepared_tx=lambda *_args: None,
        )
    except erc20.TransactionBroadcastUncertain as exc:
        assert exc.tx_hash == Web3.keccak(b"signed-transfer").hex()
    else:
        raise AssertionError("nonce-too-low result must remain uncertain")

    assert eth.nonce_reads == 1
    assert len(account.signed) == 1
    assert eth.broadcasts == [b"signed-transfer"]


def test_already_known_uses_local_hash_without_new_nonce():
    class AlreadyKnownEth(AmbiguousEth):
        def send_raw_transaction(self, raw_transaction):
            self.broadcasts.append(bytes(raw_transaction))
            raise ValueError({"message": "already known"})

    eth = AlreadyKnownEth()
    account = FakeAccount()

    def build_tx():
        return {"nonce": eth.get_transaction_count("0xsender", "pending")}

    tx_hash, tx = erc20.send_transaction_with_retry(
        make_fake_web3(eth),
        account,
        build_tx,
        network_key="USDT-ARB",
        action_name="transfer",
        persist_prepared_tx=lambda *_args: None,
    )

    assert tx_hash == Web3.keccak(b"signed-transfer").hex()
    assert tx["nonce"] == 17
    assert eth.nonce_reads == 1
    assert len(account.signed) == 1
    assert eth.broadcasts == [b"signed-transfer"]


def test_restart_rebroadcasts_only_persisted_raw_transaction(tmp_path, monkeypatch):
    db_path = str(tmp_path / "recovery.sqlite3")
    monkeypatch.setattr(app, "DB_PATH", db_path)
    asyncio.run(app.init_db())

    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT INTO sent_transactions "
            "(user_id, plan_id, order_id, network_key, amount, deposit_address, state) "
            "VALUES (?, ?, ?, ?, ?, ?, 'transfering')",
            (10001, 7, "order-1", "USDT-ARB", 25.0, "0xdeposit"),
        )

    raw_tx = Web3.to_hex(b"persisted-signed-transfer")
    tx_hash = Web3.keccak(b"persisted-signed-transfer").hex()
    app.persist_prepared_erc20_transaction(
        7, "order-1", "transfer", tx_hash, 23, raw_tx
    )

    rebroadcasts = []

    async def pending_status(network_key, candidate_hash):
        assert network_key == "USDT-ARB"
        assert candidate_hash == tx_hash
        return "pending"

    async def capture_rebroadcast(network_key, candidate_raw, candidate_hash, action):
        rebroadcasts.append((network_key, candidate_raw, candidate_hash, action))

    async def forbidden_auto_send(*_args, **_kwargs):
        raise AssertionError("recovery must not create a new transfer")

    monkeypatch.setattr(app, "get_transfer_tx_status", pending_status)
    monkeypatch.setattr(app, "rebroadcast_persisted_erc20_transaction", capture_rebroadcast)
    monkeypatch.setattr(app, "auto_send_usdt", forbidden_auto_send)

    asyncio.run(app.recovery_scan_pending_transactions())

    assert rebroadcasts == [("USDT-ARB", raw_tx, tx_hash, "transfer")]
    with sqlite3.connect(db_path) as db:
        row = db.execute(
            "SELECT state, transfer_tx_hash, transfer_tx_nonce, transfer_raw_tx, error_message "
            "FROM sent_transactions WHERE order_id = 'order-1'"
        ).fetchone()
    assert row == ("tx_pending", tx_hash, 23, raw_tx, f"TX_PENDING:{tx_hash}")


def test_unknown_receipt_error_keeps_transfer_pending(monkeypatch):
    transfer_hash = "0x" + "12" * 32
    persisted = []

    class ReceiptEth:
        def wait_for_transaction_receipt(self, _tx_hash, timeout):
            assert timeout == 120
            raise ConnectionError("receipt RPC disconnected")

    class FakeWeb3:
        eth = ReceiptEth()

        @staticmethod
        def from_wei(value, _unit):
            return value / 10**18

    monkeypatch.setattr(sender, "load_keystore", lambda _user_id: {"crypto": {}})
    monkeypatch.setattr(sender, "decrypt_private_key", lambda _keystore, _password: "1" * 64)
    monkeypatch.setattr(sender, "get_web3_instance", lambda _network: FakeWeb3())
    monkeypatch.setattr(sender, "get_network_config", lambda _network: {"native_token": "ETH"})
    monkeypatch.setattr(sender, "get_usdt_token_decimals", lambda *_args: 6)
    monkeypatch.setattr(
        sender, "get_usdt_balance_units", lambda *_args: 100_000_000
    )
    monkeypatch.setattr(sender, "get_native_balance", lambda *_args: 10.0)
    monkeypatch.setattr(sender, "estimate_gas_for_transfer", lambda *_args: 75_000)
    monkeypatch.setattr(sender, "build_gas_params", lambda *_args: {"gasPrice": 1})

    def fake_transfer(*args):
        persist = args[-1]
        persist("transfer", transfer_hash, 9, "0xdeadbeef")
        return transfer_hash

    monkeypatch.setattr(sender, "transfer_usdt", fake_transfer)
    sender._SEND_LOCKS.clear()

    result = asyncio.run(
        sender.auto_send_usdt(
            network_key="USDT-ARB",
            user_id=10001,
            wallet_password="test-password",
            deposit_address="0x1111111111111111111111111111111111111111",
            required_amount="25",
            btc_address="bc1unused",
            order_id="order-1",
            persist_prepared_tx=lambda *values: persisted.append(values),
        )
    )

    assert result == (False, None, transfer_hash, f"TX_PENDING:{transfer_hash}")
    assert persisted == [("transfer", transfer_hash, 9, "0xdeadbeef")]


def configure_direct_send(monkeypatch, *, receipt_status=1, native_balance=2e-13):
    class ReceiptEth:
        def wait_for_transaction_receipt(self, _tx_hash, timeout):
            assert timeout == 120
            return SimpleNamespace(status=receipt_status, blockNumber=123)

    class FakeWeb3:
        eth = ReceiptEth()

        @staticmethod
        def from_wei(value, _unit):
            return value / 10**18

    monkeypatch.setattr(sender, "load_keystore", lambda _user_id: {"crypto": {}})
    monkeypatch.setattr(sender, "decrypt_private_key", lambda _keystore, _password: "1" * 64)
    monkeypatch.setattr(sender, "get_web3_instance", lambda _network: FakeWeb3())
    monkeypatch.setattr(sender, "get_network_config", lambda _network: {"native_token": "ETH"})
    monkeypatch.setattr(sender, "get_usdt_token_decimals", lambda *_args: 6)
    monkeypatch.setattr(
        sender, "get_usdt_balance_units", lambda *_args: 100_000_000
    )
    monkeypatch.setattr(sender, "get_native_balance", lambda *_args: native_balance)
    monkeypatch.setattr(sender, "estimate_gas_for_transfer", lambda *_args: 75_000)
    monkeypatch.setattr(sender, "build_gas_params", lambda *_args: {"gasPrice": 1})
    sender._SEND_LOCKS.clear()


def test_new_direct_flow_skips_zero_allowance_and_approve(monkeypatch):
    configure_direct_send(monkeypatch)
    transfer_hash = "0x" + "34" * 32
    deposit_address = "0x1111111111111111111111111111111111111111"
    allowance_calls = []
    approve_calls = []
    transfer_calls = []
    persisted = []

    def zero_allowance(*args):
        allowance_calls.append(args)
        return 0.0

    def forbidden_approve(*args):
        approve_calls.append(args)
        raise AssertionError("new direct flow must not approve")

    def direct_transfer(*args):
        transfer_calls.append(args)
        args[-1]("transfer", transfer_hash, 9, "0xdeadbeef")
        return transfer_hash

    monkeypatch.setattr(erc20, "check_allowance", zero_allowance)
    monkeypatch.setattr(erc20, "approve_usdt", forbidden_approve)
    monkeypatch.setattr(sender, "transfer_usdt", direct_transfer)

    result = asyncio.run(
        sender.auto_send_usdt(
            network_key="USDT-ARB",
            user_id=10001,
            wallet_password="test-password",
            deposit_address=deposit_address,
            required_amount="25",
            btc_address="bc1unused",
            order_id="direct-order",
            persist_prepared_tx=lambda *values: persisted.append(values),
        )
    )

    assert result == (True, None, transfer_hash, "")
    assert allowance_calls == []
    assert approve_calls == []
    assert len(transfer_calls) == 1
    assert transfer_calls[0][3] == Web3.to_checksum_address(deposit_address)
    assert transfer_calls[0][4] == 25_000_000
    assert transfer_calls[0][5] is False
    assert persisted == [("transfer", transfer_hash, 9, "0xdeadbeef")]


def test_new_direct_flow_gas_check_and_dry_run_use_only_transfer(monkeypatch):
    configure_direct_send(monkeypatch)
    gas_calls = []
    transfer_calls = []

    def transfer_gas(*args):
        gas_calls.append(args)
        return 75_000

    def dry_run_transfer(*args):
        transfer_calls.append(args)
        return None

    monkeypatch.setattr(sender, "estimate_gas_for_transfer", transfer_gas)
    monkeypatch.setattr(sender, "transfer_usdt", dry_run_transfer)
    monkeypatch.setattr(
        erc20,
        "approve_usdt",
        lambda *_args: (_ for _ in ()).throw(AssertionError("dry-run must not approve")),
    )
    monkeypatch.setattr(
        erc20,
        "check_allowance",
        lambda *_args: (_ for _ in ()).throw(AssertionError("dry-run must not check allowance")),
    )
    persisted = []

    result = asyncio.run(
        sender.auto_send_usdt(
            network_key="USDT-ARB",
            user_id=10001,
            wallet_password="test-password",
            deposit_address="0x1111111111111111111111111111111111111111",
            required_amount="25",
            btc_address="bc1unused",
            order_id="dry-run-order",
            dry_run=True,
            persist_prepared_tx=lambda *values: persisted.append(values),
        )
    )

    assert result == (True, None, None, "DRY RUN: Would transfer USDT")
    assert len(gas_calls) == 1
    assert len(transfer_calls) == 1
    assert transfer_calls[0][5] is True
    assert persisted == []


def seed_legacy_approve(db_path, *, state="tx_pending", exact_intent=False):
    approve_raw = Web3.to_hex(b"legacy-approve-intent")
    approve_hash = Web3.keccak(b"legacy-approve-intent").hex()
    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT INTO dca_plans "
            "(id, user_id, from_asset, amount, interval_hours, btc_address, next_run, active, deleted, "
            "execution_state, active_order_id, active_order_token, active_order_address, "
            "active_order_amount, active_order_expires) "
            "VALUES (7, 10001, 'USDT-ARB', 25, 24, 'bc1unused', 1700000000, 1, 0, "
            "'scheduled', 'legacy-approve-order', 'order-token', "
            "'0x1111111111111111111111111111111111111111', '25 USDT', 4000000000)"
        )
        db.execute(
            "INSERT INTO sent_transactions "
            "(user_id, plan_id, order_id, order_token, network_key, approve_tx_hash, approve_tx_nonce, "
            "approve_raw_tx, amount, deposit_address, state, error_message) "
            "VALUES (10001, 7, 'legacy-approve-order', 'order-token', 'USDT-ARB', ?, 8, ?, "
            "25, '0x1111111111111111111111111111111111111111', ?, ?)",
            (approve_hash, approve_raw, state, f"APPROVE_TX_PENDING:{approve_hash}"),
        )
        if exact_intent:
            db.execute(
                "UPDATE sent_transactions SET amount_units = '25000000', token_decimals = 6 "
                "WHERE order_id = 'legacy-approve-order'"
            )
    return approve_hash, approve_raw


def test_legacy_pending_approve_rebroadcasts_only_saved_raw(tmp_path, monkeypatch):
    db_path = str(tmp_path / "legacy-approve-pending.sqlite3")
    monkeypatch.setattr(app, "DB_PATH", db_path)
    asyncio.run(app.init_db())
    approve_hash, approve_raw = seed_legacy_approve(db_path)
    rebroadcasts = []

    async def pending_status(_network_key, candidate_hash):
        assert candidate_hash == approve_hash
        return "pending"

    async def capture_rebroadcast(network_key, raw_tx, tx_hash, action_name):
        rebroadcasts.append((network_key, raw_tx, tx_hash, action_name))

    monkeypatch.setattr(app, "get_transfer_tx_status", pending_status)
    monkeypatch.setattr(app, "rebroadcast_persisted_erc20_transaction", capture_rebroadcast)
    monkeypatch.setattr(
        app,
        "resume_transfer_after_approve",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("pending approve must not start transfer")),
    )

    asyncio.run(app.recovery_scan_pending_transactions())

    assert rebroadcasts == [("USDT-ARB", approve_raw, approve_hash, "approve")]
    with sqlite3.connect(db_path) as db:
        row = db.execute(
            "SELECT state, approve_tx_hash, approve_tx_nonce, approve_raw_tx, transfer_tx_hash "
            "FROM sent_transactions WHERE order_id = 'legacy-approve-order'"
        ).fetchone()
    assert row == ("tx_pending", approve_hash, 8, approve_raw, None)


def test_confirmed_approve_with_exact_intent_resumes_one_direct_transfer(tmp_path, monkeypatch):
    db_path = str(tmp_path / "legacy-approve-confirmed.sqlite3")
    monkeypatch.setattr(app, "DB_PATH", db_path)
    asyncio.run(app.init_db())
    approve_hash, approve_raw = seed_legacy_approve(db_path, exact_intent=True)
    transfer_raw = Web3.to_hex(b"legacy-transfer-intent")
    transfer_hash = Web3.keccak(b"legacy-transfer-intent").hex()
    sends = []
    monkeypatch.setitem(app._wallet_passwords, 10001, "test-password")
    configure_direct_send(monkeypatch)

    async def confirmed_status(_network_key, candidate_hash):
        assert candidate_hash == approve_hash
        return "confirmed"

    def direct_transfer(*args):
        sends.append(args)
        args[-1]("transfer", transfer_hash, 9, transfer_raw)
        return transfer_hash

    monkeypatch.setattr(app, "get_transfer_tx_status", confirmed_status)
    monkeypatch.setattr(sender, "transfer_usdt", direct_transfer)

    asyncio.run(app.recovery_scan_pending_transactions())

    assert len(sends) == 1
    assert sends[0][3] == Web3.to_checksum_address(
        "0x1111111111111111111111111111111111111111"
    )
    assert sends[0][4] == 25_000_000
    assert callable(sends[0][-1])
    with sqlite3.connect(db_path) as db:
        row = db.execute(
            "SELECT state, approve_tx_hash, approve_tx_nonce, approve_raw_tx, transfer_tx_hash, "
            "amount_units, token_decimals "
            "FROM sent_transactions WHERE order_id = 'legacy-approve-order'"
        ).fetchone()
    assert row == (
        "confirmed", approve_hash, 8, approve_raw, transfer_hash, "25000000", 6
    )


def test_legacy_approve_failure_never_confirms_transfer(tmp_path, monkeypatch):
    db_path = str(tmp_path / "legacy-approve-failed.sqlite3")
    monkeypatch.setattr(app, "DB_PATH", db_path)
    asyncio.run(app.init_db())
    approve_hash, approve_raw = seed_legacy_approve(db_path)

    async def failed_status(_network_key, candidate_hash):
        assert candidate_hash == approve_hash
        return "failed"

    monkeypatch.setattr(app, "get_transfer_tx_status", failed_status)
    asyncio.run(app.recovery_scan_pending_transactions())

    with sqlite3.connect(db_path) as db:
        tx_row = db.execute(
            "SELECT state, approve_tx_hash, approve_raw_tx, transfer_tx_hash "
            "FROM sent_transactions WHERE order_id = 'legacy-approve-order'"
        ).fetchone()
        active_order_id = db.execute(
            "SELECT active_order_id FROM dca_plans WHERE id = 7"
        ).fetchone()[0]
    assert tx_row == ("failed", approve_hash, approve_raw, None)
    assert active_order_id is None


def test_transfer_artifacts_take_priority_over_legacy_approve(tmp_path, monkeypatch):
    db_path = str(tmp_path / "legacy-transfer-priority.sqlite3")
    monkeypatch.setattr(app, "DB_PATH", db_path)
    asyncio.run(app.init_db())
    approve_hash, approve_raw = seed_legacy_approve(db_path)
    transfer_raw = Web3.to_hex(b"persisted-transfer-intent")
    transfer_hash = Web3.keccak(b"persisted-transfer-intent").hex()
    with sqlite3.connect(db_path) as db:
        db.execute(
            "UPDATE sent_transactions SET transfer_tx_hash = ?, transfer_tx_nonce = 9, transfer_raw_tx = ? "
            "WHERE order_id = 'legacy-approve-order'",
            (transfer_hash, transfer_raw),
        )
    checked_hashes = []

    async def confirmed_status(_network_key, candidate_hash):
        checked_hashes.append(candidate_hash)
        return "confirmed"

    async def forbidden_resume(**_kwargs):
        raise AssertionError("transfer artifacts must not resume approve")

    monkeypatch.setattr(app, "get_transfer_tx_status", confirmed_status)
    monkeypatch.setattr(app, "resume_transfer_after_approve", forbidden_resume)
    asyncio.run(app.recovery_scan_pending_transactions())

    assert checked_hashes == [transfer_hash]
    with sqlite3.connect(db_path) as db:
        row = db.execute(
            "SELECT state, approve_tx_hash, approve_raw_tx, transfer_tx_hash, transfer_raw_tx "
            "FROM sent_transactions WHERE order_id = 'legacy-approve-order'"
        ).fetchone()
    assert row == ("confirmed", approve_hash, approve_raw, transfer_hash, transfer_raw)


def test_prepared_transaction_is_committed_before_broadcast(tmp_path, monkeypatch):
    db_path = str(tmp_path / "commit-before-broadcast.sqlite3")
    monkeypatch.setattr(app, "DB_PATH", db_path)
    asyncio.run(app.init_db())

    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT INTO sent_transactions "
            "(user_id, plan_id, order_id, network_key, amount, deposit_address, state) "
            "VALUES (?, ?, ?, ?, ?, ?, 'transfering')",
            (10001, 7, "order-commit", "USDT-ARB", 25.0, "0xdeposit"),
        )

    raw_bytes = b"committed-before-broadcast"
    expected_hash = Web3.keccak(raw_bytes).hex()

    class CommitCheckingEth:
        def send_raw_transaction(self, candidate_raw):
            with sqlite3.connect(db_path) as db:
                persisted = db.execute(
                    "SELECT transfer_tx_hash, transfer_tx_nonce, transfer_raw_tx "
                    "FROM sent_transactions WHERE order_id = 'order-commit'"
                ).fetchone()
            assert persisted == (expected_hash, 31, Web3.to_hex(raw_bytes))
            return Web3.keccak(candidate_raw)

    account = FakeAccount(raw_bytes)
    erc20.send_transaction_with_retry(
        make_fake_web3(CommitCheckingEth()),
        account,
        lambda: {"nonce": 31},
        network_key="USDT-ARB",
        action_name="transfer",
        persist_prepared_tx=app.make_prepared_tx_persister(7, "order-commit"),
    )


def test_prepared_transaction_cas_rejects_different_intent(tmp_path, monkeypatch):
    db_path = str(tmp_path / "prepared-cas.sqlite3")
    monkeypatch.setattr(app, "DB_PATH", db_path)
    asyncio.run(app.init_db())

    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT INTO sent_transactions "
            "(user_id, plan_id, order_id, network_key, amount, deposit_address, state) "
            "VALUES (?, ?, ?, ?, ?, ?, 'transfering')",
            (10001, 7, "order-cas", "USDT-ARB", 25.0, "0xdeposit"),
        )

    first_raw = Web3.to_hex(b"first-signed-intent")
    first_hash = Web3.keccak(b"first-signed-intent").hex()
    app.persist_prepared_erc20_transaction(
        7, "order-cas", "transfer", first_hash, 41, first_raw
    )

    rejected_intents = (
        (Web3.keccak(b"different-signed-intent").hex(), 41, first_raw),
        (first_hash, 42, first_raw),
        (first_hash, 41, Web3.to_hex(b"different-signed-intent")),
    )
    for candidate_hash, candidate_nonce, candidate_raw in rejected_intents:
        with pytest.raises(RuntimeError, match="Cannot persist prepared transfer"):
            app.persist_prepared_erc20_transaction(
                7,
                "order-cas",
                "transfer",
                candidate_hash,
                candidate_nonce,
                candidate_raw,
            )

    with sqlite3.connect(db_path) as db:
        row = db.execute(
            "SELECT state, transfer_tx_hash, transfer_tx_nonce, transfer_raw_tx "
            "FROM sent_transactions WHERE order_id = 'order-cas'"
        ).fetchone()
    assert row == ("transfering", first_hash, 41, first_raw)


def test_approve_only_transfering_crash_cannot_confirm_transfer(tmp_path, monkeypatch):
    db_path = str(tmp_path / "approve-only-crash.sqlite3")
    monkeypatch.setattr(app, "DB_PATH", db_path)
    asyncio.run(app.init_db())

    approve_raw = Web3.to_hex(b"persisted-approve")
    approve_hash = Web3.keccak(b"persisted-approve").hex()
    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT INTO sent_transactions "
            "(user_id, plan_id, order_id, network_key, approve_tx_hash, approve_tx_nonce, "
            "approve_raw_tx, amount, deposit_address, state, error_message) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'transfering', NULL)",
            (10001, 7, "order-approve", "USDT-ARB", approve_hash, 5, approve_raw, 25.0, "0xdeposit"),
        )

    checked_hashes = []
    resumed = []

    async def confirmed_status(_network_key, candidate_hash):
        checked_hashes.append(candidate_hash)
        return "confirmed"

    async def capture_resume(**kwargs):
        resumed.append(kwargs)
        return ("blocked", approve_hash, None, "TRANSFER_NOT_PREPARED")

    monkeypatch.setattr(app, "get_transfer_tx_status", confirmed_status)
    monkeypatch.setattr(app, "resume_transfer_after_approve", capture_resume)

    asyncio.run(app.recovery_scan_pending_transactions())

    assert checked_hashes == [approve_hash]
    assert resumed == []
    with sqlite3.connect(db_path) as db:
        row = db.execute(
            "SELECT state, approve_tx_hash, transfer_tx_hash, error_message FROM sent_transactions "
            "WHERE order_id = 'order-approve'"
        ).fetchone()
    assert row[:3] == ("tx_pending", approve_hash, None)
    assert row[3].startswith("INVALID_PAYMENT_AMOUNT:")


def test_legacy_blocked_without_artifacts_stays_fail_closed(tmp_path, monkeypatch):
    db_path = str(tmp_path / "legacy-blocked.sqlite3")
    monkeypatch.setattr(app, "DB_PATH", db_path)
    asyncio.run(app.init_db())

    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT INTO dca_plans (id, user_id, active_order_id) VALUES (?, ?, ?)",
            (7, 10001, "legacy-order"),
        )
        db.execute(
            "INSERT INTO sent_transactions "
            "(user_id, plan_id, order_id, network_key, amount, deposit_address, state) "
            "VALUES (?, ?, ?, ?, ?, ?, 'blocked')",
            (10001, 7, "legacy-order", "USDT-ARB", 25.0, "0xdeposit"),
        )

    async def forbidden_status(*_args):
        raise AssertionError("hashless legacy row must not query or send on-chain")

    monkeypatch.setattr(app, "get_transfer_tx_status", forbidden_status)
    asyncio.run(app.recovery_scan_pending_transactions())

    with sqlite3.connect(db_path) as db:
        tx_row = db.execute(
            "SELECT state, error_message FROM sent_transactions WHERE order_id = 'legacy-order'"
        ).fetchone()
        plan_row = db.execute(
            "SELECT active_order_id FROM dca_plans WHERE id = 7"
        ).fetchone()
    assert tx_row[0] == "blocked"
    assert "manual review required" in tx_row[1]
    assert plan_row == ("legacy-order",)


def test_confirmed_transfer_receipt_never_rebroadcasts(tmp_path, monkeypatch):
    db_path = str(tmp_path / "confirmed-no-rebroadcast.sqlite3")
    monkeypatch.setattr(app, "DB_PATH", db_path)
    asyncio.run(app.init_db())

    raw_tx = Web3.to_hex(b"confirmed-transfer")
    tx_hash = Web3.keccak(b"confirmed-transfer").hex()
    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT INTO sent_transactions "
            "(user_id, plan_id, order_id, network_key, transfer_tx_hash, transfer_tx_nonce, "
            "transfer_raw_tx, amount, deposit_address, state, error_message) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'tx_pending', ?)",
            (10001, 7, "order-confirmed", "USDT-ARB", tx_hash, 9, raw_tx, 25.0, "0xdeposit", f"TX_PENDING:{tx_hash}"),
        )

    async def confirmed_status(_network_key, candidate_hash):
        assert candidate_hash == tx_hash
        return "confirmed"

    async def forbidden_rebroadcast(*_args):
        raise AssertionError("confirmed transaction must not be rebroadcast")

    monkeypatch.setattr(app, "get_transfer_tx_status", confirmed_status)
    monkeypatch.setattr(app, "rebroadcast_persisted_erc20_transaction", forbidden_rebroadcast)
    asyncio.run(app.recovery_scan_pending_transactions())

    with sqlite3.connect(db_path) as db:
        state = db.execute(
            "SELECT state FROM sent_transactions WHERE order_id = 'order-confirmed'"
        ).fetchone()[0]
    assert state == "confirmed"


def test_rebroadcast_helper_restores_exact_raw_bytes():
    original_raw = b"\x00\x01persisted-signed-transfer\xff"
    raw_hex = Web3.to_hex(original_raw)
    expected_hash = Web3.keccak(original_raw).hex()
    broadcasts = []

    class RecordingEth:
        def send_raw_transaction(self, candidate_raw):
            broadcasts.append(bytes(candidate_raw))
            return Web3.keccak(candidate_raw)

    result = erc20.rebroadcast_raw_transaction(
        make_fake_web3(RecordingEth()),
        raw_hex,
        expected_hash,
        action_name="transfer",
    )

    assert result == expected_hash
    assert broadcasts == [original_raw]


def test_rpc_hash_mismatch_remains_uncertain():
    class MismatchedHashEth:
        def send_raw_transaction(self, _candidate_raw):
            return Web3.keccak(b"different-transaction")

    raw_bytes = b"signed-transfer"
    expected_hash = Web3.keccak(raw_bytes).hex()
    with pytest.raises(erc20.TransactionBroadcastUncertain) as exc_info:
        erc20._broadcast_raw_transaction_with_retry(
            make_fake_web3(MismatchedHashEth()),
            raw_bytes,
            expected_hash,
            action_name="transfer",
        )
    assert exc_info.value.tx_hash == expected_hash


def seed_active_order(
    db_path,
    *,
    state,
    order_id="guarded-order",
    expires=1_700_000_600,
    transfer_hash=None,
    transfer_nonce=None,
    transfer_raw=None,
):
    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT INTO dca_plans "
            "(id, user_id, from_asset, amount, interval_hours, btc_address, next_run, active, deleted, "
            "execution_state, active_order_id, active_order_token, active_order_address, "
            "active_order_amount, active_order_expires) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 1, 0, 'scheduled', ?, ?, ?, ?, ?)",
            (
                7, 10001, "USDT-ARB", 25.0, 24, "bc1unused", 1_700_000_000,
                order_id, "order-token", "0xdeposit", "25 USDT", expires,
            ),
        )
        db.execute(
            "INSERT INTO sent_transactions "
            "(user_id, plan_id, order_id, order_token, network_key, transfer_tx_hash, "
            "transfer_tx_nonce, transfer_raw_tx, amount, deposit_address, state) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                10001, 7, order_id, "order-token", "USDT-ARB", transfer_hash,
                transfer_nonce, transfer_raw, 25.0, "0xdeposit", state,
            ),
        )


def read_gate_state(db_path, order_id="guarded-order"):
    with sqlite3.connect(db_path) as db:
        tx_state = db.execute(
            "SELECT state FROM sent_transactions WHERE order_id = ?", (order_id,)
        ).fetchone()[0]
        active_order_id = db.execute(
            "SELECT active_order_id FROM dca_plans WHERE id = 7"
        ).fetchone()[0]
    return tx_state, active_order_id


def test_cas_conflict_prevents_broadcast(tmp_path, monkeypatch):
    db_path = str(tmp_path / "cas-no-broadcast.sqlite3")
    monkeypatch.setattr(app, "DB_PATH", db_path)
    asyncio.run(app.init_db())
    seed_active_order(db_path, state="transfering")

    first_raw = Web3.to_hex(b"first-intent")
    first_hash = Web3.keccak(b"first-intent").hex()
    app.persist_prepared_erc20_transaction(
        7, "guarded-order", "transfer", first_hash, 12, first_raw
    )

    class RecordingEth:
        def __init__(self):
            self.broadcasts = []

        def send_raw_transaction(self, candidate_raw):
            self.broadcasts.append(bytes(candidate_raw))
            return Web3.keccak(candidate_raw)

    eth = RecordingEth()
    with pytest.raises(RuntimeError, match="Cannot persist prepared transfer"):
        erc20.send_transaction_with_retry(
            make_fake_web3(eth),
            FakeAccount(b"second-intent"),
            lambda: {"nonce": 13},
            network_key="USDT-ARB",
            action_name="transfer",
            persist_prepared_tx=app.make_prepared_tx_persister(7, "guarded-order"),
        )
    assert eth.broadcasts == []


def test_approve_cas_rejects_any_existing_transfer_artifact(tmp_path, monkeypatch):
    db_path = str(tmp_path / "approve-cross-phase-cas.sqlite3")
    monkeypatch.setattr(app, "DB_PATH", db_path)
    asyncio.run(app.init_db())
    seed_active_order(db_path, state="transfering")
    with sqlite3.connect(db_path) as db:
        db.execute(
            "UPDATE sent_transactions SET transfer_raw_tx = ? WHERE order_id = 'guarded-order'",
            (Web3.to_hex(b"partial-transfer-intent"),),
        )

    with pytest.raises(RuntimeError, match="Cannot persist prepared approve"):
        app.persist_prepared_erc20_transaction(
            7,
            "guarded-order",
            "approve",
            Web3.keccak(b"approve-intent").hex(),
            11,
            Web3.to_hex(b"approve-intent"),
        )


def test_scheduler_final_status_keeps_unresolved_gate(tmp_path, monkeypatch):
    db_path = str(tmp_path / "scheduler-final.sqlite3")
    monkeypatch.setattr(app, "DB_PATH", db_path)
    asyncio.run(app.init_db())
    seed_active_order(db_path, state="blocked")
    monkeypatch.setattr(app.time, "time", lambda: 1_700_000_100)

    async def final_status(*_args, **_kwargs):
        return next(iter(app.FINAL_FIXEDFLOAT_ORDER_STATUSES))

    async def stop_scheduler(*_args, **_kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(app, "get_fixedfloat_order_status_with_retry", final_status)
    monkeypatch.setattr(app.asyncio, "sleep", stop_scheduler)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(app.dca_scheduler())
    assert read_gate_state(db_path) == ("blocked", "guarded-order")


def test_manual_final_status_keeps_unresolved_gate(tmp_path, monkeypatch):
    db_path = str(tmp_path / "manual-final.sqlite3")
    monkeypatch.setattr(app, "DB_PATH", db_path)
    asyncio.run(app.init_db())
    seed_active_order(db_path, state="transfering", expires=1_700_000_000)
    monkeypatch.setattr(app.time, "time", lambda: 1_700_000_100)

    async def final_status(*_args, **_kwargs):
        return next(iter(app.FINAL_FIXEDFLOAT_ORDER_STATUSES))

    progress = []

    async def capture_progress(*args, **_kwargs):
        progress.append(args)

    async def answer(*_args, **_kwargs):
        return None

    message = SimpleNamespace(
        from_user=SimpleNamespace(id=10001),
        text="/execute",
        answer=answer,
    )
    monkeypatch.setattr(app, "get_fixedfloat_order_status_with_retry", final_status)
    monkeypatch.setattr(app, "update_order_progress_message", capture_progress)

    asyncio.run(app.cmd_execute(message))
    assert progress
    assert read_gate_state(db_path) == ("transfering", "guarded-order")


def run_one_order_monitor_iteration(monkeypatch):
    sleep_calls = 0

    async def one_iteration_sleep(_seconds):
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(app.asyncio, "sleep", one_iteration_sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(app.order_monitor())


def _stop_scheduler_after_one_iteration(monkeypatch):
    async def stop_scheduler(*_args, **_kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(app.asyncio, "sleep", stop_scheduler)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(app.dca_scheduler())


def _manual_execute_message():
    async def answer(*_args, **_kwargs):
        return None

    return SimpleNamespace(
        from_user=SimpleNamespace(id=10001),
        text="/execute",
        answer=answer,
    )


def test_scheduler_approve_confirmed_prioritizes_raw_only_transfer(tmp_path, monkeypatch):
    db_path = str(tmp_path / "scheduler-raw-only-transfer.sqlite3")
    monkeypatch.setattr(app, "DB_PATH", db_path)
    asyncio.run(app.init_db())
    raw = Web3.to_hex(b"scheduler-owned-transfer")
    expected_hash = Web3.keccak(b"scheduler-owned-transfer").hex()
    seed_active_order(
        db_path,
        state="approve_confirmed",
        transfer_nonce=23,
        transfer_raw=raw,
    )
    with sqlite3.connect(db_path) as db:
        db.execute(
            "UPDATE sent_transactions SET approve_tx_hash = ?, approve_tx_nonce = 8, approve_raw_tx = ? "
            "WHERE order_id = 'guarded-order'",
            (
                Web3.keccak(b"scheduler-prior-approve").hex(),
                Web3.to_hex(b"scheduler-prior-approve"),
            ),
        )
    monkeypatch.setattr(app.time, "time", lambda: 1_700_000_100)
    async def active_status(*_args, **_kwargs):
        return "NEW"

    monkeypatch.setattr(app, "get_fixedfloat_order_status_with_retry", active_status)

    checked = []
    rebroadcasts = []

    async def pending_status(_network_key, tx_hash):
        checked.append(tx_hash)
        return "pending"

    async def forbidden_resume(**_kwargs):
        raise AssertionError("existing signed transfer must not create another intent")

    async def capture_rebroadcast(network_key, candidate_raw, tx_hash, action_name):
        rebroadcasts.append((network_key, candidate_raw, tx_hash, action_name))

    monkeypatch.setattr(app, "get_transfer_tx_status", pending_status)
    monkeypatch.setattr(app, "resume_transfer_after_approve", forbidden_resume)
    monkeypatch.setattr(app, "rebroadcast_persisted_erc20_transaction", capture_rebroadcast)
    _stop_scheduler_after_one_iteration(monkeypatch)

    assert checked == [expected_hash]
    assert rebroadcasts == [("USDT-ARB", raw, expected_hash, "transfer")]
    with sqlite3.connect(db_path) as db:
        row = db.execute(
            "SELECT state, transfer_tx_hash, transfer_tx_nonce, transfer_raw_tx, amount_units, token_decimals "
            "FROM sent_transactions WHERE order_id = 'guarded-order'"
        ).fetchone()
    assert row == ("tx_pending", None, 23, raw, None, None)
    assert read_gate_state(db_path) == ("tx_pending", "guarded-order")


def test_manual_approve_confirmed_prioritizes_raw_only_transfer(tmp_path, monkeypatch):
    db_path = str(tmp_path / "manual-raw-only-transfer.sqlite3")
    monkeypatch.setattr(app, "DB_PATH", db_path)
    asyncio.run(app.init_db())
    raw = Web3.to_hex(b"manual-owned-transfer")
    expected_hash = Web3.keccak(b"manual-owned-transfer").hex()
    seed_active_order(
        db_path,
        state="approve_confirmed",
        expires=1_700_000_000,
        transfer_nonce=24,
        transfer_raw=raw,
    )
    with sqlite3.connect(db_path) as db:
        db.execute(
            "UPDATE sent_transactions SET approve_tx_hash = ?, approve_tx_nonce = 8, approve_raw_tx = ? "
            "WHERE order_id = 'guarded-order'",
            (
                Web3.keccak(b"manual-prior-approve").hex(),
                Web3.to_hex(b"manual-prior-approve"),
            ),
        )
    monkeypatch.setattr(app.time, "time", lambda: 1_700_000_100)

    async def active_status(*_args, **_kwargs):
        return "NEW"

    checked = []
    rebroadcasts = []

    async def pending_status(_network_key, tx_hash):
        checked.append(tx_hash)
        return "pending"

    async def forbidden_resume(**_kwargs):
        raise AssertionError("existing signed transfer must not create another intent")

    async def capture_rebroadcast(network_key, candidate_raw, tx_hash, action_name):
        rebroadcasts.append((network_key, candidate_raw, tx_hash, action_name))

    monkeypatch.setattr(app, "get_fixedfloat_order_status_with_retry", active_status)
    monkeypatch.setattr(app, "get_transfer_tx_status", pending_status)
    monkeypatch.setattr(app, "resume_transfer_after_approve", forbidden_resume)
    monkeypatch.setattr(app, "rebroadcast_persisted_erc20_transaction", capture_rebroadcast)
    asyncio.run(app.cmd_execute(_manual_execute_message()))

    assert checked == [expected_hash]
    assert rebroadcasts == [("USDT-ARB", raw, expected_hash, "transfer")]
    with sqlite3.connect(db_path) as db:
        row = db.execute(
            "SELECT state, transfer_tx_hash, transfer_tx_nonce, transfer_raw_tx, amount_units, token_decimals "
            "FROM sent_transactions WHERE order_id = 'guarded-order'"
        ).fetchone()
    assert row == ("tx_pending", None, 24, raw, None, None)
    assert read_gate_state(db_path) == ("tx_pending", "guarded-order")


def _seed_confirmed_legacy_approve_without_exact_intent(db_path, *, expires=1_700_000_600):
    seed_active_order(db_path, state="approve_confirmed", expires=expires)
    approve_raw = Web3.to_hex(b"confirmed-legacy-approve")
    approve_hash = Web3.keccak(b"confirmed-legacy-approve").hex()
    with sqlite3.connect(db_path) as db:
        db.execute(
            "UPDATE sent_transactions SET approve_tx_hash = ?, approve_tx_nonce = 8, approve_raw_tx = ? "
            "WHERE order_id = 'guarded-order'",
            (approve_hash, approve_raw),
        )
    return approve_hash, approve_raw


def _seed_approve_confirmed_with_raw_transfer(db_path, *, expires=1_700_000_600):
    transfer_raw = Web3.to_hex(b"terminal-owned-transfer")
    seed_active_order(
        db_path,
        state="approve_confirmed",
        expires=expires,
        transfer_nonce=29,
        transfer_raw=transfer_raw,
    )
    approve_raw = Web3.to_hex(b"terminal-prior-approve")
    approve_hash = Web3.keccak(b"terminal-prior-approve").hex()
    with sqlite3.connect(db_path) as db:
        db.execute(
            "UPDATE sent_transactions SET approve_tx_hash = ?, approve_tx_nonce = 8, approve_raw_tx = ? "
            "WHERE order_id = 'guarded-order'",
            (approve_hash, approve_raw),
        )
    return approve_hash, approve_raw, transfer_raw


def _read_gate_artifacts(db_path):
    with sqlite3.connect(db_path) as db:
        row = db.execute(
            "SELECT state, approve_tx_hash, approve_tx_nonce, approve_raw_tx, "
            "transfer_tx_hash, transfer_tx_nonce, transfer_raw_tx "
            "FROM sent_transactions WHERE order_id = 'guarded-order'"
        ).fetchone()
        active_order_id = db.execute(
            "SELECT active_order_id FROM dca_plans WHERE id = 7"
        ).fetchone()[0]
    return row, active_order_id


def test_gate_refuses_approve_confirmed_with_persisted_artifacts(tmp_path, monkeypatch):
    db_path = str(tmp_path / "approve-confirmed-gate.sqlite3")
    monkeypatch.setattr(app, "DB_PATH", db_path)
    asyncio.run(app.init_db())
    _seed_approve_confirmed_with_raw_transfer(db_path)

    async def can_release():
        async with app.aiosqlite.connect(db_path) as db:
            return await app._active_order_gate_can_be_released(
                db, 7, "guarded-order"
            )

    assert asyncio.run(can_release()) is False


def test_scheduler_terminal_status_keeps_approve_confirmed_raw_transfer(
    tmp_path, monkeypatch
):
    db_path = str(tmp_path / "scheduler-terminal-raw-transfer.sqlite3")
    monkeypatch.setattr(app, "DB_PATH", db_path)
    asyncio.run(app.init_db())
    approve_hash, approve_raw, transfer_raw = _seed_approve_confirmed_with_raw_transfer(
        db_path
    )
    monkeypatch.setattr(app.time, "time", lambda: 1_700_000_100)

    async def final_status(*_args, **_kwargs):
        return next(iter(app.FINAL_FIXEDFLOAT_ORDER_STATUSES))

    monkeypatch.setattr(app, "get_fixedfloat_order_status_with_retry", final_status)
    _stop_scheduler_after_one_iteration(monkeypatch)

    row, active_order_id = _read_gate_artifacts(db_path)
    assert row == (
        "approve_confirmed", approve_hash, 8, approve_raw, None, 29, transfer_raw
    )
    assert active_order_id == "guarded-order"


def test_scheduler_local_expiry_keeps_approve_confirmed_raw_transfer(
    tmp_path, monkeypatch
):
    db_path = str(tmp_path / "scheduler-expired-raw-transfer.sqlite3")
    monkeypatch.setattr(app, "DB_PATH", db_path)
    asyncio.run(app.init_db())
    approve_hash, approve_raw, transfer_raw = _seed_approve_confirmed_with_raw_transfer(
        db_path, expires=1_700_000_000
    )
    monkeypatch.setattr(app.time, "time", lambda: 1_700_000_100)
    _stop_scheduler_after_one_iteration(monkeypatch)

    row, active_order_id = _read_gate_artifacts(db_path)
    assert row == (
        "approve_confirmed", approve_hash, 8, approve_raw, None, 29, transfer_raw
    )
    assert active_order_id == "guarded-order"


def test_manual_terminal_status_keeps_approve_confirmed_raw_transfer(
    tmp_path, monkeypatch
):
    db_path = str(tmp_path / "manual-terminal-raw-transfer.sqlite3")
    monkeypatch.setattr(app, "DB_PATH", db_path)
    asyncio.run(app.init_db())
    approve_hash, approve_raw, transfer_raw = _seed_approve_confirmed_with_raw_transfer(
        db_path, expires=1_700_000_000
    )
    monkeypatch.setattr(app.time, "time", lambda: 1_700_000_100)

    async def final_status(*_args, **_kwargs):
        return next(iter(app.FINAL_FIXEDFLOAT_ORDER_STATUSES))

    monkeypatch.setattr(app, "get_fixedfloat_order_status_with_retry", final_status)
    asyncio.run(app.cmd_execute(_manual_execute_message()))

    row, active_order_id = _read_gate_artifacts(db_path)
    assert row == (
        "approve_confirmed", approve_hash, 8, approve_raw, None, 29, transfer_raw
    )
    assert active_order_id == "guarded-order"


def test_monitor_local_expiry_keeps_approve_confirmed_raw_transfer(
    tmp_path, monkeypatch
):
    db_path = str(tmp_path / "monitor-expired-raw-transfer.sqlite3")
    monkeypatch.setattr(app, "DB_PATH", db_path)
    asyncio.run(app.init_db())
    approve_hash, approve_raw, transfer_raw = _seed_approve_confirmed_with_raw_transfer(
        db_path, expires=1_700_000_000
    )
    monkeypatch.setattr(app.time, "time", lambda: 1_700_000_100)
    run_one_order_monitor_iteration(monkeypatch)

    row, active_order_id = _read_gate_artifacts(db_path)
    assert row == (
        "approve_confirmed", approve_hash, 8, approve_raw, None, 29, transfer_raw
    )
    assert active_order_id == "guarded-order"


def test_scheduler_terminal_legacy_approve_without_exact_intent_is_fail_closed(
    tmp_path, monkeypatch
):
    db_path = str(tmp_path / "scheduler-terminal-legacy-approve.sqlite3")
    monkeypatch.setattr(app, "DB_PATH", db_path)
    asyncio.run(app.init_db())
    approve_hash, approve_raw = _seed_confirmed_legacy_approve_without_exact_intent(
        db_path
    )
    monkeypatch.setattr(app.time, "time", lambda: 1_700_000_100)

    async def final_status(*_args, **_kwargs):
        return next(iter(app.FINAL_FIXEDFLOAT_ORDER_STATUSES))

    monkeypatch.setattr(app, "get_fixedfloat_order_status_with_retry", final_status)
    _stop_scheduler_after_one_iteration(monkeypatch)

    row, active_order_id = _read_gate_artifacts(db_path)
    assert row == ("approve_confirmed", approve_hash, 8, approve_raw, None, None, None)
    assert active_order_id == "guarded-order"


def test_scheduler_confirmed_legacy_approve_without_exact_intent_fails_closed(
    tmp_path, monkeypatch
):
    db_path = str(tmp_path / "scheduler-legacy-approve-no-exact.sqlite3")
    monkeypatch.setattr(app, "DB_PATH", db_path)
    asyncio.run(app.init_db())
    approve_hash, approve_raw = _seed_confirmed_legacy_approve_without_exact_intent(db_path)
    monkeypatch.setattr(app.time, "time", lambda: 1_700_000_100)

    async def active_status(*_args, **_kwargs):
        return "NEW"

    async def forbidden_resume(**_kwargs):
        raise AssertionError("legacy REAL must not reach signing")

    monkeypatch.setattr(app, "get_fixedfloat_order_status_with_retry", active_status)
    monkeypatch.setattr(app, "resume_transfer_after_approve", forbidden_resume)
    _stop_scheduler_after_one_iteration(monkeypatch)

    with sqlite3.connect(db_path) as db:
        row = db.execute(
            "SELECT state, approve_tx_hash, approve_raw_tx, transfer_tx_hash, transfer_raw_tx, "
            "amount_units, token_decimals, error_message FROM sent_transactions "
            "WHERE order_id = 'guarded-order'"
        ).fetchone()
    assert row[:7] == ("tx_pending", approve_hash, approve_raw, None, None, None, None)
    assert row[7].startswith("INVALID_PAYMENT_AMOUNT:")
    assert read_gate_state(db_path) == ("tx_pending", "guarded-order")


def test_manual_confirmed_legacy_approve_without_exact_intent_fails_closed(
    tmp_path, monkeypatch
):
    db_path = str(tmp_path / "manual-legacy-approve-no-exact.sqlite3")
    monkeypatch.setattr(app, "DB_PATH", db_path)
    asyncio.run(app.init_db())
    approve_hash, approve_raw = _seed_confirmed_legacy_approve_without_exact_intent(
        db_path, expires=1_700_000_000
    )
    monkeypatch.setattr(app.time, "time", lambda: 1_700_000_100)

    async def active_status(*_args, **_kwargs):
        return "NEW"

    async def forbidden_resume(**_kwargs):
        raise AssertionError("legacy REAL must not reach signing")

    monkeypatch.setattr(app, "get_fixedfloat_order_status_with_retry", active_status)
    monkeypatch.setattr(app, "resume_transfer_after_approve", forbidden_resume)
    asyncio.run(app.cmd_execute(_manual_execute_message()))

    with sqlite3.connect(db_path) as db:
        row = db.execute(
            "SELECT state, approve_tx_hash, approve_raw_tx, transfer_tx_hash, transfer_raw_tx, "
            "amount_units, token_decimals, error_message FROM sent_transactions "
            "WHERE order_id = 'guarded-order'"
        ).fetchone()
    assert row[:7] == ("tx_pending", approve_hash, approve_raw, None, None, None, None)
    assert row[7].startswith("INVALID_PAYMENT_AMOUNT:")
    assert read_gate_state(db_path) == ("tx_pending", "guarded-order")


def test_invalid_payment_amount_survives_scheduler_final_status_and_monitor_expiry(
    tmp_path, monkeypatch
):
    db_path = str(tmp_path / "legacy-invalid-amount-lifecycle.sqlite3")
    monkeypatch.setattr(app, "DB_PATH", db_path)
    asyncio.run(app.init_db())
    seed_active_order(db_path, state="tx_pending", expires=1_700_000_600)
    approve_raw = Web3.to_hex(b"lifecycle-legacy-approve")
    approve_hash = Web3.keccak(b"lifecycle-legacy-approve").hex()
    with sqlite3.connect(db_path) as db:
        db.execute(
            "UPDATE sent_transactions SET approve_tx_hash = ?, approve_tx_nonce = 8, approve_raw_tx = ? "
            "WHERE order_id = 'guarded-order'",
            (approve_hash, approve_raw),
        )

    async def confirmed_approve(_network_key, tx_hash):
        assert tx_hash == approve_hash
        return "confirmed"

    async def forbidden_resume(**_kwargs):
        raise AssertionError("legacy REAL must not reach signing")

    monkeypatch.setattr(app, "get_transfer_tx_status", confirmed_approve)
    monkeypatch.setattr(app, "resume_transfer_after_approve", forbidden_resume)
    asyncio.run(app.recovery_scan_pending_transactions())

    with sqlite3.connect(db_path) as db:
        after_recovery = db.execute(
            "SELECT state, approve_tx_hash, approve_raw_tx, error_message FROM sent_transactions "
            "WHERE order_id = 'guarded-order'"
        ).fetchone()
    assert after_recovery[:3] == ("tx_pending", approve_hash, approve_raw)
    assert after_recovery[3].startswith("INVALID_PAYMENT_AMOUNT:")

    current_time = [1_700_000_100]
    monkeypatch.setattr(app.time, "time", lambda: current_time[0])

    async def final_status(*_args, **_kwargs):
        return next(iter(app.FINAL_FIXEDFLOAT_ORDER_STATUSES))

    monkeypatch.setattr(app, "get_fixedfloat_order_status_with_retry", final_status)
    _stop_scheduler_after_one_iteration(monkeypatch)
    assert read_gate_state(db_path) == ("tx_pending", "guarded-order")

    current_time[0] = 1_700_000_700
    run_one_order_monitor_iteration(monkeypatch)

    with sqlite3.connect(db_path) as db:
        final_row = db.execute(
            "SELECT state, approve_tx_hash, approve_raw_tx, error_message FROM sent_transactions "
            "WHERE order_id = 'guarded-order'"
        ).fetchone()
    assert final_row == after_recovery
    assert read_gate_state(db_path) == ("tx_pending", "guarded-order")


def test_order_monitor_local_expiry_keeps_tx_pending_gate(tmp_path, monkeypatch):
    db_path = str(tmp_path / "monitor-local-expiry.sqlite3")
    monkeypatch.setattr(app, "DB_PATH", db_path)
    asyncio.run(app.init_db())
    seed_active_order(db_path, state="tx_pending", expires=1_700_000_000)
    monkeypatch.setattr(app.time, "time", lambda: 1_700_000_100)

    run_one_order_monitor_iteration(monkeypatch)
    assert read_gate_state(db_path) == ("tx_pending", "guarded-order")


def test_order_monitor_final_status_keeps_legacy_blocked_gate(tmp_path, monkeypatch):
    db_path = str(tmp_path / "monitor-final.sqlite3")
    monkeypatch.setattr(app, "DB_PATH", db_path)
    asyncio.run(app.init_db())
    seed_active_order(db_path, state="blocked", expires=1_700_000_600)
    monkeypatch.setattr(app.time, "time", lambda: 1_700_000_100)

    async def final_status(*_args, **_kwargs):
        return next(iter(app.FINAL_FIXEDFLOAT_ORDER_STATUSES))

    monkeypatch.setattr(app, "get_fixedfloat_order_status_with_retry", final_status)
    run_one_order_monitor_iteration(monkeypatch)
    assert read_gate_state(db_path) == ("blocked", "guarded-order")


@pytest.mark.parametrize("legacy_state", ["blocked", "transfering"])
def test_legacy_hashless_states_keep_gate_in_common_failure_helper(
    tmp_path, monkeypatch, legacy_state
):
    db_path = str(tmp_path / f"legacy-{legacy_state}.sqlite3")
    monkeypatch.setattr(app, "DB_PATH", db_path)
    asyncio.run(app.init_db())
    seed_active_order(db_path, state=legacy_state)

    cleared = asyncio.run(
        app.mark_order_failed(7, "guarded-order", "FixedFloat order expired")
    )
    assert cleared is False
    assert read_gate_state(db_path) == (legacy_state, "guarded-order")


def test_proven_single_persisted_transfer_revert_can_release_gate(tmp_path, monkeypatch):
    db_path = str(tmp_path / "proven-transfer-revert.sqlite3")
    monkeypatch.setattr(app, "DB_PATH", db_path)
    asyncio.run(app.init_db())
    raw_bytes = b"single-persisted-transfer"
    raw_tx = Web3.to_hex(raw_bytes)
    tx_hash = Web3.keccak(raw_bytes).hex()
    seed_active_order(
        db_path,
        state="tx_pending",
        transfer_hash=tx_hash,
        transfer_nonce=27,
        transfer_raw=raw_tx,
    )

    cleared = asyncio.run(
        app.mark_order_failed(
            7,
            "guarded-order",
            "Transfer tx reverted on-chain",
            proven_transfer_failure_hash=tx_hash,
        )
    )
    assert cleared is True
    assert read_gate_state(db_path) == ("failed", None)


def test_init_db_preserves_legacy_row_and_adds_nullable_intent_columns(tmp_path, monkeypatch):
    db_path = str(tmp_path / "legacy-intent-migration.sqlite3")
    with sqlite3.connect(db_path) as db:
        db.execute(
            "CREATE TABLE sent_transactions ("
            "id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, plan_id INTEGER, "
            "order_id TEXT NOT NULL, network_key TEXT NOT NULL, approve_tx_hash TEXT, "
            "transfer_tx_hash TEXT NOT NULL, amount REAL NOT NULL, deposit_address TEXT NOT NULL, "
            "state TEXT, error_message TEXT, sent_at INTEGER)"
        )
        db.execute(
            "INSERT INTO sent_transactions VALUES "
            "(1, 10001, NULL, 'legacy-order', 'USDT-ARB', '0xapprove', '0xtransfer', "
            "25.0, '0xdeposit', 'sent', NULL, 1700000000)"
        )

    monkeypatch.setattr(app, "DB_PATH", db_path)
    asyncio.run(app.init_db())
    asyncio.run(app.init_db())

    with sqlite3.connect(db_path) as db:
        columns = {
            row[1]: row for row in db.execute("PRAGMA table_info(sent_transactions)")
        }
        legacy_row = db.execute(
            "SELECT approve_tx_hash, transfer_tx_hash, approve_tx_nonce, approve_raw_tx, "
            "transfer_tx_nonce, transfer_raw_tx, amount_units, token_decimals "
            "FROM sent_transactions WHERE order_id = 'legacy-order'"
        ).fetchone()

    for column_name in (
        "approve_tx_nonce", "approve_raw_tx", "transfer_tx_nonce", "transfer_raw_tx",
        "amount_units", "token_decimals",
    ):
        assert column_name in columns
        assert columns[column_name][3] == 0
    assert legacy_row == (
        "0xapprove", "0xtransfer", None, None, None, None, None, None
    )


def test_stale_terminal_result_cannot_clear_new_active_order(tmp_path, monkeypatch):
    db_path = str(tmp_path / "stale-terminal.sqlite3")
    monkeypatch.setattr(app, "DB_PATH", db_path)
    asyncio.run(app.init_db())
    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT INTO dca_plans (id, user_id, active_order_id, active_order_token, active_order_expires) "
            "VALUES (7, 10001, 'order-b', 'token-b', 1900000000)"
        )
        db.execute(
            "INSERT INTO sent_transactions "
            "(user_id, plan_id, order_id, network_key, amount, deposit_address, state) "
            "VALUES (10001, 7, 'order-a', 'USDT-ARB', 25, '0xdeposit', 'sent')"
        )

    assert asyncio.run(app.mark_order_completed(7, "order-a", "late-success")) is False
    assert asyncio.run(
        app.mark_order_failed(7, "order-a", "late-failure", proven_pre_broadcast=True)
    ) is False
    with sqlite3.connect(db_path) as db:
        plan = db.execute(
            "SELECT active_order_id, active_order_token, active_order_expires FROM dca_plans WHERE id = 7"
        ).fetchone()
        state = db.execute(
            "SELECT state FROM sent_transactions WHERE order_id = 'order-a'"
        ).fetchone()[0]
    assert plan == ("order-b", "token-b", 1900000000)
    assert state == "sent"


def test_persistence_conflict_resume_stays_unresolved_and_preserves_intent(tmp_path, monkeypatch):
    db_path = str(tmp_path / "resume-conflict.sqlite3")
    monkeypatch.setattr(app, "DB_PATH", db_path)
    asyncio.run(app.init_db())
    raw = Web3.to_hex(b"owned-transfer-intent")
    tx_hash = Web3.keccak(b"owned-transfer-intent").hex()
    seed_active_order(
        db_path,
        state="transfering",
        expires=1_700_000_000,
        transfer_hash=tx_hash,
        transfer_nonce=33,
        transfer_raw=raw,
    )
    app._wallet_passwords[10001] = "unused"

    async def conflict_send(**_kwargs):
        return False, "0xapprove", None, "PERSISTENCE_CONFLICT:owned intent"

    monkeypatch.setattr(app, "auto_send_usdt", conflict_send)
    result = asyncio.run(
        app.resume_transfer_after_approve(
            network_key="USDT-ARB",
            user_id=10001,
            btc_address="bc1unused",
            order_id="guarded-order",
            deposit_address="0xdeposit",
            required_amount=25,
            existing_approve_tx="0xapprove",
            plan_id=7,
            order_expires=4_000_000_000,
        )
    )
    assert result[0] == "tx_pending"
    assert app.is_persistence_conflict_error(result[3])
    with sqlite3.connect(db_path) as db:
        db.execute(
            "UPDATE sent_transactions SET state = 'tx_pending', error_message = ? WHERE order_id = 'guarded-order'",
            (result[3],),
        )

    monkeypatch.setattr(app.time, "time", lambda: 1_700_000_100)

    async def final_status(*_args, **_kwargs):
        return next(iter(app.FINAL_FIXEDFLOAT_ORDER_STATUSES))

    async def stop_scheduler(*_args, **_kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(app, "get_fixedfloat_order_status_with_retry", final_status)
    monkeypatch.setattr(app.asyncio, "sleep", stop_scheduler)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(app.dca_scheduler())

    assert asyncio.run(app.notify_and_clear_expired_order(7, "guarded-order")) is False
    with sqlite3.connect(db_path) as db:
        row = db.execute(
            "SELECT state, transfer_tx_hash, transfer_tx_nonce, transfer_raw_tx FROM sent_transactions "
            "WHERE order_id = 'guarded-order'"
        ).fetchone()
    assert row == ("tx_pending", tx_hash, 33, raw)
    assert read_gate_state(db_path) == ("tx_pending", "guarded-order")


def test_approve_resume_claim_never_rolls_transfering_back(tmp_path, monkeypatch):
    db_path = str(tmp_path / "approve-resume-race.sqlite3")
    monkeypatch.setattr(app, "DB_PATH", db_path)
    asyncio.run(app.init_db())
    seed_active_order(db_path, state="approve_confirmed")
    approve_raw = Web3.to_hex(b"approve-intent")
    approve_hash = Web3.keccak(b"approve-intent").hex()
    with sqlite3.connect(db_path) as db:
        db.execute(
            "UPDATE sent_transactions SET approve_tx_hash = ?, approve_tx_nonce = 8, approve_raw_tx = ? "
            "WHERE order_id = 'guarded-order'",
            (approve_hash, approve_raw),
        )
        tx_id = db.execute(
            "SELECT id FROM sent_transactions WHERE order_id = 'guarded-order'"
        ).fetchone()[0]

    first_claim = asyncio.run(
        app.claim_transfer_after_approve(tx_id, 7, "guarded-order", approve_hash)
    )
    second_claim = asyncio.run(
        app.claim_transfer_after_approve(tx_id, 7, "guarded-order", approve_hash)
    )
    assert first_claim is True
    assert second_claim is False
    assert read_gate_state(db_path) == ("transfering", "guarded-order")


@pytest.mark.parametrize("state", ["blocked", "transfering", "tx_pending", "sent"])
def test_expired_before_send_refuses_unresolved_or_post_send_state(
    tmp_path, monkeypatch, state
):
    db_path = str(tmp_path / f"expiry-{state}.sqlite3")
    monkeypatch.setattr(app, "DB_PATH", db_path)
    asyncio.run(app.init_db())
    raw = Web3.to_hex(b"persisted-transfer") if state in ("tx_pending", "sent") else None
    tx_hash = Web3.keccak(b"persisted-transfer").hex() if raw else None
    seed_active_order(
        db_path,
        state=state,
        transfer_hash=tx_hash,
        transfer_nonce=19 if raw else None,
        transfer_raw=raw,
    )

    cleared = asyncio.run(
        app.mark_order_expired_before_send(
            plan_id=7,
            user_id=10001,
            order_id="guarded-order",
        )
    )
    assert cleared is False
    assert read_gate_state(db_path) == (state, "guarded-order")
