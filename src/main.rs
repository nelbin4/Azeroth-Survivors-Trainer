#![windows_subsystem = "windows"]
#![deny(unsafe_op_in_unsafe_fn)]

mod diagnostic_log;
mod discovery;
mod game;
mod hud;
mod settings;
mod update;

use settings::Settings;
use std::{
    env,
    os::windows::ffi::{OsStrExt, OsStringExt},
    path::{Path, PathBuf},
    ptr::{null, null_mut},
    sync::{
        atomic::{AtomicBool, Ordering},
        mpsc::{self, Receiver, TryRecvError},
    },
    time::{Duration, Instant},
};
use windows_sys::Win32::{
    Foundation::{HWND, LPARAM, LRESULT, RECT, WPARAM},
    Graphics::{
        Dwm::{DWMWA_USE_IMMERSIVE_DARK_MODE, DwmSetWindowAttribute},
        Gdi::{
            CreateFontW, CreateSolidBrush, DC_BRUSH, DEFAULT_GUI_FONT as UI_FONT, DT_CENTER,
            DT_END_ELLIPSIS, DT_NOPREFIX, DT_RIGHT, DT_SINGLELINE, DT_VCENTER, DeleteObject,
            DrawFocusRect, DrawTextW, FillRect, GetStockObject, HFONT, SelectObject, SetBkMode,
            InvalidateRect, SetDCBrushColor, SetTextColor, TRANSPARENT,
        },
    },
    System::{
        LibraryLoader::GetModuleHandleW,
        SystemServices::{SS_CENTER, SS_NOPREFIX, SS_OWNERDRAW, SS_RIGHT},
    },
    UI::{
        Controls::{
            DRAWITEMSTRUCT,
            Dialogs::{GetOpenFileNameW, OFN_FILEMUSTEXIST, OFN_PATHMUSTEXIST, OPENFILENAMEW},
            ODS_DISABLED, ODS_FOCUS, ODS_SELECTED, ODT_STATIC,
        },
        HiDpi::{AdjustWindowRectExForDpi, GetDpiForSystem},
        Input::KeyboardAndMouse::{GetFocus, IsWindowEnabled, SetFocus},
        Shell::ShellExecuteW,
        WindowsAndMessaging::*,
    },
};

const TITLE: &str = "Azeroth Survivors Trainer";
const CHARACTER_PANEL_PROMPT: &str =
    "Open the Character Panel (press C) until the trainer finds the value.";
const CHARACTER_PANEL_PROMPT_LARGE: &str =
    "Keep Character Panel Open\r\n(Press C)\r\nuntil we find the stats";
const LOBBY_MESSAGE: &str = "Lobby detected.\r\nEnter a run to begin.";
const GLOW_INTERVAL: Duration = Duration::from_secs(2);
// Toggled every GLOW_INTERVAL to make the in-game prompt pulse yellow.
static PROMPT_GLOW: AtomicBool = AtomicBool::new(false);
const PROCESS_CHECK_INTERVAL: Duration = Duration::from_millis(1500);
// Indexes into discovery::TARGETS, which is Damage, Area, Luck, Gold Bonus.
const DISPLAY_ORDER: [usize; 4] = [0, 1, 3, 2];
const PANEL_PROMPT_HEIGHT: i32 = 360;
const REPO: &str = "https://github.com/nelbin4/Azeroth-Survivors-Trainer";
const START: u16 = 100;
const LAUNCH: u16 = 101;
const CHECK_UPDATES: u16 = 103;
const GITHUB: u16 = 104;
const PRIMARY_LAUNCH: u16 = 107;
const STATUS_DOT: u16 = 108;
const READY_DOT: u16 = 109;
const DIVIDER: u16 = 110;
const LOCATE: u16 = 111;
const STOP: u16 = 112;
const MUTED_TEXT: u16 = 113;
const STAT_CARD: u16 = 115;
const PANEL_PROMPT: u16 = 118;
const LOBBY_TEXT: u16 = 119;
const PROCESS_LOOKUP_LIMIT: u8 = 10;
const BACKGROUND: u32 = rgb(22, 23, 30);
const TEXT: u32 = rgb(242, 241, 247);
const MUTED: u32 = rgb(172, 175, 192);
const SECONDARY: u32 = rgb(38, 40, 53);
const LIVE: u32 = rgb(152, 222, 184);
const BORDER: u32 = rgb(54, 56, 72);
const WINDOW_STYLE: u32 = WS_CAPTION | WS_SYSMENU | WS_MINIMIZEBOX;

const fn rgb(red: u32, green: u32, blue: u32) -> u32 {
    red | (green << 8) | (blue << 16)
}

fn wide(value: impl AsRef<std::ffi::OsStr>) -> Vec<u16> {
    value.as_ref().encode_wide().chain(Some(0)).collect()
}

#[derive(Clone, Copy, Default, PartialEq, Eq)]
enum DiscoveryPhase {
    #[default]
    Searching,
    WaitingForPanel,
    Freezing,
    Retrying,
}

enum UpdateControl {
    Ready,
    Checking(Receiver<Result<update::ReleaseStatus, String>>),
    Latest,
    Available(update::Release),
    Installing(update::Release, Receiver<Result<(), String>>),
}

