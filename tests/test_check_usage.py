"""Unit tests for scripts/check-usage.py.

Stdlib unittest only (preserves the script's zero-dependency selling point). The script's
filename is hyphenated so it cannot be `import`ed normally; it is loaded per-test via
importlib.util.spec_from_file_location, always with CLAUDE_USAGE_CACHE / CLAUDE_CONFIG_DIR
pointed at a throwaway tempdir so nothing ever touches the real ~/.claude. `fetch` (the only
network call) is monkeypatched wherever a code path could reach it; the real endpoint is never
contacted.

Because the module captures NOW = time.time() at import time, tests that fabricate history
timestamps derive them from the freshly loaded module's `mod.NOW`, never from a fresh
time.time() call.
"""

import contextlib
import importlib.util
import io
import itertools
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

SCRIPT_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "scripts", "check-usage.py")
)

_module_counter = itertools.count()


def load_module(env=None):
    """Load a fresh instance of check-usage.py with the given environment overrides applied
    for the duration of the import (module-level code reads CLAUDE_USAGE_CACHE, CLAUDE_CONFIG_DIR,
    CLAUDE_USAGE_TTL, and CLAUDE_USAGE_429_BACKOFF at import time, so the env must be set before
    exec_module runs). Each call gets a unique module name so Python never returns a cached copy
    and each test gets independent module-level globals (JSON_ONLY, FORCE_FRESH, HISTORY, ...)."""
    env = env or {}
    saved = {}
    for k, v in env.items():
        saved[k] = os.environ.get(k)
        os.environ[k] = v
    try:
        name = f"check_usage_under_test_{next(_module_counter)}"
        spec = importlib.util.spec_from_file_location(name, SCRIPT_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        for k, old in saved.items():
            if old is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old


class CheckUsageTestCase(unittest.TestCase):
    """Base class: gives every test its own tempdir-scoped module instance so tests never share
    mutable module globals (HISTORY, JSON_ONLY, FORCE_FRESH) and never write outside a tempdir."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmpdir = self._td.name
        self.cache_path = os.path.join(self.tmpdir, ".usage-cache.json")
        self.mod = load_module({
            "CLAUDE_USAGE_CACHE": self.cache_path,
            "CLAUDE_CONFIG_DIR": self.tmpdir,
        })

    def run_with_exit(self, fn, *args, **kwargs):
        """Call fn(*args, **kwargs), which is expected to sys.exit(); return (exit_code, stdout)."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with self.assertRaises(SystemExit) as cm:
                fn(*args, **kwargs)
        return cm.exception.code, buf.getvalue()


# --------------------------------------------------------------------------- parse_limits

class TestParseLimits(CheckUsageTestCase):
    def test_modern_limits_shape(self):
        data = {"limits": [
            {"kind": "session", "percent": 42, "severity": "normal", "is_active": True,
             "resets_at": "2099-01-01T00:00:00Z", "scope": None},
        ]}
        out = self.mod.parse_limits(data)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["kind"], "session")
        self.assertEqual(out[0]["percent"], 42)
        self.assertEqual(out[0]["severity"], "normal")
        self.assertTrue(out[0]["is_active"])

    def test_legacy_five_hour_seven_day_fallback(self):
        data = {
            "five_hour": {"utilization": 0.5, "resets_at": "2099-01-01T00:00:00Z"},
            "seven_day": {"utilization": 30, "resets_at": "2099-01-01T00:00:00Z"},
        }
        out = self.mod.parse_limits(data)
        kinds = {l["kind"]: l["percent"] for l in out}
        self.assertEqual(kinds["session"], 50.0)  # 0.5 -> scaled to 50%
        self.assertEqual(kinds["weekly_all"], 30)  # already > 1, left as-is

    def test_empty_and_garbage_input(self):
        self.assertIsNone(self.mod.parse_limits({}))
        self.assertIsNone(self.mod.parse_limits(None))
        self.assertIsNone(self.mod.parse_limits({"limits": "garbage"}))
        self.assertIsNone(self.mod.parse_limits({"limits": []}))
        self.assertIsNone(self.mod.parse_limits({"limits": [1, "x", None]}))

    def test_nan_infinity_percent_normalized_to_none(self):
        data = {"limits": [
            {"kind": "session", "percent": float("nan")},
            {"kind": "weekly_all", "percent": float("inf")},
            {"kind": "weekly_scoped", "percent": float("-inf")},
        ]}
        out = self.mod.parse_limits(data)
        self.assertTrue(all(l["percent"] is None for l in out))

    def test_unknown_kind_and_percentless_meter_kept(self):
        data = {"limits": [
            {"kind": "usage_credit", "percent": None},
            {"kind": "some_future_kind", "percent": 10},
        ]}
        out = self.mod.parse_limits(data)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["kind"], "usage_credit")
        self.assertIsNone(out[0]["percent"])
        self.assertEqual(out[1]["kind"], "some_future_kind")

    def test_extra_passthrough_including_nested_objects(self):
        data = {"limits": [
            {"kind": "usage_credit", "balance": {"remaining": 42, "currency": "usd"},
             "note": "beta"},
        ]}
        out = self.mod.parse_limits(data)
        extra = out[0]["extra"]
        self.assertEqual(extra["balance"], {"remaining": 42, "currency": "usd"})
        self.assertEqual(extra["note"], "beta")
        # Known keys must not leak into extra.
        self.assertNotIn("kind", extra)

    def test_extra_none_when_only_known_keys_present(self):
        data = {"limits": [{"kind": "session", "percent": 1}]}
        out = self.mod.parse_limits(data)
        self.assertIsNone(out[0]["extra"])


