"""Tests for offline categorisation.

    python3 finance-site/test_local_categoriser.py

The case that prompted this: a statement line called "Transport" arriving with
"No suggestion — choose a category." while expenses:transportation sat right
there in the journal.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.local_categoriser import (  # noqa: E402
    LocalCategoriser, account_terms, normalise, tokens,
)

ACCOUNTS = [
    "expenses:transportation",
    "expenses:food",
    "expenses:food:dining-out",
    "expenses:groceries",
    "expenses:fitness",
    "expenses:subscriptions",
    "expenses:utilities",
    "expenses:taxes",
    "income:salary",
    "income:interest",
    "assets:bank:dbs",
    "liabilities:creditcard:dbs",
]

JOURNAL = """
2026-09-01 GRAB *TRIP 4821 SINGAPORE SG
    expenses:transportation        SGD 15.81
    liabilities:creditcard:dbs

2026-09-02 NTUC FAIRPRICE FINEST
    expenses:groceries             SGD 62.10
    liabilities:creditcard:dbs

2026-09-03 GRAB *TRIP 9915 SINGAPORE SG
    expenses:transportation        SGD 9.40
    liabilities:creditcard:dbs

2026-09-04 Salary
    assets:bank:dbs                SGD 5000.00
    income:salary

2026-09-05 ANYTIME FITNESS BUKIT TIMAH
    expenses:fitness               SGD 67.64
    liabilities:creditcard:dbs
"""


def cat(journal=JOURNAL, accounts=None, labels=None):
    return LocalCategoriser(journal, accounts if accounts is not None else ACCOUNTS, labels)


# -- the reported bug -------------------------------------------------

def test_transport_matches_transportation_account():
    """The exact failure from the screenshot: "Transport", no suggestion."""
    got = cat().suggest("Transport")
    assert got is not None, "Transport produced no suggestion at all"
    assert got[0] == "expenses:transportation", got


def test_every_expense_account_is_reachable_by_its_own_name():
    """Not just Transport — each account must be findable by its own name.

    This is the generalisation the one-off fix would have missed.
    """
    c = cat()
    failures = []
    for account in ACCOUNTS:
        if not account.startswith("expenses"):
            continue
        name = account.split(":")[-1].replace("-", " ")
        got = c.suggest(name)
        if got is None or got[0] != account:
            failures.append((name, account, got[0] if got else None))
    assert not failures, failures


def test_account_name_match_works_with_no_journal_history():
    """A brand-new ledger still gets name matching; it must not need history."""
    got = LocalCategoriser("", ACCOUNTS).suggest("Groceries")
    assert got and got[0] == "expenses:groceries", got


def test_plural_and_singular_both_match():
    c = cat()
    for word in ("Utilities", "Utility", "Subscription", "Subscriptions"):
        got = c.suggest(word)
        assert got is not None, word
        assert got[0].startswith("expenses:"), (word, got)


# -- history ----------------------------------------------------------

def test_exact_repeat_uses_what_you_filed_before():
    got = cat().suggest("NTUC FAIRPRICE FINEST")
    assert got and got[0] == "expenses:groceries", got
    assert got[1] >= 0.95, got


def test_repeated_merchant_outranks_a_one_off():
    """Two sightings of the same merchant must not weaken the match."""
    got = cat().suggest("GRAB *TRIP 4821 SINGAPORE SG")
    assert got and got[0] == "expenses:transportation", got
    assert got[1] >= 0.95, got


def test_reference_numbers_do_not_defeat_a_repeat():
    """GRAB *TRIP 1234 and GRAB *TRIP 9999 are the same merchant."""
    got = cat().suggest("GRAB *TRIP 7777 SINGAPORE SG")
    assert got and got[0] == "expenses:transportation", got
    assert got[1] >= 0.95, got


def test_partial_overlap_still_suggests():
    got = cat().suggest("ANYTIME FITNESS TANJONG PAGAR")
    assert got and got[0] == "expenses:fitness", got


def test_history_beats_account_name_match():
    """What you actually did outranks a name coincidence."""
    j = JOURNAL + """
2026-09-06 FOOD REPUBLIC
    expenses:groceries             SGD 8.00
    liabilities:creditcard:dbs
"""
    got = LocalCategoriser(j, ACCOUNTS).suggest("FOOD REPUBLIC")
    assert got[0] == "expenses:groceries", got
    assert got[1] >= 0.95, got


# -- income vs expense ------------------------------------------------

def test_income_never_suggests_an_expense_account():
    got = cat().suggest("Salary", is_income=True)
    assert got and got[0] == "income:salary", got


def test_expense_never_suggests_an_income_account():
    got = cat().suggest("Salary", is_income=False)
    assert got is None or got[0].startswith("expenses:"), got


# -- restraint --------------------------------------------------------

def test_unknown_merchant_returns_nothing_rather_than_guessing():
    got = cat().suggest("ZZQX HOLDINGS 88231")
    assert got is None, got


def test_short_words_do_not_match_accounts():
    """CAR must not match CARPET-style coincidences."""
    c = LocalCategoriser("", ["expenses:carpet-cleaning"])
    assert c.suggest("CAR") is None, c.suggest("CAR")


def test_statement_furniture_is_not_evidence():
    """A description made only of card-statement noise must not match."""
    assert cat().suggest("CARD PAYMENT SINGAPORE SG REF 88213") is None


def test_every_word_of_a_compound_account_must_be_present():
    c = LocalCategoriser("", ["expenses:food:dining-out"])
    assert c.suggest("Dining Out") is not None
    # "Out" alone is not enough to claim the dining-out account
    got = c.suggest("Out")
    assert got is None, got


# -- budget labels ----------------------------------------------------

def test_budget_labels_are_used_as_synonyms():
    """budgets.json labels are user-written synonyms for their accounts."""
    c = cat(labels={"Eating out": "expenses:food:dining-out"})
    got = c.suggest("Eating out")
    assert got and got[0] == "expenses:food:dining-out", got


# -- parsing helpers --------------------------------------------------

def test_normalise_strips_reference_noise():
    assert normalise("GRAB  *TRIP 4821  SG") == "GRAB TRIP SG"


def test_tokens_drop_furniture_and_account_prefixes():
    assert "EXPENSES" not in tokens("expenses food")
    assert "FOOD" in tokens("expenses food")


def test_account_terms_drop_the_type_segment():
    assert account_terms("expenses:transportation") == ["TRANSPORTATION"]
    assert account_terms("expenses:food:dining-out") == ["FOOD", "DINING", "OUT"]


def test_malformed_journal_does_not_break_the_index():
    """A broken block must not leave the importer with no local evidence."""
    broken = "not a journal at all\n   dangling line\n2026-13-99 bad date\n" + JOURNAL
    got = LocalCategoriser(broken, ACCOUNTS).suggest("NTUC FAIRPRICE FINEST")
    assert got and got[0] == "expenses:groceries", got


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
