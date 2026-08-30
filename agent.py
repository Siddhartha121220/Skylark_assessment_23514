"""
Agent / tool-calling layer.

Two things live here:
1. BusinessDataStore — fetches both boards from Monday.com, normalizes them (normalize.py),
   and caches the result in memory for the session (no DB, per the spec). This is what the
   agent's tools actually query.
2. BIAgent — wraps the Anthropic Claude API with tool use. The model is given two tools
   (query_work_orders / query_deals) and decides when/how to call them; we never hand the LLM
   raw, un-normalized data.

The leadership update is intentionally *not* left to the LLM to compute numbers for: all the
aggregate math (totals, win rate, status breakdowns, data-quality percentages) is done in plain
Python in `compute_leadership_metrics` so the figures are exact and reproducible, and Claude is
only used to write a short narrative paragraph on top of numbers it's handed, not to invent them.
"""
import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

from openai import OpenAI

import config
import monday_client
import normalize
from monday_client import MondayError


# ---------------------------------------------------------------------------
# Data layer: fetch + normalize + cache + filter
# ---------------------------------------------------------------------------

def _resolve_board_id(schema: config.BoardSchema) -> str:
    if schema.board_id_env:
        return schema.board_id_env
    board = monday_client.find_board_by_name(schema.name_hint)
    if not board:
        raise MondayError(
            f"Could not find a board matching '{schema.name_hint}' and no explicit board ID is "
            f"configured. Run `python list_boards.py` and set the right *_BOARD_ID in .env."
        )
    return board["id"]


class BusinessDataStore:
    """Fetches + normalizes both boards once per session, cached in memory."""

    def __init__(self):
        self._work_orders: Optional[list[dict]] = None
        self._deals: Optional[list[dict]] = None
        self._wo_report: Optional[normalize.DataQualityReport] = None
        self._deals_report: Optional[normalize.DataQualityReport] = None

    def _ensure_loaded(self):
        if self._work_orders is not None and self._deals is not None:
            return
        wo_board_id = _resolve_board_id(config.WORK_ORDERS_SCHEMA)
        deals_board_id = _resolve_board_id(config.DEALS_SCHEMA)

        raw_wo = monday_client.get_board_items(wo_board_id)
        raw_deals = monday_client.get_board_items(deals_board_id)

        self._work_orders, self._wo_report = normalize.normalize_board_items(
            raw_wo, config.WORK_ORDERS_SCHEMA, "Work Orders"
        )
        self._deals, self._deals_report = normalize.normalize_board_items(
            raw_deals, config.DEALS_SCHEMA, "Deals"
        )

    def refresh(self):
        """Force a re-fetch from Monday.com (drops the in-memory cache)."""
        self._work_orders = None
        self._deals = None

    def work_orders(self, filters: Optional[dict] = None) -> list[dict]:
        self._ensure_loaded()
        return _apply_filters(self._work_orders, filters)

    def deals(self, filters: Optional[dict] = None) -> list[dict]:
        self._ensure_loaded()
        return _apply_filters(self._deals, filters)

    def quality_reports(self) -> dict:
        self._ensure_loaded()
        return normalize.merge_quality_reports([self._wo_report, self._deals_report])


_CATEGORICAL_FILTER_FIELDS = (
    "sector", "execution_status", "wo_status", "invoice_status", "billing_status",
    "deal_status", "deal_stage",
)


def _apply_filters(records: list[dict], filters: Optional[dict]) -> list[dict]:
    if not filters:
        return records
    out = records
    for key in _CATEGORICAL_FILTER_FIELDS:
        want = filters.get(key)
        if want:
            out = [r for r in out if str(r.get(key, "")).lower() == str(want).lower()]
        # "<field>_not" excludes a value — e.g. execution_status_not="Completed" for "not yet
        # finished" questions. Exists so the model can get an exact filtered count directly
        # from the tool instead of adding up several categories from counts_by by hand (a
        # failure mode observed in testing: the model mis-summed multi-category breakdowns).
        exclude = filters.get(f"{key}_not")
        if exclude:
            out = [r for r in out if str(r.get(key, "")).lower() != str(exclude).lower()]

    date_field = filters.get("date_field")
    date_from, date_to = filters.get("date_from"), filters.get("date_to")
    if date_field and (date_from or date_to):
        def _in_range(r):
            v = r.get(date_field)
            if not v:
                return False
            if date_from and v < date_from:
                return False
            if date_to and v > date_to:
                return False
            return True
        out = [r for r in out if _in_range(r)]

    # Convenience filter matching the same "overdue" definition used in the Leadership Update
    # (compute_leadership_metrics), so chat answers and the leadership report never disagree.
    if filters.get("overdue_only"):
        today_iso = date.today().isoformat()
        out = [
            r for r in out
            if r.get("probable_end_date") and r["probable_end_date"] < today_iso
            and r.get("execution_status") != "Completed"
        ]

    return out


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

