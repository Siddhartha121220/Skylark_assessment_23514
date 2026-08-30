"""
Central configuration: env/secrets loading + the schema knowledge that ties Monday.com's
opaque board/column IDs back to meaningful business fields.

Design choice: instead of hardcoding Monday *column IDs* (which are opaque, per-account
strings like "text_mkr2x7fq" that don't exist until a human runs list_boards.py), we map by
column *title* — the human-readable header, which is stable because it came straight from the
CSV import ("Sector", "Deal Stage", "Masked Deal value", ...). This means the app needs zero
manual column-mapping step; only the two board IDs are optional config, and even those can be
auto-discovered by board name if left blank. See list_boards.py for the one-time diagnostic.
"""
import os
from dataclasses import dataclass, field
from typing import Optional

from dotenv import load_dotenv

load_dotenv()


def _clean_env(name: str) -> Optional[str]:
    val = os.getenv(name)
    if val is None:
        return None
    val = val.strip()
    return val or None


MONDAY_API_TOKEN = _clean_env("MONDAY_API_TOKEN")

# LLM access: the OpenAI Python SDK talks to any OpenAI-compatible endpoint, so we support
# either a real OpenAI key or an OpenRouter key (https://openrouter.ai) pointed at the same SDK
# via base_url — handy since OpenRouter bills separately from OpenAI/Anthropic credit balances.
# OpenRouter keys are self-identifying (they start with "sk-or-"), so we detect the provider by
# key shape rather than requiring the value to be typed into a specific env var name — either
# OPENAI_API_KEY or OPENROUTER_API_KEY works no matter which kind of key you paste into it.
OPENAI_API_KEY = _clean_env("OPENAI_API_KEY")
OPENROUTER_API_KEY = _clean_env("OPENROUTER_API_KEY")
_raw_key = OPENROUTER_API_KEY or OPENAI_API_KEY

# Default model choice: testing found "openai/gpt-4o-mini" unreliable specifically for
# aggregating tool results (wrong sums, mis-transcribed multi-call tables) even after handing
# it pre-computed aggregates — see agent.py's system prompt / accuracy comments. OpenAI's
# open-weight "gpt-oss-120b" (served via OpenRouter) matched ground truth exactly across the
# same tests at comparable cost, so it's the default for OpenRouter keys.
if _raw_key and _raw_key.startswith("sk-or-"):
    LLM_API_KEY = _raw_key
    LLM_BASE_URL = _clean_env("OPENROUTER_BASE_URL") or "https://openrouter.ai/api/v1"
    _default_model = "openai/gpt-oss-120b"
elif OPENROUTER_API_KEY:
    LLM_API_KEY = OPENROUTER_API_KEY
    LLM_BASE_URL = _clean_env("OPENROUTER_BASE_URL") or "https://openrouter.ai/api/v1"
    _default_model = "openai/gpt-oss-120b"
else:
    LLM_API_KEY = OPENAI_API_KEY
    LLM_BASE_URL = None
    _default_model = "gpt-4o"

LLM_MODEL = _clean_env("LLM_MODEL") or _default_model

MONDAY_API_URL = "https://api.monday.com/v2"

# Board IDs are optional: if unset, monday_client.find_board_by_name() locates them by title.
WORK_ORDERS_BOARD_ID = _clean_env("MONDAY_WORK_ORDERS_BOARD_ID")
DEALS_BOARD_ID = _clean_env("MONDAY_DEALS_BOARD_ID")

# Used for auto-discovery when an explicit board ID isn't configured (case-insensitive
# substring match against board names in the account).
WORK_ORDERS_BOARD_NAME_HINT = "work order"
DEALS_BOARD_NAME_HINT = "deal"


@dataclass(frozen=True)
class BoardSchema:
    """Maps logical field names -> the Monday column title that carries that field.

    Column titles are matched case-insensitively / whitespace-trimmed at lookup time (see
    normalize.py:build_title_index), so minor cosmetic differences in how the column got
    named in Monday won't break the mapping.
    """

    name_hint: str
    board_id_env: Optional[str]
    # logical_field -> column title as it appears on the Monday board
    fields: dict = field(default_factory=dict)
    date_fields: tuple = ()
    numeric_fields: tuple = ()
    # fields whose values should be run through the categorical alias/casing normalizer
    categorical_fields: tuple = ()
    # Which logical field actually lives on Monday's built-in item "Name" identity column
    # rather than a regular column. Monday's CSV import turns the *first* CSV column into the
    # item name rather than a queryable column with that title, so e.g. "Deal name masked" /
    # "Deal Name" show up as item.name, not as a column titled that — this field routes around
    # that instead of silently resolving to null. See normalize.py:normalize_board_items.
    name_field: Optional[str] = None


