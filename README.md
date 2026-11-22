# Azeroth Survivors Trainer

A standalone Rust trainer for the original Azeroth Survivors fan game on Windows x64.

For the new **Azeroth Survivors: Reborn**, see the separate
[Azeroth Survivor Reborn Trainer](../azeroth-survivors-reborn-trainer/README.md).

## Get started

1. Download `AzerothSurvivorsTrainer.exe` from the [latest release](https://github.com/nelbin4/Azeroth-Survivors-Trainer/releases/latest).
2. Open the trainer. On first use, choose **Locate game** and select `AzerothSurvivors.exe`.
3. Start a solo run or a game you host.
4. Press **Find** to start the watcher. Once you are in a run, the trainer asks you to open the character panel. Press **C** and keep it open until the stats are found.

Use the trainer in solo runs or games you host. Gameplay testing covered solo runs.

The watcher reads your screen with **OCR** and uses no memory-read mode. It works in three states:

- **Lobby:** anything that is not in-game shows "Lobby detected. Enter a run to begin."
- **In-game, stats not found:** the window shows "Keep Character Panel Open (Press C) until we find the stats" in large text. The text pulses yellow every 2 seconds.
- **Stats found:** the window shows the stat cards. Each verified stat starts freezing as soon as it is available.

The trainer detects a run by the Kills, Gold, and cache counters in the top-left corner of the game. If the game's layout changes beyond what the watcher can verify, it keeps retrying.

## Supported stats

| Stat | Target |
| --- | ---: |
| Damage | +9999% |
| Area | +9999% |
| Luck | +9999% |
| Gold Bonus | +9999% |

The stat cards always show Damage, Area, Gold Bonus, and Luck in that order once any stat is found. A stat that has not been found yet shows "-". While Gold Bonus or Luck is missing, the window suggests trying an upgrade so the trainer can see the difference.

The watcher starts rewriting verified stats every 0.5 seconds and keeps using the same record through level-ups. The game's recalculation can briefly reset a value before the next rewrite.

## Updates and troubleshooting

- **Check updates** checks GitHub. When a newer Rust release is available, **Download & Install** verifies and installs the executable and restarts the trainer.
- **Keep Character Panel Open (Press C)** means you are in a run and the watcher is waiting for the character panel. Keep it open until the stats appear. On an unfamiliar build, continue gaining experience or level up during discovery.
- **Lobby detected** means the game is not in a run. Enter a run and the watcher continues automatically.
- The watcher's settings and `run.log` are stored under `%LOCALAPPDATA%\Azeroth Survivors Trainer`. `run.log` keeps the newest 10 KB of timed events: startup, OCR panel reads with in-game state, memory scan and verification timings, and discovery warnings. Attach it when you report a problem.

The [Python v2.1.3 release](https://github.com/nelbin4/Azeroth-Survivors-Trainer/releases/tag/v2.1.3) remains available for users of the legacy trainer.

## Development

The [Build Rust trainer workflow](.github/workflows/build-rust-trainer.yml) runs the Rust tests and builds the Windows x64 executable. Each release uses the successful workflow artifact from its release commit.

### Project layout

| Path | Contents |
| --- | --- |
| `src/` | Rust GUI, game discovery, stat watcher, settings, and updater |
| `assets/` | Trainer icons |
| `docs/legacy/` | [Historical trainer research](docs/legacy/TECHNICAL_NOTES.md) |
| `releases/` | Local Windows executable and archived builds |
| `local/` | Local tools, research, and legacy data |

Cargo reads `Cargo.toml`, `Cargo.lock`, and `build.rs` at the repository root. Git ignores `releases/` and `local/`.

This is an unofficial fan tool. See the [MIT license](LICENSE).
