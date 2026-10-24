"""Inventory, validated observations, cache, and independently testable ranking."""

from __future__ import annotations

import json
import math
import os
import sys
import threading
import time
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .locking import FileLock, atomic_json

FRESH_SECONDS = 300

SERVERS = ("brooks", "moore", "hanlon", "finagle")
OTHERS = (
    "amdahl-1",
    "amdahl-2",
    "alfred",
    "bane",
    "batgirl",
    "batman",
    "clayface",
    "cobblepott",
    "freeze",
    "gordon",
    "gotham",
    "harley",
    "huntress",
    "hush",
    "joker",
    "killercroc",
    "madhatter",
    "nightwing",
    "poisonivy",
    "ras",
    "riddler",
    "robin",
    "scarecrow",
    "selina",
)


@dataclass(frozen=True)
class Host:
    alias: str
    pool: str = "servers"
    preferred: bool = False


@dataclass(frozen=True)
class Metrics:
    cpus: int
    load: float
    total_kib: int
    available_kib: int
    swap_total_kib: int
    swap_free_kib: int
    seconds: float
    timestamp: float

    @classmethod
    def checked(cls, values: Mapping[str, Any]) -> Metrics:
        result = cls(**values)
        if (
            type(result.cpus) is not int
            or not 1 <= result.cpus <= sys.float_info.max
            or any(
                type(getattr(result, key)) is not int
                or getattr(result, key) > sys.float_info.max
                for key in (
                    "total_kib",
                    "available_kib",
                    "swap_total_kib",
                    "swap_free_kib",
                )
            )
            or result.total_kib <= 0
            or not 0 <= result.available_kib <= result.total_kib
            or not 0 <= result.swap_free_kib <= result.swap_total_kib
            or any(
                not isinstance(value, (int, float))
                or not 0 <= value <= sys.float_info.max
                for value in (values["load"], values["seconds"], values["timestamp"])
            )
        ):
            raise ValueError("Invalid metrics")
        return result


@dataclass(frozen=True)
class Ranking:
    cpu_weight: float = 0.6
    memory_weight: float = 0.3
    response_weight: float = 0.1
    memory_full: float = 0.2
    response_fast: float = 0.2
    response_slow: float = 2.0
    tie_fraction: float = 0.05


def config_dir() -> Path:
    if sys.platform == "win32":
        return (
            Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming")) / "labssh"
        )
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "labssh"


def cache_path() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library/Caches"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return base / "labssh/metrics.json"


def load_config(path: Path) -> tuple[list[Host], Ranking]:
    if not path.exists():
        return (
            [Host(h, preferred=h in ("brooks", "moore")) for h in SERVERS]
            + [Host(h, "other") for h in OTHERS]
        ), Ranking()
    with path.open("rb") as stream:
        data = tomllib.load(stream)
    hosts = [Host(**item) for item in data["hosts"]]
    if not hosts or len({h.alias for h in hosts}) != len(hosts):
        raise ValueError("Inventory must contain unique hosts")
    for host, item in zip(hosts, data["hosts"], strict=True):
        if (
            not isinstance(item["alias"], str)
            or not host.alias
            or host.alias.startswith("-")
            or any(c.isspace() or not c.isprintable() for c in host.alias)
            or host.pool not in ("servers", "other")
            or type(host.preferred) is not bool
        ):
            raise ValueError("Invalid host inventory entry")
    if not any(h.pool == "servers" for h in hosts):
        raise ValueError("Inventory needs at least one server")
    ranking = Ranking(**data.get("ranking", {}))
    if (
        any(
            not isinstance(v, (float, int)) or not 0 <= v <= sys.float_info.max
            for v in asdict(ranking).values()
        )
        or ranking.memory_full <= 0
        or ranking.memory_full > 1
        or ranking.response_slow <= ranking.response_fast
        or ranking.tie_fraction > 1
        or not math.isclose(
            ranking.cpu_weight + ranking.memory_weight + ranking.response_weight, 1
        )
    ):
        raise ValueError("Invalid ranking settings")
    return hosts, ranking


def score(m: Metrics, mode: str, settings: Ranking) -> float:
    memory = min(1.0, m.available_kib / m.total_kib / settings.memory_full)
    if mode == "compute":
        return max(m.cpus - m.load, 0) * memory
    cpu = max(0.0, 1 - m.load / m.cpus)
    response = max(
        0.0,
        min(
            1.0,
            (settings.response_slow - m.seconds)
            / (settings.response_slow - settings.response_fast),
        ),
    )
    return 100 * (
        settings.cpu_weight * cpu
        + settings.memory_weight * memory
        + settings.response_weight * response
    )


def ranked(
    hosts: Sequence[Host],
    metrics: Mapping[str, Metrics],
    mode: str = "responsive",
    settings: Ranking | None = None,
) -> list[tuple[Host, float]]:
    settings = settings if settings is not None else Ranking()
    scored = [
        (host, score(metrics[host.alias], mode, settings), i)
        for i, host in enumerate(hosts)
        if host.alias in metrics
    ]
    if not scored:
        return []
    best = max(s for _, s, _ in scored)
    cutoff = best * (1 - settings.tie_fraction)
    near = [row for row in scored if row[1] >= cutoff]
    rest = [row for row in scored if row[1] < cutoff]
    near.sort(key=lambda row: (not row[0].preferred, -row[1], row[2]))
    rest.sort(key=lambda row: (-row[1], row[2]))
    return [(host, value) for host, value, _ in near + rest]


def read_cache(path: Path, now: float | None = None) -> dict[str, Metrics]:
    now = time.time() if now is None else now
    found: dict[str, Metrics] = {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        for alias, values in raw.items():
            try:
                m = Metrics.checked(values)
                if 0 <= now - m.timestamp <= FRESH_SECONDS:
                    found[alias] = m
            except (ValueError, TypeError):
                continue
    except (OSError, ValueError, AttributeError):
        pass
    return found


def write_cache(
    path: Path, metrics: Mapping[str, Metrics], stop: threading.Event | None = None
) -> bool:
    lock = FileLock(path.with_suffix(".lock"))
    try:
        if not lock.acquire(stop):
            return False
        merged = read_cache(path)
        for alias, value in metrics.items():
            if alias not in merged or value.timestamp >= merged[alias].timestamp:
                merged[alias] = value
        atomic_json(path, {key: asdict(value) for key, value in merged.items()})
        return True
    except OSError:
        return False
    finally:
        lock.release()


def invalidate_cache(
    path: Path, alias: str, attempted_at: float, stop: threading.Event | None = None
) -> None:
    lock = FileLock(path.with_suffix(".lock"))
    try:
        if not lock.acquire(stop):
            return
        values = read_cache(path)
        if alias in values and values[alias].timestamp <= attempted_at:
            del values[alias]
            atomic_json(path, {key: asdict(value) for key, value in values.items()})
    except OSError:
        pass
    finally:
        lock.release()
