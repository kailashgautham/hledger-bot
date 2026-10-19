"""Tests for the dashboard dataset, focused on how tax is treated.

Plain asserts so this runs anywhere with no dependencies:

    python3 finance-site/test_data.py

Not copied into the image — finance-site/Dockerfile lists its files explicitly.
"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from data import build_data, is_excluded_from_spend  # noqa: E402

# build_data derives "this month" from the clock, so the fixtures have to move
# with it — a fixed month would start failing at the next calendar boundary.
MONTH = date.today().strftime("%Y-%m")


def journal(*entries: str) -> str:
    return "\n".join(entries)


SALARY = f"""
{MONTH}-02 Salary
    assets:bank:dbs           SGD 5000.00
    income:salary
"""
LUNCH = f"""
{MONTH}-03 Lunch
    expenses:food             SGD 20.00
    liabilities:creditcard:dbs
"""
TAX = f"""
{MONTH}-04 IRAS income tax
    expenses:taxes            SGD 1000.00
    assets:bank:dbs
"""
TAX_SUBACCOUNT = f"""
{MONTH}-05 Property tax
    expenses:taxes:property   SGD 300.00
    assets:bank:dbs
"""
DONATION = f"""
{MONTH}-05 Charity
    expenses:donation         SGD 100.00
    assets:bank:dbs
"""
GROCERIES = f"""
{MONTH}-06 Groceries
    expenses:groceries        SGD 50.00
    liabilities:creditcard:dbs
"""


def data(*entries: str) -> dict:
    return build_data(journal(*entries), "SGD")


def month_of(d: dict) -> dict:
    """The MONTH row from the monthly series (build_data keys off today)."""
    for row in d["monthly"]:
        if row["month"] == MONTH:
            return row
    raise AssertionError(f"{MONTH} missing from monthly series")


def test_prefix_matching():
    assert is_excluded_from_spend("expenses:taxes")
    assert is_excluded_from_spend("expenses:taxes:property")
    assert is_excluded_from_spend("expenses:donation")
    assert not is_excluded_from_spend("expenses:food")
    # must not match a merely similar name
    assert not is_excluded_from_spend("expenses:taxesomething")


def test_donation_excluded_like_tax():
    d = data(LUNCH, DONATION, GROCERIES)
    assert month_of(d)["expenses"] == 70.0, month_of(d)
    assert month_of(d)["excluded"] == 100.0
    accounts = [c["account"] for c in d["expense_categories"]]
    assert "expenses:donation" not in accounts, accounts


def test_tax_not_counted_in_monthly_expenses():
    d = data(LUNCH, TAX, GROCERIES)
    assert month_of(d)["expenses"] == 70.0, month_of(d)
    assert month_of(d)["excluded"] == 1000.0


def test_tax_subaccounts_also_excluded():
    d = data(LUNCH, TAX, TAX_SUBACCOUNT)
    assert month_of(d)["expenses"] == 20.0
    assert month_of(d)["excluded"] == 1300.0


def test_tax_absent_from_category_breakdown():
    d = data(LUNCH, TAX, GROCERIES)
    accounts = [c["account"] for c in d["expense_categories"]]
    assert "expenses:taxes" not in accounts, accounts
    assert set(accounts) == {"expenses:food", "expenses:groceries"}


def test_tax_transaction_still_visible_in_ledger():
    """Excluding tax from totals must not hide it from the transaction list."""
    d = data(LUNCH, TAX)
    descriptions = [t["description"] for t in d["transactions"]]
    assert "IRAS income tax" in descriptions, descriptions


def test_savings_still_nets_off_tax():
    """Tax leaves the account, so it must not inflate the savings figure."""
    d = data(SALARY, LUNCH, TAX)
    cards = d["insights"]["cards"]
    savings = [c for c in cards if "saved" in c["body"] or "left over" in c["body"]]
    assert savings, [c["title"] for c in cards]
    # 5000 income - 20 spend - 1000 tax = 3980 saved, not 4980.
    # Amounts are placeholders now; the client formats them.
    assert "{{amt:3980.00}}" in savings[0]["body"], savings[0]["body"]


def test_no_excluded_key_means_zero():
    d = data(LUNCH)
    assert month_of(d)["excluded"] == 0.0
    assert d["month"]["excluded"] == 0.0


def test_budgets_reconcile_with_headline_spend():
    """The gap that motivated this change: budget rows vs the headline total."""
    d = data(LUNCH, TAX, DONATION, GROCERIES)
    budgeted = sum(b["spent"] for b in d["budgets"]["items"])
    assert budgeted == month_of(d)["expenses"] == 70.0, (budgeted, month_of(d))


def test_net_change_matches_net_worth_from_empty():
    """Starting from an empty ledger, this month's change must equal net worth.

    salary 5000 into the bank, 20 on a card, 1000 tax out of the bank
      assets      = 5000 - 1000 = 4000
      liabilities = -20
      net worth   = 3980
    """
    d = data(SALARY, LUNCH, TAX)
    assert d["net_worth"] == 3980.0, d["net_worth"]
    assert d["month"]["net_change"] == d["net_worth"], (d["month"]["net_change"], d["net_worth"])


def test_net_change_ignores_transfers_between_own_accounts():
    """Moving money around must not look like the net worth moved."""
    transfer = f"""
{MONTH}-07 Move to savings
    assets:bank:savings       SGD 500.00
    assets:bank:dbs          SGD -500.00
