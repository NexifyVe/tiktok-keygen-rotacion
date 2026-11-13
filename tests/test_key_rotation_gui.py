"""GUI behaviour of the RapidAPI key pool: settings, display and auto-pause."""
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from tests import test_client_modes as base

gui = base.gui


def _pool(active="bk1", has_usable=True, keys=("primary", "bk1")):
    keys = list(keys)
    index = keys.index(active) + 1 if active in keys else 0
    lines = [
        f"Key #{i + 1} (…{('fp%d' % (i + 1))}): {'150 left' if i == 0 else '7,000 left'}"
        + (" · active" if keys[i] == active else "")
        for i in range(len(keys))
    ]
    return {
        "keys": keys,
        "active": active,
        "active_fp": "fp%d" % index,
        "active_index": index,
        "threshold": 200,
        "lines": lines,
        "usable_count": 1 if has_usable else 0,
        "usable_backups": max(0, (1 if has_usable else 0) - 1),
        "has_usable": has_usable,
    }


class KeyRotationGuiTests(unittest.TestCase):
    setUpClass = classmethod(base.ModeTests.setUpClass.__func__)
    setUp = base.ModeTests.setUp

    def window(self, reset=True):
        if reset:
            Path(self.path).write_text(json.dumps(self.config))
        window = base.ModeTests.window(self)
        window._game_tags_loading = False
        window.device_initialization_attempted = True
        return window

    def test_backup_keys_roundtrip_and_config_persistence(self):
        w = self.window()
        w.rapidapi_key_edit.setText('primary-abc')
        w.backup_keys_edit.setPlainText('bk1\nbk2, bk3\nbk1')
        w.rotation_threshold_edit.setText('250')
        self.assertEqual(w.get_backup_keys(), ['bk1', 'bk2', 'bk3'])
        self.assertEqual(w.get_rotation_threshold(), 250)

        w.save_config(show_message=False)
        data = json.loads(Path(self.path).read_text())
        self.assertEqual(data['rapidapi_key'], 'primary-abc')
        self.assertEqual(data['rapidapi_backup_keys'], ['bk1', 'bk2', 'bk3'])
        self.assertEqual(data['rapidapi_rotation_threshold'], 250)

        # A second window loads what save_config wrote: reset=False keeps
        # the saved file instead of restoring the fixture.
        reloaded = self.window(reset=False)
        self.assertEqual(reloaded.get_backup_keys(), ['bk1', 'bk2', 'bk3'])
        self.assertEqual(reloaded.get_rotation_threshold(), 250)
        self.assertEqual(reloaded.rapidapi_backup_keys, ['bk1', 'bk2', 'bk3'])
        self.assertEqual(reloaded.backup_keys_edit.toPlainText(), 'bk1\nbk2\nbk3')

    def test_invalid_threshold_falls_back_to_200(self):
        w = self.window()
        for bad in ('abc', '-5', ''):
            with self.subTest(bad=bad):
                w.rotation_threshold_edit.setText(bad)
                self.assertEqual(w.get_rotation_threshold(), 200)

    def test_quota_display_shows_active_key_and_notifies_rotation(self):
        w = self.window()
        snapshot = {"limit": "10000", "remaining": "150", "used": "9850", "updated": 1}
        w._last_pool_active_fp = "fp-old"
        with patch.object(gui, '_pool_status', return_value=_pool()), \
                patch.object(gui, 'read_quota', return_value=snapshot), \
                patch.object(w, 'set_proxy_status') as status:
            w.update_rapidapi_quota_display()
            self.assertIn('key 2/2', w.rapidapi_quota_bar.format())
            self.assertIn('150', w.rapidapi_quota_bar.format())
            self.assertIn('Key #2', w.rapidapi_quota_bar.toolTip())
            self.assertIn('200', w.rapidapi_quota_bar.toolTip())
            # The active key changed since the last observation: notify.
            status.assert_called_once()
            self.assertIn('key #2', status.call_args[0][0])
            # Same active key again: no new notification.
            w.update_rapidapi_quota_display()
            status.assert_called_once()

    def test_engine_pauses_when_pool_drained(self):
        w = self.window()
        w.is_live = True
        w.is_paused = False
        w.auto_paused_for_quota = False
        with patch.object(gui, '_pool_status', return_value=_pool(has_usable=False)), \
                patch.object(w, 'pause_stream') as pause:
            w.check_keypool_for_stream()
            pause.assert_called_once_with(notify=False)
            self.assertTrue(w.auto_paused_for_quota)

    def test_engine_resumes_when_pool_recovers(self):
        w = self.window()
        w.is_live = True
        w.is_paused = True
        w.auto_paused_for_quota = True
        with patch.object(gui, '_pool_status', return_value=_pool(has_usable=True)), \
                patch.object(w, 'resume_stream') as resume:
            w.check_keypool_for_stream()
            resume.assert_called_once_with(notify=False)
            self.assertFalse(w.auto_paused_for_quota)

    def test_manual_pause_is_never_auto_resumed(self):
        w = self.window()
        w.is_live = True
        w.is_paused = True  # the user paused it
        w.auto_paused_for_quota = False
        with patch.object(gui, '_pool_status', return_value=_pool(has_usable=True)), \
                patch.object(w, 'resume_stream') as resume:
            w.check_keypool_for_stream()
            resume.assert_not_called()

    def test_engine_skips_when_not_live_or_busy(self):
        w = self.window()
        w.is_live = False
        with patch.object(gui, '_pool_status', return_value=_pool(has_usable=False)), \
                patch.object(w, 'pause_stream') as pause:
            w.check_keypool_for_stream()
            pause.assert_not_called()

        w.is_live = True
        w._foreground_task = object()
        try:
            with patch.object(gui, '_pool_status', return_value=_pool(has_usable=False)), \
                    patch.object(w, 'pause_stream') as pause2:
                w.check_keypool_for_stream()
                pause2.assert_not_called()
        finally:
            w._foreground_task = None


if __name__ == "__main__":
    unittest.main()
