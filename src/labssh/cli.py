from __future__ import annotations

import argparse
import queue
import shutil
import sys
import time
from pathlib import Path

from .console import Console
from .coordination import Coordinator, Outcome, ProbeStatus
from .core import (
    FRESH_SECONDS,
    Host,
    Metrics,
    Ranking,
    cache_path,
    config_dir,
    load_config,
    ranked,
    read_cache,
    score,
)
from .transport import Scanner, connect


def arguments(argv):
    boundary = argv.index("--") if "--" in argv else len(argv)
    parser = argparse.ArgumentParser(
        description="Live lab host selection. Put SSH arguments after --.",
        epilog="Examples: labssh -- -XY; labssh brooks -- hostname",
    )
    parser.add_argument("destination", nargs="?")
    parser.add_argument(
        "--mode", choices=("responsive", "compute"), default="responsive"
    )
    parser.add_argument("--pool", choices=("servers", "all"), default="servers")
    parser.add_argument(
        "--list", action="store_true", help="scan, print results, and exit"
    )
    parser.add_argument("--config", type=Path, default=config_dir() / "config.toml")
    result = parser.parse_args(argv[:boundary])
    if result.list and result.destination:
        parser.error("--list cannot be combined with a destination")
    if result.destination and result.destination.startswith("-"):
        parser.error("invalid destination")
    return result, argv[boundary + 1 :]


class Selection:
    def __init__(
        self, hosts: list[Host], settings: Ranking, mode: str, pool: str, ssh: str
    ) -> None:
        self.inventory, self.settings, self.mode, self.pool = (
            hosts,
            settings,
            mode,
            pool,
        )
        self.ssh = ssh
        self.path = cache_path()
        self.metrics: dict[str, Metrics] = read_cache(self.path)
        self.cached = set(self.metrics)
        self.errors: dict[str, str] = {}
        self.scanner: Scanner | None = None
        self.completed: set[str] = set()
        self.statuses: dict[str, ProbeStatus] = {}
        self.pause_until = 0.0
        self.notice = ""
        self._last_shared_check = 0.0

    @property
    def hosts(self):
        return [h for h in self.inventory if self.pool == "all" or h.pool == "servers"]

    def refresh(self, *, startup=False):
        hosts = (
            [
                h
                for h in self.inventory
                if h.pool == "servers" and h.alias not in self.metrics
            ]
            if startup
            else self.hosts
        )
        size = (
            sum(h.pool == "servers" for h in self.inventory)
            if startup
            else len(self.hosts)
        )
        if self.scanner is not None and self.scanner.request(
            hosts, force=not startup, pool_size=size
        ):
            return
        self.close()
        for host in hosts:
            self.errors.pop(host.alias, None)
            self.completed.discard(host.alias)
            self.statuses[host.alias] = ProbeStatus("queued")
        self.scanner = Scanner(
            hosts, self.ssh, path=self.path, force=not startup, pool_size=size
        )

    @property
    def scan_done(self):
        return self.scanner is None or self.scanner.done.is_set()

    def status_for(self, alias: str) -> ProbeStatus:
        if alias in self.errors:
            return ProbeStatus("error", self.errors[alias])
        if alias in self.statuses:
            status = self.statuses[alias]
            if status.label in ("ready", "cached") and alias not in self.metrics:
                return ProbeStatus("idle")
            if status.label == "cooldown" and status.retry_at <= time.time():
                return ProbeStatus("cached" if alias in self.metrics else "idle")
            return status
        if alias in self.metrics:
            return ProbeStatus("cached" if alias in self.cached else "ready")
        return ProbeStatus("idle")

    def collect(self):
        changed = False
        if self.scanner is not None:
            snapshot = self.scanner.snapshot()
            if any(
                self.statuses.get(alias) != value for alias, value in snapshot.items()
            ):
                self.statuses.update(snapshot)
                changed = True
            self.pause_until = max(self.pause_until, self.scanner.pause_until)
        while self.scanner is not None:
            try:
                result = self.scanner.results.get_nowait()
            except queue.Empty:
                break
            alias, metrics = result.alias, result.metrics
            changed = True
            self.completed.add(alias)
            if metrics:
                self.metrics[alias] = metrics
                self.errors.pop(alias, None)
                if self.statuses.get(alias, ProbeStatus("ready")).label == "cached":
                    self.cached.add(alias)
                else:
                    self.cached.discard(alias)
            elif result.outcome not in (Outcome.CANCELLED, Outcome.SKIPPED):
                self.metrics.pop(alias, None)
                self.cached.discard(alias)
                self.errors[alias] = result.message
        now = time.time()
        if now - self._last_shared_check >= 1:
            self._last_shared_check = now
            try:
                shared = Coordinator(self.path.parent).read()
                if self.notice:
                    self.notice = ""
                    changed = True
                if self.pause_until != shared.pause_until:
                    self.pause_until = shared.pause_until
                    changed = True
            except OSError as exc:
                notice = str(exc)
                changed = changed or notice != self.notice
                self.notice = notice
        for alias, metrics in list(self.metrics.items()):
            if not 0 <= now - metrics.timestamp <= FRESH_SECONDS:
                del self.metrics[alias]
                self.cached.discard(alias)
                changed = True
        return changed

    def recommendation(self):
        eligible = {
            alias: m
            for alias, m in self.metrics.items()
            if alias not in self.errors
            and 0 <= time.time() - m.timestamp <= FRESH_SECONDS
        }
        rows = ranked(self.hosts, eligible, self.mode, self.settings)
        return rows[0][0].alias if rows else None

    def lines(self, typed="", message=""):
        best = self.recommendation()
        compute = self.mode == "compute"
        cpu_label = "Spare CPU" if compute else "Load/CPU"
        score_label = "Capacity" if compute else "Score"
        lines = [
            f"Lab SSH  |  {self.mode}  |  {self.pool}  |  live recommendation: {best or 'pending / none'}",
            f"  # Host          {cpu_label}    RAM free  {score_label:>8}  Status",
        ]
        for i, host in enumerate(self.hosts, 1):
            alias = host.alias
            m = self.metrics.get(alias)
            marker = ">" if alias == best else " "
            if m:
                age = max(0, int(time.time() - m.timestamp))
                status = f"{'cached ' if alias in self.cached else ''}{age}s; {m.seconds * 1000:.0f}ms"
                status += "; " + self.status_for(alias).label
                if m.swap_total_kib > m.swap_free_kib:
                    status += f"; swap {(m.swap_total_kib - m.swap_free_kib) / 1048576:.1f}GiB"
                value = score(m, self.mode, self.settings)
                cpu_value = (
                    f"{max(m.cpus - m.load, 0):7.1f}"
                    if compute
                    else f"{m.load / m.cpus:7.2f}"
                )
                lines.append(
                    f"{marker}{i:2} {alias:13} {cpu_value} {m.available_kib / 1048576:8.1f}GiB {value:8.1f}  {status}"
                )
            else:
                lines.append(
                    f"{marker}{i:2} {alias:13} {'—':>7} {'—':>11} {'—':>8}  {self.errors.get(alias, self.status_for(alias).label)}"
                )
        lines += [
            "Enter: best | number+Enter: host | m: mode | p: pool | r: rescan | q: quit",
            f"Choice: {typed}  {message}",
        ]
        return lines

    def save(self):
        # The worker persists each observation before releasing the shared slot.
        # Do not write an old UI snapshot over another window's invalidations.
        pass

    def close(self):
        if self.scanner:
            self.scanner.close()
            self.scanner = None