# --------------------------------------------------------------------------- posture

class TestLimitPosture(CheckUsageTestCase):
    def _limit(self, **overrides):
        base = {"kind": "session", "percent": 0, "severity": None, "resets_in_seconds": None}
        base.update(overrides)
        return base

    def test_threshold_boundaries_session(self):
        # session/weekly_scoped frugal_at = 75, wind_down = 90
        self.assertEqual(self.mod.limit_posture(self._limit(percent=74.9)), "normal")
        self.assertEqual(self.mod.limit_posture(self._limit(percent=75)), "frugal")
        self.assertEqual(self.mod.limit_posture(self._limit(percent=80)), "frugal")
        self.assertEqual(self.mod.limit_posture(self._limit(percent=89.9)), "frugal")
        self.assertEqual(self.mod.limit_posture(self._limit(percent=90)), "wind_down")

    def test_threshold_boundaries_weekly_all(self):
        # weekly_all frugal_at = 80
        self.assertEqual(self.mod.limit_posture(self._limit(kind="weekly_all", percent=79.9)),
                          "normal")
        self.assertEqual(self.mod.limit_posture(self._limit(kind="weekly_all", percent=80)),
                          "frugal")

    def test_severity_bump(self):
        normal = self._limit(percent=10, severity=None)
        bumped = self._limit(percent=10, severity="warning")
        self.assertEqual(self.mod.limit_posture(normal), "normal")
        self.assertEqual(self.mod.limit_posture(bumped), "frugal")

    def test_session_near_reset_relief(self):
        # wind_down posture (95%), resets in 500s (0 < 500 < 900) -> relaxed one level.
        near = self._limit(kind="session", percent=95, resets_in_seconds=500)
        self.assertEqual(self.mod.limit_posture(near), "frugal")
        # Same percent but reset far out (>=900s) -> no relief.
        far = self._limit(kind="session", percent=95, resets_in_seconds=1000)
        self.assertEqual(self.mod.limit_posture(far), "wind_down")
        # Relief applies only to kind == "session"; weekly_all gets none.
        weekly = self._limit(kind="weekly_all", percent=95, resets_in_seconds=500)
        self.assertEqual(self.mod.limit_posture(weekly), "wind_down")
        # resets_in_seconds == 0 or negative does not qualify (must be strictly > 0).
        elapsed = self._limit(kind="session", percent=95, resets_in_seconds=0)
        self.assertEqual(self.mod.limit_posture(elapsed), "wind_down")

    def test_unknown_kind_default_frugal_threshold(self):
        unknown = self._limit(kind="mystery_meter", percent=75)
        self.assertEqual(self.mod.limit_posture(unknown), "frugal")
        below = self._limit(kind="mystery_meter", percent=74)
        self.assertEqual(self.mod.limit_posture(below), "normal")

    def test_percentless_meter_with_elevated_severity_drives_frugal(self):
        # percent is None -> treated as 0 for threshold math, but an elevated severity still
        # bumps posture (decision 4: code behavior kept, only the docs prose changed).
        l = self._limit(percent=None, severity="warning")
        self.assertEqual(self.mod.limit_posture(l), "frugal")


