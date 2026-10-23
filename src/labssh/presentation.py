"""Compact Rich rendering; loaded only by the interactive console."""

from __future__ import annotations

import os
import time
from io import StringIO

from rich.console import Console, Group
from rich.table import Table
from rich.text import Text

from .core import score


def clean(value):
    """Keep control characters and Rich markup out of terminal-supplied labels."""
    return "".join(c if c.isprintable() else " " for c in str(value))


def focused_host(state, typed):
    if typed and 1 <= int(typed) <= len(state.hosts):
        return state.hosts[int(typed) - 1].alias
    return state.recommendation()


def menu(state, typed="", message="", *, width=80, height=24):
    hosts = state.hosts
    compute = state.mode == "compute"
    best = state.recommendation()
    focused = focused_host(state, typed)
    header = Text("Lab SSH", style="bold", no_wrap=True, overflow="ellipsis")
    mode = "resp" if width < 40 and state.mode == "responsive" else state.mode
    pool = "srv" if width < 40 and state.pool == "servers" else state.pool
    header.append(f"  {mode} / {pool}", style="dim")

    table = Table(
        box=None,
        padding=(0, 1),
        pad_edge=False,
        expand=True,
        header_style="dim",
        show_edge=False,
    )
    table.add_column("#", width=len(str(len(hosts))) + 1, no_wrap=True)
    table.add_column("Host", ratio=1, min_width=4, no_wrap=True, overflow="ellipsis")
    if width >= 55:
        table.add_column(
            "Spare cores" if compute else "Load/core",
            justify="right",
            width=11 if compute else 9,
            no_wrap=True,
        )
    if width >= 28:
        table.add_column("Free GiB", justify="right", width=8, no_wrap=True)
    if width >= 40:
        table.add_column(
            "Capacity" if compute else "Score",
            justify="right",
            width=8 if compute else 5,
            no_wrap=True,
        )
    if width >= 70:
        table.add_column("State", width=10, no_wrap=True)

    available = max(1, height - 6)
    index = next((i for i, host in enumerate(hosts) if host.alias == focused), 0)
    start = max(0, min(index - available + 1, len(hosts) - available))
    end = min(len(hosts), start + available)
    for number, host in enumerate(hosts[start:end], start + 1):
        alias = host.alias
        metrics = state.metrics.get(alias)
        status = state.status_for(alias)
        preferred_style = "green" if alias == best else ""
        host_style = preferred_style + (
            " bold underline" if typed and alias == focused else ""
        )
        cells = [
            Text(f"{'›' if alias == best else ' '}{number}", style=preferred_style),
            Text(clean(alias), style=host_style.strip()),
        ]
        if width >= 55:
            value = (
                (
                    f"{max(metrics.cpus - metrics.load, 0):.1f}"
                    if compute
                    else f"{metrics.load / metrics.cpus:.2f}"
                )
                if metrics
                else "—"
            )
            cells.append(Text(value))
        if width >= 28:
            cells.append(
                Text(f"{metrics.available_kib / 1048576:.1f}" if metrics else "—")
            )
        if width >= 40:
            cells.append(
                Text(
                    f"{score(metrics, state.mode, state.settings):.1f}"
                    if metrics
                    else "—"
                )
            )
        if width >= 70:
            label = status.label
            cells.append(
                Text(
                    label,
                    style="red"
                    if label == "error"
                    else (
                        "yellow"
                        if label in ("queued", "checking", "cooldown")
                        else "dim"
                    ),
                )
            )
        table.add_row(*cells)

    detail = Text(no_wrap=True, overflow="ellipsis")
    if message:
        detail.append(clean(message), style="yellow")
    elif state.notice:
        detail.append(clean(state.notice), style="red")
    elif focused:
        detail.append(clean(focused), style="bold")
        error = state.errors.get(focused)
        metrics = state.metrics.get(focused)
        if error:
            detail.append("  " + clean(error), style="red")
        elif metrics:
            age = max(0, int(time.time() - metrics.timestamp))
            cached = " · cached" if focused in state.cached else ""
            if compute:
                memory_percent = metrics.available_kib / metrics.total_kib * 100
                detail.append(
                    f"  {metrics.cpus} CPUs · load {metrics.load:.2f} · "
                    f"RAM {memory_percent:.1f}% free · {age}s ago{cached}",
                    style="dim",
                )
            else:
                detail.append(
                    f"  SSH {metrics.seconds * 1000:.0f} ms · {age}s ago{cached}",
                    style="dim",
                )
            swap = metrics.swap_total_kib - metrics.swap_free_kib
            if swap:
                detail.append(f" · swap {swap / 1048576:.1f} GiB", style="dim")
        else:
            detail.append("  checking", style="yellow")
    else:
        detail.append(
            "Choose a number or press r",
            style="yellow",
        )

    pause = max(0, int(state.pause_until - time.time()))
    if pause:
        detail.append(f" · probes paused {pause}s", style="yellow")

    footer = Text(no_wrap=True, overflow="ellipsis")
    if typed:
        footer.append(f"[{typed}] ", style="bold")
    controls = (
        ("Enter", " connect"),
        ("#", " choose"),
        ("m", " mode"),
        ("p", " pool"),
        ("r", " refresh"),
        ("q", " quit"),
    )
    for i, (key, label) in enumerate(controls):
        if i:
            footer.append(" " if width < 40 else "  ", style="dim")
        footer.append(key, style="bold")
        if width >= 65:
            footer.append(label, style="dim")
    rows = [header, Text(""), table]
    if len(hosts) > available:
        rows.append(
            Text(
                f"{start + 1}–{end} of {len(hosts)} · type a host number",
                style="dim",
                no_wrap=True,
                overflow="ellipsis",
            )
        )
    rows.extend([detail, footer])
    if height < 7:
        return Group(header, Text("Enlarge terminal", style="yellow"), footer)
    return Group(*rows)


class MenuRenderer:
    def render(self, state, typed="", message="", *, width=80, height=24):
        output = StringIO()
        # NO_COLOR disables styles as well as colors; cursor control remains in
        # the outer console, where it is needed to maintain the live screen.
        color = "NO_COLOR" not in os.environ
        console = Console(
            file=output,
            width=width,
            height=height,
            force_terminal=color,
            color_system="standard" if color else None,
            highlight=False,
            markup=False,
            legacy_windows=False,
        )
        console.print(
            menu(state, typed, message, width=width, height=height),
            end="",
            crop=True,
            soft_wrap=False,
        )
        return output.getvalue()
