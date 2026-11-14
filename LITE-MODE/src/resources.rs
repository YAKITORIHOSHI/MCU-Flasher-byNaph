//! Installed resources and an in-process, read-only sample viewer.
use super::{Palette, Tone, action_row, board_row, button};
use eframe::egui::{self, RichText};
use serde::Deserialize;
use serde_json::{Value, json};

#[derive(Default, Deserialize)]
struct InstalledLibrary {
    id: String,
    name: String,
    version: String,
    origin: String,
    description: String,
    #[serde(skip)]
    search: String,
}

#[derive(Default, Deserialize)]
struct InstalledBoard {
    name: String,
    platform: String,
    mcu: String,
    frameworks: Vec<String>,
    #[serde(skip)]
    search: String,
}

#[derive(Default, Deserialize)]
struct Sample {
    id: String,
    name: String,
}

#[derive(Default)]
pub(super) struct ResourceBrowser {
    pub open: bool,
    pub loaded: bool,
    token: u64,
    loading: bool,
    error: String,
    libraries: Vec<InstalledLibrary>,
    boards: Vec<InstalledBoard>,
    samples: Vec<Sample>,
    query: String,
    sample_query: String,
    library: String,
    library_name: String,
    source_id: String,
    source_name: String,
    source: String,
    find: String,
    case_sensitive: bool,
    matches: Vec<(usize, usize)>,
    current: usize,
    reveal: bool,
    focus_find: bool,
    find_id: Option<egui::Id>,
    layout: Option<(bool, egui::text::LayoutJob)>,
    search_text: String,
    search_offsets: Vec<usize>,
    search_case: Option<bool>,
}

impl ResourceBrowser {
    fn request(&mut self, command: &str) -> Value {
        self.token += 1;
        self.loading = true;
        self.error.clear();
        json!({"cmd": command, "token": self.token})
    }

    pub fn refresh(&mut self) -> Value {
        self.loaded = true;
        self.request("resources")
    }

    pub fn showing_source(&self) -> bool {
        !self.source_id.is_empty()
    }

    pub fn escape_find(&mut self, ctx: &egui::Context) -> bool {
        if let Some(id) = self.find_id
            && ctx.memory(|memory| memory.has_focus(id))
        {
            ctx.memory_mut(|memory| memory.surrender_focus(id));
            self.find.clear();
            self.search();
            return true;
        }
        false
    }

    pub fn preview(&mut self, code: bool) {
        self.open = true;
        self.loaded = true;
        self.libraries = vec![InstalledLibrary {
            id: "preview".into(),
            name: "Blink library".into(),
            version: "1.0.0".into(),
            origin: "Main".into(),
            description: "Synthetic library preview".into(),
            search: "blink library main synthetic library preview".into(),
        }];
        self.boards = vec![InstalledBoard {
            name: "Arduino Uno [atmelavr:uno]".into(),
            platform: "atmelavr".into(),
            mcu: "ATmega328P".into(),
            frameworks: vec!["arduino".into()],
            search: "arduino uno atmelavr uno atmega328p".into(),
        }];
        if code {
            self.library = "preview".into();
            self.library_name = "Blink library".into();
            self.samples = vec![
                Sample {
                    id: "demo".into(),
                    name: "examples/Blink/Blink.ino".into(),
                },
                Sample {
                    id: "header".into(),
                    name: "examples/Blink/pins.h".into(),
                },
            ];
            self.source_id = "demo".into();
            self.source_name = "examples/Blink/Blink.ino".into();
            self.source = "#include <Arduino.h>\n\nvoid setup() {\n    pinMode(LED_BUILTIN, OUTPUT);\n}\n\nvoid loop() {\n    digitalWrite(LED_BUILTIN, HIGH);\n    delay(500);\n    digitalWrite(LED_BUILTIN, LOW);\n    delay(500);\n}\n".into();
        }
    }

