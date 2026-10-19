"""Deterministic, offline categorisation from the user's own journal.

Why this exists: every suggestion used to come from a single remote AI call,
so one failure meant "No suggestion — choose a category." on every row. The
free Gemini tier allows 15 requests a minute and the importer fired one call
per transaction, so a 25-row statement exhausted the quota part-way through
and the rest of the statement arrived blank — every day, by design.

Nothing here touches the network. Everything is derived from the user's own
journal and merchant rules, so there are no baked-in merchant lists to go
stale: if an account or a spelling is in their ledger, it is in here.

Tiers, strongest first:
  1. the same description, categorised before   -> 0.97
  2. a description that overlaps strongly       -> 0.80-0.93
  3. a description naming one of their accounts -> 0.75

Tier 3 is the one that catches the case that prompted this: a statement line
literally called "Transport" against an account called expenses:transportation
needs no intelligence at all.
"""
import re
from collections import Counter, defaultdict
from typing import Optional

# Tokens that carry no categorising signal: card-statement furniture and the
# account prefixes themselves. Kept deliberately tiny — this is grammar, not a
# merchant list, so it does not go stale as spending changes.
STOPWORDS = {
    "THE", "AND", "FOR", "FROM", "PTE", "LTD", "LLC", "INC", "CO", "SG",
    "SGP", "SINGAPORE", "PAYMENT", "PURCHASE", "TRANSACTION", "CARD", "POS",
    "DEBIT", "CREDIT", "REF", "NO", "ID", "TXN",
    "EXPENSES", "INCOME", "ASSETS", "LIABILITIES", "EQUITY",
}

MIN_TOKEN = 3          # shorter tokens match too much to be evidence
MIN_STEM = 4           # prefix matching below this conflates unrelated words


def normalise(text: str) -> str:
    """Uppercase, drop punctuation and reference numbers, collapse spaces."""
    s = str(text or "").upper()
    s = re.sub(r"[^A-Z0-9 ]+", " ", s)
    s = re.sub(r"\b\d{2,}\b", " ", s)       # 4821, 20260105 — reference noise
    return re.sub(r"\s+", " ", s).strip()


def tokens(text: str) -> list[str]:
    return [
        t for t in normalise(text).split()
        if len(t) >= MIN_TOKEN and t not in STOPWORDS and not t.isdigit()
    ]


def _stem(word: str) -> str:
    """Fold plurals so "Utility" and "utilities" compare equal.

    Suffix rules only — no dictionary, so it stays correct for account names
    this code has never seen.
    """
    w = word.upper()
    if len(w) > 4 and w.endswith("IES"):
        return w[:-3] + "Y"
    if len(w) > 4 and w.endswith("ES") and not w.endswith("SES"):
        return w[:-2]
    if len(w) > 3 and w.endswith("S") and not w.endswith("SS"):
        return w[:-1]
    return w


def _stem_match(a: str, b: str) -> bool:
    """True when two words name the same thing.

    Plural-folded, then prefix-compared so TRANSPORT matches TRANSPORTATION.
    Requires MIN_STEM characters of agreement so CAR does not match CARPET.
    """
    a, b = _stem(a), _stem(b)
    if a == b:
        return True
    if len(a) < MIN_STEM or len(b) < MIN_STEM:
        return False
    return a.startswith(b) or b.startswith(a)


def account_terms(account: str) -> list[str]:
    """The words in an account name that could appear in a description.

    "expenses:transportation" -> ["TRANSPORTATION"]
    "expenses:food:dining-out" -> ["FOOD", "DINING", "OUT"]
    The leading type segment is dropped: every expense account starts with
    "expenses", so it distinguishes nothing.
    """
    parts = str(account or "").split(":")[1:]
    out: list[str] = []
    for part in parts:
        for word in re.split(r"[^A-Za-z0-9]+", part):
            word = word.upper()
            if len(word) >= MIN_TOKEN and word not in STOPWORDS:
                out.append(word)
    return out


def _leaf_and_parent_terms(account: str) -> tuple[list[str], list[str]]:
    """Split an account's words into its own name and its ancestors'.

    "expenses:food:dining-out" -> (["DINING", "OUT"], ["FOOD"])

    A statement line says "Dining Out", not "Food Dining Out", so only the
    leaf's own words can be required. The parent words still count as
    corroboration when they do appear.
    """
    parts = [p for p in str(account or "").split(":")[1:] if p]
    if not parts:
        return [], []
    leaf = account_terms("x:" + parts[-1])
    parents = account_terms("x:" + ":".join(parts[:-1])) if len(parts) > 1 else []
    return leaf, parents



