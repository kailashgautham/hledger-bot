import json
import logging
import os
import re
from typing import Optional

logger = logging.getLogger(__name__)


def _resolve_gemini_model(genai, configured: str) -> str:
    """Pick a model that exists, rather than trusting a name in a config file.

    A pinned model name is a time bomb: providers retire them, and when one goes
    every call fails with a 404 that the caller cannot distinguish from a bad
    key. The configured name is honoured when the API still lists it; otherwise
    the API's own list decides, so this keeps working after a retirement without
    anyone editing config.yaml.
    """
    try:
        usable = [
            m.name.split("/")[-1]
            for m in genai.list_models()
            if "generateContent" in getattr(m, "supported_generation_methods", [])
        ]
    except Exception as exc:                       # pragma: no cover - network
        logger.warning("Could not list models (%s); trying %r", exc, configured)
        return configured
    if not usable:
        return ""
    if configured and configured in usable:
        return configured
    if configured:
        logger.warning("Model %r is not available; choosing from %d offered",
                       configured, len(usable))
    # Prefer the provider's small, high-quota tier when it offers one, since
    # this runs per statement on a free key; otherwise take what is offered.
    for name in sorted(usable):
        if "flash" in name and "thinking" not in name:
            return name
    return sorted(usable)[0]


def _resolve_groq_model(client, configured: str = "") -> str:
    """The same for Groq: ask what exists instead of trusting a pinned name.

    Groq retires models on a rolling basis. A name that worked when config.yaml
    was written stops existing, and every request then fails with an error the
    caller swallowed — which is exactly what happened here.
    """
    try:
        names = sorted(m.id for m in client.models.list().data)
    except Exception as exc:                       # pragma: no cover - network
        logger.warning("Could not list Groq models (%s); trying %r", exc, configured)
        return configured
    if not names:
        return ""
    if configured and configured in names:
        return configured
    if configured:
        logger.warning("Groq model %r is no longer offered; choosing from %d available",
                       configured, len(names))
    # Prefer a small, high-throughput chat model on a free key; skip anything
    # that is plainly not a chat model.
    chat = [n for n in names if not any(
        k in n.lower() for k in ("whisper", "tts", "guard", "embed", "vision"))]
    for name in chat:
        if "instant" in name or "8b" in name or "mini" in name:
            return name
    return (chat or names)[0]


