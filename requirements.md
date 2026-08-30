# Requirements Document: Monday.com Business Intelligence Agent

## Project Context
This is a technical assignment for Skylark Drones. The goal is to build a conversational AI agent that answers founder-level business questions by querying live data from two Monday.com boards (Work Orders and Deals). Deadline is same-day — prioritize a working end-to-end prototype over polish.

## Current Status (IMPORTANT — read before building)
- ✅ Two Monday.com boards have already been created and CSV data imported: **"Work Orders"** and **"Deals"**.
- ❌ **Monday.com API token has NOT been generated yet.** Do not assume it exists. Build the app to read the token from an environment variable (`MONDAY_API_TOKEN`), and the app must fail gracefully with a clear error message if it's missing, rather than crashing.
- ❌ Exact board IDs and column IDs are not yet known — the agent must discover these dynamically via the Monday.com API on startup (or accept them as config), NOT have them hardcoded blindly. Include a small setup/diagnostic script that lists all boards + column IDs/types the user has access to, so they can be plugged into a config file after the token is generated.

## Objective
Build a conversational agent that:
1. Connects to Monday.com via API (GraphQL) and pulls data from the Work Orders and Deals boards **dynamically at query time** — never hardcode CSV data into the app.
2. Cleans/normalizes messy real-world data (nulls, inconsistent date formats, inconsistent naming/casing, whitespace, etc.) before reasoning over it.
3. Answers natural-language business questions (revenue, pipeline health, sector performance, operational status) by combining data from both boards where relevant.
4. Asks clarifying questions when a query is ambiguous (e.g., "this quarter" is undefined — should default sensibly AND offer to clarify).
5. Optionally generates a "leadership update" — a structured summary of key metrics suitable for pasting into an exec update.

## Tech Stack (use this unless there's a strong reason not to)
- **Backend/agent logic:** Python
- **LLM:** Anthropic Claude API (`anthropic` Python SDK) — use tool use / function calling for the Monday.com queries
- **Monday.com access:** Monday.com GraphQL API v2 (`https://api.monday.com/v2`) via direct HTTP requests (`requests` library) — do NOT require MCP server setup, since this needs to be simple to host and demo
- **UI:** Streamlit (single-page chat interface) — fastest to build and deploy
- **Hosting:** Streamlit Community Cloud (or Hugging Face Spaces as backup)
- **Secrets:** `.env` file locally (`python-dotenv`), and Streamlit "Secrets" for deployment. Required secrets:
  - `MONDAY_API_TOKEN`
  - `ANTHROPIC_API_KEY`

## Required Architecture

### 1. Monday.com Client Module (`monday_client.py`)
- Function to authenticate using `MONDAY_API_TOKEN` (from env). If missing/invalid, raise a clear, user-facing error — do not crash silently.
- Function `list_boards()` — diagnostic helper to fetch all accessible boards, their IDs, and column definitions (id, title, type). Used for initial setup once the API token is created.
- Function `get_board_items(board_id)` — fetches all items (rows) from a given board with all column values, handling Monday's pagination (`items_page`, `cursor`) since boards can exceed the default item limit.
- Should NOT assume board IDs are hardcoded — read them from a config file (`config.py` or `.env`) that gets filled in after the API token exists and `list_boards()` has been run once.

### 2. Data Normalization Layer (`normalize.py`)
This is a critical grading criterion — do not skip or leave entirely to the LLM.
- Parse dates from multiple possible formats into a single standard (ISO 8601).
- Trim whitespace, standardize casing for categorical fields (e.g., sector names, status names) — build a small mapping/alias table for known variants (e.g., "Energy", "energy ", "ENERGY" → "Energy").
- Handle nulls/blank cells explicitly — represent as `None`/`"Unknown"` rather than empty strings, and track which fields were missing per record so the agent can mention data quality caveats.
- Coerce numeric fields (deal value, order value) that may be strings with currency symbols/commas into clean floats.
- Output: a clean, normalized list of dict records per board, plus a small "data quality report" (e.g., counts of missing fields) that the agent can reference when answering.

### 3. Agent / Tool-Calling Layer (`agent.py`)
- Use Claude with tool use. Define at least two tools exposed to the model:
  - `query_work_orders(filters?)` — returns normalized Work Orders data (optionally filtered by sector/status/date range if the model requests it)
  - `query_deals(filters?)` — returns normalized Deals data (optionally filtered similarly)
- System prompt must describe:
  - The schema/columns of both boards and what they mean
  - Known data quality issues (so the model doesn't panic on nulls and knows to caveat)
  - That the model should cross-reference both boards when a question spans sales + delivery (e.g., "which energy sector deals converted into active work orders")
  - Instruction to ask a clarifying question when a query is genuinely ambiguous (e.g., undefined time period, unclear metric) rather than guessing silently
- Agent should maintain conversation history across turns (simple in-memory list is fine for the prototype).

### 4. Leadership Update Feature
- Add a distinct mode/command in the UI (e.g., a button or a recognized phrase like "prepare a leadership update") that generates a structured summary, not just a chat answer. Include:
  - Pipeline summary: total deal value by stage and by sector, win rate if derivable
  - Operational summary: work order status breakdown, any overdue/at-risk items
  - Explicit data quality caveats section (e.g., "12% of deals are missing a close date")
- Output should be formatted cleanly (markdown) so it could be copy-pasted into a real update.

### 5. Conversational UI (`app.py`, Streamlit)
- Simple chat interface: message history, text input, streaming or at least clear loading state.
- A visible "Generate Leadership Update" button/action separate from free-form chat.
- On startup, if `MONDAY_API_TOKEN` or `ANTHROPIC_API_KEY` is missing, show a clear setup instructions screen instead of a broken app.
- Display data quality caveats returned by the agent visibly (not buried).

## Error Handling Requirements
- Monday.com API failures (auth error, rate limit, network error) must be caught and surfaced as a friendly message, not a stack trace.
- Empty board / zero items should be handled gracefully (agent says so, doesn't crash).
- Missing config (board IDs not yet set) should short-circuit to a helpful "run setup first" message.

## Explicit Non-Requirements (don't over-build given time constraints)
- No need for write access to Monday.com — read-only.
- No need for user authentication/login on the Streamlit app itself.
- No need for a database — pull fresh from Monday.com on each session/query (cache in-memory per session if useful for speed).
- No need for MCP protocol implementation — direct API calls are acceptable and explicitly allowed by the assignment.

## Deliverables the Coding Agent Should Produce
1. Full source code (Python project: `monday_client.py`, `normalize.py`, `agent.py`, `app.py`, `config.py`, `requirements.txt`, `.env.example`)
2. `README.md` covering: architecture overview, setup steps (including **how to generate a Monday.com API token and run `list_boards()` to populate config**, since the token doesn't exist yet), how to run locally, how it's deployed
3. Clear inline comments explaining the normalization logic and tool-calling flow, since these will be referenced in a separate Decision Log document (written by the human, not the coding agent)

## Immediate Next Step (blocking)
The human will generate the Monday.com API token from **Monday.com → Avatar → Admin → API → Generate token** (or Developer settings) and provide it as `MONDAY_API_TOKEN`. Until then, the coding agent should build everything to the point of "ready to run once token is supplied," and the `list_boards()` diagnostic script should be one of the first things ready to test the moment the token exists.