struct Window {
    settings: Settings,
    children: Vec<Control>,
    render_cursor: usize,
    rendered_height: i32,
    dpi: i32,
    font: Font,
    heading_font: Font,
    prompt_font: Font,
    ocr: hud::OcrWorker,
    next_process_check: Instant,
    glow_changed: Instant,
    watching: bool,
    solo_character: Option<String>,
    running: bool,
    game_instance: Option<game::ProcessInstance>,
    process_lookup_misses: u8,
    build: Option<String>,
    discovery: Option<discovery::Job>,
    discovery_phase: DiscoveryPhase,
    active_stats: discovery::ActiveStats,
    discovery_message: String,
    next_discovery: Instant,
    reading: hud::Reading,
    capture: Option<(u64, Receiver<Result<hud::Reading, String>>)>,
    scan_generation: u64,
    next_capture: Instant,
    capture_error: Option<String>,
    empty_reads: u8,
    message: String,
    updater: UpdateControl,
    update_message: String,
}

struct Control {
    hwnd: HWND,
    class: String,
    style: u32,
    rect: [i32; 4],
    id: u16,
    label: String,
}

struct Font(HFONT);

impl Font {
    fn new(dpi: i32, pixels: i32, weight: i32) -> Self {
        let face = wide("Segoe UI");
        Self(unsafe {
            CreateFontW(
                -(pixels * dpi + 48) / 96,
                0,
                0,
                0,
                weight,
                0,
                0,
                0,
                1,
                0,
                0,
                5,
                0,
                face.as_ptr(),
            )
        })
    }

    fn handle(&self) -> HFONT {
        if self.0.is_null() {
            unsafe { GetStockObject(UI_FONT).cast() }
        } else {
            self.0
        }
    }
}

impl Drop for Font {
    fn drop(&mut self) {
        if !self.0.is_null() {
            unsafe { DeleteObject(self.0.cast()) };
        }
    }
}

impl Window {
    fn scaled(&self, pixels: i32) -> i32 {
        (pixels * self.dpi + 48) / 96
    }

    fn child(
        &mut self,
        parent: HWND,
        class: &str,
        label: &str,
        style: u32,
        rect: [i32; 4],
        id: u16,
    ) -> HWND {
        let index = self.render_cursor;
        self.render_cursor += 1;
        let rect = rect.map(|n| self.scaled(n));
        if let Some(control) = self.children.get_mut(index) {
            if !control.hwnd.is_null()
                && control.class == class
                && control.style == style
                && control.rect == rect
                && control.id == id
            {
                if control.label != label {
                    unsafe { SetWindowTextW(control.hwnd, wide(label).as_ptr()) };
                    control.label = label.to_owned();
                }
                return control.hwnd;
            }
            if !control.hwnd.is_null() {
                unsafe { DestroyWindow(control.hwnd) };
            }
        }
        let [left, top, width, height] = rect;
        let child = unsafe {
            let child = CreateWindowExW(
                0,
                wide(class).as_ptr(),
                wide(label).as_ptr(),
                WS_CHILD | WS_VISIBLE | style,
                left,
                top,
                width,
                height,
                parent,
                id as usize as _,
                GetModuleHandleW(null()),
                null(),
            );
            if !child.is_null() {
                SendMessageW(child, WM_SETFONT, self.font.handle() as usize, 1);
            }
            child
        };
        let control = Control {
            hwnd: child,
            class: class.to_owned(),
            style,
            rect,
            id,
            label: label.to_owned(),
        };
        if index == self.children.len() {
            self.children.push(control);
        } else {
            self.children[index] = control;
        }
        child
    }

    fn button(&mut self, parent: HWND, label: &str, rect: [i32; 4], id: u16) {
        self.child(
            parent,
            "BUTTON",
            label,
            WS_TABSTOP | BS_OWNERDRAW as u32,
            rect,
            id,
        );
    }

    fn render(&mut self, hwnd: HWND) {
        let previous_focus = unsafe { GetFocus() };
        let focused_id = self
            .children
            .iter()
            .any(|control| control.hwnd == previous_focus && !previous_focus.is_null())
            .then(|| unsafe { GetDlgCtrlID(previous_focus) });
        self.render_cursor = 0;

        let active_count = self.active_stats.iter().filter(|&&active| active).count() as i32;
        // All four stat cards (two rows) show once any stat is found.
        let rows = if active_count == 0 { 0 } else { 2 };
        let height = if self.shows_panel_prompt() {
            PANEL_PROMPT_HEIGHT
        } else if rows == 0 {
            228
        } else {
            254 + rows * 62
        } + if self.update_message.is_empty() {
            0
        } else {
            38
        };
        let mut outer = RECT {
            left: 0,
            top: 0,
            right: self.scaled(500),
            bottom: self.scaled(height),
        };
        if self.rendered_height != height {
            unsafe {
                AdjustWindowRectExForDpi(&mut outer, WINDOW_STYLE, 0, 0, self.dpi as u32);
                SetWindowPos(
                    hwnd,
                    null_mut(),
                    0,
                    0,
                    outer.right - outer.left,
                    outer.bottom - outer.top,
                    SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE,
                );
            }
            self.rendered_height = height;
        }

        self.render_overview(hwnd, rows);
        self.render_footer(hwnd, height);
        for control in self.children.drain(self.render_cursor..) {
            if !control.hwnd.is_null() {
                unsafe { DestroyWindow(control.hwnd) };
            }
        }
        if let Some(id) = focused_id {
            let mut control = unsafe { GetDlgItem(hwnd, id) };
            if control.is_null() && (id == i32::from(START) || id == i32::from(STOP)) {
                let toggle = if self.watching { STOP } else { START };
                control = unsafe { GetDlgItem(hwnd, i32::from(toggle)) };
            }
            if !control.is_null()
                && control != previous_focus
                && unsafe { IsWindowEnabled(control) } != 0
            {
                unsafe { SetFocus(control) };
            }
        }
    }

