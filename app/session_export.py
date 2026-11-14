import io
import os
import tempfile
import zipfile

from telethon.sessions import SQLiteSession, StringSession

from app.session_manager import dc_addr


def sqlite_bytes(session_string: str) -> bytes:
    session = (session_string or "").strip()
    if not session:
        raise ValueError("empty session")
    src = StringSession(session)
    if not src.auth_key:
        raise ValueError("no auth key")
    dc = int(src.dc_id or 2)
    addr = src.server_address or (dc_addr(dc) or ("149.154.167.51", 443))[0]
    port = int(src.port or 443)

    fd, path = tempfile.mkstemp(suffix=".session")
    os.close(fd)
    try:
        dst = SQLiteSession(path)
        dst.set_dc(dc, addr, port)
        dst.auth_key = src.auth_key
        dst.save()
        dst.close()
        with open(path, "rb") as f:
            return f.read()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def zip_of(rows) -> bytes:
    buf = io.BytesIO()
    added = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in rows:
            try:
                blob = sqlite_bytes(p.get("session_string") or "")
            except Exception:
                continue
            name = (p.get("phone") or str(p.get("item_id"))).lstrip("+") or "session"
            z.writestr(f"{name}.session", blob)
            added += 1
    return buf.getvalue() if added else b""