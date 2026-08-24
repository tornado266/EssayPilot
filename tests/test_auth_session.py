import ast
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.auth_session import (
    ACCESS_REFRESH_SKEW_SECONDS,
    AUTH_BROWSER_COMMAND_KEY,
    AUTH_BROWSER_VERSION_KEY,
    AUTH_LOGOUT_PENDING_KEY,
    AUTH_RETENTION_SECONDS,
    PersistedRefreshSession,
    acknowledge_browser_command,
    apply_browser_command_to_record,
    begin_logout,
    browser_refresh_session,
    browser_signaled_logout,
    cloud_user_from_state,
    cloud_user_to_state,
    parse_persisted_refresh_session,
    queue_refresh_token_clear,
    queue_refresh_token_write,
    resolve_auth_session,
    start_logout_with_remote_best_effort,
    take_browser_command,
)
from src.cloud_store import CloudSessionExpiredError, CloudStoreError, CloudUser


ROOT = Path(__file__).resolve().parents[1]


def function_source(name: str) -> str:
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
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
            "expires_at",
        )
        self.rotated = CloudUser(
            "user-a",
            "person@example.com",
            "access-new",
            "refresh-new",
            int(self.now + 3600),
            3600,
            "expires_at",
        )

    def browser_value(self, token="refresh-old", version=10, age=30):
        return {
            "status": "loaded",
            "refresh_token": token,
            "saved_at": self.now - age,
            "version": version,
        }

    def test_cloud_user_expiry_round_trips_through_streamlit_state(self):
        self.assertEqual(cloud_user_from_state(cloud_user_to_state(self.current)), self.current)

    def test_write_without_ack_remains_pending_and_is_resent(self):
        state = {
            "essay_input": "keep essay",
            "report": {"score": 7},
            AUTH_BROWSER_VERSION_KEY: 9,
        }
        queue_refresh_token_write(state, "refresh-secret", now=self.now)
        first = take_browser_command(state)
        second = take_browser_command(state)

        self.assertEqual(first, second)
        self.assertEqual(first["action"], "write")
        self.assertTrue(first["command_id"])
        self.assertGreater(first["version"], 0)
        self.assertEqual(first["expected_version"], 9)
        self.assertIn(AUTH_BROWSER_COMMAND_KEY, state)
        self.assertNotIn("access_token", first)
        self.assertNotIn("email", first)
        self.assertEqual(state["essay_input"], "keep essay")

    def test_matching_write_ack_is_required_before_pending_is_deleted(self):
        state = {}
        queue_refresh_token_write(state, "refresh-secret", now=self.now)
        command = take_browser_command(state)
        mismatched = {"status": "written", "command_id": "other"}

        self.assertIsNone(acknowledge_browser_command(state, mismatched, now=self.now))
        self.assertIn(AUTH_BROWSER_COMMAND_KEY, state)

        ack = {
            "status": "written",
            "command_id": command["command_id"],
            "refresh_token": command["refresh_token"],
            "saved_at": command["saved_at"],
            "version": command["version"],
        }
        result = acknowledge_browser_command(state, ack, now=self.now)

        self.assertEqual(result.status, "written")
        self.assertNotIn(AUTH_BROWSER_COMMAND_KEY, state)
        self.assertEqual(state[AUTH_BROWSER_VERSION_KEY], command["version"])

    def test_clear_without_ack_stays_logout_pending_and_blocks_restore_path(self):
        state = {AUTH_BROWSER_VERSION_KEY: 12}
        begin_logout(state, reason="user", expected_version=12)
        command = take_browser_command(state)

        self.assertEqual(command["action"], "clear")
        self.assertEqual(command["expected_version"], 12)
        self.assertIn(AUTH_BROWSER_COMMAND_KEY, state)
        self.assertIn(AUTH_LOGOUT_PENDING_KEY, state)
        restore_source = function_source("restore_cloud_user_session")
        self.assertLess(
            restore_source.index("AUTH_LOGOUT_PENDING_KEY"),
            restore_source.index("resolve_auth_session("),
        )

    def test_matching_clear_ack_deletes_command_but_not_newer_mismatch(self):
        state = {AUTH_BROWSER_VERSION_KEY: 12}
        begin_logout(state, reason="user", expected_version=12)
        command = take_browser_command(state)
        ack = {
            "status": "cleared",
            "command_id": command["command_id"],
            "version": 12,
        }

        result = acknowledge_browser_command(state, ack, now=self.now)

        self.assertEqual(result.status, "cleared")
        self.assertNotIn(AUTH_BROWSER_COMMAND_KEY, state)
        self.assertIn(AUTH_LOGOUT_PENDING_KEY, state)

    def test_expected_version_mismatch_skips_clear_and_keeps_new_record(self):
        newer = PersistedRefreshSession("refresh-new", self.now, 22)
        command = {
            "action": "clear",
            "command_id": "clear-1",
            "expected_version": 21,
        }

        remaining, ack = apply_browser_command_to_record(newer, command)

        self.assertEqual(remaining, newer)
        self.assertEqual(ack["status"], "skipped_newer")
        self.assertEqual(ack["refresh_token"], "refresh-new")

    def test_old_tab_prefers_newer_browser_token_and_never_clears_it(self):
        store = Mock()
        store.refresh.return_value = self.rotated

        result = resolve_auth_session(
            store,
            self.current,
            self.browser_value(token="refresh-newer-tab", version=11),
            current_version=10,
            now=self.now,
        )

        self.assertEqual(result.user, self.rotated)
        self.assertFalse(result.clear_persisted)
        store.refresh.assert_called_once_with("refresh-newer-tab")

    def test_storage_change_can_sync_newer_token_or_logout_other_tabs(self):
        store = Mock()
        store.refresh.return_value = self.rotated
        changed = self.browser_value(token="refresh-other-tab", version=15)
        changed["source"] = "storage"

        result = resolve_auth_session(
            store, self.current, changed, current_version=10, now=self.now
        )

        self.assertEqual(result.user, self.rotated)
        self.assertTrue(browser_signaled_logout({
            "status": "storage_cleared", "source": "storage", "version": 15
        }))

    def test_pending_write_cannot_resurrect_token_after_other_tab_logout(self):
        state = {AUTH_BROWSER_VERSION_KEY: 10}
        queue_refresh_token_write(state, "rotated-token", now=self.now)
        command = take_browser_command(state)

        remaining, ack = apply_browser_command_to_record(None, command)

        self.assertIsNone(remaining)
        self.assertEqual(ack["status"], "skipped_cleared")
        self.assertTrue(browser_signaled_logout(ack))

    def test_missing_or_malformed_expiry_does_not_refresh_on_repeated_resolution(self):
        unknown = CloudUser(
            "user-a", "person@example.com", "not-a-jwt", "refresh-old",
            0, 0, "unknown",
        )
        malformed_state = cloud_user_to_state(unknown)
        malformed_state["expires_at"] = "broken"
        self.assertIsNone(cloud_user_from_state(malformed_state))
        store = Mock()

        first = resolve_auth_session(store, unknown, {}, now=self.now)
        second = resolve_auth_session(store, first.user, {}, now=self.now + 1)

        self.assertEqual(second.user, unknown)
        store.refresh.assert_not_called()

    def test_near_expiry_access_token_refreshes_once_and_rotates(self):
        store = Mock()
        store.refresh.return_value = self.rotated

        result = resolve_auth_session(
            store, self.current, {}, current_version=10, now=self.now
        )

        self.assertEqual(result.user, self.rotated)
        self.assertTrue(result.persist_refresh)
        store.refresh.assert_called_once_with("refresh-old")

    def test_invalid_refresh_starts_conditional_clear(self):
        store = Mock()
        store.refresh.side_effect = CloudSessionExpiredError("expired", status_code=400)

        result = resolve_auth_session(
            store, self.current, {}, current_version=10, now=self.now
        )

        self.assertIsNone(result.user)
        self.assertTrue(result.clear_persisted)
        self.assertEqual(result.clear_expected_version, 10)
        store.refresh.assert_called_once()

    def test_session_loss_restores_and_transient_error_can_later_succeed(self):
        store = Mock()
        store.refresh.side_effect = [CloudStoreError("temporary"), self.rotated]
        value = self.browser_value()

        first = resolve_auth_session(store, None, value, now=self.now)
        second = resolve_auth_session(store, None, value, now=self.now)

        self.assertTrue(first.recovery_pending)
        self.assertFalse(first.clear_persisted)
        self.assertEqual(second.user, self.rotated)
        self.assertEqual(store.refresh.call_count, 2)

    def test_refresh_token_older_than_seven_days_is_rejected_without_request(self):
        value = self.browser_value(age=AUTH_RETENTION_SECONDS)
        store = Mock()

        result = resolve_auth_session(store, None, value, now=self.now)

        self.assertTrue(result.clear_persisted)
        store.refresh.assert_not_called()

    def test_expired_access_without_refresh_token_is_cleared(self):
        partial = CloudUser(
            "user-a", "person@example.com", "expired-access", "",
            int(self.now - 1), 3600, "expires_at",
        )

        result = resolve_auth_session(Mock(), partial, {}, now=self.now)

        self.assertIsNone(result.user)
        self.assertTrue(result.state_changed)

    def test_remote_logout_failure_still_starts_ack_backed_local_logout(self):
        state = {AUTH_BROWSER_VERSION_KEY: 10}
        remote = Mock(side_effect=CloudStoreError("offline"))

        start_logout_with_remote_best_effort(
            state, self.current, remote, expected_version=10
        )

        remote.assert_called_once_with(self.current)
        self.assertIn(AUTH_LOGOUT_PENDING_KEY, state)
        self.assertEqual(take_browser_command(state)["action"], "clear")

    @patch("src.auth_session._AUTH_COMPONENT")
    def test_component_returns_ack_for_non_read_commands(self, component):
        component.return_value = SimpleNamespace(
            auth_session={"status": "cleared", "command_id": "clear-1"}
        )
        value = browser_refresh_session({
            "action": "clear", "command_id": "clear-1", "expected_version": 0
        })
        self.assertEqual(value["command_id"], "clear-1")

    def test_browser_record_validation_supports_legacy_version(self):
        legacy = self.browser_value()
        legacy.pop("version")
        parsed, should_clear = parse_persisted_refresh_session(legacy, now=self.now)

        self.assertFalse(should_clear)
        self.assertEqual(parsed.version, 0)
        future = self.browser_value()
        future["saved_at"] = self.now + ACCESS_REFRESH_SKEW_SECONDS + 1
        self.assertEqual(parse_persisted_refresh_session(future, now=self.now), (None, True))

    def test_silent_restore_has_no_first_login_or_route_side_effects(self):
        restore_source = function_source("restore_cloud_user_session")
        complete_source = function_source("complete_login")
        for forbidden in (
            "complete_login",
            "claim_guest_result",
            "login_completed",
            "login_return_mode",
        ):
            self.assertNotIn(forbidden, restore_source)
        self.assertIn("claim_guest_result", complete_source)
        self.assertIn('"login_completed"', complete_source)

    def test_browser_component_is_versioned_minimal_and_listens_for_storage(self):
        source = (ROOT / "src" / "auth_session.py").read_text(encoding="utf-8")
        component = source[source.index("_AUTH_COMPONENT ="):source.index("@dataclass")]
        self.assertNotIn("access_token", component)
        self.assertNotIn("email", component)
        self.assertNotIn("essay_input", component)
        self.assertNotIn("report_json", component)
        self.assertIn('window.addEventListener("storage", storageChanged)', component)
        self.assertIn("expectedVersion", component)
        self.assertIn("command_id", component)


if __name__ == "__main__":
    unittest.main()
