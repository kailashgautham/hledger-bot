"""Build the deterministic dashboard dataset from a hledger journal."""
import json
import logging
import re
from collections import defaultdict
from datetime import date
from pathlib import Path

from balances import account_balances, account_total, parse_amount

logger = logging.getLogger(__name__)

_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")
_STATUS_RE = re.compile(r"^[!*]\s*")

OTHERS_ACCOUNT = "expenses:other"
SETTINGS_FILENAME = "budgets.json"

# Used when no budgets.json is present, so a fresh install still renders.
DEFAULT_SETTINGS: dict = {
    "budgets": {
        "expenses:food": {"limit": 200, "label": "Food"},
        "expenses:fitness": {"limit": 90, "label": "Fitness"},
        "expenses:subscriptions": {"limit": 20, "label": "Subscriptions"},
        "expenses:entertainment": {"limit": 50, "label": "Entertainment"},
        "expenses:transportation": {"limit": 80, "label": "Transport"},
    },
    "other_budget": 60.0,
    # Accounts that are money out but not "spending" you can act on. One list,
    # applied everywhere: the headline spend figure, the cash-flow bars, the
    # category breakdown, the per-category insights and the budget rows.
    # They remain real outflow: the transactions stay in the ledger view, the
    # total is reported as month.excluded, and savings still nets them off.
    # Matching is by account prefix, so expenses:taxes:property is covered too.
    "exclude_from_spend": ["expenses:taxes", "expenses:donation"],
}


def settings_path(journal: Path) -> Path:
    """budgets.json lives beside the journal, inside the same git repo, so
    edits are versioned with the ledger and need no redeploy."""
    return journal.parent / SETTINGS_FILENAME


def load_settings(path: Path | None) -> dict:
    """Read budgets.json over the defaults.

    A missing or malformed file must never take the dashboard down, so this
    falls back to the defaults and logs rather than raising.
    """
    settings = {
        "budgets": dict(DEFAULT_SETTINGS["budgets"]),
        "other_budget": DEFAULT_SETTINGS["other_budget"],
        "exclude_from_spend": list(DEFAULT_SETTINGS["exclude_from_spend"]),
    }
    if not path or not path.exists():
        return settings
    try:
        raw = json.loads(path.read_text())
    except Exception:
        logger.exception("%s is not valid JSON; using defaults", path)
        return settings

    budgets = raw.get("budgets")
    if isinstance(budgets, dict):
        parsed = {}
        for account, spec in budgets.items():
            # Accept {"limit": 200, "label": "Food"} or a bare number.
            if isinstance(spec, dict):
                limit, label = spec.get("limit"), spec.get("label")
            else:
                limit, label = spec, None
            try:
                limit = float(limit)
            except (TypeError, ValueError):
                logger.warning("budget for %s is not a number; skipped", account)
                continue
            # A zero budget would divide by zero when computing percentages.
            if limit <= 0:
                logger.warning("budget for %s must be > 0; skipped", account)
                continue
            parsed[account] = {
                "limit": limit,
                "label": label or account.replace("expenses:", "").title(),
            }
        settings["budgets"] = parsed

    other = raw.get("other_budget")
    if other is not None:
        try:
            if float(other) > 0:
                settings["other_budget"] = float(other)
        except (TypeError, ValueError):
            logger.warning("other_budget is not a number; using default")

    exclude = raw.get("exclude_from_spend")
    if isinstance(exclude, list):
        settings["exclude_from_spend"] = [str(a) for a in exclude]

    return settings


def _excluder(settings: dict):
    prefixes = tuple(settings.get("exclude_from_spend") or ())

    def is_excluded(account: str) -> bool:
        return any(account == a or account.startswith(a + ":") for a in prefixes)

    return is_excluded


def is_excluded_from_spend(account: str) -> bool:
    """Default-settings convenience wrapper, used by tests and callers that
    have no settings object to hand."""
    return _excluder(DEFAULT_SETTINGS)(account)


