"""Companies and Contacts load every row, so the page's search, filters and
sort cover all of them."""

from tests.test_dashboard_live_status import _company, _sql, client, env  # noqa: F401


def test_companies_send_only_the_columns_shown(client, env):
    _company(env, "a", domain="a.example", profiled=True)
    _sql(env, "UPDATE companies SET description = 'long text' WHERE id = 'a'")
    rows = {c["id"]: c for c in client.get("/api/companies").json()}
    assert rows["a"]["domain"] == "a.example" and "description" not in rows["a"]


def test_contacts_are_not_cut_at_200(client, env):
    for i in range(250):
        _sql(env, "INSERT INTO prospects (id, first_name, last_name, email, status, company) "
                  "VALUES (?, 'A', 'B', ?, 'new', 'Acme')", (f"p{i}", f"p{i}@acme.example"))
    assert len(client.get("/api/prospects").json()) == 250