    fn text(&mut self, hwnd: HWND, label: &str, rect: [i32; 4], style_id: u16) -> HWND {
        self.child(hwnd, "STATIC", label, SS_NOPREFIX, rect, style_id)
    }

    fn heading(&mut self, hwnd: HWND, label: &str, rect: [i32; 4]) {
        let control = self.text(hwnd, label, rect, 0);
        set_font(control, self.heading_font.handle());
    }

    // The prompt stays up until every verified stat has been found and is frozen.
    fn shows_panel_prompt(&self) -> bool {
        self.watching
            && self.running
            && self.settings.game_path().is_some_and(game::valid_path)
            && matches!(
                self.discovery_phase,
                DiscoveryPhase::Searching
                    | DiscoveryPhase::WaitingForPanel
                    | DiscoveryPhase::Retrying
            )
    }

    // Pulses the in-game prompt. Only the glow flag changes, and the prompt is repainted.
    fn tick_glow(&mut self, hwnd: HWND) {
        if !self.shows_panel_prompt() || !self.reading.in_game {
            return;
        }
        if self.glow_changed.elapsed() < GLOW_INTERVAL {
            return;
        }
        self.glow_changed = Instant::now();
        let glow = !PROMPT_GLOW.load(Ordering::Relaxed);
        PROMPT_GLOW.store(glow, Ordering::Relaxed);
        let prompt = unsafe { GetDlgItem(hwnd, i32::from(PANEL_PROMPT)) };
        if !prompt.is_null() {
            unsafe { InvalidateRect(prompt, null(), 1) };
        }
    }

    fn render_overview(&mut self, hwnd: HWND, rows: i32) {
        let freezing = rows != 0;
        let configured = self.settings.game_path().is_some_and(game::valid_path);
        let (label, action) = if !configured {
            ("Locate game", LOCATE)
        } else if self.watching {
            ("Stop", STOP)
        } else {
            ("Find", START)
        };
        self.button(hwnd, label, [18, 16, 227, 36], action);
        let (launch_label, launch_action, launch_style) = if self.running {
            ("Game running", LAUNCH, WS_DISABLED)
        } else if configured {
            ("Launch game", PRIMARY_LAUNCH, 0)
        } else {
            ("Launch game", LAUNCH, WS_DISABLED)
        };
        self.child(
            hwnd,
            "BUTTON",
            launch_label,
            WS_TABSTOP | BS_OWNERDRAW as u32 | launch_style,
            [255, 16, 227, 36],
            launch_action,
        );
        self.text(hwnd, "", [18, 65, 464, 1], DIVIDER);

        if self.shows_panel_prompt() {
            // The prompt takes the whole body. Outside the game it shows the lobby message.
            let (text, id) = if self.reading.in_game {
                (CHARACTER_PANEL_PROMPT_LARGE, PANEL_PROMPT)
            } else {
                (LOBBY_MESSAGE, LOBBY_TEXT)
            };
            let prompt = self.child(
                hwnd,
                "STATIC",
                text,
                SS_CENTER | SS_NOPREFIX,
                [18, 96, 464, 200],
                id,
            );
            set_font(prompt, self.prompt_font.handle());
            return;
        }

        let (status, detail) = if !configured {
            (
                "Locate the game",
                "Select your AzerothSurvivors.exe to get started.",
            )
        } else if !self.watching {
            ("Watcher stopped", "Start the watcher when you’re ready.")
        } else if !self.running {
            ("Waiting for the game", "Launch Azeroth Survivors to begin.")
        } else {
            match self.discovery_phase {
                DiscoveryPhase::Searching => (
                    "Discovering stats",
                    "Looking for the live character record.",
                ),
                DiscoveryPhase::WaitingForPanel => ("Finding your character", CHARACTER_PANEL_PROMPT),
                DiscoveryPhase::Freezing => ("Stats frozen", ""),
                DiscoveryPhase::Retrying => (
                    "Reconnecting",
                    "The watcher will look for the character record again.",
                ),
            }
        };
        self.text(
            hwnd,
            "●",
            [20, 82, 18, 20],
            if freezing { READY_DOT } else { STATUS_DOT },
        );
        self.heading(hwnd, status, [42, 76, 440, 29]);
        let detail = if configured
            && self.watching
            && self.running
            && matches!(
                self.discovery_phase,
                DiscoveryPhase::Searching
                    | DiscoveryPhase::WaitingForPanel
                    | DiscoveryPhase::Retrying
            ) {
            CHARACTER_PANEL_PROMPT.to_owned()
        } else if !self.message.is_empty() {
            self.message.clone()
        } else if !self.discovery_message.is_empty()
            && matches!(
                self.discovery_phase,
                DiscoveryPhase::Searching | DiscoveryPhase::Retrying
            )
        {
            self.discovery_message.clone()
        } else if freezing {
            format!(
                "Rewriting every {:.1} seconds.",
                discovery::POLL_INTERVAL.as_secs_f32()
            )
        } else {
            detail.to_owned()
        };
        self.text(hwnd, &detail, [42, 108, 440, 36], MUTED_TEXT);

        let metadata_y = if freezing {
            self.render_stats(hwnd);
            184 + rows * 62
        } else {
            151
        };
        let build = self
            .build
            .as_deref()
            .and_then(|value| value.get(..12))
            .map_or_else(|| "—".to_owned(), |value| value.to_owned());
        self.text(
            hwnd,
            &format!("Game build  {build}"),
            [18, metadata_y, 264, 20],
            MUTED_TEXT,
        );
        if self.watching {
            self.child(
                hwnd,
                "STATIC",
                "Solo / host mode",
                SS_RIGHT | SS_NOPREFIX,
                [331, metadata_y, 151, 20],
                MUTED_TEXT,
            );
        }
    }

