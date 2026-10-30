# Changelog

## 1.0.0 — 2026-10-30

First release as **Azeroth Survivors Trainer**.

- Window (`python game.py`), silent background mode (`--background`) and command line.
- Name-guarded stat watcher: finds the character by name, validates ownership, backs up before writing, verifies by readback, re-applies after level-ups. No pausing, debugger or injection.
- Self-learning for new game builds (read-only until confirmed), plus `relayout` and `find-wallet` for moved fields.
- Duplicate-record resolution by observed activity.
- Experimental level-up card picker using background window capture and Windows text recognition (`cards-test` dry run).
- First-run setup (`setup` command or window prompts); personal state kept in the git-ignored `session.json`.
- The state shows `in menu` / `in lobby` when no run is active (screen read by background capture).
- Automatic character detection (no fixed name needed); re-detected for every run, so lobby renames just work.
- Efficiency: batched session writes, adaptive polling, per-build hints, bounded `session.json` size.
- 38 built-in tests (`python game.py self-test`); Windows CI.
- Removed the legacy process-pausing commands.
