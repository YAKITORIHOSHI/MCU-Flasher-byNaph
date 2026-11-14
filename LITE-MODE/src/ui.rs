use crate::bridge::{Bridge, Event};
use eframe::egui::{
    self, Align, Align2, Color32, FontFamily, FontId, RichText, Sense, Stroke, Vec2,
};
use serde::Deserialize;
use serde_json::{Value, json};
use std::collections::{HashSet, VecDeque};
use std::sync::mpsc;
use std::time::{Duration, Instant};
use unicode_width::UnicodeWidthStr;

const INK: Color32 = Color32::from_rgb(23, 25, 24);
const PAPER: Color32 = Color32::from_rgb(246, 243, 232);
const WHITE: Color32 = Color32::from_rgb(255, 253, 246);
const YELLOW: Color32 = Color32::from_rgb(245, 220, 72);
const GREEN: Color32 = Color32::from_rgb(179, 231, 172);
const CORAL: Color32 = Color32::from_rgb(255, 174, 151);
const MUTED: Color32 = Color32::from_rgb(89, 94, 88);
const TERMINAL: Color32 = Color32::from_rgb(26, 30, 28);
const LIMIT: usize = 4000;

fn log_columns(text: &str) -> usize {
    text.split('\t').map(UnicodeWidthStr::width).sum::<usize>() + text.matches('\t').count() * 4
}

/// One role per color: yellow marks the primary action or the selected item,
/// coral marks stopping or an error, everything else is the paper surface.
#[derive(Clone, Copy, PartialEq)]
enum Tone {
    Primary,
    Secondary,
    Danger,
}

#[derive(Clone, Copy)]
struct Palette {
    paper: Color32,
    surface: Color32,
    ink: Color32,
    muted: Color32,
    border: Color32,
    shadow: Color32,
    terminal: Color32,
    track: Color32,
    handle: Color32,
    highlight: Color32,
    disabled: Color32,
    dark: bool,
}

impl Palette {
    fn from_context(ctx: &egui::Context) -> Self {
        Self::new(ctx.style().visuals.dark_mode)
    }

    fn new(dark: bool) -> Self {
        if dark {
            Self {
                paper: Color32::from_rgb(28, 32, 30),
                surface: Color32::from_rgb(40, 46, 42),
                ink: Color32::from_rgb(239, 242, 234),
                muted: Color32::from_rgb(180, 190, 182),
                border: Color32::from_rgb(108, 118, 110),
                shadow: Color32::from_rgb(8, 10, 9),
                terminal: TERMINAL,
                track: Color32::from_rgb(46, 52, 48),
                handle: Color32::from_rgb(94, 102, 96),
                highlight: Color32::from_rgb(87, 75, 26),
                disabled: Color32::from_rgb(50, 56, 52),
                dark,
            }
        } else {
            Self {
                paper: PAPER,
                surface: WHITE,
                ink: INK,
                muted: MUTED,
                border: INK,
                shadow: INK,
                terminal: WHITE,
                track: Color32::from_rgb(234, 231, 219),
                handle: Color32::from_rgb(178, 175, 160),
                highlight: Color32::from_rgb(251, 237, 166),
                disabled: Color32::from_rgb(229, 227, 216),
                dark,
            }
        }
    }

    /// Fill, ink and outline for a button of this tone.
    fn tone(self, tone: Tone) -> (Color32, Color32, Color32) {
        match tone {
            Tone::Primary => (YELLOW, INK, INK),
            Tone::Danger => (CORAL, INK, INK),
            Tone::Secondary => (self.surface, self.ink, self.border),
        }
    }

    fn log_color(self, tone: LogTone) -> Color32 {
        match (self.dark, tone) {
            (false, LogTone::Error) => Color32::from_rgb(153, 44, 30),
            (false, LogTone::Success) => Color32::from_rgb(33, 100, 43),
            (false, LogTone::Warning) => Color32::from_rgb(113, 79, 0),
            (true, LogTone::Error) => CORAL,
            (true, LogTone::Success) => GREEN,
            (true, LogTone::Warning) => YELLOW,
            (_, LogTone::Dim) => self.muted,
            _ => self.ink,
        }
    }
}

fn bold(size: f32) -> FontId {
    FontId::new(size, FontFamily::Name("bold".into()))
}

fn heading(ui: &mut egui::Ui, text: &str) {
    ui.label(RichText::new(text).font(bold(18.0)));
}

/// Small uppercase caption shared by Connection fields and the status boxes.
fn caption(ui: &mut egui::Ui, text: &str) {
    let palette = Palette::from_context(ui.ctx());
    ui.label(
        RichText::new(text.to_uppercase())
            .font(bold(10.5))
            .color(palette.muted),
    );
}

fn scroll_style() -> egui::style::ScrollStyle {
    egui::style::ScrollStyle {
        foreground_color: true,
        ..egui::style::ScrollStyle::solid()
    }
}

#[derive(Default)]
struct LogoGesture {
    count: usize,
    last: Option<Instant>,
    pointer_armed: bool,
    pointer_frame: Option<u64>,
}

impl LogoGesture {
    fn click(&mut self, now: Instant) {
        if self
            .last
            .is_none_or(|last| now.duration_since(last) >= Duration::from_millis(800))
        {
            self.count = 0;
        }
        self.count = self.count.saturating_add(1);
        self.last = Some(now);
    }
    fn take_ready(&mut self, now: Instant) -> bool {
        if self
            .last
            .is_some_and(|last| now.duration_since(last) >= Duration::from_millis(800))
        {
            self.last = None;
            return std::mem::take(&mut self.count) == 5;
        }
        false
    }
}

fn icon(name: &str) -> egui::ImageSource<'static> {
    match name {
        "compile" => egui::include_image!("../assets/compile.svg"),
        "upload" => egui::include_image!("../assets/upload.svg"),
        "clean" => egui::include_image!("../assets/clean.svg"),
        "resources" => egui::include_image!("../assets/resources.svg"),
        "folder" => egui::include_image!("../assets/folder.svg"),
        "serial" => egui::include_image!("../assets/serial.svg"),
        "refresh" => egui::include_image!("../assets/refresh.svg"),
        "stop" => egui::include_image!("../assets/stop.svg"),
        "board" => egui::include_image!("../assets/board.svg"),
        "sun" => egui::include_image!("../assets/sun.svg"),
        "moon" => egui::include_image!("../assets/moon.svg"),
        _ => egui::include_image!("../assets/lite.svg"),
    }
}

fn logo_button(ui: &mut egui::Ui, gesture: &mut LogoGesture, now: Instant) -> egui::Response {
    let response = ui
        .add(
            egui::Image::new(icon("lite"))
                .fit_to_exact_size(egui::vec2(38.0, 38.0))
                .sense(Sense::click()),
        )
        .on_hover_cursor(egui::CursorIcon::PointingHand);
    // Native hosts may deliver several releases in one rendered frame.
    // Response::clicked is only a boolean, and a layout retry sees the same
    // input again. Process the ordered pointer events once per frame instead.
    let frame = ui.ctx().cumulative_frame_nr();
    if gesture.pointer_frame != Some(frame) {
        gesture.pointer_frame = Some(frame);
        let events = ui.input(|input| input.events.clone());
        {
            for event in &events {
                match event {
                    egui::Event::PointerButton {
                        pos,
                        button: egui::PointerButton::Primary,
                        pressed,
                        ..
                    } => {
                        let inside = ui.is_enabled()
                            && response.rect.contains(*pos)
                            && ui.ctx().layer_id_at(*pos) == Some(ui.layer_id());
                        if *pressed {
                            gesture.pointer_armed = inside;
                        } else {
                            if gesture.pointer_armed && inside {
                                gesture.click(now);
                            }
                            gesture.pointer_armed = false;
                        }
                    }
                    egui::Event::PointerGone => gesture.pointer_armed = false,
                    _ => {}
                }
            }
        }
    }
    response
}

const BOARD_ROW_HEIGHT: f32 = 48.0;

fn board_row(ui: &mut egui::Ui, name: &str, detail: &str, selected: bool) -> egui::Response {
    let palette = Palette::from_context(ui.ctx());
    let (rect, response) = ui.allocate_exact_size(
        egui::vec2(ui.available_width(), BOARD_ROW_HEIGHT),
        Sense::click(),
    );
    if ui.is_rect_visible(rect) {
        let fill = if selected {
            ui.visuals().selection.bg_fill
        } else if response.hovered() {
            palette.highlight
        } else {
            palette.surface
        };
        ui.painter().rect_filled(rect, 0, fill);
        ui.painter().rect_stroke(
            rect.shrink(1.0),
            0,
            Stroke::new(if selected { 2.0_f32 } else { 1.0_f32 }, palette.border),
            egui::StrokeKind::Inside,
        );
        let width = (rect.width() - 20.0).max(1.0);
        for (text, font, color, top) in [
            (name, bold(14.0), palette.ink, 6.0),
            (detail, FontId::proportional(11.0), palette.muted, 28.0),
        ] {
            let galley = egui::WidgetText::from(RichText::new(text).font(font).color(color))
                .into_galley(
                    ui,
                    Some(egui::TextWrapMode::Truncate),
                    width,
                    egui::TextStyle::Body,
                );
            ui.painter()
                .with_clip_rect(rect.intersect(ui.clip_rect()))
                .galley(rect.min + egui::vec2(10.0, top), galley, color);
        }
    }
    response.widget_info(|| {
        egui::WidgetInfo::selected(
            egui::WidgetType::SelectableLabel,
            ui.is_enabled(),
            selected,
            name,
        )
    });
    response
        .on_hover_cursor(egui::CursorIcon::PointingHand)
        .on_hover_text(format!("{name}\n{detail}"))
}

pub fn app_icon() -> egui::IconData {
    let options = resvg::usvg::Options::default();
    if let Ok(tree) = resvg::usvg::Tree::from_data(include_bytes!("../assets/lite.svg"), &options)
        && let Some(mut pixmap) = resvg::tiny_skia::Pixmap::new(128, 128)
    {
        resvg::render(
            &tree,
            resvg::tiny_skia::Transform::identity(),
            &mut pixmap.as_mut(),
        );
        return egui::IconData {
            rgba: pixmap.take(),
            width: 128,
            height: 128,
        };
    }
    egui::IconData::default()
}

const PROJECT_DIALOG: &str = "Open sketch";
const RESOURCES_DIALOG: &str = "Resources";
const BOARD_DIALOG: &str = "Choose your board";

/// Same sketch folder, ignoring trailing separators (and case on Windows).
fn same_folder(a: &str, b: &str) -> bool {
    let trim = |path: &str| path.trim().trim_end_matches(['/', '\\']).to_owned();
    let (a, b) = (trim(a), trim(b));
    if cfg!(windows) {
        a.eq_ignore_ascii_case(&b)
    } else {
        a == b
    }
}

/// A recent sketch row; a sketch open in another Lite window stays listed
/// but cannot be chosen until that window closes it.
fn recent_sketch(ui: &mut egui::Ui, path: &str, open_elsewhere: &[String]) -> bool {
    let name = std::path::Path::new(path)
        .file_name()
        .map(|name| name.to_string_lossy())
        .unwrap_or_default();
    let elsewhere = open_elsewhere.iter().any(|open| same_folder(open, path));
    let label = if elsewhere {
        format!("{name}  (open in another window)\n{path}")
    } else {
        format!("{name}\n{path}")
    };
    ui.add_enabled(!elsewhere, egui::Button::new(label).wrap())
        .on_disabled_hover_text("Already open in another Lite window")
        .clicked()
}

/// A dialog action row: horizontal rows get exactly their controls' height,
/// stacked rows grow downward. A centered layout given the rest of a
/// scrolling window would center its buttons in that whole height.
fn action_row<R>(
    ui: &mut egui::Ui,
    stacked: bool,
    height: f32,
    add: impl FnOnce(&mut egui::Ui) -> R,
) -> egui::InnerResponse<R> {
    let (size, layout) = if stacked {
        (
            egui::vec2(ui.available_width(), 0.0),
            egui::Layout::top_down(Align::Min),
        )
    } else {
        (
            egui::vec2(ui.available_width(), height),
            egui::Layout::left_to_right(Align::Center),
        )
    };
    ui.allocate_ui_with_layout(size, layout, add)
}

fn button(ui: &mut egui::Ui, label: &str, image: &str, tone: Tone, size: Vec2) -> egui::Response {
    let palette = Palette::from_context(ui.ctx());
    let (rect, mut response) = ui.allocate_exact_size(size, Sense::click());
    response = response.on_hover_cursor(egui::CursorIcon::PointingHand);
    response.widget_info(|| {
        egui::WidgetInfo::labeled(egui::WidgetType::Button, ui.is_enabled(), label)
    });
    let enabled = ui.is_enabled();
    let rect = rect.shrink2(egui::vec2(2.0, 3.0));
    let (fill, ink, outline) = if enabled {
        palette.tone(tone)
    } else {
        (palette.disabled, palette.muted, palette.border)
    };
    // Raised buttons sit on a hard shadow and press into it; disabled ones lie flat.
    let rect = if enabled {
        ui.painter()
            .rect_filled(rect.translate(egui::vec2(3.0, 3.0)), 0, palette.shadow);
        if response.is_pointer_button_down_on() {
            rect.translate(egui::vec2(3.0, 3.0))
        } else {
            rect
        }
    } else {
        rect
    };
    let fill = if enabled && response.hovered() {
        if tone == Tone::Secondary {
            palette.highlight
        } else {
            fill.lerp_to_gamma(INK, 0.07)
        }
    } else {
        fill
    };
    ui.painter().rect(
        rect,
        0,
        fill,
        Stroke::new(2.0_f32, outline),
        egui::StrokeKind::Inside,
    );
    let font = bold(if size.y < 36.0 { 13.0 } else { 15.0 });
    let icon_size = if image.is_empty() {
        0.0
    } else if size.y < 36.0 {
        16.0
    } else {
        20.0
    };
    let text_width = if label.is_empty() {
        0.0
    } else {
        ui.painter()
            .layout_no_wrap(label.to_owned(), font.clone(), ink)
            .size()
            .x
    };
    let gap = if icon_size > 0.0 && !label.is_empty() {
        8.0
    } else {
        0.0
    };
    let left = rect.center().x - (icon_size + gap + text_width) * 0.5;
    if icon_size > 0.0 {
        egui::Image::new(icon(image)).tint(ink).paint_at(
            ui,
            egui::Rect::from_center_size(
                egui::pos2(left + icon_size * 0.5, rect.center().y),
                egui::vec2(icon_size, icon_size),
            ),
        );
    }
    if !label.is_empty() {
        ui.painter().text(
            egui::pos2(left + icon_size + gap, rect.center().y),
            Align2::LEFT_CENTER,
            label,
            font,
            ink,
        );
    }
    response
}

