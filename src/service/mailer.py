"""Email + calendar invite rendering and sending. OWNER: C.

mailer: file  -> writes data/outbox/<key>.eml and <key>.ics (demo + tests)
mailer: smtp  -> TODO(C): send to a TEST inbox only (ask organizers what screening allows)
Both must be idempotent on payload['idempotency_key'].
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from email.message import EmailMessage

from .config import DATA_DIR, settings


def build_ics(*, uid: str, title: str, attendee: str, minutes: int, start_after: date) -> str:
    """Minimal RFC 5545 invite. Start = next weekday 11:00 IST after start_after + 2 days."""
    d = start_after + timedelta(days=2)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    start = datetime(d.year, d.month, d.day, 5, 30)  # 11:00 IST in UTC
    end = start + timedelta(minutes=minutes)
    f = "%Y%m%dT%H%M%SZ"
    return "\r\n".join([
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//contextcon05//sdr//EN", "METHOD:REQUEST",
        "BEGIN:VEVENT", f"UID:{uid}@contextcon05", f"DTSTAMP:{start.strftime(f)}",
        f"DTSTART:{start.strftime(f)}", f"DTEND:{end.strftime(f)}", f"SUMMARY:{title}",
        f"ATTENDEE;RSVP=TRUE:mailto:{attendee}", "END:VEVENT", "END:VCALENDAR", ""])


def send(payload: dict) -> dict:
    key = payload["idempotency_key"]
    draft = payload["draft"]
    mode = settings()["email"]["mailer"]
    if mode != "file":
        raise NotImplementedError(f"mailer {mode!r} (C: implement smtp to a test inbox)")
    out = DATA_DIR / "outbox"
    out.mkdir(parents=True, exist_ok=True)
    eml, ics = out / f"{key}.eml", out / f"{key}.ics"
    if eml.exists():  # provider check: already sent before a crash
        return {"message_id": key, "eml": str(eml), "already_sent": True}
    msg = EmailMessage()
    msg["To"], msg["Subject"], msg["Message-ID"] = draft["to"], draft["subject"], f"<{key}@sdr>"
    msg.set_content(draft["body"])
    if draft.get("ics"):
        msg.add_attachment(draft["ics"].encode(), maintype="text", subtype="calendar",
                           filename="invite.ics")
        ics.write_text(draft["ics"], encoding="utf-8")
    tmp = eml.with_suffix(".tmp")
    tmp.write_bytes(bytes(msg))
    tmp.replace(eml)  # atomic: either fully sent or not at all
    return {"message_id": key, "eml": str(eml), "ics": str(ics)}
