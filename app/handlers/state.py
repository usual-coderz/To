import time

TTL = 300.0
PENDING: dict[int, dict] = {}


def _prune():
    now = time.time()
    for uid in [u for u, v in PENDING.items() if now - v["ts"] > TTL]:
        PENDING.pop(uid, None)


def set(uid: int, kind: str, key: str = "", chat_id: int = 0, message_id: int = 0):
    _prune()
    PENDING[uid] = {"kind": kind, "key": key, "ts": time.time(),
                    "chat_id": chat_id, "message_id": message_id}


def get(uid: int) -> dict | None:
    hit = PENDING.get(uid)
    if not hit:
        return None
    if time.time() - hit["ts"] > TTL:
        PENDING.pop(uid, None)
        return None
    return hit


def pop(uid: int) -> dict | None:
    hit = get(uid)
    PENDING.pop(uid, None)
    return hit


def clear(uid: int):
    PENDING.pop(uid, None)


def kind(uid: int) -> str:
    hit = get(uid)
    return hit["kind"] if hit else ""