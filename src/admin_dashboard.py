"""Private developer dashboard and robust Streamlit route detection."""

import hmac
import os
from urllib.parse import parse_qs, urlsplit

import streamlit as st

from src.analytics import load_analytics


def _contains_admin_flag(value: object) -> bool:
    if isinstance(value, (list, tuple)):
        return any(str(item) == "1" for item in value)
    return str(value) == "1"


def is_admin_request() -> bool:
    """Detect ?admin=1 across current and legacy Streamlit query APIs."""
    try:
        if _contains_admin_flag(st.query_params.get("admin")):
            return True
    except (AttributeError, KeyError, TypeError):
        pass

    legacy_reader = getattr(st, "experimental_get_query_params", None)
    if callable(legacy_reader):
        try:
            if _contains_admin_flag(legacy_reader().get("admin")):
                return True
        except (AttributeError, KeyError, TypeError):
            pass

    try:
        request_url = st.context.url
        return _contains_admin_flag(parse_qs(urlsplit(request_url).query).get("admin"))
    except (AttributeError, TypeError, ValueError):
        return False


def _admin_password() -> str | None:
    """Read the admin password from Streamlit Secrets, with a local env fallback."""
    try:
        value = st.secrets["ADMIN_PASSWORD"]
    except (FileNotFoundError, KeyError):
        value = os.getenv("ADMIN_PASSWORD")
    return str(value) if value not in (None, "") else None


def render_admin_dashboard() -> None:
    """Render password protection followed by anonymous usage metrics."""
    st.title("EssayPilot Developer Dashboard")
    expected_password = _admin_password()
    if not expected_password:
        st.error("ADMIN_PASSWORD is not configured in Streamlit Secrets.")
        return

    if not st.session_state.get("admin_authenticated"):
        password = st.text_input("Admin password", type="password")
        if not password:
            st.info("Enter the developer password to view anonymous usage statistics.")
            return
        if not hmac.compare_digest(password, expected_password):
            st.error("Incorrect password.")
            return
        st.session_state.admin_authenticated = True

    analytics = load_analytics()
    first_row = st.columns(3)
    first_row[0].metric("Total Users", analytics["total_users"])
    first_row[1].metric("Total Essays", analytics["total_essays"])
    first_row[2].metric("Essays Today", analytics["essays_today"])
    second_row = st.columns(2)
    average_band = analytics["average_band"]
    average_words = analytics["average_word_count"]
    second_row[0].metric("Average Band", f"{average_band:.2f}" if average_band is not None else "-")
    second_row[1].metric("Average Word Count", f"{average_words:.0f}" if average_words is not None else "-")

    st.subheader("Recent Activity")
    recent_rows = [
        {
            "Time": event.get("timestamp", ""),
            "Band": event.get("overall_band"),
            "Words": event.get("essay_word_count"),
            "Model": event.get("model_name", ""),
        }
        for event in analytics["recent_activity"]
    ]
    if recent_rows:
        st.dataframe(recent_rows, width="stretch", hide_index=True)
    else:
        st.info("No grading activity has been recorded yet.")
