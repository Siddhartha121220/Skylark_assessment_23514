# Decision Log — Skylark Drones Monday.com BI Agent

## Key assumptions

- **Column titles, not IDs, are the stable schema anchor.** Monday.com assigns each column an
  opaque per-account ID that doesn't exist until the account/token is set up, but column
  *titles* are known upfront because they came straight from the CSV headers used to create the
  boards. `config.py` maps `logical_field → column title` and resolves titles
  case-insensitively at runtime, so the only genuinely required setup step is generating the API
  token — no manual column-ID mapping.

- **`deal_name` is the cross-board join key**, matched by exact string equality between the
  Deals board's item name and the Work Orders board's item name. This assumes masked codenames
  (e.g. "Sakura", "Scooby-Doo") are unique *enough* to be useful, even though nothing guarantees
  global uniqueness — the agent is instructed to flag ambiguous matches rather than silently
  pick one, but a real production version would need a harder key (see "What I'd do
  differently").

- **"Overdue" and "win rate" are definitions I had to invent**, since neither exists as an
  explicit field. Overdue = `probable_end_date` in the past AND `execution_status != "Completed"`.
  Win rate = `Won ÷ (Won + Dead)`, deliberately excluding in-flight states (On Hold, Not
  Relevant, etc.) from the denominator since they aren't decided outcomes yet. Both definitions
  are centralized in one place (`agent.py`) and reused identically by chat and the Leadership
  Update, so the two surfaces can never disagree with each other.

- **Ambiguous time periods default to the current calendar quarter**, not a fiscal quarter,
  since fiscal year start wasn't specified anywhere in the data or brief. The agent states this
  assumption in its answer and offers to adjust rather than silently guessing and staying quiet
  about it.

- **Monetary values are Indian Rupees**, inferred from column naming ("Amount in Rupees...").
  Values are described as masked/anonymized in the source data, so they're treated as
  internally consistent for relative comparisons (sector A vs sector B, this month vs last) but
  not asserted to be real company revenue.

- **Single-session, single-user usage.** Data is cached in memory per Streamlit session (per the
  assignment's explicit "no database needed" allowance), not shared across users or persisted
  across restarts — appropriate for a demo, not for concurrent multi-user production use.

## Trade-offs chosen and why

- **Python.** Fastest language for gluing together an HTTP/GraphQL API, an LLM SDK, and a UI,
  with minimal boilerplate — the right priority given a same-day deadline. It's also the natural
  fit for Streamlit and every LLM SDK under consideration, so nothing fights the language choice.

- **Direct Monday.com GraphQL API over MCP.** The brief explicitly says "MCP or API — your
  choice," so this was a genuinely open decision. MCP requires standing up or configuring an
  MCP server/host — extra infrastructure with more failure points to debug under time pressure.
  A direct `requests.post()` to `https://api.monday.com/v2` is a single, well-documented HTTP
  call: easier to test, easier to reason about, easier for a reviewer to verify by reading the
  source. MCP's real payoff (a uniform interface across *many* tools/servers) doesn't show up in
  a read-only, single-integration use case. Trade-off worth naming: MCP is the more
  "modern/expected" pattern for agentic tool use and would look more forward-looking — I
  consciously traded that for reliability and speed under a deadline.

- **LLM backend: planned Anthropic Claude, shipped OpenAI-compatible API via OpenRouter — a
  decision forced by circumstance, then refined by testing.** Claude was the originally
  specified model per the assignment's suggested stack, and tool-use for a "decide which
  board(s) to query, then synthesize an answer" pattern is a natural fit for it. Partway through
  the build, both the Anthropic and OpenAI credit balances available to me ran out, so the
  actual LLM layer is the OpenAI Python SDK pointed at an OpenRouter API key instead (OpenRouter
  exposes an OpenAI-compatible endpoint for many providers/models, so switching required no
  architecture change, just a different SDK and `base_url`). Within that constraint, model
  choice was itself evidence-driven, not arbitrary: I started with `gpt-4o-mini` for cost, then
  ran the agent's answers against ground truth I computed independently from the same live data
  and found real, reproducible accuracy bugs — it would sum a list of numbers incorrectly (three
  different wrong totals across three runs of the identical question), undercount a filtered
  set by only combining some of several relevant categories, and mis-transcribe values when
  compiling several tool calls into one table. I fixed the underlying architecture regardless of
  model (pre-computed server-side aggregates in every tool result, so the model reports numbers
  instead of computing them; capped and de-duplicated the payload; renamed ambiguous
  similarly-named fields to be self-documenting), which fixed most of it — then re-tested with
  `openai/gpt-oss-120b` (OpenAI's open-weight model, also served via OpenRouter) and found it
  matched ground truth exactly across every test that had broken the smaller model, at
  comparable cost. That's the shipped default. The broader trade-off worth naming: none of this
  reliability work would have been visible without deliberately testing against independently
  computed ground truth rather than eyeballing whether answers "looked right."

- **Streamlit for the UI.** The deliverable requires a conversational interface and a hosted,
  link-accessible prototype testable without local setup. Streamlit satisfies both with the
  least code — built-in chat components, one-command local run, free one-click hosting on
  Streamlit Community Cloud. A custom React/FastAPI app would look more "production," but that's
  a bad trade against a same-day deadline: Streamlit's simplicity directly serves the
  deliverable, not just development speed.

- **No database, no MCP, no auth layer.** Each is an explicit non-requirement given read-only
  access, single-user demo context, and "must be testable without local setup." Spending limited
  hours on infrastructure the rubric doesn't ask for would come directly at the expense of the
  data-cleaning/BI logic it does ask for.

**One-sentence summary:** every choice optimizes for shipping a working, testable, hosted
prototype within a single day while staying fully compliant with the stated integration
requirements — trading some architectural sophistication (MCP, a custom UI, persistence, a
locked-in model provider) for speed, reliability, and — once accuracy problems surfaced —
correctness verified against ground truth rather than assumed.

## What I'd do differently with more time

- **A real automated test suite**, not ad hoc manual scripts. The ground-truth comparisons that
  caught the LLM accuracy bugs were written by hand, one question at a time, during the build.
  With more time, that becomes a `pytest` regression suite (fixed set of questions + expected
  aggregates) that runs on every change, so a future prompt tweak or model swap can't silently
  reintroduce the bugs already found and fixed.
- **A harder cross-board join.** `deal_name` string matching is a heuristic, not a guarantee.
  With write access or more setup time, I'd add a Monday.com "linked item"/mirror column
  connecting each Work Order back to its originating Deal by ID, and use that instead of a
  name-match.
- **A fiscal-calendar-aware time model.** "This quarter" currently defaults to calendar
  quarters; a real deployment would ask the org's fiscal year convention once and remember it,
  rather than re-stating the calendar-quarter assumption on every ambiguous question.
- **Locked-in, budget-independent model choice.** The model ended up being decided partly by
  which provider still had credits, which is not how a production system should choose its LLM.
  I'd re-run the same ground-truth suite across two or three candidate models up front and pick
  based on that alone.
- **Persistent conversation history** across reloads/sessions (currently in-memory per Streamlit
  session only), and a visible trace in the UI of which tool filters the model actually used, for
  debuggability and user trust.
- **Load/scale testing** beyond the ~350-item boards used here — pagination is implemented but
  not stress-tested against a multi-thousand-item board.

## How I interpreted "leadership updates"

I treated this as a **distinct, structured output mode**, not just another chat answer, on the
theory that a document a founder might actually copy into a real update needs to be reliable
every time it's generated, not merely "usually right." Concretely: clicking "Generate Leadership
Update" produces a markdown report with a Pipeline Summary (open pipeline value, win rate, value
by stage, value by sector), an Operational Summary (work order status breakdown, overdue/at-risk
counts with examples, total receivable), and an explicit Data Quality Caveats section (missing-
field percentages per board, any rows dropped as bad data) — directly matching the assignment's
own suggested content. The one narrative paragraph at the top is the only part the LLM
generates; every number in the rest of the document is computed deterministically in plain
Python from the same normalized data the chat agent uses, and the LLM is explicitly told not to
introduce any figure beyond what it's handed. This mirrors the lesson from the accuracy testing
described above: minimize how much of the output depends on an LLM doing arithmetic, especially
in a document meant to be trusted and reused as-is.