class TestOverallPosture(CheckUsageTestCase):
    def test_worst_wins(self):
        limits = [
            {"kind": "session", "percent": 10, "severity": None, "resets_in_seconds": None},
            {"kind": "weekly_all", "percent": 95, "severity": None, "resets_in_seconds": None},
            {"kind": "weekly_scoped", "percent": 76, "severity": None, "resets_in_seconds": None},
        ]
        posture, driver = self.mod.overall_posture(limits)
        self.assertEqual(posture, "wind_down")
        self.assertIs(driver, limits[1])

    def test_all_normal(self):
        limits = [
            {"kind": "session", "percent": 1, "severity": None, "resets_in_seconds": None},
        ]
        posture, driver = self.mod.overall_posture(limits)
        self.assertEqual(posture, "normal")
        self.assertIsNone(driver)


# --------------------------------------------------------------------------- compute_burn

class TestComputeBurn(CheckUsageTestCase):
    def test_prefers_most_recent_baseline_at_least_900s_old(self):
        mod = self.mod
        limits = [{"kind": "session", "percent": 60.0, "resets_at": "R"}]
        mod.HISTORY = [
            {"at": mod.NOW - 1000, "limits": {"session|": 40.0}, "resets": {"session|": "R"}},
            {"at": mod.NOW - 940, "limits": {"session|": 45.0}, "resets": {"session|": "R"}},
            {"at": mod.NOW - 100, "limits": {"session|": 58.0}, "resets": {"session|": "R"}},
        ]
        mod.compute_burn(limits, mod.NOW)
        burn = limits[0]["burn"]
        self.assertIsNotNone(burn)
        self.assertEqual(burn["delta_percent"], 15.0)
        self.assertEqual(burn["over_seconds"], 940)

    def test_falls_back_to_most_recent_at_least_120s_old(self):
        mod = self.mod
        limits = [{"kind": "session", "percent": 60.0, "resets_at": "R"}]
        mod.HISTORY = [
            {"at": mod.NOW - 200, "limits": {"session|": 50.0}, "resets": {"session|": "R"}},
            {"at": mod.NOW - 150, "limits": {"session|": 55.0}, "resets": {"session|": "R"}},
        ]
        mod.compute_burn(limits, mod.NOW)
        burn = limits[0]["burn"]
        self.assertIsNotNone(burn)
        self.assertEqual(burn["delta_percent"], 5.0)
        self.assertEqual(burn["over_seconds"], 150)

    def test_skips_the_same_snapshot_the_reading_came_from(self):
        mod = self.mod
        limits = [{"kind": "session", "percent": 60.0, "resets_at": "R"}]
        mod.HISTORY = [
            {"at": mod.NOW, "limits": {"session|": 60.0}, "resets": {"session|": "R"}},
        ]
        mod.compute_burn(limits, mod.NOW)
        self.assertIsNone(limits[0]["burn"])

    def test_same_window_guard_skips_mismatched_resets_at(self):
        mod = self.mod
        limits = [{"kind": "session", "percent": 60.0, "resets_at": "R2"}]
        mod.HISTORY = [
            {"at": mod.NOW - 1000, "limits": {"session|": 40.0}, "resets": {"session|": "R1"}},
        ]
        mod.compute_burn(limits, mod.NOW)
        self.assertIsNone(limits[0]["burn"])

    def test_negative_delta_skipped(self):
        mod = self.mod
        limits = [{"kind": "session", "percent": 30.0, "resets_at": "R"}]
        mod.HISTORY = [
            {"at": mod.NOW - 1000, "limits": {"session|": 80.0}, "resets": {"session|": "R"}},
        ]
        mod.compute_burn(limits, mod.NOW)
        self.assertIsNone(limits[0]["burn"])

    def test_corrupt_history_entries_ignored(self):
        mod = self.mod
        limits = [{"kind": "session", "percent": 60.0, "resets_at": "R"}]
        mod.HISTORY = [
            None,
            "not a dict",
            {"limits": {"session|": 10.0}},                      # missing 'at'
            {"at": "bad", "limits": {"session|": 10.0}},          # non-numeric 'at'
            {"at": mod.NOW - 950, "limits": "not a dict"},        # limits not a dict
            {"at": mod.NOW - 950, "limits": {"session|": 45.0}, "resets": {"session|": "R"}},
        ]
        mod.compute_burn(limits, mod.NOW)
        burn = limits[0]["burn"]
        self.assertIsNotNone(burn)
        self.assertEqual(burn["delta_percent"], 15.0)

    def test_no_usable_baseline_burn_is_null(self):
        mod = self.mod
        limits = [{"kind": "session", "percent": 60.0, "resets_at": "R"}]
        mod.HISTORY = []
        mod.compute_burn(limits, mod.NOW)
        self.assertIsNone(limits[0]["burn"])

    def test_percentless_limit_skipped_entirely(self):
        mod = self.mod
        limits = [{"kind": "usage_credit", "percent": None, "resets_at": None}]
        mod.HISTORY = [
            {"at": mod.NOW - 1000, "limits": {"usage_credit|": 40.0}, "resets": {}},
        ]
        mod.compute_burn(limits, mod.NOW)
        self.assertIsNone(limits[0]["burn"])


