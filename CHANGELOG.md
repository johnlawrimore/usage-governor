# Changelog

All notable changes to usage-governor are documented here. Versioning follows
[semver](https://semver.org/): breaking changes to the JSON output contract bump the major
version, new fields or flags bump the minor, fixes bump the patch.

## [1.0.0] - 2026-07-13

### Added

- Initial public release.
- `SKILL.md`: governance instructions for Claude Code (posture-driven throttling, the no-op
  floor, per-model budget checks, failure modes).
- `scripts/check-usage.py`: stdlib-only usage reader with deterministic posture
  (`normal` / `frugal` / `wind_down`), burn rate over a recent window, time-to-reset,
  5-minute caching, 429 backoff, and forward-compatible parsing of unknown limit kinds.
- Cross-platform launchers (`check-usage.sh`, `check-usage.cmd`) with working-interpreter
  detection.
- Stdlib `unittest` suite and GitHub Actions CI (Linux + Windows, Python 3.9 and 3.13).
