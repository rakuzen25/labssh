"""Traffic control and teardown tests; all subprocesses are local Python."""

import json
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Self
from unittest.mock import patch

from labssh import cli, core, transport
from labssh.coordination import Coordinator, Outcome
from labssh.locking import FileLock


def observation(timestamp: float | None = None) -> core.Metrics:
    return core.Metrics(
        8, 0, 1000, 800, 0, 0, 0.1, time.time() if timestamp is None else timestamp
    )


def wait_for(predicate: Callable[[], bool], timeout: float = 3) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("Local worker did not reach expected state")
        time.sleep(0.01)


class TestWorkspace(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.path = self.directory / "metrics.json"
        self.now = [1000.0]
        self.coordinator = Coordinator(self.directory, lambda: self.now[0])


class PacingTests(TestWorkspace):
    def test_manual_refresh_bypasses_host_cooldown_but_keeps_shared_pacing(
        self,
    ) -> None:
        for interval in (2, 5):
            with self.subTest(interval=interval):
                directory = self.directory / str(interval)
                first = Coordinator(directory, lambda: self.now[0])
                other = Coordinator(directory, lambda: self.now[0])
                self.now[0] = 1000
                with first.slot:
                    self.assertTrue(first.reserve("a", interval).allowed)
                    first.finish(Outcome.SUCCESS)
                self.now[0] = 1001
                with other.slot:
                    self.assertEqual(other.reserve("a", interval).retry_at, 1060)
                    forced = other.reserve("a", interval, force=True)
                    self.assertFalse(forced.allowed)
                    self.assertEqual(forced.wait_until, 1000 + interval)
                    self.now[0] = 1000 + interval
                    self.assertTrue(other.reserve("a", interval, force=True).allowed)
                    self.assertEqual(other.read().attempts["a"], self.now[0])
                    self.assertEqual(
                        other.reserve("a", interval).retry_at, self.now[0] + 60
                    )

    def test_manual_refresh_cannot_bypass_shared_timeout_pause(self) -> None:
        with self.coordinator.slot:
            self.coordinator.finish(Outcome.TIMEOUT)
            self.coordinator.finish(Outcome.TIMEOUT)
        other = Coordinator(self.directory, lambda: self.now[0])
        with other.slot:
            before = other.read()
            decision = other.reserve("a", 2, force=True)
            self.assertFalse(decision.allowed)
            self.assertTrue(decision.paused)
            self.assertEqual(decision.retry_at, 1300)
            self.assertEqual(other.read(), before)

    def test_two_second_gap_and_larger_transition(self) -> None:
        with self.coordinator.slot:
            self.assertTrue(self.coordinator.reserve("a", 2).allowed)
            self.coordinator.finish(Outcome.SUCCESS)
            self.now[0] = 1002
            self.assertEqual(self.coordinator.reserve("b", 5).wait_until, 1005)
            self.now[0] = 1005
            self.assertTrue(self.coordinator.reserve("b", 5).allowed)
            self.now[0] = 1007
            self.assertEqual(self.coordinator.reserve("c", 2).wait_until, 1010)

    def test_attempt_record_is_immediate_and_persists_after_cancellation(self) -> None:
        with self.coordinator.slot:
            self.assertTrue(self.coordinator.reserve("a", 2).allowed)
            self.assertEqual(self.coordinator.read().attempts["a"], 1000)
            self.coordinator.finish(Outcome.CANCELLED)
        other = Coordinator(self.directory, lambda: 1010)
        with other.slot:
            self.assertEqual(other.reserve("a", 2).retry_at, 1060)

    def test_two_timeouts_pause_other_instances_and_expire(self) -> None:
        with self.coordinator.slot:
            self.coordinator.reserve("a", 2)
            self.coordinator.finish(Outcome.TIMEOUT)
            self.now[0] = 1003
            self.coordinator.reserve("b", 2)
            self.assertEqual(self.coordinator.finish(Outcome.TIMEOUT), 1303)
        other = Coordinator(self.directory, lambda: self.now[0])
        with other.slot:
            self.assertTrue(other.reserve("c", 2).paused)
            self.now[0] = 1303
            self.assertTrue(other.reserve("c", 2).allowed)
            self.assertEqual(other.read().timeouts, 0)

    def test_auth_host_key_and_success_reset_timeout_streak(self) -> None:
        for outcome in (Outcome.AUTH, Outcome.HOST_KEY, Outcome.SUCCESS, Outcome.ERROR):
            with self.coordinator.slot:
                self.coordinator.finish(Outcome.TIMEOUT)
                self.coordinator.finish(outcome)
                self.assertEqual(self.coordinator.read().timeouts, 0)
                self.assertEqual(self.coordinator.read().pause_until, 0)

    def test_cancellation_does_not_change_timeout_streak(self) -> None:
        with self.coordinator.slot:
            self.coordinator.finish(Outcome.TIMEOUT)
            self.coordinator.finish(Outcome.CANCELLED)
            self.assertEqual(self.coordinator.read().timeouts, 1)

    def test_corrupt_state_fails_closed(self) -> None:
        self.coordinator.path.write_text('{"timeouts": "bad"}', encoding="utf-8")
        with self.coordinator.slot, self.assertRaises(OSError):
            self.coordinator.reserve("a", 2)

    def test_oversized_coordination_number_fails_closed_with_diagnostic(self) -> None:
        self.coordinator.path.write_text(json.dumps({"last_start": 10**400}))
        with self.assertRaisesRegex(OSError, "coordination state is invalid"):
            self.coordinator.read()

    def test_five_minute_cache_and_merge_keeps_newest_aliases(self) -> None:
        now = time.time()
        core.write_cache(self.path, {"a": observation(now), "b": observation(now)})
        core.write_cache(self.path, {"a": observation(now - 1), "c": observation(now)})
        result = core.read_cache(self.path, now=now)
        self.assertEqual(set(result), {"a", "b", "c"})
        self.assertEqual(result["a"].timestamp, now)
        self.assertIn("a", core.read_cache(self.path, now=now + 300))
        self.assertNotIn("a", core.read_cache(self.path, now=now + 301))

    def test_failed_attempt_does_not_remove_newer_measurement(self) -> None:
        now = time.time()
        core.write_cache(self.path, {"a": observation(now)})
        core.invalidate_cache(self.path, "a", now - 1)
        self.assertIn("a", core.read_cache(self.path))
        core.invalidate_cache(self.path, "a", now)
        self.assertNotIn("a", core.read_cache(self.path))

    def test_cache_corruption_is_optional_but_coordination_corruption_is_not(
        self,
    ) -> None:
        self.path.write_text("garbage", encoding="utf-8")
        self.assertEqual(core.read_cache(self.path), {})
        self.coordinator.path.write_text("garbage", encoding="utf-8")
        with patch.object(transport, "ProbeProcess") as process:
            scanner = transport.Scanner([core.Host("a")], path=self.path)
            scanner.thread.join(2)
            scanner.close()
            process.assert_not_called()
            self.assertIn("disabled", scanner.results.get_nowait().message)

    def test_lock_excludes_another_process_and_releases_after_crash(self) -> None:
        lock = FileLock(self.directory / "shared.lock")
        code = (
            "from pathlib import Path; from labssh.locking import FileLock; "
            f"lock=FileLock(Path({str(lock.path)!r})); "
            "print(lock.try_acquire(),flush=True); lock.release()"
        )
        with lock:
            result = subprocess.run(
                [sys.executable, "-c", code], capture_output=True, text=True, check=True
            )
            self.assertEqual(result.stdout.strip(), "False")
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=True
        )
        self.assertEqual(result.stdout.strip(), "True")
        owner = (
            "import time; from pathlib import Path; from labssh.locking import FileLock; "
            f"lock=FileLock(Path({str(lock.path)!r})); lock.acquire(); "
            "print('ready',flush=True); time.sleep(30)"
        )
        with subprocess.Popen(
            [sys.executable, "-c", owner], stdout=subprocess.PIPE
        ) as process:
            assert process.stdout is not None
            self.assertEqual(process.stdout.readline().strip(), b"ready")
            process.kill()
            process.wait(2)
        wait_for(lock.try_acquire)
        lock.release()