# --------------------------------------------------------------------------- reset_passed

class TestResetPassed(CheckUsageTestCase):
    def test_reset_after_fetched_at_is_true(self):
        mod = self.mod
        fetched_at = mod.NOW - 100
        # resets_in_seconds negative -> reset_epoch = NOW + s; make it land after fetched_at.
        limits = [{"resets_in_seconds": -10}]
        self.assertTrue(mod.reset_passed(limits, fetched_at))

    def test_reset_before_fetched_at_is_false(self):
        mod = self.mod
        fetched_at = mod.NOW  # fetched just now, after an already-idle reset
        limits = [{"resets_in_seconds": -10}]
        self.assertFalse(mod.reset_passed(limits, fetched_at))

    def test_future_reset_ignored(self):
        mod = self.mod
        limits = [{"resets_in_seconds": 500}]
        self.assertFalse(mod.reset_passed(limits, mod.NOW - 1000))

    def test_missing_resets_in_seconds_ignored(self):
        mod = self.mod
        limits = [{"resets_in_seconds": None}]
        self.assertFalse(mod.reset_passed(limits, mod.NOW - 1000))


# --------------------------------------------------------------------------- _int_env

class TestIntEnv(CheckUsageTestCase):
    def test_non_int_falls_back_to_default(self):
        os.environ["CU_TEST_VAR"] = "not-an-int"
        try:
            self.assertEqual(self.mod._int_env("CU_TEST_VAR", 5), 5)
        finally:
            del os.environ["CU_TEST_VAR"]

    def test_negative_falls_back_to_default(self):
        os.environ["CU_TEST_VAR"] = "-1"
        try:
            self.assertEqual(self.mod._int_env("CU_TEST_VAR", 5), 5)
        finally:
            del os.environ["CU_TEST_VAR"]

    def test_zero_is_allowed(self):
        os.environ["CU_TEST_VAR"] = "0"
        try:
            self.assertEqual(self.mod._int_env("CU_TEST_VAR", 5), 0)
        finally:
            del os.environ["CU_TEST_VAR"]

    def test_missing_env_uses_default(self):
        os.environ.pop("CU_TEST_VAR", None)
        self.assertEqual(self.mod._int_env("CU_TEST_VAR", 42), 42)


# --------------------------------------------------------------------------- parse_args

