"""
Thin GraphQL client for the Monday.com API v2.

Kept deliberately dependency-light (just `requests`) per the assignment's "no MCP server"
constraint. Every public function raises one of the typed exceptions below instead of letting
requests exceptions / KeyErrors bubble up, so callers (agent.py, app.py) can catch a single
`MondayError` and show a friendly message instead of a stack trace.
"""
import re
import time
from typing import Any, Optional

import requests

import config


class MondayError(Exception):
    """Base class for all Monday.com client errors — safe to show to end users."""


class MondayAuthError(MondayError):
    pass


class MondayRateLimitError(MondayError):
    pass


class MondayAPIError(MondayError):
    pass


ITEMS_PAGE_LIMIT = 100
REQUEST_TIMEOUT_SECS = 30
MAX_RETRIES = 3


def _headers() -> dict:
    if not config.MONDAY_API_TOKEN:
        raise MondayAuthError(
            "MONDAY_API_TOKEN is not set. Generate one in Monday.com "
            "(Avatar -> Admin/Developer -> API -> Generate token) and add it to your .env file."
        )
    return {
        "Authorization": config.MONDAY_API_TOKEN,
        "Content-Type": "application/json",
        "API-Version": "2024-10",
    }


def _run_query(query: str, variables: Optional[dict] = None) -> dict:
    """POST a GraphQL query/mutation, retrying transient failures and translating errors."""
    payload: dict[str, Any] = {"query": query}
    if variables:
        payload["variables"] = variables

    last_exc: Optional[Exception] = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(
                config.MONDAY_API_URL, json=payload, headers=_headers(), timeout=REQUEST_TIMEOUT_SECS
            )
        except requests.exceptions.RequestException as exc:
            last_exc = exc
            time.sleep(min(2 ** attempt, 8))
            continue

        if resp.status_code == 401:
            raise MondayAuthError(
                "Monday.com rejected the API token (401 Unauthorized). Double-check "
                "MONDAY_API_TOKEN in your .env file and that it hasn't expired."
            )
        if resp.status_code == 429:
            retry_after = float(resp.headers.get("Retry-After", 2 * attempt))
            if attempt == MAX_RETRIES:
                raise MondayRateLimitError(
                    "Monday.com API rate limit hit repeatedly. Please wait a minute and try again."
                )
            time.sleep(retry_after)
            continue
        if resp.status_code >= 500:
            last_exc = MondayAPIError(f"Monday.com server error (HTTP {resp.status_code}).")
            time.sleep(min(2 ** attempt, 8))
            continue
        if resp.status_code >= 400:
            raise MondayAPIError(f"Monday.com API request failed (HTTP {resp.status_code}): {resp.text[:500]}")

        try:
            body = resp.json()
        except ValueError as exc:
            raise MondayAPIError("Monday.com returned a non-JSON response.") from exc

        if "errors" in body and body["errors"]:
            messages = "; ".join(e.get("message", str(e)) for e in body["errors"])
            lowered = messages.lower()
            if "unauthorized" in lowered or "not authenticated" in lowered:
                raise MondayAuthError(f"Monday.com authentication error: {messages}")
            raise MondayAPIError(f"Monday.com API returned an error: {messages}")

        return body.get("data", {})

    raise MondayAPIError(
        f"Could not reach Monday.com after {MAX_RETRIES} attempts (network error). "
        f"Last error: {last_exc}"
    )


def list_boards() -> list[dict]:
    """Diagnostic helper: fetch every board the token can see, with column id/title/type.

    This is what `list_boards.py` calls to print a copy-pasteable summary once the human has
    generated MONDAY_API_TOKEN — used to fill in board IDs in .env (column IDs are not needed
    as config; see config.py docstring).
    """
    # Note: deliberately omits `items_count` — on some accounts that field alone is expensive
    # enough to exhaust the entire per-minute complexity budget in a single call.
    query = """
    query {
        boards(limit: 200) {
            id
            name
            state
            columns {
                id
                title
                type
            }
        }
    }
    """
    data = _run_query(query)
    return data.get("boards", [])


def _normalize_for_match(text: str) -> str:
    """Lowercase and collapse underscores/hyphens to spaces, so 'Work_Order_Tracker Data'
    still matches a hint like 'work order'."""
    return re.sub(r"[_\-]+", " ", text.lower())


def find_board_by_name(name_hint: str) -> Optional[dict]:
    """Case/separator-insensitive substring match on board name; used when no board ID is
    configured explicitly."""
    normalized_hint = _normalize_for_match(name_hint)
    for board in list_boards():
        if normalized_hint in _normalize_for_match(board["name"]):
            return board
    return None


_ITEMS_QUERY = """
query GetItems($boardId: ID!, $limit: Int!) {
    boards(ids: [$boardId]) {
        items_page(limit: $limit) {
            cursor
            items {
                id
                name
                column_values {
                    id
                    text
                    value
                    type
                    column {
                        title
                    }
                }
            }
        }
    }
}
"""

_NEXT_ITEMS_QUERY = """
query GetNextItems($cursor: String!, $limit: Int!) {
    next_items_page(cursor: $cursor, limit: $limit) {
        cursor
        items {
            id
            name
            column_values {
                id
                text
                value
                type
                column {
                    title
                }
            }
        }
    }
}
"""


def get_board_items(board_id: str, limit: int = ITEMS_PAGE_LIMIT) -> list[dict]:
    """Fetch all items on a board, transparently paging through Monday's cursor-based API.

    Returns a list of raw item dicts: {id, name, column_values: [{id, title, text, value, type}]}.
    Callers should pass this straight into normalize.py rather than reading column_values by hand.
    """
    data = _run_query(_ITEMS_QUERY, {"boardId": str(board_id), "limit": limit})
    boards = data.get("boards") or []
    if not boards:
        raise MondayAPIError(f"Board {board_id} was not found or is not accessible with this token.")

    items_page = boards[0]["items_page"]
    items = list(items_page["items"])
    cursor = items_page.get("cursor")

    while cursor:
        data = _run_query(_NEXT_ITEMS_QUERY, {"cursor": cursor, "limit": limit})
        page = data.get("next_items_page")
        if not page:
            break
        items.extend(page["items"])
        cursor = page.get("cursor")

    return [_flatten_item(item) for item in items]


def _flatten_item(item: dict) -> dict:
    """Reshape column_values (list of {id, title, text, value, type}) into {title: {...}}."""
    columns = {}
    for cv in item.get("column_values", []):
        title = (cv.get("column") or {}).get("title") or cv["id"]
        columns[title] = {"text": cv.get("text"), "value": cv.get("value"), "type": cv.get("type")}
    return {"id": item["id"], "name": item.get("name"), "columns": columns}