    pub fn event(&mut self, name: &str, data: &Value) {
        if data["token"].as_u64() != Some(self.token) {
            return;
        }
        let reset = data["reset"].as_bool().unwrap_or(false);
        match name {
            "resources:libraries" => {
                if reset {
                    self.libraries.clear();
                }
                self.libraries.extend(
                    serde_json::from_value::<Vec<InstalledLibrary>>(data["items"].clone())
                        .unwrap_or_default()
                        .into_iter()
                        .map(|mut item| {
                            item.search =
                                format!("{} {} {}", item.name, item.origin, item.description)
                                    .to_lowercase();
                            item
                        }),
                );
            }
            "resources:boards" => {
                if reset {
                    self.boards.clear();
                }
                self.boards.extend(
                    serde_json::from_value::<Vec<InstalledBoard>>(data["items"].clone())
                        .unwrap_or_default()
                        .into_iter()
                        .map(|mut item| {
                            item.search = format!("{} {} {}", item.name, item.platform, item.mcu)
                                .to_lowercase();
                            item
                        }),
                );
            }
            "resources:samples" => {
                if reset {
                    self.samples.clear();
                }
                self.samples.extend(
                    serde_json::from_value::<Vec<Sample>>(data["items"].clone())
                        .unwrap_or_default(),
                );
            }
            "resources:source" => {
                if reset {
                    self.source.clear();
                    self.layout = None;
                    self.search_case = None;
                }
                if let Some(items) = data["items"].as_array() {
                    for item in items {
                        self.source_id = item["id"].as_str().unwrap_or_default().into();
                        self.source_name = item["name"].as_str().unwrap_or_default().into();
                        self.source
                            .push_str(item["text"].as_str().unwrap_or_default());
                    }
                }
                if data["done"].as_bool().unwrap_or(false) {
                    self.search();
                }
            }
            "resources:loaded" => self.loading = false,
            "resources:error" => {
                self.loading = false;
                self.error = data["message"]
                    .as_str()
                    .unwrap_or("Resource scan failed")
                    .into();
            }
            _ => {}
        }
    }

    pub fn show(&mut self, ui: &mut egui::Ui, boards: bool, idle: bool) -> Vec<Value> {
        let palette = Palette::from_context(ui.ctx());
        let mut commands = vec![];
        if !boards && !self.library.is_empty() {
            self.sample_view(ui, &mut commands);
            return commands;
        }
        action_row(ui, ui.available_width() < 330.0, 34.0, |ui| {
            ui.label(
                RichText::new(if boards {
                    "Downloaded boards"
                } else {
                    "Installed libraries"
                })
                .strong(),
            );
            if button(ui, "Refresh", "", Tone::Secondary, egui::vec2(100.0, 34.0)).clicked() {
                commands.push(self.refresh());
            }
        });
        if self.loading {
            ui.label("Reading installed resources…");
        }
        if !self.error.is_empty() {
            ui.colored_label(palette.log_color(super::LogTone::Error), &self.error);
        }
        let filter = ui.add(
            egui::TextEdit::singleline(&mut self.query)
                .hint_text(if boards {
                    "Filter board, platform or MCU (Ctrl+F)"
                } else {
                    "Filter installed libraries (Ctrl+F)"
                })
                .desired_width(f32::INFINITY)
                .char_limit(200),
        );
        if ui.input_mut(|input| input.consume_key(egui::Modifiers::CTRL, egui::Key::F)) {
            filter.request_focus();
        }
        let query = self.query.to_lowercase();
        if boards {
            let rows: Vec<_> = self
                .boards
                .iter()
                .filter(|item| item.search.contains(&query))
                .collect();
            ui.label(format!("{} downloaded board definitions", rows.len()));
            egui::ScrollArea::vertical()
                .id_salt("installed-boards")
                .max_height(300.0)
                .show_rows(ui, 48.0, rows.len(), |ui, range| {
                    for index in range {
                        let item = rows[index];
                        let detail = format!("{} · {}", item.mcu, item.frameworks.join(", "));
                        let response = ui
                            .add_enabled_ui(idle, |ui| board_row(ui, &item.name, &detail, false))
                            .inner;
                        if response.on_hover_text(&item.name).clicked() {
                            commands.push(json!({"cmd": "board", "name": item.name}));
                        }
                    }
                });
            if self.boards.is_empty() && !self.loading {
                ui.label("Bootstrap prepares the configured boards. Choose a board in Connection to prepare another exact target automatically on Compile.");
            }
        } else {
            let rows: Vec<_> = self
                .libraries
                .iter()
                .filter(|item| item.search.contains(&query))
                .collect();
            ui.label(format!(
                "{} installed libraries · select a library for its sample files",
                rows.len()
            ));
            let mut selected = None;
            egui::ScrollArea::vertical()
                .id_salt("installed-libraries")
                .max_height(300.0)
                .show_rows(ui, 48.0, rows.len(), |ui, range| {
                    for index in range {
                        let item = rows[index];
                        let detail = format!("{} · {}", item.version, item.origin);
                        if board_row(ui, &item.name, &detail, false)
                            .on_hover_text(format!("{}\n{}", item.name, item.description))
                            .clicked()
                        {
                            selected = Some((item.id.clone(), item.name.clone()));
                        }
                    }
                });
            if let Some((id, name)) = selected {
                self.library = id;
                self.library_name = name;
                self.samples.clear();
                self.source_id.clear();
                self.sample_query.clear();
                let mut request = self.request("samples");
                request["library"] = json!(self.library);
                commands.push(request);
            }
            if self.libraries.is_empty() && !self.loading {
                ui.label(
                    "No installed libraries found. Use Find more libraries below to install one.",
                );
            }
        }
        commands
    }