_WO_FILTER_PROPS = {
    "sector": {"type": "string", "description": "Filter by sector, e.g. 'Mining', 'Powerline', 'Renewables'."},
    "execution_status": {"type": "string", "description": "e.g. 'Completed', 'Ongoing', 'Not Started', 'Stuck'."},
    "execution_status_not": {"type": "string", "description": "Exclude one execution status, e.g. 'Completed' to get everything not yet finished. Prefer this over fetching everything and subtracting categories yourself."},
    "wo_status": {"type": "string", "description": "'Open' or 'Closed'."},
    "wo_status_not": {"type": "string"},
    "invoice_status": {"type": "string", "description": "e.g. 'Fully Billed', 'Partially Billed', 'Not Billed Yet'."},
    "invoice_status_not": {"type": "string"},
    "billing_status": {"type": "string"},
    "billing_status_not": {"type": "string"},
    "date_field": {
        "type": "string",
        "enum": ["probable_start_date", "probable_end_date", "data_delivery_date", "po_loi_date", "last_invoice_date", "collection_date"],
        "description": "Which date column to filter on, if date_from/date_to are given.",
    },
    "date_from": {"type": "string", "description": "ISO 8601 date, inclusive lower bound."},
    "date_to": {"type": "string", "description": "ISO 8601 date, inclusive upper bound."},
    "overdue_only": {
        "type": "boolean",
        "description": (
            "Set true for 'overdue' / 'past due' / 'behind schedule' questions instead of "
            "manually combining date_field/execution_status_not yourself. Returns exactly the "
            "work orders whose probable_end_date is in the past and execution_status isn't "
            "'Completed' — the tool's top-level 'count' is then the exact overdue count, no "
            "arithmetic needed."
        ),
    },
}

_DEALS_FILTER_PROPS = {
    "sector": {"type": "string", "description": "e.g. 'Mining', 'Powerline', 'Renewables', 'Aviation'."},
    "deal_status": {"type": "string", "description": "'Open', 'Won', 'Dead', or 'On Hold'."},
    "deal_status_not": {"type": "string", "description": "Exclude one deal status, e.g. 'Dead' to get everything not lost."},
    "deal_stage": {"type": "string", "description": "Funnel stage, e.g. 'B. Sales Qualified Leads', 'G. Project Won'."},
    "deal_stage_not": {"type": "string"},
    "date_field": {
        "type": "string",
        "enum": ["close_date_actual", "tentative_close_date", "created_date"],
        "description": "Which date column to filter on, if date_from/date_to are given.",
    },
    "date_from": {"type": "string", "description": "ISO 8601 date, inclusive lower bound."},
    "date_to": {"type": "string", "description": "ISO 8601 date, inclusive upper bound."},
}

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "query_work_orders",
            "description": (
                "Return normalized Work Orders records (Skylark's delivery/execution board): one "
                "row per work order, with sector, execution status, billing status, dates, and "
                "Rupee amounts. All filters are optional and AND together; omit filters to get "
                "every record."
            ),
            "parameters": {"type": "object", "properties": _WO_FILTER_PROPS},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_deals",
            "description": (
                "Return normalized Deals records (Skylark's sales pipeline board): one row per "
                "deal, with sector, deal stage/status, deal value, and dates. All filters are "
                "optional and AND together; omit filters to get every record."
            ),
            "parameters": {"type": "object", "properties": _DEALS_FILTER_PROPS},
        },
    },
]


def _json_default(o):
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    raise TypeError


