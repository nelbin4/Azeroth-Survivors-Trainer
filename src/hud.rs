use crate::{diagnostic_log, discovery::TARGETS};
use image::{RgbaImage, imageops::FilterType};
use std::{
    io,
    ptr::null_mut,
    slice,
    sync::mpsc::{self, Sender, SyncSender},
    thread,
    time::Instant,
};
use windows::{
    Graphics::Imaging::{BitmapAlphaMode, BitmapPixelFormat, SoftwareBitmap},
    Media::Ocr::{OcrEngine, OcrResult},
    Storage::Streams::DataWriter,
    Win32::System::WinRT::{RO_INIT_MULTITHREADED, RoInitialize, RoUninitialize},
};
use windows_sys::Win32::{
    Foundation::{HWND, LPARAM, RECT},
    Graphics::Gdi::{
        BI_RGB, BITMAPINFO, CreateCompatibleDC, CreateDIBSection, DIB_RGB_COLORS, DeleteDC,
        DeleteObject, SelectObject,
    },
    Storage::Xps::PrintWindow,
    UI::WindowsAndMessaging::{
        EnumWindows, GetClientRect, GetWindowTextW, GetWindowThreadProcessId, IsIconic,
        IsWindowVisible,
    },
};

#[derive(Default)]
pub struct Reading {
    pub stats: Vec<String>,
    pub player: Option<Player>,
    pub capture_ms: u64,
    pub total_ms: u64,
    pub in_game: bool,
}

#[derive(Clone)]
pub struct Player {
    pub name: String,
    pub level: Option<u32>,
    pub health: Option<f32>,
    pub stats: [Option<f32>; 4],
    pub captured: Instant,
}

// Coordinates are fractions of the full client area, as in the legacy reader.
const STAT_PANELS: [[f32; 4]; 2] = [[0.03, 0.44, 0.25, 0.60], [0.22, 0.12, 0.37, 0.36]];

struct Frame {
    pixels: Vec<u8>,
    width: usize,
    height: usize,
}

struct Apartment;

impl Apartment {
    fn enter() -> windows::core::Result<Self> {
        unsafe { RoInitialize(RO_INIT_MULTITHREADED) }?;
        Ok(Self)
    }
}

impl Drop for Apartment {
    fn drop(&mut self) {
        unsafe { RoUninitialize() };
    }
}

// pid, whether to run the slow full-frame pass, and where to send the reply.
type Request = (u32, bool, SyncSender<Result<Reading, String>>);

// Owns one OCR engine for the whole session. Creating the engine and its first
// recognition is slow, so it runs at startup instead of during the first search.
pub struct OcrWorker {
    requests: Sender<Request>,
}

impl OcrWorker {
    pub fn start() -> Self {
        let (requests, inbox) = mpsc::channel::<Request>();
        let spawned = thread::Builder::new().name("ocr".into()).spawn(move || {
            let _apartment = Apartment::enter().ok();
            let started = Instant::now();
            let engine = OcrEngine::TryCreateFromUserProfileLanguages()
                .map_err(|error| format!("OCR is unavailable. {error}"));
            if let Ok(engine) = &engine {
                // The first recognition loads language data, so pay that cost now.
                let blank = vec![0u8; 64 * 16 * 4];
                let _ = recognize(engine, &blank, 64, 16);
            }
            match &engine {
                Ok(_) => diagnostic_log::info(&format!(
                    "OCR engine ready in {} ms.",
                    started.elapsed().as_millis()
                )),
                Err(error) => diagnostic_log::error(error),
            }
            for (pid, fallback, reply) in inbox {
                let result = match &engine {
                    Ok(engine) => read_ocr(engine, pid, fallback),
                    Err(error) => Err(error.clone()),
                };
                let _ = reply.send(result);
            }
        });
        if let Err(error) = spawned {
            diagnostic_log::error(&format!("Could not start the OCR worker. {error}"));
        }
        Self { requests }
    }

    // If the worker has stopped, the reply channel disconnects and the caller reports it.
    pub fn request(&self, pid: u32, fallback: bool, reply: SyncSender<Result<Reading, String>>) {
        let _ = self.requests.send((pid, fallback, reply));
    }
}

