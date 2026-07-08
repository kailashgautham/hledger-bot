"""Generic AI-powered parser — works with any bank's credit card statement."""
import base64
import json
import logging
import os
import re
import time

import pdfplumber  # type: ignore

from .base import BaseParser, Transaction

logger = logging.getLogger(__name__)


class AIParser(BaseParser):
    IDENTIFIERS = []
    card_name = "Unknown"
    offset_account = "liabilities:creditcard:unknown"

    def __init__(self, config: dict):
        ai = config.get("ai", {})
        self.provider = ai.get("provider", "none")
        self.model_name = ai.get("model", "")
        self._cards = config.get("cards", [])
        self._client = self._init_client()
        self._config = config

    @property
    def available(self) -> bool:
        return self._client is not None

    def _init_client(self):
        if self.provider == "gemini":
            try:
                import google.generativeai as genai  # type: ignore
                api_key = os.environ.get("GOOGLE_API_KEY")
                if not api_key:
                    return None
                genai.configure(api_key=api_key)
                return genai.GenerativeModel(self.model_name or "gemini-1.5-flash")
            except ImportError:
                return None

        if self.provider == "groq":
            try:
                from groq import Groq  # type: ignore
                api_key = os.environ.get("GROQ_API_KEY")
                if not api_key:
                    return None
                return Groq(api_key=api_key)
            except ImportError:
                return None

        return None

    def detect(self, text: str) -> bool:
        return self.available

    def parse_image(self, image_path: str) -> list[Transaction]:
        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()

        prompt = self._extraction_prompt("<image>")
        for attempt in range(3):
            try:
                raw = self._call_vision(b64, prompt)
                return self._parse_response(raw)
            except Exception as exc:
                msg = str(exc)
                if attempt < 2 and ("429" in msg or "quota" in msg.lower() or "rate" in msg.lower()):
                    wait = 15 * (attempt + 1)
                    logger.warning("AIParser: rate limited, retrying in %ds…", wait)
                    time.sleep(wait)
                    continue
                logger.error("AIParser: image extraction failed: %s", exc)
                return []
        return []

    def _call_vision(self, b64_image: str, prompt: str) -> str:
        if self.provider == "groq":
            resp = self._client.chat.completions.create(
                model="meta-llama/llama-4-scout-17b-16e-instruct",
                messages=[{"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_image}"}},
                    {"type": "text", "text": prompt},
                ]}],
                temperature=0.0,
            )
            return resp.choices[0].message.content

        if self.provider == "gemini":
            import PIL.Image
            import io
            img = PIL.Image.open(io.BytesIO(base64.b64decode(b64_image)))
            return self._client.generate_content([prompt, img]).text

        raise RuntimeError("No AI provider configured")

    def _extraction_prompt(self, content: str) -> str:
        body = f"\nStatement text:\n{content}" if content != "<image>" else ""
        return f"""Extract all transactions from this bank statement.

Return JSON only (no markdown fences):
{{
  "card_name": "full name of the card or account as printed on the statement",
  "bank_id": "short lowercase bank identifier (e.g. scb, dbs, ocbc, citi, uob, hsbc, posb, maybank)",
  "account_type": "credit or debit",
  "transactions": [
    {{"date": "YYYY-MM-DD", "description": "merchant name", "amount": 12.50}},
    ...
  ]
}}

Rules:
- account_type: "credit" for credit cards, "debit" for debit cards, savings, or current accounts
- bank_id: the bank abbreviation only (not the card product name)
- amount is always a positive number (the spend/debit amount)
- For credit statements: skip credits, refunds, payments, and any negative amounts
- For debit statements: skip incoming transfers, deposits, interest — only include outgoing payments/purchases
- date must be ISO format YYYY-MM-DD
- description must be a clean, human-readable merchant name:
  * Remove order IDs, booking codes, random alphanumeric suffixes (e.g. "AIRBNB * HMD2S4Q5EC" → "Airbnb")
  * Remove URL prefixes/domains (e.g. "WWW.TADA.GLOBAL" → "Tada", "WWW_CONTABO_COM" → "Contabo")
  * Remove payment prefixes (e.g. "fp*Food Panda" → "Food Panda", "Grab* A-98OLA4OGW3W" → "Grab")
  * Convert ALL CAPS to Title Case (e.g. "LUCKIN COFFEE" → "Luckin Coffee")
  * Keep well-known brand names as-is in proper casing (e.g. "McDonald's", "Airbnb", "Shopee"){body}"""

    def parse(self, pdf_path: str) -> list[Transaction]:
        try:
            with pdfplumber.open(pdf_path) as pdf:
                pages_text = "\n\n".join(
                    (p.extract_text() or "") for p in pdf.pages
                )
        except Exception as exc:
            logger.error("AIParser: could not open PDF: %s", exc)
            return []

        prompt = self._extraction_prompt(pages_text[:12000])

        for attempt in range(3):
            try:
                raw = self._call(prompt)
                return self._parse_response(raw)
            except Exception as exc:
                msg = str(exc)
                if attempt < 2 and ("429" in msg or "quota" in msg.lower() or "rate" in msg.lower()):
                    wait = 15 * (attempt + 1)
                    logger.warning("AIParser: rate limited, retrying in %ds…", wait)
                    time.sleep(wait)
                    continue
                logger.error("AIParser: extraction failed: %s", exc)
                return []
        return []

    def _call(self, prompt: str) -> str:
        if self.provider == "gemini":
            return self._client.generate_content(prompt).text

        if self.provider == "groq":
            resp = self._client.chat.completions.create(
                model=self.model_name or "llama-3.3-70b-versatile",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
            )
            return resp.choices[0].message.content

        raise RuntimeError("No AI provider configured")

    def _parse_response(self, raw: str) -> list[Transaction]:
        text = raw.strip()
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        data = json.loads(text)

        detected_name = data.get("card_name", "")
        self.card_name = detected_name or "Unknown"

        bank_id = re.sub(r"[^a-z0-9]+", "-", data.get("bank_id", "unknown").lower()).strip("-") or "unknown"
        account_type = data.get("account_type", "credit").lower()
        if account_type == "debit":
            self.offset_account = f"assets:bank:{bank_id}"
        else:
            self.offset_account = f"liabilities:creditcard:{bank_id}"

        # Allow config to override offset_account
        override = self._config_override(detected_name)
        if override:
            self.card_name = override["name"]
            self.offset_account = override.get("offset_account", self.offset_account)

        transactions: list[Transaction] = []
        for item in data.get("transactions", []):
            try:
                amount = float(item["amount"])
                if amount <= 0:
                    continue
                transactions.append(Transaction(
                    date=item["date"],
                    description=str(item["description"]).strip(),
                    amount=round(amount, 2),
                ))
            except (KeyError, ValueError, TypeError):
                continue

        return transactions

    def _config_override(self, detected: str) -> dict | None:
        """Return a matching card config entry if the user has defined one, else None."""
        if not detected:
            return None
        detected_lower = detected.lower()
        for card in self._cards:
            name = card["name"].lower()
            if name in detected_lower or detected_lower in name:
                return card
        return None
