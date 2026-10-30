# Azeroth Survivors Trainer

A Windows trainer for **Azeroth Survivors**. It finds your character in the game's memory by name, applies the stat values you choose without pausing or hooking the game, and can pick level-up cards for you from a priority list. It has a small window, a silent background mode, and a command line.

> **Use it responsibly.** Changing memory in a game can crash it, corrupt a run, or get you disconnected. Use it in solo runs or as host. It does nothing useful as a guest in someone else's game (the host owns your stats there). Do not use it to disrupt other players. This is an unofficial fan tool, not affiliated with the game's developers. No warranty; see [LICENSE](LICENSE).

## What it does

- **Stat trainer.** Keeps max/current HP, damage, crit, crit damage, speed, XP, regeneration, luck, gold and more at your targets. It checks level, name and ownership before every write, verifies by readback, and re-applies after level-ups.
- **No pausing, no debugger, no injection.** It uses ordinary read/write process memory access only.
- **Self-learning for new game versions.** If the game updates, it re-learns where your character lives (read-only) and only writes fields it could verify. A `relayout` command re-learns moved stats from your character sheet.
- **Level-up card picker (experimental).** Reads the popup from a background window capture with Windows' built-in text recognition, picks by your priority list, rerolls if nothing you want is offered, and presses the card's number key. Nothing is shown on or moved on your screen.
- **Safe by default.** The window opens in the mode you choose, every field can be switched off, and an observe-only mode never writes.

## Requirements

- Windows 10/11, 64-bit.
- Python 3.12 or newer with Tkinter (included in the standard installer).
- The game, installed. Run the trainer as administrator if the game runs elevated.

## Install

```bash
git clone https://github.com/nelbin4/Azeroth-Survivors-Trainer.git
cd Azeroth-Survivors-Trainer
pip install -r requirements.txt
```

## First run

```bash
python game.py
```

The window asks for two things once: the path to `AzerothSurvivors.exe` and your character name exactly as shown in game. (Or use `python game.py setup --game "C:\path\to\AzerothSurvivors.exe" --name YourCharacter`.) Your settings are stored in `session.json` next to the program and are never uploaded.

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

### Level-up card priorities

Defaults (edit `CARD_SPELLS` and `CARD_UPGRADES` in `game.py`):

- **New spell cards:** Consecration, Redemption, Holy Wrath, Holy Shock, Hammer of Justice, Divine Shield.
- **Upgrade cards:** area of effect, then cooldown, then luck, then everything else.
- If nothing on the list is offered, it rerolls (up to 8 times per level-up), then takes the first card.

## How it stays safe

1. Finds the game by exact executable path and checks its SHA-256. An unknown build triggers read-only learning, never blind writes.
2. Identifies your character by its length-checked name, then validates player ID, level, plausible stats and the modifier table.
3. Backs up original bytes before the first write, checks ownership before each write, and verifies by readback. On failure it conditionally restores only bytes it wrote.
4. A single odd reading never resets state; a level drop must be seen twice before the run is treated as new, and writes pause meanwhile.
5. If the character is ambiguous (several valid copies), it picks the one that actually changes, or refuses to write.

## Limitations

- **Host or solo only.** As a guest, your PC holds display copies refreshed from the host every tick; writes there have no effect.
- A new game build's learned layout is only trusted after three consistent samples. If stat offsets themselves move, run `relayout`.
- Only the stats listed in the window are managed. The wallet needs its offset learned on new builds (`find-wallet`).
- The card picker is new. Validate it with `cards-test` before relying on it, and report what the popup looks like if it misreads.
- Values apply in periodic corrections (about every half second), not atomically, so the display can briefly dip after a recalculation.

## Troubleshooting

- **"Not configured yet"** — run the window once, or `setup`.
- **State stays "seeking record"** — you are probably in a lobby or a guest game, or the name does not match exactly.
- **State "blocked"** — an unrecognised build is still being learned; stay in a run and level up once.
- **Nothing happens** — run as administrator if the game is elevated.
- Errors from the window are written to `gui-error.log`.

## Development

```bash
python game.py self-test    # 32 tests, no game required
```

CI runs the tests on Windows for every push. Technical research notes (memory layout, history of approaches) are in [docs/TECHNICAL_NOTES.md](docs/TECHNICAL_NOTES.md). See [CHANGELOG.md](CHANGELOG.md) for release notes.

## License

MIT. See [LICENSE](LICENSE).
