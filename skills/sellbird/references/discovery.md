# Discovery and qualification

Sellbird follows Sellbird's discover → profile → enrich → verify → write flow.
The host agent chooses tools by capability; the skill has no hardcoded MCP tool
names, provider credentials, or dependence on a particular model vendor.

## Provider coverage

Inventory installed skills, connectors and configured CLI providers at setup
and when availability changes. Persist a coverage report with provider, purpose,
status, query count, records found and cost. "Missing credential", "not installed",
"not authorized", "budget exhausted" and "failed" are distinct from no results.

When Sellbird is installed, run `sellbird discover --providers` to inspect its
available sources. It currently includes OSM, TinyFish, DataForSEO listings/SERP/
Maps, Serper, and Scavio Maps/Search. Use an isolated Sellbird home configured
for this campaign, not an unrelated production database. Confirm the operator's
selected signals, set the property-dealer ICP, estimate each provider run and
debit all runs against **one** daily campaign budget. Never multiply the budget
by giving every provider the full daily allowance. See
[`docs/signals-and-pipeline.md`](../../../docs/signals-and-pipeline.md) in the
source checkout for native commands. A copied skill works without Sellbird.

If Sellbird is absent, the host can use connected Maps/search providers directly
and emit the same candidate schema. Read applicable installed scraping skills
before use. Maps/listing services find phone-bearing local businesses; Google
Search and alternate web search find official sites/contact pages. Use available
authorized LinkedIn tools for business/decision-maker corroboration; if no such
tool is available, record that limitation and use public company sources.
Do not log in to LinkedIn through guessed credentials or bypass access controls.

Search by region and neighborhood, varying queries each day:

- Gurgaon/Gurugram: property dealer, real estate broker, residential consultancy;
  DLF phases, Golf Course Road, Sohna Road, New Gurgaon, Dwarka Expressway.
- Mumbai: the same categories across Andheri, Borivali, Powai, Bandra, Chembur,
  Goregaon and Mulund. Do not silently expand to Pune or all Maharashtra.
- Delhi NCR: Delhi, Noida, Greater Noida, Ghaziabad and Faridabad; exclude Gurgaon.

Research a wider pool than the 30 daily selections. Exclude portals, directories,
developers without brokerage services, inactive businesses, duplicates and
unverified private numbers. A directory can supply evidence about a dealer;
the directory itself is not the lead. Keep all discovered public business
numbers on one row. Record phone source and time checked. A syntactically valid
phone is not proof it connects or that it supports WhatsApp.

## Ranking

The helper first enforces category, location, public phone, recent source checks,
and an evidenced opportunity. Those gates contribute 70 points. Evidence of
current business activity adds 15, a decision maker adds 10, and a freshly
verified deliverable email adds 5. Minimum: 80. This is a prioritization heuristic,
not a conversion probability. Quality evidence is still the agent's job.

Allowed opportunity codes: `website_gap`, `lead_capture_gap`, `followup_gap`.
Allowed corroboration codes: `active_business`, `decision_maker`.
Every signal needs a plain-language observation and its source URL. Examples:
an official page with recent inventory; a published owner role; a contact page
that only offers phone calls for enquiries. Do not use an unobserved assumption
such as "no automation". A missing listing website field alone is insufficient
to say no website. All evidence must have been checked in the past seven days.
