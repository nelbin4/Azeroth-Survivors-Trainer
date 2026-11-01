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


## Current build layout (executable SHA-256 8829953418A4D996CC5287F0E9FAD25407CA30CEB64D105A4F57833CAEF78016)

Learned automatically by the trainer and verified against the on-screen character sheet:

| Item | Location |
| --- | --- |
| Name header (capacity, pointer, length) | 792 bytes before max HP |
| Stat block | Unchanged from the previous build for offsets 0..112 (max HP, damage, haste, crit, crit damage, speed, pickup, XP, armor, regeneration, area, luck, gold bonus, extra targets, echo, kill heal, buff power, DoT stacks, DoT haste, level, current HP) |
| Modifier owner (capacity, pointer, length) | Moved from +968 to **+1040** |
| Gold wallet | Moved from +120 to **+124** (+120 now holds a sentinel). A separate, steadily rising counter at +904 mirrors lifetime gold and is not the wallet. |

Observations: extra targets is stored as the displayed value (+1 shows as 1). Extreme target and stack counts were applied in a hosted run and the game was still running at the next check; their long-term effect is untested and they may be heavy for the game.

## Guest games (why the trainer refuses them)

As a guest the character record on the local PC is not authoritative. Only a partial client copy exists (current HP and gold, other stats zero, player id 5), and it is refreshed from the network every tick. A write loop holding the wallet copy at 999,999 for 20 seconds changed nothing on screen, and the value returned to the host's number when the loop stopped. Searching memory for the stat sheet values found no matching block. Stats can only be changed while hosting or playing solo.

## Stats that are not plain fields (not covered)

Lives, thorns, lifesteal, vs elites and pet damage are not in the fixed stat block. Lifesteal and shielded are entries (kind byte plus float) in the modifier vector, which holds about six entries with room for two more. The game rebuilds that vector on level-ups and equipment changes, so appended entries do not persist; this is why replacing the vector was dropped. Locating lives would need a change to track (its value is 0).

## Screen reading findings

Kept here in case screen reading is ever extended.

- `PrintWindow` on the game window returns real pixels (OpenGL window, no black frame), with no focus change and no cursor movement.
- Windows' built-in OCR (`Windows.Media.Ocr`) runs fine from PowerShell. A persistent helper process avoids about a second of start-up per read; reads take roughly 130-350 ms. Output must be read as UTF-8: some in-game text is not cp1252.
- A file the recognizer has read stays locked briefly, so each capture needs its own filename.
- Small, dim text (the first row of the *Damage Done* panel) is skipped at native size and reads correctly from a 4x enlarged crop.
- Screens seen: main menu (class list, Zone, Collection, Talents, Achievements), co-op lobby (*Lobby code*, *Party*, *Open slot*, *A co-op run needs at least two players*), the level-up popup (*Level Up!*, three or four cards, per-card Banish, Skip and Reroll buttons, card hotkeys 1-4 and R), and an in-run *Damage Done* / *Party DPS* panel at the bottom right. The popup takes about 2 seconds to close after a pick.
- Automatic level-up card picking, built on these findings, was removed in 1.2.0 at the maintainer's request.

## Windows exe notes

The exe is a PyInstaller one-file launcher that bundles Python and the libraries and runs `game.py` from its own folder. A child process started from a bundled exe reuses the parent's temporary folder unless `PYINSTALLER_RESET_ENVIRONMENT=1` is set; without it the background watcher died as soon as the starting process exited. Tests that spawn `python -c` skip themselves inside the exe. Startup takes about 6 seconds.

## Game facts (from the game's public guides)

Free fan-made survivors-like by a developer called Aethus, based on a World of Warcraft 1.12.1 reimplementation; nine classes (Warrior, Paladin, Hunter, Rogue, Priest, Shaman, Mage, Warlock, Druid). Windows saves are under `%APPDATA%\Roaming\wenilla-rogue`. Co-op for up to five players exists in newer versions. The guides give no per-class spell or build advice.

## Modifier list findings (build 8829…8016, level 120 Hunter run)

The character's modifier list (capacity 8, length 7 at the time) was tested entry by entry: each value was set to a distinctive number, the Run Stats panel was read, and the value was restored immediately.

| Kind | Effect seen on the Run Stats panel |
| --- | --- |
| 17 | **Pet Damage** (1.0 shows as +100%; 4.44 showed +444%) |
| 19 | **Vs Elites** (0.15 shows as +15%) |
| 0, 1, 3, 4, 5 | No change on the Run Stats panel (they feed other values) |
| 14 (lifesteal), 22 (thorns) | Not present in this list. Lifesteal showed 0.5% and thorns 0% without any entry |