def parse_transactions(text: str) -> list[dict]:
    """Return [{date, description, postings:[{account, amount}]}]."""
    txns: list[dict] = []
    current = None
    for line_num, line in enumerate(text.splitlines(), 1):
        m = _DATE_RE.match(line)
        if m:
            current = {
                "date": m.group(1),
                "description": line[m.end():].split(";")[0].strip(),
                "postings": [],
                "_line": line_num,
            }
            txns.append(current)
        elif current is not None and line[:1].isspace():
            p = line.strip()
            if not p or p.startswith(";"):
                continue
            p = _STATUS_RE.sub("", p)
            parts = re.split(r"\s{2,}", p)
            account = parts[0].strip()
            amount = parse_amount(" ".join(parts[1:])) if len(parts) > 1 else None
            current["postings"].append(
                {"account": account, "amount": amount[0] if amount else None}
            )
        elif current is not None:
            current = None
    return txns


def _type_amount(postings: list[dict]) -> tuple[str, float]:
    """Classify a transaction and return its display amount."""
    # Signed sums: refunds/offsets (e.g. a friend repaying part of a meal as a
    # negative expenses:X posting) must reduce spend, not add to it.
    income = sum(p["amount"] for p in postings if p["amount"] and p["account"].startswith("income:"))
    expense = sum(p["amount"] for p in postings if p["amount"] and p["account"].startswith("expenses:"))
    if expense and not income:
        return "expense", -expense
    if income and not expense:
        return "income", -income
    largest = max((p["amount"] for p in postings if p["amount"]), default=0.0)
    return "transfer", largest


def _days_in_month(key: str) -> int:
    y, m = int(key[:4]), int(key[5:7])
    nxt = date(y + (m == 12), (m % 12) + 1, 1)
    return (nxt - date(y, m, 1)).days


def _month_label(key: str) -> str:
    return date.fromisoformat(key + "-01").strftime("%B %Y")


def _fmt(v: float, currency: str = "SGD") -> str:
    return f"{currency} {v:,.2f}"


