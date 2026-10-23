"""Offline presentation fixtures and interaction regressions."""

import os
import re
import subprocess
import sys
import time
import unittest
from dataclasses import replace
from unittest.mock import patch

from rich.cells import cell_len

from labssh import cli, core
from labssh.coordination import ProbeStatus
from labssh.presentation import MenuRenderer

ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def fixture():
    hosts = [
        core.Host("brooks", preferred=True),
        core.Host("moore", preferred=True),
        core.Host("hanlon"),
        core.Host("finagle"),
    ]
    with patch.object(cli, "read_cache", return_value={}):
        state = cli.Selection(hosts, core.Ranking(), "responsive", "servers", "ssh")
    state.metrics["brooks"] = core.Metrics(
        128, 0, 260000000, 244000000, 0, 0, 0.72, time.time()
    )
    state.metrics["moore"] = core.Metrics(
        64, 18.56, 150000000, 110000000, 1000000, 500000, 1.15, time.time()
    )
    state.errors["hanlon"] = "Host key not trusted; try manual SSH"
    state.completed = {"brooks", "moore", "hanlon"}
    state.statuses["finagle"] = ProbeStatus("queued")
    return state


class PresentationTests(unittest.TestCase):
    def setUp(self):
        self.state = fixture()
        self.renderer = MenuRenderer()

    def render(self, **kwargs):
        return self.renderer.render(self.state, **kwargs)

    def test_essential_metrics_and_single_detail_line(self):
        text = ANSI.sub("", self.render(width=100))
        self.assertIn("Load/core", text)
        self.assertIn("Free GiB", text)
        self.assertIn("Score", text)
        self.assertIn("›1", text)
        self.assertEqual(text.count("SSH 720 ms"), 1)
        self.assertNotIn("live recommendation", text)
        self.assertNotIn("1,150", text)

    def test_color_roles(self):
        with patch.dict(os.environ, {}, clear=True):
            text = self.render(width=100)
        self.assertIn("\x1b[32m", text)  # recommendation
        self.assertIn("\x1b[33m", text)  # pending
        self.assertIn("\x1b[31m", text)  # error

    def test_no_color_disables_all_sgr(self):
        for value in ("1", ""):
            with patch.dict(os.environ, {"NO_COLOR": value}):
                self.assertNotIn("\x1b", self.render(width=100))

    def test_widths_and_heights_do_not_overflow(self):
        for mode in ("responsive", "compute"):
            self.state.mode = mode
            for width in (20, 27, 32, 39, 40, 54, 55, 69, 70, 80, 100):
                for height in (4, 8, 12, 24):
                    with self.subTest(mode=mode, width=width, height=height):
                        text = ANSI.sub("", self.render(width=width, height=height))
                        self.assertTrue(
                            all(cell_len(line) <= width for line in text.splitlines())
                        )
                        self.assertLessEqual(len(text.splitlines()), height)

    def test_compute_columns_distinguish_spare_cores_from_memory_adjusted_capacity(
        self,
    ):
        self.state.mode = "compute"
        self.state.metrics["moore"] = replace(
            self.state.metrics["moore"], available_kib=15000000
        )
        text = ANSI.sub("", self.render(typed="2", width=120))
        self.assertIn("Spare cores", text)
        self.assertIn("Capacity", text)
        self.assertNotIn("Load/core", text)
        self.assertNotIn("Score", text)
        row = next(
            line for line in text.splitlines() if "moore" in line and "CPUs" not in line
        )
        self.assertIn("45.4", row)
        self.assertIn("22.7", row)
        self.assertIn("64 CPUs · load 18.56 · RAM 10.0% free", text)
        self.assertIn("s ago", text)
        self.assertNotIn("SSH 1150", text)
        self.assertIn("Capacity", self.state.lines()[1])
        self.state.mode = "responsive"
        switched = ANSI.sub("", self.render(width=100))
        self.assertIn("Load/core", switched)
        self.assertIn("Score", switched)
        self.assertIn("SSH 720 ms", switched)

    def test_rows_keep_numeric_columns_aligned(self):
        text = ANSI.sub("", self.render(width=100))
        rows = [
            line
            for line in text.splitlines()
            if any(h.alias in line for h in self.state.hosts)
        ][:4]
        load_columns = []
        for row in rows:
            match = re.search(r"\s(0\.00|0\.29|—)\s", row)
            assert match is not None, f"Missing load column in row: {row!r}"
            load_columns.append(match.start())
        # Matching starts differ when the value is a single em dash. Compare
        # measured rows, which both have a fixed-width two-decimal value.
        self.assertEqual(load_columns[0], load_columns[1])

    def test_numbered_choice_details_and_error(self):
        text = ANSI.sub("", self.render(typed="3", width=100))
        self.assertIn("Host key not trusted; try manual SSH", text)
        self.assertIn("[3]", text)
        self.assertNotIn("SSH 720 ms", text)
        # Recommendation stays Brooks while inspecting another row.
        self.assertIn("›1", text)

    def test_cached_and_swap_details(self):
        self.state.cached.add("moore")
        text = ANSI.sub("", self.render(typed="2", width=120))
        self.assertIn("cached", text)
        self.assertIn("swap", text)

    def test_live_recommendation_changes_without_reordering(self):
        before = ANSI.sub("", self.render(width=100))
        self.state.metrics.pop("brooks")
        self.state.errors["brooks"] = "Timed out"
        after = ANSI.sub("", self.render(width=100))
        self.assertIn("›1", before)
        self.assertIn("›2", after)
        self.assertLess(after.index("brooks"), after.index("moore"))

    def test_number_choice_brings_hidden_row_into_view(self):
        self.state.inventory.extend(
            core.Host(f"desktop{i}", "other") for i in range(24)
        )
        self.state.pool = "all"
        text = ANSI.sub("", self.render(typed="28", width=80, height=10))
        self.assertIn("desktop23", text)
        self.assertIn("of 28", text)
        self.assertIn("Enter", text)
        self.assertLessEqual(len(text.splitlines()), 10)

    def test_terminal_control_and_markup_in_errors_are_literal(self):
        self.state.errors["hanlon"] = "[bold]failed[/bold]\x1b[2J\nerror"
        text = ANSI.sub("", self.render(typed="3", width=120))
        self.assertIn("[bold]failed[/bold]", text)
        self.assertNotIn("\x1b", text)

    def test_explicit_host_does_not_import_rich(self):
        code = (
            "import sys; from labssh import cli; "
            "cli.shutil.which=lambda _: 'ssh'; cli.connect=lambda *a: 0; "
            "assert cli.main(['example.invalid','--','-XY']) == 0; "
            "assert not any(k == 'rich' or k.startswith('rich.') for k in sys.modules)"
        )
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=False
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_terminal_cleanup_on_render_error(self):
        class FakeConsole:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                events.append("restored")

            def draw(self, *args):
                events.append("draw")
                if len(events) > 1:
                    raise OSError("render failure")

        events = []
        with (
            patch.object(cli, "Console", FakeConsole),
            patch.object(self.state, "refresh"),
            patch.object(self.state, "collect", return_value=True),
            patch.object(self.state, "close") as close,
            patch.object(self.state, "save"),
            self.assertRaises(OSError),
        ):
            cli.run_menu(self.state)
        self.assertEqual(events[-1], "restored")
        close.assert_called_once()

    def test_idle_menu_redraws_once_per_second_without_new_probes(self):
        renders = []
        keys = iter([None, None, None, "q"])

        class FakeConsole:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def draw(self, *args):
                renders.append(args)

            def key(self):
                return next(keys)

        with (
            patch.object(cli, "Console", FakeConsole),
            patch.object(self.state, "refresh") as refresh,
            patch.object(self.state, "collect", return_value=False),
            patch.object(self.state, "close"),
            patch.object(self.state, "save"),
            patch.object(cli.time, "sleep"),
            patch.object(cli.time, "monotonic", side_effect=[0, 0, 0.4, 1.1, 1.2]),
        ):
            self.assertIsNone(cli.run_menu(self.state))
        # Initial paint, initial scan paint, then one clock tick; not every poll.
        self.assertEqual(len(renders), 3)
        refresh.assert_called_once_with(startup=True)

    def test_measurement_age_advances_in_both_modes(self):
        self.state.metrics["brooks"] = replace(
            self.state.metrics["brooks"], timestamp=100
        )
        for mode in ("responsive", "compute"):
            self.state.mode = mode
            with patch("labssh.presentation.time.time", return_value=102):
                before = ANSI.sub("", self.render(width=120))
            with patch("labssh.presentation.time.time", return_value=103):
                after = ANSI.sub("", self.render(width=120))
            self.assertIn("2s ago", before)
            self.assertIn("3s ago", after)


if __name__ == "__main__":
    unittest.main()
