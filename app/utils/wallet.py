import logging

from app.utils.money import free_of, same_currency, spendable, total_of

log = logging.getLogger("wallet")


def _coerce(entries) -> list[dict]:
    return [b for b in (entries or []) if isinstance(b, dict)]


async def snapshot(lzt, currency: str) -> dict:
    snap = {"total": 0.0, "spendable": 0.0, "entries": [], "currency": currency,
            "note": "", "error": ""}

    entries: list[dict] = []
    try:
        entries = _coerce(await lzt.balances())
    except Exception as e:
        snap["error"] = str(e)
        log.info(f"balances: {e}")

    total = 0.0
    try:
        total = float(await lzt.balance_total(currency))
    except AttributeError:
        try:
            total = total_of(await lzt.profile(), currency)
        except Exception as e:
            snap["error"] = snap["error"] or str(e)
    except Exception as e:
        snap["error"] = snap["error"] or str(e)

    usable = [b for b in entries
              if b["usable_for_buy"] and same_currency(b["currency"], currency)]
    total = max(total, sum(free_of(b) for b in usable))

    if total > 0 and not any(free_of(b) >= total for b in usable):
        if not usable:
            entries.append({
                "balance_id": 0, "currency": currency, "amount": total, "hold": 0.0,
                "kind": "market", "label": "Market balance", "usable_for_buy": True,
            })
            snap["note"] = "default balance (no usable ids)"

    others = {b["currency"] for b in entries if b.get("currency")}
    if others and currency not in others:
        snap["note"] = f"balances in {', '.join(sorted(others))}, settings say {currency}"

    snap["entries"] = entries
    snap["spendable"] = spendable(entries, currency)
    snap["total"] = max(total, snap["spendable"])
    return snap


def usable_entries(snap: dict) -> list[dict]:
    return [b for b in snap["entries"] if b["usable_for_buy"]]


def exchangable(snap: dict) -> list[dict]:
    return [b for b in snap["entries"] if b["usable_for_buy"] and free_of(b) > 0]