class TestParseArgs(CheckUsageTestCase):
    def test_unknown_flag_exits_2_with_json_line(self):
        code, out = self.run_with_exit(self.mod.parse_args, ["--bogus"])
        self.assertEqual(code, 2)
        payload = json.loads(out.strip().splitlines()[-1])
        self.assertFalse(payload["available"])
        self.assertIn("--bogus", payload["reason"])

    def test_json_flag_sets_json_only_but_not_force_fresh(self):
        self.mod.parse_args(["--json"])
        self.assertTrue(self.mod.JSON_ONLY)
        self.assertFalse(self.mod.FORCE_FRESH)

    def test_fresh_flag_sets_force_fresh_but_not_json_only(self):
        self.mod.parse_args(["--fresh"])
        self.assertTrue(self.mod.FORCE_FRESH)
        self.assertFalse(self.mod.JSON_ONLY)

    def test_help_long_exits_0_prints_usage_no_json(self):
        code, out = self.run_with_exit(self.mod.parse_args, ["--help"])
        self.assertEqual(code, 0)
        self.assertEqual(out, self.mod.USAGE_TEXT)
        self.assertNotIn('"available"', out)

    def test_help_short_exits_0_prints_usage_no_json(self):
        code, out = self.run_with_exit(self.mod.parse_args, ["-h"])
        self.assertEqual(code, 0)
        self.assertEqual(out, self.mod.USAGE_TEXT)
        self.assertNotIn('"available"', out)

    def test_help_touches_no_disk_or_network(self):
        # No cache file should exist before or after --help.
        self.assertFalse(os.path.exists(self.cache_path))
        self.run_with_exit(self.mod.parse_args, ["--help"])
        self.assertFalse(os.path.exists(self.cache_path))

    def test_version_exits_0_prints_version_no_json(self):
        code, out = self.run_with_exit(self.mod.parse_args, ["--version"])
        self.assertEqual(code, 0)
        self.assertEqual(out, f"check-usage {self.mod.VERSION}\n")
        self.assertNotIn('"available"', out)


# --------------------------------------------------------------------------- time helpers

class TestTimeHelpers(CheckUsageTestCase):
    def test_seconds_until_z_suffix(self):
        target = datetime.now(timezone.utc) + timedelta(seconds=100)
        iso = target.strftime("%Y-%m-%dT%H:%M:%S") + "Z"
        secs = self.mod.seconds_until(iso)
        self.assertIsNotNone(secs)
        self.assertAlmostEqual(secs, 100, delta=5)

    def test_seconds_until_offset_suffix(self):
        target = datetime.now(timezone.utc) + timedelta(seconds=200)
        iso = target.strftime("%Y-%m-%dT%H:%M:%S") + "+00:00"
        secs = self.mod.seconds_until(iso)
        self.assertIsNotNone(secs)
        self.assertAlmostEqual(secs, 200, delta=5)

    def test_seconds_until_garbage_returns_none(self):
        self.assertIsNone(self.mod.seconds_until("not-a-date"))
        self.assertIsNone(self.mod.seconds_until(None))

    def test_rel_time_garbage_returns_empty_string(self):
        self.assertEqual(self.mod.rel_time("not-a-date"), "")
        self.assertEqual(self.mod.rel_time(None), "")

    def test_short_dur_minutes_and_hours(self):
        self.assertEqual(self.mod.short_dur(45 * 60), "45m")
        self.assertEqual(self.mod.short_dur(3 * 3600 + 5 * 60), "3h05m")


# --------------------------------------------------------------------------- age clamp

class TestAgeClamp(CheckUsageTestCase):
    def test_negative_age_renders_as_zero(self):
        code, out = self.run_with_exit(self.mod.render, [], -5, False, "cache")
        self.assertEqual(code, 0)
        self.assertIn("0s ago", out)
        json_line = out.strip().splitlines()[-1]
        payload = json.loads(json_line)
        self.assertEqual(payload["age_seconds"], 0)


# --------------------------------------------------------------------------- 429 path

