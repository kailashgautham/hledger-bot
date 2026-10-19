"""Tests for the import suggestion pipeline.

    python3 finance-site/test_import_suggestions.py

Covers the two failures behind "No suggestion — choose a category." arriving on
every row of a statement: one AI request per transaction against a 15/min free
tier, and no offline fallback when that request failed.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot.categoriser import Categoriser  # noqa: E402
from bot.local_categoriser import LocalCategoriser  # noqa: E402

ACCOUNTS = ["expenses:transportation", "expenses:food", "expenses:groceries"]


def make_categoriser(raw_response=None, fail=False, calls=None):
    """A Categoriser with the provider stubbed out — no network, no key."""
    c = Categoriser({"ai": {"provider": "none"}})
    c._client = object()  # pretend a provider is configured

    def _call(prompt):
        if calls is not None:
            calls.append(prompt)
        if fail:
            raise RuntimeError("429 quota exceeded")
        return raw_response

    c._call = _call
    return c


# -- batching ---------------------------------------------------------

def test_a_whole_statement_is_one_request():
    """25 rows used to mean 25 requests, which blew the 15/min free tier."""
    calls = []
    body = ", ".join(f'"{i}": {{"account": "expenses:food", "confidence": 0.9}}'
                     for i in range(25))
    c = make_categoriser("{" + body + "}", calls=calls)
    items = [{"description": f"SHOP {i}", "amount": -10.0} for i in range(25)]
    got = c.suggest_many(items, "SGD", ACCOUNTS, [])
    assert len(calls) == 1, f"expected 1 request, made {len(calls)}"
    assert len(got) == 25, got


def test_batch_prompt_lists_every_transaction():
    calls = []
    c = make_categoriser('{"0": {"account": "expenses:food", "confidence": 0.9}}', calls=calls)
    c.suggest_many([{"description": "KOPITIAM", "amount": -4.5},
                    {"description": "GRAB TRIP", "amount": -15.81}], "SGD", ACCOUNTS, [])
    assert "KOPITIAM" in calls[0] and "GRAB TRIP" in calls[0], calls[0]


def test_batch_marks_income_rows():
    calls = []
    c = make_categoriser("{}", calls=calls)
    c.suggest_many([{"description": "Salary", "amount": 5000.0, "type": "income"}],
                   "SGD", ACCOUNTS, [])
    assert "(income)" in calls[0], calls[0]


# -- the AI failing must not wipe out the whole statement --------------

def test_a_failed_request_returns_empty_not_an_exception():
    c = make_categoriser(fail=True)
    got = c.suggest_many([{"description": "X", "amount": -1.0}], "SGD", ACCOUNTS, [])
    assert got == {}, got


def test_unparseable_response_returns_empty():
    for bad in ("not json", "[]", '{"0": "expenses:food"}', '{"0": {}}'):
        c = make_categoriser(bad)
        got = c.suggest_many([{"description": "X", "amount": -1.0}], "SGD", ACCOUNTS, [])
        assert got == {}, (bad, got)


def test_partial_response_keeps_the_rows_it_answered():
    """A short answer must not discard the rows that did come back."""
    c = make_categoriser('{"0": {"account": "expenses:food", "confidence": 0.9}}')
    items = [{"description": "A", "amount": -1.0}, {"description": "B", "amount": -2.0}]
    got = c.suggest_many(items, "SGD", ACCOUNTS, [])
    assert set(got) == {0}, got


def test_out_of_range_indexes_are_dropped():
    c = make_categoriser('{"0": {"account": "expenses:food", "confidence": 0.9},'
                         ' "7": {"account": "expenses:groceries", "confidence": 0.9},'
                         ' "x": {"account": "expenses:food", "confidence": 0.9}}')
    got = c.suggest_many([{"description": "A", "amount": -1.0}], "SGD", ACCOUNTS, [])
    assert set(got) == {0}, got


def test_markdown_fences_are_tolerated():
    c = make_categoriser('```json\n{"0": {"account": "expenses:food", "confidence": 0.88}}\n```')
    got = c.suggest_many([{"description": "A", "amount": -1.0}], "SGD", ACCOUNTS, [])
    assert got[0] == ("expenses:food", 0.88), got


def test_no_provider_means_no_request_and_no_crash():
    c = Categoriser({"ai": {"provider": "none"}})
    assert c.available is False
    assert c.suggest_many([{"description": "A", "amount": -1.0}], "SGD", ACCOUNTS, []) == {}


# -- the pipeline's ordering ------------------------------------------
#
# Mirrors start_categorisation without needing a config.yaml or a journal on
# disk: merchant rule -> local evidence -> AI, and the AI only where local
# evidence is weak.

JOURNAL = """
2026-09-02 NTUC FAIRPRICE
    expenses:groceries             SGD 62.10
    liabilities:creditcard:dbs
"""


def pipeline(descriptions, rules=None, ai=None, ai_conf=0.95):
    rules = rules or {}
    local = LocalCategoriser(JOURNAL, ACCOUNTS)
    staged, needs_ai = [], []
    for d in descriptions:
        known = rules.get(d.upper())
        if known:
            entry = {"account": known, "conf": 0.99, "src": "your merchant rule"}
        else:
            guess = local.suggest(d)
            entry = ({"account": guess[0], "conf": guess[1], "src": guess[2]}
                     if guess else {"account": None, "conf": 0.0, "src": None})
        if entry["conf"] < 0.9:
            needs_ai.append(len(staged))
        staged.append(entry)
    for slot, idx in enumerate(needs_ai):
        answer = (ai or {}).get(slot)
        if answer and ai_conf >= staged[idx]["conf"]:
            staged[idx] = {"account": answer, "conf": ai_conf, "src": "AI"}
    return staged


def test_transport_needs_no_ai_at_all():
    """The reported bug, end to end: no provider, still a suggestion."""
    got = pipeline(["Transport"], ai={})
    assert got[0]["account"] == "expenses:transportation", got


def test_a_known_merchant_rule_wins_and_skips_the_ai():
    got = pipeline(["Transport"], rules={"TRANSPORT": "expenses:food"}, ai={0: "expenses:groceries"})
    assert got[0]["account"] == "expenses:food", got
    assert got[0]["src"] == "your merchant rule", got


def test_confident_local_matches_are_not_sent_to_the_ai():
    """A repeat merchant should not cost a request."""
    local = LocalCategoriser(JOURNAL, ACCOUNTS)
    assert local.suggest("NTUC FAIRPRICE")[1] >= 0.9
    got = pipeline(["NTUC FAIRPRICE"], ai={0: "expenses:food"})
    assert got[0]["account"] == "expenses:groceries", got


def test_ai_fills_in_only_what_local_could_not():
    got = pipeline(["ZZQX HOLDINGS"], ai={0: "expenses:food"})
    assert got[0]["account"] == "expenses:food", got
    assert got[0]["src"] == "AI", got


def test_everything_still_resolves_when_the_ai_answers_nothing():
    """The old behaviour for this input was a blank row on every line."""
    got = pipeline(["Transport", "Groceries", "NTUC FAIRPRICE"], ai={})
    assert [g["account"] for g in got] == [
        "expenses:transportation", "expenses:groceries", "expenses:groceries",
    ], got
    assert all(g["src"] for g in got), got


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
