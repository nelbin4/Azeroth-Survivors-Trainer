# Changelog

## 1.2.0 — 2026-10-30

- Pickup range, area, extra targets, echo and DoT stacks are now applied too (they were previously left alone). Experience is no longer locked to +200%: any multiplier from 1 to 1,000,000 is accepted; speed stays at +50%.
- Windows launcher exe (`launcher/launcher.py`, `build_exe.py`): bundles Python and the libraries, loads `game.py` from its folder so updates never need a new exe. Children it starts reset PyInstaller's environment so the background watcher survives the starting process.
- Removed the automatic level-up card picking and everything built around it (damage-meter ranking, per-class card priorities, class and meter options). The trainer is now only the stat trainer plus the *in menu / in lobby* state. Screen reading is limited to the times no run is active.

## 1.1.0 — 2026-10-30

- Automatic updates: at startup it checks GitHub, downloads and verifies the new version, installs it (keeping your settings) and restarts. Can be switched off in the window.

## 1.0.0 — 2026-10-30

First release as **Azeroth Survivors Trainer**.

- Window (`python game.py`), silent background mode (`--background`) and command line.
- Name-guarded stat watcher: finds the character by name, validates ownership, backs up before writing, verifies by readback, re-applies after level-ups. No pausing, debugger or injection.
- Self-learning for new game builds (read-only until confirmed), plus `relayout` and `find-wallet` for moved fields.
- Duplicate-record resolution by observed activity.
- (Removed in 1.2.0.) Experimental level-up card picker using background window capture and a persistent Windows text recognizer (about 0.15-0.3 s per look). It always picks a card: by priority, else the rarest. It never rerolls or skips. Per-class priorities in `card_priorities.json` (Paladin and a Hunter starting list), class recognised from the popup or chosen in the window. Smart upgrade picks read the in-game Damage Done panel and favour upgrades for your highest-damage spell (optional). `cards-test` / `meter-test` are dry runs.
- First-run setup (`setup` command or window prompts); personal state kept in the git-ignored `session.json`.
- The state shows `in menu` / `in lobby` when no run is active (screen read by background capture).
- Automatic character detection (no fixed name needed); re-detected for every run, so lobby renames just work.
- Efficiency: batched session writes, adaptive polling, per-build hints, bounded `session.json` size.
- 41 built-in tests (after 1.2.0) (`python game.py self-test`); Windows CI.
- Removed the legacy process-pausing commands.
