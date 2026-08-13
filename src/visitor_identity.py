"""Privacy-light browser identity for one guest grading trial per device."""

from __future__ import annotations

import hashlib
import re

import streamlit as st


VISITOR_STORAGE_KEY = "essaypilot_visitor_id_v1"
_VISITOR_ID_PATTERN = re.compile(r"^[a-f0-9-]{32,36}$", re.IGNORECASE)

_VISITOR_COMPONENT = st.components.v2.component(
    "essaypilot_visitor_identity",
    html="<span hidden aria-hidden=\"true\"></span>",
    js=f"""
    export default function({{ setStateValue }}) {{
      const key = {VISITOR_STORAGE_KEY!r};
      let visitorId = window.localStorage.getItem(key);
      if (!visitorId) {{
        visitorId = window.crypto.randomUUID();
        window.localStorage.setItem(key, visitorId);
      }}
      setStateValue("visitor_id", visitorId);
    }}
    """,
)


def browser_visitor_id() -> str:
    """Return the persistent random browser identifier, or an empty loading value."""
    result = _VISITOR_COMPONENT(
        key="essaypilot_visitor_identity",
        default={"visitor_id": ""},
        on_visitor_id_change=lambda: None,
    )
    value = str(getattr(result, "visitor_id", "") or "").strip()
    return value if _VISITOR_ID_PATTERN.fullmatch(value) else ""


def visitor_hash(visitor_id: str) -> str:
    """Create the only representation of a visitor identifier stored server-side."""
    if not _VISITOR_ID_PATTERN.fullmatch(visitor_id):
        return ""
    return hashlib.sha256(f"essaypilot-guest-v1:{visitor_id}".encode("utf-8")).hexdigest()

