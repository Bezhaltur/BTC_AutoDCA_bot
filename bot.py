import asyncio
import atexit
import copy
import html
import logging
import os
import platform
import sys
import hmac
import hashlib
import json
import math
import time
import re
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, Optional

MIN_PYTHON_VERSION = (3, 9)
if sys.version_info < MIN_PYTHON_VERSION:
    raise RuntimeError(
        f"Unsupported Python version: {sys.version_info.major}.{sys.version_info.minor}. "
        f"Use Python {MIN_PYTHON_VERSION[0]}.{MIN_PYTHON_VERSION[1]}+"
    )

try:
    import requests
except ModuleNotFoundError as exc:
    raise RuntimeError("Missing dependency 'requests'. Install requirements: pip install -r requirements.txt") from exc

try:
    from dotenv import load_dotenv
except ModuleNotFoundError as exc:
    raise RuntimeError("Missing dependency 'python-dotenv'. Install requirements: pip install -r requirements.txt") from exc

try:
    import aiosqlite
except ModuleNotFoundError as exc:
    raise RuntimeError("Missing dependency 'aiosqlite'. Install requirements: pip install -r requirements.txt") from exc

try:
    from aiogram import Bot, Dispatcher, BaseMiddleware
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode
    from aiogram.filters import Command
    from aiogram.types import BotCommand, CallbackQuery, Message
    from aiogram.fsm.storage.memory import MemoryStorage
except ModuleNotFoundError as exc:
    raise RuntimeError("Missing dependency 'aiogram'. Install requirements: pip install -r requirements.txt") from exc

try:
    from web3.exceptions import TransactionNotFound
except ModuleNotFoundError as exc:
    raise RuntimeError("Missing dependency 'web3'. Install requirements: pip install -r requirements.txt") from exc
from networks import get_network_config, get_blockchair_url
from wallet import (
    save_keystore, load_keystore,
    delete_keystore, get_wallet_address,
    save_password_to_keyring, load_password_from_keyring,
    delete_password_from_keyring, keystore_exists, KEYSTORE_DIR
)
from auto_send import auto_send_usdt
from erc20 import (
    get_web3_instance,
    get_usdt_balance,
    get_native_balance,
)

try:
    import fcntl  # Unix file lock support
    HAS_FCNTL = True
except ImportError:
    fcntl = None
    HAS_FCNTL = False

# ============================================================================
# НАСТРОЙКА И КОНФИГУРАЦИЯ
# ============================================================================

# Настройка логирования - все операции бота логируются в файл и консоль
# Создаём директорию для логов если её нет
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOGS_DIR = os.path.join(BASE_DIR, "logs")
DEFAULT_LOG_FILE = os.path.join(LOGS_DIR, "bot.log")
DEFAULT_LAST_SEEN_EXECUTION_FILE = os.path.join(LOGS_DIR, "last_seen_execution.txt")
DEFAULT_DB_PATH = os.path.join(BASE_DIR, "dca.db")
LOCK_FILE = os.path.join(LOGS_DIR, "bot.lock")
DEFAULT_LOCK_FILE = LOCK_FILE

os.makedirs(LOGS_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(DEFAULT_LOG_FILE, encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Загрузка переменных окружения из .env файла
load_dotenv(dotenv_path=os.path.join(BASE_DIR, ".env"), override=True)


def resolve_project_path(path_value: str, default_path: str) -> str:
    """Resolve env-provided path relative to project root when needed."""
    selected = path_value or default_path
    if not os.path.isabs(selected):
        selected = os.path.join(BASE_DIR, selected)
    return os.path.abspath(selected)


def is_process_alive(pid: int) -> bool:
    """Cross-platform process existence check."""
    if pid <= 0:
        return False
    if platform.system() == "Windows":
        import subprocess
        try:
            output = subprocess.check_output(
                ["tasklist", "/FI", f"PID eq {pid}"],
                stderr=subprocess.DEVNULL
            ).decode(errors="ignore")
            return re.search(rf"\b{pid}\b", output) is not None
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False

ADMIN_USER_ID_RAW = os.getenv("ADMIN_USER_ID")
if not ADMIN_USER_ID_RAW:
    raise RuntimeError("Missing ADMIN_USER_ID in .env")
try:
    ADMIN_USER_ID = int(ADMIN_USER_ID_RAW)
    if ADMIN_USER_ID <= 0:
        raise ValueError
except ValueError as exc:
    raise RuntimeError("Invalid ADMIN_USER_ID in .env. Expected a positive integer") from exc

_instance_lock_file = None
_instance_lock_path = None

# API ключи для FixedFloat (сервис обмена криптовалют)
FF_API_KEY = os.getenv("FF_API_KEY")
FF_API_SECRET = os.getenv("FF_API_SECRET")
FF_API_URL = "https://ff.io/api/v2"  # базовый URL API FixedFloat

# Import test configuration (optional)
try:
    from test_config import (
        DRY_RUN, MOCK_FIXEDFLOAT, USE_TESTNET, is_test_mode,
        get_mock_fixedfloat_order, get_mock_fixedfloat_ccies, get_mock_fixedfloat_price,
        mask_sensitive_data
    )
except ImportError:
    DRY_RUN = False
    MOCK_FIXEDFLOAT = False
    USE_TESTNET = False

    def is_test_mode() -> bool:
        return False

    def get_mock_fixedfloat_order(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("MOCK_FIXEDFLOAT disabled: test_config.py not found")

    def get_mock_fixedfloat_ccies(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("MOCK_FIXEDFLOAT disabled: test_config.py not found")

    def get_mock_fixedfloat_price(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("MOCK_FIXEDFLOAT disabled: test_config.py not found")

    def mask_sensitive_data(data):  # type: ignore[no-untyped-def]
        return data

# In-memory password cache (loaded from keyring at startup)
# Keys: user_id -> password
# This is ONLY a cache - keyring is the single source of truth
_wallet_passwords = {}
_order_progress_messages: Dict[str, tuple[int, int]] = {}
_user_plan_mapping: Dict[int, Dict[int, int]] = {}
_web3_cache: Dict[str, Any] = {}
_balances_cache: Dict[tuple[str, str], Dict[str, Any]] = {}
_fixedfloat_limits_cache: Dict[str, Dict[str, Any]] = {}
CACHE_TTL = 20
LIMITS_CACHE_TTL = 300

# Runtime-копия, может обновляться по данным API на старте
NETWORK_CODES = {
    "USDT-ARB": "USDTARBITRUM",
    "USDT-BSC": "USDTBSC",
    "USDT-POLYGON": "USDTMATIC",
}

FIXEDFLOAT_ASSET_MAP = {
    "USDT-ARB": "USDTARB",
    "USDT-BSC": "USDTBSC",
    "USDT-POLYGON": "USDTMATIC",
}

RETRYABLE_ERROR_KEYWORDS = (
    "timeout",
    "timed out",
    "connection",
    "rpc",
    "5xx",
    "unavailable",
    "failed to connect",
    "name resolution",
    "temporarily unavailable",
    "max retries exceeded",
    "nodename nor servname provided",
)
FINAL_FIXEDFLOAT_ORDER_STATUSES = {"expired", "cancelled", "failed"}
SUCCESS_FIXEDFLOAT_ORDER_STATUSES = {"finished", "completed", "done"}
WALLETSTATUS_MIN_NATIVE_GAS = {
    "USDT-ARB": 0.0001,
    "USDT-BSC": 0.0005,
    "USDT-POLYGON": 0.05,
}

try:
    DCA_EXECUTION_WINDOW_SECONDS = int(os.getenv("DCA_EXECUTION_WINDOW_SECONDS", "300"))
except ValueError:
    DCA_EXECUTION_WINDOW_SECONDS = 300
if DCA_EXECUTION_WINDOW_SECONDS < 0:
    DCA_EXECUTION_WINDOW_SECONDS = 0
LAST_SEEN_EXECUTION_FILE = resolve_project_path(
    os.getenv("LAST_SEEN_EXECUTION_FILE", ""),
    DEFAULT_LAST_SEEN_EXECUTION_FILE
)
 


# ============================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================================

def format_interval(hours: int) -> str:
    """
    Преобразует интервал в часах в читаемый формат.
    Используется в нескольких местах для единообразия.
    """
    if hours == 12:
        return "12 часов"
    elif hours == 24:
        return "день"
    elif hours == 168:
        return "неделю"
    elif hours == 720:
        return "месяц"
    else:
        return f"{hours}ч"


def escape_html(text: str) -> str:
    import html
    if text is None:
        return ""
    return html.escape(str(text)) if text else text


def format_balance(value: float) -> str:
    if value is None:
        return "—"
    return f"{float(value):.2f}"


def format_native_balance(value: float) -> str:
    if value is None:
        return "—"
    return f"{float(value):.6f}"


def format_amount(x: float) -> str:
    if x is None:
        return "0.00"
    value = float(x)
    return f"{value:.2f}"


def normalize_network_key(value: str) -> str:
    normalized = str(value or "").upper()
    if normalized == "USDT-MATIC":
        return "USDT-POLYGON"
    return normalized


def get_network_label(value: str) -> str:
    normalized_value = normalize_network_key(value)
    code_to_label = {
        "USDTARB": "Arbitrum",
        "USDTARBITRUM": "Arbitrum",
        "USDT-BSC": "BSC",
        "USDTBSC": "BSC",
        "USDT-POLYGON": "Polygon",
        "USDTMATIC": "Polygon",
        "USDTPOLYGON": "Polygon",
    }
    if normalized_value in code_to_label:
        return code_to_label[normalized_value]
    try:
        return get_network_config(normalized_value)["name"]
    except Exception:
        return ""


def format_order_amount(amount: Any, token: str = "USDT", network: str = "", network_key: str = "") -> str:
    amount_raw = amount
    token_raw = token
    if not token_raw and isinstance(amount, str):
        parts = str(amount).strip().split()
        if len(parts) >= 2:
            amount_raw = parts[0]
            token_raw = parts[1]
    elif token_raw == "USDT" and isinstance(amount, str):
        parts = str(amount).strip().split()
        if len(parts) >= 2:
            amount_raw = parts[0]
            token_raw = parts[1]
    try:
        amount_number = float(amount_raw)
    except (TypeError, ValueError):
        return escape_html(amount)

    amount_text = format_amount(amount_number)
    network_label = get_network_label(network) or get_network_label(network_key) or get_network_label(token_raw)
    token_normalized = normalize_network_key(str(token_raw or "").upper())
    token_display = "USDT" if token_normalized.startswith("USDT") else (token_raw or "USDT")
    safe_network_label = escape_html(network_label) if network_label else ""
    safe_token = escape_html(token_display)
    if network_label:
        return f"{amount_text} {safe_token} ({safe_network_label})"
    if token_display:
        return f"{amount_text} {safe_token}"
    return amount_text


def format_order_link(order_id: Any) -> str:
    safe_order_id = escape_html(order_id)
    return f'<a href="https://fixedfloat.com/order/{safe_order_id}">{safe_order_id}</a>'


def format_code_address(address: Any) -> str:
    safe_address = escape_html(address)
    return f"<code>{safe_address}</code>" if safe_address else "<code>—</code>"


NOTIFICATION_REASONS = {
    "offline": "Бот был офлайн слишком долго",
    "window_expired": "Покупка не была выполнена вовремя",
    "insufficient": "Недостаточно средств",
    "order_expired": "Оплата не была получена вовремя",
}


def get_notification_reason(reason_code: str) -> str:
    return NOTIFICATION_REASONS.get(reason_code, "Неизвестная причина")


def format_notification_amount(amount: Any) -> str:
    amount_raw = amount
    if isinstance(amount, str):
        parts = amount.strip().split()
        if parts:
            amount_raw = parts[0]
    try:
        return f"{float(amount_raw):.2f}"
    except (TypeError, ValueError):
        return escape_html(amount)


def format_time_until(target_ts: Optional[int], now_ts: Optional[int] = None) -> str:
    if now_ts is None:
        now_ts = int(time.time())
    if not target_ts:
        return "—"
    remaining = max(0, int(target_ts) - int(now_ts))
    minutes_left = math.ceil(remaining / 60)
    if minutes_left < 60:
        return f"~{minutes_left} мин"
    hours_left, rest_minutes = divmod(minutes_left, 60)
    if rest_minutes == 0:
        return f"~{hours_left} ч"
    return f"~{hours_left} ч {rest_minutes} мин"


def format_datetime_utc(ts: Optional[int]) -> str:
    if not ts:
        return "—"
    months = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]
    dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
    return f"{dt.day} {months[dt.month - 1]} {dt.hour:02d}:{dt.minute:02d} UTC"


def format_order_deadline(expires_at: Optional[int], now_ts: Optional[int] = None) -> str:
    absolute_deadline = format_datetime_utc(expires_at)
    countdown = format_time_until(expires_at, now_ts)
    if absolute_deadline == "—":
        return countdown
    if countdown == "—":
        return absolute_deadline
    return f"{absolute_deadline} ({countdown})"


def format_next_buy_time(ts: Optional[int], now_ts: Optional[int] = None) -> str:
    absolute_time = format_datetime_utc(ts)
    countdown = format_time_until(ts, now_ts)
    if absolute_time == "—":
        return countdown
    if countdown == "—":
        return absolute_time
    return f"{absolute_time} ({countdown})"


def extract_order_expires_at(order_data: dict, fallback_now: Optional[int] = None) -> int:
    if fallback_now is None:
        fallback_now = int(time.time())

    candidates = [
        order_data.get("expires_at"),
        order_data.get("expiresAt"),
        order_data.get("expire"),
        order_data.get("expires"),
        (order_data.get("time", {}) or {}).get("expiration"),
        (order_data.get("time", {}) or {}).get("expires_at"),
        (order_data.get("time", {}) or {}).get("expiresAt"),
        (order_data.get("time", {}) or {}).get("expire"),
    ]
    for candidate in candidates:
        try:
            expires_at = int(float(candidate))
        except (TypeError, ValueError):
            continue
        if expires_at > 0:
            return expires_at

    time_left = (order_data.get("time", {}) or {}).get("left", 0)
    if not isinstance(time_left, (int, float)) or time_left < 0:
        time_left = 0
    return int(fallback_now) + int(time_left)


def normalize_code(value: str) -> str:
    if not value:
        return ""
    return str(value).replace("-", "").replace("_", "").upper()


def get_fixedfloat_symbol(user_symbol: str) -> str:
    normalized_symbol = normalize_network_key(user_symbol)
    network_map = {
        "USDT-ARB": "USDTARBITRUM",
        "USDT-BSC": "USDTBSC",
        "USDT-POLYGON": "USDTMATIC",
    }
    return network_map.get(normalized_symbol) or NETWORK_CODES.get(normalized_symbol, "")


def validate_btc_address(address: str) -> bool:
    """
    Валидация Bitcoin адреса (Legacy, SegWit, Native SegWit).
    Поддерживает Legacy, SegWit и Native SegWit.
    """
    if not address:
        return False
    
    # Legacy (P2PKH) - начинается с 1
    legacy_pattern = r'^[1][a-km-zA-HJ-NP-Z1-9]{25,34}$'
    # SegWit (P2SH) - начинается с 3
    segwit_pattern = r'^[3][a-km-zA-HJ-NP-Z1-9]{25,34}$'
    # Native SegWit (Bech32) - начинается с bc1
    bech32_pattern = r'^(bc1)[a-z0-9]{39,87}$'
    
    return bool(
        re.match(legacy_pattern, address) or 
        re.match(segwit_pattern, address) or 
        re.match(bech32_pattern, address)
    )


def is_retryable_network_error(error_msg: str) -> bool:
    """True if error looks like temporary network/RPC issue."""
    lower = (error_msg or "").lower()
    return any(keyword in lower for keyword in RETRYABLE_ERROR_KEYWORDS)


def is_pending_tx_error(error_msg: str) -> bool:
    """True if auto-send returned pending tx marker."""
    msg = (error_msg or "")
    return msg.startswith("TX_PENDING:") or msg.startswith("APPROVE_TX_PENDING:")


def is_insufficient_auto_send_error(error_msg: str) -> bool:
    """True if auto-send failed because wallet funds were insufficient."""
    lower = (error_msg or "").lower()
    return (
        "insufficient usdt balance" in lower
        or "balance for gas" in lower
        or "insufficient funds for gas" in lower
    )


def _extract_amount_from_error(error_msg: str, label: str, asset: str) -> str:
    """Extract amount from lines like 'Required: 1.234000 USDT'."""
    pattern = rf"{label}:\s*([0-9]+(?:\.[0-9]+)?)\s*{re.escape(asset)}"
    match = re.search(pattern, error_msg, flags=re.IGNORECASE)
    return format_notification_amount(match.group(1)) if match else ""


def humanize_auto_send_error(error_msg: str, network_key: str) -> str:
    """Convert low-level auto-send error into user-friendly Russian text."""
    raw = (error_msg or "").strip()
    if not raw:
        return "Неизвестная ошибка авто-отправки."

    lower = raw.lower()
    native_token = get_network_config(network_key)["native_token"]

    if "insufficient usdt balance" in lower:
        return get_notification_reason("insufficient")

    if "balance for gas" in lower or "insufficient funds for gas" in lower:
        return get_notification_reason("insufficient")

    if "wallet not configured" in lower:
        return "Кошелек не настроен. Выполни /setwallet."

    if "incorrect wallet password" in lower:
        return "Не удалось расшифровать кошелек. Проверь пароль и выполни /setwallet заново."

    if "invalid private key format" in lower:
        return "Поврежден формат ключа в keystore. Настрой кошелек заново через /setwallet."

    if (
        "non-hexadecimal digit found" in lower
        or "when sending a str, it must be a hex string" in lower
        or "invalid deposit address format" in lower
    ):
        return f"Неверный формат адреса для сети {network_key}."

    if is_retryable_network_error(raw):
        return f"Временная ошибка сети {network_key}."

    short_raw = raw[:180]
    return f"Техническая ошибка авто-отправки в сети {network_key}. Детали: {short_raw}"


def build_auto_send_failed_notification(
    order_id: str,
    plan_number: Optional[int],
    network_key: str,
    required_amount: float,
    btc_address: str,
    deposit_address: str,
    order_expires: Optional[int],
    error_msg: str,
) -> str:
    """Build clear fallback message when auto-send fails."""
    human_error = humanize_auto_send_error(error_msg, network_key)
    is_insufficient = "Недостаточно средств" in human_error
    status_title = "⚠️ Недостаточно средств" if is_insufficient else "⚠️ Требуется оплата"
    return build_order_state_message(
        status_title=status_title,
        plan_number=plan_number,
        order_id=order_id,
        amount=required_amount,
        network_key=network_key,
        btc_address=btc_address,
        payment_address=deposit_address,
        deadline_text=format_order_deadline(order_expires),
        reason_text="" if is_insufficient else human_error,
    )


def format_scheduled_time(ts: int) -> str:
    """Format Unix timestamp for user-facing notifications."""
    return format_datetime_utc(ts)


def format_btc_recipient_copyable(address: Any) -> str:
    """Show the full BTC recipient on a copy-friendly line."""
    full_address = str(address or "").strip()
    if not full_address:
        return "<code>—</code>"
    return format_code_address(full_address)


def format_txid_short(txid: Any) -> str:
    text = str(txid or "").strip()
    if len(text) <= 16:
        return text or "TX"
    return f"{text[:10]}...{text[-8:]}"


def build_status_hint() -> str:
    return "📊 Статус планов: /status"


def build_execute_hint(command: str, label: str = "▶️ Выполнить сейчас:") -> str:
    return f"{label}\n{escape_html(command)}"


def build_plan_context(
    *,
    plan_number: Optional[int],
    amount: Any,
    network_key: str,
    btc_address: str,
) -> str:
    network_label = escape_html(get_network_label(network_key) or network_key or "—")
    lines = []
    if plan_number is not None:
        lines.append(f"План: #{int(plan_number)}")
    lines.extend([
        f"💰 Сумма: {format_notification_amount(amount)} USDT",
        f"🌐 Сеть: {network_label}",
        f"📥 Получатель BTC:\n{format_btc_recipient_copyable(btc_address)}",
    ])
    return "\n".join(lines)


def build_order_state_message(
    *,
    status_title: str,
    plan_number: Optional[int],
    order_id: Optional[str],
    amount: Any,
    network_key: str,
    btc_address: str,
    reason_text: str,
    deadline_text: str = "",
    scheduled_text: str = "",
    payment_address: str = "",
    extra_lines: Optional[list[str]] = None,
) -> str:
    lines = [status_title, ""]
    if order_id:
        lines.append(f"🔗 Ордер: {format_order_link(order_id)}")
        lines.append("")
    lines.append(build_plan_context(
        plan_number=plan_number,
        amount=amount,
        network_key=network_key,
        btc_address=btc_address,
    ))
    if payment_address:
        payment_network = escape_html(get_network_label(network_key) or network_key or "—")
        lines.extend([
            "",
            f"📍 Адрес для оплаты USDT ({payment_network}):",
            format_code_address(payment_address),
        ])
    if scheduled_text:
        lines.extend(["", f"📅 Планировалось:\n{escape_html(scheduled_text)}"])
    if deadline_text:
        lines.extend(["", f"⏰ Срок оплаты:\n{escape_html(deadline_text)}"])
    if reason_text:
        lines.extend(["", f"Причина:\n{escape_html(reason_text)}"])
    if extra_lines:
        lines.extend(["", *extra_lines])
    if build_status_hint() not in lines:
        lines.extend(["", build_status_hint()])
    return "\n".join(lines)


def calculate_next_run_preserving_schedule(scheduled_time: int, interval_hours: int, now_ts: int) -> int:
    """
    Calculate next run without shifting strategy schedule.
    Always advances from the original scheduled slot, not from current time.
    """
    try:
        interval_seconds = max(1, int(interval_hours) * 3600)
    except (TypeError, ValueError):
        interval_seconds = 24 * 3600
    next_run = int(scheduled_time) + interval_seconds
    if next_run <= now_ts:
        missed_intervals = ((now_ts - next_run) // interval_seconds) + 1
        next_run += missed_intervals * interval_seconds
    return next_run


def is_order_expired(order_expires: Optional[int], now_ts: Optional[int] = None) -> bool:
    """True if order expiry is missing or already passed."""
    if now_ts is None:
        now_ts = int(time.time())
    if not order_expires:
        return True
    return int(now_ts) >= int(order_expires)


async def get_execute_command_hint(user_id: int, plan_id: int) -> str:
    """Return a user-facing execute command without exposing the database ID."""
    for display_index, mapped_plan_id in _user_plan_mapping.get(int(user_id), {}).items():
        if int(mapped_plan_id) == int(plan_id):
            return f"/execute_{int(display_index)}"
    plan_number = await get_plan_display_number(int(user_id), int(plan_id))
    return f"/execute_{int(plan_number)}" if plan_number is not None else "/status"


async def get_plan_display_number(user_id: int, plan_id: int) -> Optional[int]:
    """Return a user-facing plan number without exposing the database ID."""
    for display_index, mapped_plan_id in _user_plan_mapping.get(int(user_id), {}).items():
        if int(mapped_plan_id) == int(plan_id):
            return int(display_index)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id FROM dca_plans WHERE user_id = ? AND deleted = 0 ORDER BY id",
            (user_id,)
        ) as cur:
            rows = await cur.fetchall()
    for idx, row in enumerate(rows, start=1):
        if row[0] == plan_id:
            return idx
    return None


async def resolve_display_plan_id(message: Message, display_index: int) -> Optional[int]:
    """Resolve a user-facing plan index created by the latest /status call."""
    user_id = int(message.from_user.id)
    plan_id = _user_plan_mapping.get(user_id, {}).get(int(display_index))
    if plan_id is not None:
        return int(plan_id)
    await message.answer(
        "<b>⚠️ Сведения о планах обновились. Пожалуйста, проверьте актуальное состояние через /status</b>",
        parse_mode="HTML",
    )
    return None


def build_missed_dca_cycle_notification(
    *,
    plan_number: int,
    network_key: str,
    amount: Any,
    btc_address: str,
    scheduled_time: int,
    execute_command: str,
    reason_code: str = "window_expired",
) -> str:
    """Notification text for skipped missed cycle."""
    reason_text = get_notification_reason(reason_code)
    return build_order_state_message(
        status_title="⚠️ Покупка пропущена",
        plan_number=plan_number,
        order_id=None,
        amount=amount,
        network_key=network_key,
        btc_address=btc_address,
        scheduled_text=format_scheduled_time(scheduled_time),
        reason_text=reason_text,
    )


def build_order_expired_notification(
    *,
    order_id: str,
    plan_number: Optional[int],
    network_key: str,
    amount: Any,
    btc_address: str,
    execute_command: str,
) -> str:
    """Notification text when order expired before send attempt."""
    network_label = escape_html(get_network_label(network_key) or network_key or "—")
    return (
        "❌ <b>Время для оплаты истекло</b>\n\n"
        f"🔗 Ордер: {format_order_link(order_id)}\n"
        f"🌐 Сеть: {network_label}\n"
        f"📥 Получатель BTC:\n{format_btc_recipient_copyable(btc_address)}\n"
        f"💰 Сумма: {format_notification_amount(amount)} USDT\n\n"
        "Оплата не поступила. Ордер автоматически отменён.\n\n"
        "⚠️ <b>НЕ ОТПРАВЛЯЙТЕ средства на ранее выданный адрес оплаты!</b>\n\n"
        f"{build_execute_hint(execute_command, '▶️ Создать новый ордер по этому плану:')}"
    )


def build_order_payment_notification(
    *,
    order_id: str,
    plan_number: Optional[int],
    network_key: str,
    amount: Any,
    btc_address: str,
    deposit_address: str,
    order_expires: Optional[int],
    action_text: str,
) -> str:
    """Notification text for active order payment instructions."""
    extra_lines = [escape_html(action_text)] if action_text else None
    return build_order_state_message(
        status_title="⏳ Ожидает оплаты",
        plan_number=plan_number,
        order_id=order_id,
        amount=amount,
        network_key=network_key,
        btc_address=btc_address,
        payment_address=deposit_address,
        deadline_text=format_order_deadline(order_expires),
        reason_text="Ордер создан и ожидает оплату.",
        extra_lines=extra_lines,
    )


def build_purchase_success_notification(
    *,
    order_id: str,
    plan_number: Optional[int],
    network_key: str,
    amount: Any,
    btc_address: str,
    btc_txid: str,
    btc_amount: str,
) -> str:
    return build_order_state_message(
        status_title="✅ BTC получен",
        plan_number=plan_number,
        order_id=order_id,
        amount=amount,
        network_key=network_key,
        btc_address=btc_address,
        reason_text="Выплата BTC подтверждена.",
        extra_lines=[
            f"₿ Получено:\n{escape_html(btc_amount or '—')}",
            f'🔗 BTC транзакция:\n<a href="{escape_html(get_blockchair_url(btc_txid))}">{escape_html(format_txid_short(btc_txid))}</a>',
            "Статус:\nBitcoin транзакция подтверждена сетью.",
        ],
    )


def build_btc_tx_sent_notification(
    *,
    order_id: str,
    plan_number: Optional[int],
    network_key: str,
    amount: Any,
    btc_address: str,
    btc_txid: str,
) -> str:
    return build_order_state_message(
        status_title="⏳ BTC транзакция отправлена",
        plan_number=plan_number,
        order_id=order_id,
        amount=amount,
        network_key=network_key,
        btc_address=btc_address,
        reason_text="Транзакция обнаружена в сети Bitcoin.\nОжидается подтверждение.",
        extra_lines=[f'🔗 BTC транзакция:\n<a href="{escape_html(get_blockchair_url(btc_txid))}">{escape_html(format_txid_short(btc_txid))}</a>'],
    )


def build_payout_review_notification(
    *,
    order_id: str,
    plan_number: Optional[int],
    network_key: str,
    amount: Any,
    btc_address: str,
) -> str:
    return build_order_state_message(
        status_title="⚠️ Требуется проверка выплаты",
        plan_number=plan_number,
        order_id=order_id,
        amount=amount,
        network_key=network_key,
        btc_address=btc_address,
        reason_text="Статус выплаты BTC пока не удалось подтвердить автоматически.",
    )


def build_requote_notification(
    *,
    order_id: str,
    plan_number: Optional[int],
    network_key: str,
    amount: Any,
    btc_address: str,
) -> str:
    return build_order_state_message(
        status_title="⚠️ Ордер требует подтверждения нового курса",
        plan_number=plan_number,
        order_id=order_id,
        amount=amount,
        network_key=network_key,
        btc_address=btc_address,
        reason_text=(
            "FixedFloat сообщил,\n"
            "что первоначальный курс истёк.\n\n"
            "Для продолжения обмена\n"
            "требуется подтверждение нового курса."
        ),
    )


def build_offline_startup_notification(items: list[dict]) -> str:
    grouped: Dict[int, dict] = {}
    for item in items:
        key = int(item["plan_id"])
        cycle_count = max(1, int(item.get("cycle_count", 1)))
        existing = grouped.setdefault(key, {**item, "cycle_count": 0, "times": []})
        existing["cycle_count"] += cycle_count
        existing["times"].append(int(item["scheduled_time"]))

    total_missed = sum(item["cycle_count"] for item in grouped.values())
    lines = [
        "⚠️ Бот был офлайн слишком долго",
        "",
        f"Пропущено покупок: {total_missed}",
        "",
        "Что затронуто:",
        "",
    ]
    grouped_items = list(grouped.values())
    max_items = 5
    shown_items = grouped_items[:max_items]
    hidden_count = max(0, len(grouped_items) - max_items)
    for item in shown_items:
        network_label = get_network_label(item["network_key"]) or item["network_key"]
        reason_text = get_notification_reason(item["reason_code"])
        start_time = min(item["times"])
        end_time = max(item["times"])
        lines.extend([
            f"• План #{item['plan_number']}",
            f"  Сумма: {format_notification_amount(item['amount'])} USDT",
            f"  Сеть: {escape_html(network_label)}",
            f"  Получатель BTC:\n  {format_btc_recipient_copyable(item.get('btc_address'))}",
            f"  Пропущено: {item['cycle_count']} раз",
            f"Период: {format_scheduled_time(start_time)} → {format_scheduled_time(end_time)}",
            f"Причина: {escape_html(reason_text)}",
            "",
        ])
    if hidden_count:
        lines.extend([
            f"… ещё планов: {hidden_count}",
            "",
        ])
    lines.extend(["", build_status_hint()])
    return "\n".join(lines)


async def record_plan_skip_metadata(plan_id: int, reason_code: str, missed_at: Optional[int] = None) -> None:
    """Persist skip metadata without changing scheduler ownership of execution_state."""
    if missed_at is None:
        missed_at = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE dca_plans SET skip_reason = COALESCE(skip_reason, ?), "
            "missed_count = COALESCE(missed_count, 0) + 1, last_missed_at = ?, "
            "last_execution_attempt_at = ? "
            "WHERE id = ?",
            (reason_code, int(missed_at), int(missed_at), plan_id)
        )
        await db.commit()


async def skip_missed_dca_cycle(
    *,
    plan_id: int,
    user_id: int,
    scheduled_time: int,
    interval_hours: int,
    reason_code: str = "window_expired",
) -> None:
    """Mark overdue cycle as skipped and notify user."""
    now_ts = int(time.time())
    new_next_run = calculate_next_run_preserving_schedule(scheduled_time, interval_hours, now_ts)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE dca_plans SET next_run = ?, execution_state = 'skipped', "
            "skip_notified = 0, skip_reason = COALESCE(skip_reason, ?), "
            "missed_count = COALESCE(missed_count, 0) + 1, last_missed_at = ?, "
            "last_execution_attempt_at = ? "
            "WHERE id = ?",
            (new_next_run, reason_code, now_ts, now_ts, plan_id)
        )
        await db.commit()
    logger.warning(
        "DCA цикл пропущен из-за пропущенного времени исполнения: plan_id=%s, scheduled_time=%s, now=%s",
        plan_id, scheduled_time, now_ts
    )
    execute_command = await get_execute_command_hint(user_id, plan_id)
    plan_number = await get_plan_display_number(user_id, plan_id)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT from_asset, amount, btc_address FROM dca_plans WHERE id = ?",
            (plan_id,)
        ) as cur:
            plan_details = await cur.fetchone()
    network_key = plan_details[0] if plan_details else ""
    amount = plan_details[1] if plan_details else 0
    btc_address = plan_details[2] if plan_details else ""
    try:
        await bot.send_message(
            user_id,
            build_missed_dca_cycle_notification(
                plan_number=plan_number,
                network_key=network_key,
                amount=amount,
                btc_address=btc_address,
                scheduled_time=scheduled_time,
                execute_command=execute_command,
                reason_code=reason_code,
            )
        )
    except Exception as e:
        logger.warning("Failed to send missed cycle notification to user %s: %s", user_id, e)
        return

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE dca_plans SET skip_notified = 1 WHERE id = ?",
            (plan_id,)
        )
        await db.commit()


