import asyncio
import sys

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession

from app.config import API_HASH, API_ID


async def main():
    phone = input("phone (+9198...): ").strip().replace(" ", "")
    if not phone:
        sys.exit("phone required")
    client = TelegramClient(StringSession(), API_ID, API_HASH,
                            device_model="AccStore", system_version="2.1",
                            app_version="2.1", lang_code="en")
    await client.connect()
    sent = await client.send_code_request(phone)
    code = input("code: ").strip().replace(" ", "")
    try:
        await client.sign_in(phone, code, phone_code_hash=sent.phone_code_hash)
    except SessionPasswordNeededError:
        pwd = input("2FA password: ").strip()
        await client.sign_in(password=pwd)
    me = await client.get_me()
    if me is None:
        sys.exit("login failed")
    print(f"\nlogged in: {me.id} @{me.username or '—'}\n")
    print("USERBOT_SESSION=" + StringSession.save(client.session))
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())