fn read_ocr(engine: &OcrEngine, pid: u32, fallback: bool) -> Result<Reading, String> {
    let started = Instant::now();
    let frame = capture(pid).map_err(|error| error.to_string())?;
    let captured_in = started.elapsed();
    let captured = Instant::now();
    let image = RgbaImage::from_raw(frame.width as u32, frame.height as u32, frame.pixels)
        .ok_or("Game capture has invalid image dimensions.")?;
    let stats = read_stat_panels(engine, &image, fallback).map_err(|error| error.to_string())?;
    let in_game = read_hud(engine, &image);
    let player = read_player(engine, &image, &stats, captured);
    Ok(Reading {
        stats,
        player,
        capture_ms: captured_in.as_millis() as u64,
        total_ms: started.elapsed().as_millis() as u64,
        in_game,
    })
}

// The in-game HUD shows the kill and gold counters in the top-left corner. The lobby does not.
fn read_hud(engine: &OcrEngine, image: &RgbaImage) -> bool {
    read_crop(engine, image, [0.0, 0.0, 0.2, 0.035]).is_some_and(|text| {
        let text = text.to_ascii_lowercase();
        ["kills", "gold", "cache"]
            .iter()
            .any(|word| text.contains(word))
    })
}

fn read_stat_panels(
    engine: &OcrEngine,
    image: &RgbaImage,
    fallback: bool,
) -> windows::core::Result<Vec<String>> {
    let (width, height) = image.dimensions();
    let max_dimension = OcrEngine::MaxImageDimension()?;
    let mut text = String::new();
    for [left, top, right, bottom] in STAT_PANELS {
        let x = (left * 2.0 * width as f32) as u32;
        let y = (top * height as f32) as u32;
        let w = (right * 2.0 * width as f32) as u32 - x;
        let h = (bottom * height as f32) as u32 - y;
        if w == 0 || h == 0 {
            continue;
        }
        let panel = image::imageops::crop_imm(image, x, y, w, h).to_image();
        let scale = (max_dimension / w.max(h)).clamp(1, 3);
        let enlarged = image::imageops::resize(&panel, w * scale, h * scale, FilterType::Lanczos3);
        // Lanczos filters each channel independently. The captured pixels remain BGRA.
        let result = recognize(
            engine,
            enlarged.as_raw(),
            enlarged.width() as usize,
            enlarged.height() as usize,
        )?;
        text.push_str(&stat_rows(&result)?);
    }
    let stats = stats_from_text(&text);
    if !fallback
        || TARGETS
            .iter()
            .any(|target| target.panel_value(&stats).is_some())
    {
        return Ok(stats);
    }
    // The full-frame pass is slow, so it only runs while the player is not yet known.
    let result = recognize(engine, image.as_raw(), width as usize, height as usize)?;
    Ok(stats_from_text(&stat_rows(&result)?))
}

fn read_player(
    engine: &OcrEngine,
    image: &RgbaImage,
    stats: &[String],
    captured: Instant,
) -> Option<Player> {
    let health = stats
        .iter()
        .find_map(|stat| {
            stat.strip_prefix("HP ")?
                .replace(',', "")
                .parse::<f32>()
                .ok()
        })
        .filter(|value| (50.0..=1e6).contains(value));
    let verified_stats = TARGETS.map(|target| target.panel_value(stats));
    if verified_stats.iter().all(Option::is_none) {
        return None;
    }
    let read = |bounds| read_crop(engine, image, bounds);
    let header = read([0.047, 0.131, 0.218, 0.197]);
    let parsed = header.as_deref().and_then(character_header);
    let sheet_name = parsed.and_then(|(name, _)| name);
    let level = parsed.map(|(_, level)| level);
    // The player frame has a clean name even when buff icons overlap the character sheet.
    let player_frame = read([0.065, 0.029, 0.135, 0.054]);
    let name = player_frame
        .as_deref()
        .and_then(|text| {
            text.lines()
                .map(str::trim)
                .find(|line| character_name(line))
        })
        .or(sheet_name)?;
    Some(Player {
        name: name.to_owned(),
        level,
        health,
        stats: verified_stats,
        captured,
    })
}

fn read_crop(engine: &OcrEngine, image: &RgbaImage, bounds: [f32; 4]) -> Option<String> {
    let [left, top, right, bottom] = bounds;
    // Capture stores the left half of the game, while bounds use the full client width.
    let client_width = image.width() as f32 * 2.0;
    let x = (left * client_width) as u32;
    let y = (top * image.height() as f32) as u32;
    let width = (right * client_width) as u32 - x;
    let height = (bottom * image.height() as f32) as u32 - y;
    if width == 0 || height == 0 {
        return None;
    }
    let scale = (OcrEngine::MaxImageDimension().ok()? / width.max(height)).clamp(1, 3);
    let crop = image::imageops::crop_imm(image, x, y, width, height).to_image();
    let crop = image::imageops::resize(&crop, width * scale, height * scale, FilterType::Lanczos3);
    let result = recognize(
        engine,
        crop.as_raw(),
        crop.width() as usize,
        crop.height() as usize,
    )
    .ok()?;
    stat_rows(&result).ok()
}