    fn sample_view(&mut self, ui: &mut egui::Ui, commands: &mut Vec<Value>) {
        if button(
            ui,
            "All libraries",
            "",
            Tone::Secondary,
            egui::vec2(140.0, 32.0),
        )
        .clicked()
        {
            self.library.clear();
            self.source_id.clear();
            return;
        }
        ui.label(RichText::new(&self.library_name).strong());
        ui.add(
            egui::TextEdit::singleline(&mut self.sample_query)
                .hint_text("Filter all sample files")
                .desired_width(f32::INFINITY)
                .char_limit(200),
        );
        let query = self.sample_query.to_lowercase();
        let rows: Vec<_> = self
            .samples
            .iter()
            .filter(|sample| sample.name.to_lowercase().contains(&query))
            .collect();
        ui.label(format!("{} sample files", rows.len()));
        let mut selected = None;
        egui::ScrollArea::vertical()
            .id_salt("sample-files")
            .max_height(if self.source_id.is_empty() {
                300.0
            } else {
                112.0
            })
            .show_rows(ui, 28.0, rows.len(), |ui, range| {
                for index in range {
                    let item = rows[index];
                    if ui
                        .add(
                            egui::Button::selectable(item.id == self.source_id, &item.name)
                                .wrap_mode(egui::TextWrapMode::Truncate)
                                .min_size(egui::vec2(ui.available_width(), 24.0)),
                        )
                        .on_hover_text(&item.name)
                        .clicked()
                    {
                        selected = Some(item.id.clone());
                    }
                }
            });
        if let Some(id) = selected {
            self.source_id = id.clone();
            self.source_name = self
                .samples
                .iter()
                .find(|sample| sample.id == id)
                .map(|sample| sample.name.clone())
                .unwrap_or_default();
            self.source.clear();
            self.layout = None;
            self.search_case = None;
            self.matches.clear();
            let mut request = self.request("sample_source");
            request["sample"] = json!(id);
            commands.push(request);
        }
        if self.samples.is_empty() && !self.loading {
            ui.label("This library has no sample files.");
        }
        if !self.source_id.is_empty() && !self.loading {
            self.code_view(ui);
        }
        if self.loading {
            ui.label("Reading sample files…");
        }
        if !self.error.is_empty() {
            ui.label(&self.error);
        }
    }

    fn search(&mut self) {
        self.matches.clear();
        self.current = 0;
        if !self.find.is_empty() {
            // Character offsets belong to the original source, even when a
            // Unicode lowercase conversion changes a character's byte length.
            if self.search_case != Some(self.case_sensitive) {
                self.search_text.clear();
                self.search_offsets.clear();
                for (index, character) in self.source.chars().enumerate() {
                    if self.case_sensitive {
                        self.search_text.push(character);
                        self.search_offsets
                            .extend(std::iter::repeat_n(index, character.len_utf8()));
                    } else {
                        for lower in character.to_lowercase() {
                            self.search_text.push(lower);
                            self.search_offsets
                                .extend(std::iter::repeat_n(index, lower.len_utf8()));
                        }
                    }
                }
                self.search_case = Some(self.case_sensitive);
            }
            let query = if self.case_sensitive {
                self.find.clone()
            } else {
                self.find.to_lowercase()
            };
            for (byte, value) in self.search_text.match_indices(&query) {
                self.matches.push((
                    self.search_offsets[byte],
                    self.search_offsets[byte + value.len() - 1] + 1,
                ));
            }
        }
        self.reveal = true;
    }

