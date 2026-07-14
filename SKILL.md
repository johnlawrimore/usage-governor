---
name: usage-governor
description: >-
  Check the user's live claude.ai subscription usage (5-hour session limit, weekly limit, and
  per-model weekly limits, which some models sometimes have, such as Fable) and adapt execution plans to it. Use this whenever the
  user asks about usage, quota, limits, "how much do I have left", "am I close to my limit", or
  when a limit warning appears. Just as importantly, use it proactively on your own initiative:
  BEFORE launching anything large (a Workflow, a wide Agent fan-out, a /loop, a long migration or
  audit), at phase boundaries inside long-running work, and BEFORE delegating sub-work to a
  top-tier model (Fable/Opus), since model-scoped weekly budgets may be tighter than overall
  usage. Reads are cache-served and effectively free (a check in the first few minutes after a
  limit resets may make one live call to refresh the rolled-over window), so checking at these
  decision points is safe.
license: MIT
metadata:
  author: John Lawrimore
  source: https://github.com/johnlawrimore/usage-governor
  version: 1.1.0
---

# usage-governor

Check live subscription usage and make execution decisions with it. This skill has two jobs:
answering "how much have I used" questions, and governing your own resource decisions during
long or expensive work.

## How to check

Run the launcher for the platform you are on (both take the same flags and print the same output):

```bash
# macOS / Linux
~/.claude/skills/usage-governor/scripts/check-usage.sh
```

```bat
:: Windows
%USERPROFILE%\.claude\skills\usage-governor\scripts\check-usage.cmd
```

The logic lives in `scripts/check-usage.py` (Python 3.8+, standard library only); the launchers just
find a Python interpreter and run it, so on any platform you can also invoke
`python3 .../check-usage.py` (or `python` / `py -3` on Windows) directly.

It prints a human-readable summary followed by one machine-readable JSON line. Add `--json` for
only the JSON line. The script caches the raw endpoint response, never the token, for 5 minutes
(`CLAUDE_USAGE_TTL` to change) at `<config>/.usage-cache.json`, where `<config>` is
`CLAUDE_CONFIG_DIR` or `~/.claude` (`CLAUDE_USAGE_CACHE` overrides the full path); within the TTL
it makes no network call at all, so calling it repeatedly is free and instant.

Do not curl the endpoint yourself, and do not pass `--fresh` unless the user explicitly asks for
a forced refresh: the underlying endpoint (`/api/oauth/usage`, private, undocumented) rate-limits
after only a few requests, and the cache is what makes frequent checks safe. Never print, log, or
echo the OAuth token; the script reads it in-process, in order, from the `CLAUDE_CODE_OAUTH_TOKEN`
env var, the macOS Keychain, then `<config>/.credentials.json`, and never outputs it.

## Reading the output

Usage pressure has two independent faces, and this skill scores both. **Where you are** is the raw
percent: how full a limit is right now. **Where you are heading** is pace: whether your current burn
rate will empty the window before it resets. A limit can be low on percent yet still binding, if it
is burning fast enough to run dry with hours left on the clock. Reacting only to percent (the old
behavior) means staying silent at 20% while you are on track to hit the wall well before reset. Both
axes feed one posture, worst rung wins.

The JSON line has `available`, `stale`, `age_seconds`, `source` (where the numbers came from:
`network`, `cache`, or a stale-cache variant), `posture`, `posture_driver`, and a `limits` array.
Each limit:

- `kind`: `session` (rolling 5-hour window), `weekly_all` (whole weekly allotment), or
  `weekly_scoped` (a sub-limit for one model or surface). This set is **not fixed**: model
  line-ups and billing change often, so treat any unfamiliar `kind` as a real limit and surface
  it rather than ignoring it (see "New and non-percent limits" below).
- `percent`: 0 to 100 utilization, or `null` for a meter that is not a percent (again, see below).
  A `null` percent does **not** mean "fine".
- `scope_model`: the model a scoped limit is tied to, as a display name (for example `"Fable"`).
  Read whatever appears here; never assume a particular model is or isn't present.
- `extra`: any fields the endpoint attached that aren't normalized above, preserved verbatim (for
  example a `remaining_credits` balance on a usage-credit meter). `null` when there are none.
- `severity`: `normal`, or an escalated tier. Treat anything other than `normal` as a signal to
  tighten behavior regardless of the raw percent.
- `is_active`: appears to indicate the limit currently binding (private-API field, semantics
  unverified).
- `resets_at`: ISO 8601 reset time. Always surface this when a limit influences a decision, so
  the user can choose to wait instead.
