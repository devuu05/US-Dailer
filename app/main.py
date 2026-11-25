"""Twilio click-to-call dialer — FastAPI backend.

Routes
  GET  /                    dialer page
  GET  /token               Twilio Voice access token for the browser
  POST /voice               Twilio webhook: returns TwiML that dials the number
  POST /call-status         Twilio webhook: call progress / duration updates
  POST /incoming            Twilio webhook: inbound calls to our number -> voicemail
  POST /voicemail-saved     Twilio webhook: recording finished
  POST /usage-alert         Twilio usage trigger -> banner in the dialer
  /api/...                  leads, call log, voicemails, alerts, discovery

Access: if ACCESS_KEY is set, the page and APIs need it once via a secret link
(https://your-app/?key=ACCESS_KEY); it's then remembered in a cookie.
Storage: SQLite by default; set DATABASE_URL (postgres://...) to use Postgres.
"""
from __future__ import annotations

import csv
import hmac
import io
import json
import logging
import os
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from dotenv import load_dotenv
from fastapi import FastAPI, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from twilio.jwt.access_token import AccessToken
from twilio.jwt.access_token.grants import VoiceGrant
from twilio.request_validator import RequestValidator
from twilio.twiml.voice_response import Dial, VoiceResponse

from app.qualification import qualify_lead
from app.discovery import DiscoveryPipeline

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("dialer")

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


