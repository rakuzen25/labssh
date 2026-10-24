"""Idle selection uses the current recommendation and yields to all input."""

import threading
import unittest
from unittest.mock import Mock, patch

from labssh import cli, transport
from labssh.console import Console


class AutoConnectTests(unittest.TestCase):
    def menu(self, keys, recommendation, *, fail_draw=False):
        clock = [0.0]
        state = Mock()
        state.collect.return_value = False
        state.recommendation.side_effect = lambda: recommendation(clock[0])
        console = Mock()
        console.__enter__ = Mock(return_value=console)
        console.__exit__ = Mock(return_value=False)
        seen = []

        def key():
            seen.append(clock[0])
            if clock[0] > 8:
                self.fail("menu did not finish")
            return keys(clock[0])

        console.key.side_effect = key
        if fail_draw:
            console.draw.side_effect = OSError("draw failed")
        with (
            patch.object(cli, "Console", return_value=console),
            patch.object(cli.time, "monotonic", side_effect=lambda: clock[0]),
            patch.object(
                cli.time,
                "sleep",
                side_effect=lambda _: clock.__setitem__(0, clock[0] + 1),
            ),
        ):
            if fail_draw:
                with self.assertRaisesRegex(OSError, "draw failed"):
                    cli.run_menu(state)
                result = None
            else:
                result = cli.run_menu(state)
        state.close.assert_called_once()
        state.save.assert_called_once()
        console.__exit__.assert_called_once()
        return result, seen

    def test_connects_at_three_seconds_to_latest_best(self):
        result, seen = self.menu(lambda t: None, lambda t: "old" if t < 3 else "new")
        self.assertEqual(result, "new")
        self.assertEqual(seen[-1], 3)

    def test_waits_for_first_recommendation_after_deadline(self):
        result, seen = self.menu(lambda t: None, lambda t: None if t < 5 else "ready")
        self.assertEqual(result, "ready")
        self.assertEqual(seen[-1], 5)

    def test_any_input_permanently_cancels_even_at_deadline(self):
        for key in ("x", "1", "\b", "\x00", "m", "p", "r"):
            with self.subTest(key=key):
                result, seen = self.menu(
                    lambda t, key=key: key if t == 3 else "q" if t == 6 else None,
                    lambda t: "best",
                )
                self.assertIsNone(result)
                self.assertEqual(seen[-1], 6)

    def test_no_recommendation_never_connects(self):
        result, _ = self.menu(lambda t: "q" if t == 6 else None, lambda t: None)
        self.assertIsNone(result)

    def test_draw_failure_still_cleans_up(self):
        self.menu(lambda t: None, lambda t: "best", fail_draw=True)

    def test_windows_extended_keys_are_detected_as_input(self):
        native = Mock()
        native.kbhit.return_value = True
        native.getwch.side_effect = ["\xe0", "K"]
        with (
            patch("labssh.console.os.name", "nt"),
            patch.dict("sys.modules", {"msvcrt": native}),
        ):
            self.assertEqual(Console().key(), "\x00")
        self.assertEqual(native.getwch.call_count, 2)


class ProbeCleanupTests(unittest.TestCase):
    def test_read_error_after_exit_still_drains_pipes(self):
        scanner = transport.Scanner.__new__(transport.Scanner)
        scanner.lock = threading.Lock()
        scanner.stop = threading.Event()
        scanner.ssh = "ssh"
        scanner.timeout = 3
        scanner.active = None
        scanner.statuses = {}
        process = Mock()
        process.process.poll.return_value = 0
        process.process.communicate.side_effect = [OSError("read failed"), (b"", b"")]
        with patch.object(transport, "ProbeProcess", return_value=process):
            scanner._probe("host")
        process.close.assert_called_once()
        self.assertEqual(process.process.communicate.call_count, 2)
        self.assertIsNone(scanner.active)