    fn render_stats(&mut self, hwnd: HWND) {
        self.text(hwnd, "Stats", [18, 146, 220, 20], 0);
        // Always show all four stats in this order. A stat that is not found yet shows "-".
        for (index, target_index) in DISPLAY_ORDER.into_iter().enumerate() {
            let target = &discovery::TARGETS[target_index];
            let value = if self.active_stats[target_index] {
                target.displayed_bonus()
            } else {
                "-".to_owned()
            };
            let caption = format!("{}\n{}", target.name, value);
            let x = 18 + (index % 2) as i32 * 237;
            let y = 173 + (index / 2) as i32 * 62;
            let tile = self.child(
                hwnd,
                "STATIC",
                &caption,
                SS_OWNERDRAW,
                [x, y, 227, 53],
                STAT_CARD,
            );
            set_font(tile, self.heading_font.handle());
        }
        // Gold Bonus and Luck can only be seen after an upgrade changes their values.
        if !self.active_stats[2] || !self.active_stats[3] {
            self.text(
                hwnd,
                "Try upgrading so the trainer can see the difference.",
                [18, 290, 464, 18],
                MUTED_TEXT,
            );
        }
    }

    fn render_footer(&mut self, hwnd: HWND, height: i32) {
        let footer = height - 39;
        if !self.update_message.is_empty() {
            let status = self.update_message.clone();
            self.text(hwnd, &status, [18, footer - 44, 464, 34], MUTED_TEXT);
        }
        self.text(hwnd, "", [18, footer - 8, 464, 1], DIVIDER);
        self.button(hwnd, "GitHub Page", [18, footer, 106, 27], GITHUB);
        let (label, style) = match self.updater {
            UpdateControl::Ready => ("Check updates", 0),
            UpdateControl::Checking(_) => ("Checking..", WS_DISABLED),
            UpdateControl::Latest => ("Latest already", 0),
            UpdateControl::Available(_) => ("Download & Install", 0),
            UpdateControl::Installing(..) => ("Installing..", WS_DISABLED),
        };
        self.child(
            hwnd,
            "BUTTON",
            label,
            WS_TABSTOP | BS_OWNERDRAW as u32 | style,
            [226, footer, 164, 27],
            CHECK_UPDATES,
        );
        let version = format!("v{}", env!("CARGO_PKG_VERSION"));
        self.child(
            hwnd,
            "STATIC",
            &version,
            SS_RIGHT | SS_NOPREFIX,
            [400, footer + 4, 82, 21],
            MUTED_TEXT,
        );
    }

    fn log(&self, message: &str) {
        diagnostic_log::info(message);
    }

    fn choose_game(&mut self, parent: HWND) -> bool {
        let Some(path) = pick_game(parent) else {
            return false;
        };
        if !game::valid_path(&path) {
            self.message = "Select a valid AzerothSurvivors.exe first.".into();
            return false;
        }
        if let Err(error) = self.settings.set_game_path(&path) {
            self.message = format!("Could not save the selected game. {error}");
            return false;
        }
        self.message.clear();
        self.process_lookup_misses = 0;
        self.set_game_instance(None);
        self.log("Game executable configured.");
        true
    }

    fn set_game_instance(&mut self, instance: Option<game::ProcessInstance>) -> bool {
        if self.game_instance == instance {
            return false;
        }
        self.game_instance = instance;
        self.running = instance.is_some();
        self.build = None;
        self.reset_scan();
        self.log(match instance {
            Some(process) if process.created.is_some() => "Game process identity verified.",
            Some(_) => "Game detected using legacy PID and executable path lookup.",
            None => "Game process no longer identified.",
        });
        true
    }

    fn refresh_process(&mut self) -> bool {
        let Some(path) = self
            .settings
            .game_path()
            .filter(|path| game::valid_path(path))
            .map(Path::to_owned)
        else {
            self.process_lookup_misses = 0;
            return self.set_game_instance(None);
        };
        // Process enumeration is cheap but not free, so it runs less often than the OCR poll.
        if Instant::now() < self.next_process_check {
            self.poll_hud();
            return self.poll_discovery(&path);
        }
        self.next_process_check = Instant::now() + PROCESS_CHECK_INTERVAL;
        let instance = match game::running_instance(&path) {
            Ok(Some(instance)) => {
                self.process_lookup_misses = 0;
                Some(instance)
            }
            Ok(None) | Err(_) => {
                if self.process_lookup_misses < PROCESS_LOOKUP_LIMIT {
                    self.process_lookup_misses += 1;
                    if self.process_lookup_misses == PROCESS_LOOKUP_LIMIT {
                        self.log("Process identity lookup missed 10 times. Trying legacy lookup.");
                    }
                }
                if self.process_lookup_misses == PROCESS_LOOKUP_LIMIT {
                    game::legacy_instance(&path).ok().flatten()
                } else {
                    None
                }
            }
        };
        let mut refresh = self.set_game_instance(instance);
        self.poll_hud();
        refresh |= self.poll_discovery(&path);
        refresh
    }

