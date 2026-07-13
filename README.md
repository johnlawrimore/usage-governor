# usage-governor

A [Claude Code](https://claude.com/claude-code) skill that checks your live claude.ai
subscription usage and turns it into execution decisions. It reads the rolling 5-hour session
limit, the weekly allotment, and per-model weekly limits (like Fable), caches responses so
frequent checks are effectively free, and derives three decision aids the model acts on directly:
time-to-reset, burn rate (how fast a limit is climbing), and a deterministic posture
(`normal` / `frugal` / `wind_down`).

The point is to let long or expensive work, big fan-outs, migrations, audits, /loops, self-pace
before it hits a limit rather than stopping halfway. When a budget is tight it uses smaller agent
fleets and cheaper model tiers for mechanical sub-work, but never trades away the depth or quality
of the actual deliverable, and it never throttles silently: every decision names the limit, its
percent, and its reset time, and offers a one-line override.

## What it does

- **Answers usage questions** ("how much do I have left", "am I close to my limit") with live
  numbers and reset times.
- **Governs its own resource use** proactively: before launching a Workflow, a wide agent
  fan-out, a /loop, or a long migration; at phase boundaries in long work; and before delegating
  sub-work to a top-tier model whose scoped weekly budget may be tighter than overall usage.

## Install

Drop the folder into your Claude Code skills directory:

```bash
git clone https://github.com/johnlawrimore/usage-governor.git ~/.claude/skills/usage-governor
```

Claude Code discovers the skill from `SKILL.md`. No further setup is required; the skill reads
your existing Claude Code credentials.

## Standalone CLI

The core is `scripts/check-usage.sh`, a self-contained script you can run on its own. It prints a
human-readable summary followed by one machine-readable JSON line:

```bash
~/.claude/skills/usage-governor/scripts/check-usage.sh
```

```
Claude usage (fetched 0s ago, source: network)
  Session (5h)              82%  resets in 40m (2026-...)  [severity=warning, active]
  Weekly (all models)       72%  resets in 2d 4h (2026-...)
  Weekly (Fable)            75%  resets in 2d 4h (2026-...)  [severity=warning]
  posture: wind_down (driven by Session (5h) at 82%)
---
{"available": true, "posture": "wind_down", "limits": [ ... ]}
```

Flags:

- `--json` prints only the JSON line.
- `--fresh` ignores the cache TTL and forces a network fetch (still respects the 429 backoff).

Environment overrides:

| Variable | Default | Meaning |
| --- | --- | --- |
| `CLAUDE_USAGE_CACHE` | `~/.claude/.usage-cache.json` | cache file path |
| `CLAUDE_USAGE_TTL` | `300` | cache TTL in seconds |
| `CLAUDE_USAGE_429_BACKOFF` | `900` | seconds to serve stale cache after a 429 |

Requires `bash` and `python3` (standard library only). Credentials are read from the macOS
Keychain, falling back to `~/.claude/.credentials.json` on other platforms.

## Output fields

Each entry in the `limits` array carries:

- `kind`: `session`, `weekly_all`, or `weekly_scoped`.
- `percent`: 0-100 utilization.
- `scope_model`: for `weekly_scoped`, the model's display name (e.g. `"Fable"`).
- `severity`: `normal` or an escalated tier.
- `is_active`: whether this is the limit currently binding.
- `resets_at` / `resets_in_seconds`: when the window rolls over (ISO 8601 and signed seconds).
- `burn`: `{delta_percent, over_seconds}` vs an earlier snapshot, or `null` if there is no
  baseline yet.

Top-level `posture` (`normal` / `frugal` / `wind_down`) is computed deterministically: a limit is
`frugal` at/above its frugal threshold (75% for `session` and `weekly_scoped`, 80% for
`weekly_all`) and `wind_down` at/above 90%; a non-`normal` severity bumps it one level; and a
`session` limit within 15 minutes of reset is relaxed one level. The worst limit wins, and
`posture_driver` names it.

## Caveats

**Private endpoint.** Usage data comes from `GET /api/oauth/usage`, a private, undocumented
Anthropic endpoint (the same backend behind claude.ai's usage page). It is aggressively
rate-limited, which is why the script caches aggressively and backs off on a 429. It may change or
break without notice; when it does, the script degrades gracefully to last-known data (clearly
labeled stale) and never invents numbers.

**Your token stays local.** The OAuth access token is read in-process from the macOS Keychain (or
`~/.claude/.credentials.json`) and is never printed, logged, or sent anywhere except that single
Anthropic request. The cache file is written with `0600` permissions.

**Not provider-generic (yet).** The concept, read a quota, adapt posture, throttle transparently,
is general, but this implementation is Anthropic-specific end to end. A future version could factor
out a usage-provider interface (the network fetch and the `parse_limits` normalizer are the natural
seam) and reuse the posture, burn-rate, and communication layer unchanged.

## Layout

```
usage-governor/
├── SKILL.md                 # skill instructions Claude Code loads
├── scripts/
│   └── check-usage.sh       # standalone usage CLI (bash + embedded Python)
└── README.md
```

## License

MIT
