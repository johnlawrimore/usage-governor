#!/usr/bin/env python3
"""check-usage: report claude.ai subscription usage (session, weekly, per-model, and any
future limit kinds such as usage-credit meters).

Cross-platform (macOS, Windows, Linux), Python standard library only. Normally invoked through
the small OS launchers next to it (check-usage.sh on Unix, check-usage.cmd on Windows), but it can
be run directly with Python 3.8+.

Data source: the private endpoint GET https://api.anthropic.com/api/oauth/usage (the same backend
behind claude.ai /settings/usage and Claude Code /usage). It is aggressively rate-limited, so this
script caches responses and only hits the network on a cache miss.

Beyond the raw percents it derives three decision aids so callers do not have to re-implement the
policy each time:
  - resets_in_seconds : time-to-reset per limit (models are unreliable at date math)
  - burn              : climb over roughly the last 15+ minutes vs an earlier snapshot ("+6% / 45m")
  - posture           : normal | frugal | wind_down, from percent + severity + reset

Design note on brittleness: the endpoint's set of limit "kinds" is NOT assumed to be fixed. Model
line-ups and billing change often (for example a model moving from a scoped weekly limit to
metered usage credits). Every limit the endpoint returns is passed through, unknown kinds
included, and any meter that is not a 0-100 percent is surfaced under `extra` rather than dropped,
so a new kind of budget never silently reads as "all clear".

Credentials (checked in order): the CLAUDE_CODE_OAUTH_TOKEN env var; the macOS Keychain item
"Claude Code-credentials"; then <config>/.credentials.json where <config> is CLAUDE_CONFIG_DIR or
~/.claude. The token is used in-process for the one request and is never printed or logged.

Flags:  --json (print only the JSON line)   --fresh (ignore the cache TTL; still respects backoff)
        --help / -h (print usage, exit 0)   --version (print version, exit 0)
Env:    CLAUDE_USAGE_CACHE, CLAUDE_USAGE_TTL, CLAUDE_USAGE_429_BACKOFF, CLAUDE_CONFIG_DIR
Exit:   0 = printed usage data (fresh or stale) OR --help/--version output, 2 = no data
        available / bad flag.
Version: 1.0.0

Author: John Lawrimore (https://github.com/johnlawrimore/usage-governor). MIT license.
"""

import json
import math
import os
import platform
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

# Don't let an odd byte in a display name crash output on a legacy Windows console (cp1252/cp437);
# the JSON line is ASCII-safe already, but the human summary passes scope names through print().
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="backslashreplace")
    except (AttributeError, ValueError):
        pass

NOW = time.time()
VERSION = "1.0.0"


def is_num(v):
    """True for a real, finite number. Excludes bool (a subclass of int) and NaN/Infinity, which
    json.loads accepts and which would otherwise crash round() or emit non-strict JSON."""
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _num_or_none(v):
    """Normalize a percent to a finite number or None, so NaN/Infinity (which json.loads accepts)
    never reach the output as non-strict JSON literals. Shared utility: used by the provider
    section's parse_limits() below, but kept here because it's generic (not endpoint-specific)."""
    return v if is_num(v) else None

HISTORY_MAX = 72          # cap snapshots kept in the cache (~6h at the 5-min TTL)
HISTORY_MAX_AGE = 21600   # 6h: don't compute burn across gaps older than this
BURN_MIN_ELAPSED = 120    # need >=2 min between samples for a meaningful delta
BURN_TARGET_WINDOW = 900  # 15 min: preferred baseline age for a "recent climb" reading

LEVELS = ["normal", "frugal", "wind_down"]

# Percent at which each limit kind enters 'frugal'. These mirror the thresholds in SKILL.md
# exactly (session and per-model scoped tighten at 75%, the whole weekly allotment at 80%);
# 'wind_down' is a uniform 90%. Unknown kinds default to the tighter 75% bound. Keep these in
# lockstep with the prose -- the posture is meant to REMOVE ambiguity, not add a second source.
FRUGAL_AT = {"session": 75, "weekly_all": 80, "weekly_scoped": 75}
WIND_DOWN_AT = 90

# Snapshots kept for burn-rate math; populated from the cache in main().
HISTORY = []


# --- PROVIDER: Anthropic /api/oauth/usage ---------------------------------------
# A provider exposes two things to the rest of this script:
#   fetch(token) -> (status, data)         : perform the one network request
#   parse_limits(data) -> [normalized limit dict, ...] or None : normalize the raw response
# Everything OUTSIDE this section (posture, burn, cache, render, credentials) must not
# reference the endpoint URL or its raw field names -- swapping providers means
# replacing only this section.

