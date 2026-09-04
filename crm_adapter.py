"""
CRM integration seam.
=====================

This module is the vendor-specific mapping seam for CRM integration. The HTTP
boundary and synchronization flow live in proxy.py; this file translates CRM
field names and stage vocabulary, checks the shared token, and sends optional
outbound notifications. The adapter remains inert until configured.

Two directions, independently switchable:

  INBOUND   CRM  →  this app     the CRM owns the pipeline; it pushes stage
                                 changes here. Point the CRM's webhook at
                                 POST /api/crm/webhook with the shared token.

  OUTBOUND  this app  →  CRM     a rep marks something here and the CRM is
                                 told. Set CRM_WEBHOOK_URL and it fires on
                                 every status/stage change.

Configure with environment variables (put them in .env, which is gitignored):

    CRM_WEBHOOK_TOKEN=<shared secret>   # required to accept inbound calls
    CRM_WEBHOOK_URL=https://…           # optional; enables outbound
    CRM_TIMEOUT_S=6

Matching records: an inbound `id` or `place_id` is treated as the application's
primary Place ID. If that key is not found, the endpoint performs an indexed
lookup using `crm_id`. Once linked, the two systems stay joined even if a name
or address changes.
"""
from __future__ import annotations
import hmac, json, os, threading, urllib.request

# ── stage vocabulary ───────────────────────────────────────────────────────
# Our canonical stages. Edit the right-hand lists to match the CRM's own names;
# matching is case-insensitive and ignores spaces/underscores/hyphens.
OUR_STAGES = [
    "postcard_mailed", "responded", "contacted", "consultation",
    "negotiating", "contract_sent", "contract_signed", "shipment", "operational",
]
STAGE_ALIASES: dict[str, list[str]] = {
    "postcard_mailed":  ["mailer sent", "postcard", "direct mail"],
    "responded":        ["lead", "inbound", "qr scan", "form submitted"],
    "contacted":        ["contact made", "call made", "outreach"],
    "consultation":     ["discovery", "meeting", "qualified"],
    "negotiating":      ["negotiation", "proposal", "in discussion"],
    "contract_sent":    ["agreement sent", "quote sent", "awaiting signature"],
    "contract_signed":  ["closed won", "won", "signed"],
    "shipment":         ["fulfilment", "fulfillment", "logistics", "install scheduled"],
    "operational":      ["live", "active", "installed", "deployed"],
}
# CRM statuses that mean "stop showing me this site".
REJECT_ALIASES = ["closed lost", "lost", "disqualified", "unqualified", "do not contact"]


def _norm(s: str) -> str:
    return "".join(ch for ch in str(s or "").lower() if ch.isalnum())


def map_stage(crm_stage: str) -> str | None:
    """CRM stage name → one of OUR_STAGES, or None if unrecognised."""
    n = _norm(crm_stage)
    if not n:
        return None
    for ours in OUR_STAGES:
        if n == _norm(ours):
            return ours
    for ours, aliases in STAGE_ALIASES.items():
        if any(n == _norm(a) for a in aliases):
            return ours
    return None


def map_status(crm_status: str) -> str | None:
    """CRM status → our status vocabulary (prospect / contacted / rejected /
    do_not_contact / pipeline)."""
    n = _norm(crm_status)
    if not n:
        return None
    if any(n == _norm(a) for a in REJECT_ALIASES):
        return "do_not_contact" if "donotcontact" in n else "rejected"
    if n in ("contacted", "contactmade"):
        return "contacted"
    if n in ("open", "active", "inpipeline", "pipeline"):
        return "pipeline"
    return None


# ── inbound: CRM → app ─────────────────────────────────────────────────────
def is_enabled() -> bool:
    return bool(os.getenv("CRM_WEBHOOK_TOKEN"))


def check_token(supplied: str | None) -> bool:
    """Compare the shared secret without leaking a matching prefix."""
    want = os.getenv("CRM_WEBHOOK_TOKEN", "")
    if not want or not supplied:
        return False
    return hmac.compare_digest(want, supplied)


def parse_inbound(payload: dict) -> tuple[str | None, dict]:
    """Translate one CRM payload into (match_key, patch) for our store.

    Override this for a CRM whose JSON is shaped differently — it is the only
    place that touches the vendor's field names.
    """
    # Prefer OUR id as the storage key: site records must stay joinable to
    # search results, which are keyed by Google place_id. crm_id is carried as a
    # field and is used by the caller to find an already-linked record.
    match = payload.get("id") or payload.get("place_id") or payload.get("crm_id")
    patch: dict = {}
    stage = map_stage(payload.get("stage") or payload.get("deal_stage") or "")
    if stage:
        patch["stage"] = stage
        patch.setdefault("status", "pipeline")
    status = map_status(payload.get("status") or payload.get("deal_status") or "")
    if status:
        patch["status"] = status
    for src, dst in (("owner", "owner"), ("notes", "notes"), ("reason", "reason"),
                     ("next_action", "next_action"), ("last_contact", "last_contact"),
                     ("followup_date", "followup_date"), ("name", "name"),
                     ("address", "address")):
        if payload.get(src) not in (None, ""):
            patch[dst] = payload[src]
    if payload.get("crm_id"):
        patch["crm_id"] = payload["crm_id"]
    return match, patch


# ── outbound: app → CRM ────────────────────────────────────────────────────
def notify(event: str, record: dict) -> None:
    """Fire-and-forget POST to the CRM. Never blocks or raises into a request:
    a CRM outage must not stop a rep marking a site here."""
    url = os.getenv("CRM_WEBHOOK_URL")
    if not url:
        return
    body = json.dumps({
        "event": event,                       # "site.updated" | "site.created"
        "site_id": record.get("id"),
        "crm_id": record.get("crm_id"),
        "name": record.get("name"),
        "address": record.get("address"),
        "lat": record.get("lat"), "lng": record.get("lng"),
        "status": record.get("status"), "stage": record.get("stage"),
        "reason": record.get("reason"), "owner": record.get("owner"),
        "updated": record.get("updated"),
    }).encode()

    def _send():
        try:
            req = urllib.request.Request(url, data=body, headers={
                "Content-Type": "application/json",
                "X-Source": "atm-site-map",
                **({"X-Auth-Token": os.getenv("CRM_WEBHOOK_TOKEN")} if os.getenv("CRM_WEBHOOK_TOKEN") else {}),
            })
            urllib.request.urlopen(req, timeout=float(os.getenv("CRM_TIMEOUT_S", "6"))).read()
        except Exception:
            pass                              # deliberately silent
    threading.Thread(target=_send, daemon=True).start()


def status() -> dict:
    return {"inbound_enabled": is_enabled(),
            "outbound_enabled": bool(os.getenv("CRM_WEBHOOK_URL")),
            "stages": OUR_STAGES}
