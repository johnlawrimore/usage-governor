---
name: usage-governor
description: >-
  Check the user's live claude.ai subscription usage (5-hour session limit, weekly limit, and
  per-model weekly limits such as Fable) and adapt execution plans to it. Use this whenever the
  user asks about usage, quota, limits, "how much do I have left", "am I close to my limit", or
  when a limit warning appears. Just as importantly, use it proactively on your own initiative:
  BEFORE launching anything large (a Workflow, a wide Agent fan-out, a /loop, a long migration or
  audit), at phase boundaries inside long-running work, and BEFORE delegating sub-work to a
  top-tier model (Fable/Opus), since model-scoped weekly budgets may be tighter than overall
  usage. Reads are cache-served and effectively free (a check in the first few minutes after a
  limit resets may make one live call to refresh the rolled-over window), so checking at these
  decision points is safe.
---

# usage-governor

Check live subscription usage and make execution decisions with it. This skill has two jobs:
answering "how much have I used" questions, and governing your own resource decisions during
long or expensive work.

## How to check

Run the helper (works from any directory):

```bash
~/.claude/skills/usage-governor/scripts/check-usage.sh
```

It prints a human-readable summary followed by one machine-readable JSON line. Add `--json` for
only the JSON line. The script caches responses for 5 minutes (`CLAUDE_USAGE_TTL` to change) at
`~/.claude/.usage-cache.json` (`CLAUDE_USAGE_CACHE` to change); within the TTL it makes no
network call at all, so calling it repeatedly is free and instant.

Do not curl the endpoint yourself, and do not pass `--fresh` unless the user explicitly asks for
a forced refresh: the underlying endpoint (`/api/oauth/usage`, private, undocumented) rate-limits
after only a few requests, and the cache is what makes frequent checks safe. Never print, log, or
echo the OAuth token; the script reads it in-process (macOS Keychain, or
`~/.claude/.credentials.json` elsewhere) and never outputs it.

## Reading the output

The JSON line has `available`, `stale`, `age_seconds`, `source` (where the numbers came from:
`network`, `cache`, or a stale-cache variant), `posture`, `posture_driver`, and a `limits` array.
Each limit:

- `kind`: `session` (rolling 5-hour window), `weekly_all` (whole weekly allotment), or
  `weekly_scoped` (a sub-limit for one model or surface).
- `percent`: 0 to 100 utilization.
- `scope_model`: for `weekly_scoped`, the model's display name (for example `"Fable"`).
- `severity`: `normal`, or an escalated tier. Treat anything other than `normal` as a signal to
  tighten behavior regardless of the raw percent.
- `is_active`: whether this limit is the one currently binding.
- `resets_at`: ISO 8601 reset time. Always surface this when a limit influences a decision, so
  the user can choose to wait instead.
- `resets_in_seconds`: signed seconds to that reset, computed at read time. Prefer it over doing
  date math on `resets_at` yourself. A value at or below zero means the window already reset (the
  script flags this limit `reset-elapsed` and forces a live refetch rather than trusting the old
  percent).
- `burn`: `{delta_percent, over_seconds}` when an earlier snapshot is available, else `null`. This
  is how fast the limit is climbing (for example `+6%` over the last `45m`), which answers "will
  this plan fit in what's left" far better than a bare percent. `null` just means no baseline yet
  (first check, or the last one was too recent); it is not zero burn.

Top-level `posture` is the script's own recommendation, derived deterministically so the reading
is consistent run to run:

- `normal`: no throttling. Proceed at full scale.
- `frugal`: bias toward smaller fleets and cheaper tiers for mechanical sub-work; skip speculative
  extra-thoroughness passes unless the user opts in.
- `wind_down`: stop expanding scope; finish and persist current work.

