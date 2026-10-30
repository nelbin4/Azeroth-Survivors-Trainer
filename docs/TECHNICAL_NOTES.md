# Azeroth Survivors Trainer — technical notes

Research log and operating notes written while building the trainer. Some sections describe historical approaches that were removed (process pausing, debugger helpers). The top-level [README](../README.md) is the user guide.

# Current CLI guide



Updated 30 October 2026, Asia/Shanghai. Workspace: `<project folder>`.



The supported workflow uses Python and PyMemoryEditor against the native Windows game. It identifies the character by name and ownership, then applies supported changed values without pausing the game, attaching a debugger, or replacing the modifier array. Squalr is not used.



## Start the automatic watcher

Run once in PowerShell from this folder:

```powershell
python .\game.py auto --background
```

The watcher runs for eight hours by default. It waits for the configured executable, validates its build and character identity, and reacquires supported fields after a new run or process. You do not need to issue a command for every match. Repeating the command reuses a healthy existing watcher. After restarting Windows, run the command again; no startup task is installed.

```powershell
python .\game.py status
python .\game.py new-game # Read-only current character report
python .\game.py stop
python .\game.py auto --background --seconds 3600
python .\game.py self-test
python .\game.py relayout --hp 807 --damage 104 ... # read-only: learn moved stat offsets from the character sheet
python .\game.py find-wallet --gold 1234         # read-only: learn the wallet offset from an exact gold amount
python .\game.py auto --background --trace-only  # observe only, no game writes
```

`new-game --apply` also starts/reuses this watcher. `--seconds` accepts 1–86400. The separate read-only `new-game --scan-seconds` fallback accepts 1–60. The configured character is `<character>`. Administrator access may be required when the game is elevated.

Application waits for fresh gameplay evidence: level gain, gold gain, or damage with unchanged maximum HP. Level drops restart that wait. After 120 seconds without detected activity, writes wait for activity again. This is a gameplay heuristic, not direct detection of lobby or victory screens. Repeated ownership/read failures return to discovery; process loss returns to waiting. Unknown builds and ambiguous candidates block writes.

The automatic route uses no suspension, debugger or injected hooks. The old pausing/legacy routes (`start`, `_worker`, `apply-host`) were removed after earlier disconnects.

## New game versions (self-learning)

A game update changes the executable hash. The watcher no longer just blocks:

1. It scans for the exact character name header and tries each name-to-stats distance (24..4096 bytes) and each modifier-vector owner offset (800..1400). The owner is the one table holding the kinds it edits (14 and 28). Every candidate must pass the full strict actor check.
2. It saves the layout under `learned_builds[<hash>]` in `session.json` only after three consistent samples at least 10 seconds apart with level never dropping. Later runs and restarts of that build reuse it.
3. Fields it cannot verify are never written. Example on build `8829...8016`: the wallet moved to +124 (confirmed by a rising counter matched to on-screen gold readings; +904 holds a separate counter), so offset +120 is skipped.
4. If individual stat offsets move, run `relayout` with the stats shown on the character sheet. It records per-field offsets for that build; fields it could not see are dropped when any observed field moved.
5. Anchors must still hold: player ID at -24, level at +104, max/current HP at +0/+112. If they move, the watcher stays read-only.

Learned layout for build `8829953418a4d996cc5287f0e9fad25407ca30ceb64d105a4f57833caef78016`: name header at record-792, stats unchanged from the old build, modifier owner at +1040, wallet at +124.

Two copies of a record can exist. When several validate, the watcher samples them and picks the one whose level, HP or gold actually changes. If nothing moves it stays blocked as ambiguous.

## Applied settings



| Field | Target |

| --- | --- |

| Current/max HP | 9999 / 9999 |

| Damage bonus | +9999% |

| Crit chance / crit damage | 9999% / 9999% |

| Haste / armor | Displayed 100% / 100% |

| Regeneration / kill heal | 9999 / 9999 |

| Luck / gold bonus / buff power / DoT haste | +9999% |

| Gold wallet | 999999 |

| Experience bonus | **+200%** — multiplier 3.0 |

| Speed bonus | **+50%** — multiplier 1.5 |