class Test429ClobberPreservation(CheckUsageTestCase):
    def test_429_path_preserves_concurrently_written_newer_cache(self):
        mod = self.mod

        old_cache = {
            "v": 1,
            "fetched_at": mod.NOW - 10000,  # older than TTL, forces main() past the cache-fresh path
            "data": {"limits": [{"kind": "session", "percent": 10}]},
            "last_429_at": 0,
            "history": [{"at": mod.NOW - 5000, "limits": {"session|": 10}, "resets": {}}],
        }
        new_cache = {
            "v": 1,
            "fetched_at": mod.NOW - 50,
            "data": {"limits": [{"kind": "session", "percent": 99}]},
            "last_429_at": 0,
            "history": [{"at": mod.NOW - 40, "limits": {"session|": 99}, "resets": {}}],
        }

        # main() calls load_cache() twice: once at the top, once inside the 429 branch (by
        # design, to avoid clobbering a concurrent writer). Simulate another process completing
        # a successful fetch in between by returning old_cache on the first call and new_cache
        # (the "concurrently written" newer state) on the second.
        calls = {"n": 0}

        def fake_load_cache():
            calls["n"] += 1
            return dict(old_cache) if calls["n"] == 1 else dict(new_cache)

        mod.load_cache = fake_load_cache
        mod.get_access_token = lambda: "dummy-token"
        mod.fetch = lambda token: (429, None)

        # main() parses sys.argv itself; the real argv here is whatever invoked this test run
        # (e.g. "-m unittest discover tests"), so pin it to a benign value for the duration.
        old_argv = sys.argv[:]
        sys.argv = ["check-usage.py"]
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                with self.assertRaises(SystemExit):
                    mod.main()
        finally:
            sys.argv = old_argv

        self.assertEqual(calls["n"], 2)
        with open(self.cache_path, encoding="utf-8") as f:
            written = json.load(f)

        # The newer data/history from the "concurrent" write must survive; only last_429_at
        # should have been overwritten by this run.
        self.assertEqual(written["data"], new_cache["data"])
        self.assertEqual(written["history"], new_cache["history"])
        self.assertEqual(written["fetched_at"], new_cache["fetched_at"])
        self.assertEqual(written["last_429_at"], mod.NOW)


# --------------------------------------------------------------------------- cache versioning

class TestCacheVersioning(CheckUsageTestCase):
    def test_unversioned_cache_treated_as_empty(self):
        with open(self.cache_path, "w", encoding="utf-8") as f:
            json.dump({"fetched_at": self.mod.NOW, "data": {"limits": []}}, f)
        self.assertEqual(self.mod.load_cache(), {})

    def test_v1_cache_is_honored(self):
        cache = {"v": 1, "fetched_at": self.mod.NOW, "data": {"limits": []}}
        with open(self.cache_path, "w", encoding="utf-8") as f:
            json.dump(cache, f)
        self.assertEqual(self.mod.load_cache(), cache)

    def test_wrong_version_treated_as_empty(self):
        for bad_v in (0, 2):
            with open(self.cache_path, "w", encoding="utf-8") as f:
                json.dump({"v": bad_v, "fetched_at": self.mod.NOW, "data": {"limits": []}}, f)
            self.assertEqual(self.mod.load_cache(), {})

    def test_saved_cache_carries_v1_on_disk(self):
        self.mod.save_cache({"fetched_at": self.mod.NOW, "data": {"limits": []}})
        with open(self.cache_path, encoding="utf-8") as f:
            on_disk = json.load(f)
        self.assertEqual(on_disk["v"], 1)

    def test_save_cache_does_not_mutate_caller_dict(self):
        original = {"fetched_at": self.mod.NOW, "data": {"limits": []}}
        self.mod.save_cache(original)
        self.assertNotIn("v", original)

    def test_history_round_trips_through_versioned_cache(self):
        history = [{"at": self.mod.NOW - 1000, "limits": {"session|": 40.0}, "resets": {}}]
        self.mod.save_cache({
            "fetched_at": self.mod.NOW,
            "data": {"limits": []},
            "history": history,
        })
        loaded = self.mod.load_cache()
        self.assertEqual(loaded["v"], 1)
        self.assertEqual(loaded["history"], history)


# --------------------------------------------------------------------------- import side effects

class TestImportSideEffects(CheckUsageTestCase):
    def test_import_does_not_inspect_argv_or_exit(self):
        # A bogus sys.argv (as unittest's own argv often is) must not affect module import.
        old_argv = sys.argv[:]
        sys.argv = ["check-usage.py", "--totally-bogus-flag"]
        try:
            mod = load_module({
                "CLAUDE_USAGE_CACHE": os.path.join(self.tmpdir, "other-cache.json"),
                "CLAUDE_CONFIG_DIR": self.tmpdir,
            })
            self.assertFalse(mod.JSON_ONLY)
            self.assertFalse(mod.FORCE_FRESH)
        finally:
            sys.argv = old_argv


if __name__ == "__main__":
    unittest.main()
