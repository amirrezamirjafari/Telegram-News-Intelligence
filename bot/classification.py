from __future__ import annotations

import re
from dataclasses import replace

from bot.fetchers.base import FeedEntry, MarketType

_CURRENCY = (
    r"(?:USD|EUR|GBP|JPY|CHF|CAD|AUD|NZD|CNY|CNH|SEK|NOK|MXN|"
    r"ZAR|TRY|SGD|HKD|INR|BRL)"
)
_FOREX_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bforex\b",
        r"\bforeign exchange\b",
        r"\bcurrencies?\b",
        r"\bfx\s+(?:market|trading|rates?)\b",
        r"\b(?:USD|EUR|GBP|JPY|CHF|CAD|AUD|NZD|CNY|CNH|SEK|NOK|MXN|"
        r"ZAR|TRY|SGD|HKD|INR|BRL)\b",
        rf"\b{_CURRENCY}\s*[/\-]\s*{_CURRENCY}\b",
        rf"\b{_CURRENCY}{_CURRENCY}\b",
        r"\b(?:US\s+)?dollar(?:\s+index)?\b",
        r"\bDXY\b",
        r"\beuro\b",
        r"\b(?:British\s+)?pound(?:\s+sterling)?\b",
        r"\bsterling\b",
        r"\byen\b",
        r"\bswiss\s+franc\b",
        r"\bgold\b",
        r"\bbullion\b",
        r"\bXAU(?:\s*/\s*USD|USD)?\b",
    )
)
_MACRO_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bcentral\s+banks?\b",
        r"\bfederal\s+reserve\b",
        r"\b(?:the\s+)?Fed\b",
        r"\bFOMC\b",
        r"\bECB\b",
        r"\bBank\s+of\s+England\b",
        r"\bBank\s+of\s+Japan\b",
        r"\bRBA\b|\bReserve\s+Bank\s+of\s+Australia\b",
        r"\bBank\s+of\s+Canada\b|\bBoC\b",
        r"\bSNB\b|\bSwiss\s+National\s+Bank\b",
        r"\bPBOC\b|\bPeople'?s\s+Bank\s+of\s+China\b",
        r"\binterest\s+rates?\b",
        r"\brate\s+(?:cut|hike|decision|increase|reduction)s?\b",
        r"\bmonetary\s+policy\b",
        r"\binflation\b|\bCPI\b|\bPCE\b|\bPPI\b",
        r"\bGDP\b",
        r"\bnon[-\s]?farm\s+payrolls?\b|\bNFP\b|\bpayrolls?\b",
        r"\b(?:unemployment|jobless\s+claims|jobs?\s+report)\b",
        r"\b(?:retail\s+sales|PMI|ISM|wages?|consumer\s+confidence)\b",
        r"\b(?:economic\s+data|economic\s+growth|industrial\s+production)\b",
        r"\b(?:durable\s+goods|housing\s+starts|trade\s+balance)\b",
    )
)
_GLOBAL_STOCK_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bstocks?\b|\bshares?\b|\bequities\b|\bstock\s+market\b",
        r"\bS\s*&?\s*P\s*500\b",
        r"\bNasdaq(?:\s+100)?\b",
        r"\bDow\s+Jones\b|\bDow\b",
        r"\bRussell\s+2000\b",
        r"\bFTSE(?:\s+100)?\b|\bDAX\b",
        r"\bNikkei(?:\s+225)?\b|\bHang\s+Seng\b|\bKospi\b",
        r"\bEuro\s+Stoxx\b|\bSTOXX\s+600\b|\bASX\s+200\b",
        r"\bearnings\b|\bIPO\b",
        r"\bApple\b|\bNvidia\b|\bMicrosoft\b|\bTesla\b|\bAmazon\b",
        r"\bAlphabet\b|\bGoogle\b|\bMeta\b|\bBroadcom\b|\bAMD\b|\bIntel\b",
        r"\bJPMorgan\b|\bBerkshire\s+Hathaway\b|\bWalmart\b",
        r"\bAAPL\b|\bNVDA\b|\bMSFT\b|\bTSLA\b|\bAMZN\b|\bGOOGL\b",
        r"\bGOOG\b|\bMETA\b|\bAVGO\b|\bINTC\b|\bJPM\b|\bWMT\b",
    )
)


def classify_market(text: str) -> MarketType | None:
    if any(pattern.search(text) for pattern in _MACRO_PATTERNS):
        return "macro"
    if any(pattern.search(text) for pattern in _FOREX_PATTERNS):
        return "forex"
    if any(pattern.search(text) for pattern in _GLOBAL_STOCK_PATTERNS):
        return "global_stock"
    return None


def classify_entry(entry: FeedEntry) -> FeedEntry | None:
    text = " ".join(part for part in (entry.title, entry.summary or "") if part)
    market = classify_market(text)
    if market is None:
        return None
    return replace(entry, market=market)