TWILIO_ACCOUNT_SID = env("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = env("TWILIO_AUTH_TOKEN")
TWILIO_API_KEY_SID = env("TWILIO_API_KEY_SID")
TWILIO_API_KEY_SECRET = env("TWILIO_API_KEY_SECRET")
TWILIO_TWIML_APP_SID = env("TWILIO_TWIML_APP_SID")
TWILIO_CALLER_ID = env("TWILIO_CALLER_ID")
# Render sets RENDER_EXTERNAL_URL automatically, so PUBLIC_BASE_URL is optional there.
PUBLIC_BASE_URL = (env("PUBLIC_BASE_URL") or env("RENDER_EXTERNAL_URL")).rstrip("/")
VALIDATE_SIGNATURE = env("VALIDATE_TWILIO_SIGNATURE", "true").lower() == "true"
ACCESS_KEY = env("ACCESS_KEY")
BUSINESS_NAME = env("BUSINESS_NAME", "Flow Chain")
DATABASE_URL = env("DATABASE_URL")
USE_PG = DATABASE_URL.startswith(("postgres://", "postgresql://"))
DB_PATH = ROOT / env("DB_PATH", "dialer.db")
APIFY_TOKEN = env("APIFY_TOKEN")

app = FastAPI(title="Twilio Dialer with Small Business Intelligence")

# Brand icons are public (the lock page and browser tabs need them before login).
app.mount("/icons", StaticFiles(directory=ROOT / "static" / "icons"), name="icons")


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(ROOT / "static" / "icons" / "favicon.ico", media_type="image/x-icon")


# --------------------------------------------------------------------------- db

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    segment               TEXT DEFAULT 'medium_large',
    name                  TEXT NOT NULL,
    phone                 TEXT NOT NULL UNIQUE,
    city                  TEXT DEFAULT '',
    state                 TEXT DEFAULT '',
    category              TEXT DEFAULT '',
    status                TEXT DEFAULT 'new',
    notes                 TEXT DEFAULT '',
    last_called           TEXT,
    created_at            TEXT NOT NULL,
    timezone              TEXT DEFAULT '',
    website               TEXT DEFAULT '',
    callback_at           TEXT DEFAULT '',
    lead_score            INTEGER DEFAULT 0,
    ai_opportunity        TEXT DEFAULT 'Insufficient',
    ai_reasoning          TEXT DEFAULT '',
    qualification_details TEXT DEFAULT '',
    confidence_level      TEXT DEFAULT 'Medium',
    employee_estimate     TEXT DEFAULT 'Unknown',
    is_emergency          INTEGER DEFAULT 0,
    is_after_hours        INTEGER DEFAULT 0,
    owner_name            TEXT DEFAULT '',
    email                 TEXT DEFAULT '',
    google_rating         REAL DEFAULT 0.0,
    review_count          INTEGER DEFAULT 0,
    source_url            TEXT DEFAULT '',
    verified_at           TEXT DEFAULT '',
    campaign              TEXT DEFAULT '',
    services_offered      TEXT DEFAULT '',
    discovery_job_id      TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS calls (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    call_sid    TEXT UNIQUE,
    lead_id     INTEGER,
    to_number   TEXT NOT NULL,
    status      TEXT DEFAULT 'initiated',
    duration    INTEGER DEFAULT 0,
    started_at  TEXT NOT NULL,
    agent       TEXT DEFAULT '',
    campaign    TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS voicemails (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    call_sid      TEXT,
    from_number   TEXT,
    recording_sid TEXT UNIQUE,
    recording_url TEXT,
    duration      INTEGER DEFAULT 0,
    heard         INTEGER DEFAULT 0,
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    message     TEXT NOT NULL,
    dismissed   INTEGER DEFAULT 0,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS discovery_jobs (
    id                 TEXT PRIMARY KEY,
    status             TEXT DEFAULT 'pending',
    trade              TEXT DEFAULT 'Both',
    state              TEXT DEFAULT '',
    cities             TEXT DEFAULT '',
    max_leads          INTEGER DEFAULT 20,
    min_score          INTEGER DEFAULT 0,
    min_opportunity    TEXT DEFAULT 'All',
    leads_found        INTEGER DEFAULT 0,
    duplicates_skipped INTEGER DEFAULT 0,
    leads_rejected     INTEGER DEFAULT 0,
    leads_added        INTEGER DEFAULT 0,
    error_message      TEXT DEFAULT '',
    created_at         TEXT NOT NULL,
    completed_at       TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS audit_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    action      TEXT NOT NULL,
    segment     TEXT DEFAULT '',
    lead_id     INTEGER,
    details     TEXT DEFAULT '',
    created_at  TEXT NOT NULL
);
"""

LEAD_STATUSES = {
    "new", "no_answer", "voicemail", "callback", "interested",
    "not_interested", "wrong_number", "do_not_call",
}

LIKE = "ILIKE" if USE_PG else "LIKE"  # case-insensitive search on both


class DB:
    """Tiny wrapper so the same SQL ('?' placeholders) runs on SQLite and Postgres."""

    def __init__(self, conn):
        self.conn = conn

    def run(self, sql: str, args=()):
        if USE_PG:
            sql = sql.replace("?", "%s")
        cur = self.conn.cursor()
        cur.execute(sql, tuple(args))
        return cur

    def all(self, sql: str, args=()) -> list[dict]:
        return [dict(r) for r in self.run(sql, args).fetchall()]

    def one(self, sql: str, args=()) -> dict | None:
        row = self.run(sql, args).fetchone()
        return dict(row) if row else None


@contextmanager
def db():
    if USE_PG:
        import psycopg
        from psycopg.rows import dict_row

        conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
    try:
        yield DB(conn)
        conn.commit()
    finally:
        conn.close()


NEW_COLUMNS = [  # columns added incrementally (table, column, type)
    ("leads", "timezone", "TEXT DEFAULT ''"),
    ("leads", "website", "TEXT DEFAULT ''"),
    ("leads", "callback_at", "TEXT DEFAULT ''"),
    ("calls", "agent", "TEXT DEFAULT ''"),
    ("calls", "campaign", "TEXT DEFAULT ''"),
    ("leads", "segment", "TEXT DEFAULT 'medium_large'"),
    ("leads", "lead_score", "INTEGER DEFAULT 0"),
    ("leads", "ai_opportunity", "TEXT DEFAULT 'Insufficient'"),
    ("leads", "ai_reasoning", "TEXT DEFAULT ''"),
    ("leads", "qualification_details", "TEXT DEFAULT ''"),
    ("leads", "confidence_level", "TEXT DEFAULT 'Medium'"),
    ("leads", "employee_estimate", "TEXT DEFAULT 'Unknown'"),
    ("leads", "is_emergency", "INTEGER DEFAULT 0"),
    ("leads", "is_after_hours", "INTEGER DEFAULT 0"),
    ("leads", "owner_name", "TEXT DEFAULT ''"),
    ("leads", "email", "TEXT DEFAULT ''"),
    ("leads", "google_rating", "REAL DEFAULT 0.0"),
    ("leads", "review_count", "INTEGER DEFAULT 0"),
    ("leads", "source_url", "TEXT DEFAULT ''"),
    ("leads", "verified_at", "TEXT DEFAULT ''"),
    ("leads", "campaign", "TEXT DEFAULT ''"),
    ("leads", "services_offered", "TEXT DEFAULT ''"),
    ("leads", "discovery_job_id", "TEXT DEFAULT ''"),
]


def init_db() -> None:
    with db() as c:
        if USE_PG:
            c.conn.execute(SCHEMA.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY"))
            for table, col, typ in NEW_COLUMNS:
                c.conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {typ}")
        else:
            c.conn.executescript(SCHEMA)
            for table, col, typ in NEW_COLUMNS:
                existing = {r[1] for r in c.conn.execute(f"PRAGMA table_info({table})")}
                if col not in existing:
                    c.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")

        # Ensure all existing leads retain their segment and data
        c.run("UPDATE leads SET segment = 'medium_large' WHERE segment IS NULL OR segment = ''")
        c.run("UPDATE leads SET campaign = 'Medium-Large HVAC & Plumbing' WHERE segment = 'medium_large' AND (campaign IS NULL OR campaign = '')")


init_db()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- phone utils

US_E164 = re.compile(r"^\+1[2-9]\d{2}[2-9]\d{6}$")

# Non-US numbers you're allowed to dial for testing (e.g. your own verified cell).
# Everything else stays US-only to limit toll-fraud exposure.
TEST_NUMBERS = {
    "+" + re.sub(r"\D", "", n) for n in env("ALLOWED_TEST_NUMBERS").split(",") if n.strip()
}


def normalize_us(raw: str) -> str | None:
    """Turn '(555) 123-4567', '555.123.4567', '+1 555 123 4567' into +15551234567.

    Also accepts any number listed in ALLOWED_TEST_NUMBERS (international format).
    """
    digits = re.sub(r"\D", "", raw or "")
    if "+" + digits in TEST_NUMBERS:
        return "+" + digits
    if len(digits) == 10:
        digits = "1" + digits
    candidate = "+" + digits
    return candidate if US_E164.match(candidate) else None


# Fallbacks when the area code alone isn't enough (labels from CSVs, or the lead's state).
TZ_LABELS = {
    "pacific": "America/Los_Angeles", "mountain": "America/Denver", "arizona": "America/Phoenix",
    "central": "America/Chicago", "eastern": "America/New_York", "alaska": "America/Anchorage",
    "hawaii": "Pacific/Honolulu",
}
STATE_TZ = {
    **dict.fromkeys("CA NV OR WA".split(), "America/Los_Angeles"),
    **dict.fromkeys("CO MT NM UT WY ID".split(), "America/Denver"),
    "AZ": "America/Phoenix", "AK": "America/Anchorage", "HI": "Pacific/Honolulu",
    **dict.fromkeys("AL AR IA IL KS LA MN MO MS ND NE OK SD TX WI".split(), "America/Chicago"),
    **dict.fromkeys("CT DC DE FL GA IN KY MA MD ME MI NC NH NJ NY OH PA RI SC TN VA VT WV".split(),
                    "America/New_York"),
}


def tz_for(phone: str, state: str = "", label: str = "") -> str:
    """Best-guess IANA time zone for a US number (area code first, then hints)."""
    try:
        import phonenumbers
        from phonenumbers import timezone as pn_tz

        zones = [z for z in pn_tz.time_zones_for_number(phonenumbers.parse(phone)) if z.startswith(("America/", "Pacific/"))]
        if len(zones) == 1:
            return zones[0]
        if zones and STATE_TZ.get(state.upper()) in zones:
            return STATE_TZ[state.upper()]
    except Exception:
        pass
    label = (label or "").strip()
    if "/" in label:
        return label
    return TZ_LABELS.get(label.lower()) or STATE_TZ.get(state.upper(), "")


def backfill_timezones() -> None:
    with db() as c:
        for r in c.all("SELECT id, phone, state FROM leads WHERE timezone IS NULL OR timezone = ''"):
            c.run("UPDATE leads SET timezone = ? WHERE id = ?", (tz_for(r["phone"], r["state"] or ""), r["id"]))


backfill_timezones()


# --------------------------------------------------------------------- guards

COOKIE = "dialer_key"


def has_access(request: Request) -> bool:
    if not ACCESS_KEY:
        return True
    supplied = request.cookies.get(COOKIE) or request.query_params.get("key") or ""
    return hmac.compare_digest(supplied.encode(), ACCESS_KEY.encode())


def require_access(request: Request) -> None:
    if not has_access(request):
        raise HTTPException(403, "Open the dialer with your secret link (…/?key=…) first.")


async def require_twilio(request: Request) -> dict:
    """Validate that a webhook really came from Twilio; returns the form params."""
    form = dict(await request.form())
    if not VALIDATE_SIGNATURE:
        return form
    if not (TWILIO_AUTH_TOKEN and PUBLIC_BASE_URL):
        raise HTTPException(500, "Set TWILIO_AUTH_TOKEN and PUBLIC_BASE_URL to validate webhooks.")
    url = PUBLIC_BASE_URL + request.url.path
    if request.url.query:
        url += "?" + request.url.query
    signature = request.headers.get("x-twilio-signature", "")
    if not RequestValidator(TWILIO_AUTH_TOKEN).validate(url, form, signature):
        raise HTTPException(403, "Invalid Twilio signature.")
    return form


def twiml(resp: VoiceResponse) -> Response:
    return Response(content=str(resp), media_type="application/xml")


# ------------------------------------------------------------------- pages

LOCKED_PAGE = """<!doctype html><meta charset="utf-8"><meta name=viewport content="width=device-width,initial-scale=1">
<title>Flow Chain Dialer</title><link rel="icon" href="/favicon.ico" sizes="any">
<link rel="icon" type="image/png" sizes="32x32" href="/icons/favicon-32.png">
<body style="font-family:system-ui,-apple-system,'Segoe UI',sans-serif;background:#FBF8F5;color:#1E1915;
display:grid;place-items:center;min-height:100vh;margin:0"><div style="text-align:center;padding:24px">
<img src="/icons/icon-192.png" width="96" height="96" alt="Flow Chain logo">
<h2 style="margin:12px 0 6px">Flow Chain Dialer</h2>
<p style="color:#7C7068;margin:0">Open this page with your secret link.</p></div>"""


@app.get("/")
def index(request: Request):
    if not has_access(request):
        return HTMLResponse(LOCKED_PAGE, status_code=403)
    if ACCESS_KEY and request.query_params.get("key"):
        # Remember the key in a cookie, then drop it from the address bar.
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(
            COOKIE, ACCESS_KEY, max_age=60 * 60 * 24 * 365, httponly=True,
            samesite="lax", secure=PUBLIC_BASE_URL.startswith("https"),
        )
        return resp
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/health")
def health():
    missing = [
        k for k in (
            "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_API_KEY_SID",
            "TWILIO_API_KEY_SECRET", "TWILIO_TWIML_APP_SID", "TWILIO_CALLER_ID",
        ) if not env(k) or "xxxx" in env(k) or "your" in env(k).lower() or env(k) == "+15551234567"
    ]
    if not PUBLIC_BASE_URL or "your-subdomain" in PUBLIC_BASE_URL:
        missing.append("PUBLIC_BASE_URL")
    return {
        "ok": not missing,
        "missing_env": missing,
        "db": "postgres" if USE_PG else "sqlite",
        "has_apify": bool(APIFY_TOKEN),
    }


# Our own demo line(s): callable at any hour (the 8am-9pm rule is for calling prospects).
DEMO_NUMBERS = sorted(filter(None, (normalize_us(n) for n in env("DEMO_NUMBERS", "+13463966361").split(","))))


@app.get("/api/config")
def ui_config(request: Request):
    require_access(request)
    return {
        "test_numbers": sorted(TEST_NUMBERS),
        "demo_numbers": DEMO_NUMBERS,
        "has_apify": bool(APIFY_TOKEN),
    }


# ------------------------------------------------------------------- token


@app.get("/token")
def token(request: Request):
    require_access(request)
    if not all((TWILIO_ACCOUNT_SID, TWILIO_API_KEY_SID, TWILIO_API_KEY_SECRET, TWILIO_TWIML_APP_SID)):
        raise HTTPException(500, "Twilio credentials missing — see README.")
    tok = AccessToken(
        TWILIO_ACCOUNT_SID, TWILIO_API_KEY_SID, TWILIO_API_KEY_SECRET,
        identity="dialer-agent", ttl=3600,
    )
    tok.add_grant(VoiceGrant(outgoing_application_sid=TWILIO_TWIML_APP_SID, incoming_allow=False))
    jwt = tok.to_jwt()
    return {"token": jwt if isinstance(jwt, str) else jwt.decode()}


# ---------------------------------------------------------- twilio webhooks


@app.post("/voice")
async def voice(request: Request):
    """Twilio hits this when the browser calls device.connect({params:{To, lead_id, campaign}})."""
    form = await require_twilio(request)
    resp = VoiceResponse()

    to = normalize_us(form.get("To", ""))
    if not to:
        resp.say("That is not a valid U.S. phone number.")
        return twiml(resp)
    if not TWILIO_CALLER_ID:
        resp.say("Caller ID is not configured.")
        return twiml(resp)

    lead_id = form.get("lead_id")
    lead_id = int(lead_id) if lead_id and lead_id.isdigit() else None
    agent = re.sub(r"[^\w .'-]", "", form.get("agent", ""))[:40]
    campaign = form.get("campaign") or "Small Business HVAC & Plumbing"

    with db() as c:
        if c.one("SELECT 1 AS x FROM leads WHERE phone = ? AND status = 'do_not_call'", (to,)):
            resp.say("This number is on your do not call list.")
            return twiml(resp)
        c.run(
            "INSERT INTO calls (call_sid, lead_id, to_number, started_at, agent, campaign) VALUES (?,?,?,?,?,?)"
            " ON CONFLICT DO NOTHING",
            (form.get("CallSid"), lead_id, to, now(), agent, campaign),
        )
        if lead_id:
            c.run("UPDATE leads SET last_called = ? WHERE id = ?", (now(), lead_id))

    dial = Dial(caller_id=TWILIO_CALLER_ID, answer_on_bridge=True, timeout=30)
    number_kwargs = {}
    if PUBLIC_BASE_URL:
        number_kwargs = {
            "status_callback": f"{PUBLIC_BASE_URL}/call-status",
            "status_callback_event": "initiated ringing answered completed",
            "status_callback_method": "POST",
        }
    dial.number(to, **number_kwargs)
    resp.append(dial)
    return twiml(resp)


@app.post("/call-status")
async def call_status(request: Request):
    """Progress events for the outbound leg; keyed by the browser leg's CallSid."""
    form = await require_twilio(request)
    parent = form.get("ParentCallSid") or form.get("CallSid")
    duration = form.get("CallDuration")
    with db() as c:
        c.run(
            "UPDATE calls SET status = ?, duration = COALESCE(?, duration) WHERE call_sid = ?",
            (form.get("CallStatus", "unknown"), int(duration) if duration and duration.isdigit() else None, parent),
        )
    return Response(status_code=204)


# ------------------------------------------------- inbound calls: voicemail


@app.post("/incoming")
async def incoming(request: Request):
    """Someone called our Twilio number: take a voicemail."""
    await require_twilio(request)
    resp = VoiceResponse()
    resp.say(
        f"Thanks for calling {BUSINESS_NAME}. We can't take your call right now. "
        "Please leave your name, number, and a short message after the tone, "
        "and we'll call you back.",
        voice="Polly.Joanna",
    )
    resp.record(
        action=f"{PUBLIC_BASE_URL}/voicemail-done", method="POST",
        max_length=120, play_beep=True, trim="trim-silence",
        recording_status_callback=f"{PUBLIC_BASE_URL}/voicemail-saved",
        recording_status_callback_method="POST",
        recording_status_callback_event="completed",
    )
    resp.say("Thanks, goodbye.", voice="Polly.Joanna")
    resp.hangup()
    return twiml(resp)


def save_voicemail(call_sid: str, from_number: str, recording_sid: str, recording_url: str, duration: str) -> None:
    if not recording_url or not duration.isdigit() or int(duration) < 2:
        return
    with db() as c:
        c.run(
            "INSERT INTO voicemails (call_sid, from_number, recording_sid, recording_url, duration, created_at)"
            " VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING",
            (call_sid, from_number, recording_sid, recording_url, int(duration), now()),
        )


@app.post("/voicemail-done")
async def voicemail_done(request: Request):
    form = await require_twilio(request)
    save_voicemail(form.get("CallSid", ""), form.get("From", ""), form.get("RecordingSid", ""),
                   form.get("RecordingUrl", ""), form.get("RecordingDuration", "0"))
    resp = VoiceResponse()
    resp.say("Thanks, we got your message. Goodbye.", voice="Polly.Joanna")
    resp.hangup()
    return twiml(resp)


@app.post("/voicemail-saved")
async def voicemail_saved(request: Request):
    form = await require_twilio(request)
    duration = form.get("RecordingDuration", "0")
    if form.get("RecordingStatus") != "completed" or not duration.isdigit() or int(duration) < 2:
        return Response(status_code=204)
    from_number = ""
    try:
        from twilio.rest import Client

        call = Client(TWILIO_API_KEY_SID, TWILIO_API_KEY_SECRET, TWILIO_ACCOUNT_SID).calls(
            form.get("CallSid")).fetch()
        from_number = call._from or ""
    except Exception:
        pass
    save_voicemail(form.get("CallSid", ""), from_number, form.get("RecordingSid", ""),
                   form.get("RecordingUrl", ""), duration)
    return Response(status_code=204)


@app.get("/api/voicemails")
def list_voicemails(request: Request):
    require_access(request)
    with db() as c:
        return c.all(
            "SELECT v.id, v.from_number, v.duration, v.heard, v.created_at, l.id AS lead_id, l.name AS lead_name"
            " FROM voicemails v LEFT JOIN leads l ON l.phone = v.from_number ORDER BY v.id DESC LIMIT 100"
        )


@app.get("/api/voicemails/{vm_id}/audio")
def voicemail_audio(request: Request, vm_id: int):
    require_access(request)
    import requests

    with db() as c:
        row = c.one("SELECT recording_url FROM voicemails WHERE id = ?", (vm_id,))
        c.run("UPDATE voicemails SET heard = 1 WHERE id = ?", (vm_id,))
    if not row or not row["recording_url"]:
        raise HTTPException(404, "Voicemail not found")
    r = requests.get(row["recording_url"] + ".mp3", auth=(TWILIO_API_KEY_SID, TWILIO_API_KEY_SECRET), timeout=30)
    if r.status_code != 200:
        raise HTTPException(502, "Couldn't fetch the recording from Twilio")
    return Response(r.content, media_type="audio/mpeg")


@app.delete("/api/voicemails/{vm_id}")
def delete_voicemail(request: Request, vm_id: int):
    require_access(request)
    with db() as c:
        c.run("DELETE FROM voicemails WHERE id = ?", (vm_id,))
    return Response(status_code=204)


# ------------------------------------------------------- spending alerts


@app.post("/usage-alert")
async def usage_alert(request: Request):
    form = await require_twilio(request)
    msg = (f"Twilio spending alert: {form.get('FriendlyName') or 'usage trigger'} — "
           f"current value ${form.get('CurrentValue', '?')} (limit ${form.get('TriggerValue', '?')}).")
    with db() as c:
        c.run("INSERT INTO alerts (message, created_at) VALUES (?, ?)", (msg, now()))
    return Response(status_code=204)


@app.get("/api/alerts")
def list_alerts(request: Request):
    require_access(request)
    with db() as c:
        return c.all("SELECT * FROM alerts WHERE dismissed = 0 ORDER BY id DESC LIMIT 10")


@app.post("/api/alerts/{alert_id}/dismiss")
def dismiss_alert(request: Request, alert_id: int):
    require_access(request)
    with db() as c:
        c.run("UPDATE alerts SET dismissed = 1 WHERE id = ?", (alert_id,))
    return Response(status_code=204)


# ---------------------------------------------------------------- leads API


class LeadIn(BaseModel):
    name: str
    phone: str
    city: str = ""
    state: str = ""
    category: str = ""
    notes: str = ""
    website: str = ""
    timezone: str = ""
    segment: str = "small_business"
    owner_name: str = ""
    email: str = ""
    employee_estimate: str = "1-10 (Estimated)"
    is_emergency: int = 0
    is_after_hours: int = 0
    services_offered: str = ""
    google_rating: float = 0.0
    review_count: int = 0
    source_url: str = ""


class LeadPatch(BaseModel):
    status: str | None = None
    notes: str | None = None
    website: str | None = None
    callback_at: str | None = None
    owner_name: str | None = None
    email: str | None = None
    employee_estimate: str | None = None
    is_emergency: int | None = None
    is_after_hours: int | None = None
    lead_score: int | None = None
    ai_opportunity: str | None = None


class BulkActionIn(BaseModel):
    action: str  # qualify, verify, status, delete
    lead_ids: List[int]
    status: Optional[str] = None
    segment: Optional[str] = None


@app.get("/api/leads")
def list_leads(
    request: Request,
    q: str = "",
    status: str = "",
    segment: str = "",
    category: str = "",
    state: str = "",
    city: str = "",
    min_score: Optional[int] = None,
    max_score: Optional[int] = None,
    ai_opportunity: str = "",
    is_emergency: Optional[int] = None,
    is_after_hours: Optional[int] = None,
    called: str = "",  # "called", "not_called"
    page: int = 1,
    page_size: int = 0,  # 0 = return array directly for backward compatibility
    sort_by: str = "",
    sort_order: str = "asc",
):
    require_access(request)
    sql = "SELECT * FROM leads WHERE 1=1"
    args: list = []

    if segment:
        sql += " AND segment = ?"
        args.append(segment)

    if q:
        sql += f" AND (name {LIKE} ? OR phone {LIKE} ? OR city {LIKE} ? OR category {LIKE} ? OR owner_name {LIKE} ?)"
        args += [f"%{q}%"] * 5

    if status:
        sql += " AND status = ?"
        args.append(status)

    if category:
        sql += f" AND category {LIKE} ?"
        args.append(f"%{category}%")

    if state:
        sql += " AND UPPER(state) = ?"
        args.append(state.strip().upper())

    if city:
        sql += f" AND city {LIKE} ?"
        args.append(f"%{city}%")

    if min_score is not None:
        sql += " AND lead_score >= ?"
        args.append(min_score)

    if max_score is not None:
        sql += " AND lead_score <= ?"
        args.append(max_score)

    if ai_opportunity:
        sql += f" AND ai_opportunity {LIKE} ?"
        args.append(f"%{ai_opportunity}%")

    if is_emergency is not None:
        sql += " AND is_emergency = ?"
        args.append(is_emergency)

    if is_after_hours is not None:
        sql += " AND is_after_hours = ?"
        args.append(is_after_hours)

    if called == "called":
        sql += " AND last_called IS NOT NULL"
    elif called == "not_called":
        sql += " AND (last_called IS NULL OR last_called = '')"

    # Sorting
    allowed_sort = {
        "id": "id",
        "lead_score": "lead_score",
        "name": "name",
        "last_called": "last_called",
        "city": "city",
        "state": "state",
        "google_rating": "google_rating",
    }
    order_col = allowed_sort.get(sort_by)
    direction = "DESC" if sort_order.lower() == "desc" else "ASC"

    if order_col:
        sql += f" ORDER BY {order_col} {direction}"
    else:
        sql += " ORDER BY (status = 'new') DESC, last_called IS NOT NULL, lead_score DESC, id"

    with db() as c:
        if page_size > 0:
            # Count total matching
            count_sql = f"SELECT COUNT(*) AS total FROM ({sql}) AS sub" if USE_PG else f"SELECT COUNT(*) AS total FROM ({sql})"
            total_row = c.one(count_sql, args)
            total = total_row["total"] if total_row else 0

            # Apply LIMIT and OFFSET
            offset = max(0, (page - 1) * page_size)
            paged_sql = sql + f" LIMIT {page_size} OFFSET {offset}"
            items = c.all(paged_sql, args)
            return {"leads": items, "total": total, "page": page, "page_size": page_size}

        # Legacy / unpaginated format
        return c.all(sql, args)


@app.get("/api/leads/stats")
def get_leads_stats(request: Request, segment: str = "small_business"):
    require_access(request)
    with db() as c:
        total = c.one("SELECT COUNT(*) AS c FROM leads WHERE segment = ?", (segment,))["c"]
        high_pri = c.one("SELECT COUNT(*) AS c FROM leads WHERE segment = ? AND lead_score >= 80", (segment,))["c"]
        med_pri = c.one("SELECT COUNT(*) AS c FROM leads WHERE segment = ? AND lead_score >= 60 AND lead_score < 80", (segment,))["c"]
        high_opp = c.one("SELECT COUNT(*) AS c FROM leads WHERE segment = ? AND ai_opportunity LIKE ?", (segment, "High%"))["c"]
        not_called = c.one("SELECT COUNT(*) AS c FROM leads WHERE segment = ? AND (last_called IS NULL OR last_called = '')", (segment,))["c"]
        calls_done = c.one(
            """
            SELECT COUNT(DISTINCT c.id) AS c
            FROM calls c JOIN leads l ON l.id = c.lead_id
            WHERE l.segment = ?
            """,
            (segment,),
        )["c"]
        interested = c.one("SELECT COUNT(*) AS c FROM leads WHERE segment = ? AND status = 'interested'", (segment,))["c"]
        callbacks = c.one("SELECT COUNT(*) AS c FROM leads WHERE segment = ? AND status = 'callback'", (segment,))["c"]

    return {
        "segment": segment,
        "total_leads": total,
        "high_priority_leads": high_pri,
        "medium_priority_leads": med_pri,
        "high_opportunity_leads": high_opp,
        "not_called_leads": not_called,
        "calls_completed": calls_done,
        "interested_prospects": interested,
        "follow_ups_required": callbacks,
    }


@app.post("/api/leads")
def add_lead(request: Request, lead: LeadIn):
    require_access(request)
    phone = normalize_us(lead.phone)
    if not phone:
        raise HTTPException(400, f"Not a valid US number: {lead.phone}")

    # Run AI Lead Qualification Engine
    lead_dict = lead.model_dump()
    lead_dict["phone"] = phone
    q_res = qualify_lead(lead_dict)

    tz = tz_for(phone, lead.state, lead.timezone)
    now_iso = now()
    campaign = "Small Business HVAC & Plumbing" if lead.segment == "small_business" else "Medium-Large HVAC & Plumbing"

    with db() as c:
        row = c.one(
            """
            INSERT INTO leads (
                segment, name, phone, city, state, category, status,
                notes, website, timezone, created_at,
                lead_score, ai_opportunity, ai_reasoning, qualification_details,
                confidence_level, employee_estimate, is_emergency, is_after_hours,
                owner_name, email, google_rating, review_count, source_url,
                verified_at, campaign, services_offered
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?
            ) ON CONFLICT DO NOTHING RETURNING *
            """,
            (
                lead.segment,
                lead.name.strip() or phone,
                phone,
                lead.city.strip(),
                lead.state.strip().upper(),
                lead.category.strip(),
                "new",
                lead.notes.strip(),
                lead.website.strip(),
                tz,
                now_iso,
                q_res["lead_score"],
                q_res["ai_opportunity"],
                q_res["ai_reasoning"],
                json.dumps(q_res["qualification_details"]),
                q_res["confidence_level"],
                q_res["employee_estimate"],
                q_res["is_emergency"],
                q_res["is_after_hours"],
                lead.owner_name.strip(),
                lead.email.strip(),
                float(lead.google_rating),
                int(lead.review_count),
                lead.source_url.strip(),
                now_iso,
                campaign,
                lead.services_offered.strip(),
            ),
        )
    if not row:
        raise HTTPException(409, "That phone number is already in your database.")
    return row


@app.post("/api/leads/import")
async def import_leads(
    request: Request,
    file: UploadFile,
    segment: str = Form("small_business"),
    preview: bool = Form(False),
):
    """Import CSV with automatic column mapping, duplicate detection, and preview support."""
    require_access(request)
    text = (await file.read()).decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))

    existing_phones = set()
    existing_domains = set()
    with db() as c:
        for r in c.all("SELECT phone, website FROM leads"):
            if r.get("phone"):
                existing_phones.add(r["phone"])
            w = r.get("website")
            if w:
                try:
                    d = urlparse(w if w.startswith("http") else "http://" + w).netloc.lower()
                    if d:
                        existing_domains.add(d.replace("www.", ""))
                except Exception:
                    pass

    sample_preview = []
    added, duplicates, invalid = 0, 0, []

    rows_to_insert = []
    for i, row in enumerate(reader, start=2):
        row_clean = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        phone_raw = (
            row_clean.get("phone")
            or row_clean.get("phone number")
            or row_clean.get("telephone")
            or row_clean.get("number")
            or ""
        )
        phone = normalize_us(phone_raw)
        if not phone:
            invalid.append(i)
            continue

        website = row_clean.get("website") or row_clean.get("url") or ""
        dom = ""
        if website:
            try:
                dom = urlparse(website if website.startswith("http") else "http://" + website).netloc.lower().replace("www.", "")
            except Exception:
                pass

        if phone in existing_phones or (dom and dom in existing_domains):
            duplicates += 1
            continue

        name = (
            row_clean.get("name")
            or row_clean.get("business name")
            or row_clean.get("business_name")
            or row_clean.get("company name")
            or row_clean.get("company_name")
            or row_clean.get("business")
            or row_clean.get("company")
            or phone
        )
        city = row_clean.get("city", "")
        state = row_clean.get("state", "").upper()
        category = row_clean.get("category") or row_clean.get("trade") or row_clean.get("industry") or ""
        notes = row_clean.get("notes") or row_clean.get("size / notes") or ""
        owner_name = row_clean.get("owner") or row_clean.get("owner_name") or row_clean.get("known contact") or ""
        email = row_clean.get("email") or ""

        lead_data = {
            "name": name,
            "phone": phone,
            "city": city,
            "state": state,
            "category": category,
            "notes": notes,
            "website": website,
            "owner_name": owner_name,
            "email": email,
        }
        q_res = qualify_lead(lead_data)

        if preview:
            if len(sample_preview) < 10:
                sample_preview.append({
                    "row_index": i,
                    "name": name,
                    "phone": phone,
                    "location": f"{city}, {state}".strip(", "),
                    "category": category,
                    "lead_score": q_res["lead_score"],
                    "ai_opportunity": q_res["ai_opportunity"],
                    "employee_estimate": q_res["employee_estimate"],
                })
            added += 1
        else:
            rows_to_insert.append({
                "name": name,
                "phone": phone,
                "city": city,
                "state": state,
                "category": category,
                "notes": notes,
                "website": website,
                "owner_name": owner_name,
                "email": email,
                "q_res": q_res,
                "timezone": tz_for(phone, state, row_clean.get("timezone", "")),
            })
            existing_phones.add(phone)
            if dom:
                existing_domains.add(dom)

    if preview:
        return {
            "preview": True,
            "total_evaluated": added + duplicates + len(invalid),
            "valid_new_leads": added,
            "duplicates_detected": duplicates,
            "invalid_phone_rows": invalid,
            "sample_leads": sample_preview,
        }

    now_iso = now()
    campaign = "Small Business HVAC & Plumbing" if segment == "small_business" else "Medium-Large HVAC & Plumbing"

    with db() as c:
        for r in rows_to_insert:
            q = r["q_res"]
            cur = c.run(
                """
                INSERT INTO leads (
                    segment, name, phone, city, state, category, status,
                    notes, website, timezone, created_at,
                    lead_score, ai_opportunity, ai_reasoning, qualification_details,
                    confidence_level, employee_estimate, is_emergency, is_after_hours,
                    owner_name, email, verified_at, campaign
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?, ?
                ) ON CONFLICT DO NOTHING
                """,
                (
                    segment,
                    r["name"],
                    r["phone"],
                    r["city"],
                    r["state"],
                    r["category"],
                    "new",
                    r["notes"],
                    r["website"],
                    r["timezone"],
                    now_iso,
                    q["lead_score"],
                    q["ai_opportunity"],
                    q["ai_reasoning"],
                    json.dumps(q["qualification_details"]),
                    q["confidence_level"],
                    q["employee_estimate"],
                    q["is_emergency"],
                    q["is_after_hours"],
                    r["owner_name"],
                    r["email"],
                    now_iso,
                    campaign,
                ),
            )
            added += cur.rowcount
            if cur.rowcount == 0:
                duplicates += 1

    return {"added": added, "duplicates": duplicates, "invalid_rows": invalid}


@app.patch("/api/leads/{lead_id}")
def update_lead(request: Request, lead_id: int, patch: LeadPatch):
    require_access(request)
    if patch.status is not None and patch.status not in LEAD_STATUSES:
        raise HTTPException(400, f"status must be one of {sorted(LEAD_STATUSES)}")

    with db() as c:
        if patch.status is not None:
            c.run("UPDATE leads SET status = ? WHERE id = ?", (patch.status, lead_id))
        if patch.notes is not None:
            c.run("UPDATE leads SET notes = ? WHERE id = ?", (patch.notes, lead_id))
        if patch.website is not None:
            c.run("UPDATE leads SET website = ? WHERE id = ?", (patch.website.strip()[:300], lead_id))
        if patch.callback_at is not None:
            c.run("UPDATE leads SET callback_at = ? WHERE id = ?", (patch.callback_at.strip()[:40], lead_id))
        if patch.owner_name is not None:
            c.run("UPDATE leads SET owner_name = ? WHERE id = ?", (patch.owner_name.strip(), lead_id))
        if patch.email is not None:
            c.run("UPDATE leads SET email = ? WHERE id = ?", (patch.email.strip(), lead_id))
        if patch.employee_estimate is not None:
            c.run("UPDATE leads SET employee_estimate = ? WHERE id = ?", (patch.employee_estimate.strip(), lead_id))
        if patch.is_emergency is not None:
            c.run("UPDATE leads SET is_emergency = ? WHERE id = ?", (patch.is_emergency, lead_id))
        if patch.is_after_hours is not None:
            c.run("UPDATE leads SET is_after_hours = ? WHERE id = ?", (patch.is_after_hours, lead_id))
        if patch.lead_score is not None:
            c.run("UPDATE leads SET lead_score = ? WHERE id = ?", (patch.lead_score, lead_id))
        if patch.ai_opportunity is not None:
            c.run("UPDATE leads SET ai_opportunity = ? WHERE id = ?", (patch.ai_opportunity, lead_id))

        row = c.one("SELECT * FROM leads WHERE id = ?", (lead_id,))
    if not row:
        raise HTTPException(404, "Lead not found")
    return row


@app.post("/api/leads/bulk-action")
def bulk_lead_action(request: Request, payload: BulkActionIn):
    require_access(request)
    action = payload.action.lower()
    lead_ids = payload.lead_ids

    if not lead_ids and action != "qualify_all":
        raise HTTPException(400, "No lead IDs provided.")

    with db() as c:
        if action == "status":
            if not payload.status or payload.status not in LEAD_STATUSES:
                raise HTTPException(400, f"Valid status required: {sorted(LEAD_STATUSES)}")
            for lid in lead_ids:
                c.run("UPDATE leads SET status = ? WHERE id = ?", (payload.status, lid))
            return {"updated": len(lead_ids), "action": "status", "status": payload.status}

        elif action == "verify":
            now_iso = now()
            for lid in lead_ids:
                c.run("UPDATE leads SET verified_at = ? WHERE id = ?", (now_iso, lid))
            return {"updated": len(lead_ids), "action": "verify", "verified_at": now_iso}

        elif action == "qualify":
            updated_count = 0
            for lid in lead_ids:
                row = c.one("SELECT * FROM leads WHERE id = ?", (lid,))
                if row:
                    q = qualify_lead(row)
                    c.run(
                        """
                        UPDATE leads SET
                            lead_score = ?, ai_opportunity = ?, ai_reasoning = ?,
                            qualification_details = ?, confidence_level = ?, employee_estimate = ?,
                            is_emergency = ?, is_after_hours = ?
                        WHERE id = ?
                        """,
                        (
                            q["lead_score"],
                            q["ai_opportunity"],
                            q["ai_reasoning"],
                            json.dumps(q["qualification_details"]),
                            q["confidence_level"],
                            q["employee_estimate"],
                            q["is_emergency"],
                            q["is_after_hours"],
                            lid,
                        ),
                    )
                    updated_count += 1
            return {"updated": updated_count, "action": "qualify"}

        elif action == "delete":
            for lid in lead_ids:
                c.run("DELETE FROM leads WHERE id = ?", (lid,))
            return {"deleted": len(lead_ids), "action": "delete"}

        else:
            raise HTTPException(400, f"Unsupported bulk action: {action}")


@app.get("/api/leads/export")
def export_leads(request: Request, segment: str = ""):
    require_access(request)
    cols = [
        "id", "segment", "name", "phone", "city", "state", "category",
        "lead_score", "ai_opportunity", "employee_estimate", "owner_name", "email",
        "is_emergency", "is_after_hours", "timezone", "status", "notes",
        "website", "callback_at", "last_called", "source_url", "verified_at", "created_at",
    ]
    sql = f"SELECT {', '.join(cols)} FROM leads"
    args = []
    if segment:
        sql += " WHERE segment = ?"
        args.append(segment)
    sql += " ORDER BY id"

    with db() as c:
        rows = c.all(sql, args)

    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols)
    w.writeheader()
    w.writerows(rows)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    prefix = segment if segment else "all"
    return Response(
        buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="flowchain-{prefix}-leads-{stamp}.csv"'},
    )


