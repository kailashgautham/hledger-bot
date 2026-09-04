"""Build the deterministic dashboard dataset from a hledger journal."""
import hashlib
import re
from collections import defaultdict
from datetime import date

from balances import account_balances, account_total, parse_amount

_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")
_STATUS_RE = re.compile(r"^[!*]\s*")


def _tx_id(header_line: str) -> str:
    clean = re.sub(r"\s+; orig:.*$", "", header_line.rstrip("\n"))
    return hashlib.sha256(clean.encode()).hexdigest()[:16]

BUDGETS: dict[str, float] = {
    "expenses:food": 200,
    "expenses:fitness": 90,
    "expenses:subscriptions": 20,
    "expenses:entertainment": 50,
    "expenses:transportation": 80,
}
OTHERS_BUDGET = 60.0
OTHERS_ACCOUNT = "expenses:other"
OTHERS_EXCLUDE = {"expenses:taxes", "expenses:donation"}
LABELS: dict[str, str] = {
    "expenses:food": "Food",
    "expenses:fitness": "Fitness",
    "expenses:subscriptions": "Subscriptions",
    "expenses:entertainment": "Entertainment",
    "expenses:transportation": "Transport",
}


def parse_transactions(text: str) -> list[dict]:
    """Return [{date, description, postings:[{account, amount}]}]."""
    txns: list[dict] = []
    current = None
    for line in text.splitlines():
        m = _DATE_RE.match(line)
        if m:
            current = {
                "date": m.group(1),
                "description": line[m.end():].split(";")[0].strip(),
                "postings": [],
                "_header": line,
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


def _fmt(v: float) -> str:
    return f"SGD {v:,.2f}"


def compute_insights(monthly, monthly_cats, tx_view, this_month) -> dict:
    """Deterministic monthly insights: overspending flags + positive signals."""
    today = date.today()
    mkeys = sorted(m for m in monthly
                   if monthly[m]["expenses"] > 0 and m <= this_month)
    if not mkeys:
        return {"month": None, "cards": []}

    target = mkeys[-1]
    inc, exp = monthly[target]["income"], monthly[target]["expenses"]
    days = (max(1, min(today.day, _days_in_month(target)))
            if target == this_month else _days_in_month(target))
    daily = exp / days

    cats = sorted(monthly_cats[target].items(), key=lambda kv: -kv[1])
    top_acct, top_amt = (cats[0] if cats else (None, 0.0))
    prior = [m for m in mkeys if m < target]

    cards = []

    if inc > 0:
        saved, rate = inc - exp, (inc - exp) / inc
        if rate >= 0.20:
            cards.append({"tone": "good",
                          "title": "Strong month — you kept 20%+ of your income",
                          "body": f"You saved {_fmt(saved)} of {_fmt(inc)} earned in {_month_label(target)} ({rate:.0%}). Great momentum."})
        elif rate >= 0.05:
            cards.append({"tone": "good",
                          "title": "Positive savings this month",
                          "body": f"You saved {_fmt(saved)} of {_fmt(inc)} ({rate:.0%}) in {_month_label(target)}."})
        elif rate >= 0:
            cards.append({"tone": "info",
                          "title": "Savings are thin",
                          "body": f"Only {_fmt(saved)} ({rate:.0%}) left over in {_month_label(target)}. Consider trimming a category."})
        else:
            cards.append({"tone": "bad",
                          "title": "Spending exceeded income",
                          "body": f"You spent {_fmt(exp)} against {_fmt(inc)} earned ({rate:.0%}). Cash balance is shrinking."})

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
                          "body": f"{_fmt(exp)} this month vs {_fmt(prev)} ({'+' if delta > 0 else ''}{_fmt(delta)})."})
    else:
        cards.append({"tone": "info",
                      "title": "First full month tracked",
                      "body": f"{_month_label(target)} is your baseline. Month-over-month trends will appear from next month."})

    if top_acct:
        cards.append({"tone": "info",
                      "title": f"Top category: {top_acct.replace('expenses:', '')}",
                      "body": f"{top_acct.replace('expenses:', '')} was {top_amt / exp:.0%} of spend ({_fmt(top_amt)}) in {_month_label(target)}."})

    for acct, amt in cats:
        if amt < 25:
            continue
        hist = [monthly_cats.get(m, {}).get(acct, 0.0) for m in prior]
        if len(hist) >= 1 and max(hist) > 0:
            avg = sum(hist) / len(hist)
            if amt > avg * 1.5 and amt - avg >= 20:
                cards.append({"tone": "warn",
                              "title": f"Overspending on {acct.replace('expenses:', '')}",
                              "body": f"{_fmt(amt)} vs your usual {_fmt(avg)} — {((amt - avg) / avg):.0%} more than normal. This is the main thing to watch."})

    exps = [t for t in tx_view if t["type"] == "expense" and t["amount"] < 0 and t["date"].startswith(target)]
    if exps:
        biggest = max(exps, key=lambda t: abs(t["amount"]))
        cards.append({"tone": "info",
                      "title": "Biggest purchase",
                      "body": f"{biggest['description']} — {_fmt(abs(biggest['amount']))} on {biggest['date']}."})

    return {
        "month": target,
        "month_label": _month_label(target),
        "spent": round(exp, 2),
        "daily_avg": round(daily, 2),
        "count": len(exps),
        "cards": cards,
    }