WORK_ORDERS_SCHEMA = BoardSchema(
    name_hint=WORK_ORDERS_BOARD_NAME_HINT,
    board_id_env=WORK_ORDERS_BOARD_ID,
    fields={
        "deal_name": "Deal name masked",
        "customer_code": "Customer Name Code",
        "serial_number": "Serial #",
        "nature_of_work": "Nature of Work",
        "last_executed_month": "Last executed month of recurring project",
        "execution_status": "Execution Status",
        "data_delivery_date": "Data Delivery Date",
        "po_loi_date": "Date of PO/LOI",
        "document_type": "Document Type",
        "probable_start_date": "Probable Start Date",
        "probable_end_date": "Probable End Date",
        "owner_code": "BD/KAM Personnel code",
        "sector": "Sector",
        "type_of_work": "Type of Work",
        "skylark_platform_flag": "Is any Skylark software platform part of the client deliverables in this deal?",
        "last_invoice_date": "Last invoice date",
        "latest_invoice_no": "latest invoice no.",
        "amount_excl_gst": "Amount in Rupees (Excl of GST) (Masked)",
        "amount_incl_gst": "Amount in Rupees (Incl of GST) (Masked)",
        "billed_excl_gst": "Billed Value in Rupees (Excl of GST.) (Masked)",
        "billed_incl_gst": "Billed Value in Rupees (Incl of GST.) (Masked)",
        "collected_incl_gst": "Collected Amount in Rupees (Incl of GST.) (Masked)",
        "to_be_billed_excl_gst": "Amount to be billed in Rs. (Exl. of GST) (Masked)",
        "to_be_billed_incl_gst": "Amount to be billed in Rs. (Incl. of GST) (Masked)",
        "amount_receivable": "Amount Receivable (Masked)",
        "ar_priority": "AR Priority account",
        "qty_ops": "Quantity by Ops",
        "qty_po": "Quantities as per PO",
        "qty_billed": "Quantity billed (till date)",
        "qty_balance": "Balance in quantity",
        "invoice_status": "Invoice Status",
        "expected_billing_month": "Expected Billing Month",
        "actual_billing_month": "Actual Billing Month",
        "actual_collection_month": "Actual Collection Month",
        "wo_status": "WO Status (billed)",
        "collection_status": "Collection status",
        "collection_date": "Collection Date",
        "billing_status": "Billing Status",
    },
    date_fields=(
        "data_delivery_date", "po_loi_date", "probable_start_date", "probable_end_date",
        "last_invoice_date", "collection_date",
    ),
    numeric_fields=(
        "amount_excl_gst", "amount_incl_gst", "billed_excl_gst", "billed_incl_gst",
        "collected_incl_gst", "to_be_billed_excl_gst", "to_be_billed_incl_gst",
        "amount_receivable",
    ),
    categorical_fields=("sector", "execution_status", "wo_status", "invoice_status", "billing_status"),
    name_field="deal_name",
)

DEALS_SCHEMA = BoardSchema(
    name_hint=DEALS_BOARD_NAME_HINT,
    board_id_env=DEALS_BOARD_ID,
    fields={
        "deal_name": "Deal Name",
        "owner_code": "Owner code",
        "client_code": "Client Code",
        "deal_status": "Deal Status",
        "close_date_actual": "Close Date (A)",
        "closure_probability": "Closure Probability",
        "deal_value": "Masked Deal value",
        "tentative_close_date": "Tentative Close Date",
        "deal_stage": "Deal Stage",
        "product_deal": "Product deal",
        "sector": "Sector/service",
        "created_date": "Created Date",
    },
    date_fields=("close_date_actual", "tentative_close_date", "created_date"),
    numeric_fields=("deal_value",),
    categorical_fields=("deal_status", "closure_probability", "deal_stage", "sector", "product_deal"),
    name_field="deal_name",
)

# Sector name aliasing: canonical form -> tuple of known messy variants (case/whitespace are
# already handled by the generic cleaner; list only genuine spelling/wording variants here).
SECTOR_ALIASES = {
    "Mining": ("mining", "mine"),
    "Powerline": ("powerline", "power line", "power-line"),
    "Renewables": ("renewables", "renewable", "renewable energy", "energy"),
    "Railways": ("railways", "railway", "rail"),
    "Construction": ("construction",),
    "Aviation": ("aviation",),
    "Manufacturing": ("manufacturing",),
    "Security and Surveillance": ("security and surveillance", "security & surveillance", "surveillance"),
    "Tender": ("tender", "tenders"),
    "DSP": ("dsp",),
    "Others": ("others", "other", "misc", "miscellaneous"),
}

# Free-text status columns: canonical -> known messy variants.
STATUS_ALIASES = {
    "Stuck": ("stuck", "pause / struck", "paused / stuck", "struck"),
    "Not Started": ("not started", "not yet started"),
    "Ongoing": ("ongoing", "in progress", "in-progress"),
    "Completed": ("completed", "complete", "done"),
    "Partial Completed": ("partial completed", "partially completed"),
    "Details Pending From Client": ("details pending from client", "pending from client"),
    "Open": ("open",),
    "Closed": ("closed", "close"),
    "Won": ("won", "win"),
    "Dead": ("dead", "lost"),
    "On Hold": ("on hold", "hold"),
    "Billed": ("billed", "bilied"),
    "Partially Billed": ("partially billed",),
    "Fully Billed": ("fully billed",),
    "Not Billed Yet": ("not billed yet", "not billable yet"),
    "Not Billable": ("not billable",),
    "Update Required": ("update required",),
}


def require_config_or_raise() -> None:
    """Fail fast with a human-readable message instead of crashing deep in the call stack."""
    missing = []
    if not MONDAY_API_TOKEN:
        missing.append("MONDAY_API_TOKEN")
    if not LLM_API_KEY:
        missing.append("OPENAI_API_KEY (or OPENROUTER_API_KEY)")
    if missing:
        raise RuntimeError(
            "Missing required secret(s): "
            + ", ".join(missing)
            + ". Copy .env.example to .env and fill them in (see README.md 'Setup')."
        )
