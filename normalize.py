"""
Data normalization layer.

Monday.com items come back as {"columns": {"<Column Title>": {"text": ..., "value": ..., "type": ...}}}
(see monday_client._flatten_item). This module turns that into clean, predictable Python records
that the LLM (and any code) can reason about without having to guess at date formats, currency
strings, or a dozen casings of the same status label.

Nothing here is LLM-driven on purpose: normalization has to be deterministic and auditable, so
the same messy input always produces the same clean output.
"""
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Optional

from dateutil import parser as dateutil_parser

from config import BoardSchema, SECTOR_ALIASES, STATUS_ALIASES

UNKNOWN = "Unknown"

# Merge the two alias tables into one lookup: variant (lowercased) -> canonical form.
_ALIAS_LOOKUP: dict[str, str] = {}
for _table in (SECTOR_ALIASES, STATUS_ALIASES):
    for canonical, variants in _table.items():
        _ALIAS_LOOKUP[canonical.lower()] = canonical
        for variant in variants:
            _ALIAS_LOOKUP[variant.lower()] = canonical


@dataclass
class DataQualityReport:
    board_name: str
    total_records: int = 0
    dropped_rows: int = 0
    dropped_row_reasons: list[str] = field(default_factory=list)
    missing_field_counts: dict[str, int] = field(default_factory=dict)
    unrecognized_categorical_values: dict[str, set] = field(default_factory=dict)

    def missing_field_pct(self, field_name: str) -> float:
        if not self.total_records:
            return 0.0
        return round(100 * self.missing_field_counts.get(field_name, 0) / self.total_records, 1)

    def to_dict(self) -> dict:
        return {
            "board_name": self.board_name,
            "total_records": self.total_records,
            "dropped_rows": self.dropped_rows,
            "dropped_row_reasons": self.dropped_row_reasons,
            "missing_field_counts": self.missing_field_counts,
            "missing_field_pcts": {
                k: self.missing_field_pct(k) for k in self.missing_field_counts
            },
            "unrecognized_categorical_values": {
                k: sorted(v) for k, v in self.unrecognized_categorical_values.items()
            },
        }


def build_title_index(column_titles: list[str]) -> dict[str, str]:
    """lowercased+trimmed title -> original title, so schema lookups tolerate cosmetic drift."""
    return {t.strip().lower(): t for t in column_titles}


def _clean_text(raw: Optional[str]) -> Optional[str]:
    if raw is None:
        return None
    text = str(raw).replace("\xa0", " ").strip()
    text = re.sub(r"\s+", " ", text)
    return text or None


def normalize_categorical(raw: Optional[str]) -> Optional[str]:
    """Trim/collapse whitespace, fix casing, and map known messy variants to a canonical label.

    e.g. "Energy", "energy ", "ENERGY" -> "Renewables" (via config.SECTOR_ALIASES),
    "pause / struck" -> "Stuck". Falls back to Title Case for anything not in the alias table
    so at least casing/whitespace is consistent even for values we didn't anticipate.
    """
    text = _clean_text(raw)
    if text is None:
        return None
    canonical = _ALIAS_LOOKUP.get(text.lower())
    if canonical:
        return canonical
    # Not a known alias: normalize casing generically (preserve short all-caps tokens like "PO").
    if text.isupper() and len(text) > 4:
        return text.title()
    return text


_CURRENCY_STRIP_RE = re.compile(r"[^\d.\-]")


