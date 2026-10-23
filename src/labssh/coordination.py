"""Persistent pacing shared by every local Lab SSH instance."""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path

from .core import Metrics
from .locking import FileLock, atomic_json


class Outcome(StrEnum):
    SUCCESS = "success"
    TIMEOUT = "timeout"
    AUTH = "authentication"
    HOST_KEY = "host key"
    ERROR = "error"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class ProbeResult:
    alias: str
    outcome: Outcome
    metrics: Metrics | None = None
    message: str = ""
    retry_at: float = 0


@dataclass(frozen=True)
class ProbeStatus:
    label: str
    message: str = ""
    retry_at: float = 0


@dataclass
class GateState:
    last_start: float = 0
    interval: float = 0
    attempts: dict[str, float] = field(default_factory=dict)
    timeouts: int = 0
    pause_until: float = 0


@dataclass(frozen=True)
class Decision:
    allowed: bool = False
    wait_until: float = 0
    retry_at: float = 0
    paused: bool = False


class Coordinator:
    """Call reserve/finish while holding slot; never reset unreadable state."""

    def __init__(self, directory: Path, clock: Callable[[], float] = time.time) -> None:
        self.path = directory / "probes.json"
        self.slot = FileLock(directory / "probe.lock")
        self.clock = clock

    def read(self) -> GateState:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return GateState()
        except (ValueError, UnicodeError) as exc:
            raise OSError(
                "Probe coordination state is invalid; probing disabled"
            ) from exc
        try:
            state = GateState(**raw)
            values = [state.last_start, state.interval, state.pause_until]
            if not isinstance(state.attempts, dict):
                raise TypeError("Invalid attempts")
            values.extend(state.attempts.values())
            if (
                any(
                    not isinstance(value, (int, float))
                    or not 0 <= value <= sys.float_info.max
                    for value in values
                )
                or type(state.timeouts) is not int
                or state.timeouts < 0
                or any(not isinstance(alias, str) for alias in state.attempts)
            ):
                raise ValueError("Invalid pacing data")
            return state
        except (ValueError, TypeError, AttributeError) as exc:
            raise OSError(
                "Probe coordination state is invalid; probing disabled"
            ) from exc

    def reserve(self, alias: str, interval: float) -> Decision:
        state = self.read()
        now = self.clock()
        if state.pause_until > now:
            return Decision(retry_at=state.pause_until, paused=True)
        previous = state.attempts.get(alias)
        if previous is not None and previous + 60 > now:
            return Decision(retry_at=previous + 60)
        earliest = state.last_start + max(state.interval, interval)
        if state.last_start and earliest > now:
            return Decision(wait_until=earliest)
        if state.pause_until:
            state.timeouts = 0
            state.pause_until = 0
        state.last_start = now
        state.interval = interval
        state.attempts = {
            key: value for key, value in state.attempts.items() if value + 300 > now
        }
        state.attempts[alias] = now
        # A failed write aborts the attempt before any SSH process is created.
        atomic_json(self.path, asdict(state))
        return Decision(allowed=True)

    def finish(self, outcome: Outcome) -> float:
        state = self.read()
        if outcome == Outcome.TIMEOUT:
            state.timeouts += 1
            if state.timeouts >= 2:
                state.pause_until = self.clock() + 300
        elif outcome != Outcome.CANCELLED:
            state.timeouts = 0
        atomic_json(self.path, asdict(state))
        return state.pause_until
