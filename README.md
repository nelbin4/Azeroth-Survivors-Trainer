# Azeroth Survivors Trainer

A Windows trainer for the free fan game **Azeroth Survivors**. It finds your character in the game's memory, applies the stat values you choose without pausing or hooking the game, and can pick level-up cards for you from a priority list. It has a small window, a silent background mode, and a command line.

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
- **Level-up card picker (experimental).** Reads the popup from a background window capture with Windows' built-in text recognition, picks by your priority list, and presses the card's number key (the game's own hotkeys: 1–3 or 1–4). It never rerolls and never skips. It reads the popup in a fraction of a second. Nothing is shown on or moved on your screen.
- **Safe by default.** Every field can be switched off, and an observe-only mode never writes.

## Using it

| Command | What it does |
| --- | --- |
| `python game.py` | Opens the window. Starts the watcher in the selected mode (default: Write). |
| `python game.py --background` | Starts the watcher silently, with no window. |
| `python game.py status` | Prints the watcher's current state as JSON. |
| `python game.py stop` | Stops any running watcher. |
| `python game.py cards-test` | Dry run: reads a level-up popup and says what it would pick. Presses nothing. |
| `python game.py relayout --hp 807 --damage 104 ...` | Learns moved stat offsets after a game update, from the values on your character sheet (read-only). |
| `python game.py find-wallet --gold 1234` | Learns the gold counter's location from an exact gold amount (read-only). |
| `python game.py self-test` | Runs the built-in test suite. |
| `python game.py --version` | Shows the version. |

In the window you can switch **Observe only / Write**, **Start / Stop**, include or exclude each stat, and toggle the card picker. Closing the window leaves the watcher running; press **Stop** to end it.

### Level-up card priorities (per class)

Priorities live in [`card_priorities.json`](card_priorities.json), one list per class. Edit it freely; the trainer reads it for every popup.

- **`spells`** — new-spell cards, best first. Ships with a Paladin list (Consecration, Redemption, Holy Wrath, Holy Shock, Hammer of Justice, Divine Shield) and a Hunter list (Volley, Explosive Trap, Aimed Shot, Immolation Trap; any other Hunter spell is not ranked, so the rarest card is taken).
- **`upgrades`** — keywords for upgrade cards ("Improves a spell you know"), best first: area, cooldown, luck by default. A larger percentage wins a tie.
- **`known`** — extra spell names used only to recognise the class.
- **Class choice:** `auto` recognises the class from the spell names on the popup. Or pick one in the window's **Card class** box.
- If nothing on the list is offered, it takes the rarest card (legendary, then epic, then rare). It never rerolls and never skips.
- Other classes: add a block to the JSON (copy the Hunter one). A class without a list falls back to the rarest card for spell popups and the shared upgrade keywords.

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
- Only the stats listed in the window are managed. The wallet needs its offset learned on new builds (`find-wallet`).
- The card picker is new. Validate it with `cards-test` before relying on it, and report what the popup looks like if it misreads.
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
python game.py self-test    # 44 tests, no game required
```

CI runs the tests on Windows for every push. Technical research notes (memory layout, history of approaches) are in [docs/TECHNICAL_NOTES.md](docs/TECHNICAL_NOTES.md). See [CHANGELOG.md](CHANGELOG.md) for release notes.

## License

MIT. See [LICENSE](LICENSE).