@app.get("/api/tz")
def lookup_tz(request: Request, number: str = ""):
    require_access(request)
    phone = normalize_us(number)
    return {"phone": phone, "timezone": tz_for(phone) if phone else ""}


@app.delete("/api/leads/{lead_id}")
def delete_lead(request: Request, lead_id: int):
    require_access(request)
    with db() as c:
        c.run("DELETE FROM leads WHERE id = ?", (lead_id,))
    return Response(status_code=204)


@app.get("/api/calls")
def list_calls(
    request: Request,
    lead_id: int | None = None,
    campaign: str = "",
    since: str = "",
    limit: int = 50,
):
    require_access(request)
    sql = (
        "SELECT calls.*, leads.name AS lead_name, leads.status AS lead_status, leads.segment AS lead_segment "
        "FROM calls LEFT JOIN leads ON leads.id = calls.lead_id WHERE 1=1"
    )
    args: list = []
    if lead_id is not None:
        sql += " AND calls.lead_id = ?"
        args.append(lead_id)
    if campaign:
        sql += " AND calls.campaign = ?"
        args.append(campaign)
    if since:
        sql += " AND calls.started_at >= ?"
        args.append(since[:25])
    sql += " ORDER BY calls.id DESC LIMIT ?"
    args.append(min(limit, 500))
    with db() as c:
        return c.all(sql, args)