/// A secondary button that opens a menu below itself.
fn menu_button(ui: &mut egui::Ui, label: &str, size: Vec2, contents: impl FnOnce(&mut egui::Ui)) {
    let response = button(ui, label, "", Tone::Secondary, size);
    egui::Popup::menu(&response)
        .width(size.x.max(240.0))
        .show(|ui| {
            ui.spacing_mut().button_padding = egui::vec2(8.0, 5.0);
            contents(ui);
        });
}

/// Monitor and Resources tabs: equal boxes, the selected one filled yellow.
fn tab(ui: &mut egui::Ui, selected: bool, label: &str) -> egui::Response {
    let palette = Palette::from_context(ui.ctx());
    let font = bold(14.0);
    let width = ui
        .painter()
        .layout_no_wrap(label.to_owned(), font.clone(), palette.ink)
        .size()
        .x;
    let (rect, response) = ui.allocate_exact_size(egui::vec2(width + 26.0, 32.0), Sense::click());
    let response = response.on_hover_cursor(egui::CursorIcon::PointingHand);
    response.widget_info(|| {
        egui::WidgetInfo::selected(
            egui::WidgetType::SelectableLabel,
            ui.is_enabled(),
            selected,
            label,
        )
    });
    let (fill, ink, outline) = if selected {
        (YELLOW, INK, INK)
    } else if response.hovered() {
        (palette.highlight, palette.ink, palette.border)
    } else {
        (palette.surface, palette.ink, palette.border)
    };
    ui.painter().rect(
        rect,
        0,
        fill,
        Stroke::new(2.0_f32, outline),
        egui::StrokeKind::Inside,
    );
    ui.painter()
        .text(rect.center(), Align2::CENTER_CENTER, label, font, ink);
    response
}

fn hard_shadow(palette: Palette, offset: i8) -> egui::epaint::Shadow {
    egui::epaint::Shadow {
        offset: [offset, offset],
        blur: 0,
        spread: 0,
        color: palette.shadow,
    }
}

/// Workspace cards are surface sheets; dialogs sit on paper.
fn card(ctx: &egui::Context, fill: Color32) -> egui::Frame {
    let palette = Palette::from_context(ctx);
    egui::Frame::new()
        .fill(if fill == PAPER {
            palette.paper
        } else {
            palette.surface
        })
        .stroke(Stroke::new(2.0_f32, palette.border))
        .inner_margin(12)
        .shadow(hard_shadow(palette, 4))
}

fn status_box(ui: &mut egui::Ui, label: &str, value: &str, width: f32) {
    let palette = Palette::from_context(ui.ctx());
    egui::Frame::new()
        .fill(palette.surface)
        .stroke(Stroke::new(2.0_f32, palette.border))
        .inner_margin(6)
        .show(ui, |ui| {
            ui.set_width((width - 16.0).max(50.0));
            ui.spacing_mut().item_spacing.y = 2.0;
            caption(ui, label);
            ui.add(egui::Label::new(RichText::new(value).size(12.0)).truncate())
                .on_hover_text(value);
        });
}

#[derive(Clone, Default, Deserialize)]
struct Board {
    name: String,
    #[serde(default)]
    platform: String,
    #[serde(default)]
    mcu: String,
}

#[derive(Clone, Default, Deserialize)]
struct Port {
    device: String,
    #[serde(default)]
    description: String,
    /// Selected in another MCU Flasher or Lite window.
    #[serde(default)]
    occupied: bool,
}

#[derive(Clone, Default, Deserialize)]
struct Library {
    name: String,
    owner: String,
    version: String,
    description: String,
    spec: String,
}

#[derive(Clone, Copy, PartialEq)]
enum LogTone {
    Info,
    Error,
    Warning,
    Success,
    Dim,
}

impl LogTone {
    fn from_tag(tag: &str) -> Self {
        match tag {
            "error" => Self::Error,
            "warning" => Self::Warning,
            "success" => Self::Success,
            "dim" => Self::Dim,
            _ => Self::Info,
        }
    }
    fn diagnostic(self) -> bool {
        matches!(self, Self::Error | Self::Warning)
    }
}

struct LogLine {
    text: String,
    tone: LogTone,
    progress_key: Option<String>,
}

#[derive(Default)]
struct Log {
    lines: VecDeque<LogLine>,
    partial: String,
    max_columns: usize,
}

impl Log {
    fn push(&mut self, text: &str, tag: &str, newline: bool) {
        self.push_progress(text, tag, newline, None);
    }

    fn push_progress(&mut self, text: &str, tag: &str, newline: bool, progress_key: Option<&str>) {
        let tone = LogTone::from_tag(tag);
        let progress_key = progress_key.filter(|key| {
            !key.is_empty() && newline && !text.contains(['\n', '\r']) && !tone.diagnostic()
        });
        if let Some(key) = progress_key {
            self.max_columns = self.max_columns.max(log_columns(text));
            if let Some(line) =
                self.lines.iter_mut().rev().find(|line| {
                    line.progress_key.as_deref() == Some(key) && !line.tone.diagnostic()
                })
            {
                line.text = text.to_owned();
                line.tone = tone;
                return;
            }
        }
        if text.is_empty() && newline && self.partial.is_empty() {
            self.lines.push_back(LogLine {
                text: String::new(),
                tone,
                progress_key: None,
            });
        }
        // Decode split serial lines without turning every incoming chunk into a
        // newline. Bound a device that transmits indefinitely without line ends.
        for part in text.split_inclusive('\n') {
            let cleaned = part.replace('\r', "");
            self.partial.push_str(&cleaned);
            self.max_columns = self.max_columns.max(log_columns(&self.partial));
            if part.ends_with('\n') || self.partial.len() > 8192 {
                self.lines.push_back(LogLine {
                    text: std::mem::take(&mut self.partial)
                        .trim_end_matches('\n')
                        .to_owned(),
                    tone,
                    progress_key: progress_key.map(str::to_owned),
                });
            }
        }
        if newline && !self.partial.is_empty() {
            self.lines.push_back(LogLine {
                text: std::mem::take(&mut self.partial),
                tone,
                progress_key: progress_key.map(str::to_owned),
            });
        }
        while self.lines.len() > LIMIT {
            self.lines.pop_front();
        }
    }

    fn text(&self) -> String {
        self.lines
            .iter()
            .map(|line| line.text.as_str())
            .chain(std::iter::once(self.partial.as_str()))
            .collect::<Vec<_>>()
            .join("\n")
    }
}

pub struct LiteApp {
    bridge: Option<Bridge>,
    ready: bool,
    offline_preview: bool,
    busy: bool,
    pending: bool,
    next_id: u64,
    awaiting: HashSet<u64>,
    resource_busy: bool,
    stop_enabled: bool,
    stop_reason: String,
    compact_controls: bool,
    last_monitor: Option<(Vec2, f32)>,
    phase: String,
    error: String,
    source: String,
    recent_projects: Vec<String>,
    recent_boards: Vec<String>,
    files: Vec<String>,
    project_input: String,
    project_dialog: bool,
    /// `--project <path>`: the sketch this window was opened for.
    startup_project: Option<String>,
    /// Sketch folders open in other Lite windows.
    open_elsewhere: Vec<String>,
    board: String,
    platform: String,
    board_dialog: bool,
    board_candidate: String,
    board_focus_search: bool,
    board_query: String,
    board_query_at: Instant,
    board_sent: String,
    boards: Vec<Board>,
    board_total: usize,
    board_fuzzy: bool,
    board_loading: bool,
    frameworks: Vec<String>,
    framework: String,
    ports: Vec<Port>,
    port: String,
    baud: u32,
    speed: u32,
    connected: bool,
    serial_tab: bool,
    build_log: Log,
    serial_log: Log,
    autoscroll: bool,
    send_text: String,
    line_ending: String,
    resources_open: bool,
    resource_tab: bool,
    library_query: String,
    libraries: Vec<Library>,
    state_path: String,
    last_poll: Instant,
    started: Instant,
    folder_tx: mpsc::Sender<Option<String>>,
    folder_rx: mpsc::Receiver<Option<String>>,
    desktop_tx: mpsc::Sender<String>,
    desktop_rx: mpsc::Receiver<String>,
    picker_busy: bool,
    capture: Option<std::path::PathBuf>,
    frames: usize,
    logo_gesture: LogoGesture,
    developer_open: bool,
    #[cfg(test)]
    monitor_geometry: Option<(egui::Rect, Vec2)>,
    #[cfg(test)]
    monitor_rows: Vec<egui::Rect>,
    #[cfg(test)]
    monitor_text_origins: Vec<f32>,
    #[cfg(test)]
    monitor_offset: Vec2,
    #[cfg(test)]
    board_geometry: Option<egui::Rect>,
    #[cfg(test)]
    board_rows: Vec<(String, egui::Rect)>,
    #[cfg(test)]
    stop_geometry: Option<(egui::Rect, bool)>,
    #[cfg(test)]
    controls_viewport: Option<egui::Rect>,
    #[cfg(test)]
    control_cards: Vec<egui::Rect>,
    #[cfg(test)]
    project_geometry: Option<egui::Rect>,
    #[cfg(test)]
    resource_geometry: Option<egui::Rect>,
    #[cfg(test)]
    monitor_frame: Option<egui::Rect>,
}

impl LiteApp {
    pub fn new(cc: &eframe::CreationContext<'_>) -> Self {
        let args: Vec<String> = std::env::args().collect();
        let offline_preview = args.iter().any(|arg| arg == "--preview");
        let mut app = Self::with_context(&cc.egui_ctx, offline_preview);
        app.startup_project = crate::desktop::startup_project(&args);
        app
    }

    fn with_context(ctx: &egui::Context, offline_preview: bool) -> Self {
        configure(ctx);
        let (folder_tx, folder_rx) = mpsc::channel();
        let (desktop_tx, desktop_rx) = mpsc::channel();
        let mut app = Self {
            bridge: if offline_preview {
                None
            } else {
                Some(Bridge::start(ctx.clone()))
            },
            ready: false,
            offline_preview,
            busy: false,
            pending: false,
            next_id: 0,
            awaiting: HashSet::new(),
            resource_busy: false,
            stop_enabled: false,
            stop_reason: "No cancellable operation is running".into(),
            compact_controls: false,
            last_monitor: None,
            phase: "Starting Python tools".into(),
            error: String::new(),
            source: String::new(),
            recent_projects: vec![],
            recent_boards: vec![],
            files: vec![],
            project_input: String::new(),
            project_dialog: false,
            startup_project: None,
            open_elsewhere: Vec::new(),
            board: String::new(),
            platform: String::new(),
            board_dialog: false,
            board_candidate: String::new(),
            board_focus_search: false,
            board_query: String::new(),
            board_query_at: Instant::now(),
            board_sent: "\0".into(),
            boards: vec![],
            board_total: 0,
            board_fuzzy: false,
            board_loading: false,
            frameworks: vec![],
            framework: String::new(),
            ports: vec![],
            port: String::new(),
            baud: 115200,
            speed: 460800,
            connected: false,
            serial_tab: false,
            build_log: Log::default(),
            serial_log: Log::default(),
            autoscroll: true,
            send_text: String::new(),
            line_ending: "both".into(),
            resources_open: false,
            resource_tab: false,
            library_query: String::new(),
            libraries: vec![],
            state_path: String::new(),
            last_poll: Instant::now(),
            started: Instant::now(),
            folder_tx,
            folder_rx,
            desktop_tx,
            desktop_rx,
            picker_busy: false,
            capture: std::env::args()
                .collect::<Vec<_>>()
                .windows(2)
                .find(|args| args[0] == "--capture")
                .map(|args| args[1].clone().into()),
            frames: 0,
            logo_gesture: LogoGesture::default(),
            developer_open: false,
            #[cfg(test)]
            monitor_geometry: None,
            #[cfg(test)]
            monitor_rows: vec![],
            #[cfg(test)]
            monitor_text_origins: vec![],
            #[cfg(test)]
            monitor_offset: Vec2::ZERO,
            #[cfg(test)]
            board_geometry: None,
            #[cfg(test)]
            board_rows: Vec::new(),
            #[cfg(test)]
            stop_geometry: None,
            #[cfg(test)]
            controls_viewport: None,
            #[cfg(test)]
            control_cards: Vec::new(),
            #[cfg(test)]
            project_geometry: None,
            #[cfg(test)]
            resource_geometry: None,
            #[cfg(test)]
            monitor_frame: None,
        };
        app.build_log.push("MCU Flasher / Lite\nOpen a sketch folder, select its exact board, then Compile or Upload.", "info", true);
        if offline_preview {
            if std::env::args().any(|arg| arg == "--preview-sketch") {
                app.source = if cfg!(windows) {
                    "C:\\Preview\\Blink"
                } else {
                    "/preview/Blink"
                }
                .into();
                app.files = vec!["Blink.ino".into(), "pins.h".into()];
            }
            if std::env::args().any(|arg| arg == "--preview-dark") {
                apply_theme(ctx, true);
            }
            app.ready = true;
            app.compact_controls = std::env::args().any(|arg| arg == "--preview-controls");
            app.serial_tab = std::env::args().any(|arg| arg == "--preview-serial");
            app.resources_open = std::env::args().any(|arg| arg == "--preview-resources");
            if std::env::args().any(|arg| arg == "--preview-resource-boards") {
                app.resources_open = true;
                app.resource_tab = true;
            }
            app.project_dialog = std::env::args().any(|arg| arg == "--preview-projects");
            app.board_dialog = std::env::args().any(|arg| arg == "--preview-boards");
            if app.board_dialog {
                app.boards = [
                    ("Arduino Uno [atmelavr:uno]", "atmelavr", "ATmega328P"),
                    ("Arduino Nano ATmega328 [atmelavr:nanoatmega328]", "atmelavr", "ATmega328P"),
                    ("ESP32 Development Board [espressif32:esp32dev]", "espressif32", "ESP32"),
                    ("Long vendor and product name to demonstrate stable board rows [ststm32:example]", "ststm32", "STM32"),
                ].map(|(name, platform, mcu)| Board {
                    name: name.into(), platform: platform.into(), mcu: mcu.into(),
                }).to_vec();
                app.board_total = app.boards.len();
            }
            if std::env::args().any(|arg| arg == "--preview-recents") {
                app.recent_projects =
                    vec!["/preview/WeatherStation".into(), "/preview/MySketch".into()];
                app.recent_boards = vec![
                    "Arduino Uno [atmelavr:uno]".into(),
                    "Arduino Nano ATmega328 [atmelavr:nanoatmega328]".into(),
                ];
            }
            app.phase = "Preview / no devices or tools".into();
            if std::env::args().any(|arg| arg == "--preview-error") {
                app.error = "Preview diagnostic: a long file path or tool failure remains in the Build monitor and is available in this notice's tooltip.".into();
            }
            app.build_log.push("This visual preview never starts Python, enumerates ports, writes settings or accesses hardware.", "warning", true);
        }
        app
    }

