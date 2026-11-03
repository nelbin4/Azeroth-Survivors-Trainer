# Azeroth Survivors Trainer

An experimental Windows trainer for the free fan game Azeroth Survivors.

**Use it only in a solo run or a game you host. Never when joining someone else's game.** Joined co-op has not been tested.

## Get started

1. Download `AzerothSurvivorsTrainer-<version>.zip` from the [Releases page](https://github.com/nelbin4/Azeroth-Survivors-Trainer/releases) and unzip it. Keep `game.py` next to the exe.
2. Start the Windows version of the game and begin a solo run, or host one.
3. Double-click `AzerothSurvivorsTrainer.exe`. The first time, select `AzerothSurvivors.exe`.

The window shows the status. Press **Stop** to end the trainer, or close the game. Closing the window alone leaves the trainer running.

## What it does

- **Stats:** Damage, Area, Luck and Gold Bonus are set to their maximums and kept there. The trainer re-applies them after each level-up.
- **Talent cap:** **Raise talent cap to 150** raises the game's talent point limit from 51 right away, and again each time the game starts. **Restore 51** puts it back.
- **Max talents:** **Max Talents** creates a save with every talent at its maximum for one class or all classes. Your current save is backed up first.
- **Updates:** at startup the window checks GitHub. If a newer version exists, an **Update** button appears. Nothing installs until you press it.

## Good to know

- Only Damage, Area, Luck and Gold Bonus are applied on this build. Other stats are not applied yet.
- The game shows new talent points after you refresh the Talents page.
- The exe is **unsigned**, so Windows SmartScreen or antivirus may warn about it. This is normal for a program that reads another program's memory.
- Changing a game's memory is never risk-free. The trainer checks before every change, but a game can still crash. Use it at your own risk.
- Settings and logs are kept in the trainer folder (`session.json` and `trainer.log`). Your game saves are never changed directly.

## Max talents: using the save

1. Click **Max Talents**, pick a class (or *All classes*), and press **Create save**.
2. The new file is in the game's `Saves` folder. Click **Open Saves folder** to find it.
3. In the game, import the file with **Import Save**.

To undo, import the `AzerothSurvivors-before-max-talents-...sav` backup the same way.

## Troubleshooting

- **"Waiting for the game"**: start the game and begin a run. The trainer waits for a run.
- **"Game not set up yet"**: press **Locate game** and select `AzerothSurvivors.exe`.
- **Nothing happens**: run the trainer as administrator if the game runs as administrator.
- **The exe does not start**: make sure `game.py` is in the same folder as the exe, and redownload the zip if it is missing.
- **Errors**: check `trainer.log` in the trainer folder.

*This is an unofficial fan tool, not affiliated with the game or its developer. See [LICENSE](LICENSE).*

## Development

Run the tests (no game needed):

```bash
python game.py self-test
```

Build the exe and release zip:

```bash
pip install pyinstaller
python build_exe.py
```

The release zip contains only `AzerothSurvivorsTrainer.exe` and `game.py`. Release history is in [CHANGELOG.md](CHANGELOG.md). Technical notes are in [docs/TECHNICAL_NOTES.md](docs/TECHNICAL_NOTES.md).

## License

MIT. See [LICENSE](LICENSE).