It is computed per limit and the worst wins: a limit is `frugal` at/above its frugal threshold
(75% for `session` and `weekly_scoped`, 80% for `weekly_all`) and `wind_down` at/above 90%; a
non-`normal` severity bumps it one level; and a `session` limit within 15 minutes of reset is
relaxed one level (a near-reset session window binds far less than the same percent hours out).
These thresholds are exactly the ones in the sections below, so `posture` and the prose never
disagree. `posture_driver` names the limit that set it. Treat `posture` as a strong default, not a
hard gate: you may override it up or down using `burn`, task size, and how close the reset is. If
you diverge from `posture`, say why.

If `stale` is true, the data is the last known snapshot (endpoint rate-limited or unreachable);
say so when reporting it. If `available` is false, usage is unknown: proceed normally but
conservatively, and do not guess numbers.

### The no-op floor

When every limit is `normal` severity and `posture` is `normal` (in practice, no limit has crossed
its frugal threshold: session and per-model scoped below 75%, the weekly allotment below 80%), take
no action and say nothing about usage unless the user asked. A proactive check that
comes back clear should leave no trace in your plan or your reply. Do not hedge, do not
pre-emptively shrink a fleet, do not mention percentages. Silence below the floor is what keeps
proactive checking from quietly taxing everyday work.

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
with 4 hours to go.

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

### Session (5-hour) limit: governs right now

- Above ~75% with the reset still far off: reduce parallelism (smaller agent fleets), prefer
  cheaper model tiers for sub-work, checkpoint progress to disk sooner, and warn the user before
  starting anything big.
- Above ~90%: stop expanding scope. Finish and persist current work, skip optional passes, and
  tell the user when the window resets so they can decide whether to wait.

### Weekly (all models) limit: governs the multi-day budget

- Above ~80%: bias toward frugality for the whole task. Smaller fleets, cheaper tiers, no
  speculative extra-thoroughness passes unless the user opts in. Mention the weekly reset time.
- Above ~90%: stop expanding scope for the rest of the week's work, the same as a session
  `wind_down`. Finish and persist what is in flight and tell the user the weekly reset time.

### Severity override

Any limit with `severity` other than `normal` tightens the rules above by one notch regardless
of its raw percent. If `is_active` is true on a limit, that is the budget currently being drawn
down; weight it most heavily.

## Model-scoped limits (Fable and friends)

The `limits` array can contain `weekly_scoped` entries tied to a specific model via
`scope_model`. These are per-model weekly budgets that can run out even when overall usage is
fine, and they matter most for top-tier models used deliberately for judgment-heavy sub-work.

Rules, generalized to whatever scoped models appear (not hard-coded to Fable):

- Before delegating sub-work to a model that has a scoped limit, read that limit's percent.
- If the scoped limit is high (above ~75%) even though overall usage is fine, prefer a different
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
it resets, and what you changed (fewer agents, cheaper tier, deferred pass). Offer the override
in one line: "say the word and I'll run it at full scale" or "tell me to use Fable anyway".
Never silently throttle, and never silently blow through a budget without warning.

Example: "Heads up: the Fable weekly limit is at 82% (resets Wednesday 19:00 UTC), so I'll run
the design review on Opus instead. Say the word if you want Fable anyway."

In an autonomous run where no one is watching, you cannot offer a live override, but the
transparency rule still holds: record the same one-liner in your output or the run log, so the
decision is visible after the fact rather than silent.

## Failure modes

- `stale: true`: report the numbers but label them as last-known, with their age.
- 401 in the reason field: tell the user the token is stale and that running any Claude Code
  command refreshes it, then retry.
- No credentials or unrecognized response shape: usage is unavailable. Say so plainly, proceed
  conservatively, and never invent numbers.
- Last-resort estimate only: local session logs (`~/.claude/projects/**/*.jsonl`, summed by tools
  like `npx ccusage`) can give a rough retrospective token estimate. It is known to be very
  inaccurate and measures spend, not quota. Only mention it if the endpoint is entirely
  unreachable, and clearly label it as a rough local estimate.
