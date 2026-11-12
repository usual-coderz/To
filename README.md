# AccStore — LZT Auto Buyer

Telegram bot that auto-buys LZT Market accounts by filter, logs them in, and after a
configurable session lifetime changes the mail and terminates the session.

## Stack
Python 3.12 · aiogram 3 · Telethon · MongoDB (pymongo async) · httpx

## Setup
```bash
cp .env.example .env          # BOT_TOKEN, LZT_TOKEN, OWNER_IDS, MONGO_URI
pip install -r requirements.txt
python -m scripts.seed_owner 123456789
python -m app.main
python -m scripts.make_session