async def mark_order_expired_before_send(
    *,
    plan_id: int,
    user_id: int,
    order_id: str,
    scheduled_time: Optional[int] = None,
    interval_hours: Optional[int] = None,
    manual_send_blocked: bool = False,
) -> None:
    """
    Mark order/cycle as expired and notify user.
    manual_send_blocked is kept for call compatibility; expired orders always use the same UX-safe text.
    """
    now_ts = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT next_run, interval_hours, from_asset, amount, btc_address FROM dca_plans WHERE id = ?",
            (plan_id,)
        ) as cur:
            plan_row = await cur.fetchone()

        base_scheduled_raw = scheduled_time if scheduled_time is not None else (plan_row[0] if plan_row else now_ts)
        try:
            base_scheduled = int(base_scheduled_raw)
        except (TypeError, ValueError):
            base_scheduled = now_ts

        base_interval_raw = interval_hours if interval_hours is not None else (plan_row[1] if plan_row else 24)
        try:
            base_interval_hours = int(base_interval_raw)
        except (TypeError, ValueError):
            base_interval_hours = 24
        network_key = plan_row[2] if plan_row else ""
        amount = plan_row[3] if plan_row else 0
        btc_address = plan_row[4] if plan_row else ""
        if base_scheduled > now_ts:
            # Manual execution flow should not shift future strategy schedule.
            new_next_run = base_scheduled
        else:
            new_next_run = calculate_next_run_preserving_schedule(base_scheduled, base_interval_hours, now_ts)

        await db.execute(
            "UPDATE dca_plans SET active_order_id = NULL, active_order_address = NULL, "
            "active_order_amount = NULL, active_order_expires = NULL, "
            "execution_state = 'expired', next_run = ?, skip_reason = COALESCE(skip_reason, ?), "
            "missed_count = COALESCE(missed_count, 0) + 1, last_missed_at = ?, "
            "last_execution_attempt_at = ? "
            "WHERE id = ?",
            (new_next_run, "order_expired", now_ts, now_ts, plan_id)
        )
        await db.execute(
            "UPDATE sent_transactions SET state = 'expired', error_message = ? "
            "WHERE plan_id = ? AND order_id = ? "
            "AND state IN ('sending', 'tx_pending', 'pending', 'blocked', 'approve_confirmed', 'transfering', 'sent')",
            ("Ордер истёк до отправки средств", plan_id, order_id)
        )
        await db.commit()

    logger.warning("Ордер истёк до отправки средств: plan_id=%s, order_id=%s", plan_id, order_id)

    plan_number = await get_plan_display_number(user_id, plan_id)
    execute_command = await get_execute_command_hint(user_id, plan_id)
    order_data = await fetch_fixedfloat_order_details(order_id)
    if is_requote_order_state(order_data):
        notification = build_requote_notification(
            order_id=order_id,
            plan_number=plan_number,
            network_key=network_key,
            amount=amount,
            btc_address=btc_address,
        )
    else:
        notification = build_order_expired_notification(
            order_id=order_id,
            plan_number=plan_number,
            network_key=network_key,
            amount=amount,
            btc_address=btc_address,
            execute_command=execute_command,
        )
    await bot.send_message(user_id, notification)


async def notify_and_clear_expired_order(plan_id: int, order_id: str) -> bool:
    """Send one expiry notification and clear active-order fields."""
    user_id = None
    network_key = ""
    amount = 0
    should_notify = False

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        async with db.execute(
            "SELECT user_id, from_asset, amount, btc_address, order_expired_notified "
            "FROM dca_plans WHERE id = ? AND active_order_id = ?",
            (plan_id, order_id)
        ) as cur:
            row = await cur.fetchone()

        if not row:
            await db.rollback()
            return False

        user_id, network_key, amount, btc_address, order_expired_notified = row
        should_notify = int(order_expired_notified or 0) != 1

        await db.execute(
            "UPDATE dca_plans SET order_expired_notified = 1, "
            "active_order_id = NULL, active_order_address = NULL, "
            "active_order_amount = NULL, active_order_expires = NULL "
            "WHERE id = ? AND active_order_id = ?",
            (plan_id, order_id)
        )
        await db.commit()

    if should_notify and user_id is not None:
        plan_number = await get_plan_display_number(int(user_id), plan_id)
        execute_command = await get_execute_command_hint(int(user_id), plan_id)
        notification = build_order_expired_notification(
            order_id=order_id,
            plan_number=plan_number,
            network_key=network_key,
            amount=amount,
            btc_address=btc_address,
            execute_command=execute_command,
        )
        await bot.send_message(int(user_id), notification, parse_mode="HTML", disable_web_page_preview=True)

    logger.info("Expired order cleared: plan_id=%s, order_id=%s, notified=%s", plan_id, order_id, should_notify)
    return True


async def claim_plan_execution(plan_id: int, user_id: Optional[int] = None) -> bool:
    """Atomically claim plan execution to avoid duplicate order creation."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        if user_id is None:
            cur = await db.execute(
                "UPDATE dca_plans SET execution_state = 'claiming' "
                "WHERE id = ? AND active = 1 AND deleted = 0 "
                "AND active_order_id IS NULL AND execution_state != 'claiming'",
                (plan_id,)
            )
        else:
            cur = await db.execute(
                "UPDATE dca_plans SET execution_state = 'claiming' "
                "WHERE id = ? AND user_id = ? AND active = 1 AND deleted = 0 "
                "AND active_order_id IS NULL AND execution_state != 'claiming'",
                (plan_id, user_id)
            )
        if cur.rowcount == 1:
            await db.commit()
            return True
        await db.rollback()
        return False


async def release_plan_claim(plan_id: int) -> None:
    """Release execution claim if order was not created."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE dca_plans SET execution_state = 'scheduled' "
            "WHERE id = ? AND execution_state = 'claiming' AND active_order_id IS NULL",
            (plan_id,)
        )
        await db.commit()


async def claim_auto_send_execution(plan_id: int, order_id: str) -> bool:
    """
    Atomically claim right to run auto_send_usdt for a plan/order pair.
    Uses sent_transactions state transition sending -> transfering as lock.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT active_order_id FROM dca_plans WHERE id = ?",
            (plan_id,)
        ) as cur:
            plan_row = await cur.fetchone()
        active_order_id = plan_row[0] if plan_row else None
        if active_order_id != order_id:
            logger.warning("Duplicate execution prevented for plan %s", plan_id)
            return False

        claim_cur = await db.execute(
            "UPDATE sent_transactions SET state = 'transfering' "
            "WHERE plan_id = ? AND order_id = ? AND state = 'sending'",
            (plan_id, order_id)
        )
        await db.commit()
        if claim_cur.rowcount != 1:
            logger.warning("Duplicate execution prevented for plan %s", plan_id)
            return False
        return True


async def can_resume_auto_send(plan_id: int, order_id: str) -> bool:
    """Guard resume path from duplicate transfer attempts."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT active_order_id FROM dca_plans WHERE id = ?",
            (plan_id,)
        ) as cur:
            plan_row = await cur.fetchone()
        active_order_id = plan_row[0] if plan_row else None
        if active_order_id != order_id:
            logger.warning("Duplicate execution prevented for plan %s", plan_id)
            return False

        async with db.execute(
            "SELECT state FROM sent_transactions "
            "WHERE plan_id = ? AND order_id = ? ORDER BY sent_at DESC LIMIT 1",
            (plan_id, order_id)
        ) as cur:
            tx_row = await cur.fetchone()
        tx_state = (tx_row[0] if tx_row else "") or ""
        if tx_state not in ("transfering", "approve_confirmed"):
            logger.warning("Duplicate execution prevented for plan %s", plan_id)
            return False
        return True


async def get_transfer_tx_status(network_key: str, tx_hash: str) -> str:
    """Return one of: confirmed, failed, pending."""
    if not tx_hash:
        return "pending"
    try:
        w3 = await asyncio.to_thread(get_web3_instance, network_key)
        receipt = await asyncio.to_thread(w3.eth.get_transaction_receipt, tx_hash)
        if receipt and getattr(receipt, "status", 0) == 1:
            return "confirmed"
        if receipt:
            return "failed"
    except TransactionNotFound:
        return "pending"
    except Exception as e:
        logger.warning(f"Failed to check tx status for {tx_hash}: {e}")
        return "pending"
    return "pending"


async def get_fixedfloat_order_status(order_id: str) -> str:
    """Get FixedFloat order status or empty string if unavailable."""
    if not order_id:
        return ""
    try:
        data = await ff_request_async("order", {"id": order_id})
        status = str((data or {}).get("status", "")).lower()
        return status
    except Exception as e:
        logger.warning(f"Failed to fetch FixedFloat order status for {order_id}: {e}")
        return ""


async def get_fixedfloat_order_status_with_retry(order_id: str, attempts: int = 7, delay_seconds: float = 2.5) -> str:
    """Retry FixedFloat status checks and return empty string if still unavailable."""
    for attempt in range(attempts):
        status = await get_fixedfloat_order_status(order_id)
        if status:
            return status
        if attempt < attempts - 1:
            await asyncio.sleep(delay_seconds)
    return ""


async def fetch_btc_txid(order_id: str) -> str:
    """Fetch btc_txid from completed_orders with short retries."""
    for attempt in range(3):
        try:
            async with aiosqlite.connect(DB_PATH) as tx_db:
                async with tx_db.execute(
                    "SELECT btc_txid FROM completed_orders WHERE order_id = ? LIMIT 1",
                    (order_id,)
                ) as tx_cur:
                    tx_row = await tx_cur.fetchone()
            btc_txid = tx_row[0] if tx_row and tx_row[0] else ""
            if btc_txid:
                return str(btc_txid)
        except Exception as e:
            logger.warning(f"Failed to fetch btc_txid for order {order_id} (attempt {attempt + 1}/3): {e}")
        if attempt < 2:
            await asyncio.sleep(0.4)
    return ""


async def fetch_fixedfloat_order_details(order_id: str) -> dict:
    if not order_id:
        return {}
    try:
        data = await ff_request_async("order", {"id": order_id})
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning("Failed to fetch FixedFloat order details for %s: %s", order_id, e)
        return {}


def extract_btc_amount_from_order_data(order_data: dict) -> str:
    to_info = (order_data.get("to", {}) or {})
    raw_amount = to_info.get("amount")
    try:
        return f"{float(raw_amount):.8f} BTC"
    except (TypeError, ValueError):
        return "—"


def is_requote_order_state(order_data: dict) -> bool:
    candidates = [
        str(order_data.get("status", "")).lower(),
        str(order_data.get("action", "")).lower(),
        str(order_data.get("type", "")).lower(),
        str(order_data.get("reason", "")).lower(),
        str(order_data.get("message", "")).lower(),
    ]
    flags = [
        order_data.get("needConfirm"),
        order_data.get("need_confirm"),
        order_data.get("confirm"),
        order_data.get("recalculate"),
        order_data.get("requote"),
    ]
    if any(bool(flag) for flag in flags):
        return True
    joined = " ".join(item for item in candidates if item)
    keywords = ("requote", "requote_required", "changed", "new_rate", "newprice", "confirm rate")
    return any(keyword in joined for keyword in keywords)


def _fetch_btc_tx_observation_sync(txid: str) -> dict:
    url = f"https://api.blockchair.com/bitcoin/dashboards/transaction/{txid}"
    response = requests.get(url, timeout=12)
    response.raise_for_status()
    payload = response.json() or {}
    tx_data = ((payload.get("data") or {}).get(str(txid)) or {})
    transaction = tx_data.get("transaction", {}) or {}
    confirmations = transaction.get("confirmations")
    if confirmations is None:
        confirmations = 1 if transaction.get("block_id") else 0
    return {
        "confirmations": int(confirmations or 0),
        "found": bool(tx_data),
    }


async def fetch_btc_tx_observation(txid: str) -> dict:
    if not txid:
        return {"confirmations": 0, "found": False}
    try:
        return await asyncio.to_thread(_fetch_btc_tx_observation_sync, txid)
    except Exception as e:
        logger.warning("Failed to fetch BTC tx observation for %s: %s", txid, e)
        return {"confirmations": 0, "found": False}


def track_order_progress_message(order_id: str, user_id: int, message_id: int) -> None:
    """Remember progress message for further edit updates."""
    _order_progress_messages[str(order_id)] = (int(user_id), int(message_id))


async def update_order_progress_message(user_id: int, order_id: str, text: str) -> None:
    """Try edit existing progress message, fallback to sending a new one."""
    has_order_link = "fixedfloat.com/order/" in str(text or "")
    msg_meta = _order_progress_messages.get(str(order_id))
    if msg_meta and msg_meta[0] == int(user_id):
        try:
            await bot.edit_message_text(
                chat_id=int(user_id),
                message_id=msg_meta[1],
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=has_order_link,
            )
            return
        except Exception as e:
            logger.warning("Failed to edit progress message for order %s: %s", order_id, e)
    try:
        sent_msg = await bot.send_message(
            int(user_id),
            text,
            parse_mode="HTML",
            disable_web_page_preview=has_order_link,
        )
        track_order_progress_message(str(order_id), int(user_id), int(sent_msg.message_id))
    except Exception as e:
        logger.error("Failed to send progress message for order %s: %s", order_id, e)


async def mark_order_completed(plan_id: int, order_id: str, reason: str) -> None:
    """Mark order as completed, clear active marker, and write history entry."""
    completed_at = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT user_id FROM sent_transactions WHERE plan_id = ? AND order_id = ? ORDER BY sent_at DESC LIMIT 1",
            (plan_id, order_id)
        ) as cur:
            tx_row = await cur.fetchone()
        user_id = tx_row[0] if tx_row else None
        if user_id is None:
            async with db.execute("SELECT user_id FROM dca_plans WHERE id = ?", (plan_id,)) as pcur:
                plan_row = await pcur.fetchone()
            user_id = plan_row[0] if plan_row else None

        await db.execute(
            "UPDATE sent_transactions SET state = 'confirmed', error_message = NULL "
            "WHERE plan_id = ? AND order_id = ? AND state IN ('sending', 'tx_pending', 'pending', 'blocked', 'approve_confirmed', 'transfering', 'sent')",
            (plan_id, order_id)
        )
        if user_id is not None:
            await db.execute(
                "INSERT OR IGNORE INTO completed_orders (user_id, order_id, completed_at) VALUES (?, ?, ?)",
                (user_id, order_id, completed_at)
            )
        await db.execute(
            "UPDATE dca_plans SET active_order_id = NULL, active_order_address = NULL, "
            "active_order_amount = NULL, active_order_expires = NULL, execution_state = 'scheduled', "
            "missed_count = 0 WHERE id = ?",
            (plan_id,)
        )
        await db.commit()
    _balances_cache.clear()
    logger.info("Order %s completed (reason=%s), clearing active order", order_id, reason)


