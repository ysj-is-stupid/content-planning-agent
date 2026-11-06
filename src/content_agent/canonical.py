"""Stable semantic normalization for business identities."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from datetime import datetime, timezone
from typing import Any


def normalize_text(value: str) -> str:
    """Normalize Unicode and boundary whitespace without changing inner content."""
    return unicodedata.normalize("NFC", value).strip()


def normalize_phrase(value: str) -> str:
    """Normalize keywords for explicit, case-insensitive coverage checks."""
    return " ".join(normalize_text(value).casefold().split())


def canonical_data(value: Any) -> Any:
    """Convert supported data to a deterministic, JSON-compatible structure."""
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="python")
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("datetime must include timezone")
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, str):
        return normalize_text(value)
    if isinstance(value, dict):
        return {str(k): canonical_data(v) for k, v in sorted(value.items())}
    if isinstance(value, (tuple, list)):
        return [canonical_data(v) for v in value]
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    raise TypeError(f"unsupported canonical value: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    return json.dumps(canonical_data(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
