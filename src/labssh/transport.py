"""Bounded read-only SSH probes, with cancellable process trees."""

from __future__ import annotations

import os
import queue
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, cast

from .coordination import Coordinator, Outcome, ProbeResult, ProbeStatus
from .core import Host, Metrics, cache_path, invalidate_cache, read_cache, write_cache

# A fixed remote program: no aliases or user input are interpolated into it.
REMOTE = (
    "printf 'LABSSH1\\n'; getconf _NPROCESSORS_ONLN; cat /proc/loadavg; "
    "awk '/^(MemTotal|MemAvailable|SwapTotal|SwapFree):/ {print $1, $2}' /proc/meminfo"
)
OPTIONS = [
    "-T",
    "-oBatchMode=yes",
    "-oNumberOfPasswordPrompts=0",
    "-oStrictHostKeyChecking=yes",
    "-oUpdateHostKeys=no",
    "-oForwardAgent=no",
    "-oForwardX11=no",
    "-oClearAllForwardings=yes",
    "-oRemoteCommand=none",
    "-oRequestTTY=no",
    "-oStdinNull=yes",
    "-oControlMaster=no",
    "-oControlPath=none",
    "-oConnectTimeout=3",
    "-oConnectionAttempts=1",
    "-oPermitLocalCommand=no",
    "-oLogLevel=ERROR",
]


def parse_metrics(output, seconds):
    lines = output.splitlines()
    try:
        start = lines.index("LABSSH1")
        cpus = int(lines[start + 1])
        load = float(lines[start + 2].split()[0])
        memory = dict(line.split() for line in lines[start + 3 :] if ":" in line)
        return Metrics.checked(
            {
                "cpus": cpus,
                "load": load,
                "total_kib": int(memory["MemTotal:"]),
                "available_kib": int(memory["MemAvailable:"]),
                "swap_total_kib": int(memory["SwapTotal:"]),
                "swap_free_kib": int(memory["SwapFree:"]),
                "seconds": seconds,
                "timestamp": time.time(),
            }
        )
    except (IndexError, KeyError, TypeError, ValueError) as exc:
        raise ValueError("Malformed or unsupported Linux metrics") from exc


def error_reason(stderr):
    lowered = stderr.lower()
    for needle, label in (
        ("host key", "Host key not trusted/changed; try manual SSH"),
        ("permission denied", "Authentication failed; try manual SSH"),
        ("resolve hostname", "Hostname could not be resolved"),
        ("connection refused", "Connection refused"),
        ("timed out", "Timed out"),
    ):
        if needle in lowered:
            return label
    # Avoid terminal control characters from banners or remote error output.
    clean = " ".join("".join(c for c in stderr if c.isprintable() or c == "\n").split())
    return clean[-120:] or "SSH probe failed"


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_ulonglong)
            for name in (
                "ReadOperationCount",
                "WriteOperationCount",
                "OtherOperationCount",
                "ReadTransferCount",
                "WriteTransferCount",
                "OtherTransferCount",
            )
        ]

    class BASIC_LIMIT(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class EXTENDED_LIMIT(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BASIC_LIMIT),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    nt = ctypes.WinDLL("ntdll")
    nt.NtResumeProcess.argtypes = [wintypes.HANDLE]
    nt.NtResumeProcess.restype = ctypes.c_long


class ProbeProcess:
    def __init__(self, command: list[str]) -> None:
        self.job: Any = None
        self.closed = False
        kwargs: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
        }
        if os.name == "nt":
            kwargs["creationflags"] = (
                subprocess.CREATE_NO_WINDOW | 0x4
            )  # CREATE_SUSPENDED
        else:
            kwargs["start_new_session"] = True
        self.process: subprocess.Popen[bytes] = subprocess.Popen(command, **kwargs)
        if os.name == "nt":
            try:
                self.job = kernel.CreateJobObjectW(None, None)
                if not self.job:
                    raise ctypes.WinError(ctypes.get_last_error())
                limits = EXTENDED_LIMIT()
                limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
                if not kernel.SetInformationJobObject(
                    self.job, 9, ctypes.byref(limits), ctypes.sizeof(limits)
                ):
                    raise ctypes.WinError(ctypes.get_last_error())
                # Popen's native Windows handle is absent from its public stub.
                process_handle = int(cast(Any, self.process)._handle)
                if not kernel.AssignProcessToJobObject(self.job, process_handle):
                    raise ctypes.WinError(ctypes.get_last_error())
                if nt.NtResumeProcess(process_handle) != 0:
                    raise OSError("Could not resume probe")
            except BaseException:
                self.process.kill()
                self.close()
                self.process.communicate()
                raise

    def close(self):
        if self.closed:
            return
        self.closed = True
        if os.name == "nt":
            if self.job:
                kernel.CloseHandle(self.job)
                self.job = None
        else:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


