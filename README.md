# Azeroth Survivors Trainer

A Windows trainer for the free fan game **Azeroth Survivors**. It finds your character in the game's memory, applies the stat values you choose without pausing or hooking the game,. It has a small window, a silent background mode, and a command line.

## Install

You need **Windows 10/11 (64-bit)**, **Python 3.12 or newer** (with Tkinter, included in the standard installer), and the **Windows build of Azeroth Survivors**. The trainer does not work with the browser version. Get the game from its guides and download links: [azerothsurvivorsgame.com](https://azerothsurvivorsgame.com/) or [azerothsurvivors.games](https://azerothsurvivors.games/wow-survivors/).

```bash
git clone https://github.com/nelbin4/Azeroth-Survivors-Trainer.git
cd Azeroth-Survivors-Trainer
pip install -r requirements.txt
```

## First run

> **Use it only in a solo run or a game you host. Never when joining someone else's game.** (Details below.)

Start the game, then:

```bash
python game.py
```

The window asks once for the path to `AzerothSurvivors.exe`, and optionally for your character name. **Leave the name blank and the trainer detects your character itself**, so switching characters needs no setup. You can rename your character in the lobby whenever you like; the name is re-detected for every run. Then start a run; the watcher starts with the window and applies your stats once you level up or take damage. Run the trainer as administrator if the game runs elevated.

Command-line setup instead of the prompts: `python game.py setup --game "C:\path\to\AzerothSurvivors.exe"`, adding `--name YourCharacter` if you want to pin one. Your settings are stored in `session.json` next to the program and are never uploaded.

> ### Solo or host only — do not use it when joining a game
> Use this trainer **only in a solo run, or in a game you are hosting**. Do **not** use it when you join someone else's game. As a guest, your stats are computed by the host and your PC only shows copies, so changes do nothing there, and tampering with a joined game can desync or disconnect you and disrupt the other players.
>
> Changing memory in a game can also crash it or corrupt a run, so use it at your own risk. Do not use it to disrupt other players. This is an unofficial fan tool, not affiliated with the game or its developer. No warranty; see [LICENSE](LICENSE).

## What it does

- **Automatic character detection.** Finds your character in memory by structure, not by a fixed name. In a solo run it is the only character; when hosting with others it picks the one listed first in your player frame (read from a background capture). Names it has seen are remembered.
- **Stat trainer.** Keeps max/current HP, damage, crit, crit damage, speed, XP, regeneration, luck, gold and more at your targets. It checks level, name and ownership before every write, verifies by readback, and re-applies after level-ups.
- **No pausing, no debugger, no injection.** It uses ordinary read/write process memory access only.
- **Self-learning for new game versions.** If the game updates, it re-learns where your character lives (read-only) and only writes fields it could verify. A `relayout` command re-learns moved stats from your character sheet.
- **Safe by default.** Every field can be switched off, and an observe-only mode never writes.

## Updates

At startup (the window, or `--background`) the trainer checks GitHub for a newer version, downloads it, checks that the new code compiles, replaces the program files, and restarts itself. This takes a couple of seconds and is skipped silently when you are offline.

- **What is replaced:** `game.py`, the docs, `profile.example.json`, `requirements.txt`. Your `session.json` and saves are never touched. Replaced files are backed up in `.update_backup/<old version>/`.
- **Git checkouts** (developers) only fast-forward when you have no uncommitted changes.
- **Trust:** updates come over HTTPS from this repository's `main` branch. Switch it off with the window's *Check GitHub for updates* box, or `"auto_update": false` in `session.json`, or start with `--no-update`.
- If an update adds a new dependency, run `pip install -r requirements.txt` after it.

## Using it

| Command | What it does |
| --- | --- |
| `python game.py` | Opens the window. Starts the watcher in the selected mode (default: Write). |
| `python game.py --background` | Starts the watcher silently, with no window. |
| `python game.py status` | Prints the watcher's current state as JSON. |
| `python game.py stop` | Stops any running watcher. |
| `python game.py relayout --hp 807 --damage 104 ...` | Learns moved stat offsets after a game update, from the values on your character sheet (read-only). |
| `python game.py find-wallet --gold 1234` | Learns the gold counter's location from an exact gold amount (read-only). |
| `python game.py self-test` | Runs the built-in test suite. |
| `python game.py --version` | Shows the version. |

In the window you can switch **Observe only / Write**, **Start / Stop**, and include or exclude each stat. Closing the window leaves the watcher running; press **Stop** to end it.

## What it looks at on screen

The trainer never records your screen. Only while **no run is active** it reads text from the **game's own window** about every 10 seconds (a background capture: nothing visible, no cursor or focus change) so the state can say *in menu* or *in lobby*. In a hosted game with several players it also reads your player frame once to tell which character is yours. The text is recognised by Windows' built-in recognition on your PC and the image is deleted immediately. Nothing is uploaded, and nothing is captured while a run is going.

## How it stays safe

1. Finds the game by exact executable path and checks its SHA-256. An unknown build triggers read-only learning, never blind writes.
2. Identifies your character by its length-checked name (configured or auto-detected), then validates player ID, level, plausible stats and the modifier table.
3. Backs up original bytes before the first write, checks ownership before each write, and verifies by readback. On failure it conditionally restores only bytes it wrote.
4. A single odd reading never resets state; a level drop must be seen twice before the run is treated as new, and writes pause meanwhile.
5. If the character is ambiguous (several valid copies), it picks the one that actually changes, or refuses to write.

## Limitations

- **Solo or host only — never when joining a game.** As a guest, your PC holds display copies refreshed from the host every tick; writes there have no effect and can cause problems for other players.
- **Windows build only.** The browser version is not supported.
- A new game build's learned layout is only trusted after three consistent samples. If stat offsets themselves move, run `relayout`.
- Target values live in `session.json` (`config.targets`); a displayed `+N%` is the multiplier `1 + N/100` (for example +200% experience = 3.0). Speed is locked at +50%; experience may be set from 1 to 1,000,000. Extreme values can make the game misbehave (for example many level-ups per second).
- Only the stats listed in the window are managed. The wallet needs its offset learned on new builds (`find-wallet`).
- Values apply in periodic corrections (about every half second), not atomically, so the display can briefly dip after a recalculation.

## Troubleshooting

- **"Not configured yet"** — run the window once, or `setup`.
- **State "in menu" / "in lobby"** — no run is active yet, and the trainer can tell which screen you are on (read from a background capture). Start a run and it takes over.
- **State stays "seeking record"** — you are probably in a lobby or in someone else's game (not supported). If you pinned a name, make sure it matches exactly, or clear it by running `setup` again without `--name`.
- **State "blocked ambiguous" with several names** — more than one character is in the game and yours could not be told apart; pin your name with `setup --name`.
- **State "blocked"** — an unrecognised build is still being learned; stay in a run and level up once.
- **Nothing happens** — run as administrator if the game is elevated.
- Errors from the window are written to `gui-error.log`.

## Development

```bash
python game.py self-test    # 41 tests, no game required
```

CI runs the tests on Windows for every push. Technical research notes (memory layout, history of approaches) are in [docs/TECHNICAL_NOTES.md](docs/TECHNICAL_NOTES.md). See [CHANGELOG.md](CHANGELOG.md) for release notes.

## License

MIT. See [LICENSE](LICENSE).
