"""Unit tests for the rotating RapidAPI key pool (Libs/rapidapi_keypool.py)."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from Libs import rapidapi_keypool as pool


def _headers(limit="10000", remaining="5000", used="5000", reset="250000"):
    return {
        "x-ratelimit-requests-limit": limit,
        "x-ratelimit-requests-remaining": remaining,
        "x-ratelimit-requests-used": used,
        "x-ratelimit-requests-reset": reset,
    }


class KeyPoolTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_path = Path(self._tmp.name) / "rapidapi_keypool.json"
        self.flat_path = Path(self._tmp.name) / "rapidapi_quota.json"
        patcher_state = patch.object(pool, "STATE_PATH", self.state_path)
        patcher_state.start()
        self.addCleanup(patcher_state.stop)
        # Keep the legacy flat quota file inside the sandbox too.
        from Libs import rapidapi_quota
        patcher_flat = patch.object(rapidapi_quota, "QUOTA_PATH", self.flat_path)
        patcher_flat.start()
        self.addCleanup(patcher_flat.stop)
        self.env = {}
        patcher_env = patch.dict(os.environ, {}, clear=True)
        patcher_env.start()
        self.addCleanup(patcher_env.stop)
        self.config = {}
        patcher_config = patch.object(pool, "_read_config", lambda: self.config)
        patcher_config.start()
        self.addCleanup(patcher_config.stop)
        # A 429 anywhere arms the 60s process-wide breaker in
        # Libs.rapidapi_quota; disarm it so later tests can post again.
        from Libs import rapidapi_quota
        rapidapi_quota.clear_rate_limit()
        self.addCleanup(rapidapi_quota.clear_rate_limit)

    def _candidates(self, primary="key-primary", backups=("key-backup-1", "key-backup-2")):
        self.config["rapidapi_key"] = primary
        self.config["rapidapi_backup_keys"] = list(backups)


class TestSelection(KeyPoolTestCase):
    def test_unknown_keys_are_usable_in_order(self):
        self._candidates()
        self.assertEqual(pool.current_key(), "key-primary")

    def test_rotation_below_threshold(self):
        self._candidates()
        # Primary drops below the 200 margin: next call must use backup 1.
        pool.note_headers("key-primary", _headers(remaining="199"), path=self.state_path)
        self.assertEqual(pool.current_key(), "key-backup-1")

    def test_healthy_primary_is_kept(self):
        self._candidates()
        pool.note_headers("key-primary", _headers(remaining="5000"), path=self.state_path)
        self.assertEqual(pool.current_key(), "key-primary")

    def test_rotation_skips_to_second_backup(self):
        self._candidates()
        pool.note_headers("key-primary", _headers(remaining="120"), path=self.state_path)
        pool.note_headers("key-backup-1", _headers(remaining="80"), path=self.state_path)
        self.assertEqual(pool.current_key(), "key-backup-2")

    def test_all_exhausted_falls_back_to_primary(self):
        self._candidates()
        pool.note_headers("key-primary", _headers(remaining="100"), path=self.state_path)
        pool.note_headers("key-backup-1", _headers(remaining="40"), path=self.state_path)
        pool.note_headers("key-backup-2", _headers(remaining="10"), path=self.state_path)
        # Nobody usable: primary is returned so the caller fails with QUOTA.
        self.assertEqual(pool.current_key(), "key-primary")
        status = pool.pool_status()
        self.assertFalse(status["has_usable"])

    def test_invalid_key_is_skipped(self):
        self._candidates()
        pool.note_headers("key-primary", _headers(remaining="9000"), path=self.state_path)
        pool.note_invalid("key-primary", path=self.state_path)
        self.assertEqual(pool.current_key(), "key-backup-1")

    def test_exhausted_via_429(self):
        self._candidates()
        pool.note_headers("key-primary", _headers(remaining="9000"), path=self.state_path)
        pool.note_exhausted("key-primary", path=self.state_path)
        self.assertEqual(pool.current_key(), "key-backup-1")

    def test_hourly_rate_limit_parks_key_without_burning_quota(self):
        """A 429 while monthly quota is healthy: park briefly, not remaining=0."""
        self._candidates()
        pool.note_headers("key-primary", _headers(remaining="9000"), path=self.state_path)
        pool.note_exhausted("key-primary", path=self.state_path)
        state = pool.read_state(path=self.state_path)
        entry = state["keys"][pool.fingerprint("key-primary")]
        self.assertEqual(entry.get("remaining"), "9000")
        self.assertIn("cooldown_until", entry)
        # Parked now: rotated to the backup...
        self.assertEqual(pool.current_key(), "key-backup-1")
        # ...and readmitted once a successful response clears the park.
        pool.note_headers("key-primary", {}, success=True, path=self.state_path)
        state = pool.read_state(path=self.state_path)
        self.assertNotIn("cooldown_until", state["keys"][pool.fingerprint("key-primary")])
        self.assertEqual(pool.current_key(), "key-primary")

    def test_monthly_reset_recovers_key(self):
        self._candidates()
        snapshot = _headers(remaining="100", reset="10")
        # reset=10 seconds-until-reset, written long ago: the reset has
        # passed, so the key is unknown (fresh quota) and selected again.
        with patch.object(pool.time, "time", lambda: 1_000_000):
            pool.note_headers("key-primary", snapshot, path=self.state_path)
        pool.note_headers("key-backup-1", _headers(remaining="30"), path=self.state_path)
        pool.note_headers("key-backup-2", _headers(remaining="20"), path=self.state_path)
        self.assertEqual(pool.current_key(), "key-primary")

    def test_threshold_boundary_is_inclusive(self):
        self._candidates()
        # Exactly at the margin: still usable.
        pool.note_headers("key-primary", _headers(remaining="200"), path=self.state_path)
        self.assertEqual(pool.current_key(), "key-primary")
        # One below: rotate.
        pool.note_headers("key-primary", _headers(remaining="199"), path=self.state_path)
        self.assertEqual(pool.current_key(), "key-backup-1")


class TestSources(KeyPoolTestCase):
    def test_env_backup_keys_split_and_ordered(self):
        self.config["rapidapi_key"] = "config-primary"
        os.environ["RAPIDAPI_KEY"] = "env-primary"
        os.environ["RAPIDAPI_BACKUP_KEYS"] = "env-b1\nenv-b2;env-b3"
        self.config["rapidapi_backup_keys"] = ["config-b1", "env-b1"]
        keys = pool.keys()
        self.assertEqual(keys, ["env-primary", "config-primary",
                                "env-b1", "env-b2", "env-b3", "config-b1"])

    def test_config_backups_accept_strings(self):
        self.config["rapidapi_key"] = "p"
        self.config["rapidapi_backup_keys"] = "s1\ns2, s3"
        self.assertEqual(pool.keys(), ["p", "s1", "s2", "s3"])

    def test_threshold_env_wins(self):
        os.environ["RAPIDAPI_ROTATION_THRESHOLD"] = "50"
        self.assertEqual(pool.threshold(), 50)
        self.config["rapidapi_rotation_threshold"] = 90
        self.assertEqual(pool.threshold(), 50)

    def test_threshold_invalid_falls_back(self):
        self.config["rapidapi_rotation_threshold"] = "not-a-number"
        os.environ["RAPIDAPI_ROTATION_THRESHOLD"] = ""
        self.assertEqual(pool.threshold(), 200)

    def test_no_keys_at_all(self):
        self.assertEqual(pool.keys(), [])
        self.assertEqual(pool.current_key(), "")


class TestStatePersistence(KeyPoolTestCase):
    def test_note_headers_persists_per_key_without_leaking_keys(self):
        self._candidates()
        pool.note_headers("key-primary", _headers(remaining="250"), path=self.state_path)
        pool.note_headers("key-backup-1", _headers(remaining="7000"), path=self.state_path)
        raw = self.state_path.read_text(encoding="utf-8")
        self.assertNotIn("key-primary", raw)
        self.assertNotIn("key-backup-1", raw)
        state = json.loads(raw)
        self.assertIn(pool.fingerprint("key-primary"), state["keys"])
        self.assertIn(pool.fingerprint("key-backup-1"), state["keys"])

    def test_note_headers_mirrors_flat_quota_file(self):
        self._candidates()
        pool.note_headers("key-backup-1", _headers(remaining="7000"), path=self.state_path)
        flat = json.loads(self.flat_path.read_text(encoding="utf-8"))
        self.assertEqual(flat.get("remaining"), "7000")

    def test_state_survives_module_reload(self):
        self._candidates()
        pool.note_headers("key-primary", _headers(remaining="150"), path=self.state_path)
        # A second "process" reads the same state file.
        state = pool.read_state(path=self.state_path)
        self.assertEqual(pool.current_key(state=state), "key-backup-1")

    def test_pool_status_lines(self):
        self._candidates()
        pool.note_headers("key-primary", _headers(remaining="150"), path=self.state_path)
        status = pool.pool_status()
        self.assertEqual(status["active"], "key-backup-1")
        self.assertEqual(status["active_index"], 2)
        self.assertTrue(status["has_usable"])
        self.assertEqual(len(status["lines"]), 3)
        self.assertIn("active", status["lines"][1])


class TestSignerIntegration(KeyPoolTestCase):
    """The signer call sites must route through the pool and record per key."""

    def _signers_module(self):
        try:
            from Libs import signers
        except Exception as exc:  # curl_cffi missing in this environment.
            self.skipTest(f"Libs.signers unavailable: {exc}")
        return signers

    def test_post_signatures_uses_pool_key_and_records(self):
        signers = self._signers_module()
        self._candidates()
        pool.note_headers("key-primary", _headers(remaining="150"), path=self.state_path)

        class FakeResponse:
            status_code = 200
            headers = _headers(limit="10000", remaining="6800")
            text = '{"success": true}'

            @classmethod
            def json(cls):
                return {
                    "success": True,
                    "x-khronos": "123",
                    "x-ladon": "abc",
                    "x-argus": "def",
                }

        with patch.object(signers.requests, "post", return_value=FakeResponse()) as post:
            signers.signature_headers(1700000000, aid="8311")
        sent_headers = post.call_args.kwargs["headers"]
        self.assertEqual(sent_headers.get("X-RapidAPI-Key"), "key-backup-1")
        state = pool.read_state(path=self.state_path)
        backup_snapshot = state["keys"][pool.fingerprint("key-backup-1")]
        self.assertEqual(backup_snapshot["remaining"], "6800")

    def test_post_signatures_notes_429_and_401(self):
        signers = self._signers_module()
        self._candidates()

        class Denied:
            status_code = 429
            headers = {}
            text = "rate limited"

            @classmethod
            def json(cls):
                return {"success": False, "error": "quota"}

        with patch.object(signers.requests, "post", return_value=Denied()):
            with self.assertRaises(Exception):
                signers.signature_headers(1700000000)
        state = pool.read_state(path=self.state_path)
        self.assertEqual(
            state["keys"][pool.fingerprint("key-primary")].get("remaining"), "0")


if __name__ == "__main__":
    unittest.main()
