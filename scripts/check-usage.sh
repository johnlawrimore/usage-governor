#!/usr/bin/env bash
# check-usage.sh: report claude.ai subscription usage (session, weekly, per-model limits).
#
# Data source: the private endpoint GET https://api.anthropic.com/api/oauth/usage
# (the same backend behind claude.ai /settings/usage and Claude Code /usage).
# It is aggressively rate-limited, so this script caches responses and only
# hits the network on a cache miss.
#
# Beyond the raw percents it derives three decision aids so callers do not have
# to re-implement the policy each time:
#   - resets_in_seconds : time-to-reset per limit (models are unreliable at date math)
#   - burn              : utilization delta vs an earlier snapshot ("+6% / 45m")
#   - posture           : normal | frugal | wind_down, from percent + severity + reset
#
# Env vars:
#   CLAUDE_USAGE_CACHE        cache file path   (default: ~/.claude/.usage-cache.json)
#   CLAUDE_USAGE_TTL          cache TTL seconds (default: 300)
#   CLAUDE_USAGE_429_BACKOFF  seconds to serve stale cache after a 429 (default: 900)
#
# Flags:
#   --json    print only the machine-readable JSON line
#   --fresh   ignore TTL and force a network fetch (still respects 429 backoff)
#
# Exit codes: 0 = printed usage data (fresh or stale), 2 = no data available.
# The access token is read in-process and never printed or logged.

set -euo pipefail

CLAUDE_USAGE_CACHE="${CLAUDE_USAGE_CACHE:-$HOME/.claude/.usage-cache.json}"
CLAUDE_USAGE_TTL="${CLAUDE_USAGE_TTL:-300}"
CLAUDE_USAGE_429_BACKOFF="${CLAUDE_USAGE_429_BACKOFF:-900}"
export CLAUDE_USAGE_CACHE CLAUDE_USAGE_TTL CLAUDE_USAGE_429_BACKOFF

JSON_ONLY=0
FORCE_FRESH=0
for arg in "$@"; do
  case "$arg" in
    --json)  JSON_ONLY=1 ;;
    --fresh) FORCE_FRESH=1 ;;
    *) echo "unknown flag: $arg" >&2; exit 2 ;;
  esac
done
export JSON_ONLY FORCE_FRESH

# Locate the OAuth credentials blob. macOS: Keychain first, file as fallback.
# Elsewhere: file first. Passed to python via env, never via argv.
read_credentials() {
  if [[ "$(uname -s)" == "Darwin" ]]; then
    security find-generic-password -s "Claude Code-credentials" -w 2>/dev/null \
      || cat "$HOME/.claude/.credentials.json" 2>/dev/null \
      || true
  else
    cat "$HOME/.claude/.credentials.json" 2>/dev/null \
      || security find-generic-password -s "Claude Code-credentials" -w 2>/dev/null \
      || true
  fi
}

CLAUDE_USAGE_CREDS="$(read_credentials)"
export CLAUDE_USAGE_CREDS

exec python3 - <<'PYTHON'
import json, os, sys, time, urllib.request, urllib.error
from datetime import datetime, timezone

CACHE_PATH = os.environ["CLAUDE_USAGE_CACHE"]
TTL = int(os.environ["CLAUDE_USAGE_TTL"])
BACKOFF = int(os.environ["CLAUDE_USAGE_429_BACKOFF"])
JSON_ONLY = os.environ["JSON_ONLY"] == "1"
FORCE_FRESH = os.environ["FORCE_FRESH"] == "1"
ENDPOINT = "https://api.anthropic.com/api/oauth/usage"
NOW = time.time()

HISTORY_MAX = 72          # cap snapshots kept in the cache (~6h at the 5-min TTL)
HISTORY_MAX_AGE = 21600   # 6h: don't compute burn across gaps older than this
BURN_MIN_ELAPSED = 120    # need >=2 min between samples for a meaningful delta

LEVELS = ["normal", "frugal", "wind_down"]

# Snapshots kept for burn-rate math; populated from the cache in the main flow.
HISTORY = []


def fail(reason, hint=""):
    msg = {"available": False, "reason": reason}
    if hint:
        msg["hint"] = hint
    if not JSON_ONLY:
        print(f"Usage unavailable: {reason}")
        if hint:
            print(f"Hint: {hint}")
    print(json.dumps(msg))
    sys.exit(2)


def load_cache():
    try:
        with open(CACHE_PATH) as f:
            c = json.load(f)
        if isinstance(c, dict):
            return c
    except (OSError, ValueError):
        pass
    return {}