| Existing lifesteal source | Approximately 9999%; other additive sources may raise the display |

| Existing shielded source | +9999% |



Pickup, area, extra targets, echo and DoT stack count are left unchanged. Thorns and lives are not added. The updater preserves the modifier vector's capacity, pointer, length and kind sequence; it edits the existing kind-14 and kind-28 values only.



The updater checks every half second and writes changed supported fields. Level-ups/equipment recalculation can briefly restore normal values before the next check. This is periodic correction, not an atomic change or perfectly constant display. Combat effects of every field have not been independently established.



## Efficiency observations

Recent changes: session writes are batched (every 10 s or on a state change), polling is adaptive (0.4 s applying, 1 s waiting for progress, 2 s without a game), a single lower level reading no longer counts as a run reset (writes pause until a second reading confirms), fallback scans start near the last known address, and hints are kept per build. `session.json` keeps 64 run backups; older ones move to `session.backups-archive.json`, and large old diagnostics to `session.diagnostics-archive.json`. Nothing was deleted.

An earlier five-second sample of the automatic watcher measured 1.9% of one logical CPU core, 50.41 MiB resident memory, and approximately 0.418 MiB/s each of process read/write I/O. These short observations are not peak or long-term guarantees; Windows process I/O includes more than filesystem activity. Evidence is in `auto_performance_observation`.

A Windows event handles stop checks without parsing the session JSON every half second. Heartbeats update about every five seconds; backups and heartbeats still read/write the session. Executable hashing is streamed and cached per process/file identity. Name hints avoid a full scan when valid. Fallback discovery scans writable non-executable memory incrementally, with a cooperative slice budget and cancellation checks. Individual chunks may exceed that budget. It does not prove uniqueness across unscanned memory.

## How tracing and guards work



1. Find the process by exact executable path and check its SHA-256.

2. Treat prior locations as hints; read the complete, length-checked character name from memory.

3. Validate player ID, level, plausible stats, and owned modifier capacity/pointer/length/kinds.

4. Retry a small number of transient read inconsistencies caused by record movement.

5. If hints fail, search for matching name headers in readable/writable memory and validate their associated records. Empty/ambiguous results prevent application.

6. Back up original bytes before application, check ownership before each write and verify readback.

7. Reapply changed supported fields after recalculation; recover discovery after repeated failures, wait after process loss or level reset, and exit on explicit stop or time limit.

An injected-overheal exception is restricted to the exact previously verified process generation, player ID and actor address that this updater wrote. Unknown actors retain strict HP validation. Every write still checks level, name, ownership and readback. Missing or duplicate lifesteal/shielded source kinds are skipped rather than invented.



Backups are per observed owner/level in the current updater. Failure recovery conditionally restores only bytes still equal to the updater's written value and only while ownership remains valid. Recovery is best effort; it is not atomic with the game's threads. Stopping does not blindly restore old HP or stats from a previous level.



Windows kernel mutexes serialize controlling commands, session changes and writers. Session replacement flushes data and retries sharing errors. `new-game` without `--apply` performs a read-only trace and leaves the existing writer active. Trace history retains the last 32 reports.



Some modifiers have a low-byte kind followed by three bytes that varied in observations, then a float at entry+4. The current name-guarded updater compares kind bytes and ownership; it does not interpret the varying upper bytes as stable semantic enum values. Legacy full-word fingerprint guards were more conservative and sometimes stopped unnecessarily.



## Discovery that changed the approach



A hardware-watchpoint positive-control test on a disposable Python process captured the expected read and detached successfully. It did not touch the game.



In the paused level-64 victory state, a game watchpoint captured SIMD accesses. Disassembly and eight bounded function-entry observations then confirmed **bidirectional player-record swaps**: our player ID moved to another slot and back while exchanging IDs with other players. A fixed address can therefore briefly hold another player. Historical temporary destinations must not be written later.



The same object held a Rust-style name header containing the exact six-byte string `<character>`. This provided an identity anchor independent of displayed stat values. The swap entry's first 32 bytes occurred once in the inspected executable. The observed swaps explain transient address/owner mismatches; they do not alone prove which calculation resets core fields.



