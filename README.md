# labssh

Pick a responsive Linux lab host, then connect with your existing OpenSSH client.
Python 3.13+, OpenSSH, and Rich for the interactive display. Windows, macOS, and
Linux clients are supported; remote metrics require Linux `/proc`, `getconf`, and `awk`.

## Install

```sh
uv tool install .
labssh --help
```

To share, put this directory in a source repository. Others can install with
`uv tool install 'git+https://YOUR-REPOSITORY-URL'`. Publishing is not required.
Use `uv tool install --reinstall .` after source changes. Terminal runs the installed
entry point directly, so uv does not resolve dependencies on each launch.

Configure your own SSH aliases, identity, username, and routing first. labssh ships
no credentials. The default aliases are `brooks`, `moore`, `hanlon`, and `finagle`;
the all-host pool adds 22 desktop aliases and `amdahl-1`/`amdahl-2`.

## Use

```sh
labssh
labssh -- -XY
labssh --mode compute -- -XY
labssh --pool all -- -XY
labssh moore -- -XY
labssh brooks -- hostname
labssh --list
```

Before `--`, options belong to labssh. After it, argument tokens are passed to SSH
unchanged, with the destination inserted first. Explicit destinations skip all
scanning, inventory, and caching. Native OpenSSH accepts its options after the
destination; remote commands follow those options. Shell quoting rules still apply.
For a remote command beginning with a dash, include SSH's own `--` where appropriate.

The menu appears immediately. Cached metrics are labeled with their age and expire
after five minutes. Startup only fills missing or expired metrics for the default
servers, even with `--pool all`. Fresh results and the Enter recommendation update live; host
numbers stay fixed. Enter uses the **currently displayed** recommendation. Type a
number and Enter to choose another host. `m` changes mode, `p` changes pool, `r`
explicitly scans the displayed pool, and `q`, Escape, or Ctrl+C quits. Changing
pools never starts a scan. Mode and pool reset on the next launch.
The green marker identifies the recommendation. Typing a number shows that host's
SSH timing, measurement age, and errors beneath the table. Columns adapt to terminal
width; in short windows, typing a host number brings its row into view. Set
`NO_COLOR` to disable styling. Rich loads only when opening the interactive menu.

Failed hosts can be selected manually. Probes never prompt for authentication or
accept new host keys. Manual/native SSH can prompt normally. A failed probe does
not prove a host is down. Redirected input/output requires `--list` or a destination.
`--list` explicitly requests a scan of the chosen pool and prints results when it
finishes or stops. It respects all traffic limits; exit code 1 means no usable
metrics are available. Cached metrics can remain usable during a refresh cooldown.

## Ranking and overhead

Responsiveness combines CPU headroom (60%), available-memory headroom (30%), and
SSH command completion time (10%). CPU headroom is `max(0, 1 - load_1m / CPUs)`.
Memory earns full credit at 20% available, and command time earns full credit at
200 ms, decreasing linearly to zero at 2 seconds. Load is an estimate of pressure,
not measured CPU utilization. Swap shown is allocated swap, not current swap I/O.

Compute mode estimates spare logical CPUs as `max(CPUs - load_1m, 0)`, reduced when
available RAM is below 20%. It estimates parallel-build capacity, not single-thread
speed. Hosts within 5% of the highest score form a near-tie group; Brooks and Moore
are preferred equally within that group, retaining their measured order. A materially
better host wins. Stable inventory order resolves exact ties.

Responsiveness mode shows load per core and Score, with SSH command time in the
detail line. Compute mode shows estimated Spare cores and memory-adjusted Capacity;
its details show logical CPU count, load, and available RAM percentage. Both show
free RAM and measurement age. The two modes' scores use different scales.

