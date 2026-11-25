"""
One-off script: import curated HVAC leads from US_HVAC_Plumbing_Leads.xlsx
into the Small Business segment via the local API.

Usage:
  .venv\Scripts\python.exe tools\import_xlsx_leads.py
  .venv\Scripts\python.exe tools\import_xlsx_leads.py --base https://twilio-dialer-l2kg.onrender.com --key YOUR_KEY

The script reads the 'Leads' sheet, maps columns, and POSTs each row to
POST /api/leads with segment=small_business.  It skips rows with no phone.
"""
import argparse
import re
import sys
from pathlib import Path

try:
    import openpyxl
except ImportError:
    sys.exit("Install openpyxl first: pip install openpyxl")

try:
    import requests
except ImportError:
    sys.exit("Install requests first: pip install requests")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
XLSX_PATH = Path("D:/Downloads/US_HVAC_Plumbing_Leads.xlsx")
DEFAULT_BASE = "http://127.0.0.1:8000"
DEFAULT_KEY  = "pHQgYXVFlkwZPpJDpvEO674zhOC3ZFON"

PRIORITY_MAP = {
    "High":   "High priority",
    "Medium": "Medium priority",
    "Low":    "Low priority",
}

# City/State split helper ─────────────────────────────────────────────────────
def split_city_state(city_state: str):
    """'Phoenix, AZ' → ('Phoenix', 'AZ')"""
    if not city_state:
        return "", ""
    parts = city_state.rsplit(",", 1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return city_state.strip(), ""


def infer_industry(notes: str, name: str) -> str:
    text = f"{name} {notes}".lower()
    if any(w in text for w in ["plumb", "drain", "water heater", "sewer"]):
        return "Plumbing"
    if any(w in text for w in ["hvac", "air condition", "cooling", "heating", "ac ", " ac,"]):
        return "HVAC"
    return "HVAC"  # default for this list


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default=DEFAULT_BASE)
    parser.add_argument("--key",  default=DEFAULT_KEY)
    args = parser.parse_args()

    base = args.base.rstrip("/")
    key  = args.key
    session = requests.Session()

    # Step 1: hit the secret link to establish the cookie
    r = session.get(f"{base}/?key={key}", allow_redirects=True, timeout=10)
    # Step 2: also set the cookie manually in case redirect stripped it
    session.cookies.set("access_key", key, domain="127.0.0.1")
    session.cookies.set("access_key", key)

    # Step 3: verify health (pass key as query param for safety)
    r = session.get(f"{base}/health?key={key}", timeout=10)
    r.raise_for_status()
    print(f"✅ Connected to {base} — DB: {r.json().get('db')}")

    wb = openpyxl.load_workbook(str(XLSX_PATH), data_only=True)
    ws = wb["Leads"]
    headers = [str(c.value).strip() if c.value else "" for c in next(ws.iter_rows(min_row=1, max_row=1))]

    col = {h: i for i, h in enumerate(headers)}

    imported = 0
    skipped  = 0
    errors   = 0

    for row_idx, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        if not any(row):
            continue

        def g(colname):
            idx = col.get(colname)
            if idx is None:
                return ""
            v = row[idx]
            return str(v).strip() if v is not None else ""

        name     = g("Company")
        phone    = g("Phone")
        city_st  = g("City, State")
        priority = g("Priority")
        contact  = g("Known contact")
        notes_sz = g("Size / notes")
        status   = g("Status")
        source   = g("Source")
        followup = g("Follow-up date")
        call_notes = g("Notes")

        if not name or not phone:
            print(f"  Row {row_idx}: SKIP — missing name or phone")
            skipped += 1
            continue

        city, state = split_city_state(city_st)
        industry = infer_industry(notes_sz, name)

        # Map priority → score hint (actual scoring done server-side)
        # We pass notes as services_offered so qualifier can use them
        payload = {
            "name":            name,
            "phone":           phone,
            "city":            city,
            "state":           state,
            "industry":        industry,
            "segment":         "small_business",
            "owner_name":      contact if contact and contact != "None" else "",
            "services_offered": notes_sz,
            "source_url":      source if source and source != "None" else "",
            "notes":           call_notes if call_notes and call_notes != "None" else "",
            "follow_up_date":  followup if followup and followup != "None" else "",
            # Map existing call status
            "call_status":     "not_called" if "Not called" in status else "called",
        }

        # Remove blank strings to let server use defaults
        payload = {k: v for k, v in payload.items() if v}

        try:
            r = session.post(
                f"{base}/api/leads?key={key}",
                json=payload,
                timeout=30,
            )
            if r.status_code == 200:
                data = r.json()
                score = data.get("lead_score", "?")
                opp   = data.get("ai_opportunity", "?")
                print(f"  ✅ Row {row_idx}: {name} — Score: {score} | Opp: {opp}")
                imported += 1
            elif r.status_code == 409:
                print(f"  ⚠️  Row {row_idx}: {name} — DUPLICATE skipped")
                skipped += 1
            else:
                print(f"  ❌ Row {row_idx}: {name} — HTTP {r.status_code}: {r.text[:200]}")
                errors += 1
        except Exception as e:
            print(f"  ❌ Row {row_idx}: {name} — Exception: {e}")
            errors += 1

    print(f"\n{'='*50}")
    print(f"Import complete: {imported} imported | {skipped} skipped | {errors} errors")


if __name__ == "__main__":
    main()
