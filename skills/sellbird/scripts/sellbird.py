#!/usr/bin/env python3
"""Portable, offline ledger for an agent-operated daily prospecting workflow.

Only the host agent accesses search, Sheets and email. This program validates
evidence, reserves daily quotas, and produces repeatable external-write plans.
Python 3.11+; standard library only. No credentials belong in the ledger.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlsplit

IST = timezone(timedelta(hours=5, minutes=30))
REGIONS = ("Gurgaon", "Mumbai", "Delhi NCR")
HEADERS = [
    "Lead ID", "Added (IST)", "Region", "Business", "Locality", "Phone",
    "All public phones", "Website status", "Website", "Score", "Why a fit",
    "Suggested pitch", "WhatsApp draft", "Email", "Email verification",
    "Email subject", "Email draft", "Source URLs", "Checked at", "Email status",
    "Call status", "Notes",
]
SIGNALS = {"website_gap": 25, "lead_capture_gap": 25, "followup_gap": 25,
           "active_business": 15, "decision_maker": 10}


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def phone(value):
    """Indian public business numbers, including Delhi/Mumbai landlines."""
    value = re.sub(r"\D", "", str(value))
    if value.startswith("0091"):
        value = value[4:]
    elif len(value) == 12 and value.startswith("91"):
        value = value[2:]
    elif len(value) == 11 and value.startswith("0"):
        value = value[1:]
    if len(value) != 10 or len(set(value)) == 1:
        raise ValueError("expected an Indian business phone with area code")
    if value[0] not in "6789" and not value.startswith(("11", "22", "120", "124", "129")):
        raise ValueError("unsupported or incomplete Indian phone")
    return "+91" + value


def url(value):
    parsed = urlsplit(str(value))
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username:
        raise ValueError("evidence must have an HTTP(S) source URL")
    return str(value)


def domain(value):
    if not value:
        return ""
    return (urlsplit(url(value)).hostname or "").lower().removeprefix("www.")


def validate(raw, today, max_age_days=7):
    row = dict(raw)
    for field in ("business", "locality", "pitch", "whatsapp_draft", "phone_source"):
        if not isinstance(row.get(field), str) or not row[field].strip():
            raise ValueError(f"missing {field}")
    if row.get("region") not in REGIONS:
        raise ValueError("unknown region")
    if row.get("business_type") != "property_dealer" or row.get("public_business_phone") is not True:
        raise ValueError("requires a property dealer and a publicly listed business phone")
    # Gurgaon is part of NCR; keep these campaign buckets mutually exclusive.
    in_gurgaon = bool(re.search(r"\b(gurgaon|gurugram)\b", row["locality"], re.I))
    if in_gurgaon and row["region"] != "Gurgaon":
        raise ValueError("Gurgaon must not also count toward Delhi NCR")
    if row["region"] == "Gurgaon" and not in_gurgaon:
        raise ValueError("Gurgaon locality evidence is missing")
    if row["region"] == "Mumbai" and not re.search(r"\b(mumbai|bombay)\b", row["locality"], re.I):
        raise ValueError("Mumbai locality evidence is missing")
    if row["region"] == "Delhi NCR" and not re.search(r"\b(delhi|noida|ghaziabad|faridabad)\b", row["locality"], re.I):
        raise ValueError("Delhi NCR locality evidence is missing")
    sources = row.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError("missing sources")
    row["sources"] = list(dict.fromkeys(url(s) for s in sources))
    if url(row["phone_source"]) not in sources:
        raise ValueError("phone source must be in sources")
    checked = datetime.fromisoformat(row.get("checked_at", ""))
    if checked.tzinfo is None or not 0 <= (today - checked.astimezone(IST).date()).days <= max_age_days:
        raise ValueError("evidence must be recently checked and timezone-aware")
    if not isinstance(row.get("phones"), list) or not row["phones"]:
        raise ValueError("missing phones")
    row["phones"] = list(dict.fromkeys(phone(p) for p in row["phones"]))
    status = row.get("website_status")
    if status not in ("present", "not_found", "unknown", "unreachable"):
        raise ValueError("invalid website status")
    if status == "present" and not row.get("website"):
        raise ValueError("present website needs a URL")
    if row.get("website"):
        url(row["website"])
    if status == "not_found" and (row.get("website") or len(row.get("website_checks", [])) < 2):
        raise ValueError("not_found requires a listing check and a brand search, with no website")
    for check in row.get("website_checks", []):
        url(check)
    signals = row.get("signals", [])
    codes = set()
    for signal in signals:
        if signal.get("code") not in SIGNALS or not str(signal.get("evidence", "")).strip():
            raise ValueError("unknown signal or missing evidence")
        if url(signal.get("url", "")) not in sources:
            raise ValueError("signal evidence must be in sources")
        codes.add(signal["code"])
    if "website_gap" in codes and status != "not_found":
        raise ValueError("website_gap requires not_found, not an unchecked/failed site")
    if not codes.intersection({"website_gap", "lead_capture_gap", "followup_gap"}):
        raise ValueError("needs a documented service opportunity")
    email = row.get("email", "").strip().lower()
    if email and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise ValueError("invalid email")
    row["email"] = email
    if row.get("email_status", "unknown") not in ("verified", "risky", "guess", "invalid", "unknown"):
        raise ValueError("invalid email verification status")
    if row.get("email_status") == "verified":
        if not email or not row.get("email_verifier") or not row.get("email_checked_at"):
            raise ValueError("verified email needs a verifier and timestamp")
        email_checked = datetime.fromisoformat(row["email_checked_at"])
        if email_checked.tzinfo is None or not 0 <= (today - email_checked.astimezone(IST).date()).days <= max_age_days:
            raise ValueError("email verification is stale")
    # Score components are computed, never accepted from the model.
    opportunity = 25
    row["score"] = 45 + opportunity + (15 if "active_business" in codes else 0) + (10 if "decision_maker" in codes else 0) + (5 if row.get("email_status") == "verified" else 0)
    row["why_fit"] = "; ".join(s["evidence"] for s in signals)
    row["lead_id"] = hashlib.sha256(row["phones"][0].encode()).hexdigest()[:20]
    return row


def identities(row):
    keys = {"phone:" + p for p in row["phones"]}
    if row.get("website"):
        keys.add("domain:" + domain(row["website"]))
    if row.get("place_id"):
        keys.add("place:" + row["place_id"])
    name = re.sub(r"\W+", "", row["business"].casefold())
    locality = re.sub(r"\W+", "", row["locality"].casefold().replace("gurugram", "gurgaon"))
    keys.add(f"name:{name}:{locality}")
    return keys


def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    db.executescript("""
      CREATE TABLE IF NOT EXISTS leads (
        id TEXT PRIMARY KEY, day TEXT NOT NULL, region TEXT NOT NULL,
        payload TEXT NOT NULL, sheet_row INTEGER UNIQUE NOT NULL,
        sheet_status TEXT NOT NULL DEFAULT 'pending',
        email_state TEXT NOT NULL DEFAULT 'draft', receipt TEXT NOT NULL DEFAULT '');
      CREATE TABLE IF NOT EXISTS identities (identity TEXT PRIMARY KEY, lead_id TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS suppressions (identity TEXT PRIMARY KEY, reason TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """)
    return db


def prepare(db, raw_rows, today, quota=10, min_score=80, next_row=1):
    """Reserve fixed sheet rows atomically. Replays cannot refill today's quota."""
    if not 1 <= quota <= 100 or not 0 <= min_score <= 100 or next_row < 1:
        raise ValueError("invalid quota, score or next sheet row")
    valid, rejected = [], []
    for index, raw in enumerate(raw_rows):
        try:
            lead = validate(raw, today)
            if lead["score"] < min_score:
                raise ValueError("below minimum score")
            valid.append(lead)
        except (ValueError, TypeError, KeyError) as exc:
            rejected.append({"index": index, "reason": str(exc)})
    added = []
    with db:
        db.execute("BEGIN IMMEDIATE")
        latest = db.execute("SELECT MAX(sheet_row) FROM leads").fetchone()[0]
        next_row = max(next_row, (latest + 1) if latest is not None else 1)
        counts = {r: db.execute("SELECT COUNT(*) FROM leads WHERE day=? AND region=?", (str(today), r)).fetchone()[0] for r in REGIONS}
        for row in sorted(valid, key=lambda r: (-r["score"], r["lead_id"])):
            keys = identities(row)
            if counts[row["region"]] >= quota:
                continue
            if any(db.execute("SELECT 1 FROM identities WHERE identity=? UNION SELECT 1 FROM suppressions WHERE identity=?", (k, k)).fetchone() for k in keys):
                continue
            if row.get("email") and db.execute("SELECT 1 FROM suppressions WHERE identity=?", ("email:" + row["email"],)).fetchone():
                continue
            db.execute("INSERT INTO leads(id,day,region,payload,sheet_row) VALUES (?,?,?,?,?)", (row["lead_id"], str(today), row["region"], json.dumps(row), next_row))
            db.executemany("INSERT INTO identities VALUES (?,?)", [(k, row["lead_id"]) for k in keys])
            counts[row["region"]] += 1
            next_row += 1
            added.append(row["lead_id"])
    return {"added": added, "counts": counts, "shortfall": {r: max(0, quota - counts[r]) for r in REGIONS}, "rejected": rejected}


def values(record):
    row = json.loads(record["payload"])
    return [row["lead_id"], record["day"], row["region"], row["business"], row["locality"], row["phones"][0],
            ", ".join(row["phones"]), row["website_status"], row.get("website", ""), row["score"],
            row["why_fit"], row["pitch"], row["whatsapp_draft"], row.get("email", ""), row.get("email_status", "unknown"),
            row.get("email_subject", ""), row.get("email_draft", ""), "\n".join(row["sources"]), row["checked_at"],
            record["email_state"], "Not called", ""]


def sheet_requests(db, sheet_id, spreadsheet_id):
    """Fixed cells, stringValue for untrusted strings: no formula injection."""
    if sheet_id < 0 or not spreadsheet_id:
        raise ValueError("a real spreadsheet ID and nonnegative sheet ID are required")
    binding = json.dumps([spreadsheet_id, sheet_id])
    with db:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute("SELECT value FROM settings WHERE key='sheet_binding'").fetchone()
        if existing and existing[0] != binding:
            raise ValueError("ledger is already bound to a different sheet")
        db.execute("INSERT OR IGNORE INTO settings VALUES ('sheet_binding', ?)", (binding,))
    requests = []
    for record in db.execute("SELECT * FROM leads WHERE sheet_status='pending' ORDER BY sheet_row"):
        cells = [{"userEnteredValue": {"numberValue" if isinstance(v, int) else "stringValue": v}} for v in values(record)]
        requests.append({"updateCells": {"start": {"sheetId": sheet_id, "rowIndex": record["sheet_row"], "columnIndex": 0}, "rows": [{"values": cells}], "fields": "userEnteredValue"}})
    return {"spreadsheet_id": spreadsheet_id, "requests": requests}


def confirm_sheet(db, actual_rows):
    """Require a read-back of IDs at their planned zero-based row indexes."""
    confirmed = 0
    with db:
        for row in db.execute("SELECT id,sheet_row FROM leads WHERE sheet_status='pending'").fetchall():
            if actual_rows.get(str(row["sheet_row"])) == row["id"]:
                db.execute("UPDATE leads SET sheet_status='synced' WHERE id=?", (row["id"],))
                confirmed += 1
    return {"confirmed": confirmed}


def claim_email(db, lead_id, sender, verified_sender, today, limit=30):
    """Persist 'sending' before network I/O; an ambiguous send is never retried."""
    if not sender or sender != verified_sender:
        raise ValueError("requested sender has not been verified on the connected account")
    with db:
        db.execute("BEGIN IMMEDIATE")
        record = db.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
        if not record or record["sheet_status"] != "synced" or record["email_state"] != "draft":
            raise ValueError("lead must be synced and must not have a prior send attempt")
        if record["day"] != str(today):
            raise ValueError("only today's published leads may be emailed automatically")
        row = validate(json.loads(record["payload"]), today)
        if row.get("email_status") != "verified" or not row.get("email_subject") or not row.get("email_draft"):
            raise ValueError("email needs fresh verification, subject and draft")
        keys = identities(row) | {"email:" + row["email"], "domain:" + row["email"].split("@")[-1]}
        if any(db.execute("SELECT 1 FROM suppressions WHERE identity=?", (k,)).fetchone() for k in keys):
            raise ValueError("lead is suppressed")
        count = db.execute("SELECT COUNT(*) FROM leads WHERE day=? AND email_state IN ('sending','sent','uncertain')", (str(today),)).fetchone()[0]
        if count >= limit:
            raise ValueError("daily email cap reached")
        if db.execute("SELECT 1 FROM leads WHERE json_extract(payload, '$.email')=? AND email_state IN ('sending','sent','uncertain')", (row["email"],)).fetchone():
            raise ValueError("email already attempted")
        db.execute("UPDATE leads SET email_state='sending' WHERE id=?", (lead_id,))
    return {"from": sender, "to": row["email"], "subject": row["email_subject"], "body": row["email_draft"]}


def record_email(db, lead_id, receipt):
    with db:
        result = db.execute("UPDATE leads SET email_state=?,receipt=? WHERE id=? AND email_state='sending'", ("sent" if receipt else "uncertain", receipt, lead_id))
        if not result.rowcount:
            raise ValueError("no outstanding send claim")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="Private persistent SQLite ledger")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--input", required=True)
    p.add_argument("--quota", type=int, default=10)
    p.add_argument("--min-score", type=int, default=80)
    p.add_argument("--next-row", type=int, default=1, help="Zero-based row after the sheet's existing data")
    p = sub.add_parser("sheet-request")
    p.add_argument("--sheet-id", required=True, type=int)
    p.add_argument("--spreadsheet-id", required=True)
    p = sub.add_parser("confirm-sheet")
    p.add_argument("--rows", required=True, help="JSON map of actual zero-based row indexes to lead IDs")
    p = sub.add_parser("claim-email")
    p.add_argument("--lead-id", required=True)
    p.add_argument("--sender", required=True)
    p.add_argument("--verified-sender", required=True)
    p = sub.add_parser("record-email")
    p.add_argument("--lead-id", required=True)
    p.add_argument("--receipt", default="", help="Provider message ID; omit for an uncertain result")
    p = sub.add_parser("suppress")
    p.add_argument("--identity", required=True, help="phone:+91..., domain:..., or email:...")
    p.add_argument("--reason", required=True)
    sub.add_parser("status")
    sub.add_parser("headers")
    args = parser.parse_args()
    today = datetime.now(IST).date()
    db = connect(args.db)
    try:
        if args.command == "prepare":
            result = prepare(db, read_json(args.input), today, args.quota, args.min_score, args.next_row)
        elif args.command == "sheet-request":
            result = sheet_requests(db, args.sheet_id, args.spreadsheet_id)
        elif args.command == "confirm-sheet":
            result = confirm_sheet(db, read_json(args.rows))
        elif args.command == "claim-email":
            result = claim_email(db, args.lead_id, args.sender, args.verified_sender, today)
        elif args.command == "record-email":
            record_email(db, args.lead_id, args.receipt)
            result = {"recorded": True}
        elif args.command == "suppress":
            if not args.identity.startswith(("phone:", "domain:", "email:")):
                raise ValueError("unsupported suppression identity")
            with db:
                db.execute("INSERT OR REPLACE INTO suppressions VALUES (?,?)", (args.identity.lower(), args.reason))
            result = {"suppressed": True}
        elif args.command == "headers":
            result = HEADERS
        else:
            result = {"today_ist": str(today), "counts": [dict(r) for r in db.execute("SELECT day,region,sheet_status,email_state,COUNT(*) AS count FROM leads GROUP BY day,region,sheet_status,email_state")], "pending": [dict(r) for r in db.execute("SELECT id,sheet_row,email_state FROM leads WHERE sheet_status='pending' OR email_state IN ('sending','uncertain')")]}
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, TypeError, sqlite3.Error) as exc:
        parser.exit(2, f"Sellbird: {exc}\n")
    finally:
        db.close()


if __name__ == "__main__":
    main()