def run_menu(state):
    typed, message = "", ""
    # Paint pending/cached rows before launching any probe process.
    with Console() as console:
        try:
            console.draw(state)
            state.refresh(startup=True)
            dirty = True
            size = shutil.get_terminal_size()
            next_tick = time.monotonic() + 1
            while True:
                dirty = state.collect() or dirty
                now = time.monotonic()
                if now >= next_tick:
                    dirty = True
                    next_tick = now + 1
                current_size = shutil.get_terminal_size()
                if current_size != size:
                    size = current_size
                    dirty = True
                if dirty:
                    console.draw(state, typed, message)
                    dirty = False
                key = console.key()
                if key is not None:
                    dirty = True
                    if key in ("q", "Q", "\x03", "\x1b", "\x04", ""):
                        return None
                    if key in ("\r", "\n"):
                        if typed:
                            number = int(typed)
                            if 1 <= number <= len(state.hosts):
                                return state.hosts[number - 1].alias
                            message, typed = "Invalid host number", ""
                        elif state.recommendation():
                            return state.recommendation()
                        else:
                            message = "No recommendation yet; select a host number for manual SSH."
                    elif key.isascii() and key.isdigit():
                        typed = (typed + key)[:5]
                        message = ""
                    elif key in ("\b", "\x7f"):
                        typed = typed[:-1]
                    elif key.lower() == "m":
                        state.mode = (
                            "compute" if state.mode == "responsive" else "responsive"
                        )
                        typed, message = "", ""
                    elif key.lower() in ("p", "r"):
                        if key.lower() == "p":
                            state.pool = "all" if state.pool == "servers" else "servers"
                        else:
                            state.refresh()
                        typed, message = "", ""
                time.sleep(0.025)
        finally:
            state.close()
            state.save()


def main(argv=None):
    args, ssh_args = arguments(list(sys.argv[1:] if argv is None else argv))
    ssh = shutil.which("ssh")
    if not ssh:
        print("labssh: OpenSSH executable not found on PATH", file=sys.stderr)
        return 127
    try:
        if args.destination:
            return connect(args.destination, ssh_args, ssh)
        if not args.list and not (sys.stdin.isatty() and sys.stdout.isatty()):
            print(
                "labssh: use --list or an explicit destination with redirected input/output",
                file=sys.stderr,
            )
            return 2
        hosts, settings = load_config(args.config)
        state = Selection(hosts, settings, args.mode, args.pool, ssh)
        if args.list:
            try:
                state.refresh()
                while not state.scan_done:
                    state.collect()
                    time.sleep(0.01)
                state.collect()
                print("\n".join(state.lines()[:-2]))
                return 0 if state.recommendation() else 1
            finally:
                state.close()
                state.save()
        destination = run_menu(state)
        if destination is None:
            return 0
        print(f"Connecting to {destination}…", flush=True)
        return connect(destination, ssh_args, ssh)
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"labssh: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
