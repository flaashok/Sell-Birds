"""Fictional, offline scenarios for the portable Sellbird ledger."""
import importlib.util
from datetime import date, timedelta
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("sellbird_skill", Path(__file__).parents[1] / "skills/sellbird/scripts/sellbird.py")
s = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s)
TODAY = date(2026, 11, 21)


def candidate(n=1, region="Gurgaon"):
    source = f"https://dealer{n}.example.test/contact"
    return dict(business=f"Example Dealer {n}", business_type="property_dealer",
                region=region, locality={"Gurgaon": "Gurgaon", "Mumbai": "Mumbai", "Delhi NCR": "Noida"}[region],
                phones=[f"99999{n:05d}"], public_business_phone=True,
                phone_source=source, sources=[source], checked_at="2026-11-21T10:00:00+05:30",
                website_status="present", website=f"https://dealer{n}.example.test",
                signals=[dict(code="lead_capture_gap", evidence="Viewing enquiries are phone-only on the checked contact page", url=source),
                         dict(code="active_business", evidence="Dated current listings on this page", url=source)],
                pitch="An enquiry page for viewings", whatsapp_draft="Would a viewing enquiry page help?",
                email=f"office@dealer{n}.example.test", email_status="verified",
                email_verifier="fictional-test-provider", email_checked_at="2026-11-21T09:00:00+05:30",
                email_subject="Viewing enquiries", email_draft="Hi team, would an enquiry page help? Reply no to decline.")


@pytest.fixture
def db(tmp_path):
    conn = s.connect(tmp_path / "ledger.db")
    yield conn
    conn.close()


def test_daily_quotas_idempotency_and_next_day_dedup(db):
    rows = [candidate(n + offset, region) for region, offset in zip(s.REGIONS, (0, 100, 200)) for n in range(1, 16)]
    first = s.prepare(db, rows, TODAY)
    assert first["counts"] == {r: 10 for r in s.REGIONS}
    assert len(first["added"]) == 30
    assert not s.prepare(db, rows, TODAY)["added"]
    tomorrow = s.prepare(db, rows, TODAY + timedelta(days=1))
    assert tomorrow["counts"] == {r: 5 for r in s.REGIONS}
    assert tomorrow["shortfall"] == {r: 5 for r in s.REGIONS}


def test_shared_secondary_phone_and_domain_deduplicate(db):
    a, b, c = candidate(1), candidate(2), candidate(3)
    b["phones"].append(a["phones"][0])
    c["website"] = a["website"]
    s.prepare(db, [a], TODAY)
    assert not s.prepare(db, [b, c], TODAY)["added"]


@pytest.mark.parametrize("raw,expected", [("09999900001", "+919999900001"), ("0091 99999 00001", "+919999900001"), ("011 4000 0001", "+911140000001"), ("022-4000-0001", "+912240000001")])
def test_phones(raw, expected):
    assert s.phone(raw) == expected


@pytest.mark.parametrize("raw", ["123", "9999999999", "5555551234", "+44 7911 123456"])
def test_invalid_phones(raw):
    with pytest.raises(ValueError):
        s.phone(raw)


@pytest.mark.parametrize("patch", [dict(region="Delhi NCR", locality="Gurugram"), dict(region="Mumbai", locality="Pune"), dict(public_business_phone=False), dict(checked_at="2026-10-01T10:00:00+05:30"), dict(checked_at="2026-11-22T10:00:00+05:30"), dict(checked_at="2026-11-21T10:00:00"), dict(website_status="not_found", website="", website_checks=[]), dict(email_verifier=""), dict(email_checked_at="2026-10-01T10:00:00+05:30"), dict(sources=[]), dict(business_type="directory")])
def test_bad_evidence_is_rejected_without_consuming_quota(db, patch):
    row = candidate()
    row.update(patch)
    report = s.prepare(db, [row], TODAY)
    assert report["rejected"] and not report["added"]
    assert report["counts"]["Gurgaon"] == 0


def test_unknown_website_does_not_support_website_gap(db):
    row = candidate()
    row["website_status"] = "unknown"
    row["signals"][0]["code"] = "website_gap"
    assert s.prepare(db, [row], TODAY)["rejected"]


