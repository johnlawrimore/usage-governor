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

    def test_percent_band_boundaries_session(self):
        # default bands: >=50 measured, >=75 frugal, >=90 conserve, >=97 wind_down
        self.assertEqual(self.mod.limit_posture(self._limit(percent=49.9)), "normal")
        self.assertEqual(self.mod.limit_posture(self._limit(percent=50)), "measured")
        self.assertEqual(self.mod.limit_posture(self._limit(percent=74.9)), "measured")
        self.assertEqual(self.mod.limit_posture(self._limit(percent=75)), "frugal")
        self.assertEqual(self.mod.limit_posture(self._limit(percent=89.9)), "frugal")
        self.assertEqual(self.mod.limit_posture(self._limit(percent=90)), "conserve")
        self.assertEqual(self.mod.limit_posture(self._limit(percent=96.9)), "conserve")
        self.assertEqual(self.mod.limit_posture(self._limit(percent=97)), "wind_down")

    def test_percent_band_boundaries_weekly_all(self):
        # weekly_all bands sit higher at the low end: >=55 measured, >=80 frugal,
        # >=90 conserve, >=95 wind_down
        w = lambda p: self.mod.limit_posture(self._limit(kind="weekly_all", percent=p))
        self.assertEqual(w(54.9), "normal")
        self.assertEqual(w(55), "measured")
        self.assertEqual(w(79.9), "measured")
        self.assertEqual(w(80), "frugal")
        self.assertEqual(w(90), "conserve")
        self.assertEqual(w(95), "wind_down")

    def test_severity_bump(self):
        # percent 10 -> normal on both axes; an elevated severity bumps one rung -> measured.
        normal = self._limit(percent=10, severity=None)
        bumped = self._limit(percent=10, severity="warning")
        self.assertEqual(self.mod.limit_posture(normal), "normal")
        self.assertEqual(self.mod.limit_posture(bumped), "measured")

    def test_session_near_reset_relief(self):
        # 95% -> conserve; resets in 500s (0 < 500 < 900) -> relaxed one level to frugal.
        near = self._limit(kind="session", percent=95, resets_in_seconds=500)
        self.assertEqual(self.mod.limit_posture(near), "frugal")
        # Same percent but reset far out (>=900s) -> no relief.
        far = self._limit(kind="session", percent=95, resets_in_seconds=1000)
        self.assertEqual(self.mod.limit_posture(far), "conserve")
        # Relief applies only to kind == "session"; weekly_all gets none (95% -> wind_down).
        weekly = self._limit(kind="weekly_all", percent=95, resets_in_seconds=500)
        self.assertEqual(self.mod.limit_posture(weekly), "wind_down")
        # resets_in_seconds == 0 or negative does not qualify (must be strictly > 0).
        elapsed = self._limit(kind="session", percent=95, resets_in_seconds=0)
        self.assertEqual(self.mod.limit_posture(elapsed), "conserve")

    def test_unknown_kind_uses_default_bands(self):
        unknown = self._limit(kind="mystery_meter", percent=75)
        self.assertEqual(self.mod.limit_posture(unknown), "frugal")
        mid = self._limit(kind="mystery_meter", percent=60)
        self.assertEqual(self.mod.limit_posture(mid), "measured")
        below = self._limit(kind="mystery_meter", percent=49)
        self.assertEqual(self.mod.limit_posture(below), "normal")

    def test_percentless_meter_with_elevated_severity_escalates(self):
        # percent is None -> treated as 0 for band math (normal), but an elevated severity still
        # bumps posture one rung -> measured.
        l = self._limit(percent=None, severity="warning")
        self.assertEqual(self.mod.limit_posture(l), "measured")

    def test_low_percent_high_pace_escalates(self):
        # The screenshot case: only 20% used, but burning ~1.5x sustainable -> frugal, not normal.
        # reset 15000s, burn +8% / 1000s -> exhaust in 10000s, ratio 15000/10000 = 1.5.
        l = self._limit(kind="session", percent=20, resets_in_seconds=15000,
                        burn={"delta_percent": 8.0, "over_seconds": 1000})
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


# --------------------------------------------------------------------------- pace