class ScannerTests(TestWorkspace):
    def local_success(self, command: list[str]) -> transport.ProbeProcess:
        self.starts.append(self.now[0])
        output = "LABSSH1\n8\n0 0 0 1/2 3\nMemTotal: 1000\nMemAvailable: 800\nSwapTotal: 0\nSwapFree: 0\n"
        return self.original_process([sys.executable, "-c", f"print({output!r})"])

    def setUp(self) -> None:
        super().setUp()
        self.starts: list[float] = []
        self.original_process = transport.ProbeProcess

    def scanner(
        self, hosts: list[str], *, force: bool = False, timeout: float = 3
    ) -> transport.Scanner:
        scanner = transport.Scanner(
            [core.Host(alias) for alias in hosts],
            path=self.path,
            coordinator=self.coordinator,
            force=force,
            timeout=timeout,
        )
        self.addCleanup(scanner.close)
        return scanner

    def test_fresh_cache_never_starts_a_process(self) -> None:
        core.write_cache(self.path, {"a": observation()})
        with patch.object(transport, "ProbeProcess") as process:
            scanner = self.scanner(["a"])
            self.assertTrue(scanner.done.wait(2))
            self.assertEqual(scanner.results.get_nowait().outcome, Outcome.SUCCESS)
            self.assertEqual(scanner.snapshot()["a"].label, "cached")
            process.assert_not_called()

    def test_manual_refresh_reprobes_recent_host_with_fresh_cache(self) -> None:
        with self.coordinator.slot:
            self.assertTrue(self.coordinator.reserve("a", 2).allowed)
            self.coordinator.finish(Outcome.SUCCESS)
        self.now[0] += 2
        with patch.object(transport, "ProbeProcess") as process:
            automatic = self.scanner(["a"])
            self.assertTrue(automatic.done.wait(2))
            self.assertEqual(automatic.results.get_nowait().outcome, Outcome.SKIPPED)
            process.assert_not_called()
        core.write_cache(self.path, {"a": observation()})
        with patch.object(transport, "ProbeProcess", self.local_success):
            manual = self.scanner(["a"], force=True)
            self.assertTrue(manual.done.wait(2))
            self.assertEqual(manual.results.get_nowait().outcome, Outcome.SUCCESS)
            self.assertEqual(manual.snapshot()["a"].label, "ready")
        self.assertEqual(self.starts, [1002])

    def test_refresh_upgrades_waiting_scan_without_duplicate_probes(self) -> None:
        core.write_cache(self.path, {"a": observation(), "b": observation()})
        with patch.object(transport, "ProbeProcess", self.local_success):
            with FileLock(self.directory / "probe.lock"):
                scanner = self.scanner(["a", "b"])
                worker = scanner.thread
                for _ in range(5):
                    self.assertTrue(scanner.request([core.Host("a"), core.Host("b")]))
                self.assertIs(scanner.thread, worker)
            self.assertEqual(scanner.results.get(timeout=2).alias, "a")
            self.now[0] += 2
            self.assertTrue(scanner.done.wait(2))
            self.assertEqual(scanner.results.get_nowait().alias, "b")
            self.assertTrue(scanner.results.empty())
            self.assertEqual(self.starts, [1000, 1002])

    def test_serial_gap_with_fake_clock(self) -> None:
        with patch.object(transport, "ProbeProcess", self.local_success):
            scanner = self.scanner(["a", "b"], force=True)
            self.assertEqual(scanner.results.get(timeout=2).alias, "a")
            time.sleep(0.05)
            self.assertEqual(len(self.starts), 1)
            self.now[0] += 2
            self.assertEqual(scanner.results.get(timeout=2).alias, "b")
            self.assertEqual(self.starts, [1000, 1002])
            self.assertEqual(len(scanner.threads), 1)

    def test_refresh_coalesces_and_cancel_interrupts_pacing(self) -> None:
        with patch.object(transport, "ProbeProcess", self.local_success):
            scanner = self.scanner(["a", "b"], force=True)
            scanner.results.get(timeout=2)
            self.assertTrue(
                scanner.request(
                    [core.Host("a"), core.Host("b"), core.Host("c")], pool_size=28
                )
            )
            self.assertEqual(set(scanner.requested), {"a", "b", "c"})
            start = time.perf_counter()
            scanner.close()
            self.assertLess(time.perf_counter() - start, 0.3)
            self.assertFalse(scanner.thread.is_alive())
            self.assertEqual(len(self.starts), 1)
            self.assertEqual(scanner.pending, {})
        check = FileLock(self.directory / "probe.lock")
        self.assertTrue(check.try_acquire())
        check.release()

    def test_two_timeouts_cancel_remaining_hosts_without_waiting(self) -> None:
        def timeout_process(command: list[str]) -> transport.ProbeProcess:
            self.starts.append(self.now[0])
            return self.original_process(
                [sys.executable, "-c", "import time;time.sleep(30)"]
            )

        with patch.object(transport, "ProbeProcess", timeout_process):
            scanner = self.scanner(["a", "b", "c"], force=True, timeout=0.05)
            self.assertEqual(scanner.results.get(timeout=2).outcome, Outcome.TIMEOUT)
            self.now[0] += 2
            self.assertEqual(scanner.results.get(timeout=2).outcome, Outcome.TIMEOUT)
            self.assertTrue(scanner.done.wait(2))
            self.assertEqual(scanner.snapshot()["c"].label, "cooldown")
            self.assertEqual(len(self.starts), 2)

    def test_close_reaps_active_process_and_child_before_returning(self) -> None:
        started = self.directory / "started"
        survived = self.directory / "survived"
        child = f"import time;from pathlib import Path;time.sleep(1);Path({str(survived)!r}).touch()"
        parent = (
            "import subprocess,sys,time;from pathlib import Path;"
            f"subprocess.Popen([sys.executable,'-c',{child!r}]);"
            f"Path({str(started)!r}).touch();time.sleep(30)"
        )
        processes: list[transport.ProbeProcess] = []

        def create(command: list[str]) -> transport.ProbeProcess:
            process = self.original_process([sys.executable, "-c", parent])
            processes.append(process)
            return process

        with patch.object(transport, "ProbeProcess", create):
            scanner = self.scanner(["a", "b"], force=True)
            wait_for(started.exists)
            start = time.perf_counter()
            scanner.close()
            self.assertLess(time.perf_counter() - start, 0.5)
            self.assertFalse(scanner.thread.is_alive())
            self.assertIsNotNone(processes[0].process.poll())
            self.assertIsNone(scanner.active)
            check = FileLock(self.directory / "probe.lock")
            self.assertTrue(check.try_acquire())
            check.release()
            time.sleep(1.1)
            self.assertFalse(survived.exists())

    def test_cancel_while_waiting_for_another_instance(self) -> None:
        with (
            FileLock(self.directory / "probe.lock"),
            patch.object(transport, "ProbeProcess") as process,
        ):
            scanner = self.scanner(["a"], force=True)
            time.sleep(0.05)
            scanner.close()
            process.assert_not_called()
            self.assertFalse(scanner.thread.is_alive())

    def test_menu_finishes_cleanup_before_ssh_handoff(self) -> None:
        for phase in ("pacing", "active", "automatic"):
            with self.subTest(phase=phase):
                self.check_menu_handoff(phase)

    def check_menu_handoff(self, phase: str) -> None:
        self.now[0] += 100
        aliases = [phase + "1", phase + "2"]

        def create(command: list[str]) -> transport.ProbeProcess:
            if phase == "pacing":
                return self.local_success(command)
            return self.original_process(
                [sys.executable, "-c", "import time;time.sleep(30)"]
            )

        with patch.object(transport, "ProbeProcess", create):
            scanner = self.scanner(aliases, force=True)
            if phase == "pacing":
                scanner.results.get(timeout=2)
            else:
                wait_for(lambda: scanner.active is not None)
            hosts = [core.Host(alias) for alias in aliases]
            with patch.object(cli, "read_cache", return_value={}):
                state = cli.Selection(
                    hosts, core.Ranking(), "responsive", "servers", "ssh"
                )
            state.path = self.path
            state.scanner = scanner
            if phase == "automatic":
                state.metrics[aliases[0]] = observation()
            keys = iter(["1", "\r"])

            class FakeConsole:
                def __enter__(self) -> Self:
                    return self

                def __exit__(self, *args: object) -> None:
                    pass

                def draw(self, *args: object) -> None:
                    pass

                def key(self) -> str | None:
                    return None if phase == "automatic" else next(keys)

            def connect(*args: object) -> int:
                self.assertFalse(scanner.thread.is_alive())
                self.assertEqual(scanner.pending, {})
                self.assertIsNone(scanner.active)
                check = FileLock(self.directory / "probe.lock")
                self.assertTrue(check.try_acquire())
                check.release()
                return 7

            with (
                patch.object(cli, "Console", FakeConsole),
                patch.object(cli, "Selection", return_value=state),
                patch.object(cli, "load_config", return_value=(hosts, core.Ranking())),
                patch.object(shutil, "which", return_value="ssh"),
                patch.object(cli, "connect", side_effect=connect),
                patch.object(time, "monotonic", side_effect=[0, 0, 3, 3]),
                patch.object(state, "refresh"),
                patch("sys.stdin.isatty", return_value=True),
                patch("sys.stdout.isatty", return_value=True),
            ):
                self.assertEqual(cli.main([]), 7)