class LocalCategoriser:
    """Suggests an account using only local evidence.

    Build it once per import: it reads the journal once and keeps the index.
    """

    def __init__(self, journal_text: str = "", accounts: Optional[list[str]] = None,
                 labels: Optional[dict[str, str]] = None):
        self.history: dict[str, Counter] = defaultdict(Counter)
        self.token_index: dict[str, Counter] = defaultdict(Counter)
        # The payee as actually written in the journal, per normalised key, so
        # the reason can quote what the user really filed rather than echoing
        # the incoming line. "GRAB *TRIP 9915" and "GRAB *TRIP 4821" share a
        # key; saying you filed 9915 before would be untrue.
        self.payees: dict[str, str] = {}
        self.accounts: list[str] = list(accounts or [])
        # label -> account, e.g. {"Transport": "expenses:transportation"} from
        # budgets.json. The user wrote these names themselves, so they are the
        # best synonyms available for their own accounts.
        self.labels: dict[str, str] = dict(labels or {})
        if journal_text:
            self._index_journal(journal_text)

    # -- indexing ----------------------------------------------------

    def _index_journal(self, text: str) -> None:
        """Walk the journal, pairing each payee with the accounts it posts to.

        Deliberately tolerant: a malformed block must not abort the index and
        leave the importer with no local evidence at all.
        """
        payee = None
        for line in text.splitlines():
            if re.match(r"\d{4}[-/]\d{2}[-/]\d{2}\s", line):
                payee = " ".join(line.split()[1:]).strip()
                # strip hledger's optional code/comment decorations
                payee = payee.split(";")[0].strip()
                continue
            if not payee:
                continue
            stripped = line.strip()
            if not stripped or stripped.startswith(";"):
                continue
            if not line[:1].isspace():
                payee = None
                continue
            account = re.split(r"\s{2,}", stripped)[0].strip()
            if ":" not in account:
                continue
            if not (account.startswith("expenses") or account.startswith("income")):
                continue
            key = normalise(payee)
            if not key:
                continue
            self.history[key][account] += 1
            self.payees.setdefault(key, payee)
            for tok in tokens(payee):
                self.token_index[tok][account] += 1

    # -- lookup ------------------------------------------------------

    def suggest(self, description: str, is_income: bool = False) -> Optional[tuple[str, float, str]]:
        """Return (account, confidence, reason) or None when nothing fits."""
        want = "income" if is_income else "expenses"

        def allowed(acct: str) -> bool:
            return acct.startswith(want)

        key = normalise(description)
        if not key:
            return None

        # 1. Seen this exact description before.
        hits = Counter({a: n for a, n in self.history.get(key, {}).items() if allowed(a)})
        if hits:
            account, count = hits.most_common(1)[0]
            times = "once" if count == 1 else f"{count} times"
            filed = self.payees.get(key, description)
            if normalise(filed) == key and filed.strip() != str(description).strip():
                # Same merchant, different reference number on the line.
                return account, 0.97, f"you filed “{filed}” here {times} before"
            return account, 0.97, f"you filed “{description}” here {times} before"

        # 2. Shares distinctive words with descriptions seen before. Scored by
        #    the share of this description's words that point at one account,
        #    so a single incidental word cannot carry a suggestion.
        desc_tokens = tokens(description)
        if desc_tokens:
            scores: Counter = Counter()
            for tok in desc_tokens:
                for account, n in self.token_index.get(tok, {}).items():
                    if allowed(account):
                        scores[account] += n
            if scores:
                account, _ = scores.most_common(1)[0]
                matched = [t for t in desc_tokens if account in self.token_index.get(t, {})]
                share = len(matched) / len(desc_tokens)
                if share >= 0.5:
                    conf = min(0.93, 0.80 + 0.13 * share)
                    word = ", ".join(w.title() for w in matched[:3])
                    return account, round(conf, 2), f"past transactions mentioning {word} go here"

        # 3. The description names one of their own accounts or budget labels.
        #    "Transport" -> expenses:transportation.
        match = self._match_account_name(desc_tokens, allowed)
        if match:
            account, via = match
            return account, 0.75, f"“{via}” matches your {account}"

        return None

    def _match_account_name(self, desc_tokens, allowed) -> Optional[tuple[str, str]]:
        if not desc_tokens:
            return None

        # User-written budget labels first: they are explicit synonyms.
        for label, account in self.labels.items():
            if not allowed(account):
                continue
            label_tokens = [t for t in tokens(label)]
            if label_tokens and all(
                any(_stem_match(d, l) for d in desc_tokens) for l in label_tokens
            ):
                return account, label

        best: Optional[tuple[tuple, str, str]] = None
        for account in self.accounts:
            if not allowed(account):
                continue
            leaf, parents = _leaf_and_parent_terms(account)
            if not leaf:
                continue
            matched = [t for t in leaf if any(_stem_match(d, t) for d in desc_tokens)]
            if len(matched) != len(leaf):
                continue  # every word of the account's own name must be present
            parent_hits = [t for t in parents if any(_stem_match(d, t) for d in desc_tokens)]
            # Rank: more of the account's own words first, then corroborating
            # parent words, then longer names — so "dining out" beats "food"
            # and a deeper account beats the category above it.
            score = (len(matched), len(parent_hits), sum(len(t) for t in matched))
            if best is None or score > best[0]:
                best = (score, account, " ".join(w.title() for w in matched))
        if best:
            return best[1], best[2]
        return None
