# How host selection works

labssh ranks Linux lab hosts using short SSH probes and cached measurements.
For installation and commands, see the [labssh README](../../README.md).

## Ranking modes

With the default ranking settings, responsiveness combines CPU headroom (60%),
available-memory headroom (30%), and SSH command completion time (10%).
CPU headroom is `max(0, 1 - load_1m / CPUs)`.
Memory earns full credit at 20% available, and command time earns full credit at
200 ms, decreasing linearly to zero at 2 seconds. Load is an estimate of pressure,
not measured CPU utilization. Swap shown is allocated swap, not current swap I/O.

Compute mode estimates spare logical CPUs as `max(CPUs - load_1m, 0)`, reduced when
available RAM is below 20%. It estimates parallel-build capacity, not single-thread
speed. The responsive score ranges from 0 to 100; compute capacity uses estimated
spare logical CPUs multiplied by the memory factor.

In both modes, hosts within 5% of the highest score form a near-tie group by
default. Hosts marked `preferred` rank first in that group, then by score and
inventory order. The built-in inventory marks Brooks and Moore equally preferred.
Outside the group, score determines order; inventory order resolves exact ties.

Responsiveness mode shows load per core and Score, with SSH command time in the
detail line. Compute mode shows estimated Spare cores and memory-adjusted Capacity;
its details show logical CPU count, load, and available RAM percentage. Both show
available RAM and measurement age. The free-RAM value comes from Linux
`MemAvailable`, which includes memory the kernel estimates can be reclaimed.
SSH command time includes connection setup, authentication, and the metrics
command; it is not a network ping. The two modes' scores use different scales.

The [configuration reference](../reference/configuration.md#ranking-settings)
documents adjustable weights, memory and response thresholds, and the near-tie
fraction. For example, with default settings, 8 CPUs, load 2, 10% available RAM,
and a 200 ms SSH command yield a responsive score of 70 and compute capacity of 3.

## Probe traffic and cancellation

Each probe issues one small read-only command with a three-second total timeout.
Lab SSH instances using the same cache directory share one active probe slot. Starts are spaced by at
least two seconds for pools of up to four hosts and five seconds for larger pools;
the larger gap applies when transitioning between pool sizes. Each alias has a
60-second attempt cooldown for automatic probes, including after failures and
cancellation. Repeated refresh requests coalesce with an active scan. After two consecutive timeouts,
remaining work is cancelled and probing pauses for five minutes across launches.

Fresh cache entries skip startup probes. Explicit refresh (`r` or `--list`) bypasses
cache freshness and the per-host attempt cooldown, while keeping shared pacing and
the five-minute timeout pause. During an active scan, refresh upgrades pending
automatic requests without restarting the worker or duplicating completed or active
probes. Direct destinations and manual connections
remain available during a pause. Queued, checking, cached, cooldown, and error
states distinguish waiting from an unreachable host; a shared pause countdown
appears in the status line as `Probes paused · retry in 4m 12s`. After a pause ends,
request another scan; skipped hosts are not retried automatically.
The measurement-age clock makes no network requests.

Probes do not install remote files or agents, run workloads, or monitor periodically. Selecting a host
or quitting interrupts waits, discards queued work, terminates and reaps the active
probe's process tree, joins the worker, and releases its shared slot before SSH
handoff. It does not wait for a probe's normal timeout.

Shared-home filesystem contention may affect all hosts, and these metrics do not
predict instruction-set differences or assignment compatibility.

These traffic limits reduce bursts; they are not a confirmed campus-approved rate.

## SSH configuration

Probes use existing SSH configuration. SSH arguments after `--` apply **only to
the final connection**, including `-F`, `-J`, `-p`, or identity overrides. Probes
disable forwarding, local commands, configured remote commands, multiplexing, and
host-key updates, while retaining configured authentication and proxy routing.
Configured `ProxyCommand` and `ProxyJump` routing remains enabled. Probe options
suppress SSH’s own local-command hooks, but configured proxy commands can still
run locally.