    fn poll_discovery(&mut self, path: &Path) -> bool {
        if !self.watching {
            return false;
        }
        let Some(instance) = self.game_instance else {
            return false;
        };
        let update = if instance.created.is_none() {
            Some((
                DiscoveryPhase::Searching,
                "Waiting to verify the game process.".into(),
            ))
        } else if let Some(job) = &self.discovery {
            let report = match job.result.try_recv() {
                Ok(report) => report,
                Err(TryRecvError::Empty) => return false,
                Err(TryRecvError::Disconnected) => {
                    return self.discovery_failed("The discovery worker ended.");
                }
            };
            Some(match report {
                Ok(report) => {
                    if self.build.as_ref() != Some(&report.build) {
                        self.log(&format!("Game build SHA-256: {}", report.build));
                        self.build = Some(report.build);
                    }
                    self.active_stats = report.active;
                    let names: Vec<_> = discovery::TARGETS
                        .iter()
                        .zip(report.active)
                        .filter_map(|(target, active)| active.then_some(target.name))
                        .collect();
                    if !names.is_empty() {
                        (
                            DiscoveryPhase::Freezing,
                            format!("Freezing {} every 0.5 seconds.", names.join(", ")),
                        )
                    } else {
                        (
                            DiscoveryPhase::Searching,
                            "Matching the live record to your character panel.".into(),
                        )
                    }
                }
                Err(error) => return self.discovery_failed(&error.to_string()),
            })
        } else if Instant::now() >= self.next_discovery {
            let name = self.solo_character.clone().or_else(|| {
                self.reading
                    .player
                    .as_ref()
                    .map(|player| player.name.clone())
            });
            let Some(name) = name else {
                return self.set_discovery_status(
                    DiscoveryPhase::WaitingForPanel,
                    CHARACTER_PANEL_PROMPT.into(),
                );
            };
            match discovery::Job::start(
                path.to_owned(),
                instance,
                name,
                self.reading.player.clone(),
            ) {
                Ok(job) => {
                    self.discovery = Some(job);
                    None
                }
                Err(error) => return self.discovery_failed(&error.to_string()),
            }
        } else {
            None
        };
        if let Some((phase, update)) = update {
            return self.set_discovery_status(phase, update);
        }
        false
    }

    fn discovery_failed(&mut self, error: &str) -> bool {
        diagnostic_log::warn(&format!("Discovery failed: {error}"));
        self.discovery = None;
        self.active_stats.fill(false);
        self.next_discovery = Instant::now() + Duration::from_secs(3);
        self.set_discovery_status(
            DiscoveryPhase::Retrying,
            format!("Stat discovery will retry. {error}"),
        )
    }

    fn set_discovery_status(&mut self, phase: DiscoveryPhase, update: String) -> bool {
        let changed = self.discovery_phase != phase || self.discovery_message != update;
        if changed {
            self.log(&update);
            self.discovery_phase = phase;
            self.discovery_message = update;
        }
        changed
    }

    fn poll_hud(&mut self) {
        if let Some((generation, receiver)) = &self.capture {
            let generation = *generation;
            let result = match receiver.try_recv() {
                Ok(result) => Some(result),
                Err(TryRecvError::Disconnected) => Some(Err("OCR worker ended.".into())),
                Err(TryRecvError::Empty) => None,
            };
            if let Some(result) = result {
                self.capture = None;
                if self.watching && self.scan_generation == generation {
                    match result {
                        Ok(reading) => {
                            // A single empty read is usually a flicker, so keep the last values once.
                            let empty = reading.player.is_none() && reading.stats.is_empty();
                            if empty && self.reading.player.is_some() && self.empty_reads == 0 {
                                self.empty_reads = 1;
                            } else {
                                self.empty_reads = 0;
                                if let (Some(job), Some(player)) =
                                    (&self.discovery, &reading.player)
                                {
                                    job.observe(player.clone());
                                }
                                let player_name = |reading: &hud::Reading| {
                                    reading.player.as_ref().map(|player| player.name.clone())
                                };
                                let changed = self.capture_error.take().is_some()
                                    || self.reading.stats != reading.stats
                                    || self.reading.in_game != reading.in_game
                                    || player_name(&self.reading) != player_name(&reading);
                                if changed {
                                    // Logged only on change, so the log keeps useful history.
                                    self.log(&format!(
                                        "Panel read in {} ms (capture {} ms). In game: {}. Player: {}. Values: [{}].",
                                        reading.total_ms,
                                        reading.capture_ms,
                                        if reading.in_game { "yes" } else { "no" },
                                        describe_player(reading.player.as_ref()),
                                        reading.stats.join(", ")
                                    ));
                                }
                                self.reading = reading;
                            }
                        }
                        Err(error) if self.capture_error.as_ref() != Some(&error) => {
                            self.log(&format!("Could not read the game window: {error}"));
                            self.capture_error = Some(error);
                            self.reading = hud::Reading::default();
                        }
                        Err(_) => {}
                    }
                }
            }
        }
        if self.watching
            && self.active_stats.contains(&false)
            && self.capture.is_none()
            && Instant::now() >= self.next_capture
        {
            if let Some(process) = self.game_instance {
                let (sender, receiver) = mpsc::sync_channel(1);
                // Read the full frame only while the player has not been found yet.
                let fallback = self.reading.player.is_none();
                self.ocr.request(process.pid, fallback, sender);
                self.capture = Some((self.scan_generation, receiver));
                // Poll quickly until the panel is read, then back off once the player is known.
                let interval = if self.reading.player.is_some() {
                    Duration::from_secs(2)
                } else {
                    Duration::from_millis(500)
                };
                self.next_capture = Instant::now() + interval;
            }
        }
    }

    fn reset_scan(&mut self) {
        self.scan_generation = self.scan_generation.wrapping_add(1);
        self.discovery = None;
        self.discovery_phase = DiscoveryPhase::Searching;
        self.active_stats.fill(false);
        self.discovery_message.clear();
        self.next_discovery = Instant::now();
        self.reading = hud::Reading::default();
        self.empty_reads = 0;
        self.capture = None;
        self.capture_error = None;
        self.next_capture = Instant::now();
    }