    fn send(&mut self, mut request: Value) {
        let tracked = !matches!(
            request["cmd"].as_str(),
            Some("status" | "ports" | "boards" | "history")
        );
        if self.bridge.is_none() && !tracked {
            return;
        }
        if tracked {
            self.next_id += 1;
            request["id"] = json!(self.next_id);
            self.awaiting.insert(self.next_id);
            self.pending = true;
        }
        let result = self
            .bridge
            .as_ref()
            .ok_or_else(|| "Visual preview: Python tools are disabled".to_owned())
            .and_then(|bridge| bridge.send(request));
        if let Err(error) = result {
            if tracked {
                self.awaiting.remove(&self.next_id);
            }
            self.pending = !self.awaiting.is_empty();
            self.error = error;
        }
    }

    fn action(&mut self, cmd: &str) {
        self.pending = true;
        self.error.clear();
        self.send(json!({"cmd": cmd}));
    }

    fn idle(&self) -> bool {
        self.ready && !self.busy && !self.pending && !self.resource_busy
    }

    fn take_developer_request(&mut self, now: Instant) -> bool {
        self.ready && !self.offline_preview && self.logo_gesture.take_ready(now)
    }

    fn action_problem(&self, upload: bool) -> Option<&'static str> {
        if !self.ready {
            Some("Wait for Python tools to start")
        } else if !self.idle() {
            Some("Wait for the current operation to finish")
        } else if self.source.is_empty() {
            Some("Open a sketch folder first")
        } else if self.board.is_empty() {
            Some("Choose a board first")
        } else if self.framework.is_empty() {
            Some("Choose the board's framework first")
        } else if upload
            && (self.port.is_empty() || !self.ports.iter().any(|port| port.device == self.port))
        {
            Some("Select a connected serial port before Upload")
        } else {
            None
        }
    }

    /// Like the main app's Project: the first sketch opens here; another
    /// sketch opens in its own window and this window keeps its sketch.
    fn open_project(&mut self, path: String) {
        self.error.clear();
        let path = path.trim().to_owned();
        if path.is_empty() {
            return;
        }
        if let Some(open) = self
            .open_elsewhere
            .iter()
            .find(|open| same_folder(open, &path))
        {
            let name = std::path::Path::new(open)
                .file_name()
                .map(|name| name.to_string_lossy().into_owned())
                .unwrap_or_else(|| open.clone());
            self.error = format!("{name} is already open in another Lite window.");
            return;
        }
        if self.source.is_empty() || same_folder(&path, &self.source) {
            self.pending = true;
            self.send(json!({"cmd": "project", "path": path}));
            return;
        }
        if self.offline_preview {
            return;
        }
        match crate::desktop::open_window(&path) {
            Ok(()) => {
                self.project_dialog = false;
                let name = std::path::Path::new(&path)
                    .file_name()
                    .map(|name| name.to_string_lossy().into_owned())
                    .unwrap_or_else(|| path.clone());
                self.build_log.push(
                    &format!("Opening {name} in a new Lite window."),
                    "info",
                    true,
                );
            }
            Err(error) => self.error = error,
        }
    }

    fn open_sketch_directory(&mut self, ctx: &egui::Context) {
        if self.offline_preview {
            return;
        }
        let directory = std::path::PathBuf::from(&self.source);
        let tx = self.desktop_tx.clone();
        let ctx = ctx.clone();
        if let Err(error) = std::thread::Builder::new()
            .name("lite-open-folder".into())
            .spawn(move || {
                if let Err(error) = crate::desktop::open_directory(&directory) {
                    let _ = tx.send(error);
                    ctx.request_repaint();
                }
            })
        {
            self.error = format!("Could not start the file manager: {error}");
        }
    }

    fn choose_board(&mut self, name: String) {
        self.error.clear();
        self.send(json!({"cmd": "board", "name": name}));
        self.board_dialog = false;
    }

    /// Dialogs are modal, like the main app's project selector: while one is
    /// open the workspace behind it takes no clicks, keyboard focus or popups.
    /// The board picker opens from Resources and stays above it.
    fn modal_dialogs(&mut self, ctx: &egui::Context) {
        let open = [
            (self.project_dialog, PROJECT_DIALOG),
            (self.resources_open, RESOURCES_DIALOG),
            (self.board_dialog, BOARD_DIALOG),
        ];
        let Some(top) = open
            .iter()
            .rev()
            .find(|(open, _)| *open)
            .map(|(_, title)| *title)
        else {
            return;
        };
        let palette = Palette::from_context(ctx);
        ctx.layer_painter(egui::LayerId::new(
            egui::Order::Middle,
            egui::Id::new("lite-modal-backdrop"),
        ))
        .rect_filled(
            ctx.content_rect(),
            0.0,
            Color32::from_black_alpha(if palette.dark { 120 } else { 45 }),
        );
        let layer = egui::LayerId::new(egui::Order::Foreground, egui::Id::new(top));
        ctx.move_to_top(layer);
        ctx.memory_mut(|memory| memory.set_modal_layer(layer));
        // The board picker handles its own Escape and Enter keys.
        if top != BOARD_DIALOG && ctx.input(|i| i.key_pressed(egui::Key::Escape)) {
            match top {
                PROJECT_DIALOG => self.project_dialog = false,
                _ => self.resources_open = false,
            }
        }
    }

    fn open_board_picker(&mut self) {
        self.board_candidate = self.board.clone();
        self.board_focus_search = true;
        self.board_dialog = true;
    }

    fn event(&mut self, event: Event) {
        let data = event.data;
        let text = |key: &str| data[key].as_str().unwrap_or("").to_owned();
        match event.event.as_str() {
            "ack" => {
                if let Some(id) = data["id"].as_u64() {
                    self.awaiting.remove(&id);
                }
                self.pending = !self.awaiting.is_empty();
            }
            "ready" => {
                self.ready = true;
                self.phase = "Ready".into();
                self.state_path = text("state_path");
                self.send(json!({"cmd": "boards", "query": ""}));
                self.send(json!({"cmd": "history"}));
                if let Some(path) = self.startup_project.take() {
                    self.pending = true;
                    self.send(json!({"cmd": "project", "path": path}));
                }
            }
            "history" => {
                self.recent_projects =
                    serde_json::from_value(data["projects"].clone()).unwrap_or_default();
                self.recent_boards =
                    serde_json::from_value(data["boards"].clone()).unwrap_or_default();
            }
            "state" => {
                self.busy = data["busy"].as_bool().unwrap_or(false);
                self.stop_enabled = data["stop"]["enabled"].as_bool().unwrap_or(false);
                self.stop_reason = data["stop"]["reason"]
                    .as_str()
                    .unwrap_or("Wait for operation status")
                    .to_owned();
                self.board = text("board");
                self.port = text("port");
                self.connected = data["monitor"].as_bool().unwrap_or(false);
                self.developer_open = data["developer_access"].as_bool().unwrap_or(false);
                self.baud = data["baud"].as_u64().unwrap_or(self.baud as u64) as u32;
                if !self.busy {
                    self.phase = "Ready".into();
                }
            }
            "ports" => self.ports = serde_json::from_value(data).unwrap_or_default(),
            "sketches" => {
                self.open_elsewhere =
                    serde_json::from_value(data["open_elsewhere"].clone()).unwrap_or_default()
            }
            "boards" => {
                if text("query") == self.board_query {
                    self.boards = serde_json::from_value(data["items"].clone()).unwrap_or_default();
                    self.board_total =
                        data["total"].as_u64().unwrap_or(self.boards.len() as u64) as usize;
                    self.board_fuzzy = data["fuzzy"].as_bool().unwrap_or(false);
                    self.board_loading = false;
                }
            }
            "project" => {
                self.source = text("path");
                self.project_input = self.source.clone();
                self.files = serde_json::from_value(data["files"].clone()).unwrap_or_default();
                self.project_dialog = false;
            }
            "target" => {
                self.board = text("name");
                self.platform = text("platform");
                self.framework = text("framework");
                self.frameworks =
                    serde_json::from_value(data["frameworks"].clone()).unwrap_or_default();
            }
            "console:log" => self.build_log.push_progress(
                &text("text"),
                &text("tag"),
                data["newline"].as_bool().unwrap_or(true),
                data["progress_key"].as_str(),
            ),
            "diagnostic" => self.build_log.push(&text("text"), "dim", true),
            "serial:log" => self.serial_log.push(
                &text("text"),
                &text("tag"),
                data["newline"].as_bool().unwrap_or(false),
            ),
            "console:clear" => self.build_log = Log::default(),
            "serial:clear" => self.serial_log = Log::default(),
            "serial:status" => {
                self.connected = data["connected"].as_bool().unwrap_or(false);
                if let Some(baud) = data["baud"].as_u64() {
                    self.baud = baud as u32;
                }
            }
            "operation:phase" => {
                self.busy = data["is_busy"].as_bool().unwrap_or(false);
                self.phase = text("phase");
                if !self.busy
                    || matches!(
                        self.phase.as_str(),
                        "flashing" | "writing" | "erasing" | "resetting" | "cleaning"
                    )
                {
                    // A log burst can split phase and policy across UI batches.
                    // Close the old cancellation affordance immediately; only
                    // the worker's authority may enable it again.
                    self.stop_enabled = false;
                    self.stop_reason = if self.busy {
                        "Wait for the protected operation to finish"
                    } else {
                        "No cancellable operation is running"
                    }
                    .into();
                }
            }
            "console:progress" => {
                let action = text("action");
                if !action.is_empty() {
                    self.phase = action;
                }
            }
            "resource:busy" => {
                self.resource_busy = data.as_bool().unwrap_or(false);
            }
            "stop:state" => {
                self.stop_enabled = data["enabled"].as_bool().unwrap_or(false);
                self.stop_reason = text("reason");
            }
            "libraries" => {
                if text("query") == self.library_query {
                    self.libraries =
                        serde_json::from_value(data["items"].clone()).unwrap_or_default();
                }
            }
            "resource:installed" => {
                self.phase = format!("Installed {}", text("spec"));
            }
            "developer:status" => self.developer_open = data.as_bool().unwrap_or(false),
            "developer:ready" => {
                if let Some(pid) = data["pid"].as_u64().and_then(|pid| u32::try_from(pid).ok())
                    && crate::desktop::allow_portal_activation(pid)
                {
                    self.send(json!({"cmd": "developer_activate"}));
                }
            }
            "error" | "fatal" => {
                self.error = text("message");
                if event.event == "fatal" {
                    self.pending = false;
                    self.awaiting.clear();
                    self.ready = false;
                    self.developer_open = false;
                    self.busy = false;
                    self.resource_busy = false;
                    self.stop_enabled = false;
                    self.stop_reason = "Python tools are unavailable".into();
                    self.phase = "Tools unavailable".into();
                }
                self.build_log.push(&self.error, "error", true);
            }
            "notification" if data["type"].as_str() == Some("error") => {
                self.error = text("message")
            }
            _ => {}
        }
    }

    fn project_picker(&mut self, ctx: &egui::Context) {
        let palette = Palette::from_context(ctx);
        let mut open = self.project_dialog;
        let max_height = (ctx.content_rect().height() - 96.0).max(80.0);
        let window = egui::Window::new(PROJECT_DIALOG).open(&mut open).collapsible(false).resizable(true).order(egui::Order::Foreground)
            .constrain_to(ctx.content_rect().shrink(8.0))
            .min_width(220.0).min_height(0.0)
            .max_width((ctx.content_rect().width() - 64.0).max(220.0))
            .max_height(max_height).default_width(520.0).default_height(0.0)
            .frame(card(ctx, PAPER)).show(ctx, |ui| {
                // The window's own scroll area never shrinks, which held this short
                // dialog at egui's default 420-point height. This one fits the
                // content and scrolls only when the screen is shorter than it.
                egui::ScrollArea::vertical().id_salt("open-sketch-body").auto_shrink([false, true])
                    .min_scrolled_height(max_height).max_height(max_height).show(ui, |ui| {
                ui.label("Select a folder with .ino, .cpp or .c files at its root.");
                ui.add_space(12.0);
                ui.add(egui::TextEdit::singleline(&mut self.project_input).hint_text("Full sketch folder path").margin(egui::vec2(6.0, 6.0)).desired_width(f32::INFINITY));
                ui.add_space(12.0);
                let stacked = ui.available_width() < 287.0;
                action_row(ui, stacked, 40.0, |ui| {
                    ui.add_enabled_ui(!self.picker_busy, |ui| {
                        if button(ui, "Browse", "folder", Tone::Secondary, egui::vec2(124.0, 40.0)).clicked() {
                            self.picker_busy = true;
                            let tx = self.folder_tx.clone(); let ctx = ctx.clone();
                            std::thread::spawn(move || {
                                let path = rfd::FileDialog::new().set_title("Open Lite sketch folder").pick_folder().map(|p| p.to_string_lossy().into_owned());
                                let _ = tx.send(path); ctx.request_repaint();
                            });
                        }
                    });
                    ui.add_enabled_ui(!self.project_input.trim().is_empty() && self.idle(), |ui| {
                        if button(ui, "Open sketch", "folder", Tone::Primary, egui::vec2(155.0, 40.0)).clicked() {
                            self.open_project(self.project_input.clone());
                        }
                    });
                });
                ui.add_space(14.0);
                ui.label(RichText::new("Recently opened").family(FontFamily::Name("bold".into())));
                let mut selected = None;
                egui::ScrollArea::vertical().id_salt("recent-projects-picker").max_height(190.0).show(ui, |ui| {
                    if self.recent_projects.is_empty() { ui.label("Opened sketches will appear here."); }
                    for path in &self.recent_projects {
                        if recent_sketch(ui, path, &self.open_elsewhere) { selected = Some(path.clone()); }
                    }
                });
                if let Some(path) = selected { self.open_project(path); }
                ui.add_space(8.0); ui.label(RichText::new("Edit files in your usual editor. Lite reads saved files before every build.").small().color(palette.muted));
                });
            });
        #[cfg(test)]
        {
            self.project_geometry = window.map(|window| window.response.rect);
        }
        #[cfg(not(test))]
        let _ = window;
        self.project_dialog = open;
    }

    fn board_picker(&mut self, ctx: &egui::Context) {
        #[cfg(test)]
        self.board_rows.clear();
        let palette = Palette::from_context(ctx);
        let mut open = self.board_dialog;
        let mut confirm = false;
        let mut cancel = false;
        let recent = if self.board_query.trim().is_empty() {
            self.recent_boards.clone()
        } else {
            Vec::new()
        };
        let names: Vec<String> = recent
            .iter()
            .cloned()
            .chain(
                self.boards
                    .iter()
                    .filter(|board| !recent.contains(&board.name))
                    .map(|board| board.name.clone()),
            )
            .collect();
        let mut moved = false;
        if ctx.input(|i| i.key_pressed(egui::Key::ArrowDown) || i.key_pressed(egui::Key::ArrowUp))
            && !names.is_empty()
        {
            let index = names.iter().position(|name| name == &self.board_candidate);
            let down = ctx.input(|i| i.key_pressed(egui::Key::ArrowDown));
            let next = match (index, down) {
                (Some(index), true) => (index + 1).min(names.len() - 1),
                (Some(index), false) => index.saturating_sub(1),
                (None, true) => 0,
                (None, false) => names.len() - 1,
            };
            self.board_candidate = names[next].clone();
            moved = true;
        }
        let height = (ctx.content_rect().height() - 238.0).clamp(BOARD_ROW_HEIGHT, 350.0);
        let window = egui::Window::new(BOARD_DIALOG)
            .open(&mut open)
            .order(egui::Order::Foreground)
            .collapsible(false)
            .resizable(true)
            .constrain_to(ctx.content_rect().shrink(8.0))
            .min_width(220.0)
            .min_height(0.0)
            .max_width((ctx.content_rect().width() - 64.0).max(220.0))
            .max_height((ctx.content_rect().height() - 96.0).max(80.0))
            .vscroll(true)
            .default_width(560.0)
            .frame(card(ctx, PAPER))
            .show(ctx, |ui| {
                let search = ui.add(
                    egui::TextEdit::singleline(&mut self.board_query)
                        .hint_text("Search name, board ID, MCU, vendor or framework")
                        .margin(egui::vec2(6.0, 6.0))
                        .desired_width(f32::INFINITY),
                );
                if self.board_focus_search {
                    search.request_focus();
                    self.board_focus_search = false;
                }
                if search.changed() {
                    self.board_query_at = Instant::now();
                    self.boards.clear();
                    self.board_loading = true;
                    self.board_candidate.clear();
                }
                ui.add_space(8.0);
                egui::ScrollArea::vertical()
                    .id_salt("board-results")
                    .max_height(height)
                    .min_scrolled_height(height)
                    .auto_shrink([false, false])
                    .show(ui, |ui| {
                        ui.spacing_mut().item_spacing.y = 3.0;
                        if !recent.is_empty() {
                            caption(ui, "Recent boards");
                            for name in &recent {
                                let detail = self
                                    .boards
                                    .iter()
                                    .find(|b| &b.name == name)
                                    .map(|b| format!("{} / {}", b.platform, b.mcu))
                                    .unwrap_or_else(|| "Recently selected target".into());
                                let response =
                                    board_row(ui, name, &detail, self.board_candidate == *name);
                                #[cfg(test)]
                                self.board_rows.push((name.clone(), response.rect));
                                if response.clicked() {
                                    self.board_candidate = name.clone();
                                }
                                if response.double_clicked() {
                                    confirm = true;
                                }
                                if moved && self.board_candidate == *name {
                                    response.scroll_to_me(Some(Align::Center));
                                }
                            }
                            caption(ui, "All boards");
                        }
                        for board in &self.boards {
                            if recent.contains(&board.name) {
                                continue;
                            }
                            let detail = format!("{} / {}", board.platform, board.mcu);
                            let response = board_row(
                                ui,
                                &board.name,
                                &detail,
                                self.board_candidate == board.name,
                            );
                            #[cfg(test)]
                            self.board_rows.push((board.name.clone(), response.rect));
                            if response.clicked() {
                                self.board_candidate = board.name.clone();
                            }
                            if response.double_clicked() {
                                confirm = true;
                            }
                            if moved && self.board_candidate == board.name {
                                response.scroll_to_me(Some(Align::Center));
                            }
                        }
                        if self.board_loading {
                            ui.label("Searching boards...");
                        } else if self.boards.is_empty() && recent.is_empty() {
                            ui.label("No matching boards. Try a board ID, MCU, or a shorter name.");
                        }
                    });
                ui.add_space(6.0);
                let summary = if self.board_fuzzy {
                    format!(
                        "{} closest matches. Confirm the exact target.",
                        self.board_total
                    )
                } else {
                    format!(
                        "{} matches / showing {}",
                        self.board_total,
                        self.boards.len()
                    )
                };
                ui.add(
                    egui::Label::new(RichText::new(summary).small().color(palette.muted))
                        .truncate(),
                );
                ui.add(
                    egui::Label::new(if self.board_candidate.is_empty() {
                        "Choose a target from the list"
                    } else {
                        &self.board_candidate
                    })
                    .truncate(),
                )
                .on_hover_text(&self.board_candidate);
                let stacked = ui.available_width() < 270.0;
                action_row(ui, stacked, 40.0, |ui| {
                    ui.add_enabled_ui(!self.board_candidate.is_empty() && self.idle(), |ui| {
                        if button(
                            ui,
                            "Select Board",
                            "board",
                            Tone::Primary,
                            egui::vec2(162.0, 40.0),
                        )
                        .clicked()
                        {
                            confirm = true;
                        }
                    });
                    if button(ui, "Cancel", "", Tone::Secondary, egui::vec2(100.0, 40.0)).clicked()
                    {
                        cancel = true;
                    }
                });
                if ui.input(|i| i.key_pressed(egui::Key::Enter)) {
                    confirm = true;
                }
                if ui.input(|i| i.key_pressed(egui::Key::Escape)) {
                    cancel = true;
                }
            });
        #[cfg(test)]
        {
            self.board_geometry = window.map(|window| window.response.rect);
        }
        #[cfg(not(test))]
        let _ = window;
        self.board_dialog = open && !cancel;
        if confirm && !cancel && open && self.idle() && !self.board_candidate.is_empty() {
            self.choose_board(self.board_candidate.clone());
        }
    }

    fn resources(&mut self, ctx: &egui::Context) {
        let palette = Palette::from_context(ctx);
        let mut open = self.resources_open;
        let window = egui::Window::new(RESOURCES_DIALOG).open(&mut open).collapsible(false).resizable(true).order(egui::Order::Foreground)
            .constrain_to(ctx.content_rect().shrink(8.0))
            .min_width(220.0).min_height(0.0)
            .max_width((ctx.content_rect().width() - 64.0).max(220.0))
            .max_height((ctx.content_rect().height() - 96.0).max(80.0)).vscroll(true)
            .default_width(640.0).default_height(420.0).frame(card(ctx, PAPER)).show(ctx, |ui| {
                ui.horizontal(|ui| {
                    for (boards, label) in [(false, "Libraries"), (true, "Boards")] {
                        if tab(ui, self.resource_tab == boards, label).clicked() { self.resource_tab = boards; }
                    }
                });
                ui.add_space(14.0);
                ui.add_enabled_ui(self.idle(), |ui| {
                    if self.resource_tab {
                        ui.label(RichText::new(if self.board.is_empty() { "Select an exact board first" } else { &self.board }).family(FontFamily::Name("bold".into())).size(21.0));
                        ui.add_space(8.0);
                        ui.label("Download this board's platform, framework, compiler and upload tools into Lite's private store.");
                        ui.label(RichText::new("Uses PlatformIO board packages. An internet connection is required.").small().color(palette.muted));
                        ui.add_space(16.0);
                        let stacked = ui.available_width() < 392.0;
                        action_row(ui, stacked, 40.0, |ui| {
                            if button(ui, "Choose board", "board", Tone::Secondary, egui::vec2(162.0, 40.0)).clicked() { self.open_board_picker(); }
                            ui.add_enabled_ui(!self.board.is_empty(), |ui| {
                                let width = ui.available_width().min(222.0);
                                let label = if width < 222.0 { "Prepare board" } else { "Prepare selected board" };
                                if button(ui, label, "resources", Tone::Primary, egui::vec2(width, 40.0)).on_hover_text("Prepare selected board").clicked() { self.action("board_install"); }
                            });
                        });
                    } else {
                        let stacked = ui.available_width() < 264.0;
                        action_row(ui, stacked, 36.0, |ui| {
                            let width = ui.available_width() - if stacked { 0.0 } else { 112.0 };
                            let enter = ui.add(egui::TextEdit::singleline(&mut self.library_query).hint_text("Search PlatformIO libraries").margin(egui::vec2(6.0, 6.0)).desired_width(width.max(1.0)));
                            if button(ui, "Search", "", Tone::Primary, egui::vec2(104.0, 36.0)).clicked() || (enter.lost_focus() && ui.input(|i| i.key_pressed(egui::Key::Enter))) {
                                self.pending = true; self.send(json!({"cmd": "library_search", "query": self.library_query}));
                            }
                        });
                        ui.add_space(10.0);
                        let mut install = None;
                        egui::ScrollArea::vertical().max_height(320.0).show(ui, |ui| {
                            if self.libraries.is_empty() { ui.label("Search for a library to see its publisher, description and latest version."); }
                            for library in &self.libraries {
                                egui::Frame::new().stroke(Stroke::new(2.0_f32, palette.border)).fill(palette.surface).inner_margin(12).show(ui, |ui| {
                                    ui.set_width(ui.available_width());
                                    ui.label(RichText::new(&library.name).family(FontFamily::Name("bold".into())).size(17.0));
                                    ui.label(RichText::new(format!("{} / {}", library.owner, library.version)).small().color(palette.muted));
                                    ui.label(&library.description);
                                    ui.add_space(6.0);
                                    if button(ui, "Install", "resources", Tone::Primary, egui::vec2(115.0, 36.0)).clicked() { install = Some(library.spec.clone()); }
                                });
                                ui.add_space(8.0);
                            }
                        });
                        if let Some(spec) = install { self.pending = true; self.send(json!({"cmd": "library_install", "spec": spec})); }
                    }
                });
                if self.resource_busy {
                    ui.add_space(14.0);
                    ui.label("Working. Download and install output appears in the Build monitor.");
                    ui.add_enabled_ui(self.stop_enabled, |ui| {
                        let width = ui.available_width().min(250.0);
                        let label = if width < 250.0 { "Stop operation" } else { "Cancel resource operation" };
                        if button(ui, label, "stop", Tone::Danger, egui::vec2(width, 36.0)).on_disabled_hover_text(&self.stop_reason).clicked() { self.send(json!({"cmd": "stop"})); }
                    });
                }
                ui.add_space(10.0); ui.label(RichText::new("Downloads belong to Lite. Clean keeps installed libraries and board tools.").small().color(palette.muted));
            });
        #[cfg(test)]
        {
            self.resource_geometry = window.map(|window| window.response.rect);
        }
        #[cfg(not(test))]
        let _ = window;
        self.resources_open = open;
    }

    fn controls(&mut self, ui: &mut egui::Ui) {
        let palette = Palette::from_context(ui.ctx());
        #[cfg(test)]
        self.control_cards.clear();
        ui.add_enabled_ui(self.idle(), |ui| {
            let sketch_card = card(ui.ctx(), WHITE).show(ui, |ui| {
                ui.set_width(ui.available_width());
                heading(ui, "Sketch");
                ui.add_space(4.0);
                let name = std::path::Path::new(&self.source)
                    .file_name()
                    .map(|n| n.to_string_lossy().into_owned())
                    .unwrap_or_else(|| "No sketch open".into());
                ui.add(egui::Label::new(RichText::new(name).font(bold(17.0))).truncate())
                    .on_hover_text(&self.source);
                ui.label(
                    RichText::new(format!("{} source files", self.files.len()))
                        .size(12.0)
                        .color(palette.muted),
                );
                ui.add_space(8.0);
                if button(
                    ui,
                    "Open sketch",
                    "folder",
                    Tone::Primary,
                    egui::vec2(ui.available_width(), 40.0),
                )
                .clicked()
                {
                    self.project_dialog = true;
                }
                let mut recent = None;
                ui.add_enabled_ui(!self.recent_projects.is_empty(), |ui| {
                    menu_button(
                        ui,
                        "Recent sketches",
                        egui::vec2(ui.available_width(), 36.0),
                        |ui| {
                            ui.set_max_width(360.0);
                            for path in &self.recent_projects {
                                if recent_sketch(ui, path, &self.open_elsewhere) {
                                    recent = Some(path.clone());
                                    ui.close();
                                }
                            }
                        },
                    );
                });
                if let Some(path) = recent {
                    self.open_project(path);
                }
                if !self.files.is_empty() {
                    ui.add_space(6.0);
                    let mut reveal = false;
                    egui::ScrollArea::vertical()
                        .id_salt("sources")
                        .max_height(54.0)
                        .show(ui, |ui| {
                            for file in &self.files {
                                if ui
                                    .add(
                                        egui::Label::new(
                                            RichText::new(file)
                                                .monospace()
                                                .size(12.0)
                                                .underline()
                                                .color(palette.ink),
                                        )
                                        .truncate()
                                        .sense(Sense::click()),
                                    )
                                    .on_hover_cursor(egui::CursorIcon::PointingHand)
                                    .on_hover_text(format!(
                                        "Open original sketch folder:\n{}",
                                        self.source
                                    ))
                                    .clicked()
                                {
                                    reveal = true;
                                }
                            }
                        });
                    if reveal {
                        self.open_sketch_directory(ui.ctx());
                    }
                }
            });
            #[cfg(test)]
            self.control_cards.push(sketch_card.response.rect);
            #[cfg(not(test))]
            let _ = sketch_card;
            ui.add_space(10.0);
            let connection_card = card(ui.ctx(), WHITE).show(ui, |ui| {
                ui.set_width(ui.available_width());
                heading(ui, "Connection");
                ui.add_space(6.0);
                caption(ui, "Board");
                let board_label = if self.board.is_empty() {
                    RichText::new("Choose board").color(palette.muted)
                } else {
                    RichText::new(&self.board)
                };
                if ui
                    .add(
                        egui::Button::new(board_label)
                            .wrap()
                            .min_size(egui::vec2(ui.available_width(), 32.0)),
                    )
                    .on_hover_text("Search the board catalog")
                    .clicked()
                {
                    self.open_board_picker();
                }
                let mut recent = None;
                ui.add_enabled_ui(!self.recent_boards.is_empty(), |ui| {
                    menu_button(
                        ui,
                        "Recent boards",
                        egui::vec2(ui.available_width(), 36.0),
                        |ui| {
                            for name in &self.recent_boards {
                                if ui.add(egui::Button::new(name).wrap()).clicked() {
                                    recent = Some(name.clone());
                                    ui.close();
                                }
                            }
                        },
                    );
                });
                if let Some(name) = recent {
                    self.choose_board(name);
                }
                if !self.frameworks.is_empty() {
                    ui.add_space(6.0);
                    caption(ui, "Framework");
                    let previous = self.framework.clone();
                    egui::ComboBox::from_id_salt("framework")
                        .width(ui.available_width())
                        .selected_text(&self.framework)
                        .show_ui(ui, |ui| {
                            for name in &self.frameworks {
                                ui.selectable_value(&mut self.framework, name.clone(), name);
                            }
                        });
                    if previous != self.framework {
                        self.send(json!({"cmd": "framework", "value": self.framework}));
                    }
                }
                ui.add_space(6.0);
                caption(ui, "Serial port");
                let previous = self.port.clone();
                egui::ComboBox::from_id_salt("port")
                    .width(ui.available_width())
                    .selected_text(if self.port.is_empty() {
                        RichText::new("Select port").color(palette.muted)
                    } else {
                        RichText::new(&self.port)
                    })
                    .show_ui(ui, |ui| {
                        ui.selectable_value(&mut self.port, String::new(), "No port");
                        for port in &self.ports {
                            // A port selected in another MCU Flasher or Lite window stays listed
                            // but cannot be chosen here until that window releases it.
                            let free = !port.occupied || self.port == port.device;
                            let label = if free {
                                port.device.clone()
                            } else {
                                format!("{}  (in use)", port.device)
                            };
                            ui.add_enabled_ui(free, |ui| {
                                ui.selectable_value(&mut self.port, port.device.clone(), label)
                                    .on_hover_text(&port.description)
                                    .on_disabled_hover_text(
                                        "Selected in another MCU Flasher or Lite window",
                                    );
                            });
                        }
                    });
                if previous != self.port {
                    self.pending = true;
                    self.send(json!({"cmd": "port", "value": self.port}));
                }
                ui.add_space(6.0);
                ui.horizontal(|ui| {
                    let width = (ui.available_width() - ui.spacing().item_spacing.x) / 2.0;
                    ui.vertical(|ui| {
                        ui.set_width(width);
                        caption(ui, "Monitor baud");
                        let old = self.baud;
                        egui::ComboBox::from_id_salt("baud")
                            .width(width)
                            .selected_text(self.baud.to_string())
                            .show_ui(ui, |ui| {
                                for rate in [
                                    300, 1200, 2400, 4800, 9600, 19200, 38400, 57600, 74880,
                                    115200, 230400, 250000, 460800, 921600,
                                ] {
                                    ui.selectable_value(&mut self.baud, rate, rate.to_string());
                                }
                            });
                        if self.baud != old {
                            self.send(json!({"cmd": "baud", "value": self.baud}));
                        }
                    });
                    ui.vertical(|ui| {
                        ui.set_width(width);
                        caption(ui, "Upload speed");
                        let old = self.speed;
                        egui::ComboBox::from_id_salt("speed")
                            .width(width)
                            .selected_text(self.speed.to_string())
                            .show_ui(ui, |ui| {
                                for rate in [9600, 19200, 57600, 115200, 230400, 460800, 921600] {
                                    ui.selectable_value(&mut self.speed, rate, rate.to_string());
                                }
                            });
                        if self.speed != old {
                            self.send(json!({"cmd": "upload_speed", "value": self.speed}));
                        }
                    });
                });
                ui.add_space(10.0);
                ui.horizontal(|ui| {
                    ui.add_enabled_ui(!self.port.is_empty(), |ui| {
                        let (label, tone) = if self.connected {
                            ("Disconnect", Tone::Danger)
                        } else {
                            ("Connect", Tone::Primary)
                        };
                        if button(
                            ui,
                            label,
                            "serial",
                            tone,
                            egui::vec2((ui.available_width() - 48.0).max(100.0), 40.0),
                        )
                        .clicked()
                        {
                            self.send(json!({"cmd": "monitor", "connect": !self.connected}));
                        }
                    });
                    if button(ui, "", "refresh", Tone::Secondary, egui::vec2(40.0, 40.0))
                        .on_hover_text("Refresh ports")
                        .clicked()
                    {
                        self.send(json!({"cmd": "ports"}));
                    }
                });
                self.serial_input(ui);
            });
            #[cfg(test)]
            self.control_cards.push(connection_card.response.rect);
            #[cfg(not(test))]
            let _ = connection_card;
        });
    }

    fn controls_panel(&mut self, ui: &mut egui::Ui) {
        // A solid, reserved gutter keeps the scrollbar outside both cards and
        // prevents widths from changing when source rows or fields appear.
        ui.spacing_mut().scroll = scroll_style();
        let output = egui::ScrollArea::vertical()
            .id_salt("controls")
            .scroll_bar_visibility(egui::scroll_area::ScrollBarVisibility::AlwaysVisible)
            .auto_shrink([false, false])
            .show(ui, |ui| {
                ui.set_width((ui.available_width() - 8.0).max(1.0));
                self.controls(ui);
                ui.add_space(6.0);
            });
        #[cfg(test)]
        {
            self.controls_viewport = Some(output.inner_rect);
        }
        #[cfg(not(test))]
        let _ = output;
    }

    fn stop_button(&mut self, ui: &mut egui::Ui, width: f32) {
        ui.add_enabled_ui(self.ready && self.stop_enabled, |ui| {
            let response = button(ui, "Stop", "stop", Tone::Danger, egui::vec2(width, 40.0))
                .on_hover_text(&self.stop_reason)
                .on_disabled_hover_text(&self.stop_reason);
            #[cfg(test)]
            {
                self.stop_geometry = Some((response.rect, ui.is_enabled()));
            }
            if response.clicked() {
                self.send(json!({"cmd": "stop"}));
                self.stop_enabled = false;
                self.stop_reason = "Requesting cancellation".into();
            }
        });
    }

    fn primary_actions(&mut self, ui: &mut egui::Ui) {
        let widths = if ui.available_width() < 650.0 {
            [112.0, 108.0, 90.0, 132.0, 96.0]
        } else {
            [136.0, 136.0, 104.0, 146.0, 104.0]
        };
        let available = ui.available_width();
        let mut start = 0;
        // Pack whole enabled scopes before drawing them. egui's wrapped
        // horizontal layout cannot retroactively move custom-painted children
        // when a scope turns out wider than the remaining row.
        while start < widths.len() {
            let mut end = start + 1;
            let mut used = widths[start];
            while end < widths.len()
                && used + ui.spacing().item_spacing.x + widths[end] <= available
            {
                used += ui.spacing().item_spacing.x + widths[end];
                end += 1;
            }
            ui.horizontal(|ui| {
                for (index, width) in widths.iter().enumerate().take(end).skip(start) {
                    if index == 4 {
                        self.stop_button(ui, *width);
                        continue;
                    }
                    let (label, icon, tone, problem) = match index {
                        0 => (
                            "Compile",
                            "compile",
                            Tone::Primary,
                            self.action_problem(false),
                        ),
                        1 => ("Upload", "upload", Tone::Primary, self.action_problem(true)),
                        2 => (
                            "Clean",
                            "clean",
                            Tone::Secondary,
                            if !self.idle() {
                                Some("Wait for the current operation")
                            } else if self.source.is_empty() {
                                Some("Open a sketch folder first")
                            } else {
                                None
                            },
                        ),
                        _ => (
                            "Resources",
                            "resources",
                            Tone::Secondary,
                            if self.idle() {
                                None
                            } else {
                                Some("Wait for the current operation")
                            },
                        ),
                    };
                    ui.add_enabled_ui(problem.is_none(), |ui| {
                        if button(ui, label, icon, tone, egui::vec2(*width, 40.0))
                            .on_disabled_hover_text(problem.unwrap_or(""))
                            .clicked()
                        {
                            match index {
                                0 => self.action("compile"),
                                1 => self.action("upload"),
                                2 => self.action("clean"),
                                _ => self.resources_open = true,
                            }
                        }
                    });
                }
            });
            start = end;
        }
    }

    fn monitor_panel(&mut self, ui: &mut egui::Ui) {
        let height = ui.available_height();
        if height >= 170.0 {
            self.monitor(ui);
        } else {
            // Keep complete reading/control heights on very short desktops;
            // the layout itself scrolls instead of pushing controls offscreen.
            ui.spacing_mut().scroll = scroll_style();
            egui::ScrollArea::vertical()
                .id_salt("short-monitor-layout")
                .stick_to_bottom(true)
                .auto_shrink([false, false])
                .show(ui, |ui| {
                    ui.allocate_ui_with_layout(
                        egui::vec2(ui.available_width(), 170.0),
                        egui::Layout::top_down(Align::Min),
                        |ui| self.monitor(ui),
                    );
                });
        }
    }

    fn workspace(&mut self, ui: &mut egui::Ui) {
        if ui.available_width() < 650.0 {
            ui.horizontal(|ui| {
                if tab(ui, !self.compact_controls, "Monitor").clicked() {
                    self.compact_controls = false;
                }
                if tab(ui, self.compact_controls, "Sketch & Connection").clicked() {
                    self.compact_controls = true;
                }
            });
            ui.add_space(4.0);
            if self.compact_controls {
                self.controls_panel(ui);
            } else {
                self.monitor_panel(ui);
            }
        } else {
            let available = ui.available_size();
            let left_width = if available.x < 850.0 { 248.0 } else { 276.0 };
            ui.horizontal_top(|ui| {
                ui.allocate_ui_with_layout(
                    egui::vec2(left_width, available.y),
                    egui::Layout::top_down(Align::Min),
                    |ui| self.controls_panel(ui),
                );
                ui.add_space(4.0);
                ui.allocate_ui_with_layout(
                    egui::vec2(ui.available_width(), available.y),
                    egui::Layout::top_down(Align::Min),
                    |ui| self.monitor_panel(ui),
                );
            });
        }
    }

    fn fit_viewport(&mut self, ctx: &egui::Context) {
        if self.offline_preview {
            return;
        }
        let screen = ctx.input(|input| {
            input.viewport().monitor_size.map(|size| {
                (
                    size,
                    input.viewport().native_pixels_per_point.unwrap_or(1.0),
                )
            })
        });
        if screen != self.last_monitor {
            self.last_monitor = screen;
            if let Some((monitor, _)) = screen {
                // egui already supplies logical points. Leave native borders,
                // title and desktop panels room without applying DPI twice.
                let maximum = (monitor - egui::vec2(32.0, 96.0)).max(egui::vec2(320.0, 200.0));
                let current = ctx.content_rect().size();
                let fitted = current.min(maximum);
                ctx.send_viewport_cmd(egui::ViewportCommand::MinInnerSize(
                    egui::vec2(360.0, 240.0).min(maximum),
                ));
                if fitted != current {
                    ctx.send_viewport_cmd(egui::ViewportCommand::InnerSize(fitted));
                }
            }
        }
    }

    fn serial_input(&mut self, ui: &mut egui::Ui) {
        ui.add_space(8.0);
        caption(ui, "Send to device");
        ui.add_enabled_ui(self.connected, |ui| {
            let response = ui.add(
                egui::TextEdit::singleline(&mut self.send_text)
                    .hint_text("Serial message")
                    .margin(egui::vec2(6.0, 6.0))
                    .desired_width(ui.available_width()),
            );
            let enter =
                response.lost_focus() && ui.input(|input| input.key_pressed(egui::Key::Enter));
            ui.add_space(2.0);
            ui.horizontal(|ui| {
                let send_width = 76.0;
                egui::ComboBox::from_id_salt("line-ending")
                    .width(
                        (ui.available_width() - send_width - ui.spacing().item_spacing.x).max(90.0),
                    )
                    .selected_text(match self.line_ending.as_str() {
                        "both" => "CR + LF",
                        "nl" => "LF",
                        "cr" => "CR",
                        _ => "No ending",
                    })
                    .show_ui(ui, |ui| {
                        for (value, label) in [
                            ("both", "CR + LF"),
                            ("nl", "LF"),
                            ("cr", "CR"),
                            ("none", "No ending"),
                        ] {
                            ui.selectable_value(&mut self.line_ending, value.into(), label);
                        }
                    });
                if button(
                    ui,
                    "Send",
                    "",
                    Tone::Secondary,
                    egui::vec2(send_width, 36.0),
                )
                .clicked()
                    || enter
                {
                    self.send(
                        json!({"cmd": "send", "text": self.send_text, "ending": self.line_ending}),
                    );
                    self.send_text.clear();
                    self.serial_tab = true;
                    response.request_focus();
                }
            });
        });
    }

    fn monitor_tools(&mut self, ui: &mut egui::Ui) {
        if button(ui, "Clear", "", Tone::Secondary, egui::vec2(70.0, 34.0)).clicked() {
            if self.serial_tab {
                self.serial_log = Log::default();
            } else {
                self.build_log = Log::default();
            }
        }
        if button(ui, "Copy", "", Tone::Secondary, egui::vec2(70.0, 34.0)).clicked() {
            ui.ctx().copy_text(if self.serial_tab {
                self.serial_log.text()
            } else {
                self.build_log.text()
            });
        }
        ui.checkbox(&mut self.autoscroll, "Follow");
    }

    fn monitor(&mut self, ui: &mut egui::Ui) {
        let palette = Palette::from_context(ui.ctx());
        #[cfg(test)]
        {
            self.monitor_rows.clear();
            self.monitor_text_origins.clear();
        }
        let narrow = ui.available_width() < 570.0;
        let short = ui.available_height() < 240.0;
        ui.horizontal(|ui| {
            let labels = if short {
                [(false, "Build"), (true, "Serial")]
            } else {
                [(false, "Build monitor"), (true, "Serial monitor")]
            };
            for (serial, label) in labels {
                if tab(ui, self.serial_tab == serial, label).clicked() {
                    self.serial_tab = serial;
                }
            }
            if short {
                menu_button(ui, "Options", egui::vec2(88.0, 32.0), |ui| {
                    ui.horizontal(|ui| self.monitor_tools(ui));
                });
            } else if !narrow {
                ui.with_layout(egui::Layout::right_to_left(Align::Center), |ui| {
                    self.monitor_tools(ui)
                });
            }
        });
        ui.add_space(4.0);
        let port = if self.port.is_empty() {
            "No port selected".to_owned()
        } else if self.connected {
            format!("{} / {} baud", self.port, self.baud)
        } else {
            format!("{} / monitor disconnected", self.port)
        };
        let status_width = (ui.available_width() - ui.spacing().item_spacing.x) * 0.5;
        ui.horizontal(|ui| {
            status_box(ui, "STATUS", &self.phase, status_width);
            status_box(ui, "USB PORT", &port, status_width);
        });
        if narrow && !short {
            ui.horizontal(|ui| {
                ui.with_layout(egui::Layout::right_to_left(Align::Center), |ui| {
                    self.monitor_tools(ui)
                });
            });
        }
        ui.add_space(4.0);
        // min_scrolled_height describes the reading area, excluding the
        // reserved horizontal scrollbar as well as the frame's padding/rim.
        let height =
            (ui.available_height() - 24.0 - ui.spacing().scroll.allocated_width()).max(32.0);
        let monitor_frame = egui::Frame::new()
            .fill(palette.terminal)
            .stroke(Stroke::new(2.0_f32, palette.border))
            .inner_margin(10)
            .show(ui, |ui| {
                ui.set_min_width(ui.available_width());
                // Quiet scrollbars: a muted handle on a tinted track, ink on hover.
                let visuals = ui.visuals_mut();
                visuals.extreme_bg_color = palette.track;
                visuals.widgets.inactive.fg_stroke.color = palette.handle;
                visuals.widgets.hovered.fg_stroke.color = palette.ink;
                visuals.widgets.active.fg_stroke.color = palette.ink;
                ui.spacing_mut().item_spacing.y = 0.0;
                ui.spacing_mut().scroll.floating = false;
                let row_height = ui.text_style_height(&egui::TextStyle::Monospace).ceil() + 2.0;
                let cell_width = ui
                    .painter()
                    .layout_no_wrap("M".into(), FontId::monospace(13.0), INK)
                    .size()
                    .x;
                let log = if self.serial_tab {
                    &self.serial_log
                } else {
                    &self.build_log
                };
                let content_width = (log.max_columns.max(1) as f32 * cell_width + 8.0)
                    .max(ui.available_width() - 18.0);
                let empty = log.lines.is_empty() && log.partial.is_empty();
                let rows = if empty {
                    2
                } else {
                    log.lines.len() + usize::from(!log.partial.is_empty())
                };
                let output = egui::ScrollArea::both()
                    .max_width(ui.available_width())
                    .scroll_bar_visibility(egui::scroll_area::ScrollBarVisibility::AlwaysVisible)
                    .animated(false)
                    .id_salt(if self.serial_tab {
                        "serial-output"
                    } else {
                        "build-output"
                    })
                    .stick_to_bottom(self.autoscroll)
                    .auto_shrink([false, false])
                    .max_height(height)
                    .min_scrolled_height(height)
                    .show_rows(ui, row_height, rows, |ui, range| {
                        ui.set_min_width(content_width);
                        for row in range {
                            let (text, color) = if empty {
                                (
                                    if row == 0 {
                                        if self.serial_tab {
                                            "Serial output will appear here."
                                        } else {
                                            "Build output will appear here."
                                        }
                                    } else if self.serial_tab {
                                        "Choose a port and press Connect."
                                    } else {
                                        ""
                                    },
                                    palette.muted,
                                )
                            } else if let Some(line) = log.lines.get(row) {
                                (line.text.as_str(), palette.log_color(line.tone))
                            } else {
                                (log.partial.as_str(), palette.ink)
                            };
                            let row = ui.allocate_ui_with_layout(
                                egui::vec2(content_width, row_height),
                                egui::Layout::left_to_right(Align::Center),
                                |ui| {
                                    ui.add(
                                        egui::Label::new(
                                            RichText::new(text).monospace().size(13.0).color(color),
                                        )
                                        .selectable(true)
                                        .extend(),
                                    )
                                },
                            );
                            #[cfg(test)]
                            {
                                self.monitor_rows.push(row.response.rect);
                                self.monitor_text_origins.push(row.inner.rect.left());
                            }
                            #[cfg(not(test))]
                            let _ = row;
                        }
                    });
                #[cfg(test)]
                {
                    self.monitor_geometry = Some((
                        output.inner_rect.intersect(ui.clip_rect()),
                        output.content_size,
                    ));
                    self.monitor_offset = output.state.offset;
                }
                #[cfg(not(test))]
                let _ = output;
            });
        #[cfg(test)]
        {
            self.monitor_frame = Some(monitor_frame.response.rect);
        }
        #[cfg(not(test))]
        let _ = monitor_frame;
    }
}