# Work Orders has several similarly-named money fields (the contracted order value vs. what's
# actually been billed/collected/is still owed). Testing found the model would sometimes read
# the wrong one despite the distinction being spelled out in the system prompt — so instead of
# relying on it recalling prose from earlier in the context, the field names sent to the model
# are self-documenting: the ambiguity is baked into the key itself, right next to the value.
_WO_FIELD_RELABEL = {
    "amount_excl_gst": "contracted_amount_excl_gst",
    "amount_incl_gst": "contracted_amount_incl_gst",
    "billed_excl_gst": "billed_to_date_excl_gst",
    "billed_incl_gst": "billed_to_date_incl_gst",
    "collected_incl_gst": "collected_to_date_incl_gst",
    "to_be_billed_excl_gst": "remaining_to_bill_excl_gst",
    "to_be_billed_incl_gst": "remaining_to_bill_incl_gst",
    "amount_receivable": "outstanding_receivable",
}


def _relabel(d: dict, mapping: dict) -> dict:
    return {mapping.get(k, k): v for k, v in d.items()}


def _aggregate(records: list[dict], schema: config.BoardSchema, relabel: Optional[dict] = None) -> dict:
    """Pre-compute sums/group-by counts/group-by sums server-side so the model reports these
    numbers verbatim instead of doing arithmetic itself — see the accuracy note in the system
    prompt for why this exists: testing found the model's own summation, and even its manual
    compilation of several separate tool calls into one table, both unreliable — sometimes
    wrong, sometimes inconsistent across identical repeated runs — even when the individual
    tool results it was given were correct. `sums_by` lets a "by sector" / "by status" style
    question be answered from a single unfiltered tool call instead of one call per category
    that the model then has to compile itself."""
    relabel = relabel or {}
    sums = {}
    for field_name in schema.numeric_fields:
        total = sum(r[field_name] for r in records if r.get(field_name) is not None)
        sums[relabel.get(field_name, field_name)] = round(total, 2)

    counts_by = {}
    sums_by = {}
    for field_name in schema.categorical_fields:
        counts: dict[str, int] = {}
        grouped_sums: dict[str, dict] = {}
        for r in records:
            key = r.get(field_name) or normalize.UNKNOWN
            counts[key] = counts.get(key, 0) + 1
            bucket = grouped_sums.setdefault(key, {nf: 0.0 for nf in schema.numeric_fields})
            for nf in schema.numeric_fields:
                v = r.get(nf)
                if v is not None:
                    bucket[nf] += v
        counts_by[field_name] = counts
        sums_by[field_name] = {
            key: {relabel.get(nf, nf): round(v, 2) for nf, v in bucket.items()}
            for key, bucket in grouped_sums.items()
        }

    return {"count": len(records), "sums": sums, "counts_by": counts_by, "sums_by": sums_by}