- **DoT stacks:** the record field at +68 is the bonus; the panel shows 1 + that value (3 showed as 4). While the watcher is not running the game resets the field after recalculations, which is why a panel screenshot can show the unmodified 4.
- **Lives:** shows 0. No record field or list entry was found that changes it. It may exist only after an item or talent grants a life.
- **Lifesteal, thorns, lives:** the only way to give them a value would be to insert new entries into the modifier list (one free slot existed). That was deliberately not done: the game rebuilds the list on level-ups and equipment changes, and writing a new entry's kind and the list length while the game may be appending to it risks corrupting game memory.

## Talent cap (migrated from talent_150.py)

The original script used Frida to rewrite three instructions in memory: `mov edi,0x33` (`bf 33 00 00 00`), `mov eax,0x33` (`b8 33 00 00 00`) and `mov r9d,0x33` (`41 b9 33 00 00 00`), changing 51 (0x33) to 150 (0x96). Its offsets (`0x5B7D447`, `0x5B7E4E4`, `0x5B8E8C1`, from module base) belong to executable SHA-256 `e8ff0335…8863`. The game has updated since: the build on 2026-11-01 is `71e5f176…9249`, none of the three offsets still holds the expected bytes, and the script refuses to run on it. The trainer keeps those offsets as the known sites for that old build.

The trainer now does the same patch without Frida: `VirtualProtectEx` to read/write/execute, `WriteProcessMemory` for the four constant bytes, protection restored, `FlushInstructionCache`. Sites are stored per build as an RVA plus the original instruction bytes; the constant's position is 1 byte in, or 2 when a REX prefix (`41`) precedes the opcode.

**Result on build 71e5f176 (2026-11-01).** With the talents screen open and a baseline of 51, the search stopped at its first write: `VirtualProtectEx` on the game's code page returned *Access denied* (error 5). The same calls succeed on an ordinary Python process (a same-bytes write test), so the refusal is specific to the game process. Nothing had been written at that point. About a minute later the game crashed with a heap-corruption error (0xC0000374, `ntdll`, fault offset 0x10d5f9); the cause was not determined. The stat watcher had not written to this game process since 2026-10-30, and the failed patch attempt wrote no bytes, so neither is an established cause. Next options: retry after a clean game start, or fall back to in-process patching (Frida), which the original script used on an older build.

**Correction and the search's removal (1.4.2).** The "Access denied" in the result above was not a protection problem. It was the *restore* step failing because the game had already crashed. A fresh game accepted the protection change (a same-bytes write succeeded), and the second run crashed again with the identical error (heap corruption 0xC0000374, `ntdll`, fault offset 0x10d5f9, at 17:06:57 and 17:11:03), each time within the first candidates. The candidates were chosen as every `mov reg,51` not preceded by a string-pointer load (282 raw, 122 filtered). The first three are loop and table bounds (`cmp ...,0x36b; jne; mov reg,0x33`), and even the only `mov eax,51; ret` functions (4 found) are the same table-probe pattern, so changing 51 to 150 there makes the game read past a buffer. The search was removed. Recognising the real cap would need a different method (for example finding what reads the talents screen's cap value) and the current build may not encode it as `mov reg,51` at all.

**Talent cap removed (1.5.0).** A static trace of the talents screen's text template (`{spent} of {earned} earned spent … {a}/{b}/{c} … {max} at most`, referenced from a single function at RVA 0x4C9643 in build 71e5f176) showed that `{max}` is a computed argument: the only `mov edx,0x33` in that function is the length of a string. A live read-only search for the on-screen numbers (`18, 17, 16` as consecutive 32-bit values) found only unrelated lists of small integers, with no 51 beside them, so the screen's numbers appear to be derived on the fly from per-talent ranks. With no stored value to edit and code patching shown to crash the game, the feature was removed. Possible future route: trace the callers of that function statically to find where `max` is produced.

## Talent cap: resolution (1.6.0) — supersedes the 1.4.x / 1.5.0 notes above

The earlier notes concluded that no safe change was possible. That was wrong. What actually happened and what works:

