from __future__ import annotations

import hashlib
import re
from datetime import UTC, date, datetime, time


_INVALID_WINDOWS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def safe_path_component(value: str) -> str:
    original = value
    cleaned = _INVALID_WINDOWS.sub("_", value).rstrip(" .")
    cleaned = re.sub(r"_+", "_", cleaned) or "unnamed"
    if cleaned.upper() in _RESERVED:
        cleaned = f"{cleaned}_"
    if cleaned != original:
        digest = hashlib.sha256(original.encode("utf-8")).hexdigest()[:8]
        cleaned = f"{cleaned}__{digest}"
    return cleaned


def parse_as_of(value: str | None) -> datetime:
    if not value:
        return datetime.now(UTC)
    text = value.strip()
    try:
        if len(text) == 10:
            parsed_date = date.fromisoformat(text)
            return datetime.combine(parsed_date, time.max, tzinfo=UTC)
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("--as-of must be ISO date or timezone-aware ISO datetime") from exc
    if parsed.tzinfo is None:
        raise ValueError("--as-of datetime must include a timezone offset")
    return parsed.astimezone(UTC)


def utc_iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")