"""
    d = data(SALARY, transfer)
    assert d["month"]["net_change"] == 5000.0, d["month"]["net_change"]
    assert d["net_worth"] == 5000.0


def test_net_change_is_negative_when_spending_exceeds_income():
    d = data(LUNCH, GROCERIES)
    assert d["month"]["net_change"] == -70.0, d["month"]["net_change"]


def test_settings_loaded_from_file(tmp=None):
    """budgets.json overrides the defaults, including labels and exclusions."""
    import json
    import tempfile
    from data import load_settings, settings_path
    with tempfile.TemporaryDirectory() as d:
        journal = Path(d) / "journal.hledger"
        path = settings_path(journal)
        assert path.name == "budgets.json"
        path.write_text(json.dumps({
            "budgets": {"expenses:coffee": {"limit": 45, "label": "Coffee"}},
            "other_budget": 25,
            "exclude_from_spend": ["expenses:tuition"],
        }))
        s = load_settings(path)
        assert s["budgets"] == {"expenses:coffee": {"limit": 45.0, "label": "Coffee"}}, s
        assert s["other_budget"] == 25.0
        assert s["exclude_from_spend"] == ["expenses:tuition"]

        d2 = build_data(journal_text_with_coffee(), "SGD", s)
        labels = [b["label"] for b in d2["budgets"]["items"]]
        assert labels == ["Coffee", "Other"], labels


def journal_text_with_coffee() -> str:
    return f"""
{MONTH}-03 Kopi
    expenses:coffee           SGD 5.00
    liabilities:creditcard:dbs
"""


def test_bad_settings_fall_back_instead_of_crashing():
    import tempfile
    from data import load_settings
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "budgets.json"
        path.write_text("{ not json at all")
        s = load_settings(path)
        # No invented budgets to fall back to: limits get derived from the
        # ledger's own history instead, so the fallback is an empty dict.
        assert s["budgets"] == {}, s
        assert s["other_budget"] is None, s
        # a zero limit would divide by zero when computing percentages
        path.write_text('{"budgets": {"expenses:x": {"limit": 0}}}')
        assert load_settings(path)["budgets"] == {}


def test_insight_amounts_are_placeholders_not_prefixed_text():
    """Insight prose must not carry its own currency formatting.

    It used to read "SGD 3,657.13" while every other figure on the page was
    formatted by Intl as "$3,657.13". Amounts are now placeholders the client
    substitutes, so one formatter owns all of them.
    """
    d = build_data(journal(SALARY, LUNCH), "USD")
    assert d["currency"] == "USD"
    bodies = " ".join(c["body"] for c in d["insights"]["cards"])
    assert "{{amt:" in bodies, bodies
    for code in ("USD", "SGD"):
        assert code not in bodies, (code, bodies)


def test_budgets_are_derived_from_spending_not_invented():
    """With no budgets.json, each limit is that category's own median month."""
    import datetime
    first = date.today().replace(day=1)
    m1 = (first - datetime.timedelta(days=1)).replace(day=1)
    m2 = (m1 - datetime.timedelta(days=1)).replace(day=1)
    m3 = (m2 - datetime.timedelta(days=1)).replace(day=1)
    j = ""
    for m, amount in ((m3, 100), (m2, 300), (m1, 200)):
        j += f"""
{m.strftime('%Y-%m')}-05 Lunch
    expenses:food             SGD {amount}.00
    liabilities:creditcard:dbs
"""
    rows = {b["account"]: b for b in build_data(j, "SGD")["budgets"]["items"]}
    assert "expenses:food" in rows, rows
    # median of 100, 300, 200 is 200 — not a number from this source file
    assert rows["expenses:food"]["budget"] == 200.0, rows["expenses:food"]


