"""Streamlit chat UI for the Monday.com Business Intelligence Agent."""
import os

import streamlit as st

# Bridge Streamlit Community Cloud's "Secrets" (st.secrets, from secrets.toml) into os.environ
# *before* importing config, since config.py reads plain env vars at import time via
# python-dotenv. Locally, .env already populates os.environ so this is a no-op; st.secrets is
# only present at all when a secrets.toml exists (local .streamlit/secrets.toml or the cloud
# platform's injected one) — absent entirely otherwise, hence the try/except.
try:
    for _key, _value in st.secrets.items():
        os.environ.setdefault(_key, str(_value))
except Exception:
    pass

import config
from agent import BIAgent, BusinessDataStore, generate_leadership_update
from monday_client import MondayError

st.set_page_config(page_title="Skylark BI Agent", page_icon="📊", layout="wide")


def _setup_instructions():
    st.title("📊 Skylark Drones — BI Agent")
    st.error("Setup required before this app can run.")
    missing = []
    if not config.MONDAY_API_TOKEN:
        missing.append("`MONDAY_API_TOKEN`")
    if not config.LLM_API_KEY:
        missing.append("`OPENAI_API_KEY` (or `OPENROUTER_API_KEY`)")
    st.markdown(f"**Missing secret(s):** {', '.join(missing)}")
    st.markdown(
        """
### How to fix this

1. Copy `.env.example` to `.env` in the project root.
2. Get a Monday.com API token: **Monday.com → your avatar (bottom-left) → Admin/Developer
   settings → API → Generate token** (needs read access to the *Work Orders* and *Deals*
   boards), and set it as `MONDAY_API_TOKEN`.
3. Get an LLM API key — either an OpenAI key from **platform.openai.com → API Keys**, or an
   OpenRouter key from **openrouter.ai → Keys** (OpenRouter keys are auto-detected by their
   `sk-or-` prefix) — and set it as `OPENAI_API_KEY`.
4. (Optional) Run `python list_boards.py` to confirm the boards are visible and copy their IDs
   into `MONDAY_WORK_ORDERS_BOARD_ID` / `MONDAY_DEALS_BOARD_ID` in `.env` — the app will also
   try to auto-detect them by name if you skip this.
5. Restart the app.

See `README.md` for full setup details.
"""
    )
    st.stop()


if not config.MONDAY_API_TOKEN or not config.LLM_API_KEY:
    _setup_instructions()


@st.cache_resource(show_spinner=False)
def _get_data_store() -> BusinessDataStore:
    return BusinessDataStore()


@st.cache_resource(show_spinner=False)
def _get_agent() -> BIAgent:
    return BIAgent(_get_data_store())


data_store = _get_data_store()

st.title("📊 Skylark Drones — BI Agent")
st.caption("Ask about pipeline, deliveries, sectors, or overdue work orders — pulled live from Monday.com.")

with st.sidebar:
    st.subheader("Data")
    if st.button("🔄 Refresh from Monday.com"):
        data_store.refresh()
        st.cache_resource.clear()
        st.rerun()

    st.divider()
    st.subheader("Data quality")
    try:
        reports = data_store.quality_reports()
        for board_name, report in reports.items():
            with st.expander(f"{board_name} ({report['total_records']} records)"):
                if report["dropped_rows"]:
                    st.warning(f"{report['dropped_rows']} row(s) dropped as likely bad data.")
                notable = {k: v for k, v in report["missing_field_pcts"].items() if v > 0}
                if notable:
                    st.write("Missing-field rates:")
                    st.dataframe(
                        {"field": list(notable.keys()), "% missing": list(notable.values())},
                        hide_index=True, use_container_width=True,
                    )
                else:
                    st.write("No missing-field issues detected.")
    except MondayError as exc:
        st.error(str(exc))
    except Exception as exc:  # noqa: BLE001 - surface any unexpected fetch error, don't crash the sidebar
        st.error(f"Could not load data quality report: {exc}")

    st.divider()
    generate_clicked = st.button("📋 Generate Leadership Update", use_container_width=True)

if "history" not in st.session_state:
    st.session_state.history = []  # Anthropic-format message history
if "display_messages" not in st.session_state:
    st.session_state.display_messages = []  # [(role, content_markdown)] for rendering

for role, content in st.session_state.display_messages:
    with st.chat_message(role):
        st.markdown(content)

if generate_clicked:
    with st.chat_message("assistant"):
        with st.spinner("Pulling latest data and building the update..."):
            try:
                agent = _get_agent()
                update_md = generate_leadership_update(data_store, agent)
            except MondayError as exc:
                update_md = f"⚠️ Could not generate the update: {exc}"
            except Exception as exc:  # noqa: BLE001
                update_md = f"⚠️ Unexpected error generating the update: {exc}"
        st.markdown(update_md)
    st.session_state.display_messages.append(("assistant", update_md))

user_message = st.chat_input("Ask a question, e.g. 'What's our win rate in Mining this year?'")
if user_message:
    st.session_state.display_messages.append(("user", user_message))
    with st.chat_message("user"):
        st.markdown(user_message)

    with st.chat_message("assistant"):
        with st.spinner("Thinking..."):
            try:
                agent = _get_agent()
                reply = agent.send_message(user_message, st.session_state.history)
                st.session_state.history = reply.history
                answer = reply.text
            except MondayError as exc:
                answer = f"⚠️ Monday.com error: {exc}"
            except Exception as exc:  # noqa: BLE001 - last-resort guard so the UI never shows a raw traceback
                answer = f"⚠️ Something went wrong: {exc}"
        st.markdown(answer)
    st.session_state.display_messages.append(("assistant", answer))