- `resets_in_seconds`: signed seconds to that reset, computed at read time. Prefer it over doing
  date math on `resets_at` yourself. A value at or below zero means the reset moment is in the past
  (the limit is flagged `reset-elapsed`). The script refetches on its own when a reset happened
  *after* the cached data was fetched, so you should not reach for `--fresh` on a `reset-elapsed`
  limit; an already-past reset time is intentionally left as-is to avoid a needless refetch loop.
- `burn`: `{delta_percent, over_seconds}` when an earlier snapshot is available, else `null`. This
  is the climb over roughly the last 15+ minutes (for example `+6%` over the last `45m`), not a
  session-long average, which answers "will this plan fit in what's left" far better than a bare
  percent. `null` just means no baseline yet (first check, or the last one was too recent); it is
  not zero burn.
- `pace`: `{ratio, exhaust_seconds}` when `burn` is present and pace can be judged (including a
  sustainable `ratio` below 1), else `null`. `ratio` is your current burn rate divided by the rate you could afford and still
  coast to reset, which is exactly `seconds_to_reset / seconds_to_exhaustion`. `1.0` means you land
  on empty right at reset; `1.5` means you hit the wall at two-thirds of the way there; `2.0` means
  halfway. `exhaust_seconds` is the projected wall-clock time to reach 100% at the current pace.
  This is the signal a bare percent misses: **a `ratio` above 1 means the window empties before it
  resets, no matter how low the percent is.** `null` means pace cannot be judged (no burn baseline,
  a non-percent meter, a tiny sub-2% wiggle too noisy to extrapolate, the reset already past, or the
  reset more than ~6 hours out, since a 15-minute burst does not project meaningfully across a
  multi-day window); a `null` `pace` is not "sustainable", it is "unknown", so fall back to percent
  and burn. Because pace is judged only within that ~6h horizon, it governs the whole 5-hour session
  window and the tail of a weekly window near its reset, but not a weekly limit that is still days
  from resetting: there, the percent axis governs.

Top-level `posture` is the script's own recommendation, derived deterministically so the reading
is consistent run to run. It is a five-rung ladder, weakest to strongest:

- `normal`: no throttling. Proceed at full scale.
- `measured`: do not waste. Keep the fleet at the task's natural size instead of reaching for
  maximum fan-out, and drop purely speculative extra passes, but do not yet reduce parallelism below
  what the task needs or downgrade model tiers. The earliest, gentlest nudge. (`frugal` is where you
  start actively shrinking fleets and moving sub-work to cheaper tiers.)
- `frugal`: bias toward smaller fleets and cheaper tiers for mechanical sub-work; skip speculative
  extra-thoroughness passes unless the user opts in.
- `conserve`: stop expanding scope. Cheapest viable tiers for all sub-work, checkpoint to disk
  often, and warn before starting anything nontrivial.
- `wind_down`: stop expanding scope; finish and persist current work, no new work.

It is computed per limit and the worst rung across every limit wins. Each limit is scored on two
independent axes and takes the worse of the two:

- **Percent axis** (how full now): `measured`/`frugal`/`conserve`/`wind_down` at `50`/`75`/`90`/`97`
  for `session`, `weekly_scoped`, and unknown kinds; at `55`/`80`/`90`/`95` for `weekly_all`, whose
  multi-day budget expects more mid-range use.
- **Pace axis** (how fast, from `pace.ratio`): `measured`/`frugal`/`conserve`/`wind_down` at ratio
  `1.0`/`1.25`/`2.0`/`4.0`. Escalation only ever begins at ratio `1.0`, i.e. only when the current
  pace would empty the window before it resets. A backstop floors pace at `conserve` when the window
  is already on pace to empty before reset (ratio at least `1.0`) and will do so within 30 minutes of
  wall-clock time. Burn below 2 percentage points is ignored here as noise, and pace is judged only
  when the reset is within ~6 hours (see the `pace` field above), so a weekly limit days from reset
  is scored on percent alone.

Then a non-`normal` severity bumps the result one rung, and a `session` limit within 15 minutes of
reset is relaxed one rung (a near-reset session window binds far less than the same reading hours
out). These thresholds are exactly the ones in the sections below, so `posture` and the prose never
disagree. `posture_driver` names the limit that set it. Treat `posture` as a strong default, not a
hard gate: you may override it up or down using `burn`, `pace`, task size, and how close the reset
is. If you diverge from `posture`, say why.

If `stale` is true, the data is the last known snapshot (endpoint rate-limited or unreachable);
say so when reporting it. If `available` is false, usage is unknown: proceed normally but
conservatively, and do not guess numbers.

### The no-op floor

