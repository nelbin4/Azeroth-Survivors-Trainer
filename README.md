# Azeroth Survivors Trainer

> **Experimental:** Automatic pattern-based live-record selection is disabled because it wrote to unverified candidates, including in observe-only mode. Automatic application may therefore wait indefinitely on some builds. The Damage/Area hook requires a manually verified address and the exact supported game build; it does not automatically rediscover characters. Do not use the hook with another trainer or hook at the same instructions.

> The Windows launcher bundles Python, its dependencies, and the default profile. Keep `game.py` beside the executable; a compatible trainer update replaces only that file.

An experimental Windows trainer for Azeroth Survivors. Support varies by game build and character layout; automatic discovery can remain blocked when a record cannot be verified.

> **Solo and hosted games:** the available tests were performed in solo or hosted runs. Joined co-op behavior has not been reliably verified.

## What you get

- **Configurable stats.** The existing watcher supports multiple fields on layouts it can identify; it does not reliably identify every build or run.
- **Experimental recalculation hook.** On the verified build, a manually located character record can keep Damage and Area at +9999% after recalculation.
- **Doesn't pause the game.** It works quietly in the background while you play.
- **Optional talent cap and max talents.** Raise your talent point cap (51 in the game) to 150, then max every talent for one class or all nine.
- **Easy to switch off.** Untick any stat, switch to *Observe only*, or press *Stop* at any time.
- **Updates:** the launcher can update `game.py` from GitHub. A new launcher is required when Python or bundled dependencies change.

## Get started in 3 steps

