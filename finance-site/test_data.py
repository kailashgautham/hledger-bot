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
    # 5000 income - 20 spend - 1000 tax = 3980 saved, not 4980
    assert "3,980.00" in savings[0]["body"], savings[0]["body"]


def test_no_excluded_key_means_zero():
    d = data(LUNCH)
    assert month_of(d)["excluded"] == 0.0
    assert d["month"]["excluded"] == 0.0


def test_budgets_reconcile_with_headline_spend():
    """The gap that motivated this change: budget rows vs the headline total."""
    d = data(LUNCH, TAX, DONATION, GROCERIES)
    budgeted = sum(b["spent"] for b in d["budgets"]["items"])
    assert budgeted == month_of(d)["expenses"] == 70.0, (budgeted, month_of(d))


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
        assert "expenses:food" in s["budgets"], s
        # a zero limit would divide by zero when computing percentages
        path.write_text('{"budgets": {"expenses:x": {"limit": 0}}}')
        assert load_settings(path)["budgets"] == {}


def test_currency_flows_into_insight_text():
    d = build_data(journal(SALARY, LUNCH), "USD")
    assert d["currency"] == "USD"
    bodies = " ".join(c["body"] for c in d["insights"]["cards"])
    assert "USD" in bodies and "SGD" not in bodies, bodies


def test_name_is_passed_through():
    assert build_data(journal(LUNCH), "SGD", None, "Ada")["name"] == "Ada"
    assert build_data(journal(LUNCH))["name"] == ""


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
