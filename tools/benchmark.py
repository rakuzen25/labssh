"""Time the actual menu in an inherited terminal, with automated Enter.

Run in a terminal: python tools/benchmark.py --output benchmark.json
Uses this checkout via PYTHONPATH, not the installed executable's launcher.
The final SSH session and baseline both use pipes plus -tt, so a remote shell
marker can be timed. Native console handoff is verified separately.
"""

import argparse
import json
import math
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import NotRequired, Required, TypedDict
from unittest.mock import patch


class TrialResult(TypedDict, total=False):
    success: Required[bool]
    host: str
    handoff_seconds: float
    ready_seconds: float
    menu_seconds: float
    selection_seconds: float
    diagnostic: str


class PairResult(TypedDict):
    index: int
    labssh: TrialResult
    direct: TrialResult
    added_seconds: NotRequired[float]


FLAGS = ["-XY", "-tt", "-oBatchMode=yes", "-oStrictHostKeyChecking=yes"]
PAYLOAD = (
    b"printf '\\114\\101\\102\\123\\123\\110\\137\\122\\105\\101\\104\\131\\n'; exit\n"
)


def child(args: argparse.Namespace) -> None:
    from labssh import cli
    from labssh.console import Console

    result: TrialResult = {"success": False}

    def connection(host: str, forwarded: list[str], ssh: str = "ssh") -> int:
        result["host"] = host
        result["handoff_seconds"] = time.perf_counter() - args.start
        process = subprocess.Popen(
            [ssh, host, *forwarded],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        try:
            assert process.stdin is not None
            process.stdin.write(PAYLOAD)
            process.stdin.flush()
            # communicate closes stdin and drains output; remote shell exits itself.
            output, _ = process.communicate(timeout=12)
            result["ready_seconds"] = time.perf_counter() - args.start
            result["success"] = b"LABSSH_READY" in output and process.returncode == 0
            if not result["success"]:
                result["diagnostic"] = output.decode("utf-8", "replace")[-1200:]
            assert process.returncode is not None
            return process.returncode
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            result["diagnostic"] = "Remote shell timed out"
            return 124
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    class AutoConsole(Console):
        best: str | None = None

        def draw(
            self, state: cli.Selection, typed: str = "", message: str = ""
        ) -> None:
            super().draw(state, typed, message)
            result.setdefault("menu_seconds", time.perf_counter() - args.start)
            self.best = state.recommendation()

        def key(self) -> str | None:
            if self.best:
                result["selection_seconds"] = time.perf_counter() - args.start
                return "\r"
            if time.perf_counter() - args.start > 8:
                result["diagnostic"] = "No host recommended within 8 seconds"
                return "q"
            return None

    try:
        if args.child == "direct":
            connection(args.host, FLAGS)
        else:
            with (
                patch.object(cli, "Console", AutoConsole),
                patch.object(cli, "connect", connection),
            ):
                cli.main(["--", *FLAGS])
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        result["diagnostic"] = str(exc)
    args.output.write_text(json.dumps(result), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument(
        "--interval",
        type=float,
        default=5,
        help="minimum pause between paired trials (default: 5 seconds)",
    )
    parser.add_argument("--child", choices=("selection", "direct"))
    parser.add_argument("--host", default="brooks")
    parser.add_argument("--start", type=float, default=0)
    args = parser.parse_args()
    if args.samples < 1 or args.interval < 1:
        parser.error("samples must be positive and interval must be at least 1 second")
    if args.child:
        child(args)
        return
    if not sys.stdout.isatty():
        parser.error("Run benchmark in a terminal; menu rendering is measured")
    pairs: list[PairResult] = []
    report: dict[str, object] = {"method": __doc__, "pairs": pairs}
    with tempfile.TemporaryDirectory(prefix="labssh-timing-") as directory:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        env["LOCALAPPDATA"] = directory
        env["XDG_CACHE_HOME"] = directory
        result_path = Path(directory) / "result.json"

        def trial(kind: str, host: str = "brooks") -> TrialResult:
            start = time.perf_counter()
            command = [
                sys.executable,
                str(Path(__file__).resolve()),
                "--child",
                kind,
                "--output",
                str(result_path),
                "--host",
                host,
                "--start",
                str(start),
            ]
            process = subprocess.run(command, env=env, timeout=25, check=False)
            if process.returncode or not result_path.exists():
                return {
                    "success": False,
                    "diagnostic": f"Harness exited {process.returncode}",
                }
            value: TrialResult = json.loads(result_path.read_text(encoding="utf-8"))
            result_path.unlink()
            return value

        report["cold"] = trial("selection")
        print("Cold:", report["cold"], flush=True)
        failures = 0
        for i in range(args.samples):
            time.sleep(args.interval)
            selected = trial("selection")
            time.sleep(args.interval)
            direct = trial("direct", selected.get("host", "brooks"))
            pair: PairResult = {"index": i + 1, "labssh": selected, "direct": direct}
            if selected["success"] and direct["success"]:
                assert "ready_seconds" in selected and "ready_seconds" in direct
                pair["added_seconds"] = (
                    selected["ready_seconds"] - direct["ready_seconds"]
                )
                failures = 0
            else:
                failures += 1
            pairs.append(pair)
            args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
            print(f"Trial {i + 1}: {pair.get('added_seconds', 'FAILED')}", flush=True)
            if failures >= 2:
                report["stopped_reason"] = (
                    "Two consecutive failed pairs; stop instead of retrying the network"
                )
                break
    values = sorted(p["added_seconds"] for p in pairs if "added_seconds" in p)
    if values:
        report["summary"] = {
            "successful_pairs": len(values),
            "attempted_pairs": len(pairs),
            "median_added_seconds": statistics.median(values),
            "p95_added_seconds": values[math.ceil(len(values) * 0.95) - 1],
            "median_menu_seconds": statistics.median(
                p["labssh"]["menu_seconds"]
                for p in pairs
                if "menu_seconds" in p["labssh"]
            ),
        }
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report.get("summary", {}), indent=2))


if __name__ == "__main__":
    main()
