# Changelog

## 2.0.5 (experimental)

- Smaller download: the exe no longer bundles Frida (about 84 MB down to about 43 MB). The watcher doesn't use Frida; the old manual hook commands need Frida installed separately.

## 2.0.4 (experimental)

- Logs: `trainer.log` records window messages, state changes, errors, record lock-on and each verified offset.
- If the stat pattern is not found for a minute, the window says so and writes `discovery-report.txt` for diagnosis.
- Window: fixed 320 x 320, with Stop and Launch Game under the status text. Launch Game is greyed out while the game is running. The stats list is hidden.
- GitHub button opens the project page.
- The release zip no longer contains a separate LICENSE file; the MIT notice is at the top of `game.py`.

## 2.0.3 (experimental)

- Damage, Area, Luck and Gold are discovered and applied on the current game build. Area, Luck and Gold are verified from the character panel and Run Stats box once per game version; the offsets are then saved.
- The stat scan reads only the two stat boxes, and small values such as '+ 36%' are read correctly.
- A prompt in the window asks you to open your character panel when a value still needs verifying.

## 2.0.2 (experimental)

- Luck and Gold are applied on game build `0e8e…` as second priority, after Damage and Area. Luck is at offset +48 and Gold at +52; both were confirmed by writing a test value and watching the Run Stats panel, then restoring.
- Priority: Damage and Area are checked every pass; Luck and Gold every fifth pass.
- Area uses the offset saved for this game build, so it no longer needs the character sheet after each restart.

## 2.0.1 (experimental)

- Damage and Area are applied on game build `0e8e…` in every run: Damage is pinned, and Area is pinned at the field verified against the character sheet.
- Area's field is found by reading the sheet's percentage and matching it to memory; the offset is kept per game version.
- Nothing is written in the lobby or menu, and the watcher re-applies after level-ups.
- Simpler window: Talents is a button that opens its own dialog; status text and counters are removed.

## 2.0.0 (experimental)

- **Fix: the Damage and Area hook never started.** The hook's lock and its stop event shared one Windows name, so starting it failed with "The handle is invalid". The lock now has its own name.

## 1.9.9 (experimental)

- **Simpler window.** The main window shows the status card, the stats this build holds with their values, the Talents box and Launch Game / Settings. Settings is a dialog with General, Stats and targets, and Log tabs. The log keeps recent lines while the dialog is closed.
- Shows the version in the window footer.

## 1.9.8 (experimental)

- Report unavailable automatic character discovery as blocked instead of indefinitely looking, and show unverified stats accurately. This does not add automatic discovery for the affected build.

## 1.9.7 (experimental)

- Add a Launch Game button using the configured game executable. The button avoids launching a second copy when that exact executable is already running.

## 1.9.6 (experimental)

- Use one windowed launcher for the GUI and command-line operations; output and errors are appended to `trainer.log`.
- Add `hook-stop` and a bounded hook duration so the experimental hook can be stopped without a console.
- Ship one launcher, `game.py`, and the license in the release ZIP.
- Use the supplied artwork as the launcher and application-window icon.

## 1.9.5 (experimental)

- Return to a reusable launcher that loads the adjacent `game.py` and bundles its default profile.
- Update only `game.py` from GitHub after validating it; keep a backup of the previous version.
- A launcher rebuild is still needed when its Python runtime or bundled dependencies change.

# 1.9.4 (experimental)

- Disable the unsafe candidate-write probe used by automatic pattern discovery. This may leave some runs without an automatic match.
- Add an explicit, build-checked Damage and Area recalculation hook for a manually verified record. New runs require rediscovery.
- Bundle the trainer and Frida into the Windows executable. Source-based automatic updates are disabled in frozen builds.

## 1.9.3 — 2026-11-02

- **Area is applied** on game build `eb7447…`, next to Damage (both pinned every 20 ms). Tested stepwise first: 2, 4, 10, 30 and 100.99, five seconds each, then 15 seconds at 100.99 in a live party run (level 71 to 72), game responsive throughout. Extra targets are still off.
- **Simpler window:** the status card with three tabs (Stats, Talents, Log) and a Settings button. Stats the current build does not apply yet are greyed out.

