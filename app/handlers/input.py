from aiogram import F, Router
from aiogram.types import Message

from app.handlers import state
from app.handlers.filters import on_value
from app.handlers.receivers import on_add
from app.handlers.wallet import on_amount

router = Router()


@router.message(F.text)
async def on_text(m: Message):
    if (m.text or "").startswith("/"):
        return
    hit = state.get(m.from_user.id)
    if not hit:
        return
    kind = hit["kind"]
    if kind == "exchange":
        return await on_amount(m)
    if kind == "receiver":
        return await on_add(m, hit)
    if kind == "setting":
        return await on_value(m, hit)