async def mark_order_failed(plan_id: int, order_id: str, reason: str) -> None:
    """Mark order as failed and clear active marker."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE sent_transactions SET state = 'failed', error_message = ? "
            "WHERE plan_id = ? AND order_id = ? AND state IN ('sending', 'tx_pending', 'pending', 'blocked', 'approve_confirmed', 'transfering', 'sent')",
            (reason[:500], plan_id, order_id)
        )
        await db.execute(
            "UPDATE dca_plans SET active_order_id = NULL, active_order_address = NULL, "
            "active_order_amount = NULL, active_order_expires = NULL, execution_state = 'scheduled' WHERE id = ?",
            (plan_id,)
        )
        await db.commit()
    logger.info("Order %s failed (reason=%s), clearing active order", order_id, reason)


async def finalize_expired_unavailable_order(plan_id: int, order_id: str, local_expires: Optional[int], now_ts: int) -> str:
    """
    Resolve order when FixedFloat status is unavailable and local order already expired.
    Returns: completed | failed | pending | not_expired
    """
    if not local_expires or now_ts <= local_expires:
        return "not_expired"

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT network_key, transfer_tx_hash FROM sent_transactions "
            "WHERE plan_id = ? AND order_id = ? ORDER BY sent_at DESC LIMIT 1",
            (plan_id, order_id)
        ) as cur:
            tx_row = await cur.fetchone()

    if not tx_row or not tx_row[1]:
        await mark_order_failed(plan_id, order_id, "FixedFloat status unavailable and order expired")
        return "failed"

    network_key, transfer_tx_hash = tx_row
    tx_status = await get_transfer_tx_status(network_key, transfer_tx_hash)
    if tx_status == "confirmed":
        await mark_order_completed(plan_id, order_id, "fixedfloat_status_unavailable_tx_confirmed")
        return "completed"
    if tx_status == "failed":
        await mark_order_failed(plan_id, order_id, "Transfer tx reverted on-chain after order expiry")
        return "failed"
    return "pending"


async def resume_transfer_after_approve(
    *,
    network_key: str,
    user_id: int,
    btc_address: str,
    order_id: str,
    deposit_address: str,
    required_amount: float,
    existing_approve_tx: Optional[str],
    plan_id: Optional[int] = None,
    order_expires: Optional[int] = None,
    scheduled_time: Optional[int] = None,
    interval_hours: Optional[int] = None,
) -> tuple[str, Optional[str], Optional[str], str]:
    """
    Continue flow after approve confirmation by attempting transfer step immediately.
    Returns: (state, approve_tx_hash, transfer_tx_hash, error_message)
    """
    if is_order_expired(order_expires):
        if plan_id is not None:
            await mark_order_expired_before_send(
                plan_id=plan_id,
                user_id=user_id,
                order_id=order_id,
                scheduled_time=scheduled_time,
                interval_hours=interval_hours,
            )
        else:
            logger.warning("Ордер истёк до отправки средств: order_id=%s", order_id)
        return ("expired", existing_approve_tx, None, "ORDER_EXPIRED_BEFORE_SEND")

    if plan_id is not None:
        if not await can_resume_auto_send(plan_id, order_id):
            return ("blocked", existing_approve_tx, None, "DUPLICATE_EXECUTION_PREVENTED")

    wallet_password = _wallet_passwords.get(user_id)
    if not wallet_password:
        return ("failed", existing_approve_tx, None, "Wallet password not available for transfer after approve")

    success, approve_tx, transfer_tx, error_msg = await auto_send_usdt(
        network_key=network_key,
        user_id=user_id,
        wallet_password=wallet_password,
        deposit_address=deposit_address,
        required_amount=required_amount,
        btc_address=btc_address,
        order_id=order_id,
        dry_run=DRY_RUN
    )
    if approve_tx or transfer_tx:
        _balances_cache.clear()
    final_approve_tx = approve_tx or existing_approve_tx

    if success:
        return ("confirmed", final_approve_tx, transfer_tx, "")
    if is_pending_tx_error(error_msg) or (is_retryable_network_error(error_msg) and (final_approve_tx or transfer_tx)):
        return ("tx_pending", final_approve_tx, transfer_tx, (error_msg or "TX_PENDING")[:500])
    if is_retryable_network_error(error_msg):
        return ("blocked", final_approve_tx, transfer_tx, (error_msg or "Retryable network error")[:500])
    return ("failed", final_approve_tx, transfer_tx, (error_msg or "Transfer failed")[:500])


async def recovery_scan_pending_transactions() -> None:
    """Recover in-flight transactions after bot restart."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, plan_id, user_id, order_id, network_key, approve_tx_hash, transfer_tx_hash, error_message, state, amount, deposit_address "
            "FROM sent_transactions WHERE state IN ('sending', 'tx_pending', 'pending', 'blocked')"
        ) as cur:
            rows = await cur.fetchall()

    if not rows:
        return

    now = int(time.time())
    for tx_id, plan_id, tx_user_id, order_id, network_key, approve_tx, transfer_tx, error_message, current_state, tx_amount, tx_deposit_address in rows:
        pending_is_approve = (error_message or "").startswith("APPROVE_TX_PENDING:")
        tx_hash = approve_tx if pending_is_approve else (transfer_tx or approve_tx)

        if not tx_hash:
            continue

        logger.info("Recovery: checking pending transaction %s", tx_hash)
        tx_status = await get_transfer_tx_status(network_key, tx_hash)

        async with aiosqlite.connect(DB_PATH) as db:
            if tx_status == "confirmed":
                if pending_is_approve:
                    await db.execute(
                        "UPDATE sent_transactions SET state = 'approve_confirmed', error_message = NULL WHERE id = ?",
                        (tx_id,)
                    )
                    await db.commit()

                    btc_address = ""
                    active_order_expires = None
                    plan_next_run = None
                    plan_interval_hours = None
                    if plan_id:
                        async with db.execute(
                            "SELECT btc_address, active_order_expires, next_run, interval_hours FROM dca_plans WHERE id = ?",
                            (plan_id,)
                        ) as bcur:
                            b_row = await bcur.fetchone()
                        btc_address = (b_row[0] if b_row else "") or ""
                        active_order_expires = b_row[1] if b_row else None
                        plan_next_run = b_row[2] if b_row else None
                        plan_interval_hours = b_row[3] if b_row else None

                    resume_state, resume_approve_tx, resume_transfer_tx, resume_error = await resume_transfer_after_approve(
                        network_key=network_key,
                        user_id=tx_user_id,
                        btc_address=btc_address,
                        order_id=order_id,
                        deposit_address=tx_deposit_address or "",
                        required_amount=float(tx_amount or 0.0),
                        existing_approve_tx=approve_tx,
                        plan_id=plan_id,
                        order_expires=active_order_expires,
                        scheduled_time=plan_next_run,
                        interval_hours=plan_interval_hours,
                    )

                    if resume_state == "confirmed":
                        await db.execute(
                            "UPDATE sent_transactions SET state = 'confirmed', approve_tx_hash = ?, transfer_tx_hash = ?, error_message = NULL WHERE id = ?",
                            (resume_approve_tx, resume_transfer_tx, tx_id)
                        )
                        if plan_id:
                            async with db.execute(
                                "SELECT interval_hours FROM dca_plans WHERE id = ?",
                                (plan_id,)
                            ) as pcur:
                                plan_row = await pcur.fetchone()
                            if plan_row:
                                new_next_run = now + (plan_row[0] * 3600)
                                await db.execute(
                                    "UPDATE dca_plans SET next_run = ?, missed_count = 0 WHERE id = ?",
                                    (new_next_run, plan_id)
                                )
                    else:
                        await db.execute(
                            "UPDATE sent_transactions SET state = ?, approve_tx_hash = ?, transfer_tx_hash = ?, error_message = ? WHERE id = ?",
                            (resume_state, resume_approve_tx, resume_transfer_tx, resume_error, tx_id)
                        )
                else:
                    await db.execute(
                        "UPDATE sent_transactions SET state = 'confirmed', error_message = NULL WHERE id = ?",
                        (tx_id,)
                    )
                    if plan_id:
                        async with db.execute(
                            "SELECT interval_hours FROM dca_plans WHERE id = ?",
                            (plan_id,)
                        ) as pcur:
                            plan_row = await pcur.fetchone()
                        if plan_row:
                            new_next_run = now + (plan_row[0] * 3600)
                            await db.execute(
                                "UPDATE dca_plans SET next_run = ?, missed_count = 0 WHERE id = ?",
                                (new_next_run, plan_id)
                            )
            elif tx_status == "pending":
                await db.execute(
                    "UPDATE sent_transactions SET state = 'tx_pending' WHERE id = ?",
                    (tx_id,)
                )
            else:
                await db.execute(
                    "UPDATE sent_transactions SET state = 'failed' WHERE id = ?",
                    (tx_id,)
                )
            await db.commit()