class Scanner:
    """One worker, shared pacing, and synchronous cancellation before handoff."""

    def __init__(
        self,
        hosts: list[Host],
        ssh: str = "ssh",
        timeout: float = 3,
        *,
        path: Path | None = None,
        force: bool = False,
        pool_size: int | None = None,
        coordinator: Coordinator | None = None,
    ) -> None:
        self.results: queue.Queue[ProbeResult] = queue.Queue()
        self.path = path if path is not None else cache_path()
        self.coordinator = (
            coordinator if coordinator is not None else Coordinator(self.path.parent)
        )
        self.ssh, self.timeout = ssh, timeout
        self.done = threading.Event()
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.active: ProbeProcess | None = None
        self.pending: dict[str, tuple[Host, bool, float]] = {}
        self.requested: set[str] = set()
        self.statuses: dict[str, ProbeStatus] = {}
        self.pause_until = 0.0
        self.interval = 2.0
        self.request(hosts, force=force, pool_size=pool_size)
        self.thread = threading.Thread(target=self._worker, daemon=True)
        self.threads = [self.thread]
        self.thread.start()

    def request(
        self, hosts: list[Host], *, force: bool = True, pool_size: int | None = None
    ) -> bool:
        interval = (
            5.0 if (pool_size if pool_size is not None else len(hosts)) > 4 else 2.0
        )
        with self.lock:
            if self.done.is_set() or self.stop.is_set():
                return False
            self.interval = max(self.interval, interval)
            for alias, (host, requested_force, gap) in list(self.pending.items()):
                self.pending[alias] = (host, requested_force, max(interval, gap))
            for host in hosts:
                if host.alias not in self.requested:
                    self.requested.add(host.alias)
                    self.pending[host.alias] = (host, force, interval)
                    self.statuses[host.alias] = ProbeStatus("queued")
        return True

    def snapshot(self) -> dict[str, ProbeStatus]:
        with self.lock:
            return self.statuses.copy()

    def _status(
        self, alias: str, label: str, message: str = "", retry_at: float = 0
    ) -> None:
        with self.lock:
            self.statuses[alias] = ProbeStatus(label, message, retry_at)

    def _pause(self, until: float) -> None:
        self.pause_until = until
        with self.lock:
            for alias in self.pending:
                self.statuses[alias] = ProbeStatus("cooldown", "Probing paused", until)
            self.pending.clear()

    def _worker(self) -> None:
        try:
            while not self.stop.is_set():
                with self.lock:
                    if not self.pending:
                        self.done.set()
                        return
                    alias = next(iter(self.pending))
                    host, force, interval = self.pending.pop(alias)
                try:
                    result = self._host(host, force, interval)
                except (OSError, ValueError) as exc:
                    # Coordination errors fail closed; never fall back to an
                    # uncoordinated subprocess or reset the persistent limits.
                    message = f"Probing disabled: {exc}"
                    self._status(alias, "error", message)
                    self.results.put(ProbeResult(alias, Outcome.ERROR, message=message))
                    with self.lock:
                        for pending_alias in self.pending:
                            self.statuses[pending_alias] = ProbeStatus("error", message)
                        self.pending.clear()
                    return
                if not self.stop.is_set():
                    self.results.put(result)
        finally:
            self.done.set()

    def _host(self, host: Host, force: bool, interval: float) -> ProbeResult:
        alias = host.alias
        slot = self.coordinator.slot
        while not self.stop.is_set():
            if not slot.acquire(self.stop):
                break
            try:
                # Recheck after acquiring the cross-process slot: another
                # window may have measured this host while we were waiting.
                metrics = read_cache(self.path).get(alias)
                if not force and metrics is not None:
                    self._status(alias, "cached")
                    return ProbeResult(alias, Outcome.SUCCESS, metrics)
                with self.lock:
                    interval = max(interval, self.interval)
                decision = self.coordinator.reserve(alias, interval)
                if decision.paused:
                    self._status(alias, "cooldown", "Probing paused", decision.retry_at)
                    self._pause(decision.retry_at)
                    return ProbeResult(
                        alias,
                        Outcome.SKIPPED,
                        message="Probing paused",
                        retry_at=decision.retry_at,
                    )
                if decision.retry_at:
                    self._status(
                        alias, "cooldown", "Host attempt cooldown", decision.retry_at
                    )
                    return ProbeResult(
                        alias, Outcome.SKIPPED, metrics, retry_at=decision.retry_at
                    )
                if decision.allowed:
                    attempted_at = self.coordinator.clock()
                    result = self._probe(alias)
                    if result.metrics is not None:
                        write_cache(self.path, {alias: result.metrics}, self.stop)
                    elif result.outcome not in (Outcome.CANCELLED, Outcome.SKIPPED):
                        invalidate_cache(self.path, alias, attempted_at, self.stop)
                    pause = self.coordinator.finish(result.outcome)
                    if pause > self.coordinator.clock():
                        self._pause(pause)
                    return result
                wait = max(0.01, decision.wait_until - self.coordinator.clock())
            finally:
                slot.release()
            self.stop.wait(min(wait, 0.1))
        return ProbeResult(alias, Outcome.CANCELLED)

    def _probe(self, alias: str) -> ProbeResult:
        process = None
        start = time.perf_counter()
        try:
            with self.lock:
                if self.stop.is_set():
                    return ProbeResult(alias, Outcome.CANCELLED)
                process = ProbeProcess([self.ssh, *OPTIONS, alias, REMOTE])
                self.active = process
                self.statuses[alias] = ProbeStatus("checking")
            remaining = max(0.001, self.timeout - (time.perf_counter() - start))
            stdout, stderr = process.process.communicate(timeout=remaining)
            if self.stop.is_set():
                return ProbeResult(alias, Outcome.CANCELLED)
            if process.process.returncode:
                error = stderr.decode("utf-8", "replace")
                lowered = error.lower()
                outcome = (
                    Outcome.TIMEOUT
                    if "timed out" in lowered
                    else Outcome.HOST_KEY
                    if "host key" in lowered
                    else Outcome.AUTH
                    if "permission denied" in lowered
                    else Outcome.ERROR
                )
                result = ProbeResult(alias, outcome, message=error_reason(error))
            else:
                metrics = parse_metrics(
                    stdout.decode("utf-8", "replace"), time.perf_counter() - start
                )
                result = ProbeResult(alias, Outcome.SUCCESS, metrics)
        except subprocess.TimeoutExpired:
            assert process is not None
            process.close()
            process.process.communicate()
            result = (
                ProbeResult(alias, Outcome.CANCELLED)
                if self.stop.is_set()
                else ProbeResult(
                    alias, Outcome.TIMEOUT, message=f"Timed out ({self.timeout:g} s)"
                )
            )
        except (OSError, ValueError) as exc:
            result = ProbeResult(alias, Outcome.ERROR, message=str(exc))
        finally:
            if process is not None:
                with self.lock:
                    process.close()
                    self.active = None
                if process.process.poll() is None:
                    process.process.communicate()
        label = "ready" if result.outcome == Outcome.SUCCESS else "error"
        self._status(alias, label, result.message)
        return result

    def close(self) -> None:
        # Signal first so no queued/waiting work can start after cancellation.
        self.stop.set()
        with self.lock:
            self.pending.clear()
            if self.active is not None:
                self.active.close()
        self.thread.join()
        self.coordinator.slot.release()


def connect(destination, arguments, ssh="ssh"):
    command = [ssh, destination, *arguments]
    if os.name != "nt":
        os.execvp(ssh, command)
    # Windows needs a parent to preserve the child's exit code. Ctrl+C is also
    # delivered to the inherited-console SSH child; avoid killing it ourselves.
    process = subprocess.Popen(command)
    while True:
        try:
            return process.wait()
        except KeyboardInterrupt:
            continue