    fn code_view(&mut self, ui: &mut egui::Ui) {
        ui.separator();
        ui.label(RichText::new(&self.source_name).strong());
        if ui.input_mut(|input| input.consume_key(egui::Modifiers::CTRL, egui::Key::F)) {
            self.focus_find = true;
        }
        let mut step = 0;
        if ui.input_mut(|input| input.consume_key(egui::Modifiers::SHIFT, egui::Key::F3)) {
            step = -1;
        }
        if ui.input_mut(|input| input.consume_key(egui::Modifiers::NONE, egui::Key::F3)) {
            step = 1;
        }
        ui.vertical(|ui| {
            let response = ui.add(
                egui::TextEdit::singleline(&mut self.find)
                    .hint_text("Find in code (Ctrl+F)")
                    .desired_width(f32::INFINITY)
                    .char_limit(200),
            );
            self.find_id = Some(response.id);
            if self.focus_find {
                response.request_focus();
                self.focus_find = false;
            }
            if response.changed() {
                self.search();
            }
            if (response.has_focus() || response.lost_focus())
                && ui.input(|input| input.key_pressed(egui::Key::Enter))
            {
                step = if ui.input(|input| input.modifiers.shift) {
                    -1
                } else {
                    1
                };
            }
            ui.horizontal_wrapped(|ui| {
                if ui.checkbox(&mut self.case_sensitive, "Aa").changed() {
                    self.search();
                }
                if ui.small_button("Previous").clicked() {
                    step = -1;
                }
                if ui.small_button("Next").clicked() {
                    step = 1;
                }
                if ui.small_button("Copy code").clicked() {
                    ui.ctx().copy_text(self.source.clone());
                }
            });
        });
        if !self.matches.is_empty() && step != 0 {
            self.current =
                (self.current as isize + step).rem_euclid(self.matches.len() as isize) as usize;
            self.reveal = true;
        }
        if !self.find.is_empty() {
            ui.label(if self.matches.is_empty() {
                "No matches".into()
            } else {
                format!("{} / {} matches", self.current + 1, self.matches.len())
            });
        }
        let palette = Palette::from_context(ui.ctx());
        if self
            .layout
            .as_ref()
            .is_none_or(|(dark, _)| *dark != palette.dark)
        {
            self.layout = Some((palette.dark, cpp_layout(&self.source, palette)));
        }
        let layout = &self.layout.as_ref().unwrap().1;
        let mut layouter = |ui: &egui::Ui, _text: &dyn egui::TextBuffer, _width: f32| {
            ui.fonts_mut(|fonts| fonts.layout_job(layout.clone()))
        };
        egui::Frame::new()
            .fill(palette.surface)
            .stroke(egui::Stroke::new(2.0_f32, palette.border))
            .inner_margin(8)
            .show(ui, |ui| {
                egui::ScrollArea::both()
                    .id_salt((&self.source_id, "source-code"))
                    .max_height(340.0)
                    .show(ui, |ui| {
                        let mut text = self.source.as_str();
                        let mut output = egui::TextEdit::multiline(&mut text)
                            .code_editor()
                            .layouter(&mut layouter)
                            .desired_rows(12)
                            .desired_width(ui.available_width().max(280.0))
                            .id_salt("sample-source")
                            .show(ui);
                        if self.reveal {
                            self.reveal = false;
                            if let Some(&(start, end)) = self.matches.get(self.current) {
                                let cursor = egui::text::CCursor::new(start);
                                output.state.cursor.set_char_range(Some(
                                    egui::text::CCursorRange::two(
                                        cursor,
                                        egui::text::CCursor::new(end),
                                    ),
                                ));
                                output.state.store(ui.ctx(), output.response.id);
                                ui.scroll_to_rect(
                                    output
                                        .galley
                                        .pos_from_cursor(cursor)
                                        .translate(output.galley_pos.to_vec2()),
                                    Some(egui::Align::Center),
                                );
                            }
                        }
                    });
            });
    }
}