def build_data(text: str, currency: str = "SGD") -> dict:
    balances = account_balances(text)
    txns = parse_transactions(text)

    assets = sum(account_total(balances, a, currency) for a in balances if a.startswith("assets:"))
    liabilities = sum(account_total(balances, a, currency) for a in balances if a.startswith("liabilities:"))

    monthly: dict[str, dict] = defaultdict(lambda: {"income": 0.0, "expenses": 0.0})
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
        expense = sum(p["amount"] for p in postings if p["account"].startswith("expenses:"))
        ttype, amt = _type_amount(postings)
        tx_view.append({
            "tx_id": _tx_id(t["_header"]),
            "date": t["date"],
            "description": t["description"],
            "type": ttype,
            "amount": round(amt, 2),
            "accounts": [p["account"] for p in t["postings"]],
        })
        month = t["date"][:7]
        monthly[month]["income"] += income
        monthly[month]["expenses"] += expense
        for p in postings:
            if not p["amount"]:
                continue
            if p["account"].startswith("expenses:"):
                expense_cats[p["account"]] += p["amount"]
                monthly_cats[month][p["account"]] += p["amount"]
            elif p["account"].startswith("income:"):
                monthly_income[month][p["account"]] += abs(p["amount"])

    tx_view.sort(key=lambda t: t["date"], reverse=True)
    months = sorted(monthly)
    today = date.today().isoformat()
    this_month = today[:7]
    tm = monthly.get(this_month, {"income": 0.0, "expenses": 0.0})

    insights = compute_insights(monthly, {m: dict(c) for m, c in monthly_cats.items()},
                                tx_view, this_month)
    budget_month = this_month
    budget_cats = {a: v for a, v in monthly_cats.get(budget_month, {}).items() if v > 0}
    budget_rows = []
    for acct, spent in sorted(BUDGETS.items(), key=lambda kv: -budget_cats.get(kv[0], 0.0)):
        budget = BUDGETS[acct]
        spent = budget_cats.get(acct, 0.0)
        budget_rows.append({
            "account": acct,
            "label": LABELS.get(acct, acct.replace("expenses:", "").title()),
            "spent": round(spent, 2),
            "budget": budget,
            "pct": round(spent / budget * 100, 1),
            "over": spent > budget,
            "warn": not (spent > budget) and spent >= budget * 0.75,
            "members": [acct],
        })
    others = {a: v for a, v in budget_cats.items() if a not in BUDGETS and a not in OTHERS_EXCLUDE}
    others_total = sum(others.values())
    budget_rows.append({
        "account": OTHERS_ACCOUNT,
        "label": "Other",
        "spent": round(others_total, 2),
        "budget": OTHERS_BUDGET,
        "pct": round(others_total / OTHERS_BUDGET * 100, 1),
        "over": others_total > OTHERS_BUDGET,
        "warn": not (others_total > OTHERS_BUDGET) and others_total >= OTHERS_BUDGET * 0.75,
        "members": sorted(others.keys()),
    })

    def by_magnitude(items: dict[str, float]) -> list[dict]:
        return [{"account": a, "amount": round(v, 2)} for a, v in
                sorted(items.items(), key=lambda kv: -kv[1])]

    insights = compute_insights(monthly, {m: dict(c) for m, c in monthly_cats.items()},
                                tx_view, this_month)

    return {
        "generated_at": today,
        "currency": currency,
        "as_of": today,
        "net_worth": round(assets + liabilities, 2),
        "assets": round(assets, 2),
        "liabilities": round(liabilities, 2),
        "month": {
            "key": this_month,
            "income": round(tm["income"], 2),
            "expenses": round(tm["expenses"], 2),
        },
        "monthly": [
            {"month": m, "income": round(monthly[m]["income"], 2),
             "expenses": round(monthly[m]["expenses"], 2)} for m in months if m <= this_month
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
