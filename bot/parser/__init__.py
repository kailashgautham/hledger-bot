"""Parser factory — detects bank from PDF content and returns the right parser."""
import logging
from typing import Optional

import pdfplumber  # type: ignore

from .base import BaseParser
from .dbs import DBSParser
from .ocbc import OCBCParser

logger = logging.getLogger(__name__)

_PARSER_REGISTRY: list[type[BaseParser]] = [DBSParser, OCBCParser]

# Map parser key in config.yaml → class
_PARSER_BY_KEY: dict[str, type[BaseParser]] = {
    "dbs": DBSParser,
    "ocbc": OCBCParser,
}


def get_parser(pdf_path: str, config: dict) -> Optional[BaseParser]:
    """
    Detect the bank from PDF text, then find a matching card config.
    Returns an instantiated parser whose card_name matches a configured card,
    or None if no match found.
    """
    try:
        with pdfplumber.open(pdf_path) as pdf:
            first_page_text = pdf.pages[0].extract_text() or ""
            full_text = "\n".join(
                (p.extract_text() or "") for p in pdf.pages[:3]
            )
    except Exception as exc:
        logger.error("Could not open PDF: %s", exc)
        return None

    for parser_cls in _PARSER_REGISTRY:
        instance = parser_cls()
        if instance.detect(full_text) or instance.detect(first_page_text):
            # Find the matching card in config and update card_name
            for card in config.get("cards", []):
                if card.get("parser") == _key_for(parser_cls):
                    instance.card_name = card["name"]
                    return instance
            # Parser detected but no config card — return anyway
            return instance

    return None


def _key_for(cls: type[BaseParser]) -> str:
    for key, c in _PARSER_BY_KEY.items():
        if c is cls:
            return key
    return ""
