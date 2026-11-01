# Changelog

## 1.6.0 — 2026-11-01

- **Talent cap is back, and it works.** Raise the talent point cap from 51 (default 150, any value 1 to 999) from the window's *Talents* box or `python game.py talents apply|restore|status`. Verified on game build `71e5f176…`: *Talents (148)*, *2 of 150 earned*.
- **How it finds the spots:** by scanning the game file on disk (never by trying places in the live game). The cap is returned where the talent-cost loop ends at a running cost of 875: `cmp reg,0x36b ; jne ; mov reg,51`. This build has 20 such copies.
- **How it applies them:** all copies together, while the game is paused for a few milliseconds (17 ms in the live test), with every spot checked first, verified after, and rolled back on any failure. The earlier crashes came from changing the copies one at a time, which left the game holding two different caps.
- Memory only; closing the game undoes it, so it is applied again each launch. *Apply when the game starts* is optional and off by default.

## 1.5.0 — 2026-11-01

- **Removed the talent cap** (migrated from `talent_150.py` in 1.4.0): the Talents box, the `talents` command, the code-patching helper and their tests. On the current game build the cap is a computed value, not a constant in the code, and a read-only search found no stored value to edit; patching instructions crashed the game. The findings stay in the technical notes.

## 1.4.2 — 2026-11-01

- **Removed "Find talent spots".** Running it on the current game build crashed the game twice with the same heap-corruption error: it temporarily changed the number 51 to 150 in candidate instructions, and the very first candidates are loop and table bounds, not a talent cap. The talent cap itself (Apply / Restore) stays, but it only works for game builds whose three code spots are known; the current build is not one of them.

## 1.4.1 — 2026-11-01

- Talent cap: a clear message when Windows refuses to let the trainer change the game's code (seen on build `71e5f176…`). The README and technical notes say the feature may not work on the newest game build.

## 1.4.0 — 2026-11-01

- **Talent cap** (migrated from `talent_150.py`): raise the talent point cap from 51 (to 150 by default, any value 1 to 999) from the window or `python game.py talents apply|restore|status|find`. It rewrites the 32-bit constant in three game instructions in memory only (nothing on disk), with plain Windows calls: no Frida, no hooks. Every spot is checked before writing, verified by readback and rolled back on failure. *Apply when the game starts* is optional and off by default.
- **Find talent spots**: a game update moves the three spots. The search tests candidate places one at a time, restores each immediately, checks the cap shown on the talents screen, stops if the game closes, and remembers the result per game version. It needs the talents screen open and a confirmation, because a mistake could crash the game.

## 1.3.0 — 2026-10-30

- Window: a plain-English status line instead of state codes; the Target column is editable (typed values are validated, saved, and the watcher restarts to use them; speed stays locked); a *What's new* button shows the latest release notes.

## 1.2.1 — 2026-10-30

- **Vs Elites** is now applied (modifier kind 19, confirmed by changing it and watching the Run Stats panel).
- The background watcher is now started in its own process group, so a Ctrl+C aimed at the console that launched it can no longer stop it. (Suspected cause of the watcher stopping by itself; not confirmed.)
- Findings recorded in the technical notes: DoT stacks display as 1 + the record field (the game resets that field whenever the watcher is not running), and lifesteal, thorns and lives have no entry in the modifier list.

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