    fn use_update_button(&mut self) {
        match &self.updater {
            UpdateControl::Checking(_) | UpdateControl::Installing(..) => return,
            UpdateControl::Available(release) => {
                let release = release.clone();
                let download = release.clone();
                let mut args: Vec<_> = env::args_os().skip(1).collect();
                if self.watching {
                    if !args.iter().any(|arg| arg == "--autostart") {
                        args.push("--autostart".into());
                    }
                } else {
                    args.retain(|arg| {
                        arg != "--autostart"
                            && !arg
                                .to_str()
                                .is_some_and(|arg| arg.starts_with("--solo-character="))
                    });
                }
                let (sender, receiver) = mpsc::sync_channel(1);
                std::thread::spawn(move || {
                    let result =
                        update::install(&download, &args).map_err(|error| error.to_string());
                    let _ = sender.send(result);
                });
                self.updater = UpdateControl::Installing(release, receiver);
            }
            UpdateControl::Ready | UpdateControl::Latest => {
                let (sender, receiver) = mpsc::sync_channel(1);
                std::thread::spawn(move || {
                    let result =
                        update::check(env!("CARGO_PKG_VERSION")).map_err(|error| error.to_string());
                    let _ = sender.send(result);
                });
                self.updater = UpdateControl::Checking(receiver);
            }
        }
        self.update_message.clear();
    }

    fn poll_update(&mut self) -> bool {
        self.updater = match &self.updater {
            UpdateControl::Checking(receiver) => match update_result(receiver) {
                None => return false,
                Some(Ok(update::ReleaseStatus::Latest)) => UpdateControl::Latest,
                Some(Ok(update::ReleaseStatus::Available(release))) => {
                    UpdateControl::Available(release)
                }
                Some(Err(error)) => {
                    self.log(&format!("Update check failed: {error}"));
                    self.update_message = "Update check failed. Try again.".into();
                    UpdateControl::Ready
                }
            },
            UpdateControl::Installing(release, receiver) => match update_result(receiver) {
                None => return false,
                Some(Ok(())) => {
                    self.log("Update installed. Restarting.");
                    unsafe { PostQuitMessage(0) };
                    return false;
                }
                Some(Err(error)) => {
                    self.log(&format!("Update installation failed: {error}"));
                    self.update_message = "Could not install the update. See run.log.".into();
                    UpdateControl::Available(release.clone())
                }
            },
            _ => return false,
        };
        true
    }

    fn command(&mut self, hwnd: HWND, command: u16) {
        match command {
            START | STOP | LOCATE => {
                if !self.settings.game_path().is_some_and(game::valid_path)
                    && !self.choose_game(hwnd)
                {
                    self.render(hwnd);
                    return;
                }
                self.watching = !self.watching;
                self.message.clear();
                self.reset_scan();
                self.log(if self.watching {
                    "Watcher started."
                } else {
                    "Watcher stopped."
                });
            }
            LAUNCH | PRIMARY_LAUNCH => {
                if !self.settings.game_path().is_some_and(game::valid_path)
                    && !self.choose_game(hwnd)
                {
                    self.render(hwnd);
                    return;
                }
                if let Some(path) = self.settings.game_path() {
                    self.message = match game::launch(path) {
                        Ok(true) => "Game launched.".into(),
                        Ok(false) => "Game is already running.".into(),
                        Err(error) => format!("Could not launch the game. {error}"),
                    };
                    self.log(&self.message);
                }
            }
            GITHUB => open(hwnd, REPO),
            CHECK_UPDATES => self.use_update_button(),
            _ => return,
        }
        self.refresh_process();
        self.render(hwnd);
    }
}

fn update_result<T>(receiver: &Receiver<Result<T, String>>) -> Option<Result<T, String>> {
    match receiver.try_recv() {
        Ok(result) => Some(result),
        Err(TryRecvError::Empty) => None,
        Err(TryRecvError::Disconnected) => Some(Err("Update worker ended.".into())),
    }
}

fn draw_button(item: &DRAWITEMSTRUCT) {
    unsafe {
        let disabled = item.itemState & ODS_DISABLED != 0;
        let primary = matches!(item.CtlID as u16, LOCATE | START);
        let stop = item.CtlID as u16 == STOP;
        let pressed = item.itemState & ODS_SELECTED != 0;
        let fill = if disabled {
            rgb(31, 33, 43)
        } else if stop && pressed {
            rgb(116, 62, 74)
        } else if stop {
            rgb(139, 71, 85)
        } else if primary && pressed {
            rgb(79, 69, 143)
        } else if primary {
            rgb(97, 84, 171)
        } else if pressed {
            rgb(53, 56, 75)
        } else {
            SECONDARY
        };
        let brush = CreateSolidBrush(fill);
        FillRect(item.hDC, &item.rcItem, brush);
        DeleteObject(brush);
        let mut label = [0u16; 128];
        let count = GetWindowTextW(item.hwndItem, label.as_mut_ptr(), label.len() as i32);
        SetTextColor(item.hDC, if disabled { MUTED } else { TEXT });
        SetBkMode(item.hDC, TRANSPARENT as i32);
        let font = SendMessageW(item.hwndItem, WM_GETFONT, 0, 0) as HFONT;
        let font = if font.is_null() {
            GetStockObject(UI_FONT)
        } else {
            font.cast()
        };
        let previous_font = SelectObject(item.hDC, font);
        let mut area = item.rcItem;
        DrawTextW(
            item.hDC,
            label.as_ptr(),
            count,
            &mut area,
            DT_CENTER | DT_VCENTER | DT_SINGLELINE | DT_NOPREFIX,
        );
        if item.itemState & ODS_FOCUS != 0 {
            area.left += 3;
            area.top += 3;
            area.right -= 3;
            area.bottom -= 3;
            DrawFocusRect(item.hDC, &area);
        }
        SelectObject(item.hDC, previous_font);
    }
}