impl LiteApp {
    fn update_ui(&mut self, ctx: &egui::Context) {
        self.fit_viewport(ctx);
        self.frames += 1;
        if self.offline_preview && self.frames == 8 && self.capture.is_some() {
            ctx.send_viewport_cmd(egui::ViewportCommand::Screenshot(egui::UserData::default()));
        }
        for event in ctx.input(|input| input.events.clone()) {
            if let egui::Event::Screenshot { image, .. } = event
                && let Some(path) = self.capture.take()
            {
                let pixels: Vec<u8> = image
                    .pixels
                    .iter()
                    .flat_map(|color| color.to_array())
                    .collect();
                if let Err(error) = image::save_buffer(
                    &path,
                    &pixels,
                    image.width() as u32,
                    image.height() as u32,
                    image::ColorType::Rgba8,
                ) {
                    eprintln!("Capture: {error}");
                }
                ctx.send_viewport_cmd(egui::ViewportCommand::Close);
            }
        }
        let events: Vec<_> = self
            .bridge
            .as_ref()
            .map(|b| b.events.try_iter().take(256).collect())
            .unwrap_or_default();
        for event in events {
            self.event(event);
        }
        while let Ok(path) = self.folder_rx.try_recv() {
            self.picker_busy = false;
            if let Some(path) = path {
                self.project_input = path;
            }
        }
        while let Ok(error) = self.desktop_rx.try_recv() {
            self.error = error;
        }
        if self.ready && self.last_poll.elapsed() > Duration::from_secs(2) {
            self.send(json!({"cmd": "status"}));
            self.send(json!({"cmd": "ports"}));
            self.last_poll = Instant::now();
        }
        if self.ready
            && !self.offline_preview
            && self.board_dialog
            && self.board_query != self.board_sent
            && self.board_query_at.elapsed() >= Duration::from_millis(200)
        {
            self.send(json!({"cmd": "boards", "query": self.board_query}));
            self.board_sent = self.board_query.clone();
            self.board_loading = true;
        }
        ctx.request_repaint_after(Duration::from_millis(if self.ready { 250 } else { 1000 }));
        if ctx.input(|i| i.viewport().close_requested())
            && (self.busy || self.pending || self.resource_busy || self.developer_open)
        {
            ctx.send_viewport_cmd(egui::ViewportCommand::CancelClose);
            self.error = if self.developer_open {
                "Close Developer Access before closing Lite."
            } else {
                "Wait for the current operation to finish before closing Lite."
            }
            .into();
        }
        if !self.ready
            && !self.offline_preview
            && self.started.elapsed() > Duration::from_secs(30)
            && self.error.is_empty()
        {
            self.error =
                "Python tools are taking longer to start. Check the Build monitor for details."
                    .into();
        }
        if let Some(path) = ctx.input(|i| {
            i.raw
                .dropped_files
                .first()
                .and_then(|file| file.path.clone())
        }) && self.idle()
        {
            self.open_project(path.to_string_lossy().into_owned());
        }
        if self.take_developer_request(Instant::now()) {
            self.send(json!({"cmd": "developer_access"}));
        }
        let palette = Palette::from_context(ctx);
        egui::TopBottomPanel::top("header")
            .frame(
                egui::Frame::new()
                    .fill(palette.paper)
                    .inner_margin(egui::Margin::symmetric(12, 8)),
            )
            .show(ctx, |ui| {
                let compact = ui.available_width() < 520.0;
                ui.horizontal(|ui| {
                    logo_button(ui, &mut self.logo_gesture, Instant::now());
                    ui.vertical(|ui| {
                        ui.label(
                            RichText::new(if compact {
                                "MCU / Lite"
                            } else {
                                "MCU Flasher / Lite"
                            })
                            .size(if compact { 20.0 } else { 22.0 })
                            .family(FontFamily::Name("bold".into())),
                        );
                        ui.label(
                            RichText::new(if self.offline_preview {
                                "Visual preview"
                            } else {
                                "by Naph"
                            })
                            .size(11.0)
                            .color(palette.muted),
                        );
                    });
                    ui.with_layout(egui::Layout::right_to_left(Align::Center), |ui| {
                        if button(
                            ui,
                            if compact && palette.dark {
                                "Dark"
                            } else if compact {
                                "Light"
                            } else if palette.dark {
                                "Dark mode"
                            } else {
                                "Light mode"
                            },
                            if palette.dark { "moon" } else { "sun" },
                            Tone::Secondary,
                            egui::vec2(if compact { 108.0 } else { 144.0 }, 38.0),
                        )
                        .on_hover_text(if palette.dark {
                            "Switch to Light mode"
                        } else {
                            "Switch to Dark mode"
                        })
                        .clicked()
                        {
                            apply_theme(ctx, !palette.dark);
                            ctx.request_repaint();
                        }
                    });
                });
            });
        egui::CentralPanel::default()
            .frame(
                egui::Frame::new()
                    .fill(palette.paper)
                    .inner_margin(egui::Margin::symmetric(12, 6)),
            )
            .show(ctx, |ui| {
                self.primary_actions(ui);
                ui.add_space(8.0);
                if !self.error.is_empty() {
                    egui::Frame::new()
                        .fill(CORAL)
                        .stroke(Stroke::new(2.0_f32, INK))
                        .shadow(hard_shadow(palette, 3))
                        .inner_margin(6)
                        .show(ui, |ui| {
                            ui.horizontal(|ui| {
                                let width = (ui.available_width() - 100.0).max(1.0);
                                ui.add_sized(
                                    egui::vec2(width, 32.0),
                                    egui::Label::new(RichText::new(&self.error).color(INK))
                                        .truncate(),
                                )
                                .on_hover_text(&self.error);
                                if button(
                                    ui,
                                    "Dismiss",
                                    "",
                                    Tone::Secondary,
                                    egui::vec2(92.0, 32.0),
                                )
                                .clicked()
                                {
                                    self.error.clear();
                                }
                            });
                        });
                    ui.add_space(6.0);
                }
                self.workspace(ui);
            });
        self.modal_dialogs(ctx);
        if self.project_dialog {
            self.project_picker(ctx);
        }
        if self.resources_open {
            self.resources(ctx);
        }
        if self.board_dialog {
            self.board_picker(ctx);
        }
    }
}

