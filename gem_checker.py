"""GeM Bid Checker - verify an IT vendor's tender claims before the government pays.

Checks (each produces Findings with source + passage + date):
  1. Identity      - domain resolves to the claimed firm; lookalike-domain detection
  2. Capacity      - headcount / founding year vs claims; shell-company signals
  3. GST           - GSTIN format + checksum, PAN/state/entity consistency,
                     public status lookup (active / cancelled / suspended), filing regularity
  4. Firm legal    - pending cases, insolvency (NCLT/IBC), debarment, adverse media
  5. People        - each key person currently works at the vendor (not namesake / ex-employee)
  6. People legal  - cases / fraud / disqualification against each key person

Scores are combined with a weighted formula (see WEIGHTS and score()).
Data: Crustdata API (company, person, job, web search) + bidder-declared fields.

Run:  pip install httpx rapidfuzz
      export CRUSTDATA_API_KEY=...
      python gem_checker.py bids/bid_lookalike.json            # prompts for missing fields
      python gem_checker.py bids/bid_honest.json --no-prompt --yes
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from dataclasses import asdict, dataclass, field

BASE = "https://api.crustdata.com"
API_VERSION = "2025-11-01"
MAX_CREDITS = 40
MAX_SECONDS = 180

# --------------------------------------------------------------------------- weights
# Weighted formula. Each category gets a sub-score in [0, 1] (or None = no evidence).
#   score = 100 * sum(w_i * s_i) / sum(w_i)        with s_i = 0.5 when unknown
#   coverage = sum(w_i for categories with evidence) / sum(w_i)
# Knockout rules (cancelled GST, debarment, lookalike domain, criminal/fraud case
# against firm or key person) force REJECT regardless of score.
WEIGHTS = {
    "identity": 15,      # is this really the firm it claims to be?
    "capacity": 20,      # can it actually deliver? (headcount, age, hiring, shell signals)
    "gst": 20,           # tax registration genuine, active, filing regularly
    "firm_legal": 20,    # pending cases / insolvency / debarment against the firm
    "people": 15,        # named key personnel really work there
    "people_legal": 10,  # cases against key personnel
}
UNKNOWN_PRIOR = 0.5
APPROVE_AT, HOLD_AT, MIN_COVERAGE = 75, 50, 0.6

# --------------------------------------------------------------------------- findings
CONFIRMED, CONTRADICTED, CANT_VERIFY, RED_FLAG = "confirmed", "contradicted", "cant_verify", "red_flag"


@dataclass
class Finding:
    category: str
    check: str
    status: str
    score: float | None          # contribution in [0,1]; None = no evidence
    detail: str
    source: str = ""
    passage: str = ""
    date: str = ""
    knockout: bool = False


@dataclass
class Report:
    vendor: str
    findings: list[Finding] = field(default_factory=list)
    credits_spent: float = 0.0

    def add(self, *a, **k):
        f = Finding(*a, **k)
        self.findings.append(f)
        return f


# --------------------------------------------------------------------------- client
class BudgetExceeded(BaseException):
    """BaseException so generic `except Exception` in checks never swallows a budget stop."""


class Crust:
    """Budgeted, cached Crustdata client. Re-runs hit the SQLite cache and cost nothing."""

    def __init__(self, key: str, max_credits=MAX_CREDITS, max_seconds=MAX_SECONDS, db="cache.db"):
        import httpx  # local import so tests can run without network deps

        self.http = httpx.Client(timeout=60)
        self.hdrs = {"authorization": f"Bearer {key}", "x-api-version": API_VERSION,
                     "content-type": "application/json"}
        self.max_credits, self.deadline, self.spent = max_credits, time.time() + max_seconds, 0.0
        self.db = sqlite3.connect(db)
        self.db.execute("create table if not exists cache(k text primary key, v text)")
        self.db.execute("create table if not exists ledger(ts real, path text, credits real)")

    def post(self, path: str, body: dict, est_cost: float = 1.0):
        k = hashlib.sha256((path + json.dumps(body, sort_keys=True)).encode()).hexdigest()
        row = self.db.execute("select v from cache where k=?", (k,)).fetchone()
        if row:
            return json.loads(row[0])
        if time.time() > self.deadline or self.spent + est_cost > self.max_credits:
            raise BudgetExceeded(f"spent {self.spent:.1f} credits")
        for attempt in range(4):
            r = self.http.post(BASE + path, headers=self.hdrs, json=body)
            if r.status_code not in (429, 502, 503):
                break
            time.sleep(2 ** attempt)
        if r.status_code >= 400:
            raise RuntimeError(f"{path} -> HTTP {r.status_code}: {r.text[:300]}")
        data = r.json()
        cost = _num(dig(data, "credits_used", "credits", "cost")) or est_cost
        self.spent += cost
        self.db.execute("insert into ledger values(?,?,?)", (time.time(), path, cost))
        self.db.execute("insert or replace into cache values(?,?)", (k, json.dumps(data)))
        self.db.commit()
        return data


# --------------------------------------------------------------------------- helpers
def dig(obj, *keys):
    """Depth-first search for the first non-empty value under any of `keys`.
    Response shapes vary by endpoint/version, so we search instead of hard-coding paths."""
    if isinstance(obj, dict):
        for k in keys:
            if k in obj and obj[k] not in (None, "", [], {}):
                return obj[k]
        for v in obj.values():
            r = dig(v, *keys)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = dig(v, *keys)
            if r is not None:
                return r
    return None


def records(obj, *keys):
    """Return the first list of dicts found under any of `keys` (or obj itself if a list)."""
    if isinstance(obj, list):
        return [x for x in obj if isinstance(x, dict)]
    v = dig(obj, *keys)
    return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []


def _num(v):
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def norm_name(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"\b(private|pvt|limited|ltd|llp|inc|technologies|technology|solutions|services|"
               r"infotech|systems|india|co|company|corp|corporation)\b\.?", " ", s)
    return re.sub(r"[^a-z0-9 ]", " ", re.sub(r"\s+", " ", s)).strip()


def name_sim(a: str, b: str) -> float:
    from rapidfuzz import fuzz

    a, b = norm_name(a), norm_name(b)
    if not a or not b:
        return 0.0
    return max(fuzz.token_sort_ratio(a, b), fuzz.partial_ratio(a, b) if min(len(a), len(b)) >= 5 else 0)


def domain_root(d: str) -> str:
    d = re.sub(r"^https?://", "", (d or "").lower()).split("/")[0]
    d = re.sub(r"^www\.", "", d)
    return d


def snippet_of(r: dict) -> str:
    return str(r.get("snippet") or r.get("description") or r.get("content") or r.get("text") or "")[:400]


def web_search(c: Crust, query: str, rep: Report, **extra):
    body = {"query": query, **extra}
    data = c.post("/web/search/live", body, est_cost=1)
    return records(data, "results", "data", "items")


# --------------------------------------------------------------------------- 1. identity
def check_identity(c: Crust, bid: dict, rep: Report) -> dict | None:
    dom, name = domain_root(bid.get("domain", "")), bid["vendor_name"]
    company = None
    try:
        data = c.post("/company/identify", {"domains": [dom]}, est_cost=0)
        company = (records(data, "results", "data", "matches", "companies") or [None])[0]
    except Exception as e:  # noqa: BLE001
        rep.add("identity", "domain_lookup", CANT_VERIFY, None, f"identify failed: {e}", "crustdata:/company/identify")

    if company:
        real_name = dig(company, "name", "company_name") or ""
        sim = name_sim(name, real_name)
        if sim >= 85:
            rep.add("identity", "domain_owner", CONFIRMED, 1.0,
                    f"{dom} belongs to '{real_name}' (match {sim:.0f}%)", "crustdata:/company/identify",
                    passage=real_name)
        else:
            rep.add("identity", "domain_owner", CONTRADICTED, 0.0,
                    f"{dom} belongs to '{real_name}', not '{name}' (match {sim:.0f}%)",
                    "crustdata:/company/identify", passage=real_name)
    else:
        rep.add("identity", "domain_owner", CANT_VERIFY, None, f"No company record for {dom}",
                "crustdata:/company/identify")

    # Lookalike check: who does the *name* resolve to? If a well-known firm with the same name
    # owns a different, similar-looking domain, the bid domain is likely an impersonation.
    try:
        data = c.post("/company/identify", {"names": [name]}, est_cost=0)
        for m in records(data, "results", "data", "matches", "companies")[:5]:
            mdom = domain_root(dig(m, "primary_domain", "domain", "website") or "")
            if not mdom or mdom == dom:
                continue
            from rapidfuzz import fuzz

            dsim = fuzz.ratio(mdom.split(".")[0], dom.split(".")[0])
            nsim = name_sim(name, dig(m, "name", "company_name") or "")
            if nsim >= 85 and dsim >= 70:
                rep.add("identity", "lookalike_domain", RED_FLAG, 0.0,
                        f"'{name}' is registered on {mdom}; bid uses lookalike {dom} (similarity {dsim:.0f}%)",
                        "crustdata:/company/identify", passage=mdom, knockout=True)
                return company
    except Exception:  # noqa: BLE001 - fallback key may differ by API version
        pass
    return company


# --------------------------------------------------------------------------- 2. capacity
def check_capacity(c: Crust, bid: dict, rep: Report, company: dict | None):
    dom = domain_root(bid.get("domain", ""))
    try:
        data = c.post("/company/enrich", {"domains": [dom], "fields": ["basic_info", "headcount", "locations"]},
                      est_cost=3)
    except Exception as e:  # noqa: BLE001
        rep.add("capacity", "enrich", CANT_VERIFY, None, f"enrich failed: {e}", "crustdata:/company/enrich")
        return
    hc = _num(dig(data, "total", "headcount_total", "employee_count", "linkedin_headcount"))
    founded = _num(dig(data, "year_founded", "founded_year", "founded"))
    asof = str(dig(data, "last_updated", "updated_at") or dt.date.today())
    src = "crustdata:/company/enrich"

    claimed_hc = _num(bid.get("claimed_headcount"))
    if hc is not None and claimed_hc:
        ratio = hc / claimed_hc
        if ratio >= 0.5:
            rep.add("capacity", "headcount", CONFIRMED, min(1.0, ratio), f"Claimed {claimed_hc:.0f}, observed {hc:.0f}",
                    src, passage=f"headcount={hc:.0f}", date=asof)
        else:
            rep.add("capacity", "headcount", CONTRADICTED, ratio,
                    f"Claimed {claimed_hc:.0f} staff but only {hc:.0f} observed ({ratio:.0%})",
                    src, passage=f"headcount={hc:.0f}", date=asof)
    else:
        rep.add("capacity", "headcount", CANT_VERIFY, None, "Headcount not available", src)

    claimed_yr = _num(bid.get("claimed_year_founded"))
    if founded and claimed_yr:
        if abs(founded - claimed_yr) <= 1:
            rep.add("capacity", "founded", CONFIRMED, 1.0, f"Founded {founded:.0f} (claimed {claimed_yr:.0f})", src,
                    passage=f"year_founded={founded:.0f}")
        else:
            rep.add("capacity", "founded", CONTRADICTED, 0.0,
                    f"Claimed founded {claimed_yr:.0f}, records show {founded:.0f}", src,
                    passage=f"year_founded={founded:.0f}")

    # Shell-company signal: tiny team vs big tender / big claimed turnover.
    tv = _num((bid.get("tender") or {}).get("value_inr"))
    turnover = _num(bid.get("claimed_turnover_inr"))
    if hc is not None:
        if tv and tv / max(hc, 1) > 2.5e7:  # > ₹2.5 cr of contract per observed employee
            rep.add("capacity", "shell_signal_tender", RED_FLAG, 0.1,
                    f"Tender ₹{tv/1e7:.1f} cr vs {hc:.0f} staff = ₹{tv/max(hc,1)/1e7:.1f} cr per head", src,
                    passage=f"headcount={hc:.0f}")
        if turnover and turnover / max(hc, 1) > 1.5e7:  # IT services ~₹10-40 lakh revenue / employee
            rep.add("capacity", "shell_signal_turnover", RED_FLAG, 0.1,
                    f"Claimed turnover ₹{turnover/1e7:.1f} cr with {hc:.0f} staff "
                    f"(₹{turnover/max(hc,1)/1e5:.0f} lakh/head; IT norm is ₹10–40 lakh)", src)

    # Hiring signal: a firm bidding on a large build usually has open roles.
    name = dig(company or {}, "name", "company_name") or bid["vendor_name"]
    try:
        jobs = c.post("/job/search", {
            "filters": {"op": "and", "conditions": [
                {"field": "company.basic_info.name", "type": "in", "value": [name]}]},
            "limit": 5}, est_cost=0.15)
        n = len(records(jobs, "jobs", "results", "data"))
        total = _num(dig(jobs, "total_count", "total")) or n
        if total:
            rep.add("capacity", "hiring", CONFIRMED, 1.0, f"{total:.0f} open job listings found", "crustdata:/job/search")
        elif tv and tv >= 5e7:
            rep.add("capacity", "hiring", CONTRADICTED, 0.3,
                    f"No open roles despite bidding ₹{tv/1e7:.1f} cr build", "crustdata:/job/search")
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- 3. GST
GST_STATES = {
    "01": "Jammu and Kashmir", "02": "Himachal Pradesh", "03": "Punjab", "04": "Chandigarh",
    "05": "Uttarakhand", "06": "Haryana", "07": "Delhi", "08": "Rajasthan", "09": "Uttar Pradesh",
    "10": "Bihar", "11": "Sikkim", "12": "Arunachal Pradesh", "13": "Nagaland", "14": "Manipur",
    "15": "Mizoram", "16": "Tripura", "17": "Meghalaya", "18": "Assam", "19": "West Bengal",
    "20": "Jharkhand", "21": "Odisha", "22": "Chhattisgarh", "23": "Madhya Pradesh", "24": "Gujarat",
    "26": "Dadra and Nagar Haveli and Daman and Diu", "27": "Maharashtra", "29": "Karnataka",
    "30": "Goa", "31": "Lakshadweep", "32": "Kerala", "33": "Tamil Nadu", "34": "Puducherry",
    "35": "Andaman and Nicobar Islands", "36": "Telangana", "37": "Andhra Pradesh", "38": "Ladakh",
    "97": "Other Territory",
}
PAN_ENTITY = {"C": "company", "F": "firm/llp", "P": "individual/proprietor", "H": "huf", "A": "aop",
              "T": "trust", "B": "boi", "L": "local authority", "J": "artificial juridical", "G": "government"}
_GST_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
GSTIN_RE = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")


def gstin_checksum_ok(g: str) -> bool:
    total = 0
    for i, ch in enumerate(g[:14]):
        p = _GST_CHARS.index(ch) * (2 if i % 2 else 1)
        total += p // 36 + p % 36
    return _GST_CHARS[(36 - total % 36) % 36] == g[14]


def check_gst(c: Crust | None, bid: dict, rep: Report):
    g = (bid.get("gstin") or "").strip().upper()
    if not g:
        rep.add("gst", "gstin_present", CANT_VERIFY, None, "No GSTIN supplied - mandatory for GeM sellers")
        return
    if not GSTIN_RE.match(g) or not gstin_checksum_ok(g):
        rep.add("gst", "gstin_format", RED_FLAG, 0.0, f"{g} is not a valid GSTIN (format/checksum fails)",
                "GSTIN checksum algorithm", knockout=True)
        return
    rep.add("gst", "gstin_format", CONFIRMED, 1.0, f"{g} passes format + checksum", "GSTIN checksum algorithm")

    pan, state_code, entity = g[2:12], g[:2], PAN_ENTITY.get(g[5], "unknown")
    if bid.get("pan"):
        ok = bid["pan"].strip().upper() == pan
        rep.add("gst", "pan_match", CONFIRMED if ok else CONTRADICTED, 1.0 if ok else 0.0,
                f"PAN in GSTIN {pan} {'matches' if ok else 'does NOT match'} declared PAN {bid['pan']}",
                "GSTIN structure", knockout=not ok)
    if bid.get("state"):
        st = GST_STATES.get(state_code, "?")
        ok = st.lower() == bid["state"].strip().lower()
        rep.add("gst", "state_match", CONFIRMED if ok else CONTRADICTED, 1.0 if ok else 0.4,
                f"GSTIN registered in {st} (code {state_code}); bid says {bid['state']}", "GSTIN structure")
    declared = (bid.get("entity_type") or "").lower()
    if declared:
        expect = {"private limited": "C", "public limited": "C", "company": "C", "llp": "F",
                  "partnership": "F", "proprietorship": "P"}.get(declared)
        if expect:
            ok = g[5] == expect
            rep.add("gst", "entity_type", CONFIRMED if ok else CONTRADICTED, 1.0 if ok else 0.2,
                    f"PAN type '{entity}' vs declared '{declared}'", "PAN 4th character")

    # Status: declared/manual value first (from GST portal search), else public web lookup.
    status = (bid.get("gst_status") or "").lower()
    src, passage = "declared / services.gst.gov.in", ""
    if not status and c is not None:
        try:
            for r in web_search(c, f'"{g}" GST status', rep)[:6]:
                text = (str(r.get("title", "")) + " " + snippet_of(r))
                if g not in text.upper():
                    continue
                low = text.lower()
                m = re.search(r"\b(active|cancelled|canceled|suspended|inactive)\b", low)
                if m:
                    status, src, passage = m.group(1), r.get("url", "web"), snippet_of(r)
                    if bid["vendor_name"] and name_sim(bid["vendor_name"], text) < 60:
                        rep.add("gst", "gst_legal_name", CONTRADICTED, 0.2,
                                "Public GSTIN record does not mention the vendor's name", src, passage)
                    break
        except Exception as e:  # noqa: BLE001
            rep.add("gst", "gst_status", CANT_VERIFY, None, f"status lookup failed: {e}")
    if status in ("cancelled", "canceled", "suspended", "inactive"):
        rep.add("gst", "gst_status", RED_FLAG, 0.0, f"GSTIN status: {status.upper()}", src, passage, knockout=True)
    elif status == "active":
        rep.add("gst", "gst_status", CONFIRMED, 1.0, "GSTIN status: ACTIVE", src, passage)
    elif not any(f.check == "gst_status" for f in rep.findings):
        rep.add("gst", "gst_status", CANT_VERIFY, None,
                f"Status unknown - check https://services.gst.gov.in/services/searchtp for {g}")

    # Filing regularity (from GST portal 'Return filing' table: GSTR-3B filed in last 12 months).
    filed = _num(bid.get("gstr3b_filed_last_12"))
    if filed is not None:
        s = max(0.0, min(1.0, filed / 12))
        rep.add("gst", "filing_regularity", CONFIRMED if s >= 0.9 else CONTRADICTED, s,
                f"GSTR-3B filed {filed:.0f}/12 months", "declared / GST portal")
    reg = bid.get("gst_registration_date")
    if reg:
        try:
            yrs = (dt.date.today() - dt.date.fromisoformat(reg)).days / 365.25
            s = 1.0 if yrs >= 3 else 0.6 if yrs >= 1 else 0.2
            rep.add("gst", "gst_age", CONFIRMED if yrs >= 1 else CONTRADICTED, s,
                    f"GST registered {yrs:.1f} years ago ({reg})", "declared / GST portal", date=reg)
        except ValueError:
            pass


# --------------------------------------------------------------------------- 4/6. legal
# Severity of a hit (snippet must also name the subject to count).
LEGAL_TERMS = [
    (re.compile(r"\b(debarred|blacklist(ed)?|banned from (bidding|tender)|suspended from gem)\b", re.I), "debarment", 0.0, True),
    (re.compile(r"\b(fraud|cheating|section 420|forgery|money laundering|pmla|enforcement directorate|cbi (case|probe|raid)|fir (lodged|registered)|chargesheet|arrested)\b", re.I), "criminal/fraud", 0.0, True),
    (re.compile(r"\b(disqualified director|director disqualified|din (deactivated|disqualified))\b", re.I), "director disqualification", 0.1, True),
    (re.compile(r"\b(insolvency|nclt|cirp|ibc|liquidation|winding[- ]up)\b", re.I), "insolvency", 0.2, False),
    (re.compile(r"\b(cag|unfruitful expenditure|show cause|penalty imposed|gst evasion|tax evasion|fake invoice)\b", re.I), "regulatory", 0.4, False),
    (re.compile(r"\b(arbitration|recovery suit|civil suit|writ petition|petitioner|respondent|high court|district court|pending case)\b", re.I), "civil case", 0.6, False),
]
LEGAL_QUERY = ('(fraud OR cheating OR blacklisted OR debarred OR insolvency OR NCLT OR "winding up" '
               'OR arbitration OR "high court" OR FIR OR CBI OR "enforcement directorate")')


def legal_scan(c: Crust, subject: str, context: str, category: str, rep: Report, label: str):
    """Search court records (Indian Kanoon, eCourts mirrors) + news for cases naming `subject`."""
    hits, seen = [], set()
    queries = [
        (" ".join(x for x in (f'"{subject}"', context and f'"{context}"', "site:indiankanoon.org") if x), "court"),
        (" ".join(x for x in (f'"{subject}"', context and f'"{context}"', LEGAL_QUERY) if x), "news"),
    ]
    for q, kind in queries:
        try:
            results = web_search(c, q, rep)
        except BudgetExceeded:
            raise
        except Exception as e:  # noqa: BLE001
            rep.add(category, f"{label}_search", CANT_VERIFY, None, f"search failed: {e}")
            continue
        for r in results[:8]:
            url = r.get("url", "")
            if url in seen:
                continue
            seen.add(url)
            text = f"{r.get('title', '')} {snippet_of(r)}"
            # Must actually name the subject (guards against namesakes and generic pages).
            if name_sim(subject, text) < 85 or (context and kind == "news" and name_sim(context, text) < 60
                                                 and category == "people_legal"):
                continue
            for rx, sev, s, ko in LEGAL_TERMS:
                if rx.search(text):
                    hits.append((sev, s, ko, url, snippet_of(r), str(r.get("date") or r.get("published_date") or "")))
                    break
    if not hits:
        # Absence of hits is weaker evidence than a clean certificate: cap at 0.85.
        rep.add(category, f"{label}_cases", CONFIRMED, 0.85,
                f"No cases / adverse records found naming {subject} (web search can miss sealed or "
                f"unindexed matters - confirm on ecourts.gov.in)", "web: indiankanoon.org + news")
        return
    hits.sort(key=lambda h: h[1])
    for sev, s, ko, url, snip, date in hits[:3]:
        # Knockouts need a strong signal: a court record or 2+ independent hits.
        strong = "indiankanoon" in url or "ecourts" in url or len(hits) >= 2
        rep.add(category, f"{label}_{sev.replace(' ', '_').replace('/', '_')}", RED_FLAG, s,
                f"{sev.upper()} record naming {subject}", url, snip, date, knockout=ko and strong)


def check_firm_legal(c: Crust, bid: dict, rep: Report):
    legal_scan(c, bid["vendor_name"], "", "firm_legal", rep, "firm")
    n = _num(bid.get("declared_pending_cases"))
    if n is not None:
        rep.add("firm_legal", "declared_cases", CONFIRMED if n == 0 else CONTRADICTED,
                1.0 if n == 0 else max(0.2, 1 - 0.2 * n), f"Vendor declared {n:.0f} pending case(s)", "bid declaration")


# --------------------------------------------------------------------------- 5. people
def check_people(c: Crust, bid: dict, rep: Report, company: dict | None):
    vendor = dig(company or {}, "name", "company_name") or bid["vendor_name"]
    people = bid.get("key_personnel") or []
    if not people:
        rep.add("people", "key_personnel", CANT_VERIFY, None, "No key personnel listed")
        return
    for p in people[:5]:
        nm = p["name"]
        try:
            data = c.post("/person/search", {
                "filters": {"op": "and", "conditions": [
                    {"field": "basic_profile.name", "type": "(.)", "value": nm}]},
                "fields": ["basic_profile.name", "basic_profile.linkedin_url", "basic_profile.last_updated",
                           "experience.employment_details.current.company_name",
                           "experience.employment_details.current.title",
                           "experience.employment_details.past.company_name",
                           "experience.employment_details.past.end_date"],
                "limit": 10}, est_cost=0.3)
        except BudgetExceeded:
            raise
        except Exception as e:  # noqa: BLE001
            rep.add("people", f"person:{nm}", CANT_VERIFY, None, f"search failed: {e}")
            continue
        cands = [x for x in records(data, "profiles", "results", "data", "people")
                 if name_sim(nm, str(dig(x, "name") or "")) >= 85]
        current, past = None, None
        for x in cands:
            cur = dig(x, "current")
            pst = dig(x, "past")
            cur_names = [str(dig(j, "company_name") or "") for j in (cur if isinstance(cur, list) else [cur or {}])]
            pst_names = [str(dig(j, "company_name") or "") for j in (pst if isinstance(pst, list) else [pst or {}])]
            if any(name_sim(vendor, n) >= 85 for n in cur_names):
                current = x
                break
            if any(name_sim(vendor, n) >= 85 for n in pst_names):
                past = x
        src = "crustdata:/person/search"
        if current:
            title = str(dig(current, "title") or "")
            rep.add("people", f"person:{nm}", CONFIRMED, 1.0, f"{nm} currently at {vendor} as '{title}'", src,
                    passage=str(dig(current, "linkedin_url") or ""), date=str(dig(current, "last_updated") or ""))
        elif past:
            now = [str(dig(j, "company_name")) for j in (dig(past, "current") or []) if isinstance(j, dict)]
            rep.add("people", f"person:{nm}", CONTRADICTED, 0.0,
                    f"{nm} LEFT {vendor}; now at {', '.join(now) or 'unknown'} - name used without being on staff",
                    src, passage=str(dig(past, "linkedin_url") or ""))
        elif cands:
            rep.add("people", f"person:{nm}", CANT_VERIFY, 0.4,
                    f"{len(cands)} people named {nm} found, none linked to {vendor} (namesake risk)", src)
        else:
            rep.add("people", f"person:{nm}", CANT_VERIFY, None, f"No public profile for {nm}", src)

        legal_scan(c, nm, norm_name(vendor), "people_legal", rep, f"person:{nm}")


# --------------------------------------------------------------------------- scoring
def score(rep: Report) -> dict:
    cats = {}
    for cat, w in WEIGHTS.items():
        vals = [f.score for f in rep.findings if f.category == cat and f.score is not None]
        # Red flags dominate: category score is min(mean, worst red flag)
        reds = [f.score for f in rep.findings if f.category == cat and f.status == RED_FLAG and f.score is not None]
        s = None
        if vals:
            s = sum(vals) / len(vals)
            if reds:
                s = min(s, min(reds))
        cats[cat] = {"weight": w, "score": None if s is None else round(s, 3)}
    total_w = sum(WEIGHTS.values())
    raw = sum(v["weight"] * (UNKNOWN_PRIOR if v["score"] is None else v["score"]) for v in cats.values())
    coverage = sum(v["weight"] for v in cats.values() if v["score"] is not None) / total_w
    final = round(100 * raw / total_w, 1)
    knockouts = [f for f in rep.findings if f.knockout]
    if knockouts or final < HOLD_AT:
        verdict = "REJECT"
    elif final >= APPROVE_AT and coverage >= MIN_COVERAGE:
        verdict = "APPROVE"
    else:
        verdict = "HOLD"
    return {"score": final, "coverage": round(coverage, 2), "verdict": verdict, "categories": cats,
            "knockouts": [f"{f.check}: {f.detail}" for f in knockouts]}


# --------------------------------------------------------------------------- intake
ASK = [
    ("gstin", "GSTIN (15 chars, from the seller's GeM profile)"),
    ("pan", "PAN of the firm"),
    ("state", "State of registration (e.g. Karnataka)"),
    ("entity_type", "Entity type (private limited / llp / partnership / proprietorship)"),
    ("gst_status", "GST status from services.gst.gov.in (active/cancelled/suspended) - blank to auto-search"),
    ("gstr3b_filed_last_12", "GSTR-3B returns filed in last 12 months (0-12) - blank if unknown"),
    ("gst_registration_date", "GST registration date YYYY-MM-DD - blank if unknown"),
    ("claimed_turnover_inr", "Claimed annual turnover in INR - blank if unknown"),
    ("declared_pending_cases", "Pending cases the vendor declared in the bid - blank if unknown"),
]


def intake(bid: dict, interactive: bool) -> dict:
    for req in ("vendor_name", "domain"):
        if not bid.get(req):
            if not interactive:
                sys.exit(f"bid is missing required field '{req}'")
            bid[req] = input(f"{req}: ").strip()
    if interactive:
        missing = [(k, q) for k, q in ASK if bid.get(k) in (None, "")]
        if missing:
            print("\nSome details would improve the check (press Enter to skip):")
        for k, q in missing:
            v = input(f"  {q}: ").strip()
            if v:
                bid[k] = v
        if not bid.get("key_personnel"):
            print("  Key personnel (name, title) - blank line to finish:")
            people = []
            while True:
                line = input("    > ").strip()
                if not line:
                    break
                n, _, t = line.partition(",")
                people.append({"name": n.strip(), "title": t.strip()})
            bid["key_personnel"] = people
    return bid


# --------------------------------------------------------------------------- main
def run(bid: dict, client: Crust) -> tuple[Report, dict]:
    rep = Report(bid["vendor_name"])
    company = None
    try:
        company = check_identity(client, bid, rep)
        check_capacity(client, bid, rep, company)
        check_gst(client, bid, rep)
        check_firm_legal(client, bid, rep)
        check_people(client, bid, rep, company)
    except BudgetExceeded as e:
        rep.add("identity", "budget", CANT_VERIFY, None, f"Stopped early: budget hit ({e}); remaining checks skipped")
    rep.credits_spent = getattr(client, "spent", 0.0)
    return rep, score(rep)


ICON = {CONFIRMED: "OK ", CONTRADICTED: "XX ", CANT_VERIFY: "?? ", RED_FLAG: "!! "}


def print_report(rep: Report, s: dict):
    print(f"\n=== GeM Bid Check: {rep.vendor} ===")
    for cat in WEIGHTS:
        cs = s["categories"][cat]
        sc = "n/a" if cs["score"] is None else f"{cs['score']*100:.0f}"
        print(f"\n[{cat}]  weight {cs['weight']}  sub-score {sc}")
        for f in [f for f in rep.findings if f.category == cat]:
            print(f"  {ICON[f.status]}{f.check}: {f.detail}" + ("  [KNOCKOUT]" if f.knockout else ""))
            if f.source:
                print(f"       source: {f.source}" + (f"  ({f.date})" if f.date else ""))
            if f.passage:
                print(f"       \"{f.passage[:160]}\"")
    print(f"\nScore {s['score']}/100   coverage {s['coverage']:.0%}   credits {rep.credits_spent:.1f}")
    for k in s["knockouts"]:
        print(f"  KNOCKOUT -> {k}")
    print(f"VERDICT: {s['verdict']}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("bid")
    ap.add_argument("--no-prompt", action="store_true", help="don't ask for missing details")
    ap.add_argument("--yes", action="store_true", help="skip human confirmation of REJECT (demo)")
    ap.add_argument("--out", default="report.json")
    a = ap.parse_args(argv)
    bid = intake(json.load(open(a.bid)), interactive=not a.no_prompt and sys.stdin.isatty())
    key = os.environ.get("CRUSTDATA_API_KEY") or sys.exit("set CRUSTDATA_API_KEY")
    rep, s = run(bid, Crust(key))
    print_report(rep, s)
    if s["verdict"] == "REJECT" and not a.yes:
        ok = input("\nREJECT requires human sign-off. Confirm reject? [y/N] ").strip().lower() == "y"
        s["human_confirmed"] = ok
        if not ok:
            s["verdict"] = "HOLD"
            print("Downgraded to HOLD pending manual review.")
    json.dump({"bid": bid, "result": s, "findings": [asdict(f) for f in rep.findings],
               "credits_spent": rep.credits_spent, "generated": dt.datetime.now().isoformat()},
              open(a.out, "w"), indent=2)
    print(f"\nWrote {a.out}")


if __name__ == "__main__":
    main()
