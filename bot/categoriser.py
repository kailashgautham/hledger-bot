import json
import logging
import os
import re
from typing import Optional

logger = logging.getLogger(__name__)


class Categoriser:
    """Suggests hledger accounts for transactions using a free AI provider."""

    def __init__(self, config: dict):
        ai = config.get("ai", {})
        self.provider = ai.get("provider", "none")
        self.model_name = ai.get("model", "")
        self.confidence_threshold = ai.get("confidence_threshold", 0.85)
        self._client = self._init_client()

    def _init_client(self):
        if self.provider == "gemini":
            try:
                import google.generativeai as genai  # type: ignore

                api_key = os.environ.get("GOOGLE_API_KEY")
                if not api_key:
                    logger.warning("GOOGLE_API_KEY not set; AI categorisation disabled")
                    return None
                genai.configure(api_key=api_key)
                return genai.GenerativeModel(self.model_name or "gemini-1.5-flash")
            except ImportError:
                logger.warning("google-generativeai not installed")
                return None

        if self.provider == "groq":
            try:
                from groq import Groq  # type: ignore

                api_key = os.environ.get("GROQ_API_KEY")
                if not api_key:
                    logger.warning("GROQ_API_KEY not set; AI categorisation disabled")
                    return None
                return Groq(api_key=api_key)
            except ImportError:
                logger.warning("groq not installed")
                return None

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
                model=self.model_name or "llama-3.3-70b-versatile",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
            )
            return response.choices[0].message.content

        raise RuntimeError("No AI provider configured")

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