class TestPaceInfo(CheckUsageTestCase):
    def _limit(self, **overrides):
        base = {"kind": "session", "percent": 20, "resets_in_seconds": 10000,
                "burn": {"delta_percent": 8.0, "over_seconds": 1000}}
        base.update(overrides)
        return base

    def test_ratio_and_exhaust_math(self):
        # remaining 80%, rate 8%/1000s = 0.008%/s -> exhaust 10000s; sustainable 80/10000 =
        # 0.008%/s -> ratio exactly 1.0 (lands on empty right at reset).
        ratio, exhaust = self.mod.pace_info(self._limit())
        self.assertAlmostEqual(ratio, 1.0, places=6)
        self.assertAlmostEqual(exhaust, 10000.0, places=6)

    def test_ratio_above_one_when_burning_too_fast(self):
        # double the burn -> exhaust in 5000s, ratio 10000/5000 = 2.0.
        ratio, exhaust = self.mod.pace_info(
            self._limit(burn={"delta_percent": 16.0, "over_seconds": 1000}))
        self.assertAlmostEqual(ratio, 2.0, places=6)
        self.assertAlmostEqual(exhaust, 5000.0, places=6)

    def test_ratio_identity_reset_over_exhaust(self):
        # ratio must equal seconds_to_reset / seconds_to_exhaustion for arbitrary inputs.
        l = self._limit(percent=37, resets_in_seconds=7200,
                        burn={"delta_percent": 9.0, "over_seconds": 640})
        ratio, exhaust = self.mod.pace_info(l)
        self.assertAlmostEqual(ratio, 7200 / exhaust, places=6)

    def test_small_burn_delta_ignored_as_noise(self):
        # below PACE_MIN_BURN_DELTA (2.0) the extrapolated rate is too noisy to steer on.
        ratio, exhaust = self.mod.pace_info(
            self._limit(burn={"delta_percent": 1.5, "over_seconds": 1000}))
        self.assertIsNone(ratio)
        self.assertIsNone(exhaust)

    def test_no_burn_baseline(self):
        self.assertEqual(self.mod.pace_info(self._limit(burn=None)), (None, None))

    def test_reset_in_past_or_unknown(self):
        self.assertEqual(self.mod.pace_info(self._limit(resets_in_seconds=0)), (None, None))
        self.assertEqual(self.mod.pace_info(self._limit(resets_in_seconds=-5)), (None, None))
        self.assertEqual(self.mod.pace_info(self._limit(resets_in_seconds=None)), (None, None))

    def test_already_full_yields_no_pace(self):
        self.assertEqual(self.mod.pace_info(self._limit(percent=100)), (None, None))

    def test_reset_beyond_horizon_yields_no_pace(self):
        # A weekly window days from reset: a ~15-min burst must not extrapolate across it.
        self.assertEqual(self.mod.pace_info(self._limit(resets_in_seconds=259200)), (None, None))

    def test_horizon_boundary(self):
        # At exactly the horizon pace is still judged; one second past it is not.
        at = self._limit(resets_in_seconds=self.mod.PACE_MAX_HORIZON_S,
                         burn={"delta_percent": 40.0, "over_seconds": 1000})
        self.assertIsNotNone(self.mod.pace_info(at)[0])
        past = self._limit(resets_in_seconds=self.mod.PACE_MAX_HORIZON_S + 1,
                           burn={"delta_percent": 40.0, "over_seconds": 1000})
        self.assertEqual(self.mod.pace_info(past), (None, None))

    def test_percentless_meter_yields_no_pace(self):
        self.assertEqual(self.mod.pace_info(self._limit(percent=None)), (None, None))

    def test_malformed_burn_fields_ignored(self):
        self.assertEqual(
            self.mod.pace_info(self._limit(burn={"delta_percent": "x", "over_seconds": 1000})),
            (None, None))
        self.assertEqual(
            self.mod.pace_info(self._limit(burn={"delta_percent": 8.0, "over_seconds": 0})),
            (None, None))


