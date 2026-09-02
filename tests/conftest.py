import os
import sys
from pathlib import Path

import dotenv


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