def save_cache(cache):
    try:
        tmp = CACHE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cache, f)
        os.replace(tmp, CACHE_PATH)
        os.chmod(CACHE_PATH, 0o600)
    except OSError:
        pass  # caching is best-effort; never block the report on it


def get_token():
    raw = os.environ.get("CLAUDE_USAGE_CREDS", "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)["claudeAiOauth"]["accessToken"]
    except (ValueError, KeyError, TypeError):
        return None


def fetch(token):
    req = urllib.request.Request(
        ENDPOINT,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, None
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None, None


def seconds_until(iso_ts):
    """Signed seconds from now to an ISO 8601 timestamp; None if unparseable."""
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        return (dt - datetime.now(timezone.utc)).total_seconds()
    except (ValueError, AttributeError, TypeError):
        return None


def rel_time(iso_ts):
    """Human 'in 1h 32m' from an ISO 8601 timestamp; '' if unparseable."""
    secs = seconds_until(iso_ts)
    if secs is None:
        return ""
    if secs <= 0:
        return "now"
    days, rem = divmod(int(secs), 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    if days:
        return f"in {days}d {hours}h"
    if hours:
        return f"in {hours}h {mins}m"
    return f"in {mins}m"


def short_dur(secs):
    """Compact duration for burn display: '45m' or '3h05m'."""
    mins = int(secs // 60)
    if mins < 60:
        return f"{mins}m"
    hours, mins = divmod(mins, 60)
    return f"{hours}h{mins:02d}m"


def parse_limits(data):
    """Extract a normalized list of limits; None if the shape is unrecognizable."""
    limits = data.get("limits") if isinstance(data, dict) else None
    out = []
    if isinstance(limits, list):
        for l in limits:
            if not isinstance(l, dict) or "percent" not in l:
                continue
            scope_model = None
            scope = l.get("scope")
            if isinstance(scope, dict):
                model = scope.get("model")
                if isinstance(model, dict):
                    scope_model = model.get("display_name")
            resets_at = l.get("resets_at")
            s = seconds_until(resets_at)
            out.append({
                "kind": l.get("kind"),
                "percent": l.get("percent"),
                "severity": l.get("severity"),
                "is_active": l.get("is_active"),
                "resets_at": resets_at,
                "resets_in_seconds": int(s) if s is not None else None,
                "scope_model": scope_model,
            })
    if out:
        return out
    # Fallback: older/simpler shape with just five_hour / seven_day blocks.
    for key, kind in (("five_hour", "session"), ("seven_day", "weekly_all")):
        blk = data.get(key) if isinstance(data, dict) else None
        if isinstance(blk, dict) and "utilization" in blk:
            util = blk.get("utilization")
            # 'utilization' is conventionally a 0-1 fraction; scale to 0-100.
            # Values already above 1 are assumed to be percents and left as-is.
            # Heuristic caveat: a genuine sub-1% value (e.g. 0.5 meaning 0.5%)
            # would be misread as 50%. This only affects the legacy fallback
            # shape, which the live endpoint does not currently emit.
            if isinstance(util, (int, float)) and 0 <= util <= 1:
                util = util * 100
            resets_at = blk.get("resets_at")
            s = seconds_until(resets_at)
            out.append({
                "kind": kind,
                "percent": util,
                "severity": None,
                "is_active": None,
                "resets_at": resets_at,
                "resets_in_seconds": int(s) if s is not None else None,
                "scope_model": None,
            })
    return out or None


def reset_passed(limits, fetched_at):
    """True if any limit reset AFTER the cached data was fetched, meaning the
    cached percent describes a window that has since rolled over.

    The `fetched_at` guard is essential: the endpoint can legitimately return a
    `resets_at` already in the past (an idle limit, server-side lag right after a
    boundary, or local clock skew). Invalidating on those would make every call
    refetch and spiral into the 429 backoff. We only invalidate when the reset
    moment falls after the fetch -- i.e. we are holding pre-reset numbers."""
    for l in (limits or []):
        s = l.get("resets_in_seconds")
        if s is None or s > 0:
            continue
        reset_epoch = NOW + s  # absolute time the reset occurred
        if reset_epoch > fetched_at:
            return True
    return False


def limit_key(l):
    return f"{l.get('kind')}|{l.get('scope_model') or ''}"


def compute_burn(limits, data_at):
    """Annotate each limit with a 'burn' delta vs the oldest same-window snapshot
    taken strictly before this data point.

    `data_at` is when the rendered percents were actually measured (which may be
    earlier than now, if they are served from cache). Measuring elapsed from
    data_at and only accepting baselines older than it stops a reading from being
    compared to the very snapshot it came from, which would fabricate a hard
    '+0%'. History entries are user-corruptible, so every field is type-checked."""
    usable = [h for h in HISTORY
              if isinstance(h, dict) and isinstance(h.get("limits"), dict)
              and h.get("at", 0) < data_at
              and (data_at - h.get("at", 0)) <= HISTORY_MAX_AGE]
    usable.sort(key=lambda x: x.get("at", 0))  # oldest first -> most stable window
    for l in limits:
        l["burn"] = None
        if not isinstance(l.get("percent"), (int, float)):
            continue
        key = limit_key(l)
        cur_reset = l.get("resets_at")
        for h in usable:
            elapsed = data_at - h.get("at", 0)
            if elapsed < BURN_MIN_ELAPSED:
                continue  # too close in time to be a meaningful baseline
            past = h["limits"].get(key)
            if not isinstance(past, (int, float)):
                continue
            # Same-window guard: if the baseline recorded a different reset time,
            # a reset boundary fell between the samples, so the delta understates
            # the true burn (10% -> reset -> 15% would read as a tame +5%). Older
            # caches with no 'resets' map fall back to the negative-delta check.
            base_reset = (h.get("resets") or {}).get(key)
            if base_reset is not None and cur_reset is not None and base_reset != cur_reset:
                continue
            delta = l["percent"] - past
            if delta < 0:
                continue  # a reset happened between samples; rate not meaningful
            l["burn"] = {"delta_percent": round(delta, 1), "over_seconds": int(elapsed)}
            break


def _bump(p):
    return LEVELS[min(LEVELS.index(p) + 1, len(LEVELS) - 1)]


def _relax(p):
    return LEVELS[max(LEVELS.index(p) - 1, 0)]


# Percent at which each limit kind enters 'frugal'. These mirror the thresholds
# in SKILL.md exactly (session and per-model scoped tighten at 75%, the whole
# weekly allotment at 80%); 'wind_down' is a uniform 90% for every kind. Keep
# these two sources in lockstep -- the posture is meant to REMOVE ambiguity, so
# it must agree with the prose the model reads.
FRUGAL_AT = {"session": 75, "weekly_all": 80, "weekly_scoped": 75}
WIND_DOWN_AT = 90


def limit_posture(l):
    pct = l["percent"] if isinstance(l.get("percent"), (int, float)) else 0
    frugal_at = FRUGAL_AT.get(l.get("kind"), 75)  # default to the tighter bound
    if pct >= WIND_DOWN_AT:
        p = "wind_down"
    elif pct >= frugal_at:
        p = "frugal"
    else:
        p = "normal"
    if l.get("severity") not in (None, "normal"):
        p = _bump(p)
    # Near-reset relief for the short (session) window only: high usage 10 min
    # before reset binds far less than the same usage hours out. Weekly windows,
    # which reset days out, get no such relief.
    reset_s = l.get("resets_in_seconds")
    if l.get("kind") == "session" and reset_s is not None and 0 < reset_s < 900:
        p = _relax(p)
    return p


def overall_posture(limits):
    """Worst per-limit posture wins; return (posture, driving_limit_or_None)."""
    worst, driver = "normal", None
    for l in limits:
        p = limit_posture(l)
        if LEVELS.index(p) > LEVELS.index(worst):
            worst, driver = p, l
    return worst, driver


def render(limits, age_secs, stale, source):
    compute_burn(limits, NOW - age_secs)
    posture, driver = overall_posture(limits)
    if not JSON_ONLY:
        age_str = (f"{int(age_secs // 60)}m {int(age_secs % 60)}s ago"
                   if age_secs >= 60 else f"{int(age_secs)}s ago")
        stale_note = " [STALE: showing last known data]" if stale else ""
        print(f"Claude usage (fetched {age_str}, source: {source}){stale_note}")
        for l in limits:
            pct = f"{round(l['percent'])}%" if isinstance(l["percent"], (int, float)) else "?"
            reset = rel_time(l.get("resets_at") or "")
            reset_str = f"  resets {reset} ({l['resets_at']})" if reset else ""
            flags = []
            if l.get("severity") not in (None, "normal"):
                flags.append(f"severity={l['severity']}")
            if l.get("is_active"):
                flags.append("active")
            if l.get("resets_in_seconds") is not None and l["resets_in_seconds"] <= 0:
                flags.append("reset-elapsed")
            flag_str = f"  [{', '.join(flags)}]" if flags else ""
            burn = l.get("burn")
            burn_str = (f"  +{burn['delta_percent']}% / {short_dur(burn['over_seconds'])}"
                        if burn else "")
            print(f"  {label(l):<24} {pct:>4}{reset_str}{flag_str}{burn_str}")
        if driver and isinstance(driver.get("percent"), (int, float)):
            driver_str = f" (driven by {label(driver)} at {round(driver['percent'])}%)"
        elif driver:
            driver_str = f" (driven by {label(driver)})"
        else:
            driver_str = ""
        print(f"  posture: {posture}{driver_str}")
        print("---")
    print(json.dumps({
        "available": True,
        "stale": stale,
        "source": source,
        "age_seconds": int(age_secs),
        "posture": posture,
        "posture_driver": ({
            "kind": driver["kind"],
            "label": label(driver),
            "percent": driver["percent"],
        } if driver else None),
        "limits": limits,
    }))
    sys.exit(0)


def label(l):
    if l["kind"] == "session":
        return "Session (5h)"
    if l["kind"] == "weekly_all":
        return "Weekly (all models)"
    if l["kind"] == "weekly_scoped":
        return f"Weekly ({l['scope_model'] or 'scoped'})"
    return str(l["kind"] or "unknown limit")


cache = load_cache()
HISTORY = cache.get("history", [])
if not isinstance(HISTORY, list):
    HISTORY = []
cached_data = cache.get("data")
fetched_at = cache.get("fetched_at", 0)
cache_age = NOW - fetched_at if fetched_at else None
cached_limits = parse_limits(cached_data) if cached_data else None

# 1. Fresh cache: serve it, no network call -- unless a limit has passed its
#    reset, in which case the cached percent is for a window that no longer
#    exists, so we fall through and fetch fresh.
if (not FORCE_FRESH and cached_limits and cache_age is not None
        and cache_age < TTL and not reset_passed(cached_limits, fetched_at)):
    render(cached_limits, cache_age, stale=False, source="cache")

# 2. Recently rate-limited: don't retry yet, serve stale if we have it.
last_429 = cache.get("last_429_at", 0)
if last_429 and (NOW - last_429) < BACKOFF:
    if cached_limits:
        render(cached_limits, cache_age or 0, stale=True, source="cache (429 backoff)")
    fail("endpoint rate-limited (429) and no cached data",
         f"retry after {int(BACKOFF - (NOW - last_429))}s")

# 3. Cache miss (or reset boundary crossed): fetch.
token = get_token()
if not token:
    if cached_limits:
        render(cached_limits, cache_age or 0, stale=True, source="cache (no credentials)")
    fail("no OAuth credentials found",
         "expected macOS Keychain item 'Claude Code-credentials' or ~/.claude/.credentials.json")

status, data = fetch(token)

if status == 200 and data is not None:
    limits = parse_limits(data)
    if limits:
        # Append this snapshot to history so future runs can compute burn rate.
        # 'resets' records each limit's reset time so burn can tell whether a
        # reset boundary fell between two samples (see compute_burn).
        snapshot = {
            "at": NOW,
            "limits": {limit_key(l): l["percent"]
                       for l in limits if isinstance(l["percent"], (int, float))},
            "resets": {limit_key(l): l.get("resets_at")
                       for l in limits if isinstance(l["percent"], (int, float))},
        }
        HISTORY = [h for h in HISTORY if isinstance(h, dict)
                   and (NOW - h.get("at", 0)) <= HISTORY_MAX_AGE]
        HISTORY.append(snapshot)
        HISTORY = HISTORY[-HISTORY_MAX:]
        save_cache({"fetched_at": NOW, "data": data, "last_429_at": 0, "history": HISTORY})
        render(limits, 0, stale=False, source="network")
    # 200 but unrecognizable shape: the private API may have changed.
    if cached_limits:
        render(cached_limits, cache_age or 0, stale=True, source="cache (response shape changed)")
    fail("endpoint returned 200 but the response shape was not recognized",
         "the private /api/oauth/usage format may have changed")

if status == 429:
    cache["last_429_at"] = NOW
    save_cache(cache)
    if cached_limits:
        render(cached_limits, cache_age or 0, stale=True, source="cache (got 429)")
    fail("endpoint rate-limited (429) and no cached data",
         f"retry in ~{BACKOFF}s; the script will back off automatically")

if status == 401:
    if cached_limits:
        render(cached_limits, cache_age or 0, stale=True, source="cache (token stale)")
    fail("OAuth token is stale (401)",
         "run any Claude Code command to refresh credentials, then retry")

# Network error or unexpected status.
if cached_limits:
    render(cached_limits, cache_age or 0, stale=True,
           source=f"cache (fetch failed{': HTTP ' + str(status) if status else ''})")
fail(f"could not reach usage endpoint{' (HTTP ' + str(status) + ')' if status else ''}",
     "check network connectivity")
PYTHON
