"""Build a call list from Google Maps via Apify (runs on Apify's servers, not this PC).

Usage:
  python tools/apify_leads.py check              # token + credit check, no charges
  python tools/apify_leads.py run [per_search]   # scrape (default 15 places per search) -> leads_apify.csv
  python tools/apify_leads.py import             # import leads_apify.csv into the live dialer

Needs APIFY_TOKEN in .env (and ACCESS_KEY + PUBLIC_BASE_URL/RENDER URL for import).
"""
import csv
import re
import sys
import time
from pathlib import Path

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
ENV = dotenv_values(ROOT / ".env")
TOKEN = (ENV.get("APIFY_TOKEN") or "").strip()
ACTOR = "compass~crawler-google-places"
API = "https://api.apify.com/v2"
DIALER = "https://twilio-dialer-l2kg.onrender.com"
OUT = ROOT / "leads_apify.csv"

TRADES = ["HVAC contractor", "plumber"]
CITIES = [
    # West Coast / Mountain-Arizona: open late afternoon when calling at 5:30 AM IST
    ("Los Angeles", "CA"), ("San Diego", "CA"), ("San Jose", "CA"), ("Sacramento", "CA"),
    ("Fresno", "CA"), ("Riverside", "CA"), ("Portland", "OR"), ("Seattle", "WA"),
    ("Las Vegas", "NV"), ("Phoenix", "AZ"), ("Tucson", "AZ"),
    # Texas + Florida: call ~7:30-9:30 PM IST
    ("Houston", "TX"), ("Dallas", "TX"), ("San Antonio", "TX"), ("Austin", "TX"), ("Fort Worth", "TX"),
    ("Miami", "FL"), ("Tampa", "FL"), ("Orlando", "FL"), ("Jacksonville", "FL"),
]

# Chains, franchises and non-contractors: the AI receptionist pitch fits small owner-run shops
SKIP = re.compile(
    r"home depot|lowe'?s|ferguson|johnstone|winsupply|supply|wholesale|distribut|parts|"
    r"roto-?rooter|mr\.? rooter|benjamin franklin|one hour|aire serv|service experts|"
    r"rescue rooter|sears|ace hardware|hardware|abc home|horizon services|goettl|"
    r"rental|school|college|academy|union|local \d+", re.I)


def H():
    if not TOKEN:
        sys.exit("APIFY_TOKEN missing from .env")
    return {"Authorization": f"Bearer {TOKEN}"}


def check():
    me = httpx.get(f"{API}/users/me", headers=H(), timeout=30)
    if me.status_code != 200:
        sys.exit(f"Apify rejected the token (HTTP {me.status_code}). Copy it again from Settings > Integrations.")
    lim = httpx.get(f"{API}/users/me/limits", headers=H(), timeout=30).json().get("data", {})
    used = lim.get("current", {}).get("monthlyUsageUsd", 0)
    cap = lim.get("limits", {}).get("maxMonthlyUsageUsd", 0)
    print(f"Apify OK | plan: {me.json()['data'].get('plan', {}).get('id')} | used ${used:.2f} of ${cap:.2f} this cycle")
    return cap - used


def run(per_search=15):
    left = check()
    searches = [f"{t} in {c}, {s}" for (c, s) in CITIES for t in TRADES]
    est = len(searches) * per_search * 0.0055 + 0.05
    print(f"{len(searches)} searches x {per_search} places = up to {len(searches) * per_search} places, est. ${est:.2f}")
    if est > left:
        sys.exit(f"Not enough free credit left (${left:.2f}). Lower per_search.")
    body = {
        "searchStringsArray": searches,
        "maxCrawledPlacesPerSearch": per_search,
        "language": "en",
        "countryCode": "us",
        "skipClosedPlaces": True,
        "scrapePlaceDetailPage": False,
        "scrapeContacts": False,
        "maxReviews": 0,
        "maxImages": 0,
    }
    r = httpx.post(f"{API}/acts/{ACTOR}/runs", headers=H(), json=body,
                   params={"maxTotalChargeUsd": round(est + 0.5, 2)}, timeout=60)
    r.raise_for_status()
    run_ = r.json()["data"]
    print("run started:", run_["id"])
    while run_["status"] in ("READY", "RUNNING"):
        time.sleep(15)
        run_ = httpx.get(f"{API}/actor-runs/{run_['id']}", headers=H(), timeout=30).json()["data"]
        print(" ", run_["status"], "| items so far:", (run_.get("stats") or {}).get("resultCount", "?"))
    if run_["status"] != "SUCCEEDED":
        sys.exit(f"run ended: {run_['status']}")
    items = httpx.get(f"{API}/datasets/{run_['defaultDatasetId']}/items", headers=H(),
                      params={"clean": "true", "format": "json"}, timeout=120).json()
    print("raw places:", len(items), "| cost: $%.2f" % (run_.get("usageTotalUsd") or 0))
    clean(items)


