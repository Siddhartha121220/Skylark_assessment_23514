# Skylark Drones — Monday.com BI Agent

A conversational agent that answers founder-level business questions by querying live data from
two Monday.com boards ("Work Orders" and "Deals"), normalizing it, and reasoning over it with
an LLM (tool use via the OpenAI API). Built as a same-day technical assignment prototype.

## Architecture

```
config.py        Env/secrets loading + schema knowledge (logical field -> Monday column title,
                  sector/status alias tables). See its docstring for why we map by column
                  *title* instead of hardcoded column IDs.
monday_client.py  Minimal GraphQL client for the Monday.com API v2 (requests, no MCP). Handles
                  auth errors, rate limits, retries, and cursor-based pagination via items_page /
                  next_items_page.
normalize.py      Deterministic data cleaning: date parsing -> ISO 8601, currency/comma stripping
                  -> float, categorical alias/casing normalization, explicit null handling, and a
                  per-board "data quality report" (missing-field %, dropped junk rows).
agent.py          BusinessDataStore (fetch + normalize + in-memory cache per session) and BIAgent
                  (OpenAI function-calling loop over query_work_orders / query_deals). Also computes the
                  Leadership Update: metrics are calculated in plain Python (exact, reproducible),
                  and the model only writes a short narrative on top of numbers it's handed.
app.py            Streamlit chat UI: setup-instructions screen if secrets are missing, sidebar
                  data-quality panel, "Generate Leadership Update" button, free-form chat.
list_boards.py    One-time diagnostic script — run once MONDAY_API_TOKEN exists to confirm board
                  visibility and (optionally) grab board IDs for .env.
```

**Why column titles, not column IDs, for schema mapping:** Monday assigns each column an opaque
ID (e.g. `text_mkr2x7fq`) that doesn't exist until the token is generated and boards are
inspected. Column *titles*, however, are known today because they came straight from the CSV
headers used to create the boards ("Sector", "Deal Stage", "Masked Deal value", ...). So
`config.py` maps `logical_field -> column title`, and `normalize.py` resolves titles
case-insensitively against whatever the live board actually has. Net effect: the only genuinely
optional config is the two board IDs (and even those auto-resolve by name if left blank) — no
manual column-ID mapping step is needed after the token is created.

## Setup

### 1. Install dependencies

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Get your secrets

```bash
cp .env.example .env
```

- **Monday.com API token:** Monday.com → your avatar (bottom-left) → **Admin** (or **Developer**)
  → **API** → **Generate token** (Personal API token is fine; needs read access to the *Work
  Orders* and *Deals* boards). Paste it into `.env` as `MONDAY_API_TOKEN`.
- **LLM API key:** either a real OpenAI key (platform.openai.com → API Keys) or an OpenRouter
  key (openrouter.ai → Keys) — paste either one into `.env` as `OPENAI_API_KEY`. OpenRouter
  keys are auto-detected by their `sk-or-` prefix and routed to `https://openrouter.ai/api/v1`
  instead of OpenAI directly (see `config.py`), which is useful if you'd rather spend an
  OpenRouter balance than OpenAI/Anthropic credits directly.

### 3. Confirm board access and (optionally) pin board IDs

```bash
python list_boards.py
```

This lists every board the token can see, with IDs and columns, and flags anything that looks
like the Work Orders / Deals boards by name. The app will auto-detect these two boards by name
at runtime even if you skip this step — but if you have multiple similarly-named boards, copy
the correct IDs into `.env`:

```
MONDAY_WORK_ORDERS_BOARD_ID=123456789
MONDAY_DEALS_BOARD_ID=123456790
```

### 4. Run locally

```bash
streamlit run app.py
```

Opens at `http://localhost:8501`. If either secret is missing, the app shows a setup-instructions
screen instead of crashing.

## Deployment (Streamlit Community Cloud)

1. Push this repo to GitHub (make sure `.env` is **not** committed — it's in `.gitignore`).
2. On share.streamlit.io, create a new app pointing at `app.py`.
3. In the app's **Settings → Secrets**, paste:
   ```toml
   MONDAY_API_TOKEN = "..."
   OPENAI_API_KEY = "..."
   MONDAY_WORK_ORDERS_BOARD_ID = "..."   # optional
   MONDAY_DEALS_BOARD_ID = "..."         # optional
   ```
4. Deploy. (Hugging Face Spaces works the same way via its Secrets UI, as a backup host.)

## Using the app

- **Chat:** ask things like "What's our total pipeline value in Powerline?", "Which work orders
  are overdue?", "Which Mining deals converted into active work orders?" The agent decides when
  to call `query_work_orders` / `query_deals` and cross-references by `deal_name` when a question
  spans both boards.
- **Ambiguous questions:** if you ask something like "how are we doing this quarter", the agent
  will state the default it's assuming (current calendar quarter) and offer to adjust, or ask
  outright if there's no sensible default.
- **Leadership Update button:** produces a markdown summary (pipeline by stage/sector, win rate,
  work order status breakdown, overdue/at-risk items, and explicit data-quality caveats) that's
  copy-paste ready. The numbers are computed deterministically in Python, not by the LLM.
- **Data quality panel (sidebar):** shows per-board missing-field rates and any rows dropped as
  likely bad data (e.g. duplicated header rows found in the source CSV import).
- **Refresh button:** drops the in-memory cache and re-pulls fresh data from Monday.com.

## Error handling

- Missing `MONDAY_API_TOKEN` / `OPENAI_API_KEY` (or `OPENROUTER_API_KEY`) → setup-instructions
  screen, not a crash.
- Monday auth errors (bad/expired token), rate limits, and network errors are caught in
  `monday_client.py` and surfaced as plain-English messages (with automatic retry on transient
  failures).
- Missing/unresolvable board config → clear "run list_boards.py first" style error.
- Empty boards / zero matching records → the agent reports zero results rather than erroring.

## Known limitations (given the same-day scope)

- Conversation history is in-memory per Streamlit session only (no persistence across reloads).
- Cross-board joins use `deal_name` string matching (masked codenames aren't guaranteed unique);
  the agent is instructed to flag ambiguous matches rather than silently pick one.
- Sector/status alias tables in `config.py` are seeded from the real values observed in the
  source data plus common variants — a genuinely novel label not in the alias table still gets
  generic casing/whitespace cleanup but may not collapse into an existing canonical bucket.