When `posture` is `normal` (in practice: every limit below its `measured` percent band, sustainable
pace on all of them, and no elevated severity), take no action and say nothing about usage unless
the user asked. A proactive check that comes back clear should leave no trace in your plan or your
reply. Do not hedge, do not pre-emptively shrink a fleet, do not mention percentages. Silence below
the floor is what keeps proactive checking from quietly taxing everyday work. Note the floor is now
`posture == normal`, not a bare percent: a low-percent limit burning fast enough to escalate on the
pace axis is above the floor and worth surfacing, which is exactly the case a percent-only floor
used to miss.

### New and non-percent limits

The skill does not assume today's limit taxonomy is permanent. Model line-ups and billing shift
often, a model can move from a scoped weekly limit to metered usage credits, a new surface can get
its own budget, so the script passes through every limit the endpoint returns, unknown `kind`s
included, and never drops one just because it lacks a `percent`. When you see one:

- **An unfamiliar `kind` with a percent** behaves like any other limit. Its posture uses the
  default percent bands (`measured`/`frugal`/`conserve`/`wind_down` at 50/75/90/97) and the same
  pace axis as every other limit; report it by whatever name the endpoint gave it.
- **A meter with `percent: null` and data in `extra`** (typically a usage-credit balance or spend)
  is not a utilization percent, so percent-based posture scoring does not apply to it, but an
  elevated `severity` on that same meter still escalates posture. Do not read the missing percent
  as "all clear". Surface it to the user in plain terms, and remember that credit-based usage is
  usually **real money** (pay-as-you-go), so it warrants an explicit heads-up before you spend a
  lot of it, not silent consumption. If the shape is unclear, say what the raw `extra` fields show
  rather than guessing at a percentage.

## When to check

- The user asks about usage, quota, or limits.
- Before starting anything large: a Workflow, a multi-agent fan-out, a /loop, a big migration or
  audit. One check, then decide scale.
- At phase boundaries inside long work (between finder and verifier rounds, between migration
  batches). These reads come from cache and cost nothing.
- Before delegating sub-work to a specific top-tier model: check that model's scoped limit first
  (see below).
- Not on every trivial turn, and never in a tight polling loop with `--fresh`.

## Turning usage into decisions

Thresholds below are guidance, not hard laws; combine them with task size and how far away the
reset is. A 90% session reading 10 minutes before reset is a very different situation from 90%
with 4 hours to go. And percent is only half the picture: a 20% session that `pace.ratio` says will
empty in under three hours, with four hours to reset, binds like a much fuller window. Read pace
alongside percent, never percent alone.

### What frugality may and may not touch

Usage pressure changes *how much machinery* you throw at a task, never *how well you do the
task the user asked for*. When a limit pushes you toward frugality you may reduce parallelism
(smaller agent fleets), drop speculative extra-thoroughness passes, and downgrade the model tier
for mechanical sub-work. You may not shorten your reads, skip edge cases, cut corners on the
primary deliverable, or lower the depth and rigor of the actual answer. Budget is a reason to use
fewer agents, not a reason to think less. And it never lowers verification or Definition-of-Done
review below the level that work requires (see the model-scoped section). If the real work cannot
be done well within the remaining budget, say so and let the user decide, rather than silently
delivering a thinner result.

Each rung of the ladder is reached by either axis, whichever is worse (see "Reading the output"),
so the guidance below is written per rung, not per percent. A pace-driven `frugal` at 20% is the
same `frugal` as a percent-driven one at 75%: act on the rung, not on which axis raised it.

### Session (5-hour) limit: governs right now

- `measured` (percent ~50%+, or pace ratio ~1.0-1.25): right-size the fleet instead of reaching for
  maximum fan-out, and drop purely speculative extra passes. No constraint on the actual task yet.
- `frugal` (percent ~75%+, or pace ratio ~1.25-2.0): reduce parallelism, prefer cheaper model tiers
  for sub-work, checkpoint progress sooner, and warn the user before starting anything big.
- `conserve` (percent ~90%+, or pace ratio ~2.0-4.0, or under ~30 min of runway left): stop
  expanding scope, use the cheapest viable tier for all sub-work, checkpoint often, and tell the
  user the reset time so they can decide whether to wait.
- `wind_down` (percent ~97%+, or pace ratio ~4.0+): finish and persist current work only, no new
  scope, and tell the user when the window resets.

### Weekly (all models) limit: governs the multi-day budget