async def recover_stale_plan_claims() -> None:
    """
    Clear stale 'claiming' states left after unexpected restart/crash.
    Safe because only rows without active_order_id are reset.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "UPDATE dca_plans SET execution_state = 'scheduled' "
            "WHERE execution_state = 'claiming' AND active_order_id IS NULL"
        )
        await db.commit()
        recovered = cur.rowcount or 0
    if recovered:
        logger.warning("Recovered stale plan claims: %s", recovered)


def load_last_seen_execution_time() -> Optional[int]:
    """Load last bot execution timestamp from local file."""
    try:
        with open(LAST_SEEN_EXECUTION_FILE, "r", encoding="utf-8") as f:
            raw = f.read().strip()
        if not raw:
            return None
        return int(raw)
    except (FileNotFoundError, ValueError, OSError):
        return None


def save_last_seen_execution_time(ts: int) -> None:
    """Persist current bot execution timestamp to local file."""
    try:
        os.makedirs(LOGS_DIR, exist_ok=True)
        target_dir = os.path.dirname(LAST_SEEN_EXECUTION_FILE)
        if target_dir:
            os.makedirs(target_dir, exist_ok=True)
        with open(LAST_SEEN_EXECUTION_FILE, "w", encoding="utf-8") as f:
            f.write(str(int(ts)))
    except OSError as e:
        logger.warning("Failed to save last seen execution time: %s", e)


async def notify_offline_startup_status() -> None:
    """
    Notify users at startup if bot was offline long enough.
    Rules:
    - If downtime <= execution window and no skipped plans: no notification.
    - If downtime > execution window:
      - with skipped plans -> notify about missed cycles;
      - without skipped plans -> notify that no cycles were missed.
    """
    now_ts = int(time.time())
    last_seen_ts = load_last_seen_execution_time()
    downtime = (now_ts - last_seen_ts) if last_seen_ts else None
    offline_detected = bool(last_seen_ts and downtime is not None and downtime > DCA_EXECUTION_WINDOW_SECONDS)

    try:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT user_id, id, from_asset, amount, btc_address, next_run, skip_reason, missed_count, last_missed_at "
                "FROM dca_plans "
                "WHERE deleted = 0 AND execution_state = 'skipped' "
                "AND COALESCE(skip_notified, 0) = 0 ORDER BY user_id, id"
            ) as cur:
                skipped_rows = await cur.fetchall()

            async with db.execute(
                "SELECT DISTINCT user_id FROM dca_plans WHERE deleted = 0 ORDER BY user_id"
            ) as users_cur:
                active_user_rows = await users_cur.fetchall()

            async with db.execute(
                "SELECT user_id, id, from_asset, amount, btc_address, interval_hours, next_run, "
                "COALESCE(missed_count, 0), last_execution_attempt_at "
                "FROM dca_plans WHERE active = 1 AND deleted = 0 ORDER BY user_id, id"
            ) as plans_cur:
                active_plan_rows = await plans_cur.fetchall()
    except Exception as e:
        logger.error("Failed to load startup offline status data: %s", e)
        save_last_seen_execution_time(now_ts)
        return

    skipped_by_user: Dict[int, list[dict]] = {}
    for user_id, plan_id, network_key, amount, btc_address, scheduled_time, skip_reason, missed_count, last_missed_at in skipped_rows:
        execute_command = await get_execute_command_hint(user_id, plan_id)
        scheduled_at = int(last_missed_at or scheduled_time or now_ts)
        skipped_by_user.setdefault(user_id, []).append({
            "plan_id": plan_id,
            "plan_number": await get_plan_display_number(user_id, plan_id),
            "execute_command": execute_command,
            "network_key": network_key,
            "amount": amount,
            "btc_address": btc_address,
            "scheduled_time": scheduled_at,
            "reason_code": skip_reason or "window_expired",
            "missed_count": 1,
            "cycle_count": 1,
        })

    offline_by_user: Dict[int, list[dict]] = {}
    offline_updates: list[tuple[int, int, int]] = []
    if offline_detected:
        offline_end = now_ts - DCA_EXECUTION_WINDOW_SECONDS
        for user_id, plan_id, network_key, amount, btc_address, interval_hours, next_run, current_missed_count, last_execution_attempt_at in active_plan_rows:
            try:
                scheduled_time = int(next_run)
                interval_seconds = max(1, int(interval_hours) * 3600)
            except (TypeError, ValueError):
                continue
            offline_start = max(int(last_seen_ts), int(last_execution_attempt_at or 0))
            while scheduled_time < offline_start:
                scheduled_time += interval_seconds
            execute_command = await get_execute_command_hint(user_id, plan_id)
            plan_number = await get_plan_display_number(user_id, plan_id)
            offline_count = 0
            last_offline_missed_at = None
            while scheduled_time <= offline_end:
                offline_by_user.setdefault(user_id, []).append({
                    "plan_id": plan_id,
                    "plan_number": plan_number,
                    "execute_command": execute_command,
                    "network_key": network_key,
                    "amount": amount,
                    "btc_address": btc_address,
                    "scheduled_time": scheduled_time,
                    "reason_code": "offline",
                    "missed_count": 1,
                    "cycle_count": 1,
                })
                offline_count += 1
                last_offline_missed_at = scheduled_time
                scheduled_time += interval_seconds
            if offline_count and last_offline_missed_at is not None:
                offline_updates.append((offline_count, last_offline_missed_at, plan_id))

    if offline_updates:
        async with aiosqlite.connect(DB_PATH) as db:
            for offline_count, last_offline_missed_at, plan_id in offline_updates:
                await db.execute(
                    "UPDATE dca_plans SET skip_notified = 0, skip_reason = COALESCE(skip_reason, ?), "
                    "missed_count = COALESCE(missed_count, 0) + ?, last_missed_at = ?, "
                    "last_execution_attempt_at = ? "
                    "WHERE id = ?",
                    ("offline", offline_count, now_ts, now_ts, plan_id)
                )
            await db.commit()

    if not offline_detected and not skipped_by_user:
        save_last_seen_execution_time(now_ts)
        return

    missed_by_user: Dict[int, list[dict]] = {}
    for user_id, items in offline_by_user.items():
        missed_by_user.setdefault(user_id, []).extend(items)
    for user_id, items in skipped_by_user.items():
        existing_keys = {(item["plan_id"], item["scheduled_time"]) for item in missed_by_user.get(user_id, [])}
        for item in items:
            key = (item["plan_id"], item["scheduled_time"])
            if key not in existing_keys:
                missed_by_user.setdefault(user_id, []).append(item)
                existing_keys.add(key)

    if missed_by_user:
        notified_users = set()
        notified_plan_ids = set()
        for user_id, missed_items in missed_by_user.items():
            notified_users.add(user_id)
            message_text = build_offline_startup_notification(missed_items)
            try:
                await bot.send_message(user_id, message_text)
                notified_plan_ids.update(
                    item["plan_id"] for item in missed_items
                )
            except Exception as e:
                logger.warning("Failed to send offline/skipped notification to user %s: %s", user_id, e)

        if notified_plan_ids:
            try:
                async with aiosqlite.connect(DB_PATH) as db:
                    placeholders = ",".join("?" for _ in notified_plan_ids)
                    await db.execute(
                        f"UPDATE dca_plans SET execution_state = 'scheduled' "
                        f"WHERE execution_state = 'skipped' AND id IN ({placeholders})",
                        tuple(notified_plan_ids)
                    )
                    await db.execute(
                        f"UPDATE dca_plans SET skip_notified = 1, missed_count = 0, last_missed_at = NULL "
                        f"WHERE id IN ({placeholders})",
                        tuple(notified_plan_ids)
                    )
                    await db.commit()
            except Exception as e:
                logger.warning("Failed to clear processed skipped states after startup notification: %s", e)

        if not offline_detected:
            save_last_seen_execution_time(now_ts)
            return

        for (user_id,) in active_user_rows:
            if user_id in notified_users:
                continue
            message_text = (
                "ℹ️ Бот был офлайн, но покупки не были пропущены.\n\n"
                f"{build_status_hint()}"
            )
            try:
                await bot.send_message(user_id, message_text)
            except Exception as e:
                logger.warning("Failed to send offline/ok notification to user %s: %s", user_id, e)
    elif offline_detected:
        for (user_id,) in active_user_rows:
            message_text = (
                "ℹ️ Бот был офлайн, но покупки не были пропущены.\n\n"
                f"{build_status_hint()}"
            )
            try:
                await bot.send_message(user_id, message_text)
            except Exception as e:
                logger.warning("Failed to send offline/ok notification to user %s: %s", user_id, e)

    save_last_seen_execution_time(now_ts)


def ff_sign(data_str: str) -> str:
    """
    Создание HMAC-SHA256 подписи для запроса к FixedFloat API.
    Подпись создаётся из тела запроса и секретного ключа.
    """
    if not FF_API_SECRET:
        raise ValueError("FF_API_SECRET не задан в .env")
    return hmac.new(
        key=FF_API_SECRET.encode("utf-8"),
        msg=data_str.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).hexdigest()


def ff_request(method: str, params=None) -> dict:
    """
    Универсальный синхронный POST-запрос к FixedFloat API.
    Supports mock mode for testing.
    
    Args:
        method: endpoint API (например: "ccies", "price", "create")
        params: параметры запроса (dict)
    
    Returns:
        dict с данными ответа от API
    
    Raises:
        RuntimeError: если API вернул ошибку (code != 0)
    """
    # Mock mode - return mocked responses
    if MOCK_FIXEDFLOAT:
        logger.info(f"[MOCK] FixedFloat API запрос: {method} с параметрами {mask_sensitive_data(params)}")
        
        if method == "ccies":
            mock_response = get_mock_fixedfloat_ccies()
            logger.info(f"[MOCK] FixedFloat ответ: {method}")
            return mock_response["data"]
        
        elif method == "price":
            network_key = params.get("fromCcy", "").replace("USDT", "USDT-")
            if "ARBITRUM" in network_key.upper():
                network_key = "USDT-ARB"
            elif "BSC" in network_key.upper():
                network_key = "USDT-BSC"
            elif "POLYGON" in network_key.upper():
                network_key = "USDT-POLYGON"
            mock_response = get_mock_fixedfloat_price(network_key)
            logger.info(f"[MOCK] FixedFloat ответ: {method}")
            return mock_response["data"]
        
        elif method == "create":
            # Extract network from fromCcy
            from_ccy = params.get("fromCcy", "")
            network_key = "USDT-ARB"  # default
            if "ARBITRUM" in from_ccy.upper():
                network_key = "USDT-ARB"
            elif "BSC" in from_ccy.upper():
                network_key = "USDT-BSC"
            elif "POLYGON" in from_ccy.upper():
                network_key = "USDT-POLYGON"
            
            amount = float(params.get("amount", 0))
            btc_address = params.get("toAddress", "")
            mock_response = get_mock_fixedfloat_order(network_key, amount, btc_address)
            logger.info(f"[MOCK] FixedFloat ответ: {method}, order_id={mock_response['data']['id']}")
            return mock_response["data"]
        
        else:
            logger.warning(f"[MOCK] Unknown method {method}, returning empty data")
            return {}
    
    # Real API call
    if not FF_API_KEY or not FF_API_SECRET:
        raise ValueError("FF_API_KEY или FF_API_SECRET не заданы в .env")

    if params is None:
        params = {}

    url = f"{FF_API_URL}/{method}"
    data_str = json.dumps(params, separators=(",", ":"), ensure_ascii=False)

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json; charset=UTF-8",
        "X-API-KEY": FF_API_KEY,
        "X-API-SIGN": ff_sign(data_str),
    }

    logger.info(f"FixedFloat API запрос: {method} с параметрами {mask_sensitive_data(params)}")
    try:
        resp = requests.post(url, data=data_str.encode("utf-8"), headers=headers, timeout=30)
        resp.raise_for_status()  # Вызовет исключение для HTTP ошибок (4xx, 5xx)
    except requests.exceptions.RequestException as e:
        logger.error(f"Ошибка HTTP запроса к FixedFloat API: {e}")
        raise RuntimeError(f"Ошибка подключения к FixedFloat API: {e}")
    
    logger.info(f"FixedFloat ответ: status={resp.status_code}")

    try:
        data = resp.json()
    except ValueError as e:
        logger.error(f"Ошибка парсинга JSON ответа от FixedFloat: {e}, response text: {resp.text[:200]}")
        raise RuntimeError(f"Неверный формат ответа от FixedFloat API: {e}")
    
    code = data.get("code")
    if code != 0:
        error_msg = data.get("msg", "Unknown error")
        error_data = data.get("data")
        
        # Специальная обработка известных ошибок
        if code == 310:
            error_msg = "Валюта или сеть недоступна для обмена"
        elif code == 311:
            error_msg = "Валюта недоступна для получения в данный момент"
        elif code == 312:
            error_msg = "Валюта недоступна для отправки в данный момент"
        elif code == 301:
            error_msg = "Сумма вне допустимых лимитов"
        elif code == 401:
            error_msg = "Неверные API ключи"
        elif code == 501:
            error_msg = "Нет прав доступа к API"
        
        logger.error(f"FixedFloat API ошибка (code={code}): {error_msg}, data={error_data}")
        raise RuntimeError(f"FixedFloat error (code={code}): {error_msg}")
    
    return data["data"]




async def ff_request_async(method: str, params=None) -> dict:
    """
    Асинхронная обёртка над ff_request для неблокирующих вызовов API.
    Выполняет синхронный запрос в отдельном потоке, чтобы не блокировать event loop бота.
    """
    return await asyncio.to_thread(ff_request, method, params)


async def get_fixedfloat_limits(network_key: str) -> dict:
    """
    Получает минимальные и максимальные лимиты для сети из FixedFloat API.
    
    Args:
        network_key: ключ сети из NETWORK_CODES (например "USDT-ARB")
    
    Returns:
        dict с ключами 'min' и 'max' (float значения в USDT)
    
    Raises:
        RuntimeError: если сеть недоступна или API вернул ошибку
    """
    from_ccy = get_fixedfloat_symbol(network_key)
    if not from_ccy:
        raise ValueError(f"Неизвестная сеть: {network_key}")
    cached = _fixedfloat_limits_cache.get(network_key)
    now_ts = time.time()
    if cached and now_ts - float(cached.get("ts", 0)) < LIMITS_CACHE_TTL:
        return dict(cached["data"])
    
    try:
        # Используем price API для получения лимитов
        data = await ff_request_async("price", {
            "type": "fixed",
            "fromCcy": from_ccy,
            "toCcy": "BTC",
            "direction": "from",
            "amount": 50,  # любая сумма для получения лимитов
        })
        
        from_info = data.get("from", {})
        to_info = data.get("to", {})
        min_amt = from_info.get("min")
        max_amt = from_info.get("max")
        to_amount = to_info.get("amount")
        
        if min_amt is None or max_amt is None:
            raise RuntimeError(f"Не удалось получить лимиты для {network_key}")
        rate = None
        try:
            to_amount_float = float(to_amount)
            rate = (50.0 / to_amount_float) if to_amount_float else None
        except (TypeError, ValueError):
            rate = None
        
        result = {
            "min": float(min_amt),
            "max": float(max_amt),
            "rate": rate,
        }
        _fixedfloat_limits_cache[network_key] = {"ts": now_ts, "data": result}
        return dict(result)
    except RuntimeError as e:
        # Пробрасываем ошибки API дальше
        raise
    except Exception as e:
        logger.error(f"Ошибка получения лимитов для {network_key}: {e}")
        raise RuntimeError(f"Ошибка получения лимитов для {network_key}: {e}")


async def update_network_codes():
    """
    Обновляет маппинг кодов сетей из реального API FixedFloat.
    Вызывается при старте бота для актуализации кодов валют.
    """
    try:
        items = await ff_request_async("ccies", {})
        for item in items:
            if item.get("coin") == "USDT":
                code = item.get("code")
                network = item.get("network", "").upper()
                
                # Обновляем известные маппинги
                if "ARBITRUM" in network:
                    NETWORK_CODES["USDT-ARB"] = code
                elif "BSC" in network or "BEP20" in network:
                    NETWORK_CODES["USDT-BSC"] = code
                elif "POLYGON" in network:
                    NETWORK_CODES["USDT-POLYGON"] = code
        
        logger.info(f"Обновлены коды сетей: {NETWORK_CODES}")
    except Exception as e:
        logger.error(f"Ошибка обновления кодов сетей: {e}")


def create_fixedfloat_order(network_key: str, amount_usdt: float, btc_address: str) -> dict:
    """
    Универсальная функция создания ордера на обмен USDT -> BTC через FixedFloat.
    
    Args:
        network_key: ключ сети из NETWORK_CODES (например "USDT-ARB")
        amount_usdt: сумма в USDT для обмена
        btc_address: адрес BTC для получения
    
    Returns:
        dict с данными созданного ордера (id, адрес депозита, сумма и т.д.)
    """
    from_ccy = get_fixedfloat_symbol(network_key)
    if not from_ccy:
        raise ValueError(f"Неизвестная сеть: {network_key}")

    params = {
        "type": "fixed",  # фиксированный курс
        "fromCcy": from_ccy,  # из какой валюты
        "toCcy": "BTC",  # в какую валюту
        "direction": "from",  # фиксируем исходную сумму
        "amount": float(amount_usdt),
        "toAddress": btc_address,  # куда отправить BTC
    }
    
    logger.info(f"Создание ордера: {amount_usdt} {from_ccy} -> BTC на {btc_address}")
    return ff_request("create", params)


# ============================================================================
# ИНИЦИАЛИЗАЦИЯ БОТА И БД
# ============================================================================


class AccessControlMiddleware(BaseMiddleware):
    """Allow bot usage only for ADMIN_USER_ID."""

    async def __call__(
        self,
        handler: Callable[[Any, Dict[str, Any]], Awaitable[Any]],
        event: Any,
        data: Dict[str, Any]
    ) -> Any:
        user = data.get("event_from_user") or getattr(event, "from_user", None)
        if user and user.id == ADMIN_USER_ID:
            return await handler(event, data)

        if isinstance(event, Message):
            await event.answer("⛔️ Доступ запрещен.")
        elif isinstance(event, CallbackQuery):
            await event.answer("⛔️ Доступ запрещен.", show_alert=True)
        logger.warning(f"Access denied for user_id={getattr(user, 'id', None)}")
        return

# Токен Telegram бота из переменных окружения
BOT_TOKEN = os.getenv("DCA_TELEGRAM_BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("DCA_TELEGRAM_BOT_TOKEN is not set")

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher(storage=MemoryStorage())
dp.update.middleware(AccessControlMiddleware())
DB_PATH = resolve_project_path(os.getenv("DATABASE_PATH", ""), DEFAULT_DB_PATH)


async def setup_bot_commands() -> None:
    await bot.set_my_commands([
        BotCommand(command="start", description="👋 старт"),
        BotCommand(command="setdca", description="➕ новый план"),
        BotCommand(command="status", description="📊 планы"),
        BotCommand(command="limits", description="🌐 лимиты"),
        BotCommand(command="walletstatus", description="💼 баланс"),
        BotCommand(command="history", description="📜 история"),
        BotCommand(command="help", description="ℹ️ помощь"),
    ])


def run_startup_checks() -> None:
    """Validate startup prerequisites before runtime initialization."""
    if not BOT_TOKEN:
        raise ValueError("DCA_TELEGRAM_BOT_TOKEN is not set")
    if ADMIN_USER_ID <= 0:
        raise RuntimeError("Invalid ADMIN_USER_ID in .env. Expected a positive integer")


def ensure_runtime_directories() -> None:
    """Create required directories for logs/db/keystore before writes."""
    os.makedirs(LOGS_DIR, exist_ok=True)
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    os.makedirs(KEYSTORE_DIR, exist_ok=True)
    last_seen_dir = os.path.dirname(LAST_SEEN_EXECUTION_FILE)
    if last_seen_dir:
        os.makedirs(last_seen_dir, exist_ok=True)


async def init_db():
    """
    Инициализация SQLite базы данных.
    Создаёт таблицу dca_plans для хранения планов автоматических покупок.
    
    Структура таблицы:
    - user_id: Telegram ID пользователя (НЕ уникальный - может быть несколько планов)
    - from_asset: сеть USDT (USDT-ARB, USDT-BSC, USDT-POLYGON)
    - amount: сумма покупки в USD
    - interval_hours: интервал между покупками (в часах)
    - btc_address: адрес BTC для получения
    - next_run: UNIX timestamp следующего запуска
    - active: активен ли план (1/0)
    - active_order_id: ID активного ордера на FixedFloat (если есть)
    - active_order_address: адрес для депозита активного ордера
    - active_order_amount: сумма для отправки
    - active_order_expires: timestamp истечения ордера
    - deleted: флаг мягкого удаления (0 = активен, 1 = удалён)
    - Уникальность: может быть до 3 планов на одну сеть (user_id + from_asset)
    """
    async with aiosqlite.connect(DB_PATH) as db:
        # Создаём таблицу если её нет
        await db.execute('''
            CREATE TABLE IF NOT EXISTS dca_plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                from_asset TEXT,
                amount REAL,
                interval_hours INTEGER,
                btc_address TEXT,
                next_run INTEGER,
                active BOOLEAN DEFAULT 1,
                created_at INTEGER DEFAULT (strftime('%s','now')),
                active_order_id TEXT,
                active_order_address TEXT,
                active_order_amount TEXT,
                active_order_expires INTEGER,
                deleted BOOLEAN DEFAULT 0,
                skip_notified INTEGER DEFAULT 0,
                skip_reason TEXT,
                missed_count INTEGER DEFAULT 0,
                last_missed_at INTEGER,
                last_execution_attempt_at INTEGER,
                order_expired_notified INTEGER DEFAULT 0
            )
        ''')
        
        # Проверяем существующие столбцы и добавляем новые если их нет
        async with db.execute("PRAGMA table_info(dca_plans)") as cursor:
            columns = await cursor.fetchall()
            existing_columns = [col[1] for col in columns]
        
        if "active_order_id" not in existing_columns:
            await db.execute("ALTER TABLE dca_plans ADD COLUMN active_order_id TEXT")
        if "active_order_address" not in existing_columns:
            await db.execute("ALTER TABLE dca_plans ADD COLUMN active_order_address TEXT")
        if "active_order_amount" not in existing_columns:
            await db.execute("ALTER TABLE dca_plans ADD COLUMN active_order_amount TEXT")
        if "active_order_expires" not in existing_columns:
            await db.execute("ALTER TABLE dca_plans ADD COLUMN active_order_expires INTEGER")
        if "deleted" not in existing_columns:
            await db.execute("ALTER TABLE dca_plans ADD COLUMN deleted BOOLEAN DEFAULT 0")
        if "execution_state" not in existing_columns:
            await db.execute("ALTER TABLE dca_plans ADD COLUMN execution_state TEXT DEFAULT 'scheduled'")
        if "last_tx_hash" not in existing_columns:
            await db.execute("ALTER TABLE dca_plans ADD COLUMN last_tx_hash TEXT")
        if "skip_notified" not in existing_columns:
            await db.execute("ALTER TABLE dca_plans ADD COLUMN skip_notified INTEGER DEFAULT 0")
        if "skip_reason" not in existing_columns:
            await db.execute("ALTER TABLE dca_plans ADD COLUMN skip_reason TEXT")
        if "missed_count" not in existing_columns:
            await db.execute("ALTER TABLE dca_plans ADD COLUMN missed_count INTEGER DEFAULT 0")
        if "last_missed_at" not in existing_columns:
            await db.execute("ALTER TABLE dca_plans ADD COLUMN last_missed_at INTEGER")
        if "last_execution_attempt_at" not in existing_columns:
            await db.execute("ALTER TABLE dca_plans ADD COLUMN last_execution_attempt_at INTEGER")
        if "order_expired_notified" not in existing_columns:
            await db.execute("ALTER TABLE dca_plans ADD COLUMN order_expired_notified INTEGER DEFAULT 0")
        
        # Создаём таблицу для хранения информации о кошельках (single wallet per user)
        await db.execute('''
            CREATE TABLE IF NOT EXISTS wallets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL UNIQUE,
                wallet_address TEXT NOT NULL,
                created_at INTEGER DEFAULT (strftime('%s','now'))
            )
        ''')
        
        # Удаляем encrypted_password если он существует (legacy migration)
        async with db.execute("PRAGMA table_info(wallets)") as cursor:
            columns = await cursor.fetchall()
            existing_columns = [col[1] for col in columns]
        
        # Note: SQLite doesn't support DROP COLUMN easily, so we'll just ignore it
        
        # Создаём таблицу для отслеживания отправленных транзакций
        # State tracking for idempotency and restart safety
        await db.execute('''
            CREATE TABLE IF NOT EXISTS sent_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                plan_id INTEGER,
                order_id TEXT NOT NULL,
                network_key TEXT NOT NULL,
                approve_tx_hash TEXT,
                transfer_tx_hash TEXT,
                amount REAL NOT NULL,
                deposit_address TEXT NOT NULL,
                state TEXT DEFAULT 'scheduled',
                error_message TEXT,
                sent_at INTEGER DEFAULT (strftime('%s','now')),
                FOREIGN KEY(plan_id) REFERENCES dca_plans(id)
            )
        ''')
        
        # Migrate sent_transactions table to add state and error_message columns if missing
        async with db.execute("PRAGMA table_info(sent_transactions)") as cursor:
            columns = await cursor.fetchall()
            existing_columns = [col[1] for col in columns]
        
        if "state" not in existing_columns:
            await db.execute("ALTER TABLE sent_transactions ADD COLUMN state TEXT DEFAULT 'scheduled'")
        if "error_message" not in existing_columns:
            await db.execute("ALTER TABLE sent_transactions ADD COLUMN error_message TEXT")

        # Safe migration: ensure transfer_tx_hash is nullable for pre-send records
        async with db.execute("PRAGMA table_info(sent_transactions)") as cursor:
            tx_columns = await cursor.fetchall()
        transfer_col = next((col for col in tx_columns if col[1] == "transfer_tx_hash"), None)
        transfer_notnull = bool(transfer_col and transfer_col[3] == 1)
        if transfer_notnull:
            logger.info("Migrating sent_transactions: transfer_tx_hash NOT NULL -> NULL")
            await db.execute("PRAGMA foreign_keys=off;")
            try:
                await db.execute("BEGIN IMMEDIATE;")
                await db.execute('''
                    CREATE TABLE sent_transactions_new (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER NOT NULL,
                        plan_id INTEGER,
                        order_id TEXT NOT NULL,
                        network_key TEXT NOT NULL,
                        approve_tx_hash TEXT,
                        transfer_tx_hash TEXT,
                        amount REAL NOT NULL,
                        deposit_address TEXT NOT NULL,
                        state TEXT DEFAULT 'scheduled',
                        error_message TEXT,
                        sent_at INTEGER DEFAULT (strftime('%s','now')),
                        FOREIGN KEY(plan_id) REFERENCES dca_plans(id)
                    )
                ''')
                await db.execute('''
                    INSERT INTO sent_transactions_new (
                        id, user_id, plan_id, order_id, network_key, approve_tx_hash,
                        transfer_tx_hash, amount, deposit_address, state, error_message, sent_at
                    )
                    SELECT
                        id, user_id, plan_id, order_id, network_key, approve_tx_hash,
                        transfer_tx_hash, amount, deposit_address, state, error_message, sent_at
                    FROM sent_transactions
                ''')
                await db.execute("DROP TABLE sent_transactions")
                await db.execute("ALTER TABLE sent_transactions_new RENAME TO sent_transactions")
                await db.execute("COMMIT;")
            except Exception:
                await db.execute("ROLLBACK;")
                raise
            finally:
                await db.execute("PRAGMA foreign_keys=on;")
        
        # Создаём таблицу для отслеживания завершённых ордеров
        await db.execute('''
            CREATE TABLE IF NOT EXISTS completed_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                order_id TEXT NOT NULL UNIQUE,
                btc_txid TEXT,
                notified INTEGER DEFAULT 0,
                completed_at INTEGER,
                FOREIGN KEY(user_id) REFERENCES dca_plans(user_id)
            )
        ''')

        await db.execute(
            "DELETE FROM sent_transactions "
            "WHERE order_id IS NOT NULL AND id NOT IN ("
            "  SELECT MIN(id) FROM sent_transactions WHERE order_id IS NOT NULL GROUP BY order_id"
            ")"
        )
        await db.commit()
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_sent_transactions_order_id "
            "ON sent_transactions(order_id)"
        )
        await db.commit()
        
        await db.commit()
    logger.info("База данных инициализирована")


# ============================================================================
# DCA SCHEDULER - автоматическое выполнение планов
# ============================================================================

async def dca_scheduler():
    """
    Фоновая задача для автоматического выполнения DCA планов.
    Проверяет планы при старте и далее каждую минуту.
    Пропущенные циклы за пределами execution window помечаются как skipped и не исполняются.
    """
    logger.info("DCA Scheduler запущен")
    
    while True:
        try:
            now = int(time.time())
            
            async with aiosqlite.connect(DB_PATH) as db:
                # Получаем все активные планы, которые пора выполнить (с ID!)
                # Только НЕ удаленные планы
                async with db.execute(
                    "SELECT id, user_id, from_asset, amount, interval_hours, btc_address, next_run "
                    "FROM dca_plans WHERE active = 1 AND deleted = 0 AND next_run <= ?",
                    (now,)
                ) as cursor:
                    plans = await cursor.fetchall()
                
                for plan in plans:
                    plan_id, user_id, from_asset, amount, interval_hours, btc_address, next_run = plan
                    plan_number = await get_plan_display_number(user_id, plan_id)
                    plan_claimed = False
                    try:
                        scheduled_time_for_cycle = int(next_run) if next_run is not None else now
                    except (TypeError, ValueError):
                        scheduled_time_for_cycle = now
                    try:
                        interval_seconds = max(1, int(interval_hours) * 3600)
                    except (TypeError, ValueError):
                        interval_seconds = 24 * 3600

                    if next_run is not None and now > scheduled_time_for_cycle:
                        elapsed_seconds = now - scheduled_time_for_cycle
                        if elapsed_seconds >= interval_seconds:
                            scheduled_time_for_cycle += (elapsed_seconds // interval_seconds) * interval_seconds
                    
                    try:
                        async with db.execute(
                            "SELECT order_id, state FROM sent_transactions "
                            "WHERE plan_id = ? AND state IN ('sending', 'tx_pending', 'pending', 'blocked', 'transfering') "
                            "ORDER BY sent_at DESC LIMIT 1",
                            (plan_id,)
                        ) as inflight_cur:
                            inflight_row = await inflight_cur.fetchone()
                        if inflight_row:
                            inflight_order_id, inflight_state = inflight_row
                            inflight_status = await get_fixedfloat_order_status_with_retry(inflight_order_id)
                            if inflight_status in SUCCESS_FIXEDFLOAT_ORDER_STATUSES:
                                await mark_order_completed(plan_id, inflight_order_id, f"fixedfloat_{inflight_status}")
                                continue
                            if inflight_status in FINAL_FIXEDFLOAT_ORDER_STATUSES:
                                logger.info("Order %s expired, clearing active order", inflight_order_id)
                                await mark_order_failed(plan_id, inflight_order_id, f"FixedFloat order {inflight_status}")
                            elif inflight_status == "":
                                logger.info(
                                    "Skip DCA plan_id=%s: in-flight order %s status unavailable after retries",
                                    plan_id, inflight_order_id
                                )
                                continue
                            else:
                                logger.info(f"Skip DCA plan_id={plan_id}: in-flight tx state={inflight_state} order={inflight_order_id}")
                                continue

                        # Проверяем нет ли уже активного ордера для этого плана
                        async with db.execute(
                            "SELECT active_order_id, active_order_expires FROM dca_plans WHERE id = ?",
                            (plan_id,)
                        ) as cur:
                            order_check = await cur.fetchone()
                        
                        if order_check:
                            existing_order_id, existing_order_expires = order_check
                            if existing_order_id:
                                if existing_order_expires and now > int(existing_order_expires):
                                    await notify_and_clear_expired_order(plan_id, existing_order_id)
                                    continue
                                ff_order_status = await get_fixedfloat_order_status_with_retry(existing_order_id)
                                if ff_order_status in SUCCESS_FIXEDFLOAT_ORDER_STATUSES:
                                    await mark_order_completed(plan_id, existing_order_id, f"fixedfloat_{ff_order_status}")
                                    continue
                                if ff_order_status in FINAL_FIXEDFLOAT_ORDER_STATUSES:
                                    logger.info("Order %s expired, clearing active order", existing_order_id)
                                    await mark_order_failed(plan_id, existing_order_id, f"FixedFloat order {ff_order_status}")
                                    existing_order_id = None
                                    existing_order_expires = None
                            if existing_order_id:
                                async with db.execute(
                                    "SELECT id, state, sent_at, approve_tx_hash, transfer_tx_hash, error_message, amount, deposit_address "
                                    "FROM sent_transactions "
                                    "WHERE order_id = ? AND plan_id = ? ORDER BY sent_at DESC LIMIT 1",
                                    (existing_order_id, plan_id)
                                ) as state_cur:
                                    state_row = await state_cur.fetchone()

                                if state_row:
                                    existing_tx_id, existing_state, last_attempt_time, existing_approve_tx, existing_transfer_tx, existing_error, existing_amount, existing_deposit_address = state_row
                                    if existing_state == 'approve_confirmed':
                                        claim_cur = await db.execute(
                                            "UPDATE sent_transactions SET state='transfering' WHERE id=? AND state='approve_confirmed'",
                                            (existing_tx_id,)
                                        )
                                        await db.commit()
                                        if claim_cur.rowcount != 1:
                                            logger.info(f"Skip DCA plan_id={plan_id}: transfer claim not acquired for order {existing_order_id}")
                                            continue
                                        logger.info(f"Approve already confirmed for order {existing_order_id}; attempting transfer step")
                                        resume_state, resume_approve_tx, resume_transfer_tx, resume_error = await resume_transfer_after_approve(
                                            network_key=from_asset,
                                            user_id=user_id,
                                            btc_address=btc_address,
                                            order_id=existing_order_id,
                                            deposit_address=existing_deposit_address or "",
                                            required_amount=float(existing_amount or amount),
                                            existing_approve_tx=existing_approve_tx,
                                            plan_id=plan_id,
                                            order_expires=existing_order_expires,
                                            scheduled_time=scheduled_time_for_cycle,
                                            interval_hours=interval_hours,
                                        )
                                        if resume_state == "confirmed":
                                            await db.execute(
                                                "UPDATE sent_transactions SET state = 'confirmed', approve_tx_hash = ?, transfer_tx_hash = ?, error_message = NULL "
                                                "WHERE order_id = ? AND plan_id = ?",
                                                (resume_approve_tx, resume_transfer_tx, existing_order_id, plan_id)
                                            )
                                            new_next_run = now + (interval_hours * 3600)
                                            await db.execute(
                                                "UPDATE dca_plans SET next_run = ?, missed_count = 0 WHERE id = ?",
                                                (new_next_run, plan_id)
                                            )
                                        else:
                                            await db.execute(
                                                "UPDATE sent_transactions SET state = ?, approve_tx_hash = ?, transfer_tx_hash = ?, error_message = ? "
                                                "WHERE order_id = ? AND plan_id = ?",
                                                (resume_state, resume_approve_tx, resume_transfer_tx, resume_error, existing_order_id, plan_id)
                                            )
                                        await db.commit()
                                        continue
                                    if existing_state in ('pending', 'tx_pending', 'blocked'):
                                        pending_is_approve = (existing_error or "").startswith("APPROVE_TX_PENDING:")
                                        pending_tx_hash = existing_approve_tx if pending_is_approve else (existing_transfer_tx or existing_approve_tx)
                                        if not pending_tx_hash:
                                            if existing_state == 'blocked':
                                                await db.execute(
                                                    "UPDATE sent_transactions SET state = 'failed', error_message = ? "
                                                    "WHERE order_id = ? AND plan_id = ?",
                                                    ("Blocked state without tx hash", existing_order_id, plan_id)
                                                )
                                                await db.commit()
                                            logger.info(f"Skip DCA plan_id={plan_id}: no tx hash for state={existing_state} order {existing_order_id}")
                                            continue
                                        tx_status = await get_transfer_tx_status(from_asset, pending_tx_hash)
                                        if tx_status == "confirmed":
                                            if pending_is_approve:
                                                await db.execute(
                                                    "UPDATE sent_transactions SET state = 'approve_confirmed', error_message = NULL "
                                                    "WHERE id = ?",
                                                    (existing_tx_id,)
                                                )
                                                claim_cur = await db.execute(
                                                    "UPDATE sent_transactions SET state='transfering' WHERE id=? AND state='approve_confirmed'",
                                                    (existing_tx_id,)
                                                )
                                                await db.commit()
                                                if claim_cur.rowcount != 1:
                                                    logger.info(f"Skip DCA plan_id={plan_id}: transfer claim not acquired for order {existing_order_id}")
                                                    continue
                                                logger.info(f"Approve tx confirmed for order {existing_order_id}; starting transfer step")
                                                resume_state, resume_approve_tx, resume_transfer_tx, resume_error = await resume_transfer_after_approve(
                                                    network_key=from_asset,
                                                    user_id=user_id,
                                                    btc_address=btc_address,
                                                    order_id=existing_order_id,
                                                    deposit_address=existing_deposit_address or "",
                                                    required_amount=float(existing_amount or amount),
                                                    existing_approve_tx=existing_approve_tx,
                                                    plan_id=plan_id,
                                                    order_expires=existing_order_expires,
                                                    scheduled_time=scheduled_time_for_cycle,
                                                    interval_hours=interval_hours,
                                                )
                                                if resume_state == "confirmed":
                                                    await db.execute(
                                                        "UPDATE sent_transactions SET state = 'confirmed', approve_tx_hash = ?, transfer_tx_hash = ?, error_message = NULL "
                                                        "WHERE order_id = ? AND plan_id = ?",
                                                        (resume_approve_tx, resume_transfer_tx, existing_order_id, plan_id)
                                                    )
                                                    new_next_run = now + (interval_hours * 3600)
                                                    await db.execute(
                                                        "UPDATE dca_plans SET next_run = ?, missed_count = 0 WHERE id = ?",
                                                        (new_next_run, plan_id)
                                                    )
                                                else:
                                                    await db.execute(
                                                        "UPDATE sent_transactions SET state = ?, approve_tx_hash = ?, transfer_tx_hash = ?, error_message = ? "
                                                        "WHERE order_id = ? AND plan_id = ?",
                                                        (resume_state, resume_approve_tx, resume_transfer_tx, resume_error, existing_order_id, plan_id)
                                                    )
                                                await db.commit()
                                                continue
                                            await db.execute(
                                                "UPDATE sent_transactions SET state = 'confirmed', error_message = NULL "
                                                "WHERE order_id = ? AND plan_id = ?",
                                                (existing_order_id, plan_id)
                                            )
                                            new_next_run = now + (interval_hours * 3600)
                                            await db.execute(
                                                "UPDATE dca_plans SET next_run = ?, missed_count = 0 WHERE id = ?",
                                                (new_next_run, plan_id)
                                            )
                                            await db.commit()
                                            logger.info(f"Pending tx confirmed for order {existing_order_id}")
                                            continue
                                        if tx_status == "failed":
                                            await db.execute(
                                                "UPDATE sent_transactions SET state = 'failed', error_message = ? "
                                                "WHERE order_id = ? AND plan_id = ?",
                                                ("Transfer tx reverted on-chain", existing_order_id, plan_id)
                                            )
                                            await db.commit()
                                            logger.warning(f"Pending tx failed for order {existing_order_id}")
                                            continue
                                        await db.execute(
                                            "UPDATE sent_transactions SET state = 'tx_pending' WHERE order_id = ? AND plan_id = ?",
                                            (existing_order_id, plan_id)
                                        )
                                        await db.commit()
                                        logger.info(f"Skip DCA plan_id={plan_id}: tx status still pending for order {existing_order_id}")
                                        continue

                                    if existing_order_expires and existing_order_expires > now:
                                        if existing_state in ('sent', 'confirmed'):
                                            # Stale active order marker - clear and skip this cycle to avoid duplicates
                                            logger.warning(f"Active order {existing_order_id} already sent, clearing stale active order")
                                            await db.execute(
                                                "UPDATE dca_plans SET active_order_id = NULL, active_order_address = NULL, "
                                                "active_order_amount = NULL, active_order_expires = NULL, execution_state = 'scheduled' WHERE id = ?",
                                                (plan_id,)
                                            )
                                            await db.commit()
                                            continue
                                        elif existing_state == 'sending':
                                            # Order still being sent - wait
                                            logger.info(f"Skip DCA plan_id={plan_id}: order {existing_order_id} still sending")
                                            continue
                                        elif existing_state == 'blocked':
                                            logger.info(f"Skip DCA plan_id={plan_id}: blocked order {existing_order_id}, waiting for tx status resolution")
                                            continue
                                        elif existing_state == 'failed':
                                            # Stale active order marker - clear and skip this cycle to avoid duplicates
                                            logger.warning(f"Active order {existing_order_id} failed, clearing stale active order")
                                            await db.execute(
                                                "UPDATE dca_plans SET active_order_id = NULL, active_order_address = NULL, "
                                                "active_order_amount = NULL, active_order_expires = NULL, execution_state = 'scheduled' WHERE id = ?",
                                                (plan_id,)
                                            )
                                            await db.commit()
                                            continue
                                    else:
                                        fallback_result = await finalize_expired_unavailable_order(
                                            plan_id, existing_order_id, existing_order_expires, now
                                        )
                                        if fallback_result == "pending":
                                            logger.info(
                                                f"Skip DCA plan_id={plan_id}: order {existing_order_id} expired, tx status still pending"
                                            )
                                            continue
                                        if fallback_result == "completed":
                                            continue
                                elif existing_order_expires and existing_order_expires > now:
                                    # No transaction record yet - order exists but not attempted
                                    logger.info(f"Skip DCA plan_id={plan_id}: active order {existing_order_id} not yet attempted")
                                    continue
                                else:
                                    fallback_result = await finalize_expired_unavailable_order(
                                        plan_id, existing_order_id, existing_order_expires, now
                                    )
                                    if fallback_result == "pending":
                                        logger.info(
                                            f"Skip DCA plan_id={plan_id}: order {existing_order_id} expired, tx status still pending"
                                        )
                                        continue
                                    if fallback_result == "completed":
                                        continue
                        if next_run is not None and now > scheduled_time_for_cycle:
                            overdue_seconds = now - scheduled_time_for_cycle
                            if overdue_seconds > DCA_EXECUTION_WINDOW_SECONDS:
                                await skip_missed_dca_cycle(
                                    plan_id=plan_id,
                                    user_id=user_id,
                                    scheduled_time=scheduled_time_for_cycle,
                                    interval_hours=interval_hours,
                                )
                                continue

                        logger.info(f"Выполнение DCA для plan_id={plan_id}, user_id={user_id}: {amount} {from_asset}")
                        
                        # Проверяем лимиты перед созданием ордера
                        try:
                            limits = await get_fixedfloat_limits(from_asset)
                            min_limit = limits["min"]
                            max_limit = limits["max"]
                            effective_max = min(max_limit, 500.0)
                            
                            if amount < min_limit or amount > effective_max:
                                logger.warning(f"Сумма {amount} вне лимитов для {from_asset}: min={min_limit:.2f}, max={effective_max:.2f}")
                                # Отправляем уведомление пользователю
                                await bot.send_message(
                                    user_id,
                                    f"❌ Ошибка выполнения DCA плана:\n\n"
                                    f"Сумма {format_amount(amount)} USDT вне допустимых лимитов для {from_asset}\n"
                                    f"Минимум: {format_amount(min_limit)} USDT\n"
                                    f"Максимум: {format_amount(effective_max)} USDT\n\n"
                                    f"💡 Обнови план с корректной суммой"
                                )
                                # Откладываем на следующий интервал
                                new_next_run = now + (interval_hours * 3600)
                                await db.execute(
                                    "UPDATE dca_plans SET next_run = ? WHERE id = ?",
                                    (new_next_run, plan_id)
                                )
                                await db.commit()
                                continue
                        except RuntimeError as e:
                            error_msg = str(e)
                            logger.error(f"Ошибка проверки лимитов для plan_id={plan_id}: {e}")
                            # Если сеть недоступна, пропускаем этот запуск
                            if "недоступна" in error_msg.lower() or "311" in error_msg or "312" in error_msg:
                                await bot.send_message(
                                    user_id,
                                    f"⚠️ Сеть {from_asset} недоступна на FixedFloat в данный момент\n\n"
                                    f"План будет повторён через {interval_hours}ч"
                                )
                                new_next_run = now + (interval_hours * 3600)
                                await db.execute(
                                    "UPDATE dca_plans SET next_run = ? WHERE id = ?",
                                    (new_next_run, plan_id)
                                )
                                await db.commit()
                                continue
                        
                        plan_claimed = await claim_plan_execution(plan_id)
                        if not plan_claimed:
                            logger.info(f"Skip DCA plan_id={plan_id}: atomic claim not acquired")
                            continue

                        # Создаём ордер на обмен
                        order_data = await asyncio.to_thread(
                            create_fixedfloat_order,
                            from_asset,
                            amount,
                            btc_address
                        )
                        
                        order_id = order_data.get("id")
                        from_obj = order_data.get("from", {}) or {}
                        deposit_code = from_obj.get("code")
                        deposit_address = from_obj.get("address")
                        deposit_amount = from_obj.get("amount")
                        
                        # Получаем timestamp истечения ордера из ответа FixedFloat.
                        order_expires = extract_order_expires_at(order_data)
                        
                        # ВАЖНО: Сохраняем активный ордер в БД для предотвращения дубликатов
                        await db.execute(
                            "UPDATE dca_plans SET active_order_id = ?, active_order_address = ?, "
                            "active_order_amount = ?, active_order_expires = ?, order_expired_notified = 0, execution_state = 'scheduled', "
                            "last_execution_attempt_at = ? WHERE id = ?",
                            (order_id, deposit_address, f"{deposit_amount} {deposit_code}", order_expires, now, plan_id)
                        )
                        await db.commit()
                        plan_claimed = False
                        
                        # Проверяем есть ли настроенный кошелёк для автоматической отправки (single wallet)
                        async with db.execute(
                            "SELECT wallet_address FROM wallets WHERE user_id = ?",
                            (user_id,)
                        ) as cur:
                            wallet_row = await cur.fetchone()
                        
                        # Проверяем есть ли пароль в памяти (user_id key)
                        wallet_password = _wallet_passwords.get(user_id)
                        
                        if wallet_row and wallet_password:
                            
                            # Парсим сумму из строки "amount code"
                            try:
                                required_amount = float(deposit_amount)
                            except:
                                required_amount = amount  # Fallback to plan amount
                            
                            # Create transaction record in 'sending' state BEFORE attempting send
                            await db.execute(
                                "INSERT INTO sent_transactions (user_id, plan_id, order_id, network_key, amount, deposit_address, state) VALUES (?, ?, ?, ?, ?, ?, 'sending')",
                                (user_id, plan_id, order_id, from_asset, required_amount, deposit_address)
                            )
                            await db.commit()
                            
                            progress_msg = await bot.send_message(
                                user_id,
                                build_order_state_message(
                                    status_title="⏳ Ожидается оплата",
                                    plan_number=plan_number,
                                    order_id=order_id,
                                    amount=required_amount,
                                    network_key=from_asset,
                                    btc_address=btc_address,
                                    payment_address=deposit_address,
                                    deadline_text=format_order_deadline(order_expires),
                                    reason_text="",
                                ),
                                parse_mode="HTML",
                                disable_web_page_preview=True,
                            )
                            track_order_progress_message(str(order_id), int(user_id), int(progress_msg.message_id))
                            
                            if not await claim_auto_send_execution(plan_id, order_id):
                                continue

                            if is_order_expired(order_expires):
                                await mark_order_expired_before_send(
                                    plan_id=plan_id,
                                    user_id=user_id,
                                    order_id=order_id,
                                    scheduled_time=scheduled_time_for_cycle,
                                    interval_hours=interval_hours,
                                )
                                continue

                            # Автоматическая отправка USDT
                            try:
                                success, approve_tx, transfer_tx, error_msg = await auto_send_usdt(
                                    network_key=from_asset,
                                    user_id=user_id,
                                    wallet_password=wallet_password,
                                    deposit_address=deposit_address,
                                    required_amount=required_amount,
                                    btc_address=btc_address,
                                    order_id=order_id,
                                    dry_run=DRY_RUN
                                )
                                if approve_tx or transfer_tx:
                                    _balances_cache.clear()
                            except Exception as send_error:
                                # RPC/Network error - mark as blocked, don't advance schedule
                                error_str = str(send_error)
                                logger.error(f"RPC/Network error during auto-send: {error_str}")
                                human_error = humanize_auto_send_error(error_str, from_asset)
                                
                                # Check if it's a retryable error (RPC, timeout, connection)
                                is_retryable = is_retryable_network_error(error_str)
                                
                                if is_retryable:
                                    # Mark as blocked - will retry when DCA interval reached
                                    await db.execute(
                                        "UPDATE sent_transactions SET state = 'blocked', error_message = ? WHERE order_id = ? AND plan_id = ?",
                                        (error_str[:500], order_id, plan_id)
                                    )
                                    await db.commit()
                                    
                                    await bot.send_message(
                                        user_id,
                                        build_order_state_message(
                                            status_title="⚠️ Требуется проверка выплаты",
                                            plan_number=plan_number,
                                            order_id=order_id,
                                            amount=required_amount,
                                            network_key=from_asset,
                                            btc_address=btc_address,
                                            deadline_text=format_order_deadline(order_expires),
                                            reason_text=human_error,
                                        ),
                                        parse_mode="HTML",
                                        disable_web_page_preview=True,
                                    )
                                    # DO NOT advance schedule - will retry
                                    continue
                                else:
                                    # Non-retryable error - mark as failed, advance schedule
                                    await db.execute(
                                        "UPDATE sent_transactions SET state = 'failed', error_message = ? WHERE order_id = ? AND plan_id = ?",
                                        (error_str[:500], order_id, plan_id)
                                    )
                                    await db.commit()

                                    if is_order_expired(order_expires):
                                        await mark_order_expired_before_send(
                                            plan_id=plan_id,
                                            user_id=user_id,
                                            order_id=order_id,
                                            scheduled_time=scheduled_time_for_cycle,
                                            interval_hours=interval_hours,
                                            manual_send_blocked=True,
                                        )
                                        continue
                                    if is_insufficient_auto_send_error(error_str):
                                        await record_plan_skip_metadata(plan_id, "insufficient", now)

                                    await bot.send_message(
                                        user_id,
                                        build_auto_send_failed_notification(
                                            order_id=order_id,
                                            plan_number=plan_number,
                                            network_key=from_asset,
                                            required_amount=required_amount,
                                            btc_address=btc_address,
                                            deposit_address=deposit_address,
                                            order_expires=order_expires,
                                            error_msg=error_str,
                                        ),
                                        parse_mode="HTML",
                                        disable_web_page_preview=True,
                                    )
                                    # Advance schedule for failed transactions
                                    new_next_run = now + (interval_hours * 3600)
                                    await db.execute(
                                        "UPDATE dca_plans SET next_run = ? WHERE id = ?",
                                        (new_next_run, plan_id)
                                    )
                                    await db.commit()
                                    continue
                            
                            if success:
                                # Update transaction record with hashes and 'sent' state
                                await db.execute(
                                    "UPDATE sent_transactions SET approve_tx_hash = ?, transfer_tx_hash = ?, state = 'sent' WHERE order_id = ? AND plan_id = ?",
                                    (approve_tx, transfer_tx, order_id, plan_id)
                                )
                                await db.commit()
                                
                                msg = (
                                    build_order_state_message(
                                        status_title="✅ Оплата получена",
                                        plan_number=plan_number,
                                        order_id=order_id,
                                        amount=required_amount,
                                        network_key=from_asset,
                                        btc_address=btc_address,
                                        payment_address=deposit_address,
                                        reason_text="USDT отправлены. Ожидаем перевод BTC.",
                                    )
                                )
                                
                                if DRY_RUN:
                                    msg += f"\n⚠️ DRY RUN MODE - transactions not broadcast"
                                
                                await update_order_progress_message(int(user_id), str(order_id), msg)
                                
                                logger.info(f"Auto-send successful: order_id={order_id}, approve_tx={approve_tx}, transfer_tx={transfer_tx}")
                                
                                # Advance schedule ONLY on successful send
                                new_next_run = now + (interval_hours * 3600)
                                await db.execute(
                                    "UPDATE dca_plans SET next_run = ?, missed_count = 0 WHERE id = ?",
                                    (new_next_run, plan_id)
                                )
                                await db.commit()
                            else:
                                # Check if error is retryable
                                is_retryable = is_retryable_network_error(error_msg)
                                human_error = humanize_auto_send_error(error_msg, from_asset)
                                if is_pending_tx_error(error_msg):
                                    pending_tx_hash = approve_tx if error_msg.startswith("APPROVE_TX_PENDING:") else transfer_tx
                                    await db.execute(
                                        "UPDATE sent_transactions SET approve_tx_hash = ?, transfer_tx_hash = ?, state = 'tx_pending', error_message = ? "
                                        "WHERE order_id = ? AND plan_id = ?",
                                        (approve_tx, transfer_tx, error_msg[:500], order_id, plan_id)
                                    )
                                    await db.commit()
                                    await bot.send_message(
                                        user_id,
                                        build_order_state_message(
                                            status_title="⚠️ Требуется проверка выплаты",
                                            plan_number=plan_number,
                                            order_id=order_id,
                                            amount=required_amount,
                                            network_key=from_asset,
                                            btc_address=btc_address,
                                            reason_text="Транзакция отправлена, но подтверждение сети ещё не получено.",
                                        ),
                                        parse_mode="HTML",
                                        disable_web_page_preview=True,
                                    )
                                    continue
                                
                                if is_retryable:
                                    if transfer_tx or approve_tx:
                                        pending_tx_hash = transfer_tx or approve_tx
                                        await db.execute(
                                            "UPDATE sent_transactions SET state = 'tx_pending', approve_tx_hash = ?, transfer_tx_hash = ?, error_message = ? "
                                            "WHERE order_id = ? AND plan_id = ?",
                                            (approve_tx, transfer_tx, error_msg[:500], order_id, plan_id)
                                        )
                                        await db.commit()
                                        await bot.send_message(
                                            user_id,
                                            build_order_state_message(
                                                status_title="⚠️ Требуется проверка выплаты",
                                                plan_number=plan_number,
                                                order_id=order_id,
                                                amount=required_amount,
                                                network_key=from_asset,
                                                btc_address=btc_address,
                                                reason_text="Транзакция отправлена, но подтверждение сети ещё не получено.",
                                            ),
                                            parse_mode="HTML",
                                            disable_web_page_preview=True,
                                        )
                                    else:
                                        # No tx hash available; keep blocked for manual/scheduler retry policy
                                        await db.execute(
                                            "UPDATE sent_transactions SET state = 'blocked', error_message = ? WHERE order_id = ? AND plan_id = ?",
                                            (error_msg[:500], order_id, plan_id)
                                        )
                                        await db.commit()
                                        await bot.send_message(
                                            user_id,
                                            build_order_state_message(
                                                status_title="⚠️ Требуется проверка выплаты",
                                                plan_number=plan_number,
                                                order_id=order_id,
                                                amount=required_amount,
                                                network_key=from_asset,
                                                btc_address=btc_address,
                                                deadline_text=format_order_deadline(order_expires),
                                                reason_text=human_error,
                                            ),
                                            parse_mode="HTML",
                                            disable_web_page_preview=True,
                                        )
                                    continue
                                else:
                                    # Non-retryable error - mark as failed
                                    await db.execute(
                                        "UPDATE sent_transactions SET state = 'failed', error_message = ? WHERE order_id = ? AND plan_id = ?",
                                        (error_msg[:500], order_id, plan_id)
                                    )
                                    await db.commit()

                                    if is_order_expired(order_expires):
                                        await mark_order_expired_before_send(
                                            plan_id=plan_id,
                                            user_id=user_id,
                                            order_id=order_id,
                                            scheduled_time=scheduled_time_for_cycle,
                                            interval_hours=interval_hours,
                                            manual_send_blocked=True,
                                        )
                                        continue
                                    if is_insufficient_auto_send_error(error_msg):
                                        await record_plan_skip_metadata(plan_id, "insufficient", now)

                                    error_notification = build_auto_send_failed_notification(
                                        order_id=order_id,
                                        plan_number=plan_number,
                                        network_key=from_asset,
                                        required_amount=required_amount,
                                        btc_address=btc_address,
                                        deposit_address=deposit_address,
                                        order_expires=order_expires,
                                        error_msg=error_msg,
                                    )
                                    await bot.send_message(
                                        user_id,
                                        error_notification,
                                        parse_mode="HTML",
                                        disable_web_page_preview=True,
                                    )
                                    logger.error(f"Auto-send failed for order {order_id}: {error_msg}")
                                    
                                    # Advance schedule ONLY for failed (non-retryable) errors
                                    new_next_run = now + (interval_hours * 3600)
                                    await db.execute(
                                        "UPDATE dca_plans SET next_run = ? WHERE id = ?",
                                        (new_next_run, plan_id)
                                    )
                                    await db.commit()
                        else:
                            # Wallet not configured - ask to send manually
                            if is_order_expired(order_expires):
                                await mark_order_expired_before_send(
                                    plan_id=plan_id,
                                    user_id=user_id,
                                    order_id=order_id,
                                    scheduled_time=scheduled_time_for_cycle,
                                    interval_hours=interval_hours,
                                    manual_send_blocked=True,
                                )
                                continue
                            await bot.send_message(
                                user_id,
                                build_order_payment_notification(
                                    order_id=order_id,
                                    plan_number=plan_number,
                                    network_key=from_asset,
                                    amount=deposit_amount,
                                    btc_address=btc_address,
                                    deposit_address=deposit_address,
                                    order_expires=order_expires,
                                    action_text="Оплатите ордер на указанный адрес.",
                                ),
                                parse_mode="HTML",
                                disable_web_page_preview=True,
                            )
                            # Advance schedule for manual send case (order created, user notified)
                            new_next_run = now + (interval_hours * 3600)
                            await db.execute(
                                "UPDATE dca_plans SET next_run = ? WHERE id = ?",
                                (new_next_run, plan_id)
                            )
                            await db.commit()
                        
                        logger.info(f"DCA execution completed for plan_id={plan_id}, user_id={user_id}, order_id={order_id}")
                        
                    except Exception as e:
                        if plan_claimed:
                            await release_plan_claim(plan_id)
                        logger.error(f"Ошибка выполнения DCA для plan_id={plan_id}, user_id={user_id}: {e}")
                        # Отправляем уведомление об ошибке
                        try:
                            await bot.send_message(
                                user_id,
                                f"❌ Ошибка выполнения DCA плана:\n{escape_html(e)}\n\n"
                                f"План будет повторён через {interval_hours}ч",
                                parse_mode=None,
                            )
                        except:
                            pass
                        
                        # Откладываем на следующий интервал ТОЛЬКО для этого конкретного плана
                        new_next_run = now + (interval_hours * 3600)
                        await db.execute(
                            "UPDATE dca_plans SET next_run = ? WHERE id = ?",
                            (new_next_run, plan_id)
                        )
                        await db.commit()
                        
        except Exception as e:
            logger.error(f"Ошибка в DCA scheduler: {e}")
        await asyncio.sleep(60)  # проверка каждую минуту


# ============================================================================
# TELEGRAM КОМАНДЫ - обработчики команд от пользователей
# ============================================================================


@dp.message(Command("start"))
async def cmd_start(message: Message):
    """
    Команда /start - приветствие и список доступных команд.
    Первая команда, которую видит новый пользователь.
    """
    await message.answer(
        "👋 <b>Добро пожаловать в AutoDCA Cryptobotan Bot</b>\n\n"
        "Я создан для автоматической покупки BTC по стратегии DCA через FixedFloat.\n\n"

        "━━━━━━━━━━━━━━━━━━\n"
        "<b>🚀 Быстрый старт</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"

        "1️⃣ <b>Настрой кошелёк</b>\n\n"

        "Создай файл wallet.json со своим приватным ключом и выполни:\n\n"
        "/setwallet\n\n"

        "Приватный ключ будет зашифрован и сохранён в безопасном виде на твоем устройстве.\n\n"

        "После успешной настройки файл wallet.json рекомендуется удалить.\n\n"

        "2️⃣ <b>Создай DCA план</b>\n\n"
        "/setdca\n\n"

        "После этого бот будет выполнять покупки автоматически по заданному расписанию.\n\n"

        "━━━━━━━━━━━━━━━━━━\n"
        "<b>📌 Полезные команды</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"

        "📊 /status — планы и ордера\n"
        "🌐 /limits — сети и лимиты\n"
        "💼 /walletstatus — баланс\n"
        "📜 /history — история покупок\n"
        "ℹ️ /help — подробная справка\n\n"

        "━━━━━━━━━━━━━━━━━━\n"
        "<b>Created by @Cryptobotan</b>",
        parse_mode="HTML",
    )
    logger.info(f"Start command from user: {message.from_user.id}")


@dp.message(Command("help"))
async def cmd_help(message: Message):
    """
    Команда /help - подробная справка по использованию бота.
    """
    await message.answer(
    "ℹ️ <b>Справка AutoDCA Cryptobotan Bot</b>\n\n"

    "Автоматическая покупка BTC по стратегии DCA через FixedFloat.\n\n"

    "━━━━━━━━━━━━━━━━━━\n"
    "<b>⚙️ Как работает бот</b>\n"
    "━━━━━━━━━━━━━━━━━━\n\n"

    "1. Создаётся DCA план через /setdca\n"
    "2. По расписанию бот создаёт ордер на FixedFloat\n"
    "3. Бот автоматически отправляет USDT с EVM-кошелька на адрес депозита FixedFloat\n"
    "4. После получения USDT FixedFloat отправляет BTC на указанный адрес\n\n"

    "<b>Поддерживаемые сети:</b>\n\n"
    "• Arbitrum\n"
    "• Polygon\n"
    "• BSC\n\n"

    "<b>Интервалы покупок:</b>\n\n"
    "• Каждые 12 часов\n"
    "• Каждый день\n"
    "• Каждую неделю\n"
    "• Каждый месяц\n\n"

    "━━━━━━━━━━━━━━━━━━\n"
    "<b>📜 Команды</b>\n"
    "━━━━━━━━━━━━━━━━━━\n\n"

    "🔐 /setwallet - Добавить EVM-кошелёк\n"
    "➕ /setdca - Создать DCA план\n"
    "📊 /status - Планы и активные ордера\n"
    "🌐 /limits - Поддерживаемые сети и лимиты\n"
    "💼 /walletstatus - Баланс EVM-кошелька\n"
    "📜 /history - История покупок\n"
    "❌ /deletewallet - Удалить текущий кошелёк\n\n"

    "━━━━━━━━━━━━━━━━━━\n"
    "<b>🔒 Безопасность</b>\n"
    "━━━━━━━━━━━━━━━━━━\n\n"

    "Во время настройки через /setwallet приватный ключ шифруется и сохраняется на устройстве.\n\n"

    "После успешной настройки файл wallet.json рекомендуется удалить.\n\n"

    "━━━━━━━━━━━━━━━━━━\n"
    "<b>🔄 Замена EVM-кошелька</b>\n"
    "━━━━━━━━━━━━━━━━━━\n\n"

    "1. Выполни /deletewallet\n"
    "2. Вручную создай новый wallet.json\n"
    "3. Выполни /setwallet снова\n\n"

    "━━━━━━━━━━━━━━━━━━\n"
    "<b>Created by @Cryptobotan</b>",
    parse_mode="HTML",
)


@dp.message(Command("history"))
async def cmd_history(message: Message):
    """
    Команда /history - показать историю операций.
    """
    user_id = message.from_user.id
    
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT st.plan_id, st.order_id, st.network_key, st.amount, "
            "COALESCE(dp.btc_address, '') AS btc_address, "
            "COALESCE(co.btc_txid, '') AS btc_tx_hash, "
            "st.sent_at, co.completed_at "
            "FROM completed_orders co "
            "JOIN sent_transactions st ON st.order_id = co.order_id "
            "LEFT JOIN dca_plans dp ON st.plan_id = dp.id "
            "WHERE co.user_id = ? "
            "AND st.id = ("
            "  SELECT st2.id FROM sent_transactions st2 "
            "  WHERE st2.order_id = st.order_id ORDER BY st2.sent_at DESC LIMIT 1"
            ") "
            "ORDER BY co.completed_at DESC LIMIT 10",
            (user_id,)
        ) as cur:
            rows = await cur.fetchall()
    
    if not rows:
        await message.answer("История операций пуста.")
        return

    lines = ["📜 Последние завершённые операции:\n"]
    for idx, (plan_id, order_id, network_key, amount, btc_address, btc_tx_hash, created_at, completed_at) in enumerate(rows, start=1):
        created_str = format_datetime_utc(int(created_at or 0))
        completed_str = format_datetime_utc(int(completed_at or 0))
        normalized_network_key = normalize_network_key(str(network_key or ""))
        network_label = get_network_label(normalized_network_key) or normalized_network_key
        if btc_tx_hash:
            safe_tx_hash = html.escape(str(btc_tx_hash))
            tx_line = f'🔗 BTC транзакция: <a href="{escape_html(get_blockchair_url(safe_tx_hash))}">{escape_html(format_txid_short(safe_tx_hash))}</a>\n'
        else:
            tx_line = ""
        display_index = next(
            (
                display_idx
                for display_idx, mapped_plan_id in _user_plan_mapping.get(user_id, {}).items()
                if mapped_plan_id == plan_id
            ),
            None,
        )
        plan_line = f"План: {display_index} | " if display_index is not None else ""
        lines.append(
            f"{idx}. 🔗 Ордер: {format_order_link(order_id)}\n"
            f"{plan_line}Сеть: {network_label}\n"
            f"Сумма: {format_order_amount(amount, network_key=normalized_network_key)}\n"
            f"📥 Получатель BTC:\n{format_btc_recipient_copyable(btc_address)}\n"
            f"{tx_line}"
            f"Создан: {created_str}\n"
            f"Завершён: {completed_str}\n"
        )

    await message.answer(
        "\n".join(lines),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


@dp.message(Command("ping"))
async def cmd_ping(message: Message):
    """
    Команда /ping - проверка работоспособности бота.
    Показывает user_id для технической поддержки.
    """
    user_id = message.from_user.id
    await message.answer(
        f"✅ Бот работает!\n\n"
        f"👤 Твой user_id: {user_id}\n"
        f"🕐 Время сервера: {time.strftime('%Y-%m-%d %H:%M:%S')}"
    )


@dp.message(Command("limits"))
async def cmd_limits(message: Message):
    """
    Команда /limits - показать лимиты обмена для USDT -> BTC для всех сетей.
    """
    try:
        await message.answer("⏳ Получаю лимиты...")
        network_order = ["USDT-ARB", "USDT-BSC", "USDT-POLYGON"]
        bot_max = 500
        blocks = []

        for network_name in network_order:
            for attempt in range(2):
                try:
                    limits = await get_fixedfloat_limits(network_name)
                    min_value = float(limits["min"])
                    rate_value = limits.get("rate")
                    break
                except Exception as e:
                    logger.error(
                        f"Ошибка получения лимитов для {network_name}, попытка {attempt + 1}/2: {e}"
                    )
                    if attempt == 1:
                        min_value = None
                        rate_value = None

            if min_value is not None:
                rate_line = (
                    f"\nКурс: 1 BTC = {rate_value:,.2f} USDT"
                    if rate_value
                    else ""
                )
                blocks.append(
                    f"🟢 {network_name}\n"
                    f"Мин: {format_amount(min_value)} USDT\n"
                    f"Макс: {bot_max} USDT (ограничено ботом)"
                    f"{rate_line}"
                )
            else:
                blocks.append(
                    f"🔴 {network_name}\n"
                    f"Причина: Сеть временно недоступна"
                )

        limits_text = (
            "🌐 Лимиты и сети (USDT → BTC)\n\n"
            + "\n\n".join(blocks)
            + "\n\n💡 Данные обновляются в реальном времени"
        )
        await message.answer(limits_text, parse_mode=None)

    except Exception as e:
        logger.error(f"Ошибка получения лимитов: {e}")
        await message.answer(f"❌ Ошибка получения лимитов: {escape_html(e)}")


async def fetch_fixedfloat_available_networks() -> tuple[dict[str, str], bool]:
    available_networks: dict[str, str] = {}
    try:
        items = await ff_request_async("ccies", {})
        for item in items:
            logger.info(f"[FF RAW] item: {item}")
            if item.get("coin") == "USDT":
                code = normalize_code(item.get("code"))
                network = normalize_code(item.get("network"))
                if code:
                    available_networks[code] = network
        logger.info(f"[FF] available_networks: {available_networks}")
        return available_networks, True
    except Exception as api_error:
        logger.error(f"API FixedFloat недоступен: {api_error}")
        return available_networks, False


def is_network_available_on_fixedfloat(network_key: str, available_networks: dict[str, str]) -> bool:
    network_map = {
        "USDT-ARB": "USDTARBITRUM",
        "USDT-BSC": "USDTBSC",
        "USDT-POLYGON": "USDTMATIC",
    }
    candidates = {
        normalize_code(network_map.get(network_key)),
        normalize_code(FIXEDFLOAT_ASSET_MAP.get(network_key)),
    }
    candidates.discard("")
    if not candidates:
        return False
    for key, network in available_networks.items():
        normalized_key = normalize_code(key)
        normalized_network = normalize_code(network)
        for candidate in candidates:
            if (
                candidate == normalized_key
                or candidate in normalized_key
                or normalized_key in candidate
                or candidate in normalized_network
                or normalized_network in candidate
            ):
                return True
    return False


@dp.message(lambda message: message.text and re.fullmatch(r"/execute(?:_\d+|\s+\d+)?", message.text.strip()))
async def cmd_execute(message: Message):
    """
    Команда /execute или /execute_<index> - ручное выполнение обмена по DCA-плану.
    """
    user_id = message.from_user.id
    
    # Экранный индекс разрешается через mapping последнего /status.
    text = message.text.strip()
    requested_index = None
    
    if "_" in text:
        try:
            requested_index = int(text.split("_", 1)[1])
        except:
            pass
    elif " " in text:
        try:
            requested_index = int(text.split()[1])
        except:
            pass
    
    # Получаем список всех планов пользователя (в том же порядке что и в /status)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, from_asset, amount, interval_hours FROM dca_plans WHERE user_id = ? AND deleted = 0 ORDER BY id",
            (user_id,),
        ) as cur:
            plans = await cur.fetchall()
    
    if not plans:
        await message.answer(
            "❗️У тебя нет DCA-планов.\n\n"
            "Создай план командой:\n"
            "/setdca USDT-ARB 50 24 BTC_АДРЕС"
        )
        return
    
    if requested_index is None:
        if len(plans) == 1:
            plan_id = int(plans[0][0])
        else:
            await message.answer(
                "📋 Найдено несколько планов.\n\n"
                "Открой /status и выбери нужный план по его номеру."
            )
            return
    else:
        mapped_plan_id = await resolve_display_plan_id(message, requested_index)
        if mapped_plan_id is None:
            return
        plan_id = mapped_plan_id
    
    # Получаем конкретный план по ID (только не удаленные)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT from_asset, amount, interval_hours, btc_address, active_order_id, active_order_address, "
            "active_order_amount, active_order_expires, next_run "
            "FROM dca_plans WHERE id = ? AND user_id = ? AND deleted = 0",
            (plan_id, user_id)
        ) as cur:
            row = await cur.fetchone()
    
    if not row:
        await message.answer("❌ План не найден или не принадлежит тебе")
        return
    
    from_asset, amount, interval_hours, btc_address, active_order_id, active_order_address, active_order_amount, active_order_expires, plan_next_run = row
    plan_number = await get_plan_display_number(user_id, plan_id)
    plan_claimed = False

    now = int(time.time())
    if active_order_id and (not active_order_expires or int(active_order_expires) > now):
        existing_text = (
            f"⚠️ <b>Ордер {format_order_link(active_order_id)} уже создан и ожидает обработки.</b>\n\n"
            f"{build_status_hint()}"
        )
        progress_meta = _order_progress_messages.get(str(active_order_id))
        if progress_meta and progress_meta[0] == int(user_id):
            try:
                await bot.edit_message_text(
                    chat_id=int(user_id),
                    message_id=progress_meta[1],
                    text=existing_text,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
                return
            except Exception as e:
                logger.warning("Failed to refresh existing order message %s: %s", active_order_id, e)
        await message.answer(existing_text, parse_mode="HTML", disable_web_page_preview=True)
        return

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT order_id, state FROM sent_transactions "
            "WHERE plan_id = ? AND state IN ('sending', 'tx_pending', 'pending', 'blocked', 'transfering') "
            "ORDER BY sent_at DESC LIMIT 1",
            (plan_id,)
        ) as inflight_cur:
            inflight_row = await inflight_cur.fetchone()
    if inflight_row:
        inflight_order_id, inflight_state = inflight_row
        inflight_status = await get_fixedfloat_order_status_with_retry(inflight_order_id)
        if inflight_status in SUCCESS_FIXEDFLOAT_ORDER_STATUSES:
            await mark_order_completed(plan_id, inflight_order_id, f"fixedfloat_{inflight_status}")
            active_order_id = None
            active_order_address = None
            active_order_amount = None
            active_order_expires = None
        elif inflight_status in FINAL_FIXEDFLOAT_ORDER_STATUSES:
            logger.info("Order %s expired, clearing active order", inflight_order_id)
            await mark_order_failed(plan_id, inflight_order_id, f"FixedFloat order {inflight_status}")
            active_order_id = None
            active_order_address = None
            active_order_amount = None
            active_order_expires = None
        else:
            await message.answer(
                f"⚠️ Для плана #{plan_number} уже идёт предыдущая покупка.\n\n{build_status_hint()}",
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            return

    # Проверяем есть ли уже активный ордер для ЭТОГО конкретного плана
    now = int(time.time())
    if active_order_id:
        ff_order_status = await get_fixedfloat_order_status_with_retry(active_order_id)
        if ff_order_status in SUCCESS_FIXEDFLOAT_ORDER_STATUSES:
            await mark_order_completed(plan_id, active_order_id, f"fixedfloat_{ff_order_status}")
            active_order_id = None
            active_order_address = None
            active_order_amount = None
            active_order_expires = None
        elif ff_order_status in FINAL_FIXEDFLOAT_ORDER_STATUSES:
            logger.info("Order %s expired, clearing active order", active_order_id)
            await mark_order_failed(plan_id, active_order_id, f"FixedFloat order {ff_order_status}")
            active_order_id = None
            active_order_address = None
            active_order_amount = None
            active_order_expires = None

    if active_order_id:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT id, state, approve_tx_hash, transfer_tx_hash, error_message, amount, deposit_address FROM sent_transactions "
                "WHERE order_id = ? AND plan_id = ? ORDER BY sent_at DESC LIMIT 1",
                (active_order_id, plan_id)
            ) as cur:
                tx_state_row = await cur.fetchone()
        if tx_state_row and tx_state_row[1] in ("pending", "tx_pending", "blocked", "approve_confirmed"):
            tx_id = tx_state_row[0]
            tx_state = tx_state_row[1]
            approve_tx_hash = tx_state_row[2]
            transfer_tx_hash = tx_state_row[3]
            tx_error = tx_state_row[4]
            tx_amount = tx_state_row[5]
            tx_deposit_address = tx_state_row[6]

            if tx_state == "approve_confirmed":
                async with aiosqlite.connect(DB_PATH) as db:
                    claim_cur = await db.execute(
                        "UPDATE sent_transactions SET state='transfering' WHERE id=? AND state='approve_confirmed'",
                        (tx_id,)
                    )
                    await db.commit()
                if claim_cur.rowcount != 1:
                    await message.answer(
                        f"⚠️ Выплата по ордеру {format_order_link(active_order_id)} уже обрабатывается.\n\n{build_status_hint()}",
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                    return
                resume_state, resume_approve_tx, resume_transfer_tx, resume_error = await resume_transfer_after_approve(
                    network_key=from_asset,
                    user_id=user_id,
                    btc_address=btc_address,
                    order_id=active_order_id,
                    deposit_address=tx_deposit_address or "",
                    required_amount=float(tx_amount or amount),
                    existing_approve_tx=approve_tx_hash,
                    plan_id=plan_id,
                    order_expires=active_order_expires,
                    scheduled_time=plan_next_run,
                    interval_hours=interval_hours,
                )
                async with aiosqlite.connect(DB_PATH) as db:
                    if resume_state == "confirmed":
                        await db.execute(
                            "UPDATE sent_transactions SET state = 'confirmed', approve_tx_hash = ?, transfer_tx_hash = ?, error_message = NULL WHERE order_id = ? AND plan_id = ?",
                            (resume_approve_tx, resume_transfer_tx, active_order_id, plan_id)
                        )
                    else:
                        await db.execute(
                            "UPDATE sent_transactions SET state = ?, approve_tx_hash = ?, transfer_tx_hash = ?, error_message = ? WHERE order_id = ? AND plan_id = ?",
                            (resume_state, resume_approve_tx, resume_transfer_tx, resume_error, active_order_id, plan_id)
                        )
                    await db.commit()
                if resume_state == "confirmed":
                    await message.answer(
                        f"✅ Оплата по ордеру {format_order_link(active_order_id)} подтверждена.\n\n{build_status_hint()}",
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                elif resume_state == "tx_pending":
                    await message.answer(
                        f"⚠️ Выплата по ордеру {format_order_link(active_order_id)} ещё проверяется.\n\n{build_status_hint()}",
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                elif resume_state == "expired":
                    return
                else:
                    await message.answer(
                        f"⚠️ Выплата по ордеру {format_order_link(active_order_id)} требует проверки.\n\n{build_status_hint()}",
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                return

            pending_is_approve = (tx_error or "").startswith("APPROVE_TX_PENDING:")
            pending_tx_hash = approve_tx_hash if pending_is_approve else (transfer_tx_hash or approve_tx_hash)
            if not pending_tx_hash:
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute(
                        "UPDATE sent_transactions SET state = 'failed', error_message = ? WHERE order_id = ? AND plan_id = ?",
                        ("Blocked state without tx hash", active_order_id, plan_id)
                    )
                    await db.commit()
                await message.answer(
                    f"⚠️ Для ордера {format_order_link(active_order_id)} требуется ручная проверка выплаты.\n\n{build_status_hint()}",
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
                return
            tx_status = await get_transfer_tx_status(from_asset, pending_tx_hash)
            if tx_status == "pending":
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute(
                        "UPDATE sent_transactions SET state = 'tx_pending' WHERE order_id = ? AND plan_id = ?",
                        (active_order_id, plan_id)
                    )
                    await db.commit()
                await message.answer(
                    f"⚠️ Выплата по ордеру {format_order_link(active_order_id)} ещё проверяется.\n\n{build_status_hint()}",
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
                return
            if tx_status == "confirmed":
                if pending_is_approve:
                    async with aiosqlite.connect(DB_PATH) as db:
                        await db.execute(
                            "UPDATE sent_transactions SET state = 'approve_confirmed', error_message = NULL WHERE order_id = ? AND plan_id = ?",
                            (active_order_id, plan_id)
                        )
                        claim_cur = await db.execute(
                            "UPDATE sent_transactions SET state='transfering' WHERE id=? AND state='approve_confirmed'",
                            (tx_id,)
                        )
                        await db.commit()
                    if claim_cur.rowcount != 1:
                        await message.answer(
                            f"⚠️ Выплата по ордеру {format_order_link(active_order_id)} уже обрабатывается.\n\n{build_status_hint()}",
                            parse_mode="HTML",
                            disable_web_page_preview=True,
                        )
                        return
                    resume_state, resume_approve_tx, resume_transfer_tx, resume_error = await resume_transfer_after_approve(
                        network_key=from_asset,
                        user_id=user_id,
                        btc_address=btc_address,
                        order_id=active_order_id,
                        deposit_address=tx_deposit_address or "",
                        required_amount=float(tx_amount or amount),
                        existing_approve_tx=approve_tx_hash,
                        plan_id=plan_id,
                        order_expires=active_order_expires,
                        scheduled_time=plan_next_run,
                        interval_hours=interval_hours,
                    )
                    async with aiosqlite.connect(DB_PATH) as db:
                        if resume_state == "confirmed":
                            await db.execute(
                                "UPDATE sent_transactions SET state = 'confirmed', approve_tx_hash = ?, transfer_tx_hash = ?, error_message = NULL WHERE order_id = ? AND plan_id = ?",
                                (resume_approve_tx, resume_transfer_tx, active_order_id, plan_id)
                            )
                        else:
                            await db.execute(
                                "UPDATE sent_transactions SET state = ?, approve_tx_hash = ?, transfer_tx_hash = ?, error_message = ? WHERE order_id = ? AND plan_id = ?",
                                (resume_state, resume_approve_tx, resume_transfer_tx, resume_error, active_order_id, plan_id)
                            )
                        await db.commit()
                    if resume_state == "confirmed":
                        await message.answer(
                            f"✅ Оплата по ордеру {format_order_link(active_order_id)} подтверждена.\n\n{build_status_hint()}",
                            parse_mode="HTML",
                            disable_web_page_preview=True,
                        )
                    elif resume_state == "tx_pending":
                        await message.answer(
                            f"⚠️ Выплата по ордеру {format_order_link(active_order_id)} ещё проверяется.\n\n{build_status_hint()}",
                            parse_mode="HTML",
                            disable_web_page_preview=True,
                        )
                    elif resume_state == "expired":
                        return
                    else:
                        await message.answer(
                            f"⚠️ Выплата по ордеру {format_order_link(active_order_id)} требует проверки.\n\n{build_status_hint()}",
                            parse_mode="HTML",
                            disable_web_page_preview=True,
                        )
                    return
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute(
                        "UPDATE sent_transactions SET state = 'confirmed', error_message = NULL WHERE order_id = ? AND plan_id = ?",
                        (active_order_id, plan_id)
                    )
                    await db.commit()
                await message.answer(
                    f"✅ Оплата по ордеру {format_order_link(active_order_id)} подтверждена.\n\n{build_status_hint()}",
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
                return
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "UPDATE sent_transactions SET state = 'failed', error_message = ? WHERE order_id = ? AND plan_id = ?",
                    ("Transfer tx reverted on-chain", active_order_id, plan_id)
                )
                await db.commit()

    if active_order_id and active_order_expires and active_order_expires > now:
        # У этого плана уже есть активный неистёкший ордер
        await message.answer(
            build_order_payment_notification(
                order_id=active_order_id,
                plan_number=plan_number,
                network_key=from_asset,
                amount=active_order_amount,
                btc_address=btc_address,
                deposit_address=active_order_address,
                order_expires=active_order_expires,
                action_text="Дождитесь завершения текущего ордера.",
            ),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return
    elif active_order_id and active_order_expires and active_order_expires <= now:
        fallback_result = await finalize_expired_unavailable_order(plan_id, active_order_id, active_order_expires, now)
        if fallback_result == "pending":
            await message.answer(
                f"⚠️ По ордеру {format_order_link(active_order_id)} ещё проверяется выплата.\n\n{build_status_hint()}",
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            return

    try:
        # Проверяем лимиты перед созданием ордера
        try:
            limits = await get_fixedfloat_limits(from_asset)
            min_limit = limits["min"]
            max_limit = limits["max"]
            
            # Ограничиваем максимальный лимит бота (500 USD)
            effective_max = min(max_limit, 500.0)
            
            if amount < min_limit:
                await message.answer(
                    f"❌ Сумма меньше минимального лимита FixedFloat\n\n"
                    f"Минимальная сумма для {from_asset}: {format_amount(min_limit)} USDT\n"
                    f"Сумма в плане: {format_amount(amount)} USDT\n\n"
                    f"💡 Создай новый план с суммой от {format_amount(min_limit)} USDT"
                )
                return
            
            if amount > effective_max:
                await message.answer(
                    f"❌ Сумма больше максимального лимита\n\n"
                    f"Максимальная сумма для {from_asset}: {format_amount(effective_max)} USDT\n"
                    f"Сумма в плане: {format_amount(amount)} USDT\n\n"
                    f"💡 Создай новый план с суммой до {format_amount(effective_max)} USDT"
                )
                return
            
            logger.info(f"Лимиты для {from_asset}: min={min_limit:.2f}, max={effective_max:.2f}, amount={amount:.2f}")
        except RuntimeError as e:
            error_msg = str(e)
            if "недоступна" in error_msg.lower() or "311" in error_msg or "312" in error_msg:
                await message.answer(
                    f"❌ Сеть {from_asset} недоступна на FixedFloat в данный момент\n\n"
                    f"Попробуй позже или выбери другую сеть"
                )
            else:
                await message.answer(
                    f"❌ Не удалось проверить лимиты для {from_asset}\n\n"
                    f"Ошибка: {escape_html(error_msg)}\n\n"
                    f"Попробуй позже"
                )
            return
        
        plan_claimed = await claim_plan_execution(plan_id, user_id)
        if not plan_claimed:
            await message.answer("⚠️ План уже выполняется другим процессом. Повтори через несколько секунд.")
            return

        progress_msg = await message.answer(f"⏳ Создаю ордер {from_asset} на FixedFloat...")
        
        # Создаём ордер через универсальную функцию
        data = await asyncio.to_thread(
            create_fixedfloat_order,
            from_asset,
            amount,
            btc_address
        )

        if not data or not isinstance(data, dict):
            if plan_claimed:
                await release_plan_claim(plan_id)
                plan_claimed = False
            await bot.edit_message_text(
                chat_id=int(user_id),
                message_id=progress_msg.message_id,
                text=f"❌ Неожиданный ответ FixedFloat: {escape_html(data)}",
                parse_mode="HTML",
            )
            return

        # Парсим ответ
        order_id = data.get("id")
        from_obj = data.get("from", {}) or {}
        deposit_code = from_obj.get("code")
        deposit_amount = from_obj.get("amount")
        deposit_address = from_obj.get("address")
        if order_id:
            track_order_progress_message(str(order_id), int(user_id), int(progress_msg.message_id))
        
        # Сохраняем информацию об активном ордере в БД
        order_expires = extract_order_expires_at(data)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE dca_plans SET active_order_id = ?, active_order_address = ?, "
                "active_order_amount = ?, active_order_expires = ?, order_expired_notified = 0, execution_state = 'scheduled', "
                "last_execution_attempt_at = ? WHERE id = ?",
                (order_id, deposit_address, f"{deposit_amount} {deposit_code}", order_expires, int(time.time()), plan_id)
            )
            await db.commit()
            plan_claimed = False
            
            # Проверяем есть ли настроенный кошелёк для автоматической отправки
            async with db.execute(
                "SELECT wallet_address FROM wallets WHERE user_id = ?",
                (user_id,)
            ) as cur:
                wallet_row = await cur.fetchone()
            
            # Проверяем есть ли пароль в памяти
            wallet_password = _wallet_passwords.get(user_id)
        
        if wallet_row and wallet_password:
            
            # Парсим сумму из строки "amount code"
            try:
                required_amount = float(deposit_amount)
            except:
                required_amount = amount  # Fallback to plan amount

            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "INSERT INTO sent_transactions (user_id, plan_id, order_id, network_key, amount, deposit_address, state) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'sending')",
                    (user_id, plan_id, order_id, from_asset, required_amount, deposit_address)
                )
                await db.commit()
            
            await update_order_progress_message(
                int(user_id),
                str(order_id),
                build_order_state_message(
                    status_title="⏳ Ожидается оплата",
                    plan_number=plan_number,
                    order_id=order_id,
                    amount=required_amount,
                    network_key=from_asset,
                    btc_address=btc_address,
                    payment_address=deposit_address,
                    deadline_text=format_order_deadline(order_expires),
                    reason_text="",
                ),
            )
            
            if not await claim_auto_send_execution(plan_id, order_id):
                return

            if is_order_expired(order_expires):
                await mark_order_expired_before_send(
                    plan_id=plan_id,
                    user_id=user_id,
                    order_id=order_id,
                    scheduled_time=plan_next_run,
                    interval_hours=interval_hours,
                )
                return

            # Автоматическая отправка USDT
            success, approve_tx, transfer_tx, error_msg = await auto_send_usdt(
                network_key=from_asset,
                user_id=user_id,
                wallet_password=wallet_password,
                deposit_address=deposit_address,
                required_amount=required_amount,
                btc_address=btc_address,
                order_id=order_id,
                dry_run=DRY_RUN
            )
            if approve_tx or transfer_tx:
                _balances_cache.clear()
            
            if success:
                # Сохраняем информацию о транзакции
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute(
                        "UPDATE sent_transactions SET approve_tx_hash = ?, transfer_tx_hash = ?, state = 'sent' "
                        "WHERE order_id = ? AND plan_id = ?",
                        (approve_tx, transfer_tx, order_id, plan_id)
                    )
                    await db.execute(
                        "UPDATE dca_plans SET missed_count = 0 WHERE id = ?",
                        (plan_id,)
                    )
                    await db.commit()
                
                msg = (
                    build_order_state_message(
                        status_title="✅ Оплата получена",
                        plan_number=plan_number,
                        order_id=order_id,
                        amount=required_amount,
                        network_key=from_asset,
                        btc_address=btc_address,
                        payment_address=deposit_address,
                        reason_text="USDT отправлены. Ожидаем перевод BTC.",
                    )
                )
                
                if DRY_RUN:
                    msg += f"\n⚠️ DRY RUN MODE - транзакции не были отправлены"
                
                await update_order_progress_message(int(user_id), str(order_id), msg)
                
                logger.info(f"Auto-send successful: order_id={order_id}, approve_tx={approve_tx}, transfer_tx={transfer_tx}")
            else:
                async with aiosqlite.connect(DB_PATH) as db:
                    if is_pending_tx_error(error_msg):
                        await db.execute(
                            "UPDATE sent_transactions SET approve_tx_hash = ?, transfer_tx_hash = ?, state = 'tx_pending', error_message = ? "
                            "WHERE order_id = ? AND plan_id = ?",
                            (approve_tx, transfer_tx, error_msg[:500], order_id, plan_id)
                        )
                    elif is_retryable_network_error(error_msg):
                        if approve_tx or transfer_tx:
                            await db.execute(
                                "UPDATE sent_transactions SET approve_tx_hash = ?, transfer_tx_hash = ?, state = 'tx_pending', error_message = ? "
                                "WHERE order_id = ? AND plan_id = ?",
                                (approve_tx, transfer_tx, error_msg[:500], order_id, plan_id)
                            )
                        else:
                            await db.execute(
                                "UPDATE sent_transactions SET approve_tx_hash = ?, transfer_tx_hash = ?, state = 'blocked', error_message = ? "
                                "WHERE order_id = ? AND plan_id = ?",
                                (approve_tx, transfer_tx, error_msg[:500], order_id, plan_id)
                            )
                    else:
                        await db.execute(
                            "UPDATE sent_transactions SET approve_tx_hash = ?, transfer_tx_hash = ?, state = 'failed', error_message = ? "
                            "WHERE order_id = ? AND plan_id = ?",
                            (approve_tx, transfer_tx, error_msg[:500], order_id, plan_id)
                        )
                    await db.commit()

                if is_pending_tx_error(error_msg):
                    pending_tx_hash = approve_tx if error_msg.startswith("APPROVE_TX_PENDING:") else transfer_tx
                    await message.answer(
                        build_order_state_message(
                            status_title="⚠️ Требуется проверка выплаты",
                            plan_number=plan_number,
                            order_id=order_id,
                            amount=required_amount,
                            network_key=from_asset,
                            btc_address=btc_address,
                            reason_text="Транзакция отправлена, но подтверждение сети ещё не получено.",
                        ),
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                    return
                if is_retryable_network_error(error_msg) and (approve_tx or transfer_tx):
                    pending_tx_hash = transfer_tx or approve_tx
                    await message.answer(
                        build_order_state_message(
                            status_title="⚠️ Требуется проверка выплаты",
                            plan_number=plan_number,
                            order_id=order_id,
                            amount=required_amount,
                            network_key=from_asset,
                            btc_address=btc_address,
                            reason_text="Транзакция отправлена, но подтверждение сети ещё не получено.",
                        ),
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                    return

                # Ошибка автоматической отправки - уведомляем пользователя
                if is_order_expired(order_expires):
                    await mark_order_expired_before_send(
                        plan_id=plan_id,
                        user_id=user_id,
                        order_id=order_id,
                        scheduled_time=plan_next_run,
                        interval_hours=interval_hours,
                        manual_send_blocked=True,
                    )
                    return
                if is_insufficient_auto_send_error(error_msg):
                    await record_plan_skip_metadata(plan_id, "insufficient")
                error_notification = build_auto_send_failed_notification(
                    order_id=order_id,
                    plan_number=plan_number,
                    network_key=from_asset,
                    required_amount=required_amount,
                    btc_address=btc_address,
                    deposit_address=deposit_address,
                    order_expires=order_expires,
                    error_msg=error_msg,
                )
                await message.answer(
                    error_notification,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
                logger.error(f"Auto-send failed for order {order_id}: {error_msg}")
        else:
            # Кошелёк не настроен - просим отправить вручную
            if is_order_expired(order_expires):
                await mark_order_expired_before_send(
                    plan_id=plan_id,
                    user_id=user_id,
                    order_id=order_id,
                    scheduled_time=plan_next_run,
                    interval_hours=interval_hours,
                    manual_send_blocked=True,
                )
                return
            await update_order_progress_message(
                int(user_id),
                str(order_id),
                build_order_payment_notification(
                    order_id=order_id,
                    plan_number=plan_number,
                    network_key=from_asset,
                    amount=deposit_amount,
                    btc_address=btc_address,
                    deposit_address=deposit_address,
                    order_expires=order_expires,
                    action_text="Оплатите ордер на указанный адрес.",
                ),
            )
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "UPDATE dca_plans SET missed_count = 0 WHERE id = ?",
                    (plan_id,)
                )
                await db.commit()
        
        logger.info(f"Ручной ордер создан: user_id={user_id}, plan_id={plan_id}, order_id={order_id}")
        
    except Exception as e:
        if plan_claimed:
            await release_plan_claim(plan_id)
        logger.error(f"Ошибка создания ордера для user_id={user_id}: {e}")
        await message.answer(f"❌ Ошибка при создании ордера:\n{escape_html(e)}")

@dp.message(Command("status"))
async def cmd_status(message: Message):
    """
    Команда /status - показать все DCA планы пользователя.
    Отображает все планы с деталями и временем следующего запуска.
    """
    user_id = message.from_user.id
    
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, from_asset, amount, interval_hours, btc_address, next_run, active, "
            "active_order_id, active_order_address, active_order_amount, active_order_expires "
            "FROM dca_plans WHERE user_id = ? AND deleted = 0 ORDER BY id", 
            (user_id,)
        ) as cursor:
            plans = await cursor.fetchall()
    
    if not plans:
        _user_plan_mapping[user_id] = {}
        await message.answer(
            "📋 У тебя нет DCA планов\n\n"
            "Создай план командой:\n"
            "/setdca USDT-ARB 50 24 BTC_АДРЕС"
        )
        return
    
    # Вычисляем текущее время
    now = int(time.time())
    transfer_hash_by_order = {}
    state_by_order = {}
    error_by_order = {}
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT plan_id, order_id, transfer_tx_hash, state, error_message "
            "FROM sent_transactions WHERE user_id = ? ORDER BY sent_at DESC",
            (user_id,)
        ) as tx_cur:
            tx_rows = await tx_cur.fetchall()
    for tx_plan_id, tx_order_id, tx_hash, tx_state, tx_error in tx_rows:
        key = (tx_plan_id, tx_order_id)
        if key not in transfer_hash_by_order:
            transfer_hash_by_order[key] = tx_hash
            state_by_order[key] = tx_state
            error_by_order[key] = tx_error or ""
    
    status_text = f"📊 Твои DCA планы ({len(plans)}):\n\n"
    _user_plan_mapping[user_id] = {}
    
    # Экранные индексы существуют только в mapping последнего /status.
    for index, plan in enumerate(plans, start=1):
        plan_id, from_asset, amount, interval_hours, btc_address, next_run, active, \
        order_id, order_address, order_amount, order_expires = plan
        _user_plan_mapping[user_id][index] = int(plan_id)
        
        status_emoji = "🟢" if active else "🔴"
        status_name = "Активен" if active else "На паузе"
        
        btc_display = format_btc_recipient_copyable(btc_address)
        network_label = get_network_label(from_asset) or from_asset
        amount_compact = f"{format_amount(float(amount))} USDT / {interval_hours}ч"
        schedule_line = (
            "⏱ Следующая покупка: " + escape_html(format_next_buy_time(next_run, now))
            if active
            else "⏸ Покупки приостановлены"
        )
        
        status_text += (
            f"📌 План {index}\n"
            f"{status_emoji} {escape_html(network_label)} — {status_name}\n"
            f"💰 {amount_compact}\n"
            f"📥 Получатель BTC:\n{btc_display}\n"
            f"{schedule_line}\n"
        )
        
        # Проверяем есть ли активный ордер (и не истёк ли он)
        if active and order_id and order_expires:
            ff_order_status = await get_fixedfloat_order_status(order_id)
            if ff_order_status in SUCCESS_FIXEDFLOAT_ORDER_STATUSES:
                await mark_order_completed(plan_id, order_id, f"fixedfloat_{ff_order_status}")
                order_id = None
                order_address = None
                order_amount = None
                order_expires = None
            elif order_expires > now:
                # Ордер активен
                order_time_left = order_expires - now
                order_hours = order_time_left // 3600
                order_minutes = (order_time_left % 3600) // 60
                order_time_text = f"{order_hours}ч {order_minutes}мин" if order_hours > 0 else f"{order_minutes}мин"
                
                transfer_tx_hash = transfer_hash_by_order.get((plan_id, order_id))
                order_state = (state_by_order.get((plan_id, order_id)) or "").lower()
                order_error = error_by_order.get((plan_id, order_id)) or ""
                if transfer_tx_hash:
                    status_line = "✅ Оплата получена"
                elif "insufficient" in order_error.lower():
                    status_line = "⚠️ Недостаточно средств"
                elif order_state in {"failed", "blocked"}:
                    status_line = "⏳ Ожидает оплаты"
                else:
                    status_line = "⏳ Ожидает оплаты"
                formatted_order_amount = format_order_amount(order_amount, network_key=from_asset)
                amount_line = formatted_order_amount
                retry_line = (
                    f"\n🔄 Пополните баланс и выполните вручную:\n/execute_{index}\n"
                    if status_line == "⚠️ Недостаточно средств"
                    else ""
                )
                
                status_text += (
                    f"\n└─ 🔄 Активный ордер\n"
                    f"{status_line}\n"
                    f"   🔗 {format_order_link(order_id)}\n"
                    f"   💵 {amount_line}\n"
                    f"   📍 Адрес оплаты: {format_code_address(order_address or '—')}\n"
                    f"   ⏰ Срок оплаты: {escape_html(format_order_deadline(order_expires, now))}\n"
                    f"{retry_line}"
                )
            else:
                # Очистку и уведомление об истечении выполняет scheduler.
                pass

        plan_toggle_command = f"/pause_{index}" if active else f"/resume_{index}"
        status_text += (
            f"\n⚙️ /execute_{index} · {plan_toggle_command} · /delete_{index}\n\n"
        )
    
    await message.answer(
        status_text,
        parse_mode="HTML",
        disable_web_page_preview=("fixedfloat.com/order/" in status_text),
    )


@dp.message(lambda message: message.text and re.fullmatch(r"/pause(?:_\d+|\s+\d+)?", message.text.strip()))
async def cmd_pause(message: Message):
    """
    Команда /pause или /pause_<index> - приостановить автоматическое выполнение DCA плана.
    """
    user_id = message.from_user.id
    
    text = message.text.strip()
    plan_id = None
    display_index = None
    
    if "_" in text:
        try:
            display_index = int(text.split("_", 1)[1])
        except:
            pass
    elif " " in text:
        try:
            display_index = int(text.split()[1])
        except:
            pass
    if display_index is not None:
        plan_id = await resolve_display_plan_id(message, display_index)
        if plan_id is None:
            return
    
    async with aiosqlite.connect(DB_PATH) as db:
        if plan_id:
            async with db.execute(
                "SELECT id FROM dca_plans WHERE id = ? AND user_id = ? AND deleted = 0",
                (plan_id, user_id)
            ) as cur:
                plan_row = await cur.fetchone()
            
            if not plan_row:
                await message.answer(f"❌ План не найден.\n\n{build_status_hint()}")
                return
            
            # Приостанавливаем по ID
            await db.execute(
                "UPDATE dca_plans SET active = 0 WHERE id = ? AND user_id = ? AND deleted = 0",
                (plan_id, user_id)
            )
            plan_number = await get_plan_display_number(user_id, plan_id)
            msg = f"⏸ План #{plan_number} приостановлен"
        else:
            # Приостанавливаем все планы пользователя (только не удаленные)
            await db.execute(
                "UPDATE dca_plans SET active = 0 WHERE user_id = ? AND deleted = 0",
                (user_id,)
            )
            msg = "⏸ Все DCA планы приостановлены"
        
        await db.commit()
    _user_plan_mapping.pop(user_id, None)
    
    await message.answer(
        f"{msg}\n\n"
        "Автоматические покупки остановлены.\n"
        f"{build_status_hint()}"
    )
    if plan_id:
        logger.info(f"DCA план приостановлен: user_id={user_id}, plan_id={plan_id}")
    else:
        logger.info(f"Все DCA планы приостановлены: user_id={user_id}")


@dp.message(lambda message: message.text and re.fullmatch(r"/resume(?:_\d+|\s+\d+)?", message.text.strip()))
async def cmd_resume(message: Message):
    """
    Команда /resume или /resume_<index> - возобновить автоматическое выполнение DCA плана.
    """
    user_id = message.from_user.id
    
    text = message.text.strip()
    plan_id = None
    display_index = None
    
    if "_" in text:
        try:
            display_index = int(text.split("_", 1)[1])
        except:
            pass
    elif " " in text:
        try:
            display_index = int(text.split()[1])
        except:
            pass
    if display_index is not None:
        plan_id = await resolve_display_plan_id(message, display_index)
        if plan_id is None:
            return
    
    async with aiosqlite.connect(DB_PATH) as db:
        if plan_id:
            async with db.execute(
                "SELECT id FROM dca_plans WHERE id = ? AND user_id = ? AND deleted = 0",
                (plan_id, user_id)
            ) as cur:
                plan_row = await cur.fetchone()
            
            if not plan_row:
                await message.answer(f"❌ План не найден.\n\n{build_status_hint()}")
                return
            
            # Возобновляем по ID
            await db.execute(
                "UPDATE dca_plans SET active = 1 WHERE id = ? AND user_id = ? AND deleted = 0",
                (plan_id, user_id)
            )
            plan_number = await get_plan_display_number(user_id, plan_id)
            msg = f"▶️ План #{plan_number} возобновлён"
        else:
            # Возобновляем все планы пользователя (только не удаленные)
            await db.execute(
                "UPDATE dca_plans SET active = 1 WHERE user_id = ? AND deleted = 0",
                (user_id,)
            )
            msg = "▶️ Все DCA планы возобновлены"
        
        await db.commit()
    _user_plan_mapping.pop(user_id, None)
    
    await message.answer(
        f"{msg}\n\n"
        "Автоматические покупки снова активны.\n"
        f"{build_status_hint()}"
    )
    if plan_id:
        logger.info(f"DCA план возобновлён: user_id={user_id}, plan_id={plan_id}")
    else:
        logger.info(f"Все DCA планы возобновлены: user_id={user_id}")


@dp.message(lambda message: message.text and re.fullmatch(r"/delete(?:_\d+|\s+\d+)?", message.text.strip()))
async def cmd_delete(message: Message):
    """
    Команда /delete_<index> - удалить DCA план полностью.
    """
    user_id = message.from_user.id
    
    text = message.text.strip()
    plan_id = None
    display_index = None
    
    if "_" in text:
        try:
            display_index = int(text.split("_", 1)[1])
        except:
            pass
    elif " " in text:
        try:
            display_index = int(text.split()[1])
        except:
            pass
    if display_index is not None:
        plan_id = await resolve_display_plan_id(message, display_index)
        if plan_id is None:
            return
    
    if plan_id is None:
        await message.answer(
            f"❌ Не удалось определить план для удаления.\n\n{build_status_hint()}"
        )
        return
    
    async with aiosqlite.connect(DB_PATH) as db:
        # Проверяем что план существует и принадлежит пользователю (только не удаленные)
        async with db.execute(
            "SELECT from_asset, active_order_id, active_order_expires FROM dca_plans WHERE id = ? AND user_id = ? AND deleted = 0",
            (plan_id, user_id)
        ) as cur:
            row = await cur.fetchone()
        
        if not row:
            await message.answer("❌ План не найден или не принадлежит тебе")
            return
        
        from_asset, active_order_id, active_order_expires = row
        
        # Проверяем есть ли активный ордер и предупреждаем пользователя
        if active_order_id and active_order_expires:
            now = int(time.time())
            if active_order_expires > now:
                # Ордер еще действителен - помечаем план как удаленный (НЕ удаляем!)
                # Это сохраняет информацию об активном ордере для предотвращения дубликатов
                # Помечаем план как удаленный (мягкое удаление)
                await db.execute(
                    "UPDATE dca_plans SET deleted = 1, active = 0 WHERE id = ? AND user_id = ?",
                    (plan_id, user_id)
                )
                await db.commit()
                _user_plan_mapping.pop(user_id, None)
                
                await message.answer(
                    f"🗑 План {from_asset} удалён\n\n"
                    f"⚠️ У этого плана был активный ордер:\n"
                    f"🔗 Ордер: {format_order_link(active_order_id)}\n"
                    f"⏰ Срок оплаты: {format_order_deadline(active_order_expires, now)}\n\n"
                    f"💡 Ордер остаётся активным на FixedFloat.\n"
                    f"Дождитесь завершения или истечения.\n\n"
                    f"{build_status_hint()}",
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
                logger.info(f"DCA план с активным ордером помечен как удаленный: user_id={user_id}, plan_id={plan_id}, asset={from_asset}, order_id={active_order_id}")
                return
        
        # Удаляем план без активного ордера (можно удалить физически)
        await db.execute(
            "UPDATE dca_plans SET deleted = 1, active = 0 WHERE id = ? AND user_id = ?",
            (plan_id, user_id)
        )
        await db.commit()
    _user_plan_mapping.pop(user_id, None)
    
    await message.answer(
        f"🗑 План {from_asset} удалён\n\n"
        f"{build_status_hint()}"
    )
    logger.info(f"DCA план удалён: user_id={user_id}, plan_id={plan_id}, asset={from_asset}")


@dp.message(Command("setwallet"))
async def cmd_setwallet(message: Message):
    """
    Команда /setwallet - настроить единый EVM кошелёк (NO ARGUMENTS).
    
    Читает wallet.json из корня проекта:
    {
      "private_key": "0xYOUR_PRIVATE_KEY",
      "password": "STRONG_PASSWORD"
    }
    
    Создаёт keystore, сохраняет пароль в keyring, перезаписывает wallet.json.
    """
    user_id = message.from_user.id
    
    # Check if keystore already exists
    if keystore_exists(user_id):
        await message.answer(
            "❌ Кошелёк уже инициализирован\n\n"
            "Если нужно сбросить кошелёк:\n"
            "1. Останови бота\n"
            "2. Удали файл keystore вручную\n"
            "3. Перезапусти бота\n"
            "4. Создай новый wallet.json\n"
            "5. Запусти /setwallet"
        )
        return
    
    # Read wallet.json from project root
    wallet_json_path = os.path.join(BASE_DIR, "wallet.json")
    if not os.path.exists(wallet_json_path):
        await message.answer(
            "❌ wallet.json не найден\n\n"
            "Создай wallet.json в папке с ботом:\n\n"
            "```json\n"
            "{\n"
            '  "private_key": "0xYOUR_PRIVATE_KEY",\n'
            '  "password": "YOUR_PASSWORD"\n'
            "}\n"
            "```\n\n"
            "Затем запусти /setwallet снова",
            parse_mode="Markdown"
        )
        return
    
    try:
        with open(wallet_json_path, "r", encoding="utf-8") as f:
            wallet_data = json.load(f)
        
        private_key = wallet_data.get("private_key")
        password = wallet_data.get("password")
        
        if not private_key or not password:
            await message.answer(
                "❌ Неверный формат wallet.json\n\n"
                "Обязательные поля:\n"
                "• private_key\n"
                "• password"
            )
            return
        
        # Validate private key format
        if not private_key.startswith("0x"):
            private_key = "0x" + private_key
        
        # Create Ethereum keystore using eth_account
        from eth_account import Account
        account = Account.from_key(private_key)
        wallet_address = account.address
        
        # Encrypt to create keystore (v3)
        keystore = account.encrypt(password)
        
        # Save keystore using existing storage logic
        save_keystore(keystore, user_id)
        
        # Store password in OS keyring (single source of truth)
        save_password_to_keyring(user_id, password)
        
        # Populate in-memory cache
        _wallet_passwords[user_id] = password
        
        # Delete private_key from memory explicitly
        private_key = None
        del private_key
        
        # Overwrite wallet.json to contain ONLY keystore
        with open(wallet_json_path, "w", encoding="utf-8") as f:
            json.dump({"keystore": keystore}, f, indent=2)
        
        # Save wallet address to database
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute('''
                INSERT OR REPLACE INTO wallets (user_id, wallet_address)
                VALUES (?, ?)
            ''', (user_id, wallet_address))
            await db.commit()
        
        await message.answer(
            f"✅ Кошелёк инициализирован успешно!\n\n"
            f"📍 Адрес: {format_code_address(wallet_address)}\n\n"
            f"🔐 Безопасность:\n"
            f"• Wallet.json используется только для первичного импорта\n"
            f"• Приватный ключ зашифрован и сохранён в keystore\n\n"
            f"⚠️ УДАЛИ все резервные копии wallet.json с приватным ключом!\n\n"
            f"💡 Автоотправка активирована для всех сетей",
            parse_mode="HTML"
        )
        
        logger.info(f"Wallet initialized for user {user_id}: address={wallet_address}")
    
    except Exception as e:
        logger.error(f"Error in cmd_setwallet: {e}", exc_info=True)
        await message.answer(f"❌ Ошибка: {escape_html(e)}")


async def fetch_network_status(network_key: str, wallet_address: str):
    from web3 import Web3

    network_key = normalize_network_key(network_key)
    config = get_network_config(network_key)
    gas_token = config["native_token"]
    rpc_timeout = 10
    chain_id_timeout = 8
    balance_timeout = 10
    cache_key = (wallet_address.lower(), network_key)
    cached = _balances_cache.get(cache_key)
    if cached:
        age = time.time() - cached["ts"]
        if age < CACHE_TTL:
            return copy.deepcopy(cached["data"])

    w3 = _web3_cache.get(network_key)
    connect_error = None
    balance_error = False
    usdt_balance = None
    native_balance = None
    usdt_error = False
    native_error = False

    try:
        if w3 is not None:
            try:
                cached_chain_id = await asyncio.wait_for(
                    asyncio.to_thread(lambda: int(w3.eth.chain_id)),
                    timeout=chain_id_timeout
                )
                if cached_chain_id != int(config["chain_id"]):
                    raise RuntimeError(
                        f"Cached chain ID mismatch: expected {config['chain_id']}, got {cached_chain_id}"
                    )
                await asyncio.wait_for(
                    asyncio.to_thread(lambda: int(w3.eth.block_number)),
                    timeout=chain_id_timeout
                )
            except Exception as e:
                logger.warning("walletstatus cached web3 invalid for %s: %s", network_key, e)
                _web3_cache.pop(network_key, None)
                w3 = None

        for attempt in range(1, 4):
            if w3 is not None:
                connect_error = None
                break
            try:
                w3 = await asyncio.wait_for(
                    asyncio.to_thread(get_web3_instance, network_key),
                    timeout=rpc_timeout
                )
                chain_id = await asyncio.wait_for(
                    asyncio.to_thread(lambda: int(w3.eth.chain_id)),
                    timeout=chain_id_timeout
                )
                if chain_id != int(config["chain_id"]):
                    raise RuntimeError(
                        f"Chain ID mismatch: expected {config['chain_id']}, got {chain_id}"
                    )
                await asyncio.wait_for(
                    asyncio.to_thread(lambda: int(w3.eth.block_number)),
                    timeout=chain_id_timeout
                )
                chain_id_debug = await asyncio.to_thread(lambda: w3.eth.chain_id)
                logger.info(f"[RPC] {network_key} chain_id={chain_id_debug}")
                _web3_cache.setdefault(network_key, w3)
                connect_error = None
                break
            except Exception as e:
                connect_error = e
                logger.warning(
                    "walletstatus RPC attempt %s/3 failed for %s: %s",
                    attempt, network_key, e
                )
                if attempt < 3:
                    await asyncio.sleep(1.0)

        if connect_error or w3 is None:
            return {
                "name": config["name"],
                "gas_token": gas_token,
                "usdt_text": "—",
                "native_text": "—",
                "rpc_error": True,
                "balance_error": False,
                "has_usdt": False,
            }

        logger.info(f"[BALANCE] {network_key} address: {wallet_address}")
        try:
            checksum = Web3.to_checksum_address(wallet_address)
        except Exception:
            checksum = wallet_address
        logger.info(f"[BALANCE] {network_key} checksum: {checksum}")

        try:
            usdt_balance = await asyncio.wait_for(
                asyncio.to_thread(get_usdt_balance, w3, network_key, wallet_address),
                timeout=balance_timeout
            )
            if usdt_balance is not None and usdt_balance == 0:
                await asyncio.sleep(0.3)
                usdt_balance = await asyncio.wait_for(
                    asyncio.to_thread(get_usdt_balance, w3, network_key, wallet_address),
                    timeout=balance_timeout
                )
            logger.info(f"[BALANCE] {network_key} usdt raw: {usdt_balance}")
        except Exception as e:
            logger.error(f"Error getting USDT balance for {network_key}: {e}")
            usdt_error = True
            logger.info(f"[BALANCE] {network_key} usdt raw: {usdt_balance}")

        try:
            native_balance = await asyncio.wait_for(
                asyncio.to_thread(get_native_balance, w3, wallet_address),
                timeout=balance_timeout
            )
            if native_balance is not None and native_balance == 0:
                await asyncio.sleep(0.3)
                native_balance = await asyncio.wait_for(
                    asyncio.to_thread(get_native_balance, w3, wallet_address),
                    timeout=balance_timeout
                )
            logger.info(f"[BALANCE] {network_key} native raw: {native_balance}")
        except Exception as e:
            logger.error(f"Error getting native balance for {network_key}: {e}")
            native_error = True
            logger.info(f"[BALANCE] {network_key} native raw: {native_balance}")

        balance_error = usdt_error or native_error

        if usdt_balance is None:
            usdt_text = "— ⚠️ Нет данных"
        elif usdt_balance < 1e-6:
            usdt_text = f"{format_balance(usdt_balance)} ⚠️ Недостаточно"
        else:
            usdt_text = format_balance(usdt_balance)

        if native_balance is None:
            native_text = "— ⚠️ Нет данных"
        elif native_balance < 1e-8:
            native_text = f"{format_native_balance(native_balance)} ⚠️ Нет газа"
        else:
            native_text = format_native_balance(native_balance)

        result = {
            "name": config["name"],
            "gas_token": gas_token,
            "usdt_text": usdt_text,
            "native_text": native_text,
            "rpc_error": False,
            "balance_error": balance_error,
            "has_usdt": usdt_balance is not None and usdt_balance > 1e-6,
            "usdt_balance": usdt_balance,
            "native_balance": native_balance,
        }
        if not connect_error and not balance_error:
            _balances_cache[cache_key] = {
                "data": result,
                "ts": time.time()
            }
        return result
    except Exception as e:
        logger.error(f"Unexpected walletstatus error for {network_key}: {e}")
        return {
            "name": config["name"],
            "gas_token": gas_token,
            "usdt_text": "—",
            "native_text": "—",
            "rpc_error": True,
            "balance_error": False,
            "has_usdt": False,
        }


@dp.message(Command("walletstatus"))
async def cmd_walletstatus(message: Message):
    """
    Команда /walletstatus - показать статус кошелька и балансы на всех сетях.
    """
    user_id = message.from_user.id
    
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT wallet_address FROM wallets WHERE user_id = ?",
            (user_id,)
        ) as cursor:
            wallet_row = await cursor.fetchone()
    
    if not wallet_row:
        await message.answer(
            "📋 Wallet not configured\n\n"
            "Setup your wallet:\n"
            "/setwallet"
        )
        return
    
    from web3 import Web3

    wallet_address = wallet_row[0]
    if not Web3.is_address(wallet_address):
        await message.answer("<b>❌ Invalid wallet address</b>", parse_mode="HTML")
        return

    network_order = ["USDT-ARB", "USDT-BSC", "USDT-POLYGON"]
    semaphore = asyncio.Semaphore(3)

    async def _fetch_with_limit(network_key: str):
        async with semaphore:
            try:
                return await fetch_network_status(network_key, wallet_address)
            except Exception as e:
                logger.error("walletstatus failed for %s: %s", network_key, e)
                fallback = {
                    "USDT-ARB": ("Arbitrum", "ETH"),
                    "USDT-BSC": ("BSC", "BNB"),
                    "USDT-POLYGON": ("Polygon", "MATIC"),
                }
                name, gas_token = fallback[network_key]
                return {
                    "name": name,
                    "gas_token": gas_token,
                    "rpc_error": True,
                    "balance_error": False,
                    "usdt_balance": None,
                    "native_balance": None,
                }

    async def _fetch_minimum(network_key: str) -> Optional[float]:
        try:
            return float((await get_fixedfloat_limits(network_key))["min"])
        except Exception as e:
            logger.warning("walletstatus FixedFloat minimum unavailable for %s: %s", network_key, e)
            return None

    results, minimums = await asyncio.gather(
        asyncio.gather(*[_fetch_with_limit(network_key) for network_key in network_order]),
        asyncio.gather(*[_fetch_minimum(network_key) for network_key in network_order]),
        return_exceptions=False
    )

    blocks = []
    for network_key, result, ff_minimum in zip(network_order, results, minimums):
        name = escape_html(result["name"])
        gas_token = escape_html(result["gas_token"])
        rpc_error = result["rpc_error"]
        balance_error = result["balance_error"]

        if rpc_error:
            blocks.append(
                f"🌐 <b>{name}</b>\n"
                f"💵 USDT: —\n"
                f"⛽ {gas_token}: —\n"
                "<b>❌ RPC недоступен</b>"
            )
            continue

        if balance_error:
            blocks.append(
                f"🌐 <b>{name}</b>\n"
                f"💵 USDT: —\n"
                f"⛽ {gas_token}: —\n"
                "<b>⚠️ Ошибка получения баланса</b>"
            )
            continue

        usdt_balance = result.get("usdt_balance")
        native_balance = result.get("native_balance")
        usdt_text = format_balance(float(usdt_balance)) if usdt_balance is not None else "—"
        native_text = format_native_balance(float(native_balance)) if native_balance is not None else "—"
        if usdt_balance is not None and ff_minimum is not None and float(usdt_balance) < ff_minimum:
            usdt_line = f"💵 USDT: {usdt_text} ⚠️ (Минимум {format_amount(ff_minimum)} USDT)"
        else:
            usdt_line = f"💵 USDT: {usdt_text}"

        gas_required = WALLETSTATUS_MIN_NATIVE_GAS.get(network_key)
        gas_warning = (
            " ⚠️ Недостаточно газа"
            if (
                native_balance is not None
                and (
                    (gas_required is not None and float(native_balance) < gas_required)
                    or float(native_balance) <= 0
                )
            )
            else ""
        )
        blocks.append(
            f"🌐 <b>{name}</b>\n"
            f"{usdt_line}\n"
            f"⛽ {gas_token}: {native_text}{gas_warning}"
        )

    status_text = (
        "💼 <b>Статус кошелька</b>\n\n"
        "📍 <b>Адрес:</b>\n"
        f"{format_code_address(wallet_address)}\n"
        "━━━━━━━━━━━━━━\n"
        "<b>Баланс:</b>\n\n"
        + "\n\n━━━━━━━━━━━━━━\n".join(blocks)
    )
    
    await message.answer(status_text, parse_mode="HTML")



@dp.message(Command("deletewallet"))
async def cmd_deletewallet(message: Message):
    """
    Команда /deletewallet - удалить кошелёк пользователя.
    Формат: /deletewallet (no arguments)
    """
    user_id = message.from_user.id
    
    # Удаляем из БД и файловой системы
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM wallets WHERE user_id = ?",
            (user_id,)
        )
        await db.commit()
    
    deleted = delete_keystore(user_id)
    
    # Очищаем пароль из keyring и памяти
    delete_password_from_keyring(user_id)
    if user_id in _wallet_passwords:
        del _wallet_passwords[user_id]
    
    if deleted:
        await message.answer(
            f"✅ Wallet deleted\n\n"
            f"• Keystore file removed from disk\n"
            f"• Password removed from keyring\n"
            f"• Auto-send disabled"
        )
    else:
        await message.answer(
            f"✅ Wallet deleted from database\n\n"
            f"• Keystore file not found (may have been already deleted)\n"
            f"• Password removed from keyring\n"
            f"• Auto-send disabled"
        )
    
    logger.info(f"Wallet deleted: user_id={user_id}")


@dp.message(Command("setdca"))
async def cmd_setdca(message: Message):
    """
    Команда /setdca - создать или обновить DCA план.
    Формат: /setdca СЕТЬ СУММА ИНТЕРВАЛ BTC_АДРЕС
    
    Параметры:
    - СЕТЬ: USDT-ARB, USDT-BSC, USDT-POLYGON
    - СУММА: 10-500 USD
    - ИНТЕРВАЛ: 12, 24, 168, 720 (часов)
    - BTC_АДРЕС: валидный Bitcoin адрес
    """
    args = message.text.split()[1:]
    
    if len(args) != 4:
        await message.answer(
            "❌ Неверный формат\n\n"
            "Используй:\n"
            "/setdca СЕТЬ СУММА ИНТЕРВАЛ BTC_АДРЕС\n\n"
            "Примеры:\n"
            "/setdca USDT-ARB 50 24 BTC_АДРЕС\n"
            "/setdca USDT-BSC 100 168 BTC_АДРЕС\n"
            "/setdca USDT-POLYGON 75 24 BTC_АДРЕС\n\n"
            "Интервалы:\n"
            "12 - раз в 12 часов\n"
            "24 - раз в день\n"
            "168 - раз в неделю\n"
            "720 - раз в месяц\n\n"
            "Подробнее: /help"
        )
        return
    
    try:
        from_asset, amount_str, interval_str, btc_address = args
        
        # Нормализация названия сети
        from_asset = from_asset.upper().replace("_", "-")
        amount = float(amount_str)
        interval = int(interval_str)
        
        # Валидация параметров
        allowed_assets = set(NETWORK_CODES.keys())
        
        if from_asset not in allowed_assets:
            await message.answer(
                f"❌ Неподдерживаемая сеть: {from_asset}\n\n"
                f"Доступные сети:\n" + "\n".join(f"• {a}" for a in allowed_assets)
            )
            return
        
        # Базовая проверка диапазона
        if amount < 10 or amount > 500:
            await message.answer(
                "❌ Неверная сумма\n\n"
                "Максимум: 500 USDT (ограничено настройками бота)\n\n"
                "Минимум зависит от сети, проверь /limits"
            )
            return
        
        # Проверка лимитов FixedFloat API
        try:
            limits = await get_fixedfloat_limits(from_asset)
            min_limit = limits["min"]
            max_limit = limits["max"]
            
            # Ограничиваем максимальный лимит бота (500 USD)
            effective_max = min(max_limit, 500.0)
            
            if amount < min_limit:
                await message.answer(
                    f"❌ Сумма меньше минимального лимита FixedFloat\n\n"
                    f"Минимум: {format_amount(min_limit)} USDT (сетевой лимит FixedFloat)\n"
                    f"Твоя сумма: {format_amount(amount)} USDT\n\n"
                    f"💡 Увеличь сумму до минимум {format_amount(min_limit)} USDT"
                )
                return
            
            if amount > effective_max:
                await message.answer(
                    f"❌ Сумма больше максимального лимита\n\n"
                    f"Максимум: 500 USDT (ограничено настройками бота)\n"
                    f"Твоя сумма: {format_amount(amount)} USDT\n\n"
                    f"💡 Уменьши сумму до максимум 500 USDT"
                )
                return
            
            logger.info(f"Лимиты для {from_asset}: min={min_limit:.2f}, max={effective_max:.2f}, amount={amount:.2f}")
        except RuntimeError as e:
            # Если не удалось получить лимиты, проверяем базовый диапазон
            error_msg = str(e)
            if "недоступна" in error_msg.lower() or "311" in error_msg or "312" in error_msg:
                await message.answer(
                    f"❌ Сеть {from_asset} недоступна на FixedFloat в данный момент\n\n"
                    f"Попробуй позже или выбери другую сеть"
                )
            else:
                await message.answer(
                    f"❌ Не удалось проверить лимиты для {from_asset}\n\n"
                    f"Ошибка: {escape_html(error_msg)}\n\n"
                    f"Попробуй позже"
                )
            return
        
        if interval not in [12, 24, 168, 720]:
            await message.answer(
                "❌ Неверный интервал\n\n"
                "Доступные:\n"
                "• 12 - раз в 12 часов\n"
                "• 24 - раз в день\n"
                "• 168 - раз в неделю (7 дней)\n"
                "• 720 - раз в месяц (30 дней)"
            )
            return
        
        # Валидация BTC адреса
        if not validate_btc_address(btc_address):
            await message.answer(
                "❌ Неверный BTC адрес\n\n"
                "Проверь адрес и попробуй снова.\n"
                "Поддерживаются форматы:\n"
                "• Legacy (1...)\n"
                "• SegWit (3...)\n"
                "• Native SegWit (bech32)"
            )
            return
        
        # Сохранение плана в БД
        user_id = message.from_user.id
        next_run = int(time.time()) + (interval * 3600)
        now = int(time.time())
        
        async with aiosqlite.connect(DB_PATH) as db:
            new_plan_id = None
            # Проверяем сколько НЕ удаленных планов уже есть для этой сети
            async with db.execute(
                "SELECT COUNT(*) FROM dca_plans WHERE user_id = ? AND from_asset = ? AND deleted = 0",
                (user_id, from_asset)
            ) as cur:
                count_row = await cur.fetchone()
                plans_count = count_row[0] if count_row else 0
            
            # Проверяем не существует ли уже такой же НЕ удаленный план (сеть + сумма + интервал)
            async with db.execute(
                "SELECT id, active_order_id, active_order_expires FROM dca_plans "
                "WHERE user_id = ? AND from_asset = ? AND amount = ? AND interval_hours = ? AND deleted = 0",
                (user_id, from_asset, amount, interval)
            ) as cur:
                duplicate = await cur.fetchone()
            
            if duplicate:
                plan_id, order_id, order_expires = duplicate
                
                # Проверяем есть ли активный ордер для этого плана
                if order_id and order_expires and order_expires > now:
                    await message.answer(
                        f"❌ Такой план уже существует и у него есть активный ордер!\n\n"
                        f"📋 План: {get_network_label(from_asset) or from_asset}, {format_order_amount(amount, network_key=from_asset)}, раз в {format_interval(interval)}\n\n"
                        f"🔥 Активный ордер:\n"
                        f"🔗 Ордер: {format_order_link(order_id)}\n"
                        f"⏰ До истечения: {format_order_deadline(order_expires, now)}\n\n"
                        f"💡 Дождись истечения ордера или используй другие параметры",
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                    return
                else:
                    # План есть, но ордера нет или истёк
                    await message.answer(
                        f"❌ Такой план уже существует!\n\n"
                        f"📋 План: {get_network_label(from_asset) or from_asset}, {format_order_amount(amount, network_key=from_asset)}, раз в {format_interval(interval)}\n\n"
                        f"💡 Используй другую сумму или интервал"
                    )
                    return
            
            # Проверяем лимит (не больше 3 планов на сеть)
            if plans_count >= 3:
                await message.answer(
                    f"❌ Достигнут лимит планов для {from_asset}\n\n"
                    f"Максимум: 3 плана на одну сеть\n"
                    f"Текущих планов: {plans_count}\n\n"
                    f"💡 Удали один из планов: /status"
                )
                return
            
            # Проверяем есть ли активный ордер для ТОЧНО ТАКОГО ЖЕ плана (сеть + сумма + интервал + BTC адрес)
            # в удалённых планах
            async with db.execute(
                "SELECT active_order_id, active_order_address, active_order_amount, active_order_expires, btc_address "
                "FROM dca_plans WHERE user_id = ? AND from_asset = ? AND amount = ? AND interval_hours = ? "
                "AND active_order_id IS NOT NULL AND deleted = 1 "
                "ORDER BY active_order_expires DESC LIMIT 1",
                (user_id, from_asset, amount, interval)
            ) as cur:
                existing_order = await cur.fetchone()
            
            # Создаём новый план
            if existing_order and existing_order[3] and existing_order[3] > now:
                # Есть активный ордер от удалённого плана с теми же параметрами
                order_id, order_address, order_amount, order_expires, old_btc_address = existing_order
                
                # ВАЖНО: Проверяем совпадение BTC адреса!
                if old_btc_address != btc_address:
                    # BTC адрес отличается - не наследуем ордер, создаём новый план
                    await message.answer(
                        f"⚠️ Найден активный ордер от удалённого плана, но BTC адрес отличается!\n\n"
                        f"Старый BTC адрес:\n{format_btc_recipient_copyable(old_btc_address)}\n"
                        f"Новый BTC адрес:\n{format_btc_recipient_copyable(btc_address)}\n\n"
                        f"💡 Создаю новый план без наследования ордера.\n"
                        f"Старый ордер остаётся активным на FixedFloat."
                    )
                    # Создаём план без наследования ордера
                    cursor = await db.execute('''
                        INSERT INTO dca_plans 
                        (user_id, from_asset, amount, interval_hours, btc_address, next_run, active)
                        VALUES (?, ?, ?, ?, ?, ?, 1)
                    ''', (user_id, from_asset, amount, interval, btc_address, next_run))
                    new_plan_id = cursor.lastrowid
                else:
                    # BTC адрес совпадает - наследуем ордер
                    cursor = await db.execute('''
                        INSERT INTO dca_plans 
                        (user_id, from_asset, amount, interval_hours, btc_address, next_run, active,
                         active_order_id, active_order_address, active_order_amount, active_order_expires)
                        VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                    ''', (user_id, from_asset, amount, interval, btc_address, next_run,
                          order_id, order_address, order_amount, order_expires))
                    new_plan_id = cursor.lastrowid
            else:
                # Нет активного ордера - создаём чистый план
                cursor = await db.execute('''
                    INSERT INTO dca_plans 
                    (user_id, from_asset, amount, interval_hours, btc_address, next_run, active)
                    VALUES (?, ?, ?, ?, ?, ?, 1)
                ''', (user_id, from_asset, amount, interval, btc_address, next_run))
                new_plan_id = cursor.lastrowid
            
            await db.commit()
            action = "создан"
        
        plan_number = await get_plan_display_number(user_id, int(new_plan_id)) if new_plan_id else None
        execute_command = f"/execute_{plan_number}" if plan_number is not None else "/status"
        masked_addr = format_btc_recipient_copyable(btc_address)
        
        # Форматируем интервал
        interval_text = format_interval(interval)
        
        await message.answer(
            f"✅ DCA план {action}!\n\n"
            f"💱 Сеть: {get_network_label(from_asset) or from_asset}\n"
            f"💵 Сумма: {format_order_amount(amount, network_key=from_asset)}\n"
            f"⏱ Интервал: раз в {interval_text}\n"
            f"📥 Получатель BTC:\n{masked_addr}\n\n"
            f"⏰ Первый запуск через {interval_text}\n\n"
            f"{build_execute_hint(execute_command)}\n\n"
            f"{build_status_hint()}"
        )
        
        logger.info(f"DCA план {action}: user_id={user_id}, {from_asset}, {amount} USD, {interval}ч")
        
    except ValueError as e:
        await message.answer(f"❌ Ошибка в параметрах: {escape_html(e)}")
    except Exception as e:
        logger.error(f"Ошибка создания DCA плана: {e}")
        await message.answer(f"❌ Ошибка: {escape_html(e)}")


# ============================================================================
# ЗАПУСК БОТА
# ============================================================================

async def order_monitor():
    """
    Фоновая задача для мониторинга завершения ордеров FixedFloat.
    Проверяет статус ордеров и отправляет уведомления с Blockchair ссылками.
    """
    logger.info("Order Monitor запущен")
    
    while True:
        try:
            await asyncio.sleep(300)  # Проверка каждые 5 минут

            now = int(time.time())

            # Проверяем активные ордера в dca_plans и корректно завершаем их по фактическому статусу
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute(
                    "SELECT id, active_order_id, active_order_expires FROM dca_plans "
                    "WHERE active_order_id IS NOT NULL"
                ) as active_cur:
                    active_orders = await active_cur.fetchall()

            for plan_id, order_id, active_order_expires in active_orders:
                if active_order_expires and int(active_order_expires) <= now:
                    await notify_and_clear_expired_order(plan_id, order_id)
                    continue
                status = await get_fixedfloat_order_status_with_retry(order_id)
                if status in SUCCESS_FIXEDFLOAT_ORDER_STATUSES:
                    await mark_order_completed(plan_id, order_id, f"fixedfloat_{status}")
                    continue
                if status in FINAL_FIXEDFLOAT_ORDER_STATUSES:
                    logger.info("Order %s expired, clearing active order", order_id)
                    await mark_order_failed(plan_id, order_id, f"FixedFloat order {status}")
                    continue
                if status == "":
                    logger.info("Order %s status unavailable after retries; keeping order active", order_id)
                    continue
            
            async with aiosqlite.connect(DB_PATH) as db:
                # Получаем все отправленные ордера без history-записи
                async with db.execute(
                    "SELECT st.order_id, st.user_id, st.plan_id, st.network_key, st.amount, "
                    "st.transfer_tx_hash, st.sent_at, dp.btc_address, dp.active_order_expires, COALESCE(co.notified, 0) "
                    "FROM sent_transactions st "
                    "JOIN dca_plans dp ON st.plan_id = dp.id "
                    "LEFT JOIN completed_orders co ON st.order_id = co.order_id "
                    "WHERE (co.order_id IS NULL OR COALESCE(co.notified, 0) < 2) AND st.transfer_tx_hash IS NOT NULL "
                    "AND st.id = ("
                    "  SELECT st2.id FROM sent_transactions st2 "
                    "  WHERE st2.order_id = st.order_id ORDER BY st2.sent_at DESC LIMIT 1"
                    ")"
                ) as cursor:
                    orders_to_check = await cursor.fetchall()
            
            for order_id, user_id, plan_id, network_key, amount, transfer_tx_hash, sent_at, btc_address, active_order_expires, notified_stage in orders_to_check:
                try:
                    plan_number = await get_plan_display_number(int(user_id), int(plan_id))
                    status = await get_fixedfloat_order_status_with_retry(order_id)
                    if status in SUCCESS_FIXEDFLOAT_ORDER_STATUSES:
                        await mark_order_completed(plan_id, order_id, f"fixedfloat_{status}")
                        btc_txid = await fetch_btc_txid(order_id)
                        if btc_txid:
                            tx_observation = await fetch_btc_tx_observation(btc_txid)
                            confirmations = int(tx_observation.get("confirmations", 0) or 0)
                            order_data = await fetch_fixedfloat_order_details(order_id)
                            btc_amount = extract_btc_amount_from_order_data(order_data)
                            if confirmations >= 1:
                                completion_text = build_purchase_success_notification(
                                    order_id=order_id,
                                    plan_number=plan_number,
                                    network_key=network_key,
                                    amount=amount,
                                    btc_address=btc_address,
                                    btc_txid=btc_txid,
                                    btc_amount=btc_amount,
                                )
                                await bot.send_message(
                                    int(user_id),
                                    completion_text,
                                    parse_mode="HTML",
                                    disable_web_page_preview=True,
                                )
                                async with aiosqlite.connect(DB_PATH) as ndb:
                                    await ndb.execute("UPDATE completed_orders SET notified = 2 WHERE order_id = ?", (order_id,))
                                    await ndb.commit()
                                _order_progress_messages.pop(str(order_id), None)
                            elif int(notified_stage or 0) < 1:
                                sent_text = build_btc_tx_sent_notification(
                                    order_id=order_id,
                                    plan_number=plan_number,
                                    network_key=network_key,
                                    amount=amount,
                                    btc_address=btc_address,
                                    btc_txid=btc_txid,
                                )
                                await bot.send_message(
                                    int(user_id),
                                    sent_text,
                                    parse_mode="HTML",
                                    disable_web_page_preview=True,
                                )
                                async with aiosqlite.connect(DB_PATH) as ndb:
                                    await ndb.execute("UPDATE completed_orders SET notified = 1 WHERE order_id = ?", (order_id,))
                                    await ndb.commit()
                            else:
                                logger.info("BTC tx pending confirmations for order %s", order_id)
                        else:
                            order_data = await fetch_fixedfloat_order_details(order_id)
                            if is_requote_order_state(order_data):
                                await update_order_progress_message(
                                    int(user_id),
                                    str(order_id),
                                    build_requote_notification(
                                        order_id=order_id,
                                        plan_number=plan_number,
                                        network_key=network_key,
                                        amount=amount,
                                        btc_address=btc_address,
                                    ),
                                )
                            else:
                                logger.info("Waiting for BTC TX for order %s", order_id)
                                waiting_text = build_payout_review_notification(
                                    order_id=order_id,
                                    plan_number=plan_number,
                                    network_key=network_key,
                                    amount=amount,
                                    btc_address=btc_address,
                                )
                                await update_order_progress_message(int(user_id), str(order_id), waiting_text)
                        logger.info(f"Order {order_id} marked as completed for user {user_id}")
                    elif status in FINAL_FIXEDFLOAT_ORDER_STATUSES:
                        await mark_order_failed(plan_id, order_id, f"FixedFloat order {status}")
                    elif status == "":
                        logger.info("Order %s status unavailable after retries; keeping order active", order_id)
                        continue
                
                except Exception as e:
                    logger.error(f"Error checking order {order_id}: {e}")
        
        except Exception as e:
            logger.error(f"Ошибка в order monitor: {e}")


async def load_passwords_at_startup():
    """
    Load passwords from OS keyring into memory cache at bot startup.
    This ensures auto-send continues to work after restarts.
    """
    logger.info("Loading wallet passwords from keyring...")
    
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM wallets") as cursor:
            users = await cursor.fetchall()
    
    for (user_id,) in users:
        password = load_password_from_keyring(user_id)
        if password:
            _wallet_passwords[user_id] = password
            logger.info(f"Wallet password loaded from keyring for user {user_id}")
        else:
            logger.warning(f"No password in keyring for user {user_id}")





def acquire_instance_lock(lock_path: str) -> bool:
    """
    Cross-platform single-instance lock.
    - Unix/macOS: fcntl.flock on opened lock file.
    - Windows: atomic file creation via O_EXCL.
    """
    global _instance_lock_file, _instance_lock_path

    if _instance_lock_file is not None:
        logger.error("Lock already acquired in current process")
        return False

    lock_dir = os.path.dirname(lock_path)
    if lock_dir:
        os.makedirs(lock_dir, exist_ok=True)

    if HAS_FCNTL:
        lock_fp = open(lock_path, "w", encoding="utf-8")
        try:
            fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            _instance_lock_file = lock_fp
            _instance_lock_path = lock_path
            return True
        except BlockingIOError:
            logger.error(f"Another bot instance is already running (lock: {lock_path})")
            lock_fp.close()
            return False

    # Windows fallback: stale lock detection via PID check.
    if os.path.exists(lock_path):
        stale_lock = False
        try:
            with open(lock_path, "r", encoding="utf-8") as f:
                pid_raw = f.read().strip()
            if pid_raw.isdigit():
                if is_process_alive(int(pid_raw)):
                    logger.error(f"Another bot instance is already running (lock: {lock_path})")
                    return False
                stale_lock = True
            else:
                stale_lock = True
        except OSError:
            stale_lock = True

        if stale_lock:
            try:
                os.remove(lock_path)
            except OSError as e:
                logger.error(f"Failed to remove stale lock {lock_path}: {e}")
                return False

    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        lock_fp = os.fdopen(fd, "w", encoding="utf-8")
        lock_fp.write(str(os.getpid()))
        lock_fp.flush()
        _instance_lock_file = lock_fp
        _instance_lock_path = lock_path
        return True
    except FileExistsError:
        logger.error(f"Another bot instance is already running (lock: {lock_path})")
        return False
    except OSError as e:
        logger.error(f"Failed to acquire instance lock {lock_path}: {e}")
        return False


def release_instance_lock() -> None:
    """Release cross-platform single-instance lock."""
    global _instance_lock_file, _instance_lock_path
    lock_path = _instance_lock_path

    if _instance_lock_file:
        try:
            if HAS_FCNTL:
                fcntl.flock(_instance_lock_file.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            _instance_lock_file.close()
            _instance_lock_file = None

    if lock_path:
        try:
            if os.path.exists(lock_path):
                os.remove(lock_path)
        except OSError as e:
            logger.warning(f"Failed to remove lock file {lock_path}: {e}")

    _instance_lock_path = None


atexit.register(release_instance_lock)


async def main():
    """
    Главная функция запуска бота.
    Инициализирует БД, обновляет коды сетей, запускает scheduler и polling.
    """
    logger.info("=" * 60)
    logger.info("Запуск AutoDCA Bot...")

    run_startup_checks()
    ensure_runtime_directories()

    lock_path = resolve_project_path(os.getenv("BOT_LOCK_PATH", ""), DEFAULT_LOCK_FILE)
    if not acquire_instance_lock(lock_path):
        return

    try:
        if is_test_mode():
            logger.warning("=" * 60)
            logger.warning("⚠️ TEST MODE(S) ENABLED:")
            if DRY_RUN:
                logger.warning("  • DRY_RUN: No transactions will be broadcast")
            if MOCK_FIXEDFLOAT:
                logger.warning("  • MOCK_FIXEDFLOAT: Using mocked API responses")
            if USE_TESTNET:
                logger.warning("  • USE_TESTNET: Using testnet networks")
            logger.warning("=" * 60)
        
        # Инициализация базы данных
        await init_db()
        
        # Load passwords from keyring into memory cache
        await load_passwords_at_startup()
        await setup_bot_commands()
        
        # Обновление актуальных кодов сетей из FixedFloat
        await update_network_codes()

        # Recovery scan for in-flight transactions after restart
        await recovery_scan_pending_transactions()
        await recover_stale_plan_claims()
        
        logger.info("🚀 AutoDCA Bot успешно запущен!")
        logger.info("=" * 60)
        
        await notify_offline_startup_status()
        
        # Запуск фонового планировщика DCA
        asyncio.create_task(dca_scheduler())
        
        # Запуск мониторинга завершения ордеров
        asyncio.create_task(order_monitor())
        
        # Запуск обработки сообщений от Telegram
        await dp.start_polling(bot)
    finally:
        release_instance_lock()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен пользователем")
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}", exc_info=True)
