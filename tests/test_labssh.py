import json
import queue
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from labssh import cli, core, transport
from labssh.coordination import Outcome, ProbeResult


def metric(**overrides):
    values = {
        "cpus": 8,
        "load": 0.0,
        "total_kib": 1000,
        "available_kib": 800,
        "swap_total_kib": 100,
        "swap_free_kib": 100,
        "seconds": 0.2,
        "timestamp": time.time(),
    }
    values.update(overrides)
    return core.Metrics.checked(values)


class RankingTests(unittest.TestCase):
    def setUp(self):
        self.hosts = [
            core.Host("brooks", preferred=True),
            core.Host("moore", preferred=True),
            core.Host("other"),
        ]

    def test_full_headroom(self):
        self.assertAlmostEqual(core.score(metric(), "responsive", core.Ranking()), 100)

    def test_cpu_pressure(self):
        self.assertEqual(core.score(metric(load=16), "responsive", core.Ranking()), 40)

    def test_memory_pressure(self):
        self.assertEqual(
            core.score(metric(available_kib=0), "responsive", core.Ranking()), 70
        )

    def test_compute_capacity_and_memory(self):
        self.assertEqual(
            core.score(metric(cpus=128, load=28), "compute", core.Ranking()), 100
        )
        self.assertEqual(
            core.score(
                metric(cpus=128, load=28, available_kib=100), "compute", core.Ranking()
            ),
            50,
        )

    def test_preference_within_margin(self):
        rows = core.ranked(self.hosts, {"brooks": metric(load=0.4), "other": metric()})
        self.assertEqual(rows[0][0].alias, "brooks")

    def test_no_preference_outside_margin(self):
        rows = core.ranked(self.hosts, {"brooks": metric(load=2), "other": metric()})
        self.assertEqual(rows[0][0].alias, "other")

    def test_brooks_and_moore_equal_preference(self):
        rows = core.ranked(self.hosts, {"brooks": metric(load=0.4), "moore": metric()})
        self.assertEqual(rows[0][0].alias, "moore")

    def test_exact_tie_inventory_order(self):
        rows = core.ranked(self.hosts, {h.alias: metric() for h in self.hosts})
        self.assertEqual([h.alias for h, _ in rows], ["brooks", "moore", "other"])

    def test_compute_preference_boundary(self):
        rows = core.ranked(
            self.hosts,
            {"brooks": metric(cpus=96), "other": metric(cpus=100)},
            "compute",
        )
        self.assertEqual(rows[0][0].alias, "brooks")
        rows = core.ranked(
            self.hosts,
            {"brooks": metric(cpus=94), "other": metric(cpus=100)},
            "compute",
        )
        self.assertEqual(rows[0][0].alias, "other")

    def test_zero_scores_and_empty(self):
        self.assertEqual(core.ranked(self.hosts, {}), [])
        rows = core.ranked(
            self.hosts, {h.alias: metric(load=1000) for h in self.hosts}, "compute"
        )
        self.assertEqual(rows[0][0].alias, "brooks")