class SelectionPolicyTests(unittest.TestCase):
    def state(self) -> cli.Selection:
        hosts = [core.Host("a"), core.Host("b"), core.Host("desktop", "other")]
        with patch.object(cli, "read_cache", return_value={"a": observation()}):
            return cli.Selection(hosts, core.Ranking(), "responsive", "all", "ssh")

    def test_startup_fills_only_default_server_gaps(self) -> None:
        state = self.state()
        with patch.object(cli, "Scanner") as scanner:
            state.refresh(startup=True)
            args, kwargs = scanner.call_args
            self.assertEqual([h.alias for h in args[0]], ["b"])
            self.assertFalse(kwargs["force"])
            self.assertEqual(kwargs["pool_size"], 2)

    def test_explicit_refresh_coalesces_without_restarting(self) -> None:
        state = self.state()
        with patch.object(cli, "Scanner") as scanner:
            state.refresh(startup=True)
            state.refresh()
            self.assertEqual(scanner.call_count, 1)
            request = scanner.return_value.request
            self.assertEqual(
                [h.alias for h in request.call_args.args[0]], ["a", "b", "desktop"]
            )
            scanner.return_value.close.assert_not_called()

    def test_pool_switch_never_starts_a_scan(self) -> None:
        state = self.state()
        keys = iter(["p", "q"])

        class FakeConsole:
            def __enter__(self) -> Self:
                return self

            def __exit__(self, *args: object) -> None:
                pass

            def draw(self, *args: object) -> None:
                pass

            def key(self) -> str | None:
                return next(keys)

        with (
            patch.object(cli, "Console", FakeConsole),
            patch.object(state, "refresh") as refresh,
            patch.object(state, "collect", return_value=False),
            patch.object(state, "close"),
            patch.object(time, "sleep"),
        ):
            cli.run_menu(state)
        refresh.assert_called_once_with(startup=True)


if __name__ == "__main__":
    unittest.main()