- **Why the earlier attempts crashed.** The cap is returned from about 20 inlined copies of one routine. The game rebuilds the talent screen constantly, so changing a single copy from 51 to 150 left it holding two different caps for an instant. Code that sizes a list from one value and fills it from the other writes past the end, giving the heap corruption (0xC0000374, `ntdll`, fault offset 0x10d5f9) seen twice at the first candidate. The "Access denied" in those runs was the restore step failing after the game had already died, not a protection problem.
- **What the cap sites are.** Talent points come from a loop in which each step adds 3 points and the running cost rises by 3 x 15 = 45 (0x2d) from a start of 80; when the running cost reaches **875 (0x36b)** the player has the 51-point cap and the routine returns 51. Each inlined copy is `cmp reg,0x36b ; jne loop ; mov reg,51` (for example `add r8d,0x2d ; add ecx,3 ; cmp r8d,0x36b ; jne ; mov ecx,0x33`). This is the same shape an earlier investigation of the browser build documented (it patched "cap-return immediates" found by exactly this signature). The build on 2026-11-01 (`71e5f176…`) has **20** copies, one per register choice; the older native build had 3.
- **Why a scan for any `mov reg,51` was misleading.** There are 282 of those; most are text lengths or other bounds. Only the ones after `cmp …,0x36b` and a branch are the cap.
- **Why memory searches found nothing.** The numbers on the talents screen (spent, per tree, to spend) are computed each time it draws, so no stored counter exists to find. The only thing to change is the code above.
- **What works.** Find the sites by scanning `.text` of the game file; check every site in the running game; pause the game (`NtSuspendProcess`) for a few milliseconds; write all sites with `VirtualProtectEx` / `WriteProcessMemory`; verify by readback; resume; roll back on failure. In the live test all 20 were patched in 17 ms including pre-checks. After reopening the Talents tab the screen read *Talents (148)* and *2 of 150 earned*; the "at most" text is a fixed label and still says 51.
- **Limits.** Memory only, so it must be re-applied each launch. A game update can change the pattern; the trainer then finds a different count or a mismatch and does nothing. Whether the game saves a build above 51 points across restarts is not tested here: an earlier investigation noted that a normal reload without the patch reduces an over-limit saved build to 51.

## Max talents through the save (1.7.0)

- **Talent table.** The game file holds one array of talent records, 176 (0xB0) bytes apart, in `.rdata`. Each record starts with three string slices (id, display name, icon; pointer then length) and carries a packed 32-bit field 13 words in: bytes are *column, row, maximum rank, 0*. Each tree is an unbroken run of records; the file has 27 runs (13 or 14 talents each) in class order 1, 2, 3, 4, 5, 7, 8, 9, 11 (Warrior, Paladin, Hunter, Rogue, Priest, Shaman, Mage, Warlock, Druid), three trees per class. Maximum points per class: Warrior 126, Paladin 121, Hunter 125, Rogue 127, Priest 127, Shaman 121, Mage 121, Warlock 122, Druid 128. The ordering was cross-checked against the talent ids already stored in saves for Paladin, Hunter, Shaman and Mage.
- **Save format.** `characters.txt` is `RSE1:` + base64(16-byte header, XOR ciphertext, 20-byte HMAC-SHA1 tag). The keystream block *n* is HMAC-SHA1(key, `ks` + header + n as u64), the tag is HMAC-SHA1(key, `tag` + header + ciphertext). The key is a constant in the game file that starts with a fixed prefix (stored plainly or XOR 0x36); a candidate is accepted only if it authenticates the player's own save. The plaintext is one record per line (`talent|<class>|<xp>|id:rank,...`, `spec|<class>|<active>|<preset1>|<preset2>|<preset3>`, and others).
- **What the game does on import.** Imported talents are kept at their maximum for the active allocation while the cap patch is on; the Spec 1 preset is trimmed to 51 points (the preset path uses a cap the patch does not change). Without the patch a load trims an over-limit build to 51.
- **Why the live ranks were not editable.** Five rounds of memory tracking (rank 2, 3, 4, 5, in 32-bit, 64-bit and decimal form, and beside the talent name) found no stored rank. The save route sidesteps that.

## Game build eb7447 (2026-11-01): layout found, first live write hung the game

- Stats block is 912 bytes after the name header (the text header is only an anchor; any well-formed text header works). Verified against the character sheet: +0 max HP, +4 damage, +12 crit, +16 crit damage, +20 speed, +24 pickup, +28 experience, +32 armor, +40 regen, +44 area, +52 gold bonus, +56 extra targets (u32), +60 echo, +116 current HP. A new field appears at +36; haste, luck, kill heal, buff power, DoT and the wallet were not confirmed.
- Level was found at -104 (1 at level 1, 2 and 4 on later reads); the player id (-144) is a persistent hero id, not per run. Stale copies of the record also exist (display copies, other addresses) and are not live.
- The modifier list (kinds 14/19/28) moved or changed encoding and was not found, so no source targets were written.
- First write test (watcher in write mode, targets 9999-style) ran about five minutes and 119 writes at level 9, then the game showed Responding = False. Cause not established (a bad write, or a field that must not hold these values, are the suspects). The layout was removed from the shipped table in 1.8.1.

### Follow-up (1.8.2): one stat at a time
- Test run, game responsive throughout: writes to Damage, Regeneration, Experience, Max Health and Current Health held for the seconds observed; Pickup, Crit, Crit Damage, Armor, Echo and Gold Bonus were put back almost at once (recalculated each tick). Later observation showed Damage itself is recalculated every couple of seconds, so no write is permanent.
- Rewriting Damage every 20 ms (490 writes in 10 s) kept it at the target; the sheet read +9999% during the loop. The shipped layout therefore pins fields in short bursts after each normal step (`hold`) and writes only Damage (`write_only`).
- The earlier hang then abort (`0xc0000409`, inside the game's own module) came from the first all-stats test. Not yet written on this build: area and extra targets (the likeliest culprits), and the stats listed above.