The same rungs apply, with the percent bands set a little higher (`measured`/`frugal`/`conserve`/
`wind_down` at ~55/80/90/95%) because a multi-day budget expects steady mid-range use. Pace matters
here too: a weekly limit climbing fast enough to exhaust before its multi-day reset escalates on the
pace axis just as a session limit does. At `conserve` or `wind_down`, bias the whole task toward
frugality, stop expanding scope for the rest of the week's work, and mention the weekly reset time.

### Severity override

Any limit with `severity` other than `normal` tightens the rules above by one notch regardless
of its raw percent. If `is_active` is true on a limit, that appears to be the budget currently
being drawn down; give it extra weight.

## Model-scoped limits

The `limits` array can contain `weekly_scoped` entries tied to a specific model via
`scope_model`. Some models sometimes carry their own weekly budget on top of the shared one;
this is not guaranteed for any model, and the scoped set changes with plans and model line-ups.
When present, these per-model budgets can run out even when overall usage is fine, and they
matter most for top-tier models used deliberately for judgment-heavy sub-work.

Which models are scoped, and whether they are scoped at all, is not stable, a model can carry a
scoped weekly limit for a while and later move to metered usage credits (surfaced as a non-percent
meter, see "New and non-percent limits"). So read whatever `scope_model` values actually appear
this run; do not assume any specific model is present, and do not treat the absence of a scoped
limit as meaning that model is unlimited (it may now bill via credits instead).

Rules, generalized to whatever scoped models appear:

- Before delegating sub-work to a model that has a scoped limit, read that limit's percent.
- If the scoped limit is high (above ~75%, or its `pace` shows it emptying before its reset even
  at a lower percent) while overall usage is fine, prefer a different
  tier for that sub-work (for example Opus or Sonnet instead of Fable), or ask the user whether
  this task is worth spending the scarce budget on. If you are running autonomously and cannot ask
  (a scheduled run, a /loop, an unattended workflow), take the safe default instead of blocking:
  drop to the cheaper tier, note the swap in your output, and let the user reverse it later.
- If the scoped limit has plenty of headroom, do not needlessly downgrade: the top tier is the
  right tool for its designated work (architecture, design decisions, adversarial verification,
  final reviews).
- This composes with the user's standing model-selection discipline (cheapest reliable tier for
  mechanical work, top tier for judgment work): usage state is one more input that can push a
  borderline choice down a tier when the relevant budget is tight. It never justifies
  downgrading verification or Definition-of-Done review work below what the discipline requires;
  if that work cannot be afforded at the right tier, say so and let the user decide.

## Communicating throttling decisions

When usage changes your plan, say so briefly and concretely: which limit, at what percent, when
it resets, and what you changed (fewer agents, cheaper tier, deferred pass). When pace is what
drove the decision, say that too, since the percent alone will look harmless: name the projected
exhaustion time. Offer the override in one line: "say the word and I'll run it at full scale" or
"tell me to use Fable anyway". Never silently throttle, and never silently blow through a budget
without warning.

Example (percent-driven): "Heads up: the Fable weekly limit is at 82% (resets Wednesday 19:00
UTC), so I'll run the design review on Opus instead. Say the word if you want Fable anyway."

Example (pace-driven): "The 5-hour session is only at 20%, but at the current pace it'll hit the
limit in ~2h54m, before it resets in 4h16m, so I'll trim the fan-out and use Sonnet for the
mechanical passes. Say the word to run it at full scale."

In an autonomous run where no one is watching, you cannot offer a live override, but the
transparency rule still holds: record the same one-liner in your output or the run log, so the
decision is visible after the fact rather than silent.

## Failure modes

- `stale: true`: report the numbers but label them as last-known, with their age.
- 401 in the reason field: tell the user the token is stale and that running any Claude Code
  command refreshes it, then retry.
- No credentials: the script found no token in `CLAUDE_CODE_OAUTH_TOKEN`, the macOS Keychain, or
  `<config>/.credentials.json`. Usage is unavailable; say so plainly and proceed conservatively.
  On Windows the token lives at `%USERPROFILE%\.claude\.credentials.json` (or under
  `CLAUDE_CONFIG_DIR`); logging in via Claude Code creates it.
- Unrecognized response shape: usage is unavailable (the private format may have changed). Say so,
  proceed conservatively, never invent numbers, and also suggest the user check
  https://github.com/johnlawrimore/usage-governor for an updated version, since shape drift is
  exactly what an update fixes.
- Last-resort estimate only: local session logs (`~/.claude/projects/**/*.jsonl`, summed by tools
  like `npx ccusage`) can give a rough retrospective token estimate. It is known to be very
  inaccurate and measures spend, not quota. Only mention it if the endpoint is entirely
  unreachable, and clearly label it as a rough local estimate.