def test_score_is_computed_not_trusted(db):
    row = candidate()
    row.update(score=100, email_status="unknown")
    row["signals"] = row["signals"][:1]
    assert s.prepare(db, [row], TODAY)["rejected"]


def test_retry_writes_same_cells_and_never_executes_formulas(db):
    row = candidate()
    row["business"] = '=IMPORTXML("https://example.test", "//body")'
    result = s.prepare(db, [row], TODAY, next_row=8)
    req = s.sheet_requests(db, 17, "fictional-sheet-id")
    assert req == s.sheet_requests(db, 17, "fictional-sheet-id")
    update = req["requests"][0]["updateCells"]
    assert update["start"]["rowIndex"] == 8
    assert update["rows"][0]["values"][3]["userEnteredValue"] == {"stringValue": row["business"]}
    assert s.confirm_sheet(db, {"7": result["added"][0]})["confirmed"] == 0
    assert s.confirm_sheet(db, {"8": result["added"][0]})["confirmed"] == 1
    assert s.sheet_requests(db, 17, "fictional-sheet-id")["requests"] == []
    with pytest.raises(ValueError, match="different sheet"):
        s.sheet_requests(db, 18, "fictional-sheet-id")


def published(db, n=1):
    ident = s.prepare(db, [candidate(n)], TODAY)["added"][0]
    record = db.execute("SELECT sheet_row FROM leads WHERE id=?", (ident,)).fetchone()
    s.confirm_sheet(db, {str(record[0]): ident})
    return ident


def test_claim_before_send_and_ambiguous_results_never_resend(db):
    ident = published(db)
    with pytest.raises(ValueError, match="sender"):
        s.claim_email(db, ident, "sender@example.test", "other@example.test", TODAY)
    result = s.claim_email(db, ident, "sender@example.test", "sender@example.test", TODAY)
    assert result["to"] == candidate()["email"]
    with pytest.raises(ValueError, match="prior send"):
        s.claim_email(db, ident, "sender@example.test", "sender@example.test", TODAY)
    s.record_email(db, ident, "")
    with pytest.raises(ValueError, match="prior send"):
        s.claim_email(db, ident, "sender@example.test", "sender@example.test", TODAY)


def test_unsynced_old_and_unverified_leads_cannot_send(db):
    ident = s.prepare(db, [candidate()], TODAY)["added"][0]
    with pytest.raises(ValueError, match="synced"):
        s.claim_email(db, ident, "sender@example.test", "sender@example.test", TODAY)
    s.confirm_sheet(db, {"1": ident})
    with pytest.raises(ValueError, match="today"):
        s.claim_email(db, ident, "sender@example.test", "sender@example.test", TODAY + timedelta(days=1))
    row = candidate(2)
    row["email_status"] = "guess"
    other = s.prepare(db, [row], TODAY)["added"][0]
    s.confirm_sheet(db, {"2": other})
    with pytest.raises(ValueError, match="verification"):
        s.claim_email(db, other, "sender@example.test", "sender@example.test", TODAY)


def test_suppression_before_discovery_and_send(db):
    with db:
        db.execute("INSERT INTO suppressions VALUES (?,?)", ("phone:" + s.phone(candidate()["phones"][0]), "declined"))
    assert not s.prepare(db, [candidate()], TODAY)["added"]
    ident = published(db, 2)
    with db:
        db.execute("INSERT INTO suppressions VALUES (?,?)", ("email:" + candidate(2)["email"], "declined"))
    with pytest.raises(ValueError, match="suppressed"):
        s.claim_email(db, ident, "sender@example.test", "sender@example.test", TODAY)


def test_receipt_and_send_limit(db):
    a, b = published(db, 1), published(db, 2)
    s.claim_email(db, a, "sender@example.test", "sender@example.test", TODAY, limit=1)
    s.record_email(db, a, "fake-message-id")
    assert db.execute("SELECT email_state,receipt FROM leads WHERE id=?", (a,)).fetchone()[:] == ("sent", "fake-message-id")
    with pytest.raises(ValueError, match="cap"):
        s.claim_email(db, b, "sender@example.test", "sender@example.test", TODAY, limit=1)