ENDPOINT = "https://api.anthropic.com/api/oauth/usage"

# Fields of a raw limit that we normalize by name; everything else a limit carries is preserved
# under `extra` so new meters (e.g. a usage-credit balance) are surfaced rather than lost.
KNOWN_LIMIT_KEYS = {"kind", "percent", "severity", "is_active", "resets_at", "scope"}


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


def _scope_model(l):
    scope = l.get("scope")
    if isinstance(scope, dict):
        model = scope.get("model")
        if isinstance(model, dict):
            return model.get("display_name")
    return None


def _extra_fields(l):
    """Every field of a raw limit we don't normalize by name, preserved so a new meter (e.g. a
    usage-credit balance) is surfaced rather than dropped -- including nested objects, since a
    balance may well arrive as {"balance": {"remaining": 42, "currency": "usd"}}. Everything here
    came from json.loads, so it is JSON-serializable by construction."""
    extra = {k: v for k, v in l.items() if k not in KNOWN_LIMIT_KEYS}
    return extra or None


def parse_limits(data):
    """Normalize the endpoint's limits into a stable list; None if nothing is recognizable.

    Every dict entry is kept, even one without a `percent` (its percent is None), so an unfamiliar
    limit kind or a non-percent meter still appears in the output. Callers must not read a missing
    percent as 'fine'."""
    limits = data.get("limits") if isinstance(data, dict) else None
    out = []
    if isinstance(limits, list):
        for l in limits:
            if not isinstance(l, dict):
                continue
            resets_at = l.get("resets_at")
            s = seconds_until(resets_at)
            out.append({
                "kind": l.get("kind"),
                "percent": _num_or_none(l.get("percent")),
                "severity": l.get("severity"),
                # assumption, unverified as of 2026-07: appears to mean the currently binding limit
                "is_active": l.get("is_active"),
                "resets_at": resets_at,
                "resets_in_seconds": int(s) if s is not None else None,
                "scope_model": _scope_model(l),
                "scope": l.get("scope"),
                "extra": _extra_fields(l),
            })
    if out:
        return out
    # Fallback: older/simpler shape with just five_hour / seven_day blocks.
    for key, kind in (("five_hour", "session"), ("seven_day", "weekly_all")):
        blk = data.get(key) if isinstance(data, dict) else None
        if isinstance(blk, dict) and "utilization" in blk:
            util = blk.get("utilization")
            # 'utilization' is conventionally a 0-1 fraction; scale to 0-100. Values already above
            # 1 are assumed to be percents and left as-is. Heuristic caveat: a genuine sub-1% value
            # (0.5 meaning 0.5%) would be misread as 50%. This only affects the legacy fallback
            # shape, which the live endpoint does not currently emit.
            if isinstance(util, (int, float)) and 0 <= util <= 1:
                util = util * 100
            resets_at = blk.get("resets_at")
            s = seconds_until(resets_at)
            out.append({
                "kind": kind,
                "percent": _num_or_none(util),
                "severity": None,
                "is_active": None,
                "resets_at": resets_at,
                "resets_in_seconds": int(s) if s is not None else None,
                "scope_model": None,
                "scope": None,
                "extra": None,
            })
    return out or None

# --- end PROVIDER section ---------------------------------------------------


# --------------------------------------------------------------------------- config / flags

def config_dir():
    override = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    return override or os.path.join(os.path.expanduser("~"), ".claude")


CACHE_PATH = os.environ.get("CLAUDE_USAGE_CACHE") or os.path.join(config_dir(), ".usage-cache.json")


def _int_env(name, default):
    try:
        v = int(os.environ.get(name, str(default)))
    except (ValueError, TypeError):
        return default
    # Clamp to >= 0: a negative TTL/backoff has no sane meaning here, so fall back to the
    # default rather than accepting it. Zero stays allowed (e.g. TTL=0 means "always fetch").
    if v < 0:
        return default
    return v


TTL = _int_env("CLAUDE_USAGE_TTL", 300)
BACKOFF = _int_env("CLAUDE_USAGE_429_BACKOFF", 900)

JSON_ONLY = False
FORCE_FRESH = False

USAGE_TEXT = """usage: check-usage.py [--json] [--fresh] [--help] [--version]

  --json      print only the machine-readable JSON line
  --fresh     ignore the cache TTL (still respects 429 backoff)
  -h, --help  show this help message and exit
  --version   show the version number and exit
"""