# ------------------------------------------------------------- Discovery API


class DiscoveryStartIn(BaseModel):
    trade: str = "Both"
    state: str = "TX"
    cities: str = "Dallas, Houston, Austin"
    max_leads: int = 20
    min_score: int = 60
    min_opportunity: str = "Medium potential"


@app.post("/api/discovery/start")
def start_discovery_job(request: Request, payload: DiscoveryStartIn):
    require_access(request)
    job_id = str(uuid.uuid4())[:8]
    now_iso = now()

    with db() as c:
        c.run(
            """
            INSERT INTO discovery_jobs (
                id, status, trade, state, cities, max_leads,
                min_score, min_opportunity, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                "running",
                payload.trade,
                payload.state,
                payload.cities,
                payload.max_leads,
                payload.min_score,
                payload.min_opportunity,
                now_iso,
            ),
        )

    pipeline = DiscoveryPipeline(apify_token=APIFY_TOKEN, db_context_factory=db)

    def worker():
        try:
            pipeline.run_job(
                job_id=job_id,
                trade=payload.trade,
                state=payload.state,
                cities_str=payload.cities,
                max_leads=payload.max_leads,
                min_score=payload.min_score,
                min_opportunity=payload.min_opportunity,
                tz_resolver=tz_for,
            )
        except Exception as e:
            logger.error(f"Discovery job {job_id} failed: {e}")
            with db() as c:
                c.run(
                    "UPDATE discovery_jobs SET status = 'failed', error_message = ? WHERE id = ?",
                    (str(e), job_id),
                )

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()

    return {"job_id": job_id, "status": "running", "message": f"Discovery job {job_id} launched."}


@app.get("/api/discovery/jobs")
def list_discovery_jobs(request: Request):
    require_access(request)
    with db() as c:
        return c.all("SELECT * FROM discovery_jobs ORDER BY created_at DESC LIMIT 20")


@app.get("/api/discovery/jobs/{job_id}")
def get_discovery_job(request: Request, job_id: str):
    require_access(request)
    with db() as c:
        row = c.one("SELECT * FROM discovery_jobs WHERE id = ?", (job_id,))
    if not row:
        raise HTTPException(404, f"Discovery job {job_id} not found.")
    return row


@app.exception_handler(HTTPException)
async def http_error(_: Request, exc: HTTPException):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
