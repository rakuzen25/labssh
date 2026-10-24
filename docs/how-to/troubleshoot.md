# Troubleshoot labssh

Use these steps when installation, host selection, or probing fails. Start with
the [README setup instructions](../../README.md#install) if labssh is not installed.
The examples use the built-in `brooks` alias; substitute a host from your inventory.

## labssh command not found

If installation succeeded but your shell cannot find `labssh`, follow
[uv’s tool PATH guidance](https://docs.astral.sh/uv/guides/tools/#installing-tools)
to add its executable directory to `PATH`, then open a new terminal.
Installing with `uv tool install .` creates a separate tool environment; activating
the checkout’s virtual environment is not required to run the installed command.
After changing the source, reinstall from the checkout root with
`uv tool install --reinstall .`.

## OpenSSH executable not found on PATH

For `labssh: OpenSSH executable not found on PATH`, install an OpenSSH client
and make `ssh` available to the terminal that launches labssh. Confirm locally:

```sh
ssh -V
```

This prints the installed SSH version, often on stderr. labssh searches for `ssh`
on `PATH`; a custom inventory does not select a different SSH executable.

## Manual SSH works but probes fail

Probes disable interactive authentication and require a trusted host key. Diagnose
the same alias that appears in the menu:

1. Run native SSH to see the authentication or host-key prompt:

   ```sh
   ssh brooks
   ```

   For a new or changed host key, verify its fingerprint through your lab’s trusted
   channel before accepting or changing trust. Exit the remote shell to continue.
2. Check that the alias can authenticate without prompting:

   ```sh
   ssh -oBatchMode=yes brooks true
   ```

   Success produces no output and exits with code 0. If it fails, configure an
   available key or load your key into your local authentication agent. An
   interactive password or passphrase login alone is insufficient for probing.
3. Put required identity, port, and proxy settings in your SSH configuration.
   Flags after labssh’s `--`, including `-F`, `-J`, `-p`, and `-i`, affect only the
   final connection. They do not configure probes.
4. After the cooldown ends, press `r` to retry, or select a host number for manual
   SSH. Manual connections can prompt normally.

If the cause remains unclear, `ssh -v brooks` provides native SSH diagnostics.

## Probe errors in the detail line

Type a host number to inspect its error beneath the menu, or run `labssh --list`
to print scan results. A failed probe does not prove that the host is down.

| Message | Check or recovery |
| --- | --- |
| `Host key not trusted/changed; try manual SSH` | Use the trusted fingerprint check and manual connection above. |
| `Authentication failed; try manual SSH` | Check the username, identity, and noninteractive authentication for this alias. |
| `Hostname could not be resolved` | Check the alias and hostname in SSH config, plus any required lab network or VPN connection. |
| `Connection refused` | Verify the configured port and whether the SSH service is available. |
| `Timed out` or `Timed out (3 s)` | Check the route to the lab and any proxy; wait for the shared pause before retrying. |
| `Malformed or unsupported Linux metrics` | The remote command must provide logical CPU count, `/proc/loadavg`, and memory/swap values from `/proc/meminfo`; non-Linux hosts cannot supply these metrics. |

## No recommendation or an idle desktop host

Startup scans only missing or expired measurements in the `servers` pool. With
`--pool all`, desktop rows can remain idle until you press `r`. Switching pools
with `p` changes the display without starting a scan.

A queued row can be waiting for pacing or another local labssh instance. Each
alias has a 60-second attempt cooldown. Two consecutive timeouts pause probing
for five minutes across instances that share the cache directory. Cancelled
probes retain their attempt cooldown.

After a cooldown or pause expires, request another scan with `r` or `--list`.
There is no periodic scan. If no measurement is usable, type a host number and
press Enter, or run `labssh brooks` to connect directly.

## Invalid config or unexpected default hosts

Check the config path first: a missing file silently selects the built-in hosts,
even with `--config`. An existing malformed TOML file is rejected.
For `Invalid ranking settings`, verify weights total 1 and check threshold ranges
in the [configuration reference](../reference/configuration.md#ranking-settings).
For inventory errors, use unique aliases and at least one `servers` entry.

A copied example config replaces the defaults and contains only four servers.
Add `pool = "other"` entries to include your desktop hosts; use
[the host field reference](../reference/configuration.md#host-inventory) for the schema.

## Probing disabled

`Probe coordination state is invalid; probing disabled` means labssh cannot
validate `probes.json`. Other `Probing disabled:` errors can indicate inaccessible
cache files or a failed coordination write. Check the cache directory’s permissions
and available disk space using the [platform paths](../reference/configuration.md#cache-and-coordination-files).
Do not delete pacing state to bypass cooldowns. If state is corrupt, preserve it
for diagnosis and report the file path and exact error to your maintainer.
Use manual SSH while the pacing state is repaired. `labssh brooks` bypasses
config, cache, and probe coordination.

## Redirected input/output

The menu needs an interactive input and output terminal. For scripts or redirected
output, use `labssh --list` or an explicit destination such as
`labssh brooks -- hostname`. `NO_COLOR` disables styling, but the menu still uses
cursor controls; it does not become a plain-text stream.

## Exit codes

Use these codes to distinguish scan results from startup errors.

| Code | Meaning |
| --- | --- |
| `0` | `--list` has a usable recommendation, help was requested, or the menu was quit normally. |
| `1` | `--list` finished or stopped without a usable recommendation. |
| `2` | Argument, config, local I/O, or interactive-terminal error. |
| `127` | OpenSSH was not found on `PATH`. |
| `130` | A keyboard interrupt reached the top-level handler; Ctrl+C handled inside the menu quits normally with 0. |

After connecting, labssh uses the native SSH exit status. `--list` never opens
a final connection, so SSH arguments after `--` have no effect on its probes.

## Next steps

- [Review configuration paths and settings](../reference/configuration.md).
- [Understand ranking and probe limits](../explanation/selection.md).
- [Return to common commands](../../README.md#use) after resolving the failure.