fn set_font(hwnd: HWND, font: HFONT) {
    if !hwnd.is_null() && unsafe { SendMessageW(hwnd, WM_GETFONT, 0, 0) } as HFONT != font {
        unsafe { SendMessageW(hwnd, WM_SETFONT, font as usize, 1) };
    }
}

fn draw_stat_card(item: &DRAWITEMSTRUCT) {
    unsafe {
        let brush = CreateSolidBrush(SECONDARY);
        FillRect(item.hDC, &item.rcItem, brush);
        DeleteObject(brush);

        let mut caption = [0u16; 96];
        let count = GetWindowTextW(item.hwndItem, caption.as_mut_ptr(), caption.len() as i32);
        let caption = &caption[..count.max(0) as usize];
        let Some(divider) = caption.iter().position(|&ch| ch == u16::from(b'\n')) else {
            return;
        };
        let (label, value) = caption.split_at(divider);
        let value = &value[1..];

        let font = SendMessageW(item.hwndItem, WM_GETFONT, 0, 0) as HFONT;
        let old_font = if font.is_null() {
            null_mut()
        } else {
            SelectObject(item.hDC, font.cast())
        };
        let scale = |pixels| pixels * (item.rcItem.bottom - item.rcItem.top) / 53;
        let mut name_rect = item.rcItem;
        let mut value_rect = item.rcItem;
        name_rect.left += scale(13);
        name_rect.right -= scale(88);
        value_rect.left = name_rect.right;
        value_rect.right -= scale(13);
        let flags = DT_VCENTER | DT_SINGLELINE | DT_NOPREFIX;
        SetBkMode(item.hDC, TRANSPARENT as i32);
        SetTextColor(item.hDC, TEXT);
        DrawTextW(
            item.hDC,
            label.as_ptr(),
            label.len() as i32,
            &mut name_rect,
            flags | DT_END_ELLIPSIS,
        );
        SetTextColor(item.hDC, LIVE);
        DrawTextW(
            item.hDC,
            value.as_ptr(),
            value.len() as i32,
            &mut value_rect,
            flags | DT_RIGHT,
        );
        if !old_font.is_null() {
            SelectObject(item.hDC, old_font);
        }
    }
}

fn pick_game(hwnd: HWND) -> Option<PathBuf> {
    let mut path = vec![0u16; 32768];
    let filters = wide("Azeroth Survivors\0AzerothSurvivors.exe\0\0");
    let mut spec: OPENFILENAMEW = unsafe { std::mem::zeroed() };
    spec.lStructSize = size_of::<OPENFILENAMEW>() as u32;
    spec.hwndOwner = hwnd;
    spec.lpstrFile = path.as_mut_ptr();
    spec.nMaxFile = path.len() as u32;
    spec.lpstrFilter = filters.as_ptr();
    spec.Flags = OFN_FILEMUSTEXIST | OFN_PATHMUSTEXIST;
    if unsafe { GetOpenFileNameW(&mut spec) } == 0 {
        return None;
    }
    let end = path.iter().position(|&value| value == 0)?;
    Some(std::ffi::OsString::from_wide(&path[..end]).into())
}

fn open(hwnd: HWND, target: impl AsRef<std::ffi::OsStr>) {
    unsafe {
        ShellExecuteW(
            hwnd,
            wide("open").as_ptr(),
            wide(target).as_ptr(),
            null(),
            null(),
            SW_SHOWNORMAL,
        )
    };
}

extern "system" fn wndproc(hwnd: HWND, message: u32, w: WPARAM, l: LPARAM) -> LRESULT {
    unsafe {
        match message {
            WM_NCCREATE => {
                let data = (*(l as *const CREATESTRUCTW)).lpCreateParams;
                SetWindowLongPtrW(hwnd, GWLP_USERDATA, data as isize);
                return 1;
            }
            WM_COMMAND if l != 0 => {
                let state = GetWindowLongPtrW(hwnd, GWLP_USERDATA) as *mut Window;
                if let Some(data) = state.as_mut() {
                    // A file dialog can dispatch timer messages while this command runs.
                    SetWindowLongPtrW(hwnd, GWLP_USERDATA, 0);
                    data.command(hwnd, w as u16);
                    SetWindowLongPtrW(hwnd, GWLP_USERDATA, state as isize);
                }
                return 0;
            }
            WM_DRAWITEM if l != 0 => {
                let item = &*(l as *const DRAWITEMSTRUCT);
                if item.CtlType == ODT_STATIC && item.CtlID == u32::from(STAT_CARD) {
                    draw_stat_card(item);
                } else {
                    draw_button(item);
                }
                return 1;
            }
            WM_CTLCOLORSTATIC => {
                let color = match GetDlgCtrlID(l as HWND) as u16 {
                    READY_DOT => LIVE,
                    STATUS_DOT => rgb(173, 158, 242),
                    MUTED_TEXT => MUTED,
                    PANEL_PROMPT => {
                        if PROMPT_GLOW.load(Ordering::Relaxed) {
                            rgb(255, 236, 96)
                        } else {
                            rgb(122, 96, 34)
                        }
                    }
                    DIVIDER => {
                        SetDCBrushColor(w as _, BORDER);
                        return GetStockObject(DC_BRUSH) as LRESULT;
                    }
                    _ => TEXT,
                };
                SetTextColor(w as _, color);
                SetBkMode(w as _, TRANSPARENT as i32);
                return GetClassLongPtrW(hwnd, GCLP_HBRBACKGROUND) as LRESULT;
            }
            WM_TIMER => {
                let state = GetWindowLongPtrW(hwnd, GWLP_USERDATA) as *mut Window;
                if let Some(state) = state.as_mut() {
                    state.tick_glow(hwnd);
                    let process_changed = state.refresh_process();
                    let update_changed = state.poll_update();
                    if process_changed || update_changed {
                        state.render(hwnd);
                    }
                }
                return 0;
            }
            WM_CLOSE => {
                let state = GetWindowLongPtrW(hwnd, GWLP_USERDATA) as *const Window;
                if state
                    .as_ref()
                    .is_some_and(|window| matches!(window.updater, UpdateControl::Installing(..)))
                {
                    return 0;
                }
            }
            WM_DESTROY => {
                PostQuitMessage(0);
                return 0;
            }
            _ => {}
        }
        DefWindowProcW(hwnd, message, w, l)
    }
}