def parse_args(argv):
    """Parse CLI flags, setting the JSON_ONLY/FORCE_FRESH globals. Called from main() so that
    importing this module never inspects sys.argv or exits (decision 9: testability).

    --help/--version are handled here and exit(0) before any cache/network work; this is the one
    deliberate exception to the "JSON on every exit path" contract -- they are not data queries."""
    global JSON_ONLY, FORCE_FRESH
    for _arg in argv:
        if _arg in ("-h", "--help"):
            print(USAGE_TEXT, end="")
            sys.exit(0)
        elif _arg == "--version":
            print(f"check-usage {VERSION}")
            sys.exit(0)
        elif _arg == "--json":
            JSON_ONLY = True
        elif _arg == "--fresh":
            FORCE_FRESH = True
        else:
            # Honor the "JSON on every exit path" contract even for a bad flag.
            sys.stderr.write(f"unknown flag: {_arg}\n")
            print(json.dumps({"available": False, "reason": f"unknown flag: {_arg}"}))
            sys.exit(2)


# --------------------------------------------------------------------------- credentials

def _keychain_blob():
    """The credentials JSON from the macOS Keychain, or None (non-mac or not found)."""
    if platform.system() != "Darwin":
        return None
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True, text=True, encoding="utf-8", timeout=10,
        )
        if out.returncode == 0 and out.stdout and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def _file_blob():
    """The credentials JSON from <config>/.credentials.json, or None."""
    path = os.path.join(config_dir(), ".credentials.json")
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return None


def get_access_token():
    """Resolve the OAuth access token from (in order) the env override, the macOS Keychain, then
    the credentials file. Returns the token string or None. Never logs the value."""
    env_tok = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
    if env_tok:
        return env_tok
    for raw in (_keychain_blob(), _file_blob()):
        if not raw:
            continue
        try:
            tok = json.loads(raw)["claudeAiOauth"]["accessToken"]
        except (ValueError, KeyError, TypeError):
            continue
        if tok:
            return tok
    return None


# --------------------------------------------------------------------------- cache / network

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
        with open(CACHE_PATH, encoding="utf-8") as f:
            c = json.load(f)
        # Schema version gate: only a cache stamped "v": 1 by this version's save_cache() is
        # honored. Missing (pre-version), older, or newer values are all treated as absent rather
        # than guessed at -- so the first run after upgrading from a pre-version cache simply
        # discards the old cache and makes one extra fetch. That's cheap and safer than reading a
        # shape this version didn't write.
        if isinstance(c, dict) and c.get("v") == 1:
            return c
    except (OSError, ValueError):
        pass
    return {}