def compute_insights(monthly, monthly_cats, tx_view, this_month,
                     currency: str = "SGD") -> dict:
    """Deterministic monthly insights: overspending flags + positive signals."""
    fmt = lambda v: _fmt(v, currency)  # noqa: E731
    today = date.today()
    mkeys = sorted(m for m in monthly
                   if monthly[m]["expenses"] > 0 and m <= this_month)
    if not mkeys:
        return {"month": None, "cards": []}

    target = mkeys[-1]
    inc, exp = monthly[target]["income"], monthly[target]["expenses"]
    # Tax is left out of `exp` so it can't distort spending comparisons, but it
    # is still money that left the account — netting it off here keeps the
    # savings figure honest rather than flattering.
    excl = monthly[target].get("excluded", 0.0)
    days = (max(1, min(today.day, _days_in_month(target)))
            if target == this_month else _days_in_month(target))
    daily = exp / days

    cats = sorted(monthly_cats[target].items(), key=lambda kv: -kv[1])
    top_acct, top_amt = (cats[0] if cats else (None, 0.0))
    prior = [m for m in mkeys if m < target]

    cards = []

    if inc > 0:
        saved, rate = inc - exp - excl, (inc - exp - excl) / inc
        if rate >= 0.20:
            cards.append({"tone": "good",
                          "title": "Strong month — you kept 20%+ of your income",
                          "body": f"You saved {fmt(saved)} of {fmt(inc)} earned in {_month_label(target)} ({rate:.0%}). Great momentum."})
        elif rate >= 0.05:
            cards.append({"tone": "good",
                          "title": "Positive savings this month",
                          "body": f"You saved {fmt(saved)} of {fmt(inc)} ({rate:.0%}) in {_month_label(target)}."})
        elif rate >= 0:
            cards.append({"tone": "info",
                          "title": "Savings are thin",
                          "body": f"Only {fmt(saved)} ({rate:.0%}) left over in {_month_label(target)}. Consider trimming a category."})
        else:
            cards.append({"tone": "info",
                          "title": "Income hasn't landed yet this month",
                          "body": f"Spent {fmt(exp)} so far, but income typically arrives later in the month. Check back after your payday."})

    if prior:
        prev = monthly[prior[-1]]["expenses"]
        if prev > 0:
            delta, pct = exp - prev, exp / prev - 1
            if pct <= -0.05:
                tone, dirn = "good", f"down {abs(pct):.0%}"
            elif pct >= 0.05:
                tone, dirn = "warn", f"up {pct:.0%}"
            else:
                tone, dirn = "info", "about flat"
            cards.append({"tone": tone,
                          "title": f"Spending {dirn} vs {_month_label(prior[-1])}",
                          "body": f"{fmt(exp)} this month vs {fmt(prev)} ({'+' if delta > 0 else ''}{fmt(delta)})."})
    else:
        cards.append({"tone": "info",
                      "title": "First full month tracked",
                      "body": f"{_month_label(target)} is your baseline. Month-over-month trends will appear from next month."})

    if top_acct:
        cards.append({"tone": "info",
                      "title": f"Top category: {top_acct.replace('expenses:', '')}",
                      "body": f"{top_acct.replace('expenses:', '')} was {top_amt / exp:.0%} of spend ({fmt(top_amt)}) in {_month_label(target)}."})

    for acct, amt in cats:
        if amt < 25:
            continue
        hist = [monthly_cats.get(m, {}).get(acct, 0.0) for m in prior]
        if len(hist) >= 1 and max(hist) > 0:
            avg = sum(hist) / len(hist)
            if amt > avg * 1.5 and amt - avg >= 20:
                cards.append({"tone": "warn",
                              "title": f"Overspending on {acct.replace('expenses:', '')}",
                              "body": f"{fmt(amt)} vs your usual {fmt(avg)} — {((amt - avg) / avg):.0%} more than normal. This is the main thing to watch."})

    exps = [t for t in tx_view if t["type"] == "expense" and t["amount"] < 0 and t["date"].startswith(target)]
    if exps:
        biggest = max(exps, key=lambda t: abs(t["amount"]))
        cards.append({"tone": "info",
                      "title": "Biggest purchase",
                      "body": f"{biggest['description']} — {fmt(abs(biggest['amount']))} on {biggest['date']}."})

    return {
        "month": target,
        "month_label": _month_label(target),
        "spent": round(exp, 2),
        "daily_avg": round(daily, 2),
        "count": len(exps),
        "cards": cards,
    }