fn cpp_layout(source: &str, palette: Palette) -> egui::text::LayoutJob {
    let mut job = egui::text::LayoutJob::default();
    let bytes = source.as_bytes();
    let mut index = 0;
    let keyword = if palette.dark {
        egui::Color32::from_rgb(161, 199, 255)
    } else {
        egui::Color32::from_rgb(31, 72, 140)
    };
    let string = if palette.dark {
        egui::Color32::from_rgb(179, 231, 172)
    } else {
        egui::Color32::from_rgb(31, 98, 45)
    };
    while index < bytes.len() {
        let start = index;
        let mut color = palette.ink;
        if source[start..].starts_with("//") {
            index += source[start..].find('\n').unwrap_or(source.len() - start);
            color = palette.muted;
        } else if source[start..].starts_with("/*") {
            index = source[start + 2..]
                .find("*/")
                .map_or(source.len(), |end| start + end + 4);
            color = palette.muted;
        } else if matches!(bytes[index], b'\"' | b'\'') {
            let quote = bytes[index];
            index += 1;
            while index < bytes.len() {
                if bytes[index] == b'\\' {
                    index = (index + 2).min(bytes.len());
                } else if bytes[index] == quote {
                    index += 1;
                    break;
                } else {
                    index += 1;
                }
            }
            color = string;
        } else if bytes[index].is_ascii_alphabetic() || bytes[index] == b'_' {
            index += 1;
            while index < bytes.len()
                && (bytes[index].is_ascii_alphanumeric() || bytes[index] == b'_')
            {
                index += 1;
            }
            if matches!(
                &source[start..index],
                "void"
                    | "int"
                    | "char"
                    | "bool"
                    | "float"
                    | "double"
                    | "const"
                    | "static"
                    | "return"
                    | "if"
                    | "else"
                    | "for"
                    | "while"
                    | "class"
                    | "struct"
                    | "public"
                    | "private"
                    | "include"
                    | "define"
                    | "true"
                    | "false"
                    | "unsigned"
                    | "long"
                    | "auto"
            ) {
                color = keyword;
            }
        } else {
            index += source[start..].chars().next().unwrap().len_utf8();
        }
        let format = egui::TextFormat {
            font_id: egui::FontId::monospace(14.0),
            color,
            ..Default::default()
        };
        // Adjacent tokens with the same ink share one section; whitespace and
        // punctuation must not cost a layout allocation for every character.
        if let Some(last) = job.sections.last_mut()
            && last.format == format
        {
            job.text.push_str(&source[start..index]);
            last.byte_range.end = job.text.len();
        } else {
            job.append(&source[start..index], 0.0, format);
        }
    }
    job
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn native_viewer_find_keys_select_matches_without_editing_the_source() {
        let ctx = egui::Context::default();
        let mut browser = ResourceBrowser::default();
        browser.preview(true);
        let original = browser.source.clone();
        fn frame(ctx: &egui::Context, browser: &mut ResourceBrowser, events: Vec<egui::Event>) {
            let _ = ctx.run(
                egui::RawInput {
                    screen_rect: Some(egui::Rect::from_min_size(
                        egui::Pos2::ZERO,
                        egui::vec2(700.0, 800.0),
                    )),
                    events,
                    ..Default::default()
                },
                |ctx| {
                    egui::CentralPanel::default().show(ctx, |ui| browser.code_view(ui));
                },
            );
        }
        fn key(key: egui::Key, modifiers: egui::Modifiers) -> egui::Event {
            egui::Event::Key {
                key,
                physical_key: None,
                pressed: true,
                repeat: false,
                modifiers,
            }
        }
        frame(&ctx, &mut browser, vec![]);
        frame(
            &ctx,
            &mut browser,
            vec![key(egui::Key::F, egui::Modifiers::CTRL)],
        );
        frame(&ctx, &mut browser, vec![egui::Event::Text("delay".into())]);
        assert_eq!(browser.find, "delay");
        assert_eq!(browser.matches.len(), 2);
        frame(
            &ctx,
            &mut browser,
            vec![key(egui::Key::F3, egui::Modifiers::NONE)],
        );
        assert_eq!(browser.current, 1);
        frame(
            &ctx,
            &mut browser,
            vec![key(egui::Key::F3, egui::Modifiers::SHIFT)],
        );
        assert_eq!(browser.current, 0);
        assert!(browser.escape_find(&ctx));
        assert!(browser.find.is_empty());
        assert_eq!(browser.source, original);
    }

    #[test]
    fn source_is_assembled_only_for_the_current_request_and_find_keeps_unicode_offsets() {
        let mut browser = ResourceBrowser::default();
        browser.request("sample_source");
        browser.event(
            "resources:source",
            &json!({"token": 0, "reset": true, "items": [{"text": "stale"}]}),
        );
        assert!(browser.source.is_empty());
        browser.event("resources:source", &json!({"token": 1, "reset": true, "items": [{"id":"a", "name":"demo.ino", "text":"İ blink\n"}]}));
        browser.event("resources:source", &json!({"token": 1, "done": true, "items": [{"id":"a", "name":"demo.ino", "text":"BLINK"}]}));
        browser.find = "blink".into();
        browser.search();
        assert_eq!(browser.matches, vec![(2, 7), (8, 13)]);
        browser.case_sensitive = true;
        browser.search();
        assert_eq!(browser.matches, vec![(2, 7)]);
    }
}
