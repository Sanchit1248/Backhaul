"""Email + calendar invite rendering and sending. OWNER: C.

mailer: file  -> writes data/outbox/<key>.eml and <key>.ics (demo + tests)
mailer: smtp  -> TODO(C): send to a TEST inbox only (ask organizers what screening allows)
Both must be idempotent on payload['idempotency_key'].
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from email.message import EmailMessage

from .config import DATA_DIR, settings


def _ics_text(s: str) -> str:
    """RFC 5545 3.3.11 TEXT escaping."""
    return (s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            .replace("\r\n", "\\n").replace("\n", "\\n"))


def _ics_param(s: str) -> str:
    """Param values (CN=...) cannot hold : ; , unquoted, nor a double quote at all."""
    return '"' + s.replace('"', "'") + '"'


def _fold(line: str) -> str:
    """RFC 5545 3.1: lines over 75 octets are folded with CRLF + one space."""
    out, cur = [], b""
    for ch in line:
        b = ch.encode()
        if len(cur) + len(b) > (75 if not out else 74):
            out.append(cur.decode())
            cur = b""
        cur += b
    out.append(cur.decode())
    return "\r\n ".join(out)


def build_ics(*, uid: str, title: str, attendee: str, minutes: int, start_after: date,
              organizer: str | None = None, organizer_name: str | None = None,
              attendee_name: str | None = None, description: str | None = None) -> str:
    """RFC 5545 invite that Google Calendar and Outlook both open.

    Start = next weekday 11:00 IST after start_after + 2 days (UTC times, no VTIMEZONE).
    With an organizer email it is a METHOD:REQUEST invite (RSVP buttons); RFC 5546
    requires ORGANIZER for REQUEST and Outlook rejects one without it, so with no
    organizer it falls back to METHOD:PUBLISH (opens as a plain "add to calendar" event).
    Deterministic for the same inputs (DTSTAMP from start_after), so re-runs match.
    """
    d = start_after + timedelta(days=2)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    start = datetime(d.year, d.month, d.day, 5, 30)  # 11:00 IST in UTC
    end = start + timedelta(minutes=minutes)
    stamp = datetime(start_after.year, start_after.month, start_after.day)
    f = "%Y%m%dT%H%M%SZ"
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//contextcon05//sdr//EN",
             "CALSCALE:GREGORIAN", f"METHOD:{'REQUEST' if organizer else 'PUBLISH'}",
             "BEGIN:VEVENT", f"UID:{uid}@contextcon05", f"DTSTAMP:{stamp.strftime(f)}",
             f"DTSTART:{start.strftime(f)}", f"DTEND:{end.strftime(f)}",
             f"SUMMARY:{_ics_text(title)}", "SEQUENCE:0", "STATUS:CONFIRMED",
             "TRANSP:OPAQUE"]
    if description:
        lines.append(f"DESCRIPTION:{_ics_text(description)}")
    if organizer:
        cn = f";CN={_ics_param(organizer_name)}" if organizer_name else ""
        lines.append(f"ORGANIZER{cn}:mailto:{organizer}")
    acn = f";CN={_ics_param(attendee_name)}" if attendee_name else ""
    lines.append(f"ATTENDEE{acn};ROLE=REQ-PARTICIPANT;PARTSTAT=NEEDS-ACTION;"
                 f"RSVP={'TRUE' if organizer else 'FALSE'}:mailto:{attendee}")
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(_fold(x) for x in lines) + "\r\n"


def ics_method(ics: str) -> str:
    m = next((x for x in ics.splitlines() if x.startswith("METHOD:")), "METHOD:PUBLISH")
    return m.split(":", 1)[1].strip()


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
        # method= on the part is what Gmail / Outlook use to show it as an invite
        msg.add_attachment(draft["ics"].encode(), maintype="text", subtype="calendar",
                           filename="invite.ics",
                           params={"method": ics_method(draft["ics"]), "charset": "UTF-8"})
        ics.write_bytes(draft["ics"].encode("utf-8"))  # keep CRLF exactly
    tmp = eml.with_suffix(".tmp")
    tmp.write_bytes(bytes(msg))
    tmp.replace(eml)  # atomic: either fully sent or not at all
    return {"message_id": key, "eml": str(eml), "ics": str(ics)}
