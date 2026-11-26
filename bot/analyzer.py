from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Literal

Sentiment = Literal["positive", "negative", "neutral"]
Impact = Literal["low", "medium", "high"]

EXPECTED_FIELDS = {
    "summary_fa",
    "assets",
    "sentiment",
    "impact",
    "reasoning_fa",
}
PERSIAN_SCRIPT = re.compile(r"[\u0600-\u06ff]")


class InvalidAnalysisResponse(ValueError):
    pass


@dataclass(frozen=True)
class NewsAnalysis:
    summary_fa: str
    assets: tuple[str, ...]
    sentiment: Sentiment
    impact: Impact
    reasoning_fa: str
    confidence: float | None = None
    possible_impact_fa: str | None = None
    deep_confidence: float | None = None
    deep_caveat_fa: str | None = None

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["assets"] = list(self.assets)
        if self.confidence is None:
            result.pop("confidence")
        if self.possible_impact_fa is None:
            result.pop("possible_impact_fa")
        if self.deep_confidence is None:
            result.pop("deep_confidence")
        if self.deep_caveat_fa is None:
            result.pop("deep_caveat_fa")
        return result


def validate_analysis(value: object) -> NewsAnalysis:
    allowed_fields = EXPECTED_FIELDS | {
        "confidence",
        "possible_impact_fa",
        "deep_confidence",
        "deep_caveat_fa",
    }
    if (
        not isinstance(value, dict)
        or not EXPECTED_FIELDS.issubset(value)
        or set(value) - allowed_fields
    ):
        raise InvalidAnalysisResponse("analysis fields do not match the required schema")

    summary = value["summary_fa"]
    assets = value["assets"]
    sentiment = value["sentiment"]
    impact = value["impact"]
    reasoning = value["reasoning_fa"]

    if not isinstance(summary, str) or not summary.strip() or len(summary) > 1200:
        raise InvalidAnalysisResponse("summary_fa must be a short non-empty string")
    sentences = [
        sentence
        for sentence in re.split(r"(?<=[.!?؟])\s+|\n+", summary.strip())
        if sentence.strip()
    ]
    if len(sentences) not in {2, 3} or not PERSIAN_SCRIPT.search(summary):
        raise InvalidAnalysisResponse("summary_fa must contain 2-3 Persian sentences")

    if (
        not isinstance(assets, list)
        or len(assets) > 20
        or any(not isinstance(asset, str) or not asset.strip() or len(asset) > 60 for asset in assets)
    ):
        raise InvalidAnalysisResponse("assets must be a list of short strings")

    if not isinstance(sentiment, str) or sentiment not in {
        "positive",
        "negative",
        "neutral",
    }:
        raise InvalidAnalysisResponse("sentiment is not an allowed value")
    if not isinstance(impact, str) or impact not in {"low", "medium", "high"}:
        raise InvalidAnalysisResponse("impact is not an allowed value")
    if (
        not isinstance(reasoning, str)
        or not reasoning.strip()
        or len(reasoning) > 300
        or not PERSIAN_SCRIPT.search(reasoning)
    ):
        raise InvalidAnalysisResponse("reasoning_fa must be short Persian text")

    confidence = value.get("confidence")
    if confidence is not None and not _is_confidence(confidence):
        raise InvalidAnalysisResponse("confidence must be between 0 and 1")
    possible_impact = value.get("possible_impact_fa")
    if possible_impact is not None and (
        not isinstance(possible_impact, str)
        or not possible_impact.strip()
        or len(possible_impact) > 700
        or not PERSIAN_SCRIPT.search(possible_impact)
    ):
        raise InvalidAnalysisResponse(
            "possible_impact_fa must be short Persian text"
        )
    deep_confidence = value.get("deep_confidence")
    if deep_confidence is not None and not _is_confidence(deep_confidence):
        raise InvalidAnalysisResponse("deep_confidence must be between 0 and 1")
    deep_caveat = value.get("deep_caveat_fa")
    if deep_caveat is not None and (
        not isinstance(deep_caveat, str)
        or not deep_caveat.strip()
        or len(deep_caveat) > 300
        or not PERSIAN_SCRIPT.search(deep_caveat)
    ):
        raise InvalidAnalysisResponse("deep_caveat_fa must be short Persian text")

    return NewsAnalysis(
        summary_fa=summary.strip(),
        assets=tuple(asset.strip() for asset in assets),
        sentiment=sentiment,
        impact=impact,
        reasoning_fa=reasoning.strip(),
        confidence=confidence,
        possible_impact_fa=(
            possible_impact.strip() if possible_impact is not None else None
        ),
        deep_confidence=deep_confidence,
        deep_caveat_fa=deep_caveat.strip() if deep_caveat is not None else None,
    )


def _is_confidence(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and 0 <= value <= 1
    )