class CacheConfigTests(unittest.TestCase):
    def test_cache_expiry_corruption_and_future(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.json"
            core.write_cache(
                path,
                {
                    "fresh": metric(timestamp=990),
                    "expired": metric(timestamp=600),
                    "future": metric(timestamp=1010),
                },
            )
            self.assertEqual(list(core.read_cache(path, now=1000)), ["fresh"])
            path.write_text("garbage")
            self.assertEqual(core.read_cache(path), {})
            path.write_text("[]")
            self.assertEqual(core.read_cache(path), {})

    def test_cache_write_failure_is_optional(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "file"
            path.write_text("file blocks parent directory")
            core.write_cache(path / "metrics.json", {"brooks": metric()})

    def test_cache_ignores_oversized_numeric_entry_and_keeps_valid_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.json"
            valid = vars(metric())
            invalid = {**valid, "load": 10**400}
            path.write_text(json.dumps({"valid": valid, "invalid": invalid}))
            self.assertEqual(set(core.read_cache(path)), {"valid"})

    def test_oversized_ranking_setting_is_rejected_as_invalid(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                '[[hosts]]\nalias="test"\n[ranking]\nresponse_slow=' + str(10**400)
            )
            with self.assertRaisesRegex(ValueError, "Invalid ranking settings"):
                core.load_config(path)

    def test_invalid_metrics(self):
        for overrides in (
            {"cpus": 0},
            {"load": float("nan")},
            {"available_kib": 2000},
            {"swap_free_kib": 200},
            {"cpus": "8"},
            {"cpus": 10**400},
            {"total_kib": 10**400},
            {"swap_total_kib": 10**400},
        ):
            with (
                self.subTest(overrides=overrides),
                self.assertRaises((ValueError, TypeError)),
            ):
                metric(**overrides)

    def test_custom_inventory_and_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                '[[hosts]]\nalias="test"\npool="servers"\npreferred=true\n[ranking]\ntie_fraction=0.03\n'
            )
            hosts, ranking = core.load_config(path)
            self.assertEqual(hosts[0].alias, "test")
            self.assertEqual(ranking.tie_fraction, 0.03)
            path.write_text('[[hosts]]\nalias="-bad"\n')
            with self.assertRaises(ValueError):
                core.load_config(path)


class InterfaceTests(unittest.TestCase):
    def test_menu_startup_error_closes_scanner_and_restores_console(self):
        events = []

        class FakeConsole:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                events.append("restored")

            def draw(self, *args):
                pass

        with patch.object(cli, "read_cache", return_value={}):
            state = cli.Selection(
                [core.Host("a")], core.Ranking(), "responsive", "servers", "ssh"
            )
        with (
            patch.object(cli, "Console", FakeConsole),
            patch.object(
                state, "refresh", side_effect=lambda **kw: events.append("started")
            ),
            patch.object(
                cli.shutil, "get_terminal_size", side_effect=OSError("startup failure")
            ),
            patch.object(state, "close", side_effect=lambda: events.append("closed")),
            self.assertRaisesRegex(OSError, "startup failure"),
        ):
            cli.run_menu(state)
        self.assertEqual(events, ["started", "closed", "restored"])

    def test_list_startup_error_closes_selection(self):
        with patch.object(cli, "read_cache", return_value={}):
            state = cli.Selection(
                [core.Host("a")], core.Ranking(), "responsive", "servers", "ssh"
            )
        with (
            patch.object(cli.shutil, "which", return_value="ssh"),
            patch.object(
                cli, "load_config", return_value=(state.inventory, state.settings)
            ),
            patch.object(cli, "Selection", return_value=state),
            patch.object(state, "refresh", side_effect=OSError("startup failure")),
            patch.object(state, "close") as close,
            patch("sys.stderr"),
        ):
            self.assertEqual(cli.main(["--list"]), 2)
        close.assert_called_once()

    def test_boundary_preserves_arguments(self):
        args, forwarded = cli.arguments(
            [
                "brooks",
                "--mode",
                "compute",
                "--",
                "-XY",
                "-o",
                "RemoteCommand=echo hello",
            ]
        )
        self.assertEqual(args.destination, "brooks")
        self.assertEqual(forwarded, ["-XY", "-o", "RemoteCommand=echo hello"])

    def test_ssh_flags_require_boundary(self):
        with patch("sys.stderr"), self.assertRaises(SystemExit):
            cli.arguments(["-XY"])

    def test_explicit_host_bypasses_inventory_and_probes(self):
        with (
            patch.object(cli.shutil, "which", return_value="ssh"),
            patch.object(cli, "connect", return_value=42) as connect,
        ):
            with patch.object(
                cli, "load_config", side_effect=AssertionError("must bypass")
            ):
                self.assertEqual(cli.main(["user@host", "--", "-tt", "hostname"]), 42)
            connect.assert_called_once_with("user@host", ["-tt", "hostname"], "ssh")

    def test_redirected_menu_rejected(self):
        with (
            patch.object(cli.shutil, "which", return_value="ssh"),
            patch("sys.stdin.isatty", return_value=False),
            patch("sys.stderr"),
        ):
            self.assertEqual(cli.main([]), 2)

    def test_live_recommendation_and_failed_cache_removal(self):
        hosts = [
            core.Host("brooks", preferred=True),
            core.Host("moore", preferred=True),
        ]
        with patch.object(cli, "read_cache", return_value={"brooks": metric()}):
            state = cli.Selection(hosts, core.Ranking(), "responsive", "servers", "ssh")

        class FakeScanner:
            results: queue.Queue[ProbeResult] = queue.Queue()
            pause_until = 0

            def snapshot(self):
                return {}

        scanner = FakeScanner()
        scanner.results.put(ProbeResult("moore", Outcome.SUCCESS, metric()))
        scanner.results.put(
            ProbeResult("brooks", Outcome.AUTH, message="Authentication failed")
        )
        with patch.object(state, "scanner", scanner):
            self.assertTrue(state.collect())
        self.assertEqual(state.recommendation(), "moore")
        lines = state.lines()
        self.assertIn("1 brooks", lines[2])
        self.assertIn("2 moore", lines[3])
        self.assertNotIn("brooks", state.metrics)

    def test_menu_mode_pool_manual_selection_and_cleanup(self):
        hosts = [core.Host("brooks"), core.Host("desktop", "other")]
        with patch.object(cli, "read_cache", return_value={}):
            state = cli.Selection(hosts, core.Ranking(), "responsive", "servers", "ssh")
        keys = iter(["m", "p", "2", "\r"])

        class FakeConsole:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def draw(self, *args):
                pass

            def key(self):
                return next(keys)

        with (
            patch.object(cli, "Console", FakeConsole),
            patch.object(state, "refresh"),
            patch.object(state, "collect", return_value=False),
            patch.object(state, "close") as close,
            patch.object(state, "save"),
            patch.object(cli.time, "sleep"),
        ):
            self.assertEqual(cli.run_menu(state), "desktop")
            self.assertEqual(state.mode, "compute")
            close.assert_called()


class TransportTests(unittest.TestCase):
    def test_parse_metrics_with_banner(self):
        output = "Welcome\nLABSSH1\n8\n0.25 0.5 1 1/2 3\nMemTotal: 1000\nMemAvailable: 800\nSwapTotal: 100\nSwapFree: 99\n"
        self.assertEqual(transport.parse_metrics(output, 0.3).cpus, 8)
        with self.assertRaises(ValueError):
            transport.parse_metrics("LABSSH1\n8\n", 0.1)

    def test_probe_options_disable_side_effects(self):
        self.assertIn("-oStrictHostKeyChecking=yes", transport.OPTIONS)
        self.assertIn("-oClearAllForwardings=yes", transport.OPTIONS)
        self.assertIn("-oRemoteCommand=none", transport.OPTIONS)

    def test_error_categories(self):
        for source, wanted in (
            ("Host key verification failed", "Host key"),
            ("Permission denied (publickey)", "Authentication"),
            ("Could not resolve hostname foo", "resolved"),
            ("Connection refused", "refused"),
        ):
            self.assertIn(wanted, transport.error_reason(source))

    def test_process_cancellation(self):
        process = transport.ProbeProcess(
            [sys.executable, "-c", "import time; time.sleep(30)"]
        )
        start = time.perf_counter()
        process.close()
        process.process.communicate(timeout=2)
        self.assertLess(time.perf_counter() - start, 1)
        process.close()  # idempotent

    def test_process_tree_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "child-survived"
            child = f"import time; from pathlib import Path; time.sleep(1); Path({str(marker)!r}).write_text('bad')"
            parent = f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{child!r}]); print('started',flush=True); time.sleep(30)"
            process = transport.ProbeProcess([sys.executable, "-c", parent])
            assert process.process.stdout is not None
            self.assertEqual(process.process.stdout.readline().strip(), b"started")
            process.close()
            process.process.communicate(timeout=2)
            time.sleep(1.1)
            self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
