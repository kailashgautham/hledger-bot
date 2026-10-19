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
  4. a description naming an everyday thing     -> 0.70

Tier 3 catches a line called "Transport" against expenses:transportation. Tier
4 catches "Cheese" and "Coffee", which name no account and may never have been
bought before, but obviously belong somewhere. It is a vocabulary of ordinary
nouns, not merchants or brands — those go stale and belong in merchant_map.json
instead — and it only ever resolves to an account the user already has.
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

# Everyday words -> the kind of account they belong to, with the account-name
# words that identify such an account in someone's chart of accounts.
#
# Deliberately generic nouns only. No brands and no merchants: those change,
# are personal, and already have a home in merchant_map.json, which is learned
# rather than guessed. These are words whose meaning does not drift — cheese is
# food this year and next.
#
# Order matters: the first concept whose words appear wins, and within a
# concept the account hints are tried in order, so a ledger with both
# expenses:groceries and expenses:food sends cheese to groceries and coffee to
# food. A concept is skipped entirely when the user has no matching account —
# nothing here invents one.
VOCABULARY: list[tuple[frozenset, tuple]] = [
    (frozenset({
        "GROCERY", "GROCERIES", "SUPERMARKET", "PROVISIONS", "CHEESE", "MILK",
        "BREAD", "EGGS", "EGG", "RICE", "FLOUR", "SUGAR", "BUTTER", "YOGURT",
        "VEGETABLE", "VEGETABLES", "VEGGIES", "FRUIT", "FRUITS", "MEAT",
        "CHICKEN", "BEEF", "PORK", "FISH", "SEAFOOD", "PASTA", "CEREAL",
        "SNACKS", "PANTRY",
    }), ("GROCERY", "GROCERIES", "SUPERMARKET", "FOOD")),
    (frozenset({
        "COFFEE", "KOPI", "TEA", "LATTE", "ESPRESSO", "CAPPUCCINO", "CAFE",
        "BREAKFAST", "BRUNCH", "LUNCH", "DINNER", "SUPPER", "MEAL", "MEALS",
        "RESTAURANT", "EATERY", "HAWKER", "KOPITIAM", "CANTEEN", "FOODCOURT",
        "BAKERY", "PASTRY", "CAKE", "DESSERT", "ICECREAM", "PIZZA", "BURGER",
        "SUSHI", "RAMEN", "NOODLES", "SANDWICH", "SALAD", "TAKEAWAY", "TAKEOUT",
        "DELIVERY", "SNACK", "DRINKS", "JUICE", "SMOOTHIE", "BUBBLETEA",
    }), ("DINING", "RESTAURANT", "EATING", "CAFE", "COFFEE", "FOOD")),
    (frozenset({
        "TAXI", "CAB", "RIDE", "FARE", "BUS", "TRAIN", "MRT", "SUBWAY",
        "METRO", "TRANSIT", "TRANSPORT", "COMMUTE", "PETROL", "GASOLINE",
        "FUEL", "DIESEL", "PARKING", "TOLL", "ERP", "CARPARK",
    }), ("TRANSPORT", "TRANSPORTATION", "TRAVEL", "COMMUTE")),
    (frozenset({
        "PHARMACY", "CHEMIST", "CLINIC", "DOCTOR", "DENTAL", "DENTIST",
        "MEDICINE", "MEDICAL", "HOSPITAL", "PRESCRIPTION", "VACCINE",
        "OPTICAL", "OPTOMETRIST", "PHYSIO", "THERAPY",
    }), ("HEALTH", "HEALTHCARE", "MEDICAL", "PHARMACY")),
    (frozenset({
        "GYM", "YOGA", "PILATES", "CLIMBING", "SWIM", "SWIMMING", "WORKOUT",
        "TRAINER", "FITNESS",
    }), ("FITNESS", "GYM", "SPORT", "SPORTS", "HEALTH")),
    (frozenset({
        "SUBSCRIPTION", "SUBSCRIPTIONS", "STREAMING", "MEMBERSHIP", "RENEWAL",
    }), ("SUBSCRIPTION", "SUBSCRIPTIONS", "MEMBERSHIP")),
    (frozenset({
        "ELECTRICITY", "WATER", "INTERNET", "WIFI", "BROADBAND", "MOBILE",
        "TELCO", "UTILITY", "UTILITIES", "BILL", "BILLS",
    }), ("UTILITY", "UTILITIES", "BILLS")),
    (frozenset({
        "MOVIE", "MOVIES", "CINEMA", "CONCERT", "THEATRE", "THEATER", "GIG",
        "SHOW", "GAME", "GAMES", "GAMING", "ARCADE",
    }), ("ENTERTAINMENT", "LEISURE", "FUN")),
    (frozenset({
        "CLOTHES", "CLOTHING", "APPAREL", "SHIRT", "SHOES", "SNEAKERS",
        "DRESS", "JACKET", "JEANS", "SHOPPING",
    }), ("SHOPPING", "CLOTHING", "CLOTHES", "APPAREL")),
    (frozenset({
        "HOTEL", "HOSTEL", "AIRBNB", "FLIGHT", "FLIGHTS", "AIRFARE",
        "AIRLINE", "LUGGAGE", "VISA", "HOLIDAY", "VACATION",
    }), ("TRAVEL", "HOLIDAY", "VACATION", "TRIPS")),
    (frozenset({
        "HAIRCUT", "BARBER", "SALON", "SPA", "MASSAGE", "NAILS", "GROOMING",
        "SKINCARE", "COSMETICS", "TOILETRIES",
    }), ("GROOMING", "PERSONAL", "SELFCARE", "BEAUTY")),
    (frozenset({
        "VET", "VETERINARY", "PETFOOD", "KIBBLE", "LITTER",
    }), ("PET", "PETS")),
    (frozenset({
        "FURNITURE", "HARDWARE", "REPAIR", "PLUMBER", "ELECTRICIAN",
        "CLEANING", "LAUNDRY", "HOUSEHOLD",
    }), ("HOME", "HOUSEHOLD", "HOUSE", "MAINTENANCE")),
    (frozenset({
        "COURSE", "TUITION", "TEXTBOOK", "TEXTBOOKS", "SEMINAR", "WORKSHOP",
        "CERTIFICATION",
    }), ("EDUCATION", "LEARNING", "COURSES", "TUITION")),
]

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
            for tok in tokens(payee):
                self.token_index[tok][account] += 1

    # -- lookup ------------------------------------------------------

    def suggest(self, description: str, is_income: bool = False) -> Optional[tuple[str, float]]:
        """Return (account, confidence) or None when nothing fits."""
        want = "income" if is_income else "expenses"

        def allowed(acct: str) -> bool:
            return acct.startswith(want)

        key = normalise(description)
        if not key:
            return None

        # 1. Seen this exact description before.
        hits = Counter({a: n for a, n in self.history.get(key, {}).items() if allowed(a)})
        if hits:
            return hits.most_common(1)[0][0], 0.97

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
                    return account, round(min(0.93, 0.80 + 0.13 * share), 2)

        # 3. The description names one of their own accounts or budget labels.
        #    "Transport" -> expenses:transportation.
        account = self._match_account_name(desc_tokens, allowed)
        if account:
            return account, 0.75

        # 4. The description names an everyday thing. "Cheese" names no
        #    account and may never have been bought before, but it is food.
        account = self._match_vocabulary(desc_tokens, allowed)
        if account:
            return account, 0.70

        return None

    def _match_vocabulary(self, desc_tokens, allowed) -> Optional[str]:
        """Resolve an everyday noun to whichever of their accounts fits it."""
        if not desc_tokens:
            return None
        for words, hints in VOCABULARY:
            if not any(_stem(t) in {_stem(w) for w in words} for t in desc_tokens):
                continue
            account = self._account_for_hints(hints, allowed)
            if account:
                return account
        return None

    def _account_for_hints(self, hints, allowed) -> Optional[str]:
        """The user's account that best answers to one of these name hints.

        Ranked shallowest first, then by hint order. Shallowest because a
        suggestion should be the general category the user can refine with one
        tap, not a sub-bucket guessed on their behalf: a ledger with both
        expenses:food and expenses:food:dining-out should offer food. Hint
        order breaks ties, so a ledger with both groceries and food sends
        cheese to groceries and coffee to food.

        Returns None when they have no such account — this never invents one.
        """
        best: Optional[tuple[int, int, str]] = None
        for rank, hint in enumerate(hints):
            for account in self.accounts:
                if not allowed(account):
                    continue
                leaf, _ = _leaf_and_parent_terms(account)
                if not any(_stem_match(term, hint) for term in leaf):
                    continue
                key = (account.count(":"), rank, account)
                if best is None or key < best:
                    best = key
        return best[2] if best else None

    def _match_account_name(self, desc_tokens, allowed) -> Optional[str]:
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
