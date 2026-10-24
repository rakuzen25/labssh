# Configuration reference

labssh reads a TOML inventory and optional ranking settings. These fields match
`load_config` in `src/labssh/core.py`. For setup, see the
[README](../../README.md#configuration).

## Config file selection

`--config PATH` overrides the default path. Relative paths resolve from the
current working directory. If the file does not exist, labssh silently uses the
built-in inventory and ranking defaults. An existing invalid file causes an error.
Explicit destinations bypass config loading entirely.

| Platform | Default config file |
| --- | --- |
| Windows | `%APPDATA%\labssh\config.toml` |
| macOS and Linux | `$XDG_CONFIG_HOME/labssh/config.toml`, default `~/.config/labssh/config.toml` |

If `APPDATA` is unset, Windows uses `~/AppData/Roaming/labssh/config.toml`.

## Host inventory

A custom file replaces the entire built-in inventory. The
[example file](../../config.example.toml) contains only four servers; it omits
the 22 desktops and two Amdahl hosts in the built-in all-host pool.

```toml
[[hosts]]
alias = "brooks"
pool = "servers"
preferred = true

[[hosts]]
alias = "amdahl-1"
pool = "other"
```

This inventory shows Brooks in the default pool and both hosts with `--pool all`.
The alias is passed directly to OpenSSH; define its hostname, username, identity,
and jump-host routing in your SSH configuration.

| Field | Default | Accepted values and effect |
| --- | --- | --- |
| `alias` | Required | Unique, nonempty string; no whitespace or nonprintable characters; cannot start with `-`. |
| `pool` | `"servers"` | `"servers"` appears in both pools; `"other"` appears only with `--pool all`. |
| `preferred` | `false` | Boolean; ranks the host ahead of nonpreferred hosts within the near-tie group in both modes. |

The inventory must contain at least one host in `servers`. Duplicate aliases and
unknown host fields are rejected. `--pool all` includes both pools; `all` is not a
valid value for a host’s `pool` field.

## Ranking settings

Omit `[ranking]` to use all defaults, or set only the values you want to change.
All values must be finite and nonnegative. Unknown ranking keys are rejected.

| Key | Default | Meaning and constraint |
| --- | --- | --- |
| `cpu_weight` | `0.6` | CPU contribution to responsive score. |
| `memory_weight` | `0.3` | Available-memory contribution to responsive score. |
| `response_weight` | `0.1` | SSH command-time contribution to responsive score. |
| `memory_full` | `0.2` | Available-memory fraction earning full credit in both modes; greater than 0 and at most 1. |
| `response_fast` | `0.2` | Seconds at or below which command time earns full credit in responsive mode. |
| `response_slow` | `2.0` | Seconds at or above which command time earns zero credit; must exceed `response_fast`. |
| `tie_fraction` | `0.05` | Fraction below the best score included in the near-tie group in both modes; 0–1 inclusive. |

The three weights must total 1 within floating-point tolerance. Compute mode uses
`memory_full` and `tie_fraction`, but ignores the weights and response thresholds
when scoring. All fields are still validated in either mode.

For example, these settings increase the responsive score’s emphasis on memory
while preserving the remaining defaults:

```toml
[ranking]
cpu_weight = 0.5
memory_weight = 0.4
response_weight = 0.1
```

This block belongs after the host entries in the same file; it is not a complete
inventory. See [how selection works](../explanation/selection.md#ranking-modes)
for formulas and near-tie ordering.

## Cache and coordination files

Instances using the same cache directory share measurements, pacing, and one
active probe slot. Changing `--config` does not create a separate cache: records
are keyed by alias.

| Platform | Cache directory |
| --- | --- |
| Windows | `%LOCALAPPDATA%\labssh`, default `~/AppData/Local/labssh` |
| macOS | `~/Library/Caches/labssh` |
| Linux | `$XDG_CACHE_HOME/labssh`, default `~/.cache/labssh` |

`metrics.json` stores observations with a five-minute lifetime. Cache updates
merge under `metrics.lock`. A failed probe removes that alias’s older measurement;
a cooldown can retain a still-fresh measurement.

`probes.json` stores attempts and shared pauses; `probe.lock` coordinates the
active slot. Cache write failures are nonfatal. Unreadable or malformed pacing
state disables probing. See [troubleshooting](../how-to/troubleshoot.md#probing-disabled)
for recovery guidance.
