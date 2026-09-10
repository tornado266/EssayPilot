"""Keep existing HTML entry links inside the original Streamlit session."""
from urllib.parse import parse_qs, urlsplit

import streamlit as st


NAVIGATION_JS = """
export default function({ setTriggerValue }) {
  const clicked = event => {
    if (event.defaultPrevented || event.button !== 0 || event.ctrlKey ||
        event.metaKey || event.shiftKey || event.altKey) return;
    const link = event.target.closest('a.ep-home-action__link, a.ep-home-preview, a.ep-report-start__demo');
    if (!link || link.hasAttribute('download') ||
        (link.target && link.target !== '_self')) return;
    const href = link.getAttribute('href') || '';
    if (!href.startsWith('?')) return;
    const url = new URL(href, location.href);
    if (!['home','write','report','training','growth','demo'].includes(url.searchParams.get('page'))) return;
    event.preventDefault();
    setTriggerValue('route', href);
  };
  document.addEventListener('click', clicked);
  return () => document.removeEventListener('click', clicked);
}
"""


def parse_navigation(value: object) -> tuple[str, str, str] | None:
    if not isinstance(value, str) or not value.startswith('?') or len(value) > 512:
        return None
    parsed = urlsplit(value)
    if parsed.fragment:
        return None
    query = parse_qs(parsed.query, keep_blank_values=True)
    if set(query) - {'page', 'run_id', 'mode'} or any(len(v) != 1 for v in query.values()):
        return None
    page = query.get('page', [''])[0]
    if page not in {'home', 'write', 'report', 'training', 'growth', 'demo'}:
        return None
    return page, query.get('run_id', [''])[0], query.get('mode', [''])[0]


def internal_navigation_event() -> tuple[str, str, str] | None:
    component = st.components.v2.component(
        'essaypilot_internal_navigation', html='<span hidden aria-hidden="true"></span>',
        js=NAVIGATION_JS,
    )
    result = component(key='essaypilot_internal_navigation', on_route_change=lambda: None)
    return parse_navigation(getattr(result, 'route', None))
