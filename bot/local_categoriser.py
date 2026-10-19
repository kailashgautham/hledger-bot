"""Categorisation learned from the journal, with no built-in knowledge.

There is nothing domain-specific in this file: no merchants, no brands, no
vocabulary of groceries or transport, no invented thresholds about what things
cost. Every judgement comes from the ledger it is given. If the ledger changes,
this changes with it; if someone else's ledger uses entirely different account
names in a different language, it works there unaltered.

What it can do, strongest first:
  1. the same payee, categorised before          -> 0.97
  2. a payee sharing words with past payees       -> 0.80-0.93, weighted by
     how reliably each word has predicted an account in the past
  3. a payee naming one of the accounts itself    -> 0.75

What it deliberately cannot do is recognise a merchant it has never seen and
whose name says nothing — "Tesco", "Uniqlo". Nothing local can: the name
carries no information this ledger has ever observed. That is the AI's job, and
when the AI cannot answer the import says so rather than leaving a blank row.

Word weighting replaces what would otherwise be a stopword list. A word like
"SINGAPORE" or "PAYMENT" appears across many different accounts, so it predicts
nothing and is weighted to near zero on the evidence. A word like "FITNESS"
appears overwhelmingly against one account, so it predicts strongly. No list
decides which is which — the journal does, and it stays correct for words this
code has never heard of.
"""
import re
from collections import Counter, defaultdict
from typing import Optional

MIN_TOKEN = 3          # shorter runs of letters match too much to be evidence
MIN_STEM = 4           # prefix matching below this conflates unrelated words


def normalise(text: str) -> str:
    """Uppercase, drop punctuation and reference numbers, collapse spaces."""
    s = str(text or "").upper()
    s = re.sub(r"[^A-Z0-9 ]+", " ", s)
    s = re.sub(r"\b\d{2,}\b", " ", s)       # 4821, 20260105 — reference noise
    return re.sub(r"\s+", " ", s).strip()


def tokens(text: str) -> list[str]:
    """The words of a description that could carry meaning.

    Structural filtering only — length and digits. Which words actually matter
    is decided from the journal, not here.
    """
    return [
        t for t in normalise(text).split()
        if len(t) >= MIN_TOKEN and not t.isdigit()
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
    The leading segment is dropped because hledger's account tree uses it for
    the account's type, so it is shared by every account of that type and
    distinguishes none of them.
    """
    parts = str(account or "").split(":")[1:]
    out: list[str] = []
    for part in parts:
        for word in re.split(r"[^A-Za-z0-9]+", part):
            word = word.upper()
            if len(word) >= MIN_TOKEN:
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
    """Suggests an account using only evidence from this ledger.

    Build it once per import: it reads the journal once and keeps the index.
    """

    def __init__(self, journal_text: str = "", accounts: Optional[list[str]] = None,
                 labels: Optional[dict[str, str]] = None):
        self.history: dict[str, Counter] = defaultdict(Counter)
        self.token_index: dict[str, Counter] = defaultdict(Counter)
        self.accounts: list[str] = list(accounts or [])
        # label -> account, e.g. {"Transport": "expenses:transportation"} from
        # budgets.json. The user wrote these names for their own accounts, so
        # they are the best synonyms available — and they come from a file, not
        # from this source.
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
            for tok in set(tokens(payee)):
                self.token_index[tok][account] += 1

    def _weight(self, token: str) -> float:
        """How reliably this word has predicted a single account, 0 to 1.

        The share of the word's postings going to its most common account.
        A word spread evenly over ten accounts scores 0.1 and contributes
        almost nothing; a word that has only ever appeared against one account
        scores 1.0. This is what a stopword list would otherwise approximate,
        except it is measured rather than guessed, and it covers words no list
        would have thought to include.
        """
        counts = self.token_index.get(token)
        if not counts:
            return 0.0
        total = sum(counts.values())
        return counts.most_common(1)[0][1] / total if total else 0.0

    # -- lookup ------------------------------------------------------

    def suggest(self, description: str, is_income: bool = False) -> Optional[tuple[str, float]]:
        """Return (account, confidence) or None when the ledger says nothing."""
        want = "income" if is_income else "expenses"

        def allowed(acct: str) -> bool:
            return acct.startswith(want)

        key = normalise(description)
        if not key:
            return None

        # 1. Seen this exact payee before.
        hits = Counter({a: n for a, n in self.history.get(key, {}).items() if allowed(a)})
        if hits:
            return hits.most_common(1)[0][0], 0.97

        # 2. Shares words with payees seen before, each word counting for as
        #    much as it has historically been worth and no more.
        desc_tokens = tokens(description)
        if desc_tokens:
            scores: Counter = Counter()
            evidence = 0.0
            for tok in set(desc_tokens):
                counts = self.token_index.get(tok)
                if not counts:
                    continue
                total = sum(counts.values())
                weight = self._weight(tok)
                if weight <= 0:
                    continue
                evidence += weight
                for account, n in counts.items():
                    if allowed(account):
                        scores[account] += (n / total) * weight
            if scores and evidence > 0:
                account, score = scores.most_common(1)[0]
                # How much of the available evidence points at this one account.
                agreement = score / evidence
                if agreement >= 0.5:
                    return account, round(min(0.93, 0.80 + 0.13 * agreement), 2)

        # 3. The payee names one of the accounts, or a label given to one.
        account = self._match_account_name(desc_tokens, allowed)
        if account:
            return account, 0.75

        return None

    def _match_account_name(self, desc_tokens, allowed) -> Optional[str]:
        if not desc_tokens:
            return None

        # Labels first: an explicit synonym the user wrote for this account.
        for label, account in self.labels.items():
            if not allowed(account):
                continue
            label_tokens = tokens(label)
            if label_tokens and all(
                any(_stem_match(d, l) for d in desc_tokens) for l in label_tokens
            ):
                return account

        best: Optional[tuple[tuple, str]] = None
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
                best = (score, account)
        if best:
            return best[1]
        return None
