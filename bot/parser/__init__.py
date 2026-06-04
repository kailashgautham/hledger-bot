"""Parser factory — detects bank from PDF content and returns the right parser."""
import logging
from typing import Optional

import pdfplumber  # type: ignore

from .ai_parser import AIParser
from .base import BaseParser
from .dbs import DBSParser
from .ocbc import OCBCParser

logger = logging.getLogger(__name__)

_PARSER_REGISTRY: list[type[BaseParser]] = [DBSParser, OCBCParser]

_PARSER_BY_KEY: dict[str, type[BaseParser]] = {
    "dbs": DBSParser,
    "ocbc": OCBCParser,
}


def get_parser(pdf_path: str, config: dict) -> Optional[BaseParser]:
    try:
        with pdfplumber.open(pdf_path) as pdf:
            first_page_text = pdf.pages[0].extract_text() or ""
            full_text = "\n".join(
                (p.extract_text() or "") for p in pdf.pages[:3]
            )
    except Exception as exc:
        logger.error("Could not open PDF: %s", exc)
        return None

    # Try fast regex-based parsers first
    for parser_cls in _PARSER_REGISTRY:
        instance = parser_cls()
        if instance.detect(full_text) or instance.detect(first_page_text):
            for card in config.get("cards", []):
                if card.get("parser") == _key_for(parser_cls):
                    instance.card_name = card["name"]
                    return instance
            return instance

    # Fall back to AI parser for any other bank
    ai_parser = AIParser(config)
    if ai_parser.available:
        logger.info("No regex parser matched — using AI parser")
        return ai_parser

    return None


def _key_for(cls: type[BaseParser]) -> str:
    for key, c in _PARSER_BY_KEY.items():
        if c is cls:
            return key
    return ""