class TestPacePosture(CheckUsageTestCase):
    def _limit(self, ratio_target, percent=20, exhaust=None):
        """Build a limit whose pace_info yields approximately ratio_target. With remaining and a
        chosen reset, pick a burn rate so exhaust = reset / ratio_target."""
        remaining = 100 - percent
        reset_s = 10000
        exhaust_s = exhaust if exhaust is not None else reset_s / ratio_target
        rate = remaining / exhaust_s          # %/s needed to hit that exhaust
        return {"kind": "session", "percent": percent, "resets_in_seconds": reset_s,
                "burn": {"delta_percent": rate * 1000, "over_seconds": 1000}}

    def test_sustainable_pace_is_normal(self):
        self.assertEqual(self.mod.pace_posture(self._limit(0.5)), "normal")

    def test_band_rungs(self):
        self.assertEqual(self.mod.pace_posture(self._limit(1.1)), "measured")
        self.assertEqual(self.mod.pace_posture(self._limit(1.5)), "frugal")
        self.assertEqual(self.mod.pace_posture(self._limit(3.0)), "conserve")
        self.assertEqual(self.mod.pace_posture(self._limit(5.0)), "wind_down")

    def test_no_burn_is_normal(self):
        l = {"kind": "session", "percent": 20, "resets_in_seconds": 10000, "burn": None}
        self.assertEqual(self.mod.pace_posture(l), "normal")

    def test_imminent_exhaustion_floors_at_conserve(self):
        # Ratio only 1.1 (would be 'measured'), but the window empties in 1500s (< 1800s backstop)
        # -> floored to conserve. reset 1650s so ratio ~= 1.1.
        l = {"kind": "session", "percent": 90, "resets_in_seconds": 1650,
             "burn": {"delta_percent": (10 / 1500) * 1000, "over_seconds": 1000}}
        ratio, exhaust = self.mod.pace_info(l)
        self.assertLess(ratio, 1.25)          # would land in 'measured' on ratio alone
        self.assertLessEqual(exhaust, self.mod.PACE_IMMINENT_EXHAUST_S)
        self.assertEqual(self.mod.pace_posture(l), "conserve")

    def test_weekly_within_horizon_reaches_middle_rung(self):
        # Regression for the binary-pace bug: a weekly limit near its reset must be able to land on
        # a middle rung, not jump straight to wind_down. 85%, 3h to reset, +2%/900s -> exhaust
        # ~6750s, ratio ~1.6 -> frugal.
        l = {"kind": "weekly_all", "percent": 85, "resets_in_seconds": 10800,
             "burn": {"delta_percent": 2.0, "over_seconds": 900}}
        self.assertEqual(self.mod.pace_posture(l), "frugal")

    def test_weekly_beyond_horizon_pace_is_normal(self):
        # Same fast burn but reset is days out: a 15-min burst must not extrapolate -> pace normal,
        # so a heavy session no longer slams the weekly limit to wind_down.
        l = {"kind": "weekly_all", "percent": 40, "resets_in_seconds": 259200,
             "burn": {"delta_percent": 3.0, "over_seconds": 900}}
        self.assertEqual(self.mod.pace_posture(l), "normal")

    def test_imminent_backstop_does_not_fire_when_sustainable(self):
        # Tiny exhaust window but reset is even sooner (ratio < 1) -> pace is normal, backstop off.
        l = {"kind": "session", "percent": 90, "resets_in_seconds": 500,
             "burn": {"delta_percent": (10 / 1000) * 1000, "over_seconds": 1000}}
        ratio, _ = self.mod.pace_info(l)
        self.assertLess(ratio, 1.0)
        self.assertEqual(self.mod.pace_posture(l), "normal")


class TestPaceInJsonOutput(CheckUsageTestCase):
    def test_render_emits_pace_and_pace_driven_posture(self):
        mod = self.mod
        mod.HISTORY = [
            {"at": mod.NOW - 1000, "limits": {"session|": 12.0}, "resets": {"session|": "R"}},
        ]
        limits = [{
            "kind": "session", "percent": 20.0, "resets_at": "R", "resets_in_seconds": 15000,
            "severity": None, "scope_model": None, "is_active": False, "extra": None,
        }]
        code, out = self.run_with_exit(mod.render, limits, 0, False, "cache")
        payload = json.loads(out.strip().splitlines()[-1])
        pace = payload["limits"][0]["pace"]
        self.assertIsNotNone(pace)
        self.assertAlmostEqual(pace["ratio"], 1.5, places=2)   # 15000s reset / 10000s exhaust
        self.assertEqual(pace["exhaust_seconds"], 10000)
        # Low percent (20) but unsustainable pace -> posture is frugal, not normal.
        self.assertEqual(payload["posture"], "frugal")

    def test_render_pace_null_without_burn_baseline(self):
        mod = self.mod
        mod.HISTORY = []
        limits = [{
            "kind": "session", "percent": 20.0, "resets_at": "R", "resets_in_seconds": 15000,
            "severity": None, "scope_model": None, "is_active": False, "extra": None,
        }]
        code, out = self.run_with_exit(mod.render, limits, 0, False, "cache")
        payload = json.loads(out.strip().splitlines()[-1])
        self.assertIsNone(payload["limits"][0]["pace"])
        self.assertEqual(payload["posture"], "normal")


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
