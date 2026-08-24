import ast
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.auth_session import (
    ACCESS_REFRESH_SKEW_SECONDS,
    AUTH_BROWSER_COMMAND_KEY,
    AUTH_RETENTION_SECONDS,
    browser_refresh_session,
    cloud_user_from_state,
    cloud_user_to_state,
    parse_persisted_refresh_session,
    queue_refresh_token_clear,
    queue_refresh_token_write,
    resolve_auth_session,
    take_browser_command,
)
from src.cloud_store import (
    CloudSessionExpiredError,
    CloudStoreError,
    CloudUser,
)


ROOT = Path(__file__).resolve().parents[1]


def function_source(name: str) -> str:
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"Function not found: {name}")


class AuthSessionTests(unittest.TestCase):
    def setUp(self):
        self.now = 2_000_000_000.0
        self.current = CloudUser(
            "user-a",
            "person@example.com",
            "access-old",
            "refresh-old",
            int(self.now + 60),
            3600,
        )
        self.rotated = CloudUser(
            "user-a",
            "person@example.com",
            "access-new",
            "refresh-new",
            int(self.now + 3600),
            3600,
        )

    def test_cloud_user_expiry_round_trips_through_streamlit_state(self):
        self.assertEqual(cloud_user_from_state(cloud_user_to_state(self.current)), self.current)

    def test_login_write_persists_only_refresh_token_and_timestamp(self):
        state = {"essay_input": "keep essay", "report": {"score": 7}}
        queue_refresh_token_write(state, "refresh-secret", now=self.now)

        self.assertEqual(
            state[AUTH_BROWSER_COMMAND_KEY],
            {
                "action": "write",
                "refresh_token": "refresh-secret",
                "saved_at": self.now,
            },
        )
        self.assertNotIn("access_token", state[AUTH_BROWSER_COMMAND_KEY])
        self.assertNotIn("email", state[AUTH_BROWSER_COMMAND_KEY])
        self.assertEqual(state["essay_input"], "keep essay")
        self.assertEqual(state["report"], {"score": 7})

    def test_near_expiry_access_token_is_refreshed_once_and_rotated(self):
        store = Mock()
        store.refresh.return_value = self.rotated

        result = resolve_auth_session(store, self.current, {}, now=self.now)

        self.assertEqual(result.user, self.rotated)
        self.assertTrue(result.persist_refresh)
        self.assertTrue(result.state_changed)
        store.refresh.assert_called_once_with("refresh-old")

    def test_legacy_session_with_refresh_token_is_upgraded_once(self):
        legacy = CloudUser(
            "user-a", "person@example.com", "access-old", "refresh-old"
        )
        store = Mock()
        store.refresh.return_value = self.rotated

        result = resolve_auth_session(store, legacy, {}, now=self.now)

        self.assertEqual(result.user, self.rotated)
        self.assertTrue(result.persist_refresh)
        store.refresh.assert_called_once_with("refresh-old")

    def test_streamlit_session_loss_restores_from_browser_once(self):
        store = Mock()
        store.refresh.return_value = self.rotated
        browser_value = {
            "status": "loaded",
            "refresh_token": "refresh-old",
            "saved_at": self.now - 30,
        }

        result = resolve_auth_session(store, None, browser_value, now=self.now)

        self.assertEqual(result.user, self.rotated)
        self.assertTrue(result.persist_refresh)
        store.refresh.assert_called_once_with("refresh-old")

    def test_rotated_refresh_token_replaces_old_pending_value(self):
        state = {}
        queue_refresh_token_write(state, "refresh-old", now=self.now - 10)
        queue_refresh_token_write(state, "refresh-new", now=self.now)
        command = take_browser_command(state)

        self.assertEqual(command["refresh_token"], "refresh-new")
        self.assertNotIn(AUTH_BROWSER_COMMAND_KEY, state)

    def test_invalid_refresh_token_clears_login(self):
        store = Mock()
        store.refresh.side_effect = CloudSessionExpiredError("expired", status_code=400)

        result = resolve_auth_session(store, self.current, {}, now=self.now)

        self.assertIsNone(result.user)
        self.assertTrue(result.clear_persisted)
        store.refresh.assert_called_once()

    def test_refresh_token_older_than_seven_days_is_rejected_without_request(self):
        browser_value = {
            "status": "loaded",
            "refresh_token": "refresh-old",
            "saved_at": self.now - AUTH_RETENTION_SECONDS,
        }
        store = Mock()

        result = resolve_auth_session(store, None, browser_value, now=self.now)

        self.assertIsNone(result.user)
        self.assertTrue(result.clear_persisted)
        store.refresh.assert_not_called()

    def test_transient_refresh_failure_does_not_clear_valid_recovery_token(self):
        store = Mock()
        store.refresh.side_effect = CloudStoreError("temporarily unavailable")
        browser_value = {
            "status": "loaded",
            "refresh_token": "refresh-old",
            "saved_at": self.now - 30,
        }

        result = resolve_auth_session(store, None, browser_value, now=self.now)

        self.assertIsNone(result.user)
        self.assertFalse(result.clear_persisted)
        store.refresh.assert_called_once()

    @patch("src.auth_session._AUTH_COMPONENT")
    def test_logout_clear_ignores_stale_component_token(self, component):
        component.return_value = SimpleNamespace(
            auth_session={
                "status": "loaded",
                "refresh_token": "stale-refresh",
                "saved_at": self.now,
            }
        )
        state = {}
        queue_refresh_token_clear(state)
        command = take_browser_command(state)

        browser_value = browser_refresh_session(command)
        result = resolve_auth_session(Mock(), None, browser_value, now=self.now)

        self.assertEqual(command, {"action": "clear"})
        self.assertEqual(browser_value, {})
        self.assertIsNone(result.user)

    def test_browser_record_validation_rejects_future_or_oversized_values(self):
        future = {
            "status": "loaded",
            "refresh_token": "token",
            "saved_at": self.now + ACCESS_REFRESH_SKEW_SECONDS + 1,
        }
        oversized = {
            "status": "loaded",
            "refresh_token": "x" * 8193,
            "saved_at": self.now,
        }
        self.assertEqual(parse_persisted_refresh_session(future, now=self.now), (None, True))
        self.assertEqual(parse_persisted_refresh_session(oversized, now=self.now), (None, True))

    def test_silent_restore_has_no_first_login_or_route_side_effects(self):
        restore_source = function_source("restore_cloud_user_session")
        complete_source = function_source("complete_login")
        logout_source = function_source("logout_cloud_user")

        for forbidden in (
            "complete_login",
            "claim_guest_result",
            "login_completed",
            "navigate(",
            "login_return_route",
        ):
            self.assertNotIn(forbidden, restore_source)
        self.assertIn("claim_guest_result", complete_source)
        self.assertIn('"login_completed"', complete_source)
        self.assertIn("queue_refresh_token_clear", logout_source)

    def test_browser_persistence_source_contains_no_access_token_or_personal_content(self):
        source = (ROOT / "src" / "auth_session.py").read_text(encoding="utf-8")
        component_source = source[source.index("_AUTH_COMPONENT ="):source.index("@dataclass")]
        self.assertNotIn("access_token", component_source)
        self.assertNotIn("email", component_source)
        self.assertNotIn("essay_input", component_source)
        self.assertNotIn("report_json", component_source)
        self.assertIn('window.addEventListener("online", wake)', component_source)
        self.assertIn('document.addEventListener("visibilitychange"', component_source)


if __name__ == "__main__":
    unittest.main()
