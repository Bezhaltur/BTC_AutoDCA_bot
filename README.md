# BTC_AutoDCA_Bot

Telegram-бот для DCA-покупки Bitcoin (BTC) через FixedFloat.

Бот поддерживает два режима:
- **Manual mode** — пользователь отправляет USDT вручную.
- **Auto Send mode** — бот автоматически отправляет USDT с EVM-кошелька.

Приватный ключ импортируется один раз, шифруется и сохраняется как `keystore` файл.
Пароль хранится в OS keyring.

## 🚀 Features

- DCA-покупка BTC по расписанию.
- Два режима работы: Manual mode и Auto Send mode.
- Поддержка сетей: Arbitrum, BSC, Polygon.
- Импорт `wallet.json` и безопасное хранение ключа в `keystore`.
- Хранение пароля в OS keyring.
- Проверка `execution_window` для защиты от позднего исполнения.
- Offline recovery при перезапуске.
- Пропуск устаревших циклов (`execution_state = skipped`).
- Защита от duplicate execution при конкурентных запусках.

## 🔄 Режимы работы

### Manual mode
Бот создаёт ордер FixedFloat и показывает адрес депозита.  
USDT отправляются вручную пользователем.

### Auto Send mode
Бот создаёт ордер FixedFloat и автоматически отправляет USDT с настроенного EVM-кошелька.

## 📋 Требования

- Python 3.9+
- Telegram Bot Token
- Telegram Admin ID
- FixedFloat API Key и FixedFloat API Secret

## 🔧 Installation

### 1) Установка проекта

1. Клонируйте репозиторий:
```bash
git clone https://github.com/yourusername/autodca-bot.git
cd autodca-bot
```

2. Создайте виртуальное окружение:
```bash
python3 -m venv venv
source venv/bin/activate  # macOS / Linux
# или
venv\Scripts\activate     # Windows
```

3. Установите зависимости:
```bash
pip install -r requirements.txt
```

### 2) Первичная настройка

Подготовьте 4 параметра:
- **Telegram Bot Token** — через [@BotFather](https://t.me/BotFather), команда `/newbot`
- **Telegram Admin ID** — через [@my_id_bot](https://t.me/my_id_bot)
- **FixedFloat API Key**
- **FixedFloat API Secret**  
  Получить: [https://ff.io/user/apikey](https://ff.io/user/apikey)

### 3) Настройка `.env`

1. Откройте папку проекта.
2. Найдите файл `.env.example`.
3. Скопируйте его.
4. Переименуйте копию в `.env`.
5. Откройте `.env` в любом текстовом редакторе и заполните значения:

```env
# Telegram bot
DCA_TELEGRAM_BOT_TOKEN=your_telegram_bot_token
ADMIN_USER_ID=123456789

# FixedFloat API
FIXEDFLOAT_API_KEY=your_fixedfloat_api_key
FIXEDFLOAT_API_SECRET=your_fixedfloat_api_secret
```

### 4) Настройка кошелька

Создайте файл `wallet.json` в корне проекта:

```json
{
  "private_key": "0xYOUR_PRIVATE_KEY",
  "password": "YOUR_PASSWORD"
}
```

После этого в Telegram выполните:

```text
/setwallet
```

Что произойдёт:
- бот зашифрует приватный ключ;
- создаст `keystore`;
- сохранит пароль в OS keyring.

После успешного импорта `wallet.json` можно удалить.

## ▶️ Запуск бота

**Mac / Linux**
```bash
python3 bot.py
```

**Windows**
```bash
python bot.py
```

После запуска бот:
- создаёт базу данных (если ещё нет);
- запускает scheduler;
- начинает выполнять DCA-планы.

## ⛽ Требования к газу

Для автоотправки USDT на кошельке должен быть газ:
- **Arbitrum** → ETH
- **BSC** → BNB
- **Polygon** → MATIC

## 📖 Commands

- `/start` — приветствие и список команд
- `/help` — подробная справка
- `/setwallet` — импорт/настройка кошелька
- `/walletstatus` — балансы кошелька
- `/setdca СЕТЬ СУММА ИНТЕРВАЛ BTC_АДРЕС` — создать DCA-план
- `/status` — статус планов
- `/execute` или `/execute_N` — выполнить план вручную
- `/pause` или `/pause_N` — приостановить план
- `/resume` или `/resume_N` — возобновить план
- `/delete` или `/delete_N` — удалить план
- `/limits` — лимиты обмена
- `/history` — история операций
- `/networks` — доступные сети

## 📝 Пример DCA-плана

```text
/setdca USDT-ARB 50 24 bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh
```

- `USDT-ARB` — сеть (Arbitrum)
- `50` — сумма в USD
- `24` — интервал в часах (12, 24, 168, 720)
- `bc1q...` — BTC-адрес для получения

## 🏗️ Architecture

Основные компоненты:
1. **DCA Scheduler** — проверяет планы каждую минуту.
2. **SQLite** — хранит планы, ордера, статусы и историю.
3. **FixedFloat API** — создание и отслеживание ордеров USDT → BTC.
4. **Telegram Bot (aiogram)** — команды и уведомления.

Ключевые механики:
- execution window (защита от позднего исполнения);
- offline recovery после рестарта;
- skipped/expired циклы;
- duplicate execution protection.

## 🔒 Security

- `private_key` импортируется из `wallet.json` один раз.
- Ключ шифруется и хранится в `keystore`.
- Пароль хранится в OS keyring.
- `wallet.json` используется только для импорта и затем удаляется.
- API-ключи хранятся в `.env` (не коммитятся в Git).

⚠️ Никогда не коммитьте `wallet.json` в Git.

## 📄 Лицензия

MIT License

## ⚠️ Отказ от ответственности

Проект предоставляется «как есть». Используйте на свой риск.