fn character_name(name: &str) -> bool {
    (2..=24).contains(&name.len())
        && name.chars().next().is_some_and(char::is_alphabetic)
        && name
            .chars()
            .all(|ch| ch.is_alphanumeric() || matches!(ch, '\'' | '-' | '_' | ' '))
}

fn character_header(text: &str) -> Option<(Option<&str>, u32)> {
    let mut name = None;
    for line in text.lines().map(str::trim) {
        let mut words = line.split_whitespace();
        if words.next().is_some_and(|word| {
            ["level", "evel", "vel"]
                .iter()
                .any(|label| word.eq_ignore_ascii_case(label))
        }) {
            let level = words.next()?.parse().ok()?;
            return (1..=10_000).contains(&level).then_some((name, level));
        }
        if character_name(line) {
            name = Some(line);
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::character_header;

    #[test]
    fn character_header_requires_a_valid_game_level() {
        assert_eq!(
            character_header("Hero \nLevel 18 Orc Shaman"),
            Some((Some("Hero"), 18))
        );
        assert_eq!(character_header("Level 20 Orc Shaman"), Some((None, 20)));
        assert_eq!(character_header("vel 27 Orc Shaman"), Some((None, 27)));
        assert!(character_header("Run Stats\nHealth 839").is_none());
        assert!(character_header("Hero\nLevel 0").is_none());
    }
}

fn recognize(
    engine: &OcrEngine,
    pixels: &[u8],
    width: usize,
    height: usize,
) -> windows::core::Result<OcrResult> {
    let writer = DataWriter::new()?;
    writer.WriteBytes(pixels)?;
    let buffer = writer.DetachBuffer()?;
    let bitmap = SoftwareBitmap::CreateWithAlpha(
        BitmapPixelFormat::Bgra8,
        width as i32,
        height as i32,
        BitmapAlphaMode::Ignore,
    )?;
    bitmap.CopyFromBuffer(&buffer)?;
    engine.RecognizeAsync(&bitmap)?.get()
}

fn stat_rows(result: &OcrResult) -> windows::core::Result<String> {
    struct Word {
        x: f32,
        center: f32,
        height: f32,
        text: String,
    }
    let lines = result.Lines()?;
    let mut words = Vec::new();
    for index in 0..lines.Size()? {
        let line = lines.GetAt(index)?.Words()?;
        for index in 0..line.Size()? {
            let word = line.GetAt(index)?;
            let bounds = word.BoundingRect()?;
            words.push(Word {
                x: bounds.X,
                center: bounds.Y + bounds.Height / 2.0,
                height: bounds.Height,
                text: word.Text()?.to_string_lossy(),
            });
        }
    }
    words.sort_by(|a, b| a.center.total_cmp(&b.center));
    let mut text = String::new();
    let mut remaining = words.as_mut_slice();
    while let Some(first) = remaining.first() {
        let end = remaining
            .partition_point(|word| word.center - first.center <= (first.height * 0.5).max(3.0));
        let (row, rest) = remaining.split_at_mut(end);
        row.sort_by(|a, b| a.x.total_cmp(&b.x));
        for word in row {
            text.push_str(&word.text);
            text.push(' ');
        }
        text.push('\n');
        remaining = rest;
    }
    Ok(text)
}

fn stats_from_text(text: &str) -> Vec<String> {
    let labels = ["Health", "Damage", "Area", "Gold", "Luck"];
    let mut values = Vec::new();
    for label in labels {
        let mut matches = text.lines().filter_map(|line| stat_on_row(line, label));
        if let Some(value) = matches.next() {
            if matches.next().is_none() {
                values.push(value);
            }
        }
    }
    values
}

fn stat_on_row(row: &str, label: &str) -> Option<String> {
    let normalized = row.replace(':', " ");
    let words: Vec<_> = normalized.split_whitespace().collect();
    let label_words: Vec<_> = label.split_whitespace().collect();
    for (offset, candidate) in words.windows(label_words.len()).enumerate() {
        if !candidate
            .iter()
            .zip(&label_words)
            .all(|(word, expected)| word.eq_ignore_ascii_case(expected))
        {
            continue;
        }
        let index = offset + label_words.len();
        let displayed = words.get(index)?;
        if label == "Health" {
            let value = displayed.replace(',', "").parse::<u32>().ok()?;
            return (value > 0).then(|| format!("HP {displayed}"));
        }
        let (displayed, next) = if *displayed == "+" {
            (format!("+{}", words.get(index + 1)?), index + 2)
        } else {
            (displayed.to_string(), index + 1)
        };
        let number = match displayed.strip_suffix('%') {
            Some(number) => number,
            None if words.get(next) == Some(&"%") => &displayed,
            None => continue,
        };
        let value = number.parse::<f32>().ok()?;
        return (value.is_finite() && value >= 0.0).then(|| format!("{label} {number}%"));
    }
    None
}

fn game_window(pid: u32) -> HWND {
    struct Search {
        pid: u32,
        hwnd: HWND,
    }
    unsafe extern "system" fn visit(hwnd: HWND, data: LPARAM) -> i32 {
        unsafe {
            let search = &mut *(data as *mut Search);
            let mut owner = 0;
            GetWindowThreadProcessId(hwnd, &mut owner);
            if owner == search.pid && IsWindowVisible(hwnd) != 0 && IsIconic(hwnd) == 0 {
                let mut title = [0u16; 64];
                let len = GetWindowTextW(hwnd, title.as_mut_ptr(), title.len() as i32) as usize;
                if String::from_utf16_lossy(&title[..len]).eq_ignore_ascii_case("Azeroth Survivors")
                {
                    search.hwnd = hwnd;
                    return 0;
                }
            }
            1
        }
    }
    unsafe {
        let mut search = Search {
            pid,
            hwnd: null_mut(),
        };
        EnumWindows(Some(visit), (&mut search as *mut Search) as LPARAM);
        search.hwnd
    }
}

fn capture(pid: u32) -> io::Result<Frame> {
    let hwnd = game_window(pid);
    unsafe {
        let mut bounds: RECT = std::mem::zeroed();
        if hwnd.is_null() || GetClientRect(hwnd, &mut bounds) == 0 {
            return Err(io::Error::other("Game window is unavailable."));
        }
        let width = bounds.right - bounds.left;
        let height = bounds.bottom - bounds.top;
        if !(200..=4096).contains(&width) || !(200..=4096).contains(&height) {
            return Err(io::Error::other(
                "Game window size is outside the capture range.",
            ));
        }
        let mut info: BITMAPINFO = std::mem::zeroed();
        info.bmiHeader.biSize = size_of_val(&info.bmiHeader) as u32;
        info.bmiHeader.biWidth = width;
        info.bmiHeader.biHeight = -height;
        info.bmiHeader.biPlanes = 1;
        info.bmiHeader.biBitCount = 32;
        info.bmiHeader.biCompression = BI_RGB;
        let dc = CreateCompatibleDC(null_mut());
        if dc.is_null() {
            return Err(io::Error::last_os_error());
        }
        let mut bits = null_mut();
        let bitmap = CreateDIBSection(dc, &info, DIB_RGB_COLORS, &mut bits, null_mut(), 0);
        if bitmap.is_null() || bits.is_null() {
            if !bitmap.is_null() {
                DeleteObject(bitmap);
            }
            DeleteDC(dc);
            return Err(io::Error::other("Unable to allocate a game screenshot."));
        }
        let previous = SelectObject(dc, bitmap);
        let frame = if !previous.is_null() && PrintWindow(hwnd, dc, 3) != 0 {
            let stride = width as usize * 4;
            let crop_width = width as usize / 2;
            let height = height as usize;
            let source = slice::from_raw_parts(bits.cast::<u8>(), stride * height);
            let mut pixels = Vec::with_capacity(crop_width * height * 4);
            for row in source.chunks_exact(stride) {
                pixels.extend_from_slice(&row[..crop_width * 4]);
            }
            Ok(Frame {
                pixels,
                width: crop_width,
                height,
            })
        } else {
            Err(io::Error::other("The game did not return a screenshot."))
        };
        if !previous.is_null() {
            SelectObject(dc, previous);
        }
        DeleteObject(bitmap);
        DeleteDC(dc);
        frame
    }
}