def test_derived_budgets_ignore_the_running_month():
    """A month three days old must not set a budget near zero."""
    j = f"""
{MONTH}-01 Lunch
    expenses:food             SGD 5.00
    liabilities:creditcard:dbs
"""
    rows = build_data(j, "SGD")["budgets"]["items"]
    # No completed month, so expenses:food gets no derived limit at all.
    assert [r["account"] for r in rows] == ["expenses:other"], rows


def test_derived_budgets_exclude_tax():
    import datetime
    prev = (date.today().replace(day=1) - datetime.timedelta(days=1)).strftime("%Y-%m")
    j = f"""
{prev}-04 IRAS income tax
    expenses:taxes            SGD 1000.00
    assets:bank:dbs
"""
    accounts = [b["account"] for b in build_data(j, "SGD")["budgets"]["items"]]
    assert "expenses:taxes" not in accounts, accounts


def test_an_explicit_budget_still_wins_over_the_derived_one():
    import datetime
    prev = (date.today().replace(day=1) - datetime.timedelta(days=1)).strftime("%Y-%m")
    j = f"""
{prev}-05 Lunch
    expenses:food             SGD 500.00
    liabilities:creditcard:dbs
"""
    settings = {"budgets": {"expenses:food": {"limit": 42.0, "label": "Food"}},
                "other_budget": 7.0, "exclude_from_spend": []}
    rows = {b["account"]: b for b in build_data(j, "SGD", settings)["budgets"]["items"]}
    assert rows["expenses:food"]["budget"] == 42.0, rows["expenses:food"]


def test_no_history_means_no_invented_budget_rows():
    """An empty ledger must not render a budget it made up."""
    assert build_data("", "SGD")["budgets"]["items"] == []


def test_spending_with_no_history_is_shown_not_measured_against_a_guess():
    d = data(LUNCH)
    rows = d["budgets"]["items"]
    assert len(rows) == 1 and rows[0]["account"] == "expenses:other", rows
    # the bar is what was spent, not a number from this source file
    assert rows[0]["budget"] == rows[0]["spent"] == 20.0, rows[0]
    assert rows[0]["over"] is False, rows[0]


def test_name_is_passed_through():
    assert build_data(journal(LUNCH), "SGD", None, "Ada")["name"] == "Ada"
    assert build_data(journal(LUNCH))["name"] == ""


def test_net_worth_series_ends_at_current_net_worth():
    """The running total must reconcile with the headline figure exactly.

    If it drifts, the trend line is telling a different story from the Net
    worth card sitting above it.
    """
    prev = (date.today().replace(day=1) - __import__("datetime").timedelta(days=1)).strftime("%Y-%m")
    older = f"""
{prev}-05 Opening
    assets:bank:uob           SGD 1000.00
    equity:opening

{prev}-10 Rent
    expenses:rent             SGD 300.00
    assets:bank:uob
"""
    d = data(older, SALARY, LUNCH, TAX)
    series = d["net_worth_series"]
    assert len(series) == 2, [s["month"] for s in series]
    assert series[-1]["value"] == d["net_worth"], (series, d["net_worth"])
    # and it must actually move, not be flat
    assert series[0]["value"] != series[-1]["value"], series


def test_net_worth_series_is_cumulative_not_per_month():
    prev = (date.today().replace(day=1) - __import__("datetime").timedelta(days=1)).strftime("%Y-%m")
    j = f"""
{prev}-05 Opening
    assets:bank:uob           SGD 100.00
    equity:opening

{MONTH}-05 More
    assets:bank:uob           SGD 50.00
    equity:opening
"""
    series = build_data(j, "SGD")["net_worth_series"]
    assert [s["value"] for s in series] == [100.0, 150.0], series


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL  {t.__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)