Each probe issues one small read-only command with a three-second total timeout.
All local Lab SSH instances share one active probe slot. Starts are spaced by at
least two seconds for pools of up to four hosts and five seconds for larger pools;
the larger gap applies when transitioning between pool sizes. Each alias has a
60-second attempt cooldown, including after failures and cancellation. Repeated
refresh requests coalesce with an active scan. After two consecutive timeouts,
remaining work is cancelled and probing pauses for five minutes across launches.

Fresh cache entries skip startup probes. Explicit refresh bypasses cache freshness,
but never bypasses pacing or cooldowns. Direct destinations and manual connections
remain available during a pause. Queued, checking, cached, cooldown, and error
states distinguish waiting from an unreachable host; a shared pause countdown
appears in the detail line. The measurement-age clock makes no network requests.

There are no remote files, agents, benchmarks, or periodic monitoring. Selection
or quit interrupts waits, discards queued work, terminates and reaps the active
probe's process tree, joins the worker, and releases its shared slot before SSH
handoff. It does not wait for a probe's normal timeout.
Shared-home filesystem contention may affect all hosts, and these metrics do not
predict instruction-set differences or assignment compatibility.

These traffic limits reduce bursts; they are not a confirmed campus-approved rate.

Probes use existing SSH configuration. SSH arguments after `--` apply **only to
the final connection**, including `-F`, `-J`, `-p`, or identity overrides. Probes
disable forwarding, local commands, configured remote commands, multiplexing, and
host-key updates, while retaining configured authentication and proxy routing.
ProxyCommand/ProxyJump remains whatever your trusted SSH configuration specifies.

## Configuration

Copy `config.example.toml` to `%APPDATA%\labssh\config.toml` on Windows or
`$XDG_CONFIG_HOME/labssh/config.toml` (default `~/.config/labssh/config.toml`) on
macOS/Linux. Or supply `--config PATH`. A custom inventory replaces the defaults.
Edit the host list and ranking parameters there. Cache lives under
`%LOCALAPPDATA%\labssh`, `~/Library/Caches/labssh`, or `$XDG_CACHE_HOME/labssh`
(default `~/.cache/labssh`) respectively. Measurements are merged atomically under
a file lock. `probes.json` records pacing, recent attempts, and shared pauses;
`probe.lock` coordinates the active slot. Cache failures are nonfatal. If pacing
state is inaccessible or malformed, probing is disabled instead of bypassing the
limits; manual SSH remains available. Do not delete pacing state to evade cooldowns.

## Windows Terminal

Run `Get-Command labssh` in PowerShell to find the installed executable. In Terminal
Settings → Open JSON file, locate the Moore profile, rename it to `Lab SSH`, preserve
its GUID and appearance, and set these fields (substitute the actual executable):

```json
{
  "name": "Lab SSH",
  "commandline": "\"C:\\Users\\YOUR-NAME\\.local\\bin\\labssh.exe\" -- -XY",
  "closeOnExit": "graceful"
}
```

Back up settings first. Normal SSH exit closes the tab; errors keep it visible.
Launch from the dropdown or `wt -p "Lab SSH"`. There is no local shell after logout.

## Development

```sh
uv run ruff check .
uv run ruff format --check .
uv run mypy src/labssh tests tools
uv run python -m unittest discover -s tests -v
uv build
```

Tests cover ranking, cache handling, SSH argument preservation, menu transitions,
probe failures, cross-process exclusion, fake-clock pacing, shared cooldowns, and
cancellation of local child processes. The test suite makes no campus connections.

To repeat timing checks intentionally, run `python tools/benchmark.py --output
benchmark.json` from an interactive terminal with this project's Python available.
The harness automates Enter and performs real SSH sessions. It paces trials and
stops after two consecutive failed pairs. Results are gitignored. Compare against
direct SSH to the same destination with identical flags, and report cold launches
separately. The harness uses this checkout through Python, excluding the installed
entry-point launcher; both final SSH paths use pipes and a forced remote TTY to
time a shell marker and logout. Verify native terminal handoff separately.
