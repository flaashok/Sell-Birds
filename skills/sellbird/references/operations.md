# Installation and daily operations

## Portable installation

Copy the complete `skills/sellbird` directory into the host's skill directory:
`~/.codex/skills/sellbird`, `~/.claude/skills/sellbird`, or
`~/.hermes/skills/business/sellbird`. For another harness, use its documented
skill loader, or load `SKILL.md` explicitly. Restart/reload skill discovery.
The entrypoint and relative references must stay together. The helper works
offline with Python 3.11+; the host supplies research, Sheets and email tools.

Create a private campaign directory, for example `data/sellbird` in a Sellbird
checkout (already gitignored). Put `campaign.json`, `ledger.db`, candidate files,
run reports and read-back receipts there. Back it up; losing the ledger loses
deduplication and send history. On another harness, move that same state and
disable the previous scheduler before enabling the new one.

## Scheduling

Use the host's supported automation/scheduler. In Codex desktop, use its
Automations tool. In Hermes, use its installed cron/scheduling capability. For
Claude Code or a generic harness, use the operator's service scheduler to invoke
a verified headless command with this skill and the campaign path. Do not guess
CLI flags, install a second scheduler, or silently start multiple loops.

Schedule daily execution at **11:00 Asia/Kolkata** (05:30 UTC). A run starts at
11:00 and finishes after research; for delivery by 11:00, stage research earlier
and publish at 11:00. A skill cannot keep an asleep or offline laptop running.
An always-on server and a supervised harness are needed for unattended 24/7
availability. Repeated invocations on the same day fill only the remaining quota.
Missed days do not create a catch-up email burst.

Suggested scheduler prompt: "Use Sellbird with the private campaign at the
configured path. Deliver today's 10 new qualified dealers in each region to the
configured Google Sheet; reconcile pending work first. Use all authorized
available providers within the combined budget. Prepare WhatsApp drafts, and
send initial emails only under the saved sender authorization and verification.
Preserve deduplication, opt-outs and manual call notes. Report counts and blockers."

## Candidate schema

Write a JSON array of objects. The following is fictional, not an example lead
to publish. Use actual observed timestamps and evidence in production.

```json
{
  "business": "Example Realty",
  "business_type": "property_dealer",
  "region": "Gurgaon",
  "locality": "Sector 00, Gurgaon",
  "phones": ["+91 99999 00001"],
  "public_business_phone": true,
  "phone_source": "https://example.test/contact",
  "sources": ["https://example.test/contact", "https://example.test/listings"],
  "checked_at": "2026-11-21T10:30:00+05:30",
  "website_status": "present",
  "website": "https://example.test",
  "signals": [
    {"code": "lead_capture_gap", "evidence": "Contact page offers calls only for viewing enquiries", "url": "https://example.test/contact"},
    {"code": "active_business", "evidence": "Current property listings on the official site", "url": "https://example.test/listings"}
  ],
  "pitch": "A property enquiry page that captures location, budget and viewing preference",
  "whatsapp_draft": "Hi Example Realty team, I noticed viewing enquiries go through calls. We build property enquiry pages that capture a buyer's budget and preferred area. Would a quick example be useful?",
  "email": "",
  "email_status": "unknown",
  "email_subject": "",
  "email_draft": ""
}
```

Optional `place_id` supplies a stable listing identity. For `not_found`, supply
`website_checks`: at least two source URLs recording the listing and independent
brand search, and leave `website` empty. Other statuses: `unknown`, `unreachable`.
For `email_status: verified`, include `email_verifier` and timezone-aware
`email_checked_at`. A website showing an email is not deliverability verification.
Never infer a verifier result from MX records alone.

## Ledger and Google Sheets protocol

In the commands below, substitute the installed helper's path and private state
paths. No command contacts a provider; the host executes external tool actions.

```sh
python3 scripts/sellbird.py --db /private/campaign/ledger.db status
python3 scripts/sellbird.py --db /private/campaign/ledger.db headers
python3 scripts/sellbird.py --db /private/campaign/ledger.db prepare --input /private/campaign/candidates.json --next-row 1
python3 scripts/sellbird.py --db /private/campaign/ledger.db sheet-request --spreadsheet-id ACTUAL_ID --sheet-id 0
python3 scripts/sellbird.py --db /private/campaign/ledger.db confirm-sheet --rows /private/campaign/readback.json
```

Create a native Google Sheet once, record its actual ID, and use a `Leads` tab
with the exact `headers` output on row 1. Read metadata to obtain the actual tab
ID; **0 above is only an example**. Freeze the header, add a filter and format
phone columns as text. Keep an optional `Runs` tab for execution status.

Before preparing, inspect existing IDs and the last occupied row. `--next-row`
is the **zero-based** first free row: row 2 in the UI is index 1. Once reserved,
row indexes are stable. Use filter views; don't physically sort/move/delete rows
on the integration tab. If someone moved them, stop and reconcile the mapping
against lead IDs before writing. Never overwrite a nonempty row with another ID.

`sheet-request` emits a Google Sheets `batchUpdate` body using `updateCells`.
Ensure grid capacity covers planned rows; expand it first if needed. Check each
target range is empty or has the same ID. Execute through the host's Sheets
connector or authenticated Sheets API. Re-read IDs **and the complete row values**
and compare to the planned values. Confirm only after both match. `readback.json`
maps actual zero-based row indexes to IDs, e.g. `{"1":"actual_lead_id"}`.

If a write succeeds but the response is lost, read back before retrying. Repeated
fixed-row writes cannot append duplicates. Do not rewrite synced rows or erase
Call status/Notes. Update only Email status after a send. A pending row with
manual edits requires reconciliation; do not replay over those edits.
Reuse one ledger with one destination; the helper refuses a different sheet.
Do not silently create a new empty ledger when restoring an existing campaign.

## Email and replies

Before enabling, verify the exact sender is connected or a verified send-as
alias. Record the evidence privately. A different logged-in mailbox is not a
substitute. Read relevant recent replies and bounces before each daily run;
write opt-outs into the ledger, and do not auto-reply. Also import Sellbird's
suppression list when sharing prospects with an existing Sellbird installation.
Sync manual "Do not contact" dispositions into the same suppressions.

```sh
python3 scripts/sellbird.py --db /private/campaign/ledger.db suppress --identity email:optout@example.test --reason requested
python3 scripts/sellbird.py --db /private/campaign/ledger.db claim-email --lead-id ACTUAL_ID --sender sender@example.test --verified-sender sender@example.test
python3 scripts/sellbird.py --db /private/campaign/ledger.db record-email --lead-id ACTUAL_ID --receipt PROVIDER_MESSAGE_ID
```

Only use `--verified-sender` after inspecting the authenticated identity/alias;
this argument records the host's check, it cannot authenticate a mailbox.
Claim immediately before sending. Use the returned recipient, subject and body;
do not send a second time if claim fails. Persist the provider receipt immediately.
For an ambiguous network failure, call `record-email` with no receipt, leaving
the lead `uncertain`. Inspect the provider's Sent folder/message trace to resolve
it manually; never blindly retry. A crash after claim leaves `sending`, likewise
blocked. Send at most one initial email per selected business, 30 per day.
Include sender identity, a relevant evidence-based offer and a simple way to
decline. Do not claim to have called or visited. Keep WhatsApp as a draft.

No credentials, contacts or customer-specific settings belong in the skill,
test fixtures, commits or PR description. The helper validates records and
coordinates writes; actual source truth, tool authorization, spreadsheet
read-back, sender authentication and opt-out processing remain host duties.