def build_data(text: str, currency: str = "SGD", settings: dict | None = None,
               name: str = "") -> dict:
    settings = settings or load_settings(None)
    is_excluded = _excluder(settings)
    budgets = settings["budgets"]
    other_budget = settings["other_budget"]
    balances = account_balances(text)
    txns = parse_transactions(text)

    assets = sum(account_total(balances, a, currency) for a in balances if a.startswith("assets:"))
    liabilities = sum(account_total(balances, a, currency) for a in balances if a.startswith("liabilities:"))

    monthly: dict[str, dict] = defaultdict(
        lambda: {"income": 0.0, "expenses": 0.0, "excluded": 0.0, "net_change": 0.0})
    monthly_cats: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    expense_cats: dict[str, float] = defaultdict(float)
    monthly_income: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    tx_view = []

    for t in txns:
        known = [p for p in t["postings"] if p["amount"] is not None]
        missing = [p for p in t["postings"] if p["amount"] is None]
        total = sum(p["amount"] for p in known)
        postings = list(known) + [
            {"account": p["account"], "amount": -total} for p in missing
        ]
        income = sum(abs(p["amount"]) for p in postings if p["account"].startswith("income:"))
        expense = sum(p["amount"] for p in postings
                      if p["account"].startswith("expenses:")
                      and not is_excluded(p["account"]))
        excluded = sum(p["amount"] for p in postings
                       if p["account"].startswith("expenses:")
                       and is_excluded(p["account"]))
        ttype, amt = _type_amount(postings)
        tx_view.append({
            "line_no": t["_line"],
            "date": t["date"],
            "description": t["description"],
            "type": ttype,
            "amount": round(amt, 2),
            "accounts": [p["account"] for p in t["postings"]],
        })
        # Net worth is assets + liabilities, so its change over a period is
        # simply the sum of that period's postings to those accounts.
        monthly[t["date"][:7]]["net_change"] += sum(
            p["amount"] for p in postings
            if p["account"].startswith(("assets:", "liabilities:")))
        month = t["date"][:7]
        monthly[month]["income"] += income
        monthly[month]["expenses"] += expense
        monthly[month]["excluded"] += excluded
        for p in postings:
            if not p["amount"]:
                continue
            if p["account"].startswith("expenses:"):
                if is_excluded(p["account"]):
                    continue
                expense_cats[p["account"]] += p["amount"]
                monthly_cats[month][p["account"]] += p["amount"]
            elif p["account"].startswith("income:"):
                monthly_income[month][p["account"]] += abs(p["amount"])

    tx_view.sort(key=lambda t: t["date"], reverse=True)
    months = sorted(monthly)
    today = date.today().isoformat()
    this_month = today[:7]
    tm = monthly.get(this_month,
                     {"income": 0.0, "expenses": 0.0, "excluded": 0.0, "net_change": 0.0})

    budget_month = this_month
    budget_cats = {a: v for a, v in monthly_cats.get(budget_month, {}).items() if v > 0}
    budget_rows = []
    for acct in sorted(budgets, key=lambda a: -budget_cats.get(a, 0.0)):
        budget = budgets[acct]["limit"]
        spent = budget_cats.get(acct, 0.0)
        budget_rows.append({
            "account": acct,
            "label": budgets[acct]["label"],
            "spent": round(spent, 2),
            "budget": budget,
            "pct": round(spent / budget * 100, 1),
            "over": spent > budget,
            "warn": not (spent > budget) and spent >= budget * 0.75,
            "members": [acct],
        })
    # Excluded accounts never reach budget_cats, so no extra filter is needed.
    others = {a: v for a, v in budget_cats.items() if a not in budgets}
    others_total = sum(others.values())
    budget_rows.append({
        "account": OTHERS_ACCOUNT,
        "label": "Other",
        "spent": round(others_total, 2),
        "budget": other_budget,
        "pct": round(others_total / other_budget * 100, 1),
        "over": others_total > other_budget,
        "warn": not (others_total > other_budget) and others_total >= other_budget * 0.75,
        "members": sorted(others.keys()),
    })

    def by_magnitude(items: dict[str, float]) -> list[dict]:
        return [{"account": a, "amount": round(v, 2)} for a, v in
                sorted(items.items(), key=lambda kv: -kv[1])]

    insights = compute_insights(monthly, {m: dict(c) for m, c in monthly_cats.items()},
                                tx_view, this_month, currency)

    return {
        "generated_at": today,
        "currency": currency,
        "name": name,
        "as_of": today,
        "net_worth": round(assets + liabilities, 2),
        "assets": round(assets, 2),
        "liabilities": round(liabilities, 2),
        "month": {
            "key": this_month,
            "income": round(tm["income"], 2),
            "expenses": round(tm["expenses"], 2),
            # Outflow deliberately kept out of "expenses" (tax). Surfaced so the
            # dashboard can show it rather than silently losing the money.
            "excluded": round(tm.get("excluded", 0.0), 2),
            "net_change": round(tm.get("net_change", 0.0), 2),
        },
        "monthly": [
            {"month": m, "income": round(monthly[m]["income"], 2),
             "expenses": round(monthly[m]["expenses"], 2),
             "excluded": round(monthly[m].get("excluded", 0.0), 2)}
            for m in months if m <= this_month
        ],
        "expense_categories": by_magnitude(expense_cats),
        "income_categories": by_magnitude(monthly_income.get(this_month, {})),
        "budgets": {
            "month": budget_month,
            "month_label": _month_label(budget_month),
            "items": budget_rows,
        },
        "accounts": {
            "assets": by_magnitude({a: account_total(balances, a, currency) for a in balances if a.startswith("assets:")}),
            "liabilities": by_magnitude({a: account_total(balances, a, currency) for a in balances if a.startswith("liabilities:")}),
        },
        "transactions": tx_view,
        "insights": insights,
    }