A small lifesteal source test changed 1.4% to 11.4% on screen and survived progression from level 8 to 18. Later equipment/source rebuilding restored original values, so the newer updater rechecks current ownership and reapplies only changed fields. A source-only health/damage experiment did not establish a reliable primary-stat source mapping; those guesses are not the current method.



A non-pausing HP-only probe held 9999/9999 for six seconds. The later bounded core updater reapplied settings across levels 37–42. A background run showed supported max combat stats, 9999/9999 HP, +200% experience and +50% speed on screen. A subsequent new run was freshly resolved at level 1 with a new modifier allocation. A four-second sample spanning levels 16–17 matched HP/XP/speed targets in 27 of 31 samples; the others were around recalculation.



Read-only `new-game` was tested while the writer remained active and reported level 44, 9999/9999 HP, wallet 999999, +9999% damage, 9999% crit damage, +200% XP and +50% speed. Thirty unit tests pass (including the layout learner, remapping and duplicate resolution), plus the separate hardware-watchpoint positive-control test.



## Build-specific layout



Executable: `<game folder>\AzerothSurvivors.exe`.



SHA-256: `DE7147A2A3A01B4FB84F3A7F036BC940F9E7D4FDFC42A7FDB79F3F1B2BD798BE`.



Name header is at object+0x1b0: three u64 values, capacity/pointer/UTF-8 length. Max-health field is object+0x4c8. Offsets below are from that max-health field:



| Offset | Type and meaning |

| --- | --- |

| -24 | u32 player ID |

| 0 / 4 / 8 / 12 / 16 | f32 max HP / damage multiplier / cooldown factor / crit fraction / crit-damage multiplier |

| 20 / 24 / 28 / 32 / 36 | f32 speed multiplier / pickup / XP multiplier / armor fraction / regeneration |

| 40 / 44 / 48 | f32 area multiplier / luck fraction / gold multiplier |

| 52 / 56 / 60 / 64 | u32 extra targets / f32 echo / f32 kill heal / f32 buff power |

| 68 / 72 | u32 DoT stack bonus / f32 DoT haste |

| 104 / 112 / 120 | u32 level / f32 current HP / u32 wallet |

| 968 | Three u64s: modifier capacity, pointer and length |



Observed modifiers occupy eight-byte entries, with kind in the first byte and f32 value at +4. The current workflow uses existing kinds 14 (lifesteal) and 28 (shielded). Historical native tests also associated 22 with thorns, 18 controlled-target damage, 20 execute, 21 afflicted damage, 16 melee, and 19 elite/boss damage; the current updater does not insert those entries.



## Limits and unresolved work



- Live evidence supports the inspected builds and hosted/solo runs. **Guest co-op:** tested 30 October 2026 on build `8829...8016`. No valid character record exists on a guest's PC. A client-side copy of current HP and gold (id 5, other stats zero) is refreshed from the host every tick. A write held at 999,999 for 20 seconds changed nothing on screen, and the value fell back when the loop stopped. Stats can only be changed while hosting or solo.

- Live automatic progression from waiting at level 1 to applying at levels 17 and 34 has been observed. Tests simulate new runs, process restart/PID reuse, ambiguity, recovery and stop handling. A real executable restart/PC restart has not yet been validated; fallback discovery can be incomplete or fail to find a unique candidate.

- Unknown executable hashes stop application. Signatures/layouts must be learned and validated after a game update.

- A name scan marked incomplete does not establish uniqueness across every memory region. Conflicting candidates prevent application.

- High memory readback or a character-sheet number does not prove every combat effect. Long-term network reliability has not been established.

- Earlier process-pausing methods coincided with disconnects; causality was not proved. They remain disabled. Do not re-enable them for a networked run.

- The current updater corrects recalculation after up to roughly one poll interval plus processing time; it does not prevent recalculation itself.

- There is no supported current-build talent-cap patch or RSE1 decryption feature in the normal command path.



## Diagnostics and historical commands



These are investigation tools, not the normal per-game workflow. Commands with arguments accept `--help`.



| Commands | Purpose |

| --- | --- |

| `resolve-name`, `discover-name` | Manual name resolution / bounded name scan |