impl eframe::App for LiteApp {
    fn update(&mut self, ctx: &egui::Context, _frame: &mut eframe::Frame) {
        self.update_ui(ctx);
    }
}

fn configure(ctx: &egui::Context) {
    egui_extras::install_image_loaders(ctx);
    let mut fonts = egui::FontDefinitions::default();
    fonts.font_data.insert(
        "lite-body".into(),
        egui::FontData::from_static(include_bytes!("../assets/fonts/Montserrat-Regular.ttf"))
            .into(),
    );
    fonts.font_data.insert(
        "lite-bold".into(),
        egui::FontData::from_static(include_bytes!("../assets/fonts/Montserrat-Bold.ttf")).into(),
    );
    fonts
        .families
        .entry(FontFamily::Proportional)
        .or_default()
        .insert(0, "lite-body".into());
    fonts
        .families
        .insert(FontFamily::Name("bold".into()), vec!["lite-bold".into()]);
    ctx.set_fonts(fonts);
    apply_theme(ctx, false);
}

fn apply_theme(ctx: &egui::Context, dark: bool) {
    let palette = Palette::new(dark);
    let mut style = egui::Style::default();
    let mut visuals = if dark {
        egui::Visuals::dark()
    } else {
        egui::Visuals::light()
    };
    visuals.override_text_color = Some(palette.ink);
    visuals.panel_fill = palette.paper;
    visuals.window_fill = palette.paper;
    visuals.extreme_bg_color = palette.surface;
    visuals.text_edit_bg_color = Some(palette.surface);
    visuals.faint_bg_color = palette.track;
    visuals.window_stroke = Stroke::new(2.0_f32, palette.border);
    visuals.window_corner_radius = egui::CornerRadius::ZERO;
    visuals.menu_corner_radius = egui::CornerRadius::ZERO;
    visuals.window_shadow = hard_shadow(palette, 4);
    visuals.popup_shadow = hard_shadow(palette, 3);
    visuals.hyperlink_color = palette.ink;
    // Fields, menus and selections share one surface, one outline and one
    // highlight; hover and selection change color only, never the box.
    visuals.selection.bg_fill = if dark { palette.highlight } else { YELLOW };
    visuals.selection.stroke = Stroke::new(1.5_f32, palette.ink);
    for widgets in [
        &mut visuals.widgets.noninteractive,
        &mut visuals.widgets.inactive,
        &mut visuals.widgets.hovered,
        &mut visuals.widgets.active,
        &mut visuals.widgets.open,
    ] {
        widgets.corner_radius = egui::CornerRadius::ZERO;
        widgets.bg_stroke = Stroke::new(2.0_f32, palette.border);
        widgets.fg_stroke = Stroke::new(1.5_f32, palette.ink);
        widgets.bg_fill = palette.surface;
        widgets.weak_bg_fill = palette.surface;
        widgets.expansion = 0.0;
    }
    visuals.widgets.noninteractive.bg_stroke = Stroke::new(1.0_f32, palette.border);
    visuals.widgets.noninteractive.bg_fill = palette.paper;
    for widgets in [&mut visuals.widgets.hovered, &mut visuals.widgets.open] {
        widgets.bg_fill = palette.highlight;
        widgets.weak_bg_fill = palette.highlight;
    }
    visuals.widgets.active.bg_fill = visuals.selection.bg_fill;
    visuals.widgets.active.weak_bg_fill = visuals.selection.bg_fill;
    style.visuals = visuals;
    style.animation_time = 0.0;
    style.spacing.item_spacing = egui::vec2(8.0, 6.0);
    style.spacing.button_padding = egui::vec2(10.0, 7.0);
    style.spacing.interact_size = egui::vec2(36.0, 32.0);
    style.spacing.combo_height = 320.0;
    style.text_styles.insert(
        egui::TextStyle::Body,
        FontId::new(14.0, FontFamily::Proportional),
    );
    style
        .text_styles
        .insert(egui::TextStyle::Monospace, FontId::monospace(13.0));
    style.spacing.scroll = scroll_style();
    style
        .text_styles
        .insert(egui::TextStyle::Heading, bold(17.0));
    style.text_styles.insert(
        egui::TextStyle::Button,
        FontId::new(14.0, FontFamily::Proportional),
    );
    ctx.set_style(style);
}

