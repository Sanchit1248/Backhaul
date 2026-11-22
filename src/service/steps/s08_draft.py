"""Step 8: draft the email from sourced facts only. OWNER: C.

Template lives in config email.template. Every sentence that states a fact gets
a [n] marker pointing at draft.claims[n-1]. No LLM needed for v1; if C adds
llm.ask for polish, the prompt must forbid new facts and the output must be
re-checked so every claim still maps to a Fact.
"""
from __future__ import annotations

from ..config import env
from ..mailer import build_ics
from ..models import EmailDraft, Prospect
from . import Ctx, active


def run(ctx: Ctx, prospects: list[Prospect]) -> list[Prospect]:
    e = ctx.cfg["email"]
    client = ctx.client.name
    # Organizer = the real sender mailbox. Without one the invite is METHOD:PUBLISH
    # (Outlook rejects a REQUEST with no ORGANIZER). Never invent an address.
    organizer = e.get("sender_email") or env("SENDER_EMAIL")
    for p in active(prospects):
        if not (p.buyer and p.buyer.business_email):
            p.hold("no verified business email for the buyer")
            continue
        t = max(p.triggers, key=lambda t: ctx.cfg["triggers"]["weights"].get(t.type, 1.0))
        fit = p.route_fit
        claims = [t.fact, *fit.facts]
        route = f"{fit.route.origin.title()} to {fit.route.destination.title()}"
        sources = "\n".join(f"[{i}] {c.sources[0].url}" for i, c in enumerate(claims, 1))
        fields = {
            "client_name": client,
            "buyer_first_name": p.buyer.name.split()[0],
            "trigger_sentence": t.fact.claim,
            "route_sentence": f"Your {fit.reason.split(';')[0]} sits on our {route} lane.",
            "spare_trucks": fit.route.spare_trucks_per_week,
            "route": route,
            "source_list": sources,
        }
        fields["sender_name"] = e["sender_name"].format(**fields)
        meeting = f"{e['meeting_minutes']}-minute call: spare truck capacity on {route}"
        p.draft = EmailDraft(
            to=p.buyer.business_email,
            subject=e["subject"].format(**fields),
            body=e["template"].format(**fields),
            claims=claims,
            ics=build_ics(uid=p.prospect_id, title=f"{client} x {p.company.name}",
                          attendee=p.buyer.business_email, attendee_name=p.buyer.name,
                          organizer=organizer, organizer_name=fields["sender_name"],
                          description=meeting, minutes=e["meeting_minutes"],
                          start_after=ctx.job.request_date),
        )
    return prospects
