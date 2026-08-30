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

> Setting up the Monday.com boards themselves (column types, structure, importing your CSVs) is
> a prerequisite covered separately below in **[Monday.com board setup guide](#mondaycom-board-setup-guide)**.
> The steps here assume the two boards already exist and you just need to point the app at them.

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

## Monday.com board setup guide

<details>
<summary><strong>Click to expand — full step-by-step guide to building the two boards from scratch with the recommended column types</strong></summary>

> **How this relates to the boards this app actually talks to:** the app was built and tested
> against boards that already existed for this assignment (Monday board names
> `Work_Order_Tracker Data` and `Deal funnel Data`, discovered live via `list_boards.py`), whose
> column titles and types don't exactly match the structure below (e.g. their `Sector` is a
> Status column rather than Dropdown, `Deal Stage` uses lettered stages A–O, there's no
> `Weighted Value` formula column, etc. — run `list_boards.py` to see the exact live schema).
> This guide is the **recommended structure for setting up the two boards cleanly from a blank
> workspace** — useful if you're standing this up fresh with your own CSVs. If you follow it,
> update the column-title mappings in `config.py`'s `WORK_ORDERS_SCHEMA` / `DEALS_SCHEMA`
> `fields` dicts to match the column titles you actually created (`config.py` matches by column
> *title*, not rigid structure — see its docstring — so that's the only code change needed).

### Board 1 — Pipeline

#### Step 1 — Create the board

Create a new board and name it **Sales Pipeline**. Don't import the Excel yet — build the
column structure first.

#### Step 2 — Keep the first column

Monday automatically gives you an Item column. Rename it to **Opportunity Name** (type:
Name/Item). It should contain values like `ABC Energy Project`, `XYZ Power Expansion`, `Tata
Solar Deal`.

#### Step 3 — Add these columns, in order

| # | Column | Monday type |
|---|---|---|
| 1 | Opportunity Name | Name |
| 2 | Customer | Text |
| 3 | Sector | Dropdown |
| 4 | Sales Owner | People |
| 5 | Deal Stage | Status |
| 6 | Deal Value | Numbers |
| 7 | Probability | Numbers |
| 8 | Weighted Value | Formula |
| 9 | Expected Close Date | Date |
| 10 | Quarter | Dropdown |
| 11 | Region | Dropdown |
| 12 | Lead Source | Dropdown |
| 13 | Last Activity | Date |
| 14 | Next Action | Long Text |
| 15 | Notes | Long Text |

Add these from the **+** button (Column Center) beside the last column.

#### Step 4 — Configure Sector

Click **Sector → Settings/Edit labels** and create: `Energy`, `Manufacturing`, `Technology`,
`Healthcare`, `Finance`, `Infrastructure`, `Government`, `Other`.

This must be a **Dropdown**, not Text — a Dropdown restricts values to a predefined label set,
so a later query like "how is our pipeline looking for Energy?" can reliably filter on
`Sector = Energy` instead of fighting free-text variants.

#### Step 5 — Configure Deal Stage

Use a **Status** column (not Dropdown — Deal Stage represents the *state* of the opportunity).
Name it **Deal Stage** with labels: `Lead`, `Qualified`, `Discovery`, `Proposal`, `Negotiation`,
`Contract`, `Won`, `Lost`.

#### Step 6 — Deal Value

Add a **Numbers** column named **Deal Value**, currency set to match your data (e.g. ₹). Numbers
columns support built-in sum/average/min/max/count, so the board itself can surface a running
"Total Pipeline" figure.

#### Step 7 — Probability

Add another **Numbers** column named **Probability**. Store raw values (`20`, `40`, `60`, `80`,
`100`) rather than strings like `"20%"`, then set the column's display format to percentage.

#### Step 8 — Weighted Value

Add a **Formula** column named **Weighted Value**:

```
{Deal Value} * {Probability} / 100
```

Example: Deal Value = ₹50,00,000, Probability = 60 → Weighted Value = ₹30,00,000.

#### Step 9 — Expected Close Date

Add a **Date** column named **Expected Close Date**. Make sure source dates are in a real date
format before import — Monday's Excel importer expects a valid date format and recommends ISO
(`2021-10-23`).

#### Step 10 — Quarter

Add a **Dropdown** (not Text) named **Quarter** with labels `Q1`, `Q2`, `Q3`, `Q4`.

#### Step 11 — Region

Add a **Dropdown** named **Region**: `North`, `South`, `East`, `West`, `International` (adjust
to your actual business geography).

#### Step 12 — Lead Source

Add a **Dropdown** named **Lead Source**: `Website`, `Referral`, `Cold Outreach`, `Partner`,
`Existing Customer`, `Event`, `Other` (adjust to match your data).

#### Pipeline board — final structure

```
Opportunity Name   ← NAME
Customer           ← TEXT
Sector             ← DROPDOWN
Sales Owner        ← PEOPLE
Deal Stage         ← STATUS
Deal Value         ← NUMBERS
Probability        ← NUMBERS
Weighted Value     ← FORMULA
Expected Close Date← DATE
Quarter            ← DROPDOWN
Region             ← DROPDOWN
Lead Source        ← DROPDOWN
Last Activity      ← DATE
Next Action        ← LONG TEXT
Notes              ← LONG TEXT
```

### Board 2 — Work Orders

Create a second board named **Work Orders**. Again, build the column structure before
importing.

#### Step 1 — First column

Rename the Item column to **Work Order** (e.g. `WO-001`, `WO-002`, `WO-003`).

#### Step 2 — Add these columns

| # | Column | Monday type |
|---|---|---|
| 1 | Work Order | Name |
| 2 | Customer | Text |
| 3 | Project | Text |
| 4 | Sector | Dropdown |
| 5 | PO Number | Text |
| 6 | PO Date | Date |
| 7 | Quantity as per PO | Numbers |
| 8 | Quantity Delivered | Numbers |
| 9 | Balance Quantity | Formula |
| 10 | Unit Price | Numbers |
| 11 | PO Value | Numbers |
| 12 | Work Order Status | Status |
| 13 | Delivery Status | Status |
| 14 | Planned Delivery Date | Date |
| 15 | Actual Delivery Date | Date |
| 16 | Owner | People |
| 17 | Remarks | Long Text |

#### ⚠️ Important — "Quantity as per PO" and "Quantity Delivered"

Both must be **Numbers**, never Text or Dropdown — they need to support arithmetic (e.g.
Quantity as per PO = 1,000, Quantity Delivered = 650 → Balance Quantity = 350).

#### Step 4 — Balance Quantity

Add a **Formula** column named **Balance Quantity**:

```
{Quantity as per PO} - {Quantity Delivered}
```

Monday calculates this automatically — don't import a value into it.

#### Step 5 — Unit Price

**Numbers** column, currency set to ₹ (or whatever matches your data).

#### Step 6 — PO Value

**Numbers** column. Import directly from Excel if present, or compute it as a **Formula**
(`{Quantity as per PO} * {Unit Price}`) if not.

#### Step 7 — Work Order Status

**Status** column with labels: `Not Started`, `In Progress`, `On Hold`, `Completed`,
`Cancelled`.

#### Step 8 — Delivery Status

A **separate Status** column (independent of Work Order Status) with labels: `Not Scheduled`,
`Scheduled`, `Partially Delivered`, `Fully Delivered`, `Delayed`. These are intentionally
independent — "Work Order = In Progress" and "Delivery = Partially Delivered" can both be true
at once.

#### Step 9 — Dates

Use real **Date** columns (not Text) for PO Date, Planned Delivery Date, Actual Delivery Date.

#### Step 10 — Sector

Use the **same Dropdown label set** as the Pipeline board (`Energy`, `Manufacturing`,
`Technology`, `Healthcare`, `Finance`, `Infrastructure`, `Government`, `Other`). Consistency
here matters — if one board says `Energy` and another says `Energy Sector`, cross-board
sector analysis gets unnecessarily hard.

#### Work Orders board — final structure

```
Work Order              ← NAME
Customer                ← TEXT
Project                 ← TEXT
Sector                  ← DROPDOWN
PO Number               ← TEXT
PO Date                 ← DATE
Quantity as per PO      ← NUMBERS
Quantity Delivered      ← NUMBERS
Balance Quantity        ← FORMULA
Unit Price              ← NUMBERS
PO Value                ← NUMBERS
Work Order Status       ← STATUS
Delivery Status         ← STATUS
Planned Delivery Date   ← DATE
Actual Delivery Date    ← DATE
Owner                   ← PEOPLE
Remarks                 ← LONG TEXT
```

### Now import your Excel files

Once the columns exist, use **New Item ▼ → Import Items → Excel** on each board. Monday will
show your Excel columns and let you map each one to an existing board column — creating the
columns beforehand (rather than importing blind) is what makes this mapping step reliable.

**Pipeline mapping**

| Excel column | → Monday column |
|---|---|
| Opportunity Name | Opportunity Name |
| Customer | Customer |
| Sector | Sector |
| Sales Owner | Sales Owner |
| Stage | Deal Stage |
| Deal Value | Deal Value |
| Probability | Probability |
| Expected Close | Expected Close Date |
| Quarter | Quarter |
| Region | Region |
| Lead Source | Lead Source |
| Last Activity | Last Activity |
| Next Action | Next Action |
| Notes | Notes |

**Work Order mapping**

| Excel column | → Monday column |
|---|---|
| Work Order | Work Order |
| Customer | Customer |
| Project | Project |
| Sector | Sector |
| PO Number | PO Number |
| PO Date | PO Date |
| Quantities as per PO | Quantity as per PO |
| Quantity Delivered | Quantity Delivered |
| Unit Price | Unit Price |
| PO Value | PO Value |
| Work Order Status | Work Order Status |
| Delivery Status | Delivery Status |
| Planned Delivery Date | Planned Delivery Date |
| Actual Delivery Date | Actual Delivery Date |
| Owner | Owner |
| Remarks | Remarks |

Do **not** import a column for `Balance Quantity` — it's a Monday Formula column and calculates
itself.

**⚠️ During import**, when Monday asks *"Import unmapped columns?"* — leave this **off** unless
you deliberately want a new column. Leaving it on can silently create duplicates like
`Quantity as per PO 2` / `Quantity as per PO 3`.

### After import — verify these

**Pipeline:** Sector = Dropdown · Deal Stage = Status · Deal Value = Numbers · Probability =
Numbers · Weighted Value = Formula · Expected Close Date = Date · Quarter = Dropdown

**Work Orders:** Quantity as per PO = Numbers · Quantity Delivered = Numbers · Balance Quantity
= Formula · PO Value = Numbers · Work Order Status = Status · Delivery Status = Status · PO
Date / Planned Delivery Date / Actual Delivery Date = Date

</details>

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
