"""Offline launch gates: recovery is interactive and never part of smoke tests."""

import threading
import unittest
from contextlib import ExitStack
from unittest.mock import Mock, patch

import desktop


class DesktopRecoveryTests(unittest.TestCase):
    def launch(self, args, *, frozen=False, media=False):
        stack = ExitStack()
        self.addCleanup(stack.close)
        for name in ("prepare_stdio", "initialize", "prepare_logging", "inform"):
            stack.enter_context(patch.object(desktop, name))
        stack.enter_context(patch.object(desktop.sys, "frozen", frozen, create=True))
        stack.enter_context(patch.object(desktop, "media_tools_available", return_value=media))
        self.setup = stack.enter_context(patch.object(desktop, "setup_desktop_media", return_value=True))
        lock = stack.enter_context(patch.object(desktop, "SingleInstanceLock")).return_value
        lock.acquire.return_value = True
        server = stack.enter_context(patch.object(desktop, "LocalServer")).return_value
        smoke = stack.enter_context(patch.object(desktop, "smoke_test", return_value=0))
        native = stack.enter_context(patch.object(desktop, "desktop_mode"))
        browser = stack.enter_context(patch.object(desktop, "browser_mode"))
        self.assertEqual(desktop.main(args), 0)
        return server, lock, smoke, native, browser

    def test_read_only_smoke_never_starts_task_queries(self):
        server, lock, smoke, native, browser = self.launch(["--smoke-test"])
        server.start.assert_called_once()
        server.start_recovery.assert_not_called()
        smoke.assert_called_once_with(server)
        native.assert_not_called()
        browser.assert_not_called()
        server.stop.assert_called_once()
        lock.acquire.assert_not_called()

    def test_normal_launch_reconnects_before_ui_then_stops_cleanly(self):
        server, lock, smoke, native, browser = self.launch([])
        server.start_recovery.assert_called_once()
        native.assert_called_once_with(server)
        smoke.assert_not_called()
        browser.assert_not_called()
        server.stop.assert_called_once()
        lock.release.assert_called_once()

    def test_portable_smoke_does_not_install_media(self):
        server, _, smoke, _, _ = self.launch(["--smoke-test"], frozen=True)
        self.setup.assert_not_called()
        smoke.assert_called_once_with(server)

    def test_portable_launch_requests_media_only_if_unavailable(self):
        self.launch([], frozen=True, media=False)
        self.setup.assert_called_once_with()

    def test_portable_launch_reuses_existing_tools(self):
        self.launch([], frozen=True, media=True)
        self.setup.assert_not_called()

    def test_browser_fallback_reconnects_without_generation(self):
        server, lock, smoke, native, browser = self.launch(["--browser"])
        server.start_recovery.assert_called_once()
        browser.assert_called_once_with(server)
        native.assert_not_called()
        smoke.assert_not_called()

    def test_server_stop_signals_workers_once_before_http_shutdown(self):
        server = desktop.LocalServer.__new__(desktop.LocalServer)
        server.lock = threading.Lock()
        server.stopped = False
        calls = []
        server.workflow = Mock()
        server.workflow.stop_recovery_workers.side_effect = lambda: calls.append("workers")
        server.server = Mock()
        server.server.shutdown.side_effect = lambda: calls.append("http")
        server.thread = Mock()
        server.stop()
        server.stop()
        self.assertEqual(calls, ["workers", "http"])
        server.workflow.stop_recovery_workers.assert_called_once()
        server.server.server_close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