def parse_number(raw: Any) -> Optional[float]:
    """Coerce a possibly-messy numeric cell (currency symbols, commas, stray text) into a float."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    text = _clean_text(str(raw))
    if not text:
        return None
    # Drop currency symbols/commas/units, keep digits, one decimal point, leading minus.
    cleaned = _CURRENCY_STRIP_RE.sub("", text)
    if cleaned in ("", "-", "."):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_date(raw: Any) -> Optional[str]:
    """Parse a date from whatever format Monday/CSV import produced, return ISO 8601 (YYYY-MM-DD).

    Handles Monday's native "YYYY-MM-DD" text, Excel-style "MM/DD/YYYY" or "DD-Mon-YYYY", and
    already-parsed datetime/date objects (e.g. if this is ever fed pandas Timestamps directly).
    """
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw.date().isoformat()
    if isinstance(raw, date):
        return raw.isoformat()
    text = _clean_text(str(raw))
    if not text:
        return None
    try:
        dt = dateutil_parser.parse(text, dayfirst=False, fuzzy=True)
    except (ValueError, OverflowError):
        return None
    return dt.date().isoformat()


def _looks_like_header_leakage_row(columns: dict, title_to_field: dict) -> bool:
    """Detect spurious rows where cell text equals its own column title (seen in the source
    data — e.g. a "Deal Status" cell literally containing the text "Deal Status"). These are
    junk rows from the CSV import, not real records, and would otherwise show up as a fake
    category value. Flag a row as leakage if at least half of its known fields are self-titled.
    """
    matches, checked = 0, 0
    for title in title_to_field:
        cell = columns.get(title)
        if not cell:
            continue
        text = _clean_text(cell.get("text"))
        if text is None:
            continue
        checked += 1
        if text.strip().lower() == title.strip().lower():
            matches += 1
    return checked >= 3 and matches / checked >= 0.5


def normalize_board_items(raw_items: list[dict], schema: BoardSchema, board_name: str) -> tuple[list[dict], DataQualityReport]:
    """Turn monday_client.get_board_items() output into clean records + a data quality report.

    Every logical field from schema.fields ends up as a key in each record. Missing/blank
    cells become explicit None (reported separately) rather than empty strings, per spec, and
    each record also carries `_missing_fields`: the list of logical fields that were blank on
    that row, so the agent can caveat individual answers if needed.
    """
    report = DataQualityReport(board_name=board_name)
    if not raw_items:
        return [], report

    all_titles = {t for item in raw_items for t in item["columns"]}
    title_index = build_title_index(list(all_titles))

    # Resolve each logical field to the actual column title present on the board (tolerant of
    # trivial casing/whitespace differences between config.py and the live Monday schema).
    resolved_fields: dict[str, str] = {}
    for logical_name, expected_title in schema.fields.items():
        actual_title = title_index.get(expected_title.strip().lower())
        if actual_title:
            resolved_fields[logical_name] = actual_title

    title_to_field = {v: k for k, v in resolved_fields.items()}

    records = []
    for item in raw_items:
        columns = item["columns"]

        if _looks_like_header_leakage_row(columns, title_to_field):
            report.dropped_rows += 1
            report.dropped_row_reasons.append(
                f"item {item.get('id')} ('{item.get('name')}') looked like a duplicated header row"
            )
            continue

        record: dict[str, Any] = {"monday_item_id": item["id"], "monday_item_name": item.get("name")}
        missing_fields = []

        for logical_name in schema.fields:
            if logical_name == schema.name_field:
                # Lives on Monday's built-in item Name field, not a regular column (see
                # config.BoardSchema.name_field docstring).
                raw_text = item.get("name")
            else:
                title = resolved_fields.get(logical_name)
                cell = columns.get(title) if title else None
                raw_text = cell.get("text") if cell else None

            if logical_name in schema.date_fields:
                value = parse_date(raw_text)
            elif logical_name in schema.numeric_fields:
                value = parse_number(raw_text)
            elif logical_name in schema.categorical_fields:
                value = normalize_categorical(raw_text)
            else:
                value = _clean_text(raw_text)

            if value is None:
                missing_fields.append(logical_name)
                report.missing_field_counts[logical_name] = report.missing_field_counts.get(logical_name, 0) + 1
                # Categorical fields default to "Unknown" per spec; numeric/date stay None so
                # aggregation code doesn't accidentally treat a missing amount as Unknown*0.
                value = UNKNOWN if logical_name in schema.categorical_fields else None

            record[logical_name] = value

        record["_missing_fields"] = missing_fields
        records.append(record)

    report.total_records = len(records)
    return records, report


def merge_quality_reports(reports: list[DataQualityReport]) -> dict:
    return {r.board_name: r.to_dict() for r in reports}