fn run() -> std::io::Result<()> {
    let settings = Settings::load()?;
    diagnostic_log::init(settings.directory.join("run.log"));
    diagnostic_log::info(&format!(
        "Trainer v{} starting. Process {}.",
        env!("CARGO_PKG_VERSION"),
        std::process::id()
    ));
    let solo_character = if let Some(mut args) = env::args_os().skip(1).find_map(|arg| {
        arg.to_str()
            .and_then(|arg| arg.strip_prefix("--solo-character="))
            .map(str::to_owned)
    }) {
        args = args.trim().to_owned();
        if !(2..=24).contains(&args.len()) {
            return Err(std::io::Error::other(
                "Set --solo-character to the name shown in your solo game.",
            ));
        }
        Some(args)
    } else {
        None
    };
    unsafe {
        SetProcessDPIAware();
        let dpi = GetDpiForSystem() as i32;
        let mut window = Window {
            settings,
            children: Vec::new(),
            render_cursor: 0,
            rendered_height: 0,
            dpi,
            font: Font::new(dpi, 15, 400),
            heading_font: Font::new(dpi, 20, 600),
            prompt_font: Font::new(dpi, 26, 700),
            ocr: hud::OcrWorker::start(),
            next_process_check: Instant::now(),
            glow_changed: Instant::now(),
            watching: solo_character.is_some() || env::args_os().any(|arg| arg == "--autostart"),
            solo_character,
            running: false,
            game_instance: None,
            process_lookup_misses: 0,
            build: None,
            discovery: None,
            discovery_phase: DiscoveryPhase::Searching,
            active_stats: discovery::ActiveStats::default(),
            discovery_message: String::new(),
            next_discovery: Instant::now(),
            reading: hud::Reading::default(),
            capture: None,
            scan_generation: 0,
            capture_error: None,
            empty_reads: 0,
            next_capture: Instant::now(),
            message: String::new(),
            updater: UpdateControl::Ready,
            update_message: String::new(),
        };
        let instance = GetModuleHandleW(null());
        let class = wide("AzerothSurvivorsTrainerRust");
        let window_class = WNDCLASSW {
            lpfnWndProc: Some(wndproc),
            hInstance: instance,
            hCursor: LoadCursorW(null_mut(), IDC_ARROW),
            hIcon: LoadIconW(instance, 1usize as _),
            hbrBackground: CreateSolidBrush(BACKGROUND),
            lpszClassName: class.as_ptr(),
            ..std::mem::zeroed()
        };
        if RegisterClassW(&window_class) == 0 {
            return Err(std::io::Error::last_os_error());
        }
        let hwnd = CreateWindowExW(
            0,
            class.as_ptr(),
            wide(TITLE).as_ptr(),
            WINDOW_STYLE,
            CW_USEDEFAULT,
            CW_USEDEFAULT,
            320,
            220,
            null_mut(),
            null_mut(),
            instance,
            (&mut window as *mut Window).cast(),
        );
        if hwnd.is_null() {
            return Err(std::io::Error::last_os_error());
        }
        SetWindowTextW(hwnd, wide(TITLE).as_ptr());
        let dark = 1i32;
        DwmSetWindowAttribute(
            hwnd,
            DWMWA_USE_IMMERSIVE_DARK_MODE as u32,
            (&dark as *const i32).cast(),
            size_of::<i32>() as u32,
        );
        window.log("Rust trainer window opened.");
        window.refresh_process();
        window.render(hwnd);
        ShowWindow(hwnd, SW_SHOWNORMAL);
        SetTimer(hwnd, 1, 500, None);
        let mut message: MSG = std::mem::zeroed();
        loop {
            let state = GetMessageW(&mut message, null_mut(), 0, 0);
            if state == 0 {
                break;
            }
            if state < 0 {
                return Err(std::io::Error::last_os_error());
            }
            if IsDialogMessageW(hwnd, &message) == 0 {
                TranslateMessage(&message);
                DispatchMessageW(&message);
            }
        }
    }
    Ok(())
}

fn describe_player(player: Option<&hud::Player>) -> String {
    match player {
        Some(player) => match player.level {
            Some(level) => format!("{} level {level}", player.name),
            None => player.name.clone(),
        },
        None => "not found".to_owned(),
    }
}

fn main() {
    if let Err(error) = run() {
        unsafe {
            MessageBoxW(
                null_mut(),
                wide(error.to_string()).as_ptr(),
                wide(TITLE).as_ptr(),
                MB_ICONERROR,
            )
        };
        std::process::exit(1);
    }
}