| `named-core` | Lower-level non-pausing updater; legacy lower-level command; the normal workflow uses `auto` |

| `source-apply`, `lifesteal-test` | Bounded existing-source tests; not an all-stat solution |

| `discover`, `learn`, `adopt` | Numeric structure discovery and pointer-path training; no cross-process path has yet earned validation |

| `numeric-scan`, `numeric-watch` | Value refinement and bounded hardware watches |

| `text-scan`, `text-watch` | Display-text search / first page-access observations |

| `capture` | Historical character-sheet signature probe; recorded zero guest hits and later a stalled helper; not the preferred co-op path |

| `inspect-copy`, `observe-swaps` | Read-only/bounded investigation of the recorded swap site |

| `watch-self-test` | Hardware-watchpoint positive control in a disposable process |

| `relayout`, `find-wallet` | Read-only learning of moved stat offsets / the wallet offset for a new build |
| `start`, `apply-host` | Removed. These paused the game and are no longer available |



Numeric scanning may produce many cached/temporary values and incomplete coverage. Guest investigations found 249.3 copies without accesses during short watches; that did not prove guest stats were absent from memory. Text monitoring caught a neighboring VCRUNTIME buffer write, not the numeric producer. Pointer paths/signatures are candidates until independently validated; confidence is not earned by selecting an address manually.



The normal updater uses PyMemoryEditor for memory access. Frida is optional for diagnostic commands, which inject instrumentation and require cleanup. Hardware/page monitoring and legacy debugger helpers are not used by `new-game --apply`.



## Four files and saved evidence



| File | Role |

| --- | --- |

| `game.py` | Current tracing, updater, guards and diagnostic CLI |

| `session.json` | Profile, current runtime, original-byte backups, traces, evidence and performance sample |

| `README.md` | Current operating instructions |
| `session.backups-archive.json`, `session.diagnostics-archive.json` | Older run backups and large old diagnostic traces moved out of `session.json` (nothing deleted) |

| `recovery.zip` | Historical scripts/guides/traces/save backups; current-status notices identify obsolete instructions |



Important session evidence includes `native_identity_recipe`, `name_discovery`, `swap_observation`, `paused_run_snapshot`, `watch_self_test`, `lifesteal_source_test`, `last_named_core`, `named_core_readback`, `current_run_sampling`, `trace_history` and `performance_observation`. Session history and backups can grow; no destructive compaction was performed during this documentation refresh.



All five archived Markdown guides now have a current-workflow notice. Their exact pre-refresh bytes, and the previous workspace README, are preserved inside the ZIP in a `documentation-originals-2026-10-28-*` directory with `.original` suffixes. Non-Markdown archive contents were SHA-256 checked unchanged, and the rebuilt ZIP passed CRC verification. Extract historical material into a separate folder before reviewing it; obsolete scripts may contain old absolute addresses and pausing methods.



## Game and save locations



Native progress was previously observed at `%APPDATA%\wenilla-rogue\characters.txt`, beginning with `RSE1:`. Automatic/export backups are under the adjacent `Saves` directory. `WOW_ROGUE_SAVE` is an optional override and was unset when inspected.



`AzerothSurvivors\benilla-config` holds UI/graphics/chat/camera/name-cache settings; its `saved-variables.lua` is not the character progression record. `Data\patch*.MPQ` are game archives. Earlier browser save investigations and historical RSE1 recovery work remain in `recovery.zip`; they are not verified current-save editing instructions.



These changes are runtime memory updates. Persistence in a save or across executable restarts is not implied. No game installation, AppData save, or Squalr dependency was changed by the documentation refresh.


## Name-free character detection (tried, rejected)

Scanning memory for the stat block by structure alone (player ID, level, plausible stats, modifier table) was tried on a live game in the main menu. It found many records, not only the player: enemies and stale records from earlier runs share the same structure (for example max health 22.4 at level 1, IDs 33 and 166). It also took about 53 seconds per scan. A character is distinguished by the name header stored next to its stats, so automatic detection keys on the presence of a valid name header, not on a configured name. That scan takes about 17 seconds and needs no name from the user.