def _execute_tool(data_store: BusinessDataStore, name: str, tool_input: dict) -> str:
    relabel = {}
    try:
        if name == "query_work_orders":
            records = data_store.work_orders(tool_input)
            schema = config.WORK_ORDERS_SCHEMA
            relabel = _WO_FIELD_RELABEL
        elif name == "query_deals":
            records = data_store.deals(tool_input)
            schema = config.DEALS_SCHEMA
        else:
            return json.dumps({"error": f"Unknown tool '{name}'"})
    except MondayError as exc:
        return json.dumps({"error": str(exc)})

    # Row-level records are for illustration only (see system prompt: all totals must come
    # from "aggregates"), so cap how many are sent — a full board's worth of ~35-field rows
    # bloats the tool payload to hundreds of KB, which is expensive and, per testing, makes it
    # *harder* for the model to reliably find the right number rather than easier.
    max_records = 30
    shown_records = [_relabel(r, relabel) for r in records[:max_records]] if relabel else records[:max_records]
    result = {
        "count": len(records),
        "aggregates": _aggregate(records, schema, relabel),
        "records": shown_records,
        "records_truncated": len(records) > max_records,
    }
    return json.dumps(result, default=_json_default)


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = f"""You are a business intelligence assistant for Skylark Drones' leadership \
team. You answer questions about the sales pipeline (Deals board) and delivery/execution \
(Work Orders board) by calling the query_work_orders and query_deals tools — never guess at \
numbers, always fetch them.

## Schema

**Work Orders** (one row per delivery/execution engagement):
- deal_name: matches the deal it originated from (see "Cross-referencing" below)
- customer_code, owner_code: internal masked codes for the client / BD-KAM owner
- sector: business sector (Mining, Powerline, Renewables, Railways, Construction, Aviation, ...)
- execution_status: delivery progress (Not Started, Ongoing, Partial Completed, Completed, Stuck, ...)
- wo_status: 'Open' or 'Closed' at a high level
- invoice_status / billing_status: billing progress (Not Billed Yet, Partially Billed, Fully Billed, Stuck, ...)
- probable_start_date, probable_end_date, data_delivery_date, po_loi_date, last_invoice_date, collection_date
- Money fields (all Rupees, masked but internally consistent) — note the tool's field names for
  these are deliberately explicit (contracted_amount_* / billed_to_date_* / etc.), not the
  generic "amount"/"billed" you might reach for by default, so match the question to the exact
  field name in the tool's response rather than the closest-sounding one:
  - contracted_amount_excl_gst / contracted_amount_incl_gst: the total CONTRACTED/PO order value
    for the whole work order (what it's worth), not what's been invoiced yet.
  - billed_to_date_excl_gst / billed_to_date_incl_gst: what has actually been INVOICED to the
    client so far. "Billed amount" / "how much have we billed" means THIS field, never
    contracted_amount_incl_gst.
  - collected_to_date_incl_gst: what has actually been COLLECTED/received from the client so far.
  - remaining_to_bill_excl_gst / remaining_to_bill_incl_gst: contracted value still remaining to
    invoice (roughly contracted amount minus billed to date).
  - outstanding_receivable: outstanding amount owed by the client right now (billed but not yet
    collected) — this is what "receivable" or "AR" means, not collected_to_date_incl_gst.

**Deals** (one row per sales pipeline opportunity):
- deal_name, owner_code, client_code
- sector: business sector
- deal_status: 'Open', 'Won', 'Dead', or 'On Hold'
- deal_stage: funnel stage from 'A. Lead Generated' through 'G. Project Won' to closed states
  like 'L. Project Lost' — the letter prefix reflects funnel order
- deal_value: Rupee value (masked but internally consistent)
- closure_probability: 'High' / 'Medium' / 'Low' (often missing)
- close_date_actual, tentative_close_date, created_date

## Data quality
Both boards were normalized before you see them (dates to ISO 8601, categorical values to a
consistent canonical casing, currency strings to plain floats), but the underlying data still
has real gaps: many deals are missing close_date_actual (only populated once actually closed)
and closure_probability; some work orders are missing billing/collection fields because they
haven't reached that stage yet. A missing categorical field appears as "Unknown"; a missing
numeric or date field appears as null/absent. Don't treat nulls as zero, and mention data-quality
caveats when they materially affect an answer (e.g. "3 of these 12 deals have no close date, so
win rate is a lower bound").

## Cross-referencing
deal_name appears on both boards and is the join key when a question spans sales + delivery
(e.g. "which Mining deals converted into active work orders"). Names are masked codenames
(e.g. "Sakura", "Scooby-Doo") and are not guaranteed globally unique, so treat matches as
approximate and say so if it matters — prefer corroborating with sector/owner_code when a name
match is ambiguous.

## Arithmetic — never compute totals yourself, read them from "aggregates"
Every query_work_orders / query_deals result includes a top-level "aggregates" object:
- "count": total matching rows.
- "sums": exact totals of every numeric field over the *full* filtered set (not just the rows
  you choose to display).
- "counts_by" / "sums_by": for each categorical field (sector, execution_status, deal_stage,
  ...), a full breakdown — count and every numeric sum — for every value of that field, already
  computed over the current filtered set.
The "records" list is for illustration only and capped at 30 rows ("records_truncated" tells
you if more exist) — "aggregates" always covers the complete filtered set regardless.
Always read the headline number you report — a total, a count, a per-category breakdown — from
"aggregates", never by mentally adding up "records" or by combining several tool calls' results
into a table by hand. Both were tested and found unreliable, even when every individual number
the model was given was correct.
In particular: for any "by sector" / "by status" / "for each of X, Y, Z" style question, make
ONE tool call (unfiltered, or filtered only to what's actually needed) and read the full
per-category table straight out of "sums_by"/"counts_by" — do not call the tool once per
category and compile the results yourself.
For a count that is "not X" and spans several categories (overdue, not yet billed, still open),
don't add those categories up either — pass `<field>_not` (e.g. execution_status_not="Completed")
or, for overdue work orders specifically, `overdue_only=true`, and read "count" directly.

## Clarifying questions
If a query is genuinely ambiguous — an undefined time period ("this quarter", "recently"), an
unclear metric ("performance", "health"), or which board a term like "status" refers to — ask a
brief clarifying question rather than silently guessing. Exception: if a sensible default is
obvious, state the default you're using in your answer and offer to adjust it, rather than
blocking on a question (e.g. "Assuming 'this quarter' means the current calendar quarter — let
me know if you meant fiscal quarter").

Keep answers concise and founder-friendly: lead with the number/answer, then brief supporting
detail. Use markdown tables for multi-row breakdowns.
Today's date is {{today}}.
"""


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