#[cfg(test)]
mod verification {
    use super::*;

    #[test]
    fn monitor_changes_fit_logical_size_once_without_pinning_normal_resizes() {
        let ctx = egui::Context::default();
        let mut app = LiteApp::with_context(&ctx, true);
        app.offline_preview = false; // No bridge was constructed for this fixture.
        for (monitor, scale, expected) in [
            (egui::vec2(1280.0, 720.0), 1.5, egui::vec2(1140.0, 624.0)),
            (egui::vec2(640.0, 360.0), 2.0, egui::vec2(608.0, 264.0)),
        ] {
            let input = || {
                let mut raw = egui::RawInput {
                    screen_rect: Some(egui::Rect::from_min_size(
                        egui::Pos2::ZERO,
                        egui::vec2(1140.0, 800.0),
                    )),
                    ..Default::default()
                };
                let viewport = raw.viewports.get_mut(&egui::ViewportId::ROOT).unwrap();
                viewport.monitor_size = Some(monitor);
                viewport.native_pixels_per_point = Some(scale);
                raw
            };
            let output = ctx.run(input(), |ctx| app.fit_viewport(ctx));
            let commands = &output.viewport_output[&egui::ViewportId::ROOT].commands;
            assert!(
                commands.contains(&egui::ViewportCommand::InnerSize(expected)),
                "Fit applies logical monitor dimensions only once: {commands:?}"
            );
            let output = ctx.run(input(), |ctx| app.fit_viewport(ctx));
            assert!(
                !output.viewport_output[&egui::ViewportId::ROOT]
                    .commands
                    .iter()
                    .any(|command| matches!(command, egui::ViewportCommand::InnerSize(_))),
                "Ordinary frames must not pin user resizing"
            );
        }
    }