class Categoriser:
    """Suggests hledger accounts for transactions using a free AI provider."""

    def __init__(self, config: dict):
        ai = config.get("ai", {})
        self.provider = ai.get("provider", "none")
        self.model_name = ai.get("model", "")
        self.confidence_threshold = ai.get("confidence_threshold", 0.85)
        # Why the AI produced nothing, in the provider's own words. Failures
        # used to go only to a container log, so a dead key, a retired model or
        # an exhausted quota all looked identical from the outside: blank rows,
        # no reason, nothing to act on.
        self.last_error: Optional[str] = None
        self._client = self._init_client()

    def status(self, probe: bool = False) -> dict:
        """Whether the AI actually works, and if not, exactly why.

        `available` only means a client object exists — for most SDKs that is
        purely local and proves nothing, which made an earlier version of this
        endpoint report a healthy AI that had never successfully answered.
        `probe` therefore makes a real request and reports what came back.
        """
        out = {
            "provider": self.provider,
            "model": self.model_name or None,
            "configured": self.available,
            "last_error": self.last_error,
        }
        if not probe:
            return out
        if not self.available:
            out["working"] = False
            out["reason"] = self.last_error or "no AI client could be created"
            return out
        answers = self.suggest_many(
            [{"description": "COFFEE SHOP", "amount": -4.5}],
            "SGD", ["expenses:food", "expenses:groceries"], [],
        )
        out["working"] = bool(answers)
        out["model"] = self.model_name or None
        # Re-read after the probe, so a stale error from start-up can never sit
        # next to a fresh result and contradict it.
        out["last_error"] = self.last_error
        out["reason"] = None if answers else (self.last_error or "no answer returned")
        if answers:
            out["answered"] = list(answers.get(0))
        return out


    def _init_client(self):
        if self.provider == "gemini":
            try:
                import google.generativeai as genai  # type: ignore

                api_key = os.environ.get("GOOGLE_API_KEY")
                if not api_key:
                    self.last_error = "GOOGLE_API_KEY is not set in this container"
                    logger.warning("GOOGLE_API_KEY not set; AI categorisation disabled")
                    return None
                genai.configure(api_key=api_key)
                self.model_name = _resolve_gemini_model(genai, self.model_name)
                if not self.model_name:
                    self.last_error = (
                        "the API key works but offers no model that can generate "
                        "content — check the key's project and quota"
                    )
                    return None
                return genai.GenerativeModel(self.model_name)
            except ImportError:
                self.last_error = "google-generativeai is not installed"
                logger.warning("google-generativeai not installed")
                return None

        if self.provider == "groq":
            try:
                from groq import Groq  # type: ignore

                api_key = os.environ.get("GROQ_API_KEY")
                if not api_key:
                    self.last_error = "GROQ_API_KEY is not set in this container"
                    logger.warning("GROQ_API_KEY not set; AI categorisation disabled")
                    return None
                client = Groq(api_key=api_key)
                self.model_name = _resolve_groq_model(client, self.model_name)
                if not self.model_name:
                    self.last_error = (
                        "the API key works but Groq offers no usable chat model"
                    )
                    return None
                return client
            except ImportError:
                self.last_error = "groq is not installed"
                logger.warning("groq not installed")
                return None

        if self.provider not in ("gemini", "groq"):
            self.last_error = f"no AI provider configured (ai.provider is {self.provider!r})"
        return None

    @property
    def available(self) -> bool:
        return self._client is not None

    def suggest_category(
        self,
        description: str,
        amount: float,
        currency: str,
        accounts: list[str],
        examples: list[dict],
    ) -> Optional[tuple[str, float]]:
        """Return (account, confidence) or None if AI is unavailable."""
        if not self._client:
            return None

        prompt = self._build_prompt(description, amount, currency, accounts, examples)
        try:
            raw = self._call(prompt)
            return self._parse_response(raw)
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            logger.error("AI categorisation failed: %s", exc)
            return None

    def _build_prompt(
        self,
        description: str,
        amount: float,
        currency: str,
        accounts: list[str],
        examples: list[dict],
    ) -> str:
        accounts_str = "\n".join(f"  - {a}" for a in accounts) or "  (none yet)"
        examples_str = (
            "\n".join(f"  {e['description']} → {e['account']}" for e in examples[:6])
            or "  (none yet)"
        )
        return f"""You are a personal finance assistant helping categorise credit card transactions into hledger accounts.

Available accounts:
{accounts_str}

Recent examples from the user's journal:
{examples_str}

Transaction to categorise:
  Description: {description}
  Amount: {currency} {amount:.2f}

Respond with JSON only (no markdown fences):
{{"account": "expenses:...", "confidence": 0.95, "reasoning": "one line"}}

Rules:
- Pick the single most appropriate account from the list above. If none fits, suggest a new one using the same naming style.
- confidence: 0.9+ = very certain, 0.7-0.89 = fairly certain, <0.7 = uncertain."""

    def _call(self, prompt: str) -> str:
        if self.provider == "gemini":
            response = self._client.generate_content(prompt)
            return response.text

        if self.provider == "groq":
            response = self._client.chat.completions.create(
                model=self.model_name,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
            )
            return response.choices[0].message.content

        raise RuntimeError("No AI provider configured")

    def suggest_many(
        self,
        items: list[dict],
        currency: str,
        accounts: list[str],
        examples: list[dict],
    ) -> dict[int, tuple[str, float]]:
        """Categorise a whole statement in ONE request.

        The importer used to call suggest_category once per transaction. The
        free Gemini tier allows 15 requests a minute, so a 25-row statement
        exhausted the quota part-way through and every remaining row came back
        with no suggestion at all. One request per statement instead of one per
        row keeps a normal import inside any provider's limit.

        Returns {index: (account, confidence)} for whatever came back; indexes
        it could not answer are simply absent, so the caller keeps its own
        fallback for those rather than being handed a wrong account.
        """
        if not self._client or not items:
            return {}
        prompt = self._build_batch_prompt(items, currency, accounts, examples)
        try:
            raw = self._call(prompt)
            answers = self._parse_batch(raw, len(items))
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            logger.error("AI batch categorisation failed: %s", exc)
            return {}
        if not answers:
            self.last_error = "the AI replied, but not with usable JSON"
        else:
            self.last_error = None
        return answers

    def _build_batch_prompt(
        self,
        items: list[dict],
        currency: str,
        accounts: list[str],
        examples: list[dict],
    ) -> str:
        accounts_str = "\n".join(f"  - {a}" for a in accounts) or "  (none yet)"
        examples_str = (
            "\n".join(f"  {e['description']} → {e['account']}" for e in examples[:12])
            or "  (none yet)"
        )
        lines = "\n".join(
            f'  {i}: "{it.get("description", "")}" — {currency} {float(it.get("amount", 0)):.2f}'
            f'{" (income)" if it.get("type") == "income" else ""}'
            for i, it in enumerate(items)
        )
        return f"""You are a personal finance assistant categorising credit card transactions into hledger accounts.

Available accounts:
{accounts_str}

Recent examples from the user's journal:
{examples_str}

Transactions to categorise (index: description — amount):
{lines}

Respond with JSON only, no markdown fences: an object keyed by the index as a
string, each value {{"account": "expenses:...", "confidence": 0.95}}.
Include every index. Example: {{"0": {{"account": "expenses:food", "confidence": 0.9}}}}

Rules:
- Pick the single most appropriate account from the list above. If none fits, suggest a new one using the same naming style.
- Income transactions must get an income:... account; everything else expenses:...
- confidence: 0.9+ = very certain, 0.7-0.89 = fairly certain, <0.7 = uncertain."""

    def _parse_batch(self, raw: str, count: int) -> dict[int, tuple[str, float]]:
        text = raw.strip()
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        data = json.loads(text)
        if not isinstance(data, dict):
            return {}
        out: dict[int, tuple[str, float]] = {}
        for key, value in data.items():
            try:
                idx = int(key)
            except (TypeError, ValueError):
                continue
            if not (0 <= idx < count) or not isinstance(value, dict):
                continue
            account = str(value.get("account", "")).strip()
            if not account:
                continue
            try:
                confidence = float(value.get("confidence", 0.0))
            except (TypeError, ValueError):
                confidence = 0.0
            out[idx] = (account, confidence)
        return out

    def _parse_response(self, raw: str) -> Optional[tuple[str, float]]:
        text = raw.strip()
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        data = json.loads(text)
        account = data.get("account", "").strip()
        confidence = float(data.get("confidence", 0.0))
        if not account:
            return None
        return account, confidence