@dataclass
class AgentReply:
    text: str
    history: list[dict]


class BIAgent:
    def __init__(self, data_store: BusinessDataStore, api_key: Optional[str] = None, model: Optional[str] = None):
        if not (api_key or config.LLM_API_KEY):
            raise RuntimeError("OPENAI_API_KEY (or OPENROUTER_API_KEY) is not set.")
        self.data_store = data_store
        self.client = OpenAI(api_key=api_key or config.LLM_API_KEY, base_url=config.LLM_BASE_URL)
        self.model = model or config.LLM_MODEL

    def _system_prompt(self) -> str:
        return SYSTEM_PROMPT.replace("{today}", date.today().isoformat())

    def send_message(self, user_message: str, history: list[dict], max_tool_iterations: int = 6) -> AgentReply:
        """Runs one user turn (including any tool-use round trips) and returns the reply text
        plus the updated history to pass into the next call. `history` holds only user/assistant/
        tool turns (no system message — that's regenerated fresh each call for today's date)."""
        messages = list(history) + [{"role": "user", "content": user_message}]

        for _ in range(max_tool_iterations):
            response = self.client.chat.completions.create(
                model=self.model,
                max_tokens=2048,
                messages=[{"role": "system", "content": self._system_prompt()}] + messages,
                tools=TOOLS,
            )

            msg = response.choices[0].message

            if not msg.tool_calls:
                messages.append({"role": "assistant", "content": msg.content or ""})
                return AgentReply(text=msg.content or "", history=messages)

            messages.append({
                "role": "assistant",
                "content": msg.content,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                    }
                    for tc in msg.tool_calls
                ],
            })

            for tc in msg.tool_calls:
                tool_input = json.loads(tc.function.arguments) if tc.function.arguments else {}
                result_json = _execute_tool(self.data_store, tc.function.name, tool_input)
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result_json})

        return AgentReply(
            text="I made several tool calls but couldn't reach a final answer — could you narrow down the question?",
            history=messages,
        )


# ---------------------------------------------------------------------------
# Leadership update (deterministic metrics + optional LLM narrative)
# ---------------------------------------------------------------------------

def _sum(records: list[dict], field: str) -> float:
    return sum(r[field] for r in records if r.get(field) is not None)


