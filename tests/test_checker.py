"""Offline tests: a fake Crustdata client returns canned responses, so the scoring logic,
GST checks and knockout rules are exercised without network or credits.
The canned shapes are guesses at the live API; the parser (`dig`/`records`) is shape-tolerant."""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import gem_checker as g  # noqa: E402

BIDS = pathlib.Path(__file__).resolve().parents[1] / "bids"
REAL = {"company_id": 1, "basic_info": {"name": "Nimbus Cloudworks Private Limited",
                                        "primary_domain": "nimbuscloudworks.in", "year_founded": 2016},
        "headcount": {"total": 210}}


def person(name, cur, past=None):
    return {"basic_profile": {"name": name, "linkedin_url": f"https://linkedin.com/in/{name.split()[0].lower()}"},
            "experience": {"employment_details": {"current": [{"company_name": cur, "title": "Lead"}],
                                                  "past": [{"company_name": p} for p in (past or [])]}}}


class Fake:
    def __init__(self, web=None, gst_status_text=None):
        self.spent, self.web, self.gst = 0.0, web or {}, gst_status_text

    def post(self, path, body, est_cost=1.0):
        self.spent += est_cost
        if path == "/company/identify":
            if "domains" in body:
                return {"results": [REAL]} if body["domains"] == ["nimbuscloudworks.in"] else {"results": []}
            return {"results": [REAL]}
        if path == "/company/enrich":
            return [REAL] if body["domains"] == ["nimbuscloudworks.in"] else []
        if path == "/job/search":
            return {"jobs": [{"title": "Dev"}], "total_count": 14}
        if path == "/person/search":
            nm = body["filters"]["conditions"][0]["value"]
            db = {"Asha Raman": [person("Asha Raman", "Nimbus Cloudworks Pvt Ltd")],
                  "Vikram Sethi": [person("Vikram Sethi", "Nimbus Cloudworks")],
                  "Rohit Khanna": [person("Rohit Khanna", "Infosys", ["Nimbus Cloudworks"])]}
            return {"profiles": db.get(nm, [])}
        if path == "/web/search/live":
            q = body["query"]
            if "GST status" in q and self.gst:
                gstin = q.split('"')[1]
                return {"results": [{"url": "https://example-gst-lookup.in", "title": gstin,
                                     "snippet": f"{gstin} Nimbus Cloudworks Private Limited status {self.gst}"}]}
            for k, v in self.web.items():
                if k in q:
                    return {"results": v}
            return {"results": []}
        raise AssertionError(path)


def load(n):
    return json.loads((BIDS / n).read_text())


def test_gstin_checksum():
    assert g.gstin_checksum_ok("27AAPFU0939F1ZV")
    assert not g.gstin_checksum_ok("27AAPFU0939F1ZA")


def test_honest_bid_approved():
    rep, s = g.run(load("bid_honest.json"), Fake(gst_status_text="Active"))
    assert s["verdict"] == "APPROVE", (s, [f.detail for f in rep.findings])
    assert s["score"] >= 75 and not s["knockouts"]


def test_lookalike_bid_rejected_with_reasons():
    rep, s = g.run(load("bid_lookalike.json"), Fake())
    assert s["verdict"] == "REJECT"
    checks = {f.check: f for f in rep.findings}
    assert checks["lookalike_domain"].knockout
    assert checks["pan_match"].status == g.CONTRADICTED      # GSTIN PAN != declared PAN
    assert checks["state_match"].status == g.CONTRADICTED    # 07 = Delhi, bid says Karnataka
    assert checks["entity_type"].status == g.CONTRADICTED    # PAN type F vs 'private limited'
    assert checks["person:Rohit Khanna"].status == g.CONTRADICTED  # left the firm


def test_cancelled_gst_is_knockout():
    bid = load("bid_honest.json")
    rep, s = g.run(bid, Fake(gst_status_text="Cancelled"))
    assert s["verdict"] == "REJECT" and any("gst_status" in k for k in s["knockouts"])


def test_invalid_gstin_is_knockout():
    bid = load("bid_honest.json") | {"gstin": "29AABCN1234F1ZA"}
    _, s = g.run(bid, Fake(gst_status_text="Active"))
    assert any("gstin_format" in k for k in s["knockouts"])


def test_court_record_against_key_person_is_knockout():
    web = {'"Vikram Sethi"': [{"url": "https://indiankanoon.org/doc/1/",
                               "title": "State vs Vikram Sethi",
                               "snippet": "Vikram Sethi, director of Nimbus Cloudworks, accused of cheating under section 420"}]}
    rep, s = g.run(load("bid_honest.json"), Fake(web=web, gst_status_text="Active"))
    assert s["verdict"] == "REJECT"
    assert any(f.category == "people_legal" and f.knockout for f in rep.findings)


def test_namesake_hit_ignored():
    # Snippet names a different person -> must not count against our key person.
    web = {'"Asha Raman"': [{"url": "https://news.example/x", "title": "Fraud case",
                             "snippet": "Asha Ramachandran-Iyer arrested for fraud in Pune"}]}
    rep, s = g.run(load("bid_honest.json"), Fake(web=web, gst_status_text="Active"))
    assert s["verdict"] == "APPROVE", [f.detail for f in rep.findings if f.status == g.RED_FLAG]


def test_insolvency_lowers_score_without_knockout():
    web = {'"Nimbus Cloudworks Private Limited" site:': [{
        "url": "https://indiankanoon.org/doc/2/", "title": "Nimbus Cloudworks Private Limited - NCLT",
        "snippet": "Application under section 9 IBC against Nimbus Cloudworks Private Limited before NCLT Bengaluru"}]}
    rep, s = g.run(load("bid_honest.json"), Fake(web=web, gst_status_text="Active"))
    assert not s["knockouts"]
    assert s["categories"]["firm_legal"]["score"] <= 0.2
    assert s["verdict"] in ("HOLD", "APPROVE") and s["score"] < 100


def test_missing_data_lowers_coverage_to_hold():
    bid = {"vendor_name": "Unknown Vendor", "domain": "unknown-vendor.in", "key_personnel": []}
    _, s = g.run(bid, Fake())
    assert s["coverage"] < g.MIN_COVERAGE and s["verdict"] in ("HOLD", "REJECT")


def test_budget_stop_is_reported():
    class Broke(Fake):
        def post(self, path, body, est_cost=1.0):
            if path == "/company/enrich":
                raise g.BudgetExceeded("spent 40")
            return super().post(path, body, est_cost)
    rep, s = g.run(load("bid_honest.json"), Broke())
    assert any(f.check == "budget" for f in rep.findings)