## 1.9.2 — 2026-11-02

- **Fix: several live characters.** The game can hold other live hero records besides yours (companions, a lobby); the trainer used to give up ("blocked ambiguous"). It now picks yours: the one whose header text is the name in the player frame on screen, or, when the screen does not show it (menus), the name remembered from the last run. Verified live: Damage applied to the right character.

## 1.9.1 — 2026-11-01

- **Fix: Damage was not applied ("Reconnecting").** The check that a text header sits before the stat block was tied to one exact distance learned in the first scan; a nearer, changing piece of text could be the one it remembered, so the check failed on later reads. Any header in range now counts.

## 1.9.0 — 2026-11-01

- **Finds Damage by itself on any new game build, quickly.** After a game patch there is no learning wait and no saved layout needed: the trainer scans for the stat block by its fixed field pattern (health, damage, haste, crit, crit damage, speed, pickup, experience) next to a text header, tells the live copy from stale ones by nudging Damage 1% (the game recalculates the live one), and pins Damage at once. It remembers where it found it, so a watcher restart in the same game takes about 4 seconds; a cold start on a patched build takes about 10 to 30 seconds. Verified live on build `eb7447…`.
- **Only Damage** is applied this way for now. Other stats are added one at a time as each is proven safe.
- Older builds that were learned before keep their full stat set.
- Tests: 57.

## 1.8.2 — 2026-11-01

- **Damage works on game build `eb7447…`.** Only Damage is applied on that build for now: it is pinned at your target every 20 ms (the game recalculates it every couple of seconds). Verified live through the trainer itself: state *applying*, Damage held at +9999% on the character sheet, game responsive. The other stats there stay off until each is proven safe, one at a time.

## 1.8.1 — 2026-11-01

- **Reverted the stats writes for game build `eb7447…`.** 1.8.0 shipped a stats-only layout for it; in the first live test the game stopped responding while it ran. That layout is no longer applied (reading and the other improvements stay). Do not use 1.8.0 on that build.

## 1.8.0 — 2026-11-01

- **Supports the newest game build (`eb7447…`)** with no learning wait. Its layout is read from the character sheet and shipped with the trainer. Verified live: the trainer finds your character on its own and keeps these stats at your targets: health, damage, crit, crit damage, speed, pickup, experience, armor, regeneration, area, gold bonus, extra targets, echo and current health.
- **No character name anywhere.** The trainer finds your character by structure; `setup --name` is gone and the window shows only your level.
- **Not applied on this build yet:** haste, luck, kill heal, buff power, DoT, Vs Elites and the gold wallet (their locations are not confirmed).
- **Faster, correct learning on unseen builds.** The name is now read off the screen instead of guessed from stale memory (it used to lock onto words like "in"), and the window says what it is waiting for.

## 1.7.2 — 2026-11-01

- **Simpler window.** Status card, a *Raise talent cap* checkbox, and the *Max talents* picker with Create save. Everything else (Mode switch, cap number, Saves folder, What's new, stat targets, Log, Settings) is folded under **More**. The cap checkbox applies the cap now and every time the game starts, and unticking it restores 51.

## 1.7.1 — 2026-11-01

- **Simpler first run.** The main window opens straight away, then a file picker asks you to locate the downloaded `AzerothSurvivors.exe`. The extra message box and the character-name question are gone (the name is detected automatically). Cancel the picker and the window stays open with a **Locate game** button.

## 1.7.0 — 2026-11-01

- **Max talents for any class (or all nine).** New *Max talents for [class] / Create save* row in the Talents box and `python game.py max-talents [--class mage]`. It reads every talent and its maximum rank from the game file (9 classes x 3 trees, 355 talents, 121 to 128 points per class), writes them into a copy of your save in the game's own signed save format, and leaves your real save untouched. Your current save is backed up first. Import the new file in the game (Options, Saves, Import Save) after raising the talent cap, or the game trims the build back to 51 points.
- **Minimal window.** One status card with Start/Stop, the Mode switch, the Talents box, and folded sections for *Stats and targets*, *Log* and *Settings*. Everything that was visible before is still there.

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