def _group_sum(records: list[dict], group_field: str, value_field: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for r in records:
        key = r.get(group_field) or normalize.UNKNOWN
        val = r.get(value_field)
        if val is not None:
            out[key] = out.get(key, 0) + val
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def _count_by(records: list[dict], field: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in records:
        key = r.get(field) or normalize.UNKNOWN
        out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def compute_leadership_metrics(data_store: BusinessDataStore) -> dict:
    deals = data_store.deals()
    work_orders = data_store.work_orders()
    quality = data_store.quality_reports()

    won = [d for d in deals if d.get("deal_status") == "Won"]
    dead = [d for d in deals if d.get("deal_status") == "Dead"]
    decided = won + dead
    win_rate = round(100 * len(won) / len(decided), 1) if decided else None

    today_iso = date.today().isoformat()
    overdue_wo = [
        w for w in work_orders
        if w.get("probable_end_date") and w["probable_end_date"] < today_iso
        and w.get("execution_status") not in ("Completed",)
    ]
    at_risk_wo = [w for w in work_orders if w.get("execution_status") in ("Stuck",)]

    return {
        "generated_at": today_iso,
        "pipeline": {
            "total_open_deal_value": _sum([d for d in deals if d.get("deal_status") == "Open"], "deal_value"),
            "value_by_stage": _group_sum(deals, "deal_stage", "deal_value"),
            "value_by_sector": _group_sum(deals, "sector", "deal_value"),
            "deal_count_by_status": _count_by(deals, "deal_status"),
            "win_rate_pct": win_rate,
            "won_count": len(won),
            "dead_count": len(dead),
            "total_deals": len(deals),
        },
        "operations": {
            "wo_status_breakdown": _count_by(work_orders, "wo_status"),
            "execution_status_breakdown": _count_by(work_orders, "execution_status"),
            "billing_status_breakdown": _count_by(work_orders, "billing_status"),
            "total_work_orders": len(work_orders),
            "overdue_count": len(overdue_wo),
            "overdue_examples": [w["monday_item_name"] for w in overdue_wo[:5]],
            "at_risk_count": len(at_risk_wo),
            "at_risk_examples": [w["monday_item_name"] for w in at_risk_wo[:5]],
            "total_receivable": _sum(work_orders, "amount_receivable"),
        },
        "data_quality": quality,
    }


def _fmt_money(v: Optional[float]) -> str:
    if v is None:
        return "—"
    return f"₹{v:,.0f}"


def format_leadership_update_markdown(metrics: dict) -> str:
    p, o, dq = metrics["pipeline"], metrics["operations"], metrics["data_quality"]
    lines = [f"# Leadership Update — {metrics['generated_at']}", ""]

    lines += ["## Pipeline Summary", ""]
    lines += [f"- **Total open pipeline value:** {_fmt_money(p['total_open_deal_value'])}"]
    win_rate = f"{p['win_rate_pct']}%" if p["win_rate_pct"] is not None else "n/a (no closed deals yet)"
    lines += [f"- **Win rate:** {win_rate} ({p['won_count']} won / {p['dead_count']} dead of {p['total_deals']} total deals)", ""]

    lines += ["**Deal value by stage**", "", "| Stage | Value |", "|---|---|"]
    lines += [f"| {k} | {_fmt_money(v)} |" for k, v in p["value_by_stage"].items()]
    lines += ["", "**Deal value by sector**", "", "| Sector | Value |", "|---|---|"]
    lines += [f"| {k} | {_fmt_money(v)} |" for k, v in p["value_by_sector"].items()]

    lines += ["", "## Operational Summary", ""]
    lines += [f"- **Total work orders:** {o['total_work_orders']}"]
    lines += [f"- **Total amount receivable:** {_fmt_money(o['total_receivable'])}"]
    lines += [f"- **Overdue (past probable end date, not completed):** {o['overdue_count']}"
              + (f" — e.g. {', '.join(o['overdue_examples'])}" if o["overdue_examples"] else "")]
    lines += [f"- **At risk (status = Stuck):** {o['at_risk_count']}"
              + (f" — e.g. {', '.join(o['at_risk_examples'])}" if o["at_risk_examples"] else ""), ""]

    lines += ["**Work order status breakdown**", "", "| Status | Count |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in o["wo_status_breakdown"].items()]

    lines += ["", "## Data Quality Caveats", ""]
    for board_name, report in dq.items():
        pcts = report.get("missing_field_pcts", {})
        notable = {k: v for k, v in pcts.items() if v > 0}
        if not notable:
            lines.append(f"- **{board_name}:** no missing-field issues detected.")
            continue
        top = sorted(notable.items(), key=lambda kv: -kv[1])[:5]
        detail = "; ".join(f"{k} missing on {v}%" for k, v in top)
        lines.append(f"- **{board_name}:** {detail}.")
        if report.get("dropped_rows"):
            lines.append(f"  - {report['dropped_rows']} row(s) were dropped as likely duplicate/header artifacts.")

    return "\n".join(lines)


def generate_leadership_update(data_store: BusinessDataStore, agent: Optional[BIAgent] = None) -> str:
    """Deterministic markdown tables (always returned) + an optional one-paragraph LLM narrative
    lead-in. If the LLM call fails for any reason, we still return the reliable tables."""
    metrics = compute_leadership_metrics(data_store)
    body = format_leadership_update_markdown(metrics)

    if agent is None:
        return body

    try:
        narrative_prompt = (
            "Write a single short paragraph (3-4 sentences, no markdown headers) summarizing the "
            "key takeaways a founder should notice from this data, in plain founder-friendly "
            "language. Do not invent any numbers beyond what's given here. All monetary figures "
            "are in Indian Rupees — write them with the ₹ symbol, never $:\n\n"
            + json.dumps(metrics, default=_json_default)
        )
        response = agent.client.chat.completions.create(
            model=agent.model,
            max_tokens=400,
            messages=[{"role": "user", "content": narrative_prompt}],
        )
        narrative = (response.choices[0].message.content or "").strip()
        if narrative:
            body = body.replace(
                f"# Leadership Update — {metrics['generated_at']}",
                f"# Leadership Update — {metrics['generated_at']}\n\n{narrative}",
                1,
            )
    except Exception:
        pass

    return body