    #[test]
    fn protected_phase_disables_stale_stop_before_the_next_policy_batch() {
        let ctx = egui::Context::default();
        let mut app = LiteApp::with_context(&ctx, true);
        app.event(Event {
            event: "stop:state".into(),
            data: json!({"enabled": true, "reason": "Compilation"}),
        });
        assert!(app.stop_enabled);
        app.event(Event {
            event: "operation:phase".into(),
            data: json!({"is_busy": true, "phase": "flashing"}),
        });
        assert!(!app.stop_enabled);
        app.event(Event {
            event: "operation:phase".into(),
            data: json!({"is_busy": true, "phase": "compiling"}),
        });
        assert!(
            !app.stop_enabled,
            "A display phase alone cannot grant cancellation"
        );
        app.event(Event {
            event: "stop:state".into(),
            data: json!({"enabled": true, "reason": "Compilation"}),
        });
        assert!(app.stop_enabled);
        app.event(Event {
            event: "fatal".into(),
            data: json!({"message": "Fixture worker ended"}),
        });
        assert!(!app.stop_enabled);
    }

    #[test]
    fn monitor_frame_includes_both_scrollbar_gutters_inside_the_pane() {
        for size in [egui::vec2(340.0, 240.0), egui::vec2(778.0, 500.0)] {
            let ctx = egui::Context::default();
            let mut app = LiteApp::with_context(&ctx, true);
            let screen = egui::Rect::from_min_size(egui::Pos2::ZERO, size);
            for _ in 0..4 {
                let _ = ctx.run(
                    egui::RawInput {
                        screen_rect: Some(screen),
                        ..Default::default()
                    },
                    |ctx| {
                        egui::CentralPanel::default().show(ctx, |ui| app.monitor(ui));
                    },
                );
            }
            assert!(
                screen.shrink(7.5).contains_rect(app.monitor_frame.unwrap()),
                "Monitor's rim or scrollbar leaves its pane at {size:?}: {:?}",
                app.monitor_frame
            );
        }
    }

    #[test]
    fn control_scrollbars_and_shadows_never_cover_cards_or_long_sources() {
        for scale in [1.0, 1.25, 1.5, 1.75, 2.0] {
            for size in [
                egui::vec2(248.0, 240.0),
                egui::vec2(276.0, 800.0),
                egui::vec2(520.0, 300.0),
            ] {
                let ctx = egui::Context::default();
                let mut app = LiteApp::with_context(&ctx, true);
                ctx.set_pixels_per_point(scale);
                app.source = format!("/fixture/{}", "LongSketchName".repeat(15));
                app.files = (0..12)
                    .map(|n| format!("{n}_{}.hpp", "long_source_name".repeat(12)))
                    .collect();
                app.frameworks = vec!["arduino".into()];
                app.framework = "arduino".into();
                for _ in 0..5 {
                    let _ = ctx.run(
                        egui::RawInput {
                            screen_rect: Some(egui::Rect::from_min_size(egui::Pos2::ZERO, size)),
                            ..Default::default()
                        },
                        |ctx| {
                            egui::CentralPanel::default().show(ctx, |ui| app.controls_panel(ui));
                        },
                    );
                }
                let viewport = app.controls_viewport.unwrap();
                assert_eq!(app.control_cards.len(), 2);
                for card in &app.control_cards {
                    assert!(
                        card.left() >= viewport.left()
                            && card.right() + 4.0 <= viewport.right() + 0.5,
                        "Card/shadow overlaps scrollbar at {size:?}/{scale}: {card:?} in {viewport:?}"
                    );
                }
            }
        }
    }

    #[test]
    fn scrolling_dialogs_keep_action_rows_at_their_content_height() {
        // A centered row given the rest of a scrolling window centered its buttons
        // in that whole height, opening hundreds of pixels of empty space.
        for scale in [1.0, 1.5, 2.0] {
            for kind in 0..3 {
                let ctx = egui::Context::default();
                let mut app = LiteApp::with_context(&ctx, true);
                ctx.set_pixels_per_point(scale);
                let screen = egui::Rect::from_min_size(egui::Pos2::ZERO, egui::vec2(1140.0, 800.0));
                app.project_dialog = true;
                app.resources_open = true;
                app.resource_tab = kind == 2;
                let mut heights = Vec::new();
                for _ in 0..8 {
                    let _ = ctx.run(
                        egui::RawInput {
                            screen_rect: Some(screen),
                            ..Default::default()
                        },
                        |ctx| match kind {
                            0 => app.project_picker(ctx),
                            _ => app.resources(ctx),
                        },
                    );
                    let window = if kind == 0 {
                        app.project_geometry
                    } else {
                        app.resource_geometry
                    };
                    heights.push(window.unwrap().height());
                }
                let last = *heights.last().unwrap();
                // Open sketch fits its content; Resources keeps its 420-point default.
                let limit = if kind == 0 { 360.0 } else { 520.0 };
                assert!(
                    last < limit,
                    "Dialog {kind} at {scale} opens empty space: {heights:?}"
                );
                // ...and still shows its whole content: path, actions and recents.
                assert!(
                    last > 200.0,
                    "Dialog {kind} at {scale} hides its content: {heights:?}"
                );
                assert!(
                    (heights[3] - last).abs() < 1.0,
                    "Dialog {kind} at {scale} keeps growing: {heights:?}"
                );
            }
        }
    }

    #[test]
    fn dialogs_are_modal_and_the_board_picker_stays_above_resources() {
        let ctx = egui::Context::default();
        let mut app = LiteApp::with_context(&ctx, true);
        let screen = egui::Rect::from_min_size(egui::Pos2::ZERO, egui::vec2(1140.0, 800.0));
        let frame = |app: &mut LiteApp, events: Vec<egui::Event>| {
            let _ = ctx.run(
                egui::RawInput {
                    screen_rect: Some(screen),
                    events,
                    ..Default::default()
                },
                |ctx| app.update_ui(ctx),
            );
        };
        let layer = |title: &str| egui::LayerId::new(egui::Order::Foreground, egui::Id::new(title));
        frame(&mut app, Vec::new());
        assert!(ctx.memory(|m| m.top_modal_layer()).is_none());
        app.project_dialog = true;
        for _ in 0..3 {
            frame(&mut app, Vec::new());
        }
        assert_eq!(
            ctx.memory(|m| m.top_modal_layer()),
            Some(layer(PROJECT_DIALOG))
        );
        assert!(
            !ctx.memory(|m| m.allows_interaction(egui::LayerId::background())),
            "The workspace behind Open sketch must not take input"
        );
        let escape = egui::Event::Key {
            key: egui::Key::Escape,
            physical_key: None,
            pressed: true,
            repeat: false,
            modifiers: Default::default(),
        };
        frame(&mut app, vec![escape]);
        assert!(!app.project_dialog, "Escape closes the top dialog");
        app.resources_open = true;
        app.board_dialog = true;
        for _ in 0..3 {
            frame(&mut app, Vec::new());
        }
        assert_eq!(
            ctx.memory(|m| m.top_modal_layer()),
            Some(layer(BOARD_DIALOG))
        );
        assert!(!ctx.memory(|m| m.allows_interaction(layer(RESOURCES_DIALOG))));
        app.board_dialog = false;
        app.resources_open = false;
        for _ in 0..3 {
            frame(&mut app, Vec::new());
        }
        assert!(ctx.memory(|m| m.allows_interaction(egui::LayerId::background())));
    }

    #[test]
    fn another_sketch_opens_in_its_own_window() {
        assert!(same_folder("/home/me/Blink/", "/home/me/Blink"));
        assert!(!same_folder("/home/me/Blink", "/home/me/Fade"));
        let ctx = egui::Context::default();
        let mut app = LiteApp::with_context(&ctx, true);
        app.offline_preview = false; // No bridge: requests are numbered, then refused.
        app.open_project("/home/me/Blink".into());
        assert_eq!(app.next_id, 1, "The first sketch opens in this window");
        app.source = "/home/me/Blink".into();
        app.open_project("/home/me/Blink/".into());
        assert_eq!(
            app.next_id, 2,
            "Reopening this window's sketch refreshes it here"
        );
        // In the test harness current_exe is this test binary: never launch it.
        app.offline_preview = true;
        app.open_project("/home/me/Fade".into());
        assert_eq!(
            app.next_id, 2,
            "Another sketch never replaces this window's sketch"
        );
        assert_eq!(app.source, "/home/me/Blink");
        app.offline_preview = false;
        app.open_elsewhere = vec!["/home/me/Fade".into()];
        app.open_project("/home/me/Fade/".into());
        assert_eq!(
            app.next_id, 2,
            "A sketch open in another window is not requested"
        );
        assert!(app.error.contains("already open in another Lite window"));
    }

    #[test]
    fn pickers_and_resources_fit_short_portrait_and_scaled_desktops() {
        for scale in [1.0, 1.25, 1.5, 1.75, 2.0] {
            for size in [
                egui::vec2(320.0, 240.0),
                egui::vec2(360.0, 240.0),
                egui::vec2(496.0, 252.0),
                egui::vec2(700.0, 560.0),
            ] {
                for kind in 0..4 {
                    let ctx = egui::Context::default();
                    let mut app = LiteApp::with_context(&ctx, true);
                    ctx.set_pixels_per_point(scale);
                    let screen = egui::Rect::from_min_size(egui::Pos2::ZERO, size);
                    fixture_boards(&mut app);
                    app.board_dialog = true;
                    app.project_dialog = true;
                    app.resources_open = true;
                    app.resource_tab = kind == 3;
                    app.board = "A very long board name to verify wrapping and the preparation footer at high display scales".into();
                    for _ in 0..5 {
                        let _ = ctx.run(
                            egui::RawInput {
                                screen_rect: Some(screen),
                                ..Default::default()
                            },
                            |ctx| match kind {
                                0 => app.board_picker(ctx),
                                1 => app.project_picker(ctx),
                                _ => app.resources(ctx),
                            },
                        );
                    }
                    let window = match kind {
                        0 => app.board_geometry,
                        1 => app.project_geometry,
                        _ => app.resource_geometry,
                    }
                    .unwrap();
                    assert!(
                        screen.expand(1.0).contains_rect(window),
                        "Dialog {kind} exceeds {size:?}/{scale}: {window:?}"
                    );
                }
            }
        }
    }

    #[test]
    fn workspace_keeps_stop_visible_and_monitor_reachable_on_scaled_screens() {
        for scale in [1.0, 1.25, 1.5, 1.75, 2.0] {
            for physical in [
                egui::vec2(1024.0, 600.0),
                egui::vec2(1366.0, 768.0),
                egui::vec2(1920.0, 1080.0),
                egui::vec2(1080.0, 1920.0),
                egui::vec2(3840.0, 2160.0),
            ] {
                let ctx = egui::Context::default();
                let mut app = LiteApp::with_context(&ctx, true);
                let size = (physical - egui::vec2(32.0, 96.0)) / scale;
                ctx.set_pixels_per_point(scale);
                let screen = egui::Rect::from_min_size(egui::Pos2::ZERO, size);
                for busy in [false, true, false] {
                    app.busy = busy;
                    app.stop_enabled = busy;
                    for _ in 0..3 {
                        let _ = ctx.run(
                            egui::RawInput {
                                screen_rect: Some(screen),
                                ..Default::default()
                            },
                            |ctx| app.update_ui(ctx),
                        );
                    }
                    let (stop, enabled) = app.stop_geometry.expect("Stop stays present while idle");
                    assert!(
                        screen.contains_rect(stop),
                        "Stop leaves {size:?} at {scale}: {stop:?}"
                    );
                    assert_eq!(enabled, busy);
                    let (monitor, _) = app.monitor_geometry.expect("Monitor remains available");
                    assert!(
                        screen.expand(1.0).contains_rect(monitor),
                        "Monitor leaves {size:?} at {scale}: {monitor:?}"
                    );
                    assert!(
                        monitor.width() >= 200.0 && monitor.height() >= 30.0,
                        "No useful reading area at {size:?}: {monitor:?}"
                    );
                }
            }
        }
    }

    #[test]
    fn startup_does_not_discard_the_developer_gesture() {
        let ctx = egui::Context::default();
        let mut app = LiteApp::with_context(&ctx, true);
        app.offline_preview = false;
        app.ready = false;
        let start = Instant::now();
        for n in 0..5 {
            app.logo_gesture
                .click(start + Duration::from_millis(n * 100));
        }
        assert!(!app.take_developer_request(start + Duration::from_secs(2)));
        app.ready = true;
        assert!(app.take_developer_request(start + Duration::from_secs(3)));
        assert!(!app.take_developer_request(start + Duration::from_secs(4)));
    }

    fn fixture_boards(app: &mut LiteApp) {
        app.boards = [
            ("Arduino Uno [atmelavr:uno]", "atmelavr", "ATmega328P"),
            ("ESP32 Development Board with a deliberately long vendor and product name [espressif32:esp32dev]", "espressif32", "ESP32"),
            ("STM32 Nucleo [ststm32:nucleo_f401re]", "ststm32", "STM32F401RE"),
        ].map(|(name, platform, mcu)| Board { name: name.into(), platform: platform.into(), mcu: mcu.into() }).to_vec();
        app.board_total = app.boards.len();
        app.recent_boards = vec![app.boards[0].name.clone()];
    }

    #[test]
    fn board_picker_keeps_rows_aligned_and_bounded_at_windows_scales() {
        for scale in [1.0, 1.25, 1.5, 1.75, 2.0] {
            for size in [egui::vec2(700.0, 560.0), egui::vec2(1140.0, 800.0)] {
                let ctx = egui::Context::default();
                let mut app = LiteApp::with_context(&ctx, true);
                ctx.set_pixels_per_point(scale);
                fixture_boards(&mut app);
                app.open_board_picker();
                let screen = egui::Rect::from_min_size(egui::Pos2::ZERO, size);
                for _ in 0..5 {
                    let _ = ctx.run(
                        egui::RawInput {
                            screen_rect: Some(screen),
                            ..Default::default()
                        },
                        |ctx| app.board_picker(ctx),
                    );
                }
                let window = app.board_geometry.unwrap();
                assert!(
                    screen.expand(1.0).contains_rect(window),
                    "Board picker exceeds {size:?} at {scale}: {window:?}"
                );
                assert_eq!(
                    app.board_rows.len(),
                    3,
                    "Recent boards must not be duplicated in all boards"
                );
                let first = app.board_rows[0].1;
                for (_, row) in &app.board_rows {
                    assert!((row.height() - BOARD_ROW_HEIGHT).abs() < 0.1);
                    assert!((row.left() - first.left()).abs() < 0.1);
                    assert!(
                        (row.width() - first.width()).abs() < 0.1,
                        "Long names must not resize rows"
                    );
                    assert!(window.contains_rect(*row));
                }
                // Arrow navigation changes the candidate, never the configured target.
                let input = |key| egui::RawInput {
                    screen_rect: Some(screen),
                    events: vec![egui::Event::Key {
                        key,
                        physical_key: None,
                        pressed: true,
                        repeat: false,
                        modifiers: egui::Modifiers::NONE,
                    }],
                    ..Default::default()
                };
                let _ = ctx.run(input(egui::Key::ArrowDown), |ctx| app.board_picker(ctx));
                assert_eq!(app.board_candidate, app.boards[0].name);
                assert!(app.board.is_empty());
                assert!(app.board_dialog);
                let _ = ctx.run(input(egui::Key::Escape), |ctx| app.board_picker(ctx));
                assert!(!app.board_dialog);
                assert!(app.board.is_empty());
            }
        }
    }

