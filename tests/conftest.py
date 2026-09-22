import os
import sys
import logging
import socket
from pathlib import Path
from unittest.mock import patch

import dotenv
import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Import bot.py with inert, test-only configuration.  In particular, do not
# load either the developer's .env or credentials from another checkout.
dotenv.load_dotenv = lambda *args, **kwargs: False
os.environ["ADMIN_USER_ID"] = "10001"
os.environ["DCA_TELEGRAM_BOT_TOKEN"] = (
    "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk"
)
os.environ["DATABASE_PATH"] = "/tmp/bitcoin-auto-dca-unused.sqlite3"
os.environ["DCA_CONFIRMATION_TIMEOUT_SECONDS"] = "600"

# Import-time logging/directory setup must not create runtime files in DEV.
with patch("logging.FileHandler", return_value=logging.NullHandler()), patch("os.makedirs"):
    import bot  # noqa: E402,F401


@pytest.fixture(autouse=True)
def no_external_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Real network access is forbidden in tests")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
