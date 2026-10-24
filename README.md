# labssh

Pick a responsive Linux lab host, then connect with your existing OpenSSH client.
Requires Python 3.13+ and OpenSSH; installation includes Rich for the menu.
Windows, macOS, and Linux clients are supported; remote metrics require Linux
`/proc`, `getconf`, and `awk`.

## Install

Before installing, [install uv](https://docs.astral.sh/uv/getting-started/installation/)
and ensure OpenSSH is available on `PATH`. Clone or download this repository, then
open a terminal in its root directory (the one containing `pyproject.toml`).
Run:

```sh
uv tool install .
labssh --help
```

The help output lists `--mode {responsive,compute}`, `--pool {servers,all}`,
`--list`, `--config CONFIG`, and the optional `destination`.

Use `uv tool install --reinstall .` after source changes. The installed `labssh`
command runs directly, so uv does not resolve dependencies on each launch.
If the command is missing, follow [installation troubleshooting](docs/how-to/troubleshoot.md#labssh-command-not-found).

Before opening the menu, make sure native SSH can connect using your aliases,
username, identity, and routing. For example, run `ssh brooks`, then `exit` to
return locally. Replace `brooks` with one of your configured hosts. Probes cannot
answer password, passphrase, or host-key prompts; see
[authentication troubleshooting](docs/how-to/troubleshoot.md#manual-ssh-works-but-probes-fail).
labssh ships no credentials. The default aliases are `brooks`, `moore`, `hanlon`,
and `finagle`; the all-host pool adds 22 desktop aliases and `amdahl-1`/`amdahl-2`.

## Use

Run `labssh` to open the menu. These examples show common choices:

```sh
labssh                         # Open the default server menu
labssh -- -XY                   # Forward X11 for the final connection
labssh --mode compute -- -XY    # Rank by estimated parallel-build capacity
labssh --pool all -- -XY        # Display every host; press r to scan this pool
labssh moore -- -XY             # Connect directly without probing
labssh brooks -- hostname       # Run hostname without opening the menu
labssh --list                   # Scan servers, print results, and exit
```

| Option | Default | Effect |
| --- | --- | --- |
| `--mode responsive` or `--mode compute` | `responsive` | Select the ranking method. |
| `--pool servers` or `--pool all` | `servers` | Choose which hosts to display or list. |
| `--list` | Off | Scan the chosen pool and print results; cannot be combined with a destination. |
| `--config PATH` | Platform config path | Load a custom inventory and ranking settings. |
| `-h`, `--help` | — | Print usage and exit. |
| `destination` | None | Connect directly using an SSH destination, even one outside the inventory. |

Before `--`, options belong to labssh. After it, argument tokens are passed to SSH
unchanged, with the destination inserted first. Explicit destinations skip all
scanning, inventory, and caching. Native OpenSSH accepts its options after the
destination; remote commands follow those options. Shell quoting rules still apply.
For compound remote commands, quote the complete command for the remote shell;
OpenSSH joins remote-command arguments with spaces. See the
[OpenSSH command reference](https://man.openbsd.org/ssh.1).

The menu appears immediately. Cached metrics are labeled with their age and expire
after five minutes. Startup fills missing or expired measurements only for hosts
in the `servers` pool, even with `--pool all`. Results and the recommendation
update live; host numbers stay fixed. With no keyboard input, the menu connects
to the current recommendation after three seconds. Any key permanently cancels
auto-connect for that menu session. If no recommendation is available yet, it
waits for one; you can still press any key to cancel. All probe processes and
workers are stopped before the SSH connection starts.

| Key | Action |
| --- | --- |
| Enter | Connect to the current recommendation. |
| Number, then Enter | Connect to that host, including one with a failed probe. |
| `m` | Switch ranking mode. |
| `p` | Switch the displayed pool without starting a scan. |
| `r` | Request a scan of the displayed pool. |
| `q`, Escape, Ctrl+C | Quit. |

Mode and pool choices reset on the next launch.

The green `›` marker identifies the recommendation; `*` marks a typed selection.
Typing a number shows that host's details beneath the table: SSH timing in
responsive mode, CPU/load/RAM in compute mode, and measurement age or errors in
either mode. A separate status line keeps countdowns and notices visible alongside
host details. Columns adapt to terminal
width; in short windows, typing a host number brings its row into view. Set
`NO_COLOR` to disable styling. Rich loads only when opening the interactive menu.

Failed hosts can be selected manually. Probes never prompt for authentication or
accept new host keys. Manual/native SSH can prompt normally. A failed probe does
not prove a host is down. Redirected input/output requires `--list` or a destination.
`--list` explicitly requests a scan of the chosen pool and prints results when it
finishes or stops. Like `r`, it bypasses cache freshness and the per-host attempt
cooldown, while keeping shared pacing and timeout pauses; exit code 1 means no
usable metrics are available. Cached metrics can remain usable during a pause.
After a pause ends, press `r` or run `--list` again to request a scan;
expired measurements do not trigger periodic probes.

Use `--mode compute` to rank estimated parallel-build capacity; the default
`responsive` mode balances CPU, memory, and SSH command time. See
[how host selection works](docs/explanation/selection.md) for scoring formulas,
probe traffic limits, and SSH configuration behavior. SSH arguments after `--`
apply only to the final connection; configure probe authentication and routing
in your SSH configuration.

## Configuration

A config file is optional when the built-in lab aliases match your SSH setup.
For other hosts, edit [config.example.toml](config.example.toml) and try it from
the checkout root:

```sh
labssh --config config.example.toml --list
```

This explicitly scans the four example servers and prints their measurements or
errors. The example replaces the inventory, so `--pool all` still shows only
those four servers until you add hosts. A missing config path silently uses the
built-in inventory, even when you supply `--config`.

See the [configuration reference](docs/reference/configuration.md) for default
paths, host fields, ranking parameters, and cache files. If a host cannot be
measured, follow [troubleshooting](docs/how-to/troubleshoot.md).

## Windows Terminal

To launch labssh directly from Windows Terminal, configure a profile:

1. In Windows Terminal **Settings**, select **Open JSON file** and save a backup
   of `settings.json` before editing it.
2. In PowerShell, run `Get-Command labssh` and note the executable path in `Source`.
3. In the JSON file, locate `profiles.list`. Add the profile object below as a new
   array entry, separating it from adjacent entries with a comma. To reuse an
   existing profile instead, update these fields and preserve its GUID and appearance.
4. Replace `YOUR_NAME` and the example path with the executable path from step 2.
   Escape each backslash as `\\` in JSON.

```json
{
  "name": "Lab SSH",
  "commandline": "\"C:\\Users\\YOUR_NAME\\.local\\bin\\labssh.exe\" -- -XY",
  "closeOnExit": "graceful"
}
```

Save `settings.json`, then launch **Lab SSH** from the dropdown or run
`wt -p "Lab SSH"`. Normal SSH exit closes the tab; errors keep it visible.
There is no local shell after logout. See the
[Windows Terminal profile reference](https://learn.microsoft.com/en-us/windows/terminal/customize-settings/profile-general)
for profile settings.

## Development

From the repository root, run the local checks and build the package:

```sh
uv run ruff check .
uv run ruff format --check .
uv run mypy src/labssh tests tools
uv run basedpyright
uv run python -m unittest discover -s tests -v
uv build
```

Mypy and BasedPyright both use strict checking. BasedPyright reads its source paths
and Python 3.13 settings from `pyproject.toml`; its development dependency includes
the Node runtime, so no separate npm setup is needed.

Tests cover ranking, cache handling, SSH argument preservation, menu transitions,
probe failures, cross-process exclusion, fake-clock pacing, shared cooldowns, and
cancellation of local child processes. The test suite makes no campus connections.

To repeat timing checks intentionally, run the following from an interactive
terminal in the checkout root:

```sh
uv run python tools/benchmark.py --output benchmark.json
```

The harness uses noninteractive SSH authentication and automates Enter to perform
real SSH sessions. It runs one cold selection plus 20 paired trials by default,
with five-second pauses between trials. It stops after two consecutive failed
pairs. Results are gitignored. Compare against
direct SSH to the same destination with identical flags, and report cold launches
separately. The harness uses this checkout through Python, excluding the installed
entry-point launcher; both final SSH paths use pipes and a forced remote TTY to
time a shell marker and logout. On Windows and Linux, it uses a temporary cache;
on macOS, it uses the normal `~/Library/Caches/labssh` location. Verify native
terminal handoff separately.

## Next steps

- [Configure your own hosts](docs/reference/configuration.md) and ranking settings.
- [Troubleshoot connection and probe failures](docs/how-to/troubleshoot.md).
- [Understand host selection](docs/explanation/selection.md) and probe traffic limits.
