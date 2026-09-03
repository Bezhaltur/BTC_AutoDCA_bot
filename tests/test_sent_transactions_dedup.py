import asyncio
import sqlite3

import pytest

import bot as app


def test_init_db_deduplicates_sent_transactions_keeping_newest(tmp_path, monkeypatch):
    db_path = tmp_path / "dedup.sqlite3"
    monkeypatch.setattr(app, "DB_PATH", str(db_path))

    # Create the current schema first.
    asyncio.run(app.init_db())

    # Simulate a legacy database that already contains duplicate order_id rows.
    with sqlite3.connect(db_path) as db:
        db.execute("DROP INDEX idx_sent_transactions_order_id")

        db.execute(
            """
            INSERT INTO sent_transactions (
                user_id,
                order_id,
                network_key,
                transfer_tx_hash,
                amount,
                deposit_address,
                state
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                10001,
                "duplicate-order",
                "USDT-ARB",
                "old-tx",
                10.0,
                "old-address",
                "scheduled",
            ),
        )

        db.execute(
            """
            INSERT INTO sent_transactions (
                user_id,
                order_id,
                network_key,
                transfer_tx_hash,
                amount,
                deposit_address,
                state
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                10001,
                "duplicate-order",
                "USDT-ARB",
                "new-tx",
                20.0,
                "new-address",
                "sent",
            ),
        )
        db.commit()

    # Startup migration must deduplicate before recreating the unique index.
    asyncio.run(app.init_db())

    with sqlite3.connect(db_path) as db:
        rows = db.execute(
            """
            SELECT id, transfer_tx_hash, amount, deposit_address, state
            FROM sent_transactions
            WHERE order_id = ?
            ORDER BY id
            """,
            ("duplicate-order",),
        ).fetchall()

        assert len(rows) == 1
        assert rows[0][1] == "new-tx"
        assert rows[0][2] == 20.0
        assert rows[0][3] == "new-address"
        assert rows[0][4] == "sent"

        index = db.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'index'
              AND name = 'idx_sent_transactions_order_id'
            """
        ).fetchone()

        assert index is not None

        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                """
                INSERT INTO sent_transactions (
                    user_id,
                    order_id,
                    network_key,
                    amount,
                    deposit_address
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    10001,
                    "duplicate-order",
                    "USDT-ARB",
                    30.0,
                    "another-address",
                ),
            )