def norm_phone(p):
    d = re.sub(r"\D", "", p or "")
    if len(d) == 10:
        d = "1" + d
    if re.fullmatch(r"1[2-9]\d{2}[2-9]\d{6}", d) and d[1:4] not in ("800", "833", "844", "855", "866", "877", "888") and d[4:7] != "555":
        return "+" + d
    return None


def clean(items):
    existing = set()
    try:  # skip numbers already in the dialer
        c = httpx.Client(base_url=DIALER, timeout=60)
        c.get("/", params={"key": ENV.get("ACCESS_KEY", "")})
        existing = {l["phone"] for l in c.get("/api/leads").json()}
    except Exception as e:
        print("could not read current leads:", e)
    rows, seen = [], set()
    for it in items:
        name = (it.get("title") or "").strip()
        phone = norm_phone(it.get("phoneUnformatted") or it.get("phone"))
        if not name or not phone or phone in seen or phone in existing or SKIP.search(name):
            continue
        if it.get("permanentlyClosed") or it.get("temporarilyClosed"):
            continue
        rating, reviews = it.get("totalScore") or 0, it.get("reviewsCount") or 0
        if reviews and rating and rating < 3.5:
            continue
        if reviews > 1500:  # big operations already have call centers
            continue
        cat = (it.get("categoryName") or "").lower()
        trade = "Plumbing" if "plumb" in cat or "plumb" in (it.get("searchString") or "").lower() else "HVAC"
        seen.add(phone)
        # service-area businesses have no address: fall back to the city we searched
        m = re.search(r" in (.+), ([A-Z]{2})$", it.get("searchString") or "")
        city = it.get("city") or (m.group(1) if m else "")
        state = _abbr(it.get("state") or "") or (m.group(2) if m else "")
        rows.append({
            "name": name, "phone": phone, "city": city, "state": state,
            "category": trade, "website": it.get("website") or "",
            "notes": f"Google: {rating}★ ({reviews} reviews)" + (f" · {it.get('categoryName')}" if it.get("categoryName") else ""),
        })
    st = {"CA": 0, "OR": 0, "WA": 0, "NV": 0, "AZ": 1, "TX": 2, "FL": 3}
    rows.sort(key=lambda r: (st.get(_abbr(r["state"]), 9), r["city"], r["name"]))
    with open(OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader(); w.writerows(rows)
    from collections import Counter
    print(f"kept {len(rows)} new leads -> {OUT.name}")
    print("by state:", Counter(_abbr(r['state']) for r in rows).most_common())
    print("by trade:", Counter(r['category'] for r in rows))


STATES = {"California": "CA", "Oregon": "OR", "Washington": "WA", "Nevada": "NV", "Arizona": "AZ", "Texas": "TX", "Florida": "FL"}


def _abbr(s):
    return STATES.get(s, s)


def do_import():
    c = httpx.Client(base_url=DIALER, timeout=120)
    c.get("/", params={"key": ENV.get("ACCESS_KEY", "")})
    rows = list(csv.DictReader(open(OUT, encoding="utf-8")))
    for r in rows:
        r["state"] = _abbr(r["state"])
    import io
    buf = io.StringIO(); w = csv.DictWriter(buf, fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)
    print(c.post("/api/leads/import", files={"file": ("leads_apify.csv", buf.getvalue().encode(), "text/csv")}).json())


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    {"check": check, "run": lambda: run(int(sys.argv[2]) if len(sys.argv) > 2 else 15), "import": do_import}[cmd]()