    #[test]
    fn logo_widget_keeps_clicks_batched_by_a_slow_native_frame() {
        let ctx = egui::Context::default();
        configure(&ctx);
        let start = Instant::now();
        let mut gesture = LogoGesture::default();
        let mut rect = egui::Rect::NOTHING;
        let _ = ctx.run(egui::RawInput::default(), |ctx| {
            egui::CentralPanel::default().show(ctx, |ui| {
                rect = logo_button(ui, &mut gesture, start).rect;
            });
        });
        let pos = rect.center();
        let mut events = vec![egui::Event::PointerMoved(pos)];
        for _ in 0..5 {
            for pressed in [true, false] {
                events.push(egui::Event::PointerButton {
                    pos,
                    button: egui::PointerButton::Primary,
                    pressed,
                    modifiers: egui::Modifiers::NONE,
                });
            }
        }
        let _ = ctx.run(
            egui::RawInput {
                events,
                ..Default::default()
            },
            |ctx| {
                egui::CentralPanel::default().show(ctx, |ui| {
                    logo_button(ui, &mut gesture, start + Duration::from_millis(500));
                });
                if ctx.current_pass_index() == 0 {
                    ctx.request_discard("fixture layout retry");
                }
            },
        );
        assert_eq!(
            gesture.count, 5,
            "Native pointer releases must not collapse into one rendered click"
        );
    }

    #[test]
    fn logo_widget_counts_real_pointer_releases_including_double_clicks() {
        let ctx = egui::Context::default();
        configure(&ctx);
        let start = Instant::now();
        let mut gesture = LogoGesture::default();
        let mut rect = egui::Rect::NOTHING;
        for frame in 0..11 {
            let pos = rect.center();
            let events = if frame == 0 {
                vec![]
            } else {
                vec![
                    egui::Event::PointerMoved(pos),
                    egui::Event::PointerButton {
                        pos,
                        button: egui::PointerButton::Primary,
                        pressed: frame % 2 == 1,
                        modifiers: egui::Modifiers::NONE,
                    },
                ]
            };
            let now = start + Duration::from_millis(frame * 50);
            let _ = ctx.run(
                egui::RawInput {
                    time: Some(frame as f64 * 0.05),
                    screen_rect: Some(egui::Rect::from_min_size(
                        egui::Pos2::ZERO,
                        egui::vec2(700.0, 560.0),
                    )),
                    events,
                    ..Default::default()
                },
                |ctx| {
                    egui::CentralPanel::default().show(ctx, |ui| {
                        rect = logo_button(ui, &mut gesture, now).rect;
                    });
                },
            );
        }
        assert_eq!(gesture.count, 5);
        assert!(gesture.take_ready(start + Duration::from_millis(1301)));
    }

    fn contrast(a: Color32, b: Color32) -> f32 {
        fn light(color: Color32) -> f32 {
            let c = color.to_array().map(|c| {
                let c = c as f32 / 255.0;
                if c <= 0.04045 {
                    c / 12.92
                } else {
                    ((c + 0.055) / 1.055).powf(2.4)
                }
            });
            0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
        }
        let (a, b) = (light(a), light(b));
        (a.max(b) + 0.05) / (a.min(b) + 0.05)
    }

    #[test]
    fn controls_share_one_readable_material_in_both_modes() {
        for dark in [false, true] {
            let ctx = egui::Context::default();
            configure(&ctx);
            apply_theme(&ctx, dark);
            let palette = Palette::new(dark);
            let visuals = ctx.style().visuals.clone();
            // Every field, menu and combo uses the same surface and outline;
            // only hover/selection add the single highlight color.
            for state in [&visuals.widgets.inactive, &visuals.widgets.active] {
                assert_eq!(state.bg_stroke, Stroke::new(2.0_f32, palette.border));
            }
            assert_eq!(visuals.widgets.inactive.weak_bg_fill, palette.surface);
            assert_eq!(visuals.widgets.hovered.weak_bg_fill, palette.highlight);
            assert_eq!(
                visuals.widgets.hovered.bg_stroke, visuals.widgets.inactive.bg_stroke,
                "Hover must not change a field's box"
            );
            for background in [
                palette.paper,
                palette.surface,
                palette.highlight,
                visuals.selection.bg_fill,
            ] {
                assert!(contrast(palette.ink, background) >= 4.5);
            }
            assert!(contrast(palette.muted, palette.surface) >= 4.5);
            for tone in [Tone::Primary, Tone::Secondary, Tone::Danger] {
                let (fill, ink, _) = palette.tone(tone);
                assert!(contrast(ink, fill) >= 4.5, "Button ink must stay readable");
            }
            let mut rect = egui::Rect::NOTHING;
            let mut opened = false;
            for frame in 0..4 {
                let pos = rect.center();
                let events = if frame == 1 || frame == 2 {
                    vec![
                        egui::Event::PointerMoved(pos),
                        egui::Event::PointerButton {
                            pos,
                            button: egui::PointerButton::Primary,
                            pressed: frame == 1,
                            modifiers: egui::Modifiers::NONE,
                        },
                    ]
                } else {
                    vec![]
                };
                let _ = ctx.run(
                    egui::RawInput {
                        screen_rect: Some(egui::Rect::from_min_size(
                            egui::Pos2::ZERO,
                            egui::vec2(500.0, 400.0),
                        )),
                        events,
                        ..Default::default()
                    },
                    |ctx| {
                        egui::CentralPanel::default().show(ctx, |ui| {
                            let response = egui::ComboBox::from_id_salt("fixture-port")
                                .selected_text("Select port")
                                .show_ui(ui, |ui| {
                                    opened = true;
                                    assert_eq!(ui.visuals().override_text_color, Some(palette.ink));
                                    assert!(contrast(palette.ink, ui.visuals().window_fill) >= 4.5);
                                    ui.label("Fixture port");
                                });
                            rect = response.response.rect;
                        });
                    },
                );
            }
            assert!(opened, "The actual connection dropdown must open");
        }
    }

    #[test]
    fn developer_gesture_requires_exactly_five_releases_then_a_pause() {
        let start = Instant::now();
        for count in [1, 4, 5, 6] {
            let mut gesture = LogoGesture::default();
            for n in 0..count {
                gesture.click(start + Duration::from_millis(n as u64 * 100));
            }
            assert!(
                !gesture.take_ready(start + Duration::from_millis((count as u64 - 1) * 100 + 799))
            );
            assert_eq!(
                gesture.take_ready(start + Duration::from_millis((count as u64 - 1) * 100 + 800)),
                count == 5
            );
            assert!(!gesture.take_ready(start + Duration::from_secs(3)));
        }
        let mut gesture = LogoGesture::default();
        for n in 0..5 {
            gesture.click(start + Duration::from_millis(n * 900));
        }
        assert!(!gesture.take_ready(start + Duration::from_secs(6)));
    }

    #[test]
    fn progress_updates_one_owned_row_and_keeps_diagnostics() {
        let mut log = Log::default();
        log.push_progress("Writing 0%", "info", true, Some("1:writing"));
        log.push("Diagnostic retained", "warning", true);
        log.push_progress(
            "Writing 100% / 1702 bytes written",
            "success",
            true,
            Some("1:writing"),
        );
        assert_eq!(log.lines.len(), 2);
        assert!(log.lines[0].text.contains("1702 bytes"));
        assert!(log.lines[0].tone == LogTone::Success);
        assert_eq!(log.lines[1].text, "Diagnostic retained");
        log.push_progress("Writing 0%", "info", true, Some("2:writing"));
        assert_eq!(
            log.lines.len(),
            3,
            "A new upload must have its own progress history"
        );
        log.push("", "info", true);
        assert_eq!(log.lines.back().unwrap().text, "");
    }

    #[test]
    fn theme_switch_preserves_logs_and_monospaced_table_alignment() {
        let ctx = egui::Context::default();
        let mut app = LiteApp::with_context(&ctx, true);
        assert!(
            !ctx.style().visuals.dark_mode,
            "Light must be the launch default"
        );
        app.build_log.push("Saved output", "success", true);
        for dark in [false, true, false] {
            apply_theme(&ctx, dark);
            assert_eq!(ctx.style().visuals.dark_mode, dark);
            let _ = ctx.run(egui::RawInput::default(), |ctx| {
                let rows = [
                    format!("╔{}╗", "═".repeat(69)),
                    format!(
                        "║ {:<67} ║",
                        "Restart : DTR/RTS pulse; Serial Monitor attached at 115200 baud"
                    ),
                ];
                let widths: Vec<_> = rows
                    .iter()
                    .map(|row| {
                        ctx.fonts_mut(|fonts| {
                            fonts
                                .layout_no_wrap(row.clone(), FontId::monospace(13.0), INK)
                                .size()
                                .x
                        })
                    })
                    .collect();
                assert!(
                    (widths[0] - widths[1]).abs() < 1.0,
                    "Box borders and ASCII fields must share the same column grid"
                );
            });
            assert!(app.build_log.text().contains("Saved output"));
            let palette = Palette::new(dark);
            for tone in [
                LogTone::Info,
                LogTone::Error,
                LogTone::Warning,
                LogTone::Success,
                LogTone::Dim,
            ] {
                assert!(
                    contrast(palette.log_color(tone), palette.terminal) >= 4.5,
                    "Log colors must remain readable in either theme"
                );
            }
        }
    }

    #[test]
    fn actions_follow_board_port_and_operation_readiness() {
        let ctx = egui::Context::default();
        let mut app = LiteApp::with_context(&ctx, true);
        assert!(app.action_problem(false).is_some());
        app.source = "/fixture/Sketch".into();
        assert!(app.action_problem(false).is_some());
        app.board = "fixture-board".into();
        assert!(app.action_problem(false).is_some());
        app.framework = "arduino".into();
        assert_eq!(app.action_problem(false), None);
        assert!(app.action_problem(true).is_some());
        app.port = "fixture-port".into();
        assert!(app.action_problem(true).is_some());
        app.ports.push(Port {
            device: app.port.clone(),
            ..Default::default()
        });
        assert_eq!(app.action_problem(true), None);
        for flag in 0..3 {
            app.pending = flag == 0;
            app.busy = flag == 1;
            app.resource_busy = flag == 2;
            assert!(app.action_problem(false).is_some());
            assert!(app.action_problem(true).is_some());
        }
        app.resource_busy = false;
        app.ports.clear();
        assert!(app.action_problem(true).is_some());
        assert_eq!(app.action_problem(false), None);
    }

    #[test]
    fn late_search_results_cannot_replace_the_current_query() {
        let ctx = egui::Context::default();
        let mut app = LiteApp::with_context(&ctx, true);
        app.board_query = "uno".into();
        app.board_loading = true;
        app.event(Event {
            event: "boards".into(),
            data: json!({"query": "esp", "items": [{"name": "Wrong board"}], "total": 1}),
        });
        assert!(app.boards.is_empty());
        assert!(app.board_loading);
        app.event(Event {
            event: "boards".into(),
            data: json!({"query": "uno", "items": [{"name": "Exact board"}], "total": 1}),
        });
        assert_eq!(app.boards[0].name, "Exact board");
        assert!(!app.board_loading);
    }

    #[test]
    fn streaming_monitors_keep_fixed_viewports_and_row_heights() {
        for scale in [1.0, 1.5, 2.0] {
            for width in [350.0, 778.0] {
                let ctx = egui::Context::default();
                ctx.set_pixels_per_point(scale);
                let mut app = LiteApp::with_context(&ctx, true);
                let mut baseline = None;
                let mut content_widths = [0.0_f32; 2];
                // Apply the pending DPI/font change before measuring stream
                // frames, as the native host does before its first layout.
                let _ = ctx.run(egui::RawInput::default(), |_| {});
                for frame in 0..90 {
                    app.serial_tab = frame % 2 == 1;
                    let index = usize::from(app.serial_tab);
                    let log = if app.serial_tab {
                        &mut app.serial_log
                    } else {
                        &mut app.build_log
                    };
                    match frame % 6 {
                        0 => log.push(&"long output ".repeat(90), "info", true),
                        1 => log.push("partial", "info", false),
                        2 => log.push("\tmore\nshort\n", "success", false),
                        3 => log.push("Unicode: 温度 24°C\n", "info", false),
                        _ => log.push("short\n", "info", false),
                    }
                    let _ = ctx.run(
                        egui::RawInput {
                            screen_rect: Some(egui::Rect::from_min_size(
                                egui::Pos2::ZERO,
                                egui::vec2(width, 700.0),
                            )),
                            ..Default::default()
                        },
                        |ctx| {
                            egui::CentralPanel::default().show(ctx, |ui| {
                                app.monitor(ui);
                            });
                        },
                    );
                    let (viewport, content) = app.monitor_geometry.unwrap();
                    assert!(
                        viewport.bottom() > 658.0,
                        "Monitor left an unused footer below its viewport"
                    );
                    assert!(
                        app.monitor_offset.x.abs() < 0.1,
                        "Streaming output moved the horizontal scroll position"
                    );
                    if let Some(expected) = baseline {
                        assert_eq!(
                            viewport, expected,
                            "Monitor moved at scale {scale}, width {width}, frame {frame}"
                        );
                    } else {
                        baseline = Some(viewport);
                    }
                    assert!(
                        content.x >= content_widths[index],
                        "Streaming output shrank its horizontal extent"
                    );
                    content_widths[index] = content.x;
                    for (row, origin) in app.monitor_rows.iter().zip(&app.monitor_text_origins) {
                        assert!(
                            (origin - row.left()).abs() < 0.1,
                            "Partial text moved away from the left edge"
                        );
                    }
                    for rows in app.monitor_rows.windows(2) {
                        assert!(
                            (rows[0].height() - rows[1].height()).abs() < 0.1,
                            "Virtual rows changed height"
                        );
                        assert!(
                            (rows[1].top() - rows[0].bottom()).abs() < 0.1,
                            "Virtual rows overlap or change spacing"
                        );
                    }
                }
            }
        }
    }
}