1. **Download** the latest zip from the [Releases page](https://github.com/nelbin4/Azeroth-Survivors-Trainer/releases) and unzip it anywhere. You don't need to install anything else. *(Prefer Python? See "Install in detail" below.)*
2. **Open Azeroth Survivors** (the Windows version) and start a **solo run**, or host one.
3. **Double-click `AzerothSurvivorsTrainer.exe`.** The first time, select the game executable. The window shows whether it could identify the character; some builds remain unsupported.

If the game runs as administrator, run the trainer as administrator too.

## Quick answers

- **Can I use it in a joined co-op game?** The trainer's effects there have not been reliably verified; avoid using it in games where other players have not agreed.
- **Will it break my game?** It's careful (it checks before every change and keeps backups), but changing a game's memory is never risk-free. Use it at your own risk, and don't push stats to extremes you can't undo (for example, very high experience means many level-ups per second).
- **My antivirus warns about it.** That's common for any trainer that edits another program's memory, because the program is unsigned. The full source is in this repository.
- **Does it work with the browser version of the game?** No, only the Windows version.
- **How do I stop it?** Press *Stop* in the window, or just close the game. Closing the window alone leaves it running.

*This is an unofficial fan tool, not affiliated with the game or its developer. No warranty; see [LICENSE](LICENSE).*

---

# Details

Everything below is for people who want to know more or to use the command line.

## Install in detail

### Option A: the Windows program (no Python needed)

Download the release zip, unzip it anywhere, and double-click **`AzerothSurvivorsTrainer.exe`**. Keep `game.py` in the same folder. The executable bundles Python, the dependencies and the default profile; the first start takes a few seconds while they unpack. Command-line output and diagnostic errors are appended to `trainer.log` in that folder.

> Windows SmartScreen or your antivirus may warn about an unsigned program that reads another program's memory. That is expected for any trainer; the source of everything is in this repository.

### Option B: with Python

You need **Windows 10/11 (64-bit)**, **Python 3.12 or newer** (with Tkinter, included in the standard installer) and the **Windows build of Azeroth Survivors**. The trainer does not work with the browser version. Get the game from its guides and download links: [azerothsurvivorsgame.com](https://azerothsurvivorsgame.com/) or [azerothsurvivors.games](https://azerothsurvivors.games/wow-survivors/).

```bash
git clone https://github.com/nelbin4/Azeroth-Survivors-Trainer.git
cd Azeroth-Survivors-Trainer
pip install -r requirements.txt
python game.py
```

## First run in detail

The window asks for the path to `AzerothSurvivors.exe`. Start a run and watch its status: the watcher may not find a sufficiently verified character record, especially after a game update. The experimental command-line hook requires a manually located record and character name.

Command-line setup instead of the prompts: `python game.py setup --game "C:\path\to\AzerothSurvivors.exe"`. Your settings are stored in `session.json` next to the program and are never uploaded.

> ### Co-op
> The current trainer has been tested in solo and hosted games. Whether a change in a joined co-op game affects gameplay is unverified; memory values and displays alone do not establish which participant controls combat state.
>
> Changing memory in a game can also crash it or corrupt a run, so use it at your own risk. Do not use it to disrupt other players.

## What it does

- **Character detection.** The watcher attempts to identify a character from memory and screen information. It can fail or encounter ambiguous candidates; automatic discovery across new runs is unfinished.
- **Stat trainer.** On supported, identified layouts, the watcher can apply configured stat targets. The separate experimental hook currently covers Damage and Area only; its address must be revalidated for each run. Readback and a matching name alone do not establish that a field controls gameplay.
- **Runtime instrumentation.** The experimental hook uses Frida to intercept instructions in the game process. The optional talent cap also changes code in memory.
- **Game updates.** Layout discovery is not reliable for every update. The experimental hook rejects all executable hashes other than the verified build.
- **Menu / lobby awareness.** While no run is active the state says *in menu* or *in lobby* instead of just waiting.
- **Safe by default.** Every field can be switched off, and an observe-only mode never writes.
- **Source updates.** Both launcher and Python installations can check GitHub at startup (see Updates).

### Experimental Damage and Area hook

For the game executable with SHA-256 `0e8e314df4d78ab211b1bef5c48d78d713ed64260894af342f998d298d74d274`, run `.\AzerothSurvivorsTrainer.exe hook-current --pid PID --base 0xADDRESS --name CHARACTER` from PowerShell or Command Prompt. Check `trainer.log` for the result. The base must be the manually verified live stat record for the current run. This command does not discover the address. It checks the executable hash, instruction bytes, and name header before attaching, and checks the name again on each write. The hook runs for at most 600 seconds by default; set `--seconds` to a value from 1 to 86400 to change this, or run `.\AzerothSurvivorsTrainer.exe hook-stop` to detach it early. It does not automatically follow a new run or process; do not run it alongside other hooks at those instructions. Python users can use the same commands with `python game.py` instead of the executable.

## Updates

The launcher checks `game.py` on this repository's `main` branch. When it finds a higher `VERSION`, it downloads that file, checks that the source compiles, backs up the previous file under `.update_backup/`, replaces it and restarts. Compilation checks syntax, not whether the update works with every game build or bundled dependency. Your settings and saves are not replaced.

The check runs when the trainer window starts, or when `auto --background` is launched. **Starting a new game run while the trainer is already running does not trigger an update check.** Stop the existing watcher and restart the trainer to check again. Direct commands such as `hook-current` do not check for updates. Offline checks are skipped silently.

- **What is replaced:** only `game.py`. Your `session.json`, profile, and saves are not changed by the updater. The previous `game.py` is backed up in `.update_backup/<old version>/`.
- **Git checkouts** (developers) fetch and fast-forward the checkout only when tracked files have no uncommitted changes; this can update files other than `game.py`.
- **Trust:** updates come over HTTPS from this repository's `main` branch. Switch it off with the window's *Check GitHub for updates* box, or `"auto_update": false` in `session.json`, or start with `--no-update`.
- README and other documentation updates on GitHub are **not** downloaded by the launcher's source updater. Download a new release if you want its bundled documentation.
- If an update adds a dependency or requires a newer Python runtime or default profile, install a newer launcher executable. Python installations may need `pip install -r requirements.txt`.

### Publishing a source-only update

1. Change and test `game.py`; increment its `VERSION` to a numerically higher version (for example, `1.9.5` to `1.9.6`).
2. Commit and push it to `main`. Existing launcher installations with update checks enabled can then receive the source file the next time the trainer starts. The check reads `main`, not the GitHub Releases page.
3. Publish a new launcher package if the change needs different bundled dependencies, Python, or default profile. Publish a new GitHub Release when you want the Releases page and new downloads to include the update; pushing source alone does not update an existing release ZIP.

## Using it

| Command | What it does |
| --- | --- |
| `python game.py` | Opens the window. Starts the watcher in the selected mode (default: Write). |
| `python game.py --background` | Starts the watcher silently, with no window. |
| `python game.py status` | Prints the watcher's current state as JSON. |
| `python game.py stop` | Stops any running watcher. |
| `python game.py relayout --hp 807 --damage 104 ...` | Learns moved stat offsets after a game update, from the values on your character sheet (read-only). |
| `python game.py find-wallet --gold 1234` | Learns the gold counter's location from an exact gold amount (read-only). |
| `python game.py talents apply --cap 150` | Raises the talent point cap in the running game. Also `restore` (back to 51) and `status`. |
| `python game.py max-talents --class mage` | Writes an importable save with every talent at its maximum (`--class all` for all nine classes). Your current save is backed up first and never modified. |
| `python game.py self-test` | Runs the built-in test suite. |
| `python game.py --version` | Shows the version. |

Add `--no-update` after any command (for example `python game.py gui --no-update`) to skip the startup update check.

The same commands work with `AzerothSurvivorsTrainer.exe` instead of `python game.py`. The executable runs without a console window and appends output to `trainer.log`; it does not print results to the terminal. A hook command remains active until its timeout, the game closes, or `hook-stop` is called.

The window is a status card (what the trainer is doing, your level, and a Start/Stop button) with three tabs. **Stats**: tick stats on or off, or type a new target and press Enter (the watcher restarts to use it); stats the current game build does not apply yet are greyed out. **Talents**: a *Raise talent cap* checkbox (applied now, and every time the game starts) and the *Max talents* class picker with **Create save** and *Open Saves folder*. **Log**: what the trainer is doing. **Settings** (bottom left) holds the Write / Observe-only switch, the cap number, the update check and *What's new*. Closing the window leaves the watcher running; press **Stop** to end it.

### Max talents without clicking

1. Start the game and tick **Raise talent cap** (150 covers every class; the biggest build is 128 points).
2. In the Talents box pick a class (or *All classes*) and press **Create save**. The new file is `AzerothSurvivors-max-talents-<class>.sav` in the game's `Saves` folder (*Open Saves folder* gets you there), and your current save is copied to `AzerothSurvivors-before-max-talents-<time>.sav`.
3. In the game: **Options, Saves, Import Save**, choose the new file, check the preview and accept.

To undo it, import the `before-max-talents` backup the same way. Notes: the game keeps your Spec 1 *preset* trimmed to 51 points even though the active talents are full, so avoid switching specs; and after restarting the game, apply the cap *before* importing again, or the game cuts an over-limit build back to 51 when it loads.

## What it looks at on screen

The trainer never records your screen. Only while **no run is active** it reads text from the **game's own window** about every 10 seconds (a background capture: nothing visible, no cursor or focus change) so the state can say *in menu* or *in lobby*. In a hosted game with several players it also reads your player frame once to tell which character is yours. The text is recognised by Windows' built-in recognition on your PC and the image is deleted immediately. Nothing is uploaded, and nothing is captured while a run is going.

## How it stays safe

The watcher includes structural checks and can refuse ambiguous candidates, but those checks do not guarantee that a record belongs to the local player or controls gameplay. The old pattern-based candidate-writing probe is disabled. The experimental hook checks its exact game hash, instructions and name header; it still requires the user to supply a verified record for the current run.

## Limitations

- **Joined co-op:** effects on live gameplay are unverified, including which participant controls a given stat.
- **Windows build only.** The browser version is not supported.
- A new game build's learned layout is only trusted after three consistent samples. If stat offsets themselves move, run `relayout`.
- Target values live in `session.json` (`config.targets`); a displayed `+N%` is the multiplier `1 + N/100` (for example +200% experience = 3.0). Speed is locked at +50%; experience may be set from 1 to 1,000,000. Extreme values can make the game misbehave (for example many level-ups per second).
- **Max talents use a signed copy of your save.** The trainer finds the game's own save key inside the game file and accepts it only if it authenticates your current save; it refuses to write anything if the game's talent table does not match the talents already in your save. It never edits `characters.txt` itself.
- **Talent cap:** it is applied to the running game only, so it is applied again every time the game starts while *Raise talent cap* is ticked. The small "51 at most" text on the talents screen is a fixed label and keeps saying 51; the points you can spend are what change. Reopen the Talents tab after applying so the screen redraws. Each game update can move the code the cap lives in; the trainer finds it again from the game file and refuses to touch the game if what it finds does not fit.
- **Not covered:** lives, thorns, lifesteal and pet damage. Lives, thorns and lifesteal were not found as editable values at all in the character's data (they appear to exist only once you own something that grants them); pet damage is simply not applied. Vs elites and DoT stacks are covered, but the game resets them whenever the trainer is not running, so keep the watcher running.
- **New runs or patched builds:** automatic discovery and reacquisition are not reliable. The experimental Damage/Area hook supports only the documented build and needs a newly verified address for each run.
- Only the stats listed in the window are managed. The wallet needs its offset learned on new builds (`find-wallet`).
- Values apply in periodic corrections (about every half second), not atomically, so the display can briefly dip after a recalculation.

## Troubleshooting

- **"Not configured yet"** — run the window once, or `setup`.
- **State "in menu" / "in lobby"** — no run is active yet, and the trainer can tell which screen you are on (read from a background capture). Start a run and it takes over.
- **State stays "seeking record"** — the watcher has not identified a usable character record. It can remain in this state during a run.
- **State "blocked ambiguous"** — more than one character is in the game (a hosted lobby) and yours could not be told apart; keep the player frame visible on screen and it is picked from there.
- **State "blocked"** — an unrecognised build is still being learned; stay in a run and level up once.
- **Nothing happens** — run as administrator if the game is elevated.
- **The exe does not start** — check that `game.py` is next to it, or reinstall the release archive. Antivirus or SmartScreen may warn about an unsigned program.
- Diagnostic output and window errors are appended to `trainer.log` next to the executable.

## Development

`launcher/launcher.py` loads the adjacent `game.py`; PyInstaller bundles the Python runtime and dependencies into the executable. Build it with:

```bash
pip install pyinstaller
python build_exe.py            # windowed exe + release zip in release/
```

```bash
python game.py self-test    # no game required
```

CI runs the tests on Windows for every push. Technical research notes (memory layout, history of approaches) are in [docs/TECHNICAL_NOTES.md](docs/TECHNICAL_NOTES.md). See [CHANGELOG.md](CHANGELOG.md) for release notes.

## License

MIT. See [LICENSE](LICENSE).
