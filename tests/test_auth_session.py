import ast
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from streamlit.testing.v1 import AppTest

from src.auth_session import (
    ACCESS_REFRESH_SKEW_SECONDS,
    AUTH_BROWSER_COMMAND_KEY,
    AUTH_BROWSER_RECOVERY_KEY,
    AUTH_BROWSER_VERSION_KEY,
    AUTH_BROWSER_TOMBSTONES_KEY,
    AUTH_LISTENER_RERUN_KEY,
    AUTH_LOGOUT_PENDING_KEY,
    AUTH_RECOVERY_STATE_KEY,
    AUTH_REQUEST_RERUN_KEY,
    AUTH_BROWSER_READ_EPOCH_KEY,
    AUTH_PERSIST_WARNING_KEY,
    AUTH_USER_VERSION_KEY,
    AUTH_RETENTION_SECONDS,
    PersistedRefreshSession,
    _browser_component_key,
    acknowledge_browser_command,
    apply_browser_command_to_record,
    begin_logout,
    browser_ack_needs_listener_rerun,
    browser_bootstrap_transition,
    browser_refresh_session,
    browser_signaled_logout,
    cloud_user_from_state,
    cloud_user_to_state,
    consume_auth_request_rerun,
    mark_browser_listener_stable,
    parse_persisted_refresh_session,
    queue_refresh_token_clear,
    queue_refresh_token_write,
    resolve_auth_session,
    start_logout_with_remote_best_effort,
    take_browser_command,
)
from src.cloud_store import CloudSessionExpiredError, CloudStoreError, CloudUser, SupabaseStore


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

    def test_clear_only_matches_expected_or_exact_superseded_writes(self):
        state = {AUTH_BROWSER_VERSION_KEY: 10}
        first_version = queue_refresh_token_write(
            state, "refresh-first", now=self.now
        )
        first_command = take_browser_command(state)
        second_version = queue_refresh_token_write(
            state, "refresh-second", now=self.now + 1
        )
        second_command = take_browser_command(state)

        self.assertEqual(
            second_command["superseded_write_versions"], [first_version]
        )
        begin_logout(state, reason="invalid", expected_version=10)
        clear = take_browser_command(state)

        self.assertEqual(clear["expected_version"], 10)
        self.assertEqual(
            clear["superseded_write_versions"], [first_version, second_version]
        )
        self.assertNotIn("clear_through_version", clear)

        other_tab = PersistedRefreshSession(
            "refresh-other-tab", self.now + 0.5, first_version + 1
        )
        remaining, other_ack = apply_browser_command_to_record(other_tab, clear)
        self.assertEqual(remaining, other_tab)
        self.assertEqual(other_ack["status"], "skipped_newer")

        own_pending = PersistedRefreshSession(
            second_command["refresh_token"],
            second_command["saved_at"],
            second_version,
        )
        remaining, own_ack = apply_browser_command_to_record(own_pending, clear)
        self.assertIsNone(remaining)
        self.assertEqual(own_ack["status"], "cleared")

    def test_valid_access_adopts_newer_browser_token_without_refresh(self):
        store = Mock()
        valid = CloudUser(
            "user-a", "person@example.com", "access-valid", "refresh-old",
            int(self.now + 3600), 3600, "expires_at",
        )

        result = resolve_auth_session(
            store,
            valid,
            self.browser_value(token="refresh-newer-tab", version=11),
            current_version=10,
            now=self.now,
        )

        self.assertEqual(result.user.access_token, "access-valid")
        self.assertEqual(result.user.refresh_token, "refresh-newer-tab")
        self.assertEqual(result.browser_version, 11)
        self.assertFalse(result.clear_persisted)
        store.refresh.assert_not_called()

    def test_alternating_storage_events_converge_without_refresh(self):
        store = Mock()
        user = CloudUser(
            "user-a", "person@example.com", "access-valid", "refresh-r1",
            int(self.now + 3600), 3600, "expires_at",
        )
        version = 10
        for token, next_version in (
            ("refresh-r2", 15),
            ("refresh-r1", 10),
            ("refresh-r2", 15),
        ):
            changed = self.browser_value(token=token, version=next_version)
            changed["source"] = "storage"
            result = resolve_auth_session(
                store, user, changed, current_version=version, now=self.now
            )
            user = result.user
            version = max(version, result.browser_version)

        self.assertEqual(user.refresh_token, "refresh-r2")
        self.assertEqual(version, 15)
        store.refresh.assert_not_called()
        listener_state = {AUTH_USER_VERSION_KEY: 15}
        listener = take_browser_command(listener_state)
        cleared = {
            "status": "storage_cleared",
            "source": "storage",
            "read_epoch": listener["read_epoch"],
            "version": 15,
        }
        stable = mark_browser_listener_stable(listener_state, listener, cleared)
        self.assertTrue(browser_signaled_logout(
            cleared,
            has_current_user=True,
            command=listener,
            listener_stable=stable,
            current_version=15,
        ))

    def test_expired_access_uses_newer_browser_token_once(self):
        expired = CloudUser(
            "user-a", "person@example.com", "expired", "refresh-r1",
            int(self.now - 1), 3600, "expires_at",
        )
        store = Mock()
        store.refresh.return_value = self.rotated

        result = resolve_auth_session(
            store,
            expired,
            self.browser_value(token="refresh-r2", version=15),
            current_version=10,
            now=self.now,
        )

        self.assertEqual(result.user, self.rotated)
        store.refresh.assert_called_once_with("refresh-r2")

    def test_pending_write_cannot_resurrect_token_after_other_tab_logout(self):
        state = {AUTH_BROWSER_VERSION_KEY: 10}
        queue_refresh_token_write(state, "rotated-token", now=self.now)
        command = take_browser_command(state)

        remaining, ack = apply_browser_command_to_record(
            None, command, tombstone_versions=[command["version"]]
        )

        self.assertIsNone(remaining)
        self.assertEqual(ack["status"], "skipped_cleared")
        matched = acknowledge_browser_command(state, ack, now=self.now)
        self.assertEqual(matched.status, "skipped_cleared")
        self.assertTrue(browser_signaled_logout(
            ack,
            has_current_user=True,
            ack=matched,
        ))

    def test_exact_tombstone_blocks_only_the_cleared_login_write(self):
        state = {}
        first_version = queue_refresh_token_write(
            state, "refresh-first-login", now=self.now
        )
        delayed_write = dict(take_browser_command(state))
        self.assertEqual(delayed_write["expected_version"], 0)

        begin_logout(state, reason="user", expected_version=0)
        clear = take_browser_command(state)
        self.assertIn(first_version, clear["tombstone_versions"])

        record, clear_value = apply_browser_command_to_record(None, clear)
        clear_ack = acknowledge_browser_command(state, clear_value, now=self.now)
        active_tombstones = clear_value["tombstone_versions"]
        self.assertEqual(clear_ack.status, "cleared")
        self.assertEqual(
            state[AUTH_BROWSER_TOMBSTONES_KEY], active_tombstones
        )

        record, delayed_value = apply_browser_command_to_record(
            record, delayed_write, tombstone_versions=active_tombstones
        )
        self.assertIsNone(record)
        self.assertEqual(delayed_value["status"], "skipped_cleared")

        independent_version = first_version - 1
        independent_write = {
            "action": "write",
            "command_id": "other-tab-login",
            "refresh_token": "refresh-other-tab",
            "saved_at": self.now,
            "version": independent_version,
            "expected_version": independent_version - 1,
        }
        record, independent_value = apply_browser_command_to_record(
            record,
            independent_write,
            tombstone_versions=active_tombstones,
        )
        self.assertEqual(independent_value["status"], "written")
        self.assertEqual(record.refresh_token, "refresh-other-tab")
        self.assertLess(independent_version, max(active_tombstones))

        next_state = {AUTH_BROWSER_TOMBSTONES_KEY: active_tombstones}
        next_version = queue_refresh_token_write(
            next_state, "refresh-next-login", now=self.now + 1
        )
        next_write = take_browser_command(next_state)
        self.assertGreater(next_version, max(active_tombstones))
        record, next_value = apply_browser_command_to_record(
            record, next_write, tombstone_versions=active_tombstones
        )
        self.assertEqual(next_value["status"], "written")
        self.assertEqual(record.refresh_token, "refresh-next-login")

    def test_clear_state_cannot_be_overwritten_by_late_write(self):
        state = {AUTH_BROWSER_VERSION_KEY: 10}
        begin_logout(state, reason="invalid", expected_version=10)
        clear = take_browser_command(state)

        queue_refresh_token_write(
            state, "late-rotated-token", now=self.now, request_rerun=True
        )

        self.assertEqual(take_browser_command(state), clear)
        self.assertNotIn(AUTH_REQUEST_RERUN_KEY, state)

    def test_write_ack_reruns_once_then_read_mode_is_listener_stable(self):
        state = {}
        queue_refresh_token_write(
            state, "refresh-new", now=self.now, request_rerun=True
        )
        command = take_browser_command(state)
        record, value = apply_browser_command_to_record(None, command)
        self.assertIsNotNone(record)
        ack = acknowledge_browser_command(state, value, now=self.now)

        self.assertTrue(browser_ack_needs_listener_rerun(state, ack))
        self.assertFalse(browser_ack_needs_listener_rerun(state, ack))
        self.assertNotIn(AUTH_REQUEST_RERUN_KEY, state)
        read_command = take_browser_command(state)
        self.assertEqual(read_command["action"], "read")
        listener_value = self.browser_value(token="refresh-new", version=record.version)
        listener_value.update(
            {"source": "read", "read_epoch": read_command["read_epoch"]}
        )
        self.assertTrue(
            mark_browser_listener_stable(state, read_command, listener_value)
        )
        self.assertNotIn(AUTH_LISTENER_RERUN_KEY, state)

    def test_skipped_newer_write_ack_also_returns_to_listener_once(self):
        state = {AUTH_BROWSER_VERSION_KEY: 10}
        queue_refresh_token_write(state, "refresh-stale", now=self.now)
        command = take_browser_command(state)
        newer = PersistedRefreshSession("refresh-newer", self.now + 1, command["version"] + 1)
        _record, value = apply_browser_command_to_record(newer, command)
        ack = acknowledge_browser_command(state, value, now=self.now + 1)

        self.assertEqual(ack.status, "skipped_newer")
        self.assertTrue(browser_ack_needs_listener_rerun(state, ack))
        self.assertFalse(browser_ack_needs_listener_rerun(state, ack))

    def test_401_persistence_rerun_flag_is_one_shot(self):
        state = {}
        queue_refresh_token_write(
            state, "refresh-new", now=self.now, request_rerun=True
        )
        self.assertTrue(consume_auth_request_rerun(state))
        self.assertFalse(consume_auth_request_rerun(state))

    def test_loading_and_unavailable_browser_bootstrap_are_bounded(self):
        state = {}
        self.assertEqual(
            browser_bootstrap_transition(state, {"status": "loading"}), "wait"
        )
        self.assertNotIn(AUTH_BROWSER_RECOVERY_KEY, state)
        self.assertEqual(
            browser_bootstrap_transition(state, {"status": "unavailable"}), "retry"
        )
        self.assertEqual(
            browser_bootstrap_transition(state, {"status": "unavailable"}), "degraded"
        )
        self.assertEqual(
            browser_bootstrap_transition(state, {"status": "unavailable"}), "degraded"
        )
        self.assertEqual(
            browser_bootstrap_transition(state, {"status": "empty"}), "ready"
        )
        self.assertNotIn(AUTH_BROWSER_RECOVERY_KEY, state)

    def test_initial_component_loading_shows_recovery_not_login_form(self):
        app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30)
        app.query_params["page"] = "login"

        with patch(
            "src.auth_session._AUTH_COMPONENT",
            return_value=SimpleNamespace(
                auth_session={"status": "loading"}, auth_wake=0
            ),
        ):
            app.run()

        self.assertEqual(len(app.exception), 0)
        self.assertIn("正在恢复登录…", [item.value for item in app.info])
        self.assertNotIn("邮箱", [item.label for item in app.text_input])

    def test_unavailable_storage_keeps_logout_clear_but_does_not_retry_forever(self):
        state = {AUTH_BROWSER_VERSION_KEY: 10}
        begin_logout(state, reason="user", expected_version=10)
        clear = take_browser_command(state)

        self.assertEqual(
            browser_bootstrap_transition(state, {"status": "unavailable"}), "retry"
        )
        self.assertEqual(
            browser_bootstrap_transition(state, {"status": "unavailable"}), "degraded"
        )
        self.assertEqual(
            browser_bootstrap_transition(state, {"status": "unavailable"}), "degraded"
        )
        self.assertEqual(take_browser_command(state), clear)
        self.assertIn(AUTH_LOGOUT_PENDING_KEY, state)

    def test_explicit_empty_requires_confirmed_current_listener(self):
        state = {}
        command = take_browser_command(state)
        empty = {
            "status": "empty",
            "source": "read",
            "read_epoch": command["read_epoch"],
        }
        self.assertFalse(browser_signaled_logout(empty, has_current_user=True))
        stable = mark_browser_listener_stable(state, command, empty)
        self.assertTrue(browser_signaled_logout(
            empty,
            has_current_user=True,
            command=command,
            listener_stable=stable,
            has_pending_command=False,
        ))
        self.assertFalse(browser_signaled_logout(
            empty,
            has_current_user=True,
            command=command,
            listener_stable=stable,
            persistence_failed=True,
        ))

    def test_clear_signals_require_current_epoch_version_or_matching_ack(self):
        state = {AUTH_USER_VERSION_KEY: 20}
        command = take_browser_command(state)
        stale_epoch = {
            "status": "storage_cleared",
            "source": "storage",
            "read_epoch": "previous-login-listener",
            "version": 20,
        }

        self.assertFalse(
            mark_browser_listener_stable(state, command, stale_epoch)
        )
        self.assertFalse(browser_signaled_logout(
            stale_epoch,
            has_current_user=True,
            command=command,
            listener_stable=False,
            current_version=20,
        ))

        stale_version = {
            **stale_epoch,
            "read_epoch": command["read_epoch"],
            "version": 10,
        }
        stable = mark_browser_listener_stable(state, command, stale_version)
        self.assertTrue(stable)
        self.assertFalse(browser_signaled_logout(
            stale_version,
            has_current_user=True,
            command=command,
            listener_stable=stable,
            current_version=20,
        ))

        current_clear = {**stale_version, "version": 20}
        self.assertTrue(browser_signaled_logout(
            current_clear,
            has_current_user=True,
            command=command,
            listener_stable=mark_browser_listener_stable(
                state, command, current_clear
            ),
            current_version=20,
        ))
        self.assertFalse(browser_signaled_logout(
            {"status": "skipped_cleared", "command_id": "old-write"},
            has_current_user=True,
        ))

    def test_invalid_browser_record_is_cleared_without_refresh(self):
        store = Mock()
        result = resolve_auth_session(
            store, None, {"status": "invalid", "version": 0}, now=self.now
        )
        self.assertIsNone(result.user)
        self.assertTrue(result.clear_persisted)
        store.refresh.assert_not_called()

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

        def remote_failure(_user):
            self.assertIn(AUTH_LOGOUT_PENDING_KEY, state)
            self.assertEqual(take_browser_command(state)["action"], "clear")
            raise CloudStoreError("offline")

        remote = Mock(side_effect=remote_failure)

        start_logout_with_remote_best_effort(
            state,
            self.current,
            remote,
            expected_version=10,
            remote_runner=lambda task: task(),
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
        self.assertEqual(
            component.call_args.kwargs["key"],
            _browser_component_key({"action": "clear", "command_id": "clear-1"}),
        )

    def test_component_key_remounts_between_read_and_write_but_not_write_retry(self):
        state = {}
        read = take_browser_command(state)
        read_key = _browser_component_key(read)
        self.assertEqual(read_key, _browser_component_key(dict(read)))

        queue_refresh_token_write(state, "refresh-secret", now=self.now)
        write = take_browser_command(state)
        write_key = _browser_component_key(write)

        self.assertNotEqual(read_key, write_key)
        self.assertEqual(write_key, _browser_component_key(dict(write)))
        self.assertNotIn("refresh-secret", write_key)

        acknowledge_browser_command(
            state,
            {
                "status": "written",
                "command_id": write["command_id"],
                "refresh_token": write["refresh_token"],
                "saved_at": write["saved_at"],
                "version": write["version"],
            },
            now=self.now,
        )
        listener = take_browser_command(state)
        self.assertNotEqual(write_key, _browser_component_key(listener))

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
        self.assertIn('key + "_write_tombstones"', component)
        self.assertIn("tombstoneVersions.includes(requested.version)", component)
        self.assertNotIn("requested.version <= writeFenceVersion", component)
        self.assertNotIn("!existing && expectedVersion > 0", component)
        self.assertIn("expires_at", component)
        self.assertIn("now + retentionSeconds", component)
        self.assertIn("slice(-maxTombstones)", component)
        self.assertNotIn("fenceKey", component)

    def test_first_login_pending_write_ignores_stale_empty_until_listener(self):
        state = {}
        guest_read = take_browser_command(state)
        guest_empty = {
            "status": "empty",
            "source": "read",
            "read_epoch": guest_read["read_epoch"],
        }
        self.assertTrue(mark_browser_listener_stable(state, guest_read, guest_empty))

        logged_in = CloudUser(
            "user-a", "person@example.com", "access-login", "refresh-login",
            int(self.now + 3600), 3600, "expires_at",
        )
        queue_refresh_token_write(state, logged_in.refresh_token, now=self.now)
        write_command = take_browser_command(state)

        self.assertEqual(write_command["action"], "write")
        self.assertNotIn(AUTH_BROWSER_READ_EPOCH_KEY, state)
        stale_is_stable = mark_browser_listener_stable(
            state, write_command, guest_empty
        )
        self.assertFalse(stale_is_stable)
        self.assertFalse(browser_signaled_logout(
            guest_empty,
            has_current_user=True,
            command=write_command,
            listener_stable=stale_is_stable,
            has_pending_command=AUTH_BROWSER_COMMAND_KEY in state,
        ))
        before_ack = resolve_auth_session(
            Mock(),
            logged_in,
            guest_empty,
            current_version=state[AUTH_USER_VERSION_KEY],
            now=self.now,
        )
        self.assertEqual(before_ack.user, logged_in)

        record, write_value = apply_browser_command_to_record(None, write_command)
        ack = acknowledge_browser_command(state, write_value, now=self.now)
        self.assertEqual(ack.status, "written")
        self.assertTrue(browser_ack_needs_listener_rerun(state, ack))

        listener_read = take_browser_command(state)
        self.assertNotEqual(
            listener_read["read_epoch"], guest_read["read_epoch"]
        )
        self.assertFalse(
            mark_browser_listener_stable(state, listener_read, guest_empty)
        )
        self.assertFalse(browser_signaled_logout(
            guest_empty,
            has_current_user=True,
            command=listener_read,
            listener_stable=False,
            has_pending_command=False,
        ))

        listener_value = {
            "status": "loaded",
            "source": "read",
            "read_epoch": listener_read["read_epoch"],
            "refresh_token": record.refresh_token,
            "saved_at": record.saved_at,
            "version": record.version,
        }
        self.assertTrue(
            mark_browser_listener_stable(state, listener_read, listener_value)
        )
        final = resolve_auth_session(
            Mock(),
            logged_in,
            listener_value,
            current_version=state[AUTH_USER_VERSION_KEY],
            now=self.now,
        )
        self.assertEqual(final.user, logged_in)

    def test_newer_token_temporary_failure_keeps_token_version_pair(self):
        expired = CloudUser(
            "user-a", "person@example.com", "access-expired", "refresh-r1",
            int(self.now - 1), 3600, "expires_at",
        )
        store = Mock()
        store.refresh.side_effect = [CloudStoreError("temporary"), self.rotated]
        browser_value = self.browser_value(token="refresh-r2", version=15)

        first = resolve_auth_session(
            store,
            expired,
            browser_value,
            current_version=10,
            now=self.now,
        )

        self.assertEqual(first.user.refresh_token, "refresh-r2")
        self.assertEqual(first.browser_version, 15)
        self.assertTrue(first.state_changed)
        self.assertTrue(first.recovery_pending)
        state = {
            "cloud_user": cloud_user_to_state(first.user),
            AUTH_USER_VERSION_KEY: first.browser_version,
        }
        self.assertEqual(
            (
                state["cloud_user"]["refresh_token"],
                state[AUTH_USER_VERSION_KEY],
            ),
            ("refresh-r2", 15),
        )

        second = resolve_auth_session(
            store,
            cloud_user_from_state(state["cloud_user"]),
            browser_value,
            current_version=state[AUTH_USER_VERSION_KEY],
            now=self.now,
        )
        self.assertEqual(second.user, self.rotated)
        self.assertEqual(
            [item.args[0] for item in store.refresh.call_args_list],
            ["refresh-r2", "refresh-r2"],
        )

    def test_invalid_local_v20_forces_authoritative_browser_v15_refresh(self):
        current = CloudUser(
            "user-a", "person@example.com", "access-v20", "refresh-v20",
            int(self.now + 3600), 3600, "expires_at",
        )
        recovered = CloudUser(
            "user-a", "person@example.com", "access-v21", "refresh-v21",
            int(self.now + 7200), 3600, "expires_at",
        )
        browser_v15 = self.browser_value(token="refresh-v15", version=15)

        normal_store = Mock()
        normal = resolve_auth_session(
            normal_store, current, browser_v15, current_version=20, now=self.now
        )
        self.assertEqual(normal.user.refresh_token, "refresh-v20")
        normal_store.refresh.assert_not_called()

        recovery_store = Mock()
        recovery_store.refresh.return_value = recovered
        forced = resolve_auth_session(
            recovery_store,
            current,
            browser_v15,
            current_version=15,
            now=self.now,
            force_browser_refresh=True,
        )
        self.assertEqual(forced.user, recovered)
        self.assertTrue(forced.persist_refresh)
        self.assertEqual(forced.browser_version, 15)
        recovery_store.refresh.assert_called_once_with("refresh-v15")

        temporary_store = Mock()
        temporary_store.refresh.side_effect = CloudStoreError("temporary")
        temporary = resolve_auth_session(
            temporary_store,
            current,
            browser_v15,
            current_version=15,
            now=self.now,
            force_browser_refresh=True,
        )
        self.assertEqual(temporary.user.refresh_token, "refresh-v15")
        self.assertEqual(temporary.browser_version, 15)
        self.assertTrue(temporary.recovery_pending)
        self.assertFalse(temporary.clear_persisted)

        stale_listener_store = Mock()
        stale_listener_store.refresh.return_value = recovered
        stale_listener = resolve_auth_session(
            stale_listener_store,
            temporary.user,
            {"status": "empty", "source": "read", "read_epoch": "stale"},
            current_version=15,
            now=self.now,
            force_browser_refresh=True,
        )
        self.assertEqual(stale_listener.user, recovered)
        stale_listener_store.refresh.assert_called_once_with("refresh-v15")

    def test_rejected_write_is_bounded_and_returns_to_listener(self):
        state = {}
        queue_refresh_token_write(state, "refresh-login", now=self.now)
        command = take_browser_command(state)
        rejected = {
            "status": "rejected",
            "command_id": command["command_id"],
        }

        ack = acknowledge_browser_command(state, rejected, now=self.now)

        self.assertEqual(ack.status, "rejected")
        self.assertNotIn(AUTH_BROWSER_COMMAND_KEY, state)
        self.assertTrue(state[AUTH_PERSIST_WARNING_KEY])
        self.assertTrue(browser_ack_needs_listener_rerun(state, ack))
        read_command = take_browser_command(state)
        self.assertEqual(read_command["action"], "read")
        self.assertEqual(take_browser_command(state), read_command)

        empty = {
            "status": "empty",
            "source": "read",
            "read_epoch": read_command["read_epoch"],
        }
        stable = mark_browser_listener_stable(state, read_command, empty)
        self.assertTrue(stable)
        self.assertFalse(browser_signaled_logout(
            empty,
            has_current_user=True,
            command=read_command,
            listener_stable=stable,
            persistence_failed=state[AUTH_PERSIST_WARNING_KEY],
        ))

    def test_app_restore_rotated_token_is_written_before_page_render(self):
        app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30)
        now = time.time()
        restored = CloudUser(
            "user-a",
            "person@example.com",
            "access-new",
            "refresh-new",
            int(now + 3600),
            3600,
            "expires_at",
        )
        browser = {
            "record": PersistedRefreshSession("refresh-old", now, 10),
            "tombstones": [],
        }
        component_actions = []
        render_tokens = []

        def auth_component(*_args, **kwargs):
            data = kwargs["data"]
            action = str(data.get("action") or "read")
            component_actions.append(action)
            if action == "write":
                browser["record"], value = apply_browser_command_to_record(
                    browser["record"],
                    data,
                    tombstone_versions=browser["tombstones"],
                )
                value["tombstone_versions"] = browser["tombstones"]
            else:
                record = browser["record"]
                value = {
                    "status": "loaded",
                    "source": "read",
                    "read_epoch": data["read_epoch"],
                    "refresh_token": record.refresh_token,
                    "saved_at": record.saved_at,
                    "version": record.version,
                    "tombstone_versions": browser["tombstones"],
                }
            return SimpleNamespace(auth_session=value, auth_wake=0)

        def visitor_id():
            render_tokens.append(browser["record"].refresh_token)
            return ""
        with (
            patch("src.auth_session._AUTH_COMPONENT", side_effect=auth_component),
            patch("src.cloud_store._setting", return_value=""),
            patch.object(SupabaseStore, "refresh", return_value=restored) as refresh,
            patch("src.ai_grader.grade_essay_package") as grade,
            patch("src.storage.save_markdown_record") as local_save,
            patch("src.error_book.append_error_book") as error_save,
            patch.object(SupabaseStore, "save_grading_cycle") as cloud_save,
            patch("src.product_analytics.record_event_safely", return_value=None),
            patch("src.visitor_identity.browser_visitor_id", side_effect=visitor_id),
        ):
            app.run()

        self.assertEqual(len(app.exception), 0)
        refresh.assert_called_once_with("refresh-old")
        self.assertTrue(render_tokens)
        self.assertEqual(render_tokens[0], "refresh-new")
        self.assertGreaterEqual(len(component_actions), 3)
        self.assertEqual(component_actions[:3], ["read", "write", "read"])
        self.assertEqual(browser["record"].refresh_token, "refresh-new")
        self.assertEqual(
            app.session_state["cloud_user"]["refresh_token"], "refresh-new"
        )
        self.assertNotIn(AUTH_BROWSER_COMMAND_KEY, app.session_state)
        grade.assert_not_called()
        local_save.assert_not_called()
        error_save.assert_not_called()
        cloud_save.assert_not_called()

    def test_app_stale_clear_signals_do_not_logout_new_user(self):
        now = time.time()
        user = CloudUser(
            "user-a", "person@example.com", "access-login", "refresh-login",
            int(now + 3600), 3600, "expires_at",
        )
        signals = (
            (
                "old storage event",
                {
                    "status": "storage_cleared",
                    "source": "storage",
                    "read_epoch": "previous-login-listener",
                    "version": 10,
                },
            ),
            (
                "old skipped write",
                {
                    "status": "skipped_cleared",
                    "command_id": "previous-login-write",
                    "version": 10,
                },
            ),
        )

        for label, browser_value in signals:
            with self.subTest(signal=label):
                app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30)
                app.session_state["cloud_user"] = cloud_user_to_state(user)
                app.session_state["user_id"] = user.id
                app.session_state["page_mode"] = "home"
                app.session_state[AUTH_USER_VERSION_KEY] = 20
                app.session_state[AUTH_BROWSER_VERSION_KEY] = 20

                with patch(
                    "src.auth_session._AUTH_COMPONENT",
                    return_value=SimpleNamespace(
                        auth_session=browser_value, auth_wake=0
                    ),
                ), patch(
                    "src.visitor_identity.browser_visitor_id", return_value=""
                ):
                    app.run()

                self.assertEqual(len(app.exception), 0)
                self.assertEqual(
                    app.session_state["cloud_user"]["refresh_token"],
                    "refresh-login",
                )
                self.assertNotIn(AUTH_LOGOUT_PENDING_KEY, app.session_state)

    def test_app_pending_write_ignores_stale_empty(self):
        app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30)
        now = time.time()
        user = CloudUser(
            "user-a", "person@example.com", "access-login", "refresh-login",
            int(now + 3600), 3600, "expires_at",
        )
        version = int(now * 1_000_000)
        command = {
            "action": "write",
            "command_id": "login-write",
            "refresh_token": user.refresh_token,
            "saved_at": now,
            "version": version,
            "expected_version": 0,
        }
        app.session_state["cloud_user"] = cloud_user_to_state(user)
        app.session_state["user_id"] = user.id
        app.session_state["page_mode"] = "home"
        app.session_state[AUTH_USER_VERSION_KEY] = version
        app.session_state[AUTH_BROWSER_COMMAND_KEY] = command

        with patch(
            "src.auth_session._AUTH_COMPONENT",
            return_value=SimpleNamespace(
                auth_session={
                    "status": "empty",
                    "source": "read",
                    "read_epoch": "guest-read",
                },
                auth_wake=0,
            ),
        ), patch(
            "src.visitor_identity.browser_visitor_id", return_value=""
        ):
            app.run()

        self.assertEqual(len(app.exception), 0)
        self.assertEqual(
            app.session_state["cloud_user"]["refresh_token"], "refresh-login"
        )
        self.assertEqual(
            app.session_state[AUTH_BROWSER_COMMAND_KEY]["command_id"],
            "login-write",
        )
        self.assertNotIn(AUTH_LOGOUT_PENDING_KEY, app.session_state)

        self.assertIn(
            "正在保存登录状态…",
            [item.value for item in app.info],
        )
    def test_app_unavailable_storage_logout_leaves_waiting_state(self):
        app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30)
        now = time.time()
        user = CloudUser(
            "user-a", "person@example.com", "access-login", "refresh-login",
            int(now + 3600), 3600, "expires_at",
        )
        app.session_state["cloud_user"] = cloud_user_to_state(user)
        app.session_state["user_id"] = user.id
        app.session_state["page_mode"] = "home"
        app.session_state[AUTH_USER_VERSION_KEY] = 10
        app.session_state[AUTH_BROWSER_VERSION_KEY] = 10
        app.session_state[AUTH_LOGOUT_PENDING_KEY] = {
            "reason": "user",
            "clear_retries": 0,
        }
        app.session_state[AUTH_BROWSER_COMMAND_KEY] = {
            "action": "clear",
            "command_id": "logout-clear",
            "expected_version": 10,
        }
        warning = (
            "\u6d4f\u89c8\u5668\u5b58\u50a8\u672a\u80fd\u6e05\u7406\uff0c"
            "\u8bf7\u5173\u95ed\u5176\u4ed6\u6807\u7b7e\u9875\u6216\u6e05\u9664"
            "\u672c\u7ad9\u70b9\u6570\u636e\u3002"
        )

        with patch(
            "src.auth_session._AUTH_COMPONENT",
            return_value=SimpleNamespace(
                auth_session={"status": "unavailable"},
                auth_wake=0,
            ),
        ), patch(
            "src.visitor_identity.browser_visitor_id", return_value=""
        ):
            for _ in range(3):
                app.run()
                if AUTH_LOGOUT_PENDING_KEY not in app.session_state:
                    break

        self.assertEqual(len(app.exception), 0)
        self.assertNotIn(AUTH_LOGOUT_PENDING_KEY, app.session_state)
        self.assertNotIn("cloud_user", app.session_state)
        self.assertIn(warning, [item.value for item in app.warning])

    def test_app_exact_tombstone_allows_lower_other_tab_login(self):
        app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30)
        now = time.time()
        user = CloudUser(
            "user-a", "person@example.com", "access-login", "refresh-login",
            int(now + 3600), 3600, "expires_at",
        )
        other_user = CloudUser(
            "user-b", "other@example.com", "access-other",
            "refresh-other-rotated", int(now + 7200), 3600, "expires_at",
        )
        auth_state = {}
        queue_refresh_token_write(auth_state, user.refresh_token, now=now)
        delayed_write = dict(take_browser_command(auth_state))
        begin_logout(auth_state, reason="user", expected_version=0)
        clear = take_browser_command(auth_state)
        independent_write = {
            "action": "write",
            "command_id": "other-tab-login",
            "refresh_token": "refresh-other-tab",
            "saved_at": now,
            "version": delayed_write["version"] - 1,
            "expected_version": delayed_write["version"] - 2,
        }

        app.session_state["cloud_user"] = cloud_user_to_state(user)
        app.session_state["user_id"] = user.id
        app.session_state["page_mode"] = "home"
        for key, value in auth_state.items():
            app.session_state[key] = value

        browser = {
            "record": None,
            "tombstones": [],
            "delayed_status": "",
            "independent_status": "",
        }
        component_actions = []

        def auth_component(*_args, **kwargs):
            data = kwargs["data"]
            action = str(data.get("action") or "read")
            component_actions.append(action)
            if action == "clear":
                browser["record"], value = apply_browser_command_to_record(
                    browser["record"],
                    data,
                    tombstone_versions=browser["tombstones"],
                )
                browser["tombstones"] = value["tombstone_versions"]
                browser["record"], delayed_value = apply_browser_command_to_record(
                    browser["record"],
                    delayed_write,
                    tombstone_versions=browser["tombstones"],
                )
                browser["delayed_status"] = delayed_value["status"]
                browser["record"], independent_value = apply_browser_command_to_record(
                    browser["record"],
                    independent_write,
                    tombstone_versions=browser["tombstones"],
                )
                browser["independent_status"] = independent_value["status"]
            elif action == "write":
                browser["record"], value = apply_browser_command_to_record(
                    browser["record"],
                    data,
                    tombstone_versions=browser["tombstones"],
                )
                value["tombstone_versions"] = browser["tombstones"]
            elif browser["record"] is None:
                value = {
                    "status": "empty",
                    "source": "read",
                    "read_epoch": data["read_epoch"],
                    "tombstone_versions": browser["tombstones"],
                }
            else:
                record = browser["record"]
                value = {
                    "status": "loaded",
                    "source": "read",
                    "read_epoch": data["read_epoch"],
                    "refresh_token": record.refresh_token,
                    "saved_at": record.saved_at,
                    "version": record.version,
                    "tombstone_versions": browser["tombstones"],
                }
            return SimpleNamespace(auth_session=value, auth_wake=0)

        with (
            patch("src.auth_session._AUTH_COMPONENT", side_effect=auth_component),
            patch("src.cloud_store._setting", return_value=""),
            patch.object(SupabaseStore, "refresh", return_value=other_user) as refresh,
            patch("src.ai_grader.grade_essay_package") as grade,
            patch("src.storage.save_markdown_record") as local_save,
            patch("src.error_book.append_error_book") as error_save,
            patch.object(SupabaseStore, "save_grading_cycle") as cloud_save,
            patch("src.product_analytics.record_event_safely", return_value=None),
            patch("src.visitor_identity.browser_visitor_id", return_value=""),
        ):
            for _ in range(5):
                app.run()
                if (
                    AUTH_BROWSER_COMMAND_KEY not in app.session_state
                    and "cloud_user" in app.session_state
                    and app.session_state["cloud_user"]["id"] == other_user.id
                    and component_actions[-1] == "read"
                ):
                    break

        self.assertEqual(len(app.exception), 0)
        self.assertEqual(browser["delayed_status"], "skipped_cleared")
        self.assertEqual(browser["independent_status"], "written")
        self.assertEqual(browser["record"].refresh_token, "refresh-other-rotated")
        self.assertLess(independent_write["version"], max(browser["tombstones"]))
        refresh.assert_called_once_with("refresh-other-tab")
        self.assertIn("clear", component_actions)
        self.assertNotIn(AUTH_LOGOUT_PENDING_KEY, app.session_state)
        self.assertEqual(app.session_state["cloud_user"]["id"], other_user.id)
        self.assertEqual(
            app.session_state["cloud_user"]["refresh_token"], "refresh-other-rotated"
        )
        self.assertEqual(
            app.session_state[AUTH_BROWSER_TOMBSTONES_KEY],
            clear["tombstone_versions"],
        )
        grade.assert_not_called()
        local_save.assert_not_called()
        error_save.assert_not_called()
        cloud_save.assert_not_called()

    def test_app_invalid_v20_adopts_protected_browser_v15(self):
        app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30)
        now = time.time()
        invalid_local = CloudUser(
            "user-a", "person@example.com", "access-v20", "refresh-v20",
            int(now + 3600), 3600, "expires_at",
        )
        recovered = CloudUser(
            invalid_local.id, invalid_local.email, "access-v21", "refresh-v21",
            int(now + 7200), 3600, "expires_at",
        )
        app.session_state["cloud_user"] = cloud_user_to_state(invalid_local)
        app.session_state["user_id"] = invalid_local.id
        app.session_state["page_mode"] = "home"
        app.session_state[AUTH_USER_VERSION_KEY] = 20
        app.session_state[AUTH_BROWSER_VERSION_KEY] = 10
        app.session_state[AUTH_LOGOUT_PENDING_KEY] = {
            "reason": "invalid",
            "clear_retries": 0,
        }
        app.session_state[AUTH_BROWSER_COMMAND_KEY] = {
            "action": "clear",
            "command_id": "invalid-v20-clear",
            "expected_version": 10,
            "tombstone_versions": [10, 20],
            "superseded_write_versions": [20],
        }

        browser = {
            "record": PersistedRefreshSession("refresh-v15", now, 15),
            "tombstones": [],
            "stale_empty_reads": 1,
        }
        component_actions = []

        def auth_component(*_args, **kwargs):
            data = kwargs["data"]
            action = str(data.get("action") or "read")
            component_actions.append(action)
            if action == "clear":
                browser["record"], value = apply_browser_command_to_record(
                    browser["record"],
                    data,
                    tombstone_versions=browser["tombstones"],
                )
                browser["tombstones"] = value["tombstone_versions"]
            elif action == "write":
                browser["record"], value = apply_browser_command_to_record(
                    browser["record"],
                    data,
                    tombstone_versions=browser["tombstones"],
                )
            elif browser["stale_empty_reads"] > 0:
                browser["stale_empty_reads"] -= 1
                value = {
                    "status": "empty",
                    "source": "read",
                    "read_epoch": data["read_epoch"],
                    "tombstone_versions": browser["tombstones"],
                }
            elif browser["record"] is None:
                value = {
                    "status": "empty",
                    "source": "read",
                    "read_epoch": data["read_epoch"],
                    "tombstone_versions": browser["tombstones"],
                }
            else:
                record = browser["record"]
                value = {
                    "status": "loaded",
                    "source": "read",
                    "read_epoch": data["read_epoch"],
                    "refresh_token": record.refresh_token,
                    "saved_at": record.saved_at,
                    "version": record.version,
                    "tombstone_versions": browser["tombstones"],
                }
            return SimpleNamespace(auth_session=value, auth_wake=0)

        with (
            patch("src.auth_session._AUTH_COMPONENT", side_effect=auth_component),
            patch("src.cloud_store._setting", return_value=""),
            patch.object(SupabaseStore, "refresh", return_value=recovered) as refresh,
            patch("src.ai_grader.grade_essay_package") as grade,
            patch("src.storage.save_markdown_record") as local_save,
            patch("src.error_book.append_error_book") as error_save,
            patch.object(SupabaseStore, "save_grading_cycle") as cloud_save,
            patch("src.product_analytics.record_event_safely", return_value=None),
            patch("src.visitor_identity.browser_visitor_id", return_value=""),
        ):
            for _ in range(5):
                app.run()
                if (
                    AUTH_BROWSER_COMMAND_KEY not in app.session_state
                    and browser["record"].refresh_token == "refresh-v21"
                    and component_actions[-1] == "read"
                ):
                    break

        self.assertEqual(len(app.exception), 0)
        refresh.assert_called_once_with("refresh-v15")
        self.assertEqual(browser["tombstones"], [10, 20])
        self.assertNotIn(15, browser["tombstones"])
        self.assertEqual(browser["record"].refresh_token, "refresh-v21")
        self.assertGreater(browser["record"].version, max(browser["tombstones"]))
        self.assertEqual(
            app.session_state["cloud_user"]["refresh_token"], "refresh-v21"
        )
        self.assertEqual(
            app.session_state[AUTH_USER_VERSION_KEY], browser["record"].version
        )
        self.assertNotIn(AUTH_LOGOUT_PENDING_KEY, app.session_state)
        self.assertNotIn(AUTH_RECOVERY_STATE_KEY, app.session_state)
        self.assertNotIn(AUTH_BROWSER_COMMAND_KEY, app.session_state)
        self.assertEqual(component_actions.count("clear"), 1)
        self.assertIn("write", component_actions)
        grade.assert_not_called()
        local_save.assert_not_called()
        error_save.assert_not_called()
        cloud_save.assert_not_called()

    def test_app_second_401_reruns_to_clear_ack_without_side_effects(self):
        app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30)
        now = time.time()
        old = CloudUser(
            "user-a", "person@example.com", "access-old", "refresh-old",
            int(now + 3600), 3600, "expires_at",
        )
        app.query_params["page"] = "write"
        app.session_state["cloud_user"] = cloud_user_to_state(old)
        app.session_state["user_id"] = old.id
        app.session_state["page_mode"] = "write"
        app.session_state["topic_input"] = (
            "Some people think public transport should be free. Discuss both views."
        )
        app.session_state["essay_input"] = "word " * 260
        app.session_state[AUTH_USER_VERSION_KEY] = 10
        app.session_state[AUTH_BROWSER_VERSION_KEY] = 10

        browser = {
            "record": PersistedRefreshSession(old.refresh_token, now, 10)
        }
        component_actions = []
        clear_expected_versions = []
        clear_payloads = []

        def auth_component(*_args, **kwargs):
            data = kwargs["data"]
            action = str(data.get("action") or "read")
            component_actions.append(action)
            if action == "clear":
                clear_expected_versions.append(data["expected_version"])
                clear_payloads.append(dict(data))
                superseded = data["superseded_write_versions"]
                browser["record"] = PersistedRefreshSession(
                    "refresh-new", now + 1, superseded[-1]
                )
                browser["record"], value = apply_browser_command_to_record(
                    browser["record"], data
                )
            elif browser["record"] is None:
                value = {
                    "status": "empty",
                    "source": "read",
                    "read_epoch": data["read_epoch"],
                }
            else:
                value = {
                    "status": "loaded",
                    "source": "read",
                    "read_epoch": data["read_epoch"],
                    "refresh_token": old.refresh_token,
                    "saved_at": now,
                    "version": 10,
                }
            return SimpleNamespace(auth_session=value, auth_wake=0)

        def response(status, data):
            result = Mock(status_code=status, content=b"{}")
            result.json.return_value = data
            return result

        refreshed_data = {
            "user": {"id": old.id, "email": old.email},
            "access_token": "access-new",
            "refresh_token": "refresh-new",
            "expires_at": int(now + 3600),
            "expires_in": 3600,
        }
        settings = {
            "SUPABASE_URL": "https://example.supabase.co",
            "SUPABASE_ANON_KEY": "public-anon-key",
        }

        with (
            patch(
                "src.auth_session._AUTH_COMPONENT",
                side_effect=auth_component,
            ),
            patch(
                "src.cloud_store._setting",
                side_effect=lambda name: settings.get(name, ""),
            ),
            patch("src.cloud_store.requests.request") as request,
            patch("src.ai_grader.grade_essay_package") as grade,
            patch("src.storage.save_markdown_record") as local_save,
            patch("src.error_book.append_error_book") as error_save,
            patch.object(SupabaseStore, "save_grading_cycle") as cloud_save,
            patch("src.product_analytics.record_event_safely", return_value=None),
            patch("src.visitor_identity.browser_visitor_id", return_value=""),
        ):
            request.side_effect = [
                response(401, {"message": "expired"}),
                response(200, refreshed_data),
                response(401, {"message": "still expired"}),
            ]
            app.run()
            self.assertEqual(len(app.exception), 0)
            submit = next(
                button for button in app.button
                if button.label == "开始批改作文"
            )
            submit.click()
            app.run()
            for _ in range(3):
                if AUTH_LOGOUT_PENDING_KEY not in app.session_state:
                    break
                app.run()

        self.assertEqual(len(app.exception), 0)
        self.assertEqual(clear_expected_versions, [10])
        self.assertEqual(len(clear_payloads), 1)
        self.assertEqual(len(clear_payloads[0]["superseded_write_versions"]), 1)
        self.assertGreater(clear_payloads[0]["superseded_write_versions"][0], 10)
        self.assertNotIn("clear_through_version", clear_payloads[0])
        self.assertIsNone(browser["record"])
        self.assertEqual(request.call_count, 3)
        self.assertIn("clear", component_actions)
        self.assertNotIn(AUTH_LOGOUT_PENDING_KEY, app.session_state)
        self.assertNotIn(AUTH_REQUEST_RERUN_KEY, app.session_state)
        self.assertNotIn("cloud_user", app.session_state)
        grade.assert_not_called()
        local_save.assert_not_called()
        error_save.assert_not_called()
        cloud_save.assert_not_called()


    def test_retry_guest_claim_refresh_reruns_to_browser_write_once(self):
        app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30)
        now = time.time()
        old = CloudUser(
            "user-a", "person@example.com", "access-old", "refresh-old",
            int(now + 3600), 3600, "expires_at",
        )
        refreshed = CloudUser(
            old.id, old.email, "access-new", "refresh-new",
            int(now + 7200), 3600, "expires_at",
        )
        app.session_state["cloud_user"] = cloud_user_to_state(old)
        app.session_state["user_id"] = old.id
        app.session_state["page_mode"] = "home"
        app.session_state[AUTH_USER_VERSION_KEY] = 10
        app.session_state[AUTH_BROWSER_VERSION_KEY] = 10
        app.session_state["pending_guest_claim"] = {
            "topic": "Discuss both views.",
            "essay": "word " * 260,
            "word_count": 260,
            "fingerprint": "guest-fingerprint",
            "package": {"structured": {}},
        }

        browser = {
            "record": PersistedRefreshSession(old.refresh_token, now, 10)
        }
        component_actions = []
        save_calls = []

        def auth_component(*_args, **kwargs):
            data = kwargs["data"]
            action = str(data.get("action") or "read")
            component_actions.append(action)
            if action in {"write", "clear"}:
                browser["record"], value = apply_browser_command_to_record(
                    browser["record"], data
                )
            elif browser["record"] is None:
                value = {
                    "status": "empty",
                    "source": "read",
                    "read_epoch": data["read_epoch"],
                }
            else:
                record = browser["record"]
                value = {
                    "status": "loaded",
                    "source": "read",
                    "read_epoch": data["read_epoch"],
                    "refresh_token": record.refresh_token,
                    "saved_at": record.saved_at,
                    "version": record.version,
                }
            return SimpleNamespace(auth_session=value, auth_wake=0)

        def save_after_refresh(store, user, **_kwargs):
            save_calls.append(user.refresh_token)
            self.assertIsNotNone(store._auth_user_updated)
            store._auth_user_updated(refreshed)
            raise CloudStoreError("temporary save failure", status_code=503)

        with (
            patch("src.auth_session._AUTH_COMPONENT", side_effect=auth_component),
            patch("src.cloud_store._setting", return_value=""),
            patch.object(
                SupabaseStore, "save_grading_cycle", new=save_after_refresh
            ),
            patch("src.ai_grader.grade_essay_package") as grade,
            patch("src.storage.save_markdown_record") as local_save,
            patch("src.error_book.append_error_book") as error_save,
            patch("src.product_analytics.record_event_safely", return_value=None),
            patch("src.visitor_identity.browser_visitor_id", return_value=""),
        ):
            app.run()
            retry = next(
                button for button in app.button
                if button.label == "重试保存这次批改"
            )
            component_actions.clear()
            retry.click()
            app.run()

        self.assertEqual(len(app.exception), 0)
        self.assertEqual(save_calls, ["refresh-old"])
        self.assertIn("write", component_actions)
        write_index = component_actions.index("write")
        self.assertIn("read", component_actions[write_index + 1:])
        self.assertEqual(browser["record"].refresh_token, "refresh-new")
        self.assertEqual(
            app.session_state["cloud_user"]["refresh_token"], "refresh-new"
        )
        self.assertTrue(app.session_state["guest_claim_failed"])
        self.assertIn("pending_guest_claim", app.session_state)
        self.assertNotIn(AUTH_BROWSER_COMMAND_KEY, app.session_state)
        self.assertNotIn(AUTH_REQUEST_RERUN_KEY, app.session_state)
        grade.assert_not_called()
        local_save.assert_not_called()
        error_save.assert_not_called()

if __name__ == "__main__":
    unittest.main()