def save_cache(cache):
    # Copy so we never mutate the caller's dict, then stamp the schema version so every call site
    # (the success path and the 429 path alike) writes a versioned cache automatically.
    cache = dict(cache)
    cache["v"] = 1
    try:
        cache_dir = os.path.dirname(CACHE_PATH) or "."
        os.makedirs(cache_dir, exist_ok=True)  # e.g. a fresh CLAUDE_CONFIG_DIR
        # Unique temp name in the same dir so two concurrent runs can't clobber each other's write.
        fd, tmp = tempfile.mkstemp(dir=cache_dir, prefix=".usage-cache.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(cache, f)
            os.replace(tmp, CACHE_PATH)  # atomic on POSIX and Windows
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        try:
            os.chmod(CACHE_PATH, 0o600)  # best-effort; a no-op on Windows/NTFS
        except OSError:
            pass
    except OSError:
        pass  # caching is best-effort; never block the report on it


# --------------------------------------------------------------------------- time helpers

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


# --------------------------------------------------------------------------- parsing (general)

def reset_passed(limits, fetched_at):
    """True if any limit reset AFTER the cached data was fetched, meaning the cached percent
    describes a window that has since rolled over.

    The `fetched_at` guard is essential: the endpoint can legitimately return a `resets_at`
    already in the past (an idle limit, server-side lag right after a boundary, or local clock
    skew). Invalidating on those would make every call refetch and spiral into the 429 backoff. We
    only invalidate when the reset moment falls after the fetch -- i.e. we hold pre-reset numbers."""
    for l in (limits or []):
        s = l.get("resets_in_seconds")
        if s is None or s > 0:
            continue
        reset_epoch = NOW + s  # absolute time the reset occurred
        if reset_epoch > fetched_at:
            return True
    return False


# --------------------------------------------------------------------------- burn rate

def limit_key(l):
    """Stable identity for burn/history matching. Uses the scoped model name when present, else a
    serialization of the raw scope, so two same-`kind` limits scoped differently (or two unknown
    kinds both lacking a model) don't collide and cross-contaminate each other's burn baseline."""
    sm = l.get("scope_model")
    if sm:
        return f"{l.get('kind')}|{sm}"
    scope = l.get("scope")
    if scope is not None:
        try:
            return f"{l.get('kind')}|{json.dumps(scope, sort_keys=True)}"
        except (TypeError, ValueError):
            return f"{l.get('kind')}|{scope!r}"
    return f"{l.get('kind')}|"


def compute_burn(limits, data_at):
    """Annotate each limit with a 'burn' delta representing the climb over roughly the last 15+
    minutes, not a session-long average.

    Baseline selection prefers the MOST RECENT same-window snapshot at least BURN_TARGET_WINDOW
    (900s / 15min) older than `data_at`, since a longer baseline gives a more stable rate reading;
    if none qualifies (e.g. the script has only run recently), it falls back to the most recent
    snapshot at least BURN_MIN_ELAPSED (120s) older. This intentionally favors recency over age --
    the opposite of picking the oldest snapshot in the history window, which would report a ~6h
    average instead of a recent climb.

    `data_at` is when the rendered percents were actually measured (which may be earlier than now,
    if served from cache). Measuring elapsed from data_at and only accepting baselines older than
    it stops a reading from being compared to the very snapshot it came from, which would fabricate
    a hard '+0%'. History entries are user-corruptible, so every field is type-checked."""
    usable = [h for h in HISTORY
              if isinstance(h, dict) and isinstance(h.get("limits"), dict)
              and is_num(h.get("at")) and h["at"] < data_at
              and (data_at - h["at"]) <= HISTORY_MAX_AGE]
    usable.sort(key=lambda x: x["at"], reverse=True)  # most recent first
    for l in limits:
        l["burn"] = None
        if not is_num(l.get("percent")):
            continue
        key = limit_key(l)
        cur_reset = l.get("resets_at")
        candidates = []  # (elapsed, delta), most-recent-first, for every snapshot passing guards
        for h in usable:
            elapsed = data_at - h["at"]
            if elapsed < BURN_MIN_ELAPSED:
                continue  # too close in time to be a meaningful baseline
            past = h["limits"].get(key)
            if not is_num(past):
                continue
            # Same-window guard: if the baseline recorded a different reset time, a reset boundary
            # fell between the samples, so the delta understates the true burn (10% -> reset ->
            # 15% would read as a tame +5%). Older caches with no 'resets' map fall back to the
            # negative-delta check below.
            base_reset = (h.get("resets") or {}).get(key)
            if base_reset is not None and cur_reset is not None and base_reset != cur_reset:
                continue
            delta = l["percent"] - past
            if delta < 0:
                continue  # a reset happened between samples; rate not meaningful
            candidates.append((elapsed, delta))
        if not candidates:
            continue
        # Prefer the most recent candidate that's at least 15min old (a stable "recent climb"
        # read); otherwise take the most recent candidate at least 2min old. `candidates` is
        # already most-recent-first because `usable` was sorted that way, so candidates[0] is
        # exactly that fallback.
        elapsed, delta = next((c for c in candidates if c[0] >= BURN_TARGET_WINDOW), candidates[0])
        l["burn"] = {"delta_percent": round(delta, 1), "over_seconds": int(elapsed)}


# --------------------------------------------------------------------------- posture

def _bump(p):
    return LEVELS[min(LEVELS.index(p) + 1, len(LEVELS) - 1)]


def _relax(p):
    return LEVELS[max(LEVELS.index(p) - 1, 0)]


def limit_posture(l):
    pct = l["percent"] if is_num(l.get("percent")) else 0
    frugal_at = FRUGAL_AT.get(l.get("kind"), 75)  # default to the tighter bound
    if pct >= WIND_DOWN_AT:
        p = "wind_down"
    elif pct >= frugal_at:
        p = "frugal"
    else:
        p = "normal"
    # assumption, unverified as of 2026-07: any severity other than 'normal' is treated as escalated
    if l.get("severity") not in (None, "normal"):
        p = _bump(p)
    # Near-reset relief for the short (session) window only: high usage 10 min before reset binds
    # far less than the same usage hours out. Weekly windows, which reset days out, get no relief.
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


# --------------------------------------------------------------------------- render

def label(l):
    if l["kind"] == "session":
        return "Session (5h)"
    if l["kind"] == "weekly_all":
        return "Weekly (all models)"
    if l["kind"] == "weekly_scoped":
        return f"Weekly ({l['scope_model'] or 'scoped'})"
    # Unknown/future kind: show whatever the endpoint called it, plus any scope.
    base = str(l["kind"] or "unknown limit")
    return f"{base} ({l['scope_model']})" if l.get("scope_model") else base


def render(limits, age_secs, stale, source):
    compute_burn(limits, NOW - age_secs)
    posture, driver = overall_posture(limits)
    age_secs = max(0, age_secs)  # a backwards clock jump must never print "fetched -5s ago"
    if not JSON_ONLY:
        age_str = (f"{int(age_secs // 60)}m {int(age_secs % 60)}s ago"
                   if age_secs >= 60 else f"{int(age_secs)}s ago")
        stale_note = " [STALE: showing last known data]" if stale else ""
        print(f"Claude usage (fetched {age_str}, source: {source}){stale_note}")
        for l in limits:
            pct = f"{round(l['percent'])}%" if is_num(l["percent"]) else "?"
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
            # Show `extra` only when there is no percent to show -- i.e. this is a non-percent
            # meter (e.g. a usage-credit balance) that would otherwise render as a bare "?". For
            # ordinary percent limits `extra` stays in the JSON but out of the human line.
            extra = l.get("extra")
            extra_str = ""
            if extra and not is_num(l["percent"]):
                extra_str = "  {" + ", ".join(f"{k}={v}" for k, v in extra.items()) + "}"
            print(f"  {label(l):<24} {pct:>4}{reset_str}{flag_str}{burn_str}{extra_str}")
        if driver and is_num(driver.get("percent")):
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


# --------------------------------------------------------------------------- main

def main():
    global HISTORY
    parse_args(sys.argv[1:])
    cache = load_cache()
    HISTORY = cache.get("history", [])
    if not isinstance(HISTORY, list):
        HISTORY = []
    cached_data = cache.get("data")
    fetched_at = cache.get("fetched_at", 0)
    cache_age = NOW - fetched_at if fetched_at else None
    cached_limits = parse_limits(cached_data) if cached_data else None

    # 1. Fresh cache: serve it, no network call -- unless a limit has passed its reset, in which
    #    case the cached percent is for a window that no longer exists, so we fetch fresh.
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
    token = get_access_token()
    if not token:
        if cached_limits:
            render(cached_limits, cache_age or 0, stale=True, source="cache (no credentials)")
        fail("no OAuth credentials found",
             "set CLAUDE_CODE_OAUTH_TOKEN, or ensure the macOS Keychain item "
             "'Claude Code-credentials' or <CLAUDE_CONFIG_DIR|~/.claude>/.credentials.json exists")

    status, data = fetch(token)

    if status == 200 and data is not None:
        limits = parse_limits(data)
        if limits:
            # Append this snapshot to history so future runs can compute burn rate. 'resets'
            # records each limit's reset time so burn can tell whether a reset boundary fell
            # between two samples (see compute_burn).
            snapshot = {
                "at": NOW,
                "limits": {limit_key(l): l["percent"]
                           for l in limits if is_num(l["percent"])},
                "resets": {limit_key(l): l.get("resets_at")
                           for l in limits if is_num(l["percent"])},
            }
            HISTORY = [h for h in HISTORY if isinstance(h, dict)
                       and is_num(h.get("at")) and (NOW - h["at"]) <= HISTORY_MAX_AGE]
            HISTORY.append(snapshot)
            HISTORY = HISTORY[-HISTORY_MAX:]
            save_cache({"fetched_at": NOW, "data": data, "last_429_at": 0, "history": HISTORY})
            render(limits, 0, stale=False, source="network")
        # 200 but unrecognizable shape: the private API may have changed.
        if cached_limits:
            render(cached_limits, cache_age or 0, stale=True, source="cache (response shape changed)")
        fail("endpoint returned 200 but the response shape was not recognized",
             "the private usage endpoint's response format may have changed")

    if status == 429:
        # Re-load from disk rather than mutating the `cache` dict loaded at the start of main():
        # another process may have completed a successful fetch (newer `data`/`history`) since we
        # loaded, and a naive read-modify-write here would clobber it.
        fresh = load_cache()
        fresh["last_429_at"] = NOW
        save_cache(fresh)
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


if __name__ == "__main__":
    main()
