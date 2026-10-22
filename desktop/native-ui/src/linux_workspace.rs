//! Native workspace chrome; only the editor and terminal use embedded pages.
use gtk::{glib, prelude::*};
use mcu_native_ui_probe::{
    workspace_design::{Theme, PRIMARY_ACTIONS, TOOL_TABS},
    RendererMessage,
};
use std::{
    cell::{Cell, RefCell},
    collections::BTreeMap,
    path::PathBuf,
    rc::Rc,
    sync::{mpsc, Arc, Mutex},
    time::{Duration, Instant},
};
use wry::WebView;

use mcu_native_ui_probe::workspace_project::{
    read_project, ProjectSessions, SAVE_WORKER_LOST_MESSAGE,
};
pub use mcu_native_ui_probe::workspace_project::{CHECK_HEADER, CHECK_SOURCE};
use mcu_native_ui_probe::workspace_save::{SaveResult, SaveWork};
type TabStates = BTreeMap<String, (gtk::Label, String, u64, gtk::Button)>;
const EXTERNAL_SAVE_FIXTURE: &[u8] = b"// external native-save fixture edit\n";

// This worker helper is reachable only from the explicit --save-smoke mode.
// Its root was atomically created by the native fixture launcher, never IPC.
fn save_fixture_io(
    root: PathBuf,
    documents: Vec<(String, String)>,
    phase: u8,
) -> Result<BTreeMap<String, bool>, String> {
    use std::io::{Read, Write};
    let checkout = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
        .canonicalize()
        .map_err(|e| e.to_string())?;
    let scratch =
        mcu_native_ui_probe::checked_scratch_root(&checkout).map_err(|e| e.to_string())?;
    let canonical = root.canonicalize().map_err(|e| e.to_string())?;
    if canonical != root
        || !root.starts_with(&scratch)
        || !root
            .parent()
            .and_then(|p| p.file_name())
            .is_some_and(|name| name.to_string_lossy().starts_with("native-ui-"))
        || documents.len() != 2
        || !documents.iter().any(|(name, _)| name == "Sketch.ino")
        || !documents.iter().any(|(name, _)| name == "Config.h")
    {
        return Err("Save verification refused an unowned source fixture.".into());
    }
    let mut checks = BTreeMap::new();
    for (name, content) in documents {
        if !matches!(name.as_str(), "Sketch.ino" | "Config.h") || content.len() > 8192 {
            return Err("Save fixture source exceeds its fixed name/byte budget.".into());
        }
        let source = root.join(&name);
        let metadata = std::fs::symlink_metadata(&source).map_err(|e| e.to_string())?;
        if !metadata.is_file() || metadata.file_type().is_symlink() {
            return Err("Save fixture source is not a regular owned file.".into());
        }
        let mut bytes = Vec::new();
        std::fs::File::open(&source)
            .map_err(|e| e.to_string())?
            .take(8193)
            .read_to_end(&mut bytes)
            .map_err(|e| e.to_string())?;
        let expected = if phase == 4 && name == "Sketch.ino" {
            EXTERNAL_SAVE_FIXTURE
        } else {
            content.as_bytes()
        };
        if bytes != expected {
            return Err(format!(
                "Native save fixture bytes differ for {name}, phase {phase}."
            ));
        }
        if phase == 1 && name == "Sketch.ino" {
            checks.insert(
                "saved_bom_unicode".into(),
                bytes.starts_with(&[0xef, 0xbb, 0xbf])
                    && content.contains("// native saved café 日本語"),
            );
        }
        if phase == 1 && name == "Config.h" {
            checks.insert(
                "save_preserves_other_source".into(),
                bytes == CHECK_HEADER.as_bytes(),
            );
        }
        if phase == 3 {
            checks.insert(
                format!("save_all_{}", name.replace('.', "_")),
                content.contains("// saved all αβ") && !content.contains("// later unsaved γ"),
            );
            if name == "Sketch.ino" {
                // Deliberately simulate an external editor only in this owned
                // developer fixture, after checking the real saved bytes.
                let mut file = std::fs::OpenOptions::new()
                    .write(true)
                    .truncate(true)
                    .open(&source)
                    .map_err(|e| e.to_string())?;
                file.write_all(EXTERNAL_SAVE_FIXTURE)
                    .and_then(|()| file.sync_all())
                    .map_err(|e| e.to_string())?;
            }
        }
    }
    checks.insert(format!("phase_{phase}_disk_bytes"), true);
    let mut maps = String::new();
    std::fs::File::open("/proc/self/maps")
        .map_err(|e| e.to_string())?
        .take(4 * 1024 * 1024)
        .read_to_string(&mut maps)
        .map_err(|e| e.to_string())?;
    checks.insert(
        "no_python_application_service".into(),
        !maps.to_ascii_lowercase().contains("libpython"),
    );
    Ok(checks)
}

pub struct WorkspaceShell {
    pub body: gtk::Box,
    pub split: gtk::Paned,
    pub tools: gtk::Notebook,
    sources: gtk::Box,
    tabs: RefCell<TabStates>,
    css: gtk::CssProvider,
    theme: Cell<Theme>,
    editor: RefCell<Option<Rc<WebView>>>,
    terminal: RefCell<Option<Rc<WebView>>>,
    status: gtk::Label,
    project: gtk::Button,
    save: gtk::Button,
    save_all: gtk::Button,
    save_transfer_timer: RefCell<Option<glib::SourceId>>,
    save_completion: RefCell<Option<glib::SourceId>>,
    save_check: Cell<bool>,
    save_check_phase: Cell<u8>,
    save_check_result: Cell<Option<bool>>,
    save_checks: RefCell<BTreeMap<String, bool>>,
    save_fixture_io: Cell<bool>,
    project_path: Option<PathBuf>,
    loading: Cell<bool>,
    projects: RefCell<ProjectSessions>,
    self_check: bool,
    save_read_only: bool,
    check_phase: Cell<u8>,
    check_result: Cell<Option<bool>>,
    checks: RefCell<BTreeMap<String, bool>>,
}

fn label(text: &str, class: &str) -> gtk::Label {
    let label = gtk::Label::new(Some(text));
    label.set_xalign(0.0);
    label.style_context().add_class(class);
    label
}

fn flow() -> gtk::FlowBox {
    let row = gtk::FlowBox::new();
    row.set_selection_mode(gtk::SelectionMode::None);
    row.set_homogeneous(false);
    row.set_min_children_per_line(1);
    row.set_max_children_per_line(30);
    row.set_column_spacing(4);
    row.set_row_spacing(4);
    row.set_valign(gtk::Align::Start);
    row
}

fn action(text: &str, enabled: bool) -> gtk::Button {
    let button = gtk::Button::with_label(text);
    button.set_sensitive(enabled);
    button.style_context().add_class("workspace-action");
    if !enabled {
        button.set_tooltip_text(Some(
            "This action is not available in this development build.",
        ));
    }
    button
}

fn refresh_icons(widget: &gtk::Widget, theme: Theme) {
    if let Some(button) = widget.downcast_ref::<gtk::Button>() {
        let text = button.label().unwrap_or_default();
        let svg = match text.as_str() {
            "Compile" => Some(include_str!(
                "../../../src/assets/icons/actions/compile.svg"
            )),
            "Upload" => Some(include_str!("../../../src/assets/icons/actions/upload.svg")),
            "Stop" => Some(include_str!("../../../src/assets/icons/actions/stop.svg")),
            "Clean" => Some(include_str!("../../../src/assets/icons/actions/clean.svg")),
            "Save" | "Save All" => Some(include_str!("../../../src/assets/icons/actions/save.svg")),
            "Reload" => Some(include_str!("../../../src/assets/icons/actions/reload.svg")),
            "Modify" => Some(include_str!("../../../src/assets/icons/actions/modify.svg")),
            "Project" => Some(include_str!(
                "../../../src/assets/icons/actions/project.svg"
            )),
            "Cloud" => Some(include_str!("../../../src/assets/icons/actions/cloud.svg")),
            "Resources" => Some(include_str!(
                "../../../src/assets/icons/actions/download.svg"
            )),
            "Detach editor" | "Detach Editor" => {
                Some(include_str!("../../../src/assets/icons/actions/detach.svg"))
            }
            "AI Assistant" => Some(include_str!("../../../src/assets/icons/actions/ai.svg")),
            "Copy" => Some(include_str!("../../../src/assets/icons/actions/copy.svg")),
            "Clear" => Some(include_str!("../../../src/assets/icons/actions/clear.svg")),
            _ => None,
        };
        if let Some(svg) = svg {
            let loader = gtk::gdk_pixbuf::PixbufLoader::new();
            let source = svg.replace("currentColor", theme.color("TEXT_BRIGHT"));
            if loader.write(source.as_bytes()).is_ok() && loader.close().is_ok() {
                if let Some(pixbuf) = loader.pixbuf().and_then(|image| {
                    image.scale_simple(16, 16, gtk::gdk_pixbuf::InterpType::Bilinear)
                }) {
                    let image = gtk::Image::from_pixbuf(Some(&pixbuf));
                    button.set_image(Some(&image));
                    button.set_always_show_image(true);
                }
            }
        }
    }
    if let Some(container) = widget.downcast_ref::<gtk::Container>() {
        for child in container.children() {
            refresh_icons(&child, theme);
        }
    }
}

impl WorkspaceShell {
    #[allow(clippy::too_many_arguments)] // Native widget wiring, all UI-thread borrows.
    pub fn new(
        window: &gtk::Window,
        boards: &gtk::ComboBoxText,
        compile: &gtk::Button,
        detach: &gtk::Button,
        editor_host: &gtk::Box,
        terminal_host: &gtk::Box,
        status: &gtk::Label,
        smoke: bool,
        theme: Theme,
        project_path: Option<PathBuf>,
        self_check: bool,
    ) -> Result<Rc<Self>, Box<dyn std::error::Error>> {
        let body = gtk::Box::new(gtk::Orientation::Vertical, 0);
        body.style_context().add_class("workspace");
        let header = flow();
        header.style_context().add_class("header");
        let brand = label("MCU Flasher by Naph", "brand");
        brand.set_margin_end(18);
        header.insert(&brand, -1);
        let project = action("Project", true);
        let save = action("Save", false);
        let save_all = action("Save All", false);
        save.set_tooltip_text(Some("Save the active source file (Ctrl+S)."));
        save_all.set_tooltip_text(Some("Save all changed source files (Ctrl+Shift+S)."));
        for text in PRIMARY_ACTIONS {
            if *text == "Compile" {
                header.insert(compile, -1);
            } else if *text == "Project" {
                header.insert(&project, -1);
            } else if *text == "Save" {
                header.insert(&save, -1);
            } else if *text == "Save All" {
                header.insert(&save_all, -1);
            } else {
                header.insert(&action(text, false), -1);
            }
        }
        if !smoke {
            compile.set_label("Compile");
            compile.set_sensitive(false);
        }
        compile.style_context().add_class("workspace-action");
        let controls = flow();
        controls.style_context().add_class("controls");
        let board_group = gtk::Box::new(gtk::Orientation::Vertical, 3);
        board_group.pack_start(&label("BOARD", "caption"), false, false, 0);
        boards.set_size_request(190, 28);
        board_group.pack_start(boards, false, false, 0);
        controls.insert(&board_group, -1);
        if !smoke {
            boards.remove_all();
            boards.append_text("Select board");
            boards.set_active(Some(0));
        }
        for (caption, values) in [
            ("Serial port", vec!["Select serial port"]),
            ("Upload speed", vec!["460800", "115200", "230400", "921600"]),
        ] {
            let group = gtk::Box::new(gtk::Orientation::Vertical, 3);
            group.pack_start(&label(caption, "caption"), false, false, 0);
            let combo = gtk::ComboBoxText::new();
            for value in values {
                combo.append_text(value);
            }
            combo.set_active(Some(0));
            if caption == "Serial port" {
                combo.set_size_request(190, 28);
            }
            group.pack_start(&combo, false, false, 0);
            controls.insert(&group, -1);
        }
        let options = gtk::Box::new(gtk::Orientation::Horizontal, 6);
        options.set_valign(gtk::Align::End);
        options.pack_start(&gtk::CheckButton::with_label("Timestamps"), false, false, 0);
        options.pack_start(
            &gtk::CheckButton::with_label("Skip Compile"),
            false,
            false,
            0,
        );
        controls.insert(&options, -1);
        let workspace = gtk::Box::new(gtk::Orientation::Vertical, 3);
        workspace.pack_start(&label("Workspace", "caption"), false, false, 0);
        let workspace_actions = gtk::Box::new(gtk::Orientation::Horizontal, 4);
        workspace_actions.pack_start(detach, false, false, 0);
        let hide_editor = action("Hide Editor", true);
        let hide_monitors = action("Hide Monitors", true);
        workspace_actions.pack_start(&hide_editor, false, false, 0);
        workspace_actions.pack_start(&hide_monitors, false, false, 0);
        let themes = gtk::ComboBoxText::new();
        for choice in [Theme::Glass, Theme::FrostedLight, Theme::SolarizedDark] {
            themes.append(Some(choice.key()), choice.label());
        }
        themes.set_active_id(Some(theme.key()));
        themes.set_tooltip_text(Some("Workspace theme"));
        workspace_actions.pack_start(&themes, false, false, 0);
        workspace_actions.pack_start(&action("AI Assistant", false), false, false, 0);
        workspace.pack_start(&workspace_actions, false, false, 0);
        controls.insert(&workspace, -1);
        let sources = gtk::Box::new(gtk::Orientation::Horizontal, 4);
        sources.style_context().add_class("source-tabs");
        let editor_area = gtk::Box::new(gtk::Orientation::Vertical, 0);
        let source_scroll =
            gtk::ScrolledWindow::new(None::<&gtk::Adjustment>, None::<&gtk::Adjustment>);
        source_scroll.set_policy(gtk::PolicyType::Automatic, gtk::PolicyType::Never);
        source_scroll.set_propagate_natural_width(false);
        source_scroll.set_min_content_width(1);
        source_scroll.add(&sources);
        editor_area.pack_start(&source_scroll, false, false, 0);
        editor_area.pack_start(editor_host, true, true, 0);
        let split = gtk::Paned::new(gtk::Orientation::Vertical);
        split.set_wide_handle(true);
        split.pack1(&editor_area, true, false);
        let tools = gtk::Notebook::new();
        tools.set_scrollable(true);
        tools.set_show_border(false);
        tools.style_context().add_class("tool-tabs");
        for text in TOOL_TABS {
            let panel = gtk::Box::new(gtk::Orientation::Vertical, 0);
            if *text == "Terminal" {
                panel.pack_start(terminal_host, true, true, 0);
            } else {
                let toolbar = gtk::Box::new(gtk::Orientation::Horizontal, 6);
                toolbar.set_margin_start(8);
                toolbar.set_margin_end(8);
                toolbar.set_margin_top(5);
                toolbar.set_margin_bottom(5);
                toolbar.pack_start(
                    &label(
                        if *text == "Build Console" {
                            "BUILD OUTPUT"
                        } else {
                            text
                        },
                        "caption",
                    ),
                    true,
                    true,
                    0,
                );
                let auto_scroll = gtk::CheckButton::with_label("Auto-scroll");
                auto_scroll.set_active(true);
                toolbar.pack_end(&action("Clear", false), false, false, 0);
                toolbar.pack_end(&action("Copy", false), false, false, 0);
                toolbar.pack_end(&auto_scroll, false, false, 0);
                panel.pack_start(&toolbar, false, false, 0);
                let output = gtk::TextView::new();
                output.set_editable(false);
                output.set_cursor_visible(false);
                output.set_left_margin(8);
                output.set_top_margin(8);
                output.set_monospace(true);
                if let Some(buffer) = output.buffer() {
                    buffer.set_text(if *text == "Build Console" {
                    "Choose the exact board before compiling.\nNative Rust workspace development build: device operations are not enabled."
                } else { "" });
                }
                let scroll =
                    gtk::ScrolledWindow::new(None::<&gtk::Adjustment>, None::<&gtk::Adjustment>);
                scroll.add(&output);
                panel.pack_start(&scroll, true, true, 0);
            }
            tools.append_page(&panel, Some(&gtk::Label::new(Some(text))));
        }
        tools.set_current_page(Some(if smoke { 5 } else { 0 }));
        split.pack2(&tools, true, false);
        split.set_position(430);
        body.pack_start(&header, false, false, 0);
        body.pack_start(&controls, false, false, 0);
        body.pack_start(&split, true, true, 0);
        status.style_context().add_class("status");
        body.pack_end(status, false, false, 0);
        let css = gtk::CssProvider::new();
        if let Some(screen) = gtk::gdk::Screen::default() {
            gtk::StyleContext::add_provider_for_screen(
                &screen,
                &css,
                gtk::STYLE_PROVIDER_PRIORITY_APPLICATION,
            );
        }
        let shell = Rc::new(Self {
            body,
            split,
            tools,
            sources,
            tabs: RefCell::new(BTreeMap::new()),
            css,
            theme: Cell::new(theme),
            editor: RefCell::new(None),
            terminal: RefCell::new(None),
            status: status.clone(),
            project,
            save,
            save_all,
            save_transfer_timer: RefCell::new(None),
            save_completion: RefCell::new(None),
            save_check: Cell::new(false),
            save_check_phase: Cell::new(0),
            save_check_result: Cell::new(None),
            save_checks: RefCell::new(BTreeMap::new()),
            save_fixture_io: Cell::new(false),
            project_path,
            loading: Cell::new(false),
            projects: RefCell::new(ProjectSessions::default()),
            self_check,
            save_read_only: smoke || self_check,
            check_phase: Cell::new(0),
            check_result: Cell::new(None),
            checks: RefCell::new(BTreeMap::new()),
        });
        for (button, all) in [(&shell.save, false), (&shell.save_all, true)] {
            let weak = Rc::downgrade(&shell);
            button.connect_clicked(move |_| {
                if let Some(shell) = weak.upgrade() {
                    shell.start_save(all);
                }
            });
        }
        shell.bind_save_shortcuts(window);
        let weak = Rc::downgrade(&shell);
        window.connect_delete_event(move |_, _| {
            if let Some(shell) = weak.upgrade() {
                if shell.projects.borrow().is_saving() || shell.save_fixture_io.get() {
                    shell.status.set_text(
                        "A source save is still in progress. Wait for it to finish before closing.",
                    );
                    return glib::Propagation::Stop;
                }
            }
            glib::Propagation::Proceed
        });
        shell.apply_theme(theme)?;
        let weak = Rc::downgrade(&shell);
        themes.connect_changed(move |combo| {
            if let Some(shell) = weak.upgrade() {
                if let Some(key) = combo.active_id() {
                    if let Err(error) = shell.apply_theme(Theme::parse(&key)) {
                        shell.status.set_text(&error.to_string());
                    }
                }
            }
        });
        let editor_area = editor_area.clone();
        hide_editor.connect_clicked(move |button| {
            let visible = editor_area.is_visible();
            editor_area.set_visible(!visible);
            button.set_label(if visible {
                "Show Editor"
            } else {
                "Hide Editor"
            });
        });
        let monitors = shell.tools.clone();
        hide_monitors.connect_clicked(move |button| {
            let visible = monitors.is_visible();
            monitors.set_visible(!visible);
            button.set_label(if visible {
                "Show Monitors"
            } else {
                "Hide Monitors"
            });
        });
        let parent = window.clone();
        let weak = Rc::downgrade(&shell);
        shell.project.connect_clicked(move |_| {
            let dialog = gtk::FileChooserDialog::new(
                Some("Open project in a separate workspace"),
                Some(&parent),
                gtk::FileChooserAction::SelectFolder,
            );
            dialog.add_buttons(&[
                ("Cancel", gtk::ResponseType::Cancel),
                ("Open", gtk::ResponseType::Accept),
            ]);
            if dialog.run() == gtk::ResponseType::Accept {
                if let Some(path) = dialog.filename() {
                    let result = std::env::current_exe().and_then(|exe| {
                        std::process::Command::new(exe)
                            .arg("--project")
                            .arg(path)
                            .spawn()
                    });
                    match result {
                        Ok(mut child) => {
                            std::thread::spawn(move || {
                                let _ = child.wait();
                            });
                        }
                        Err(error) => {
                            if let Some(shell) = weak.upgrade() {
                                shell.status.set_text(&error.to_string());
                            }
                        }
                    }
                }
            }
            dialog.close();
        });
        Ok(shell)
    }

    pub fn bind_views(&self, editor: Rc<WebView>, terminal: Rc<WebView>) {
        *self.editor.borrow_mut() = Some(editor);
        *self.terminal.borrow_mut() = Some(terminal);
    }
    pub fn is_loading(&self) -> bool {
        self.loading.get()
    }
    pub fn check_passed(&self) -> bool {
        self.check_result.get() == Some(true)
    }
    pub fn enable_save_check(&self) {
        self.save_check.set(true);
    }
    pub fn save_check_passed(&self) -> bool {
        self.save_check_result.get() == Some(true)
    }
    pub fn release_views(&self) {
        self.stop_save_transfer_timer();
        if let Some(source) = self.save_completion.borrow_mut().take() {
            source.remove();
        }
        // Drop every view reference captured by source-tab callbacks before
        // WebContext destruction. The local native adapter still owns views.
        for child in self.sources.children() {
            self.sources.remove(&child);
        }
        self.tabs.borrow_mut().clear();
        self.editor.borrow_mut().take();
        self.terminal.borrow_mut().take();
    }
    pub fn receive(self: &Rc<Self>, message: &RendererMessage) {
        match message {
            RendererMessage::SaveStarted { .. }
            | RendererMessage::SaveChunk { .. }
            | RendererMessage::SaveFinished { .. }
            | RendererMessage::SaveFailed { .. } => self.receive_save(message),
            RendererMessage::SaveSnapshot {
                phase,
                same_models,
                history_retained,
                selection_retained,
                first_dirty,
                second_dirty,
            } if self.save_check.get() => self.receive_save_check(
                *phase,
                *same_models,
                *history_retained,
                *selection_retained,
                *first_dirty,
                *second_dirty,
            ),
            RendererMessage::TerminalSnapshot { .. } => {
                if let Err(error) = self.apply_theme(self.theme.get()) {
                    self.status.set_text(&error.to_string());
                }
            }
            RendererMessage::Error { message } => {
                if self.projects.borrow().is_saving() && self.save_completion.borrow().is_none() {
                    self.abort_save(message);
                }
                self.loading.set(false);
                self.projects.borrow_mut().cancel_pending();
                self.status.set_text(message);
                if self.self_check {
                    self.finish_check(false);
                }
                if self.save_check.get() {
                    self.finish_save_check(false);
                }
            }
            RendererMessage::DocumentsOpened { documents } => {
                let acknowledged = self.projects.borrow_mut().acknowledge(documents);
                match acknowledged {
                    Ok(Some(entries)) => {
                        self.set_tabs(&entries);
                        self.loading.set(false);
                        self.sync_save_controls();
                        self.status.set_text(
                            "Project opened. Source files are unchanged; edits stay in memory.",
                        );
                        if self.save_check.get() {
                            self.request_save_check(0);
                        } else if self.self_check {
                            self.request_snapshot(0);
                        }
                    }
                    Ok(None) => {}
                    Err(error) => {
                        self.projects.borrow_mut().cancel_pending();
                        self.loading.set(false);
                        self.status.set_text(&error);
                        if self.self_check {
                            self.finish_check(false);
                        }
                    }
                }
            }
            RendererMessage::WorkspaceSnapshot {
                phase,
                documents,
                same_models,
                dirty_retained,
                history_retained,
                selection_retained,
                theme,
                background,
            } if self.self_check => {
                let palette = self.theme.get();
                let rgb = palette.rgb("BG_DARKEST");
                let expected = format!(
                    "rgb({}, {}, {})",
                    (rgb >> 16) & 255,
                    (rgb >> 8) & 255,
                    rgb & 255
                );
                let mut checks = self.checks.borrow_mut();
                checks.insert(
                    format!("phase_{phase}_models"),
                    *phase == self.check_phase.get() && *documents == 2 && *same_models,
                );
                checks.insert(
                    format!("phase_{phase}_dirty_selection"),
                    *dirty_retained && *selection_retained,
                );
                checks.insert(
                    format!("phase_{phase}_theme"),
                    theme == palette.key() && background == &expected,
                );
                if *phase == 3 {
                    checks.insert("undo_redo".into(), *history_retained);
                }
                let passed = checks.values().all(|value| *value);
                drop(checks);
                if !passed || *phase == 3 {
                    self.finish_check(passed);
                } else {
                    let next = phase + 1;
                    self.check_phase.set(next);
                    if let Err(error) = self.apply_theme(
                        [
                            Theme::Glass,
                            Theme::FrostedLight,
                            Theme::SolarizedDark,
                            Theme::Glass,
                        ][usize::from(next)],
                    ) {
                        self.status.set_text(&error.to_string());
                        self.finish_check(false);
                    } else {
                        self.request_snapshot(next);
                    }
                }
            }
            RendererMessage::WorkspaceReady {} => {
                self.set_tabs(&[("untitled".into(), "Untitled.ino".into())]);
                if let Err(error) = self.apply_theme(self.theme.get()) {
                    self.status.set_text(&error.to_string());
                }
                if let Some(root) = &self.project_path {
                    self.load_project(root.clone());
                }
            }
            RendererMessage::DocumentChanged {
                document_id,
                edit_revision,
                dirty,
            } => {
                if let Some((label, name, revision, _)) =
                    self.tabs.borrow_mut().get_mut(document_id)
                {
                    if *edit_revision > *revision {
                        *revision = *edit_revision;
                        label.set_text(&format!("{}{}", name, if *dirty { " *" } else { "" }));
                    }
                }
            }
            _ => {}
        }
    }

    /// Bind native shortcuts to each owning main/detached window. Renderer
    /// reports supply only the snapshot requested by these native commands.
    pub fn bind_save_shortcuts(self: &Rc<Self>, window: &gtk::Window) {
        let weak = Rc::downgrade(self);
        window.connect_key_press_event(move |_, event| {
            let modifiers = event.state();
            if modifiers.contains(gtk::gdk::ModifierType::CONTROL_MASK)
                && !modifiers.intersects(
                    gtk::gdk::ModifierType::MOD1_MASK | gtk::gdk::ModifierType::SUPER_MASK,
                )
                && matches!(
                    event.keyval(),
                    gtk::gdk::keys::constants::s | gtk::gdk::keys::constants::S
                )
            {
                if let Some(shell) = weak.upgrade() {
                    shell.start_save(modifiers.contains(gtk::gdk::ModifierType::SHIFT_MASK));
                }
                return glib::Propagation::Stop;
            }
            glib::Propagation::Proceed
        });
    }

    fn sync_save_controls(&self) {
        let projects = self.projects.borrow();
        let saving = projects.is_saving() || self.save_fixture_io.get();
        let enabled = !self.save_read_only && !self.loading.get() && projects.can_save() && !saving;
        drop(projects);
        self.save.set_sensitive(enabled);
        self.save_all.set_sensitive(enabled);
        for (_, _, _, button) in self.tabs.borrow().values() {
            button.set_sensitive(!saving);
        }
    }

    fn evaluate_save_script(&self, script: &str) -> Result<(), String> {
        let editor = self
            .editor
            .borrow()
            .clone()
            .ok_or("The editor is unavailable.")?;
        editor
            .evaluate_script(script)
            .map_err(|error| error.to_string())
    }

    fn start_save(self: &Rc<Self>, all: bool) {
        if self.save_read_only || self.loading.get() || self.save_fixture_io.get() {
            self.status.set_text(
                "Saving is unavailable while the read-only fixture or project reader is active.",
            );
            return;
        }
        let begin = self.projects.borrow_mut().begin_save(all);
        let script = match begin {
            Ok(script) => script,
            Err(error) => {
                self.status.set_text(&error);
                return;
            }
        };
        self.sync_save_controls();
        self.status
            .set_text("Reading changed editor buffers for saving…");
        let weak = Rc::downgrade(self);
        let source = glib::timeout_add_local(Duration::from_millis(100), move || {
            let Some(shell) = weak.upgrade() else {
                return glib::ControlFlow::Break;
            };
            if shell.projects.borrow().save_timed_out() {
                shell.abort_save(
                    "The editor save snapshot timed out. Unsaved buffers are retained.",
                );
                return glib::ControlFlow::Break;
            }
            if !shell.projects.borrow().is_saving() {
                return glib::ControlFlow::Break;
            }
            glib::ControlFlow::Continue
        });
        *self.save_transfer_timer.borrow_mut() = Some(source);
        if let Err(error) = self.evaluate_save_script(&script) {
            self.abort_save(&format!(
                "The editor could not provide save buffers: {error}"
            ));
        }
    }

    fn stop_save_transfer_timer(&self) {
        if let Some(source) = self.save_transfer_timer.borrow_mut().take() {
            source.remove();
        }
    }

    fn abort_save(&self, reason: &str) {
        let script = self.projects.borrow_mut().abort_save();
        self.stop_save_transfer_timer();
        self.sync_save_controls();
        self.status.set_text(reason);
        if let Some(script) = script {
            if let Err(error) = self.evaluate_save_script(&script) {
                self.status
                    .set_text(&format!("{reason} Editor acknowledgement failed: {error}"));
            }
        }
    }

    fn receive_save(self: &Rc<Self>, message: &RendererMessage) {
        let received = self.projects.borrow_mut().accept_save_message(message);
        match received {
            Ok(Some(work)) => {
                self.stop_save_transfer_timer();
                self.run_save_worker(work);
            }
            Ok(None) => {}
            Err(error) => self.abort_save(&error),
        }
    }

    fn run_save_worker(self: &Rc<Self>, work: SaveWork) {
        self.status.set_text("Saving source files…");
        // glib 0.18's supported channel wakes the UI only for completion. It
        // carries owned Send results; no GTK/WebView/Rc value enters the worker.
        #[allow(deprecated)]
        let (sender, receiver) =
            glib::MainContext::channel::<Result<SaveResult, ()>>(glib::Priority::DEFAULT);
        let weak = Rc::downgrade(self);
        let source = receiver.attach(None, move |result| {
            if let Some(shell) = weak.upgrade() {
                shell.save_completion.borrow_mut().take();
                match result {
                    Ok(result) => shell.finish_save(result),
                    Err(()) => shell.save_worker_lost(),
                }
            }
            glib::ControlFlow::Break
        });
        *self.save_completion.borrow_mut() = Some(source);
        // Keep the work recoverable until the OS accepts the thread. The slot
        // is held only while moving ownership, never during filesystem work.
        let slot = Arc::new(Mutex::new(Some(work)));
        let worker_slot = Arc::clone(&slot);
        let spawned = std::thread::Builder::new()
            .name("native-source-save".into())
            .spawn(move || {
                let work = worker_slot
                    .lock()
                    .unwrap_or_else(|poisoned| poisoned.into_inner())
                    .take();
                let result = work.ok_or(()).and_then(|work| {
                    // A terminated worker cannot return its native authority.
                    // Report that terminal state without pretending a rollback
                    // or allowing another save against an unknown baseline.
                    std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| work.run()))
                        .map_err(|_| ())
                });
                let _ = sender.send(result);
            });
        if let Err(error) = spawned {
            if let Some(source) = self.save_completion.borrow_mut().take() {
                source.remove();
            }
            let work = slot
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner())
                .take();
            if let Some(work) = work {
                self.finish_save(
                    work.cancel_before_run(format!("The save worker could not start: {error}")),
                );
            }
        }
    }

    fn save_worker_lost(&self) {
        let script = self.projects.borrow_mut().save_worker_lost();
        self.sync_save_controls();
        self.status.set_text(SAVE_WORKER_LOST_MESSAGE);
        if let Some(script) = script {
            if let Err(error) = self.evaluate_save_script(&script) {
                self.status.set_text(&format!("The save worker stopped; partial disk changes are possible. Buffers remain open, saving is disabled until reopening. Editor acknowledgement failed: {error}"));
            }
        }
        if self.save_check.get() {
            self.finish_save_check(false);
        }
    }

    fn finish_save(&self, result: SaveResult) {
        let status = result.status_text();
        if self.save_check.get() {
            match self.save_check_phase.get() {
                1 => {
                    self.save_checks
                        .borrow_mut()
                        .insert("native_save".into(), status == "1 file(s) saved.");
                }
                3 => {
                    self.save_checks
                        .borrow_mut()
                        .insert("native_save_all".into(), status == "2 file(s) saved.");
                }
                _ => {}
            }
        }
        if self.save_check.get() && self.save_check_phase.get() == 4 {
            self.save_checks.borrow_mut().insert(
                "external_change_reported".into(),
                status.starts_with("0 file(s) saved. "),
            );
        }
        let finished = self.projects.borrow_mut().finish_save(result);
        self.sync_save_controls();
        match finished {
            Ok(mut script) => {
                self.status.set_text(&status);
                if self.save_check.get() {
                    script.push_str(&format!(
                        "window.__mcuSaveFixtureStep({});",
                        self.save_check_phase.get()
                    ));
                }
                if let Err(error) = self.evaluate_save_script(&script) {
                    self.status.set_text(&format!("{status} Editor acknowledgement failed: {error}; changed buffers remain dirty."));
                }
            }
            Err(error) => {
                self.status.set_text(&error);
                if self.save_check.get() {
                    self.finish_save_check(false);
                }
            }
        }
    }

    fn request_save_check(&self, phase: u8) {
        self.save_check_phase.set(phase);
        if let Err(error) =
            self.evaluate_save_script(&format!("window.__mcuSaveFixtureStep({phase});"))
        {
            self.status.set_text(&error);
            self.finish_save_check(false);
        }
    }

    fn receive_save_check(
        self: &Rc<Self>,
        phase: u8,
        same: bool,
        history: bool,
        selection: bool,
        first_dirty: bool,
        second_dirty: bool,
    ) {
        if self.save_check_result.get().is_some() {
            return;
        }
        let mut checks = self.save_checks.borrow_mut();
        checks
            .entry("same_models".into())
            .and_modify(|passed| *passed &= same)
            .or_insert(same);
        checks
            .entry("selection_retained".into())
            .and_modify(|passed| *passed &= selection)
            .or_insert(selection);
        if phase > 0 {
            checks
                .entry("undo_redo_retained".into())
                .and_modify(|passed| *passed &= history)
                .or_insert(history);
        }
        if phase == 3 {
            checks.insert("late_edit_retained".into(), first_dirty && !second_dirty);
        }
        checks.insert(
            format!("phase_{phase}_models_selection"),
            phase == self.save_check_phase.get() && same && selection,
        );
        checks.insert(
            format!("phase_{phase}_dirty"),
            match phase {
                0 => first_dirty && !second_dirty,
                1 => !first_dirty && !second_dirty,
                2 => first_dirty && second_dirty,
                3 | 4 => first_dirty && !second_dirty,
                _ => false,
            },
        );
        if phase > 0 {
            checks.insert(format!("phase_{phase}_undo_redo"), history);
        }
        let passed = checks.values().all(|passed| *passed);
        drop(checks);
        if !passed {
            self.finish_save_check(false);
            return;
        }
        match phase {
            0 => {
                self.save_check_phase.set(1);
                self.save.emit_clicked();
            }
            1 | 3 | 4 => self.run_save_fixture_io(phase),
            2 => {
                self.save_check_phase.set(3);
                self.save_all.emit_clicked();
            }
            _ => self.finish_save_check(false),
        }
    }

    fn run_save_fixture_io(self: &Rc<Self>, phase: u8) {
        let root = self.project_path.clone();
        let documents = self.projects.borrow().active().map(|session| {
            session
                .documents()
                .iter()
                .map(|document| (document.document.name.clone(), document.content.clone()))
                .collect::<Vec<_>>()
        });
        let (Some(root), Some(documents)) = (root, documents) else {
            self.finish_save_check(false);
            return;
        };
        self.save_fixture_io.set(true);
        self.sync_save_controls();
        #[allow(deprecated)]
        let (sender, receiver) = glib::MainContext::channel::<Result<BTreeMap<String, bool>, String>>(
            glib::Priority::DEFAULT,
        );
        let weak = Rc::downgrade(self);
        let source = receiver.attach(None, move |result| {
            if let Some(shell) = weak.upgrade() {
                shell.save_fixture_io.set(false);
                shell.sync_save_controls();
                match result {
                    Ok(checks) => {
                        shell.save_checks.borrow_mut().extend(checks);
                        match phase {
                            1 => shell.request_save_check(2),
                            3 => {
                                shell.save_check_phase.set(4);
                                shell.save.emit_clicked();
                            }
                            4 => shell.finish_save_check(true),
                            _ => shell.finish_save_check(false),
                        }
                    }
                    Err(error) => {
                        shell.status.set_text(&error);
                        shell.finish_save_check(false);
                    }
                }
            }
            glib::ControlFlow::Break
        });
        if let Err(error) = std::thread::Builder::new()
            .name("native-save-fixture".into())
            .spawn(move || {
                let _ = sender.send(save_fixture_io(root, documents, phase));
            })
        {
            source.remove();
            self.save_fixture_io.set(false);
            self.status
                .set_text(&format!("Save fixture worker could not start: {error}"));
            self.finish_save_check(false);
        }
    }

    fn finish_save_check(&self, passed: bool) {
        if self.save_check_result.get().is_some() {
            return;
        }
        let mut checks = self.save_checks.borrow_mut();
        checks.insert(
            "native_save_buttons".into(),
            self.save.parent().is_some() && self.save_all.parent().is_some(),
        );
        let disk_bytes_verified = [1, 3, 4]
            .iter()
            .all(|phase| checks.get(&format!("phase_{phase}_disk_bytes")) == Some(&true));
        checks.insert("disk_bytes_verified".into(), disk_bytes_verified);
        let conflict = checks.get("external_change_reported") == Some(&true)
            && checks.get("phase_4_disk_bytes") == Some(&true);
        checks.insert("external_change_refused".into(), conflict);
        let passed = passed && checks.values().all(|passed| *passed);
        self.save_check_result.set(Some(passed));
        println!(
            "MCU_NATIVE_SAVE={}",
            serde_json::json!({"passed":passed,"checks":&*checks,
            "scope":"Native GTK Save/Save All, owned two-file fixture and worker byte checks; no live project/device/settings operations"})
        );
        if gtk::main_level() > 0 {
            gtk::main_quit();
        }
    }
    fn set_tabs(self: &Rc<Self>, entries: &[(String, String)]) {
        for child in self.sources.children() {
            self.sources.remove(&child);
        }
        self.tabs.borrow_mut().clear();
        for (id, name) in entries {
            let button = gtk::Button::new();
            button.style_context().add_class("source-tab");
            let text = gtk::Label::new(Some(name));
            button.add(&text);
            let id_for_click = id.clone();
            let shell = Rc::downgrade(self);
            button.connect_clicked(move |_| {
                if let Some(shell) = shell.upgrade() {
                    shell.select_tab(&id_for_click);
                }
            });
            self.tabs
                .borrow_mut()
                .insert(id.clone(), (text, name.clone(), 0, button.clone()));
            self.sources.pack_start(&button, false, false, 0);
        }
        self.sources.show_all();
        if let Some((id, _)) = entries.first() {
            self.select_tab(id);
        }
    }
    fn select_tab(&self, id: &str) {
        let tabs = self.tabs.borrow();
        if !tabs.contains_key(id) {
            return;
        }
        for (key, (_, _, _, button)) in tabs.iter() {
            if key == id {
                button.style_context().add_class("selected");
            } else {
                button.style_context().remove_class("selected");
            }
        }
        if let Some(editor) = self.editor.borrow().as_ref() {
            if let Ok(id) = serde_json::to_string(id) {
                let _ = editor.evaluate_script(&format!("window.__mcuSelectDocument({id});"));
            }
        }
    }
    fn request_snapshot(&self, phase: u8) {
        if let Some(editor) = self.editor.borrow().as_ref() {
            if let Err(error) = editor.evaluate_script(&format!("window.__mcuWorkspaceSnapshot({phase}).catch(error => window.ipc.postMessage(JSON.stringify({{kind:'error',message:String(error).slice(0,1024)}})));")) {
                self.status.set_text(&error.to_string()); self.finish_check(false);
            }
        }
    }
    fn finish_check(&self, passed: bool) {
        if self.check_result.get().is_some() {
            return;
        }
        let mut checks = self.checks.borrow_mut();
        checks.insert("native_tools".into(), self.tools.n_pages() == 7);
        checks.insert("native_source_tabs".into(), self.tabs.borrow().len() == 2);
        checks.insert(
            "native_project_session".into(),
            self.projects.borrow().active().is_some_and(|project| {
                !project.workspace_id().is_empty() && project.documents().len() == 2
            }),
        );
        checks.insert(
            "native_owned_source_controls".into(),
            self.tabs
                .borrow()
                .values()
                .all(|(_, _, _, button)| button.parent().is_some()),
        );
        checks.insert(
            "no_python_application_service".into(),
            std::fs::read_to_string("/proc/self/maps")
                .is_ok_and(|maps| !maps.to_ascii_lowercase().contains("libpython")),
        );
        let unchanged = self.project_path.as_ref().is_some_and(|root| {
            std::fs::read(root.join("Sketch.ino"))
                .is_ok_and(|bytes| bytes == CHECK_SOURCE.as_bytes())
                && std::fs::read(root.join("Config.h"))
                    .is_ok_and(|bytes| bytes == CHECK_HEADER.as_bytes())
        });
        checks.insert("source_bytes_unchanged".into(), unchanged);
        let passed = passed && checks.values().all(|value| *value);
        self.check_result.set(Some(passed));
        println!(
            "MCU_NATIVE_WORKSPACE={}",
            serde_json::json!({"passed":passed,"checks":&*checks,"scope":"Native GTK workspace and Rust source reader; owned files only; no save/device/settings operations"})
        );
        gtk::main_quit();
    }
    fn load_project(self: &Rc<Self>, selected: PathBuf) {
        if self.loading.replace(true) {
            return;
        }
        self.status.set_text("Reading project sources…");
        let (sender, receiver) = mpsc::sync_channel(1);
        std::thread::spawn(move || {
            let _ = sender.send(read_project(&selected));
        });
        let weak = Rc::downgrade(self);
        let started = Instant::now();
        let acknowledgement = Rc::downgrade(self);
        glib::timeout_add_local_once(Duration::from_secs(25), move || {
            if let Some(shell) = acknowledgement.upgrade() {
                if shell.loading.replace(false) {
                    shell.projects.borrow_mut().cancel_pending();
                    shell.status.set_text("The editor did not acknowledge the project load. Existing buffers are unchanged.");
                    if shell.self_check {
                        shell.finish_check(false);
                    }
                }
            }
        });
        glib::timeout_add_local(Duration::from_millis(20), move || {
            let Some(shell) = weak.upgrade() else {
                return glib::ControlFlow::Break;
            };
            if started.elapsed() > Duration::from_secs(20) {
                shell.loading.set(false);
                shell.projects.borrow_mut().cancel_pending();
                shell
                    .status
                    .set_text("The project read timed out. Existing editor buffers are unchanged.");
                if shell.self_check {
                    shell.finish_check(false);
                }
                return glib::ControlFlow::Break;
            }
            match receiver.try_recv() {
                Ok(Ok(project)) => {
                    if let Some(editor) = shell.editor.borrow().as_ref() {
                        let staged = shell.projects.borrow_mut().stage(project);
                        let result = staged.and_then(|()| {
                            let script = shell
                                .projects
                                .borrow_mut()
                                .take_pending_script()
                                .ok_or("The pending project script was already dispatched.")?;
                            editor
                                .evaluate_script(&script)
                                .map_err(|error| error.to_string())
                        });
                        if let Err(error) = result {
                            shell.projects.borrow_mut().cancel_pending();
                            shell.loading.set(false);
                            shell.status.set_text(&error);
                        }
                    } else {
                        shell.loading.set(false);
                        shell
                            .status
                            .set_text("The editor is unavailable; the project read was released.");
                    }
                    glib::ControlFlow::Break
                }
                Ok(Err(error)) => {
                    shell.loading.set(false);
                    shell.status.set_text(&error);
                    glib::ControlFlow::Break
                }
                Err(mpsc::TryRecvError::Empty) => glib::ControlFlow::Continue,
                Err(mpsc::TryRecvError::Disconnected) => {
                    shell.loading.set(false);
                    shell
                        .status
                        .set_text("The project reader stopped before completing.");
                    glib::ControlFlow::Break
                }
            }
        });
    }
    fn apply_theme(&self, theme: Theme) -> Result<(), Box<dyn std::error::Error>> {
        self.theme.set(theme);
        let c = |key| theme.color(key);
        self.css.load_from_data(format!(r#"
            .workspace, window {{ background-color: {dark}; color: {text}; font-family: Montserrat; font-size: 11px; }}
            .workspace {{ background-image: linear-gradient(135deg, {light}, {dark} 35%, {deep} 80%); }}
            .header {{ padding: 6px 8px; border-bottom: 1px solid {border}; background-image: linear-gradient(to bottom,{light},{dark}); }}
            .controls {{ padding: 10px 8px 8px; }} flowboxchild {{ padding: 0; background: transparent; }}
            .brand {{ color: {logo}; font-weight: bold; font-size: 14px; }}
            .caption {{ color: {dim}; font-weight: 600; font-size: 10px; }}
            button, combobox button {{ min-height: 20px; padding: 3px 9px; color: {text}; background-image: linear-gradient(to bottom,{light},{dark}); border: 1px solid {border}; border-radius: 6px; box-shadow: none; text-shadow: none; }}
            button:hover, button:focus {{ border-color: {accent}; }} button:disabled {{ opacity: 0.45; }}
            .workspace-action {{ font-weight: 600; border-color: {accent}; }}
            entry, textview text {{ color: {text}; background-color: {deep}; }}
            checkbutton check {{ min-width: 14px; min-height: 14px; border: 1px solid {border}; border-radius: 3px; background: {dark}; color: {bright}; }}
            checkbutton check:checked {{ background: {hover}; border-color: {accent}; }}
            notebook, notebook > stack {{ background: transparent; border: none; }}
            notebook header {{ background: transparent; border: none; padding: 4px 0; }}
            notebook tab {{ min-height: 18px; padding: 4px 12px; margin-right: 4px; border: 1px solid {border}; border-radius: 7px; background-image: linear-gradient(to bottom,{light},{dark}); }}
            notebook tab label {{ color: {dim}; }}
            notebook tab:checked label {{ color: {bright}; }}
            notebook tab:checked {{ box-shadow: inset 0 -2px {accent}; border-color: {accent}; color: {bright}; background: {hover}; }}
            .source-tabs {{ padding: 4px 8px; }} .source-tab.selected {{ border-color: {accent}; background: {hover}; }}
            paned separator {{ min-height: 8px; background: transparent; }}
            .status {{ padding: 4px 8px; color: {dim}; border-top: 1px solid {border}; }}
        "#, dark=c("BG_DARK"), deep=c("BG_DARKEST"), light=c("BG_LIGHT"), text=c("TEXT"),
            dim=c("TEXT_DIM"), bright=c("TEXT_BRIGHT"), border=c("BORDER"), accent=c("CYAN"), hover=c("BG_HOVER"),
            logo=mcu_native_ui_probe::workspace_design::LOGO_INK).as_bytes())?;
        let palette = serde_json::json!({"background": c("BG_DARKEST"), "foreground": c("TEXT"), "accent": c("CYAN"), "selection":c("BG_LIGHT")});
        refresh_icons(self.body.upcast_ref(), theme);
        let mode = serde_json::to_string(theme.key())?;
        let script = format!("if(window.__mcuApplyTheme)window.__mcuApplyTheme({mode},{palette});");
        if let Some(view) = self.editor.borrow().as_ref() {
            view.evaluate_script(&script)?;
        }
        if let Some(view) = self.terminal.borrow().as_ref() {
            view.evaluate_script(&script)?;
        }
        Ok(())
    }
}

#[cfg(test)]
mod source_row_checks {
    use super::*;
    #[test]
    #[ignore = "requires an isolated GTK display"]
    fn wide_source_manifest_keeps_native_window_bounded_and_all_tabs() {
        gtk::init().expect("isolated GTK display");
        let window = gtk::Window::new(gtk::WindowType::Toplevel);
        window.set_default_size(960, 540);
        let shell = WorkspaceShell::new(
            &window,
            &gtk::ComboBoxText::new(),
            &gtk::Button::new(),
            &gtk::Button::new(),
            &gtk::Box::new(gtk::Orientation::Vertical, 0),
            &gtk::Box::new(gtk::Orientation::Vertical, 0),
            &gtk::Label::new(None),
            false,
            Theme::Glass,
            None,
            false,
        )
        .expect("native workspace");
        let entries: Vec<_> = (0..40)
            .map(|index| {
                (
                    format!("owned-{index}"),
                    format!("LongSourceHeader{index:02}.hpp"),
                )
            })
            .collect();
        shell.set_tabs(&entries);
        window.add(&shell.body);
        window.show_all();
        glib::timeout_add_local_once(Duration::from_millis(250), gtk::main_quit);
        gtk::main();
        println!(
            "Bounded source row: window={} row={} retained_tabs={}",
            window.allocated_width(),
            shell.sources.allocated_width(),
            shell.tabs.borrow().len()
        );
        assert_eq!(
            shell.tabs.borrow().len(),
            40,
            "Every source model selector must remain reachable"
        );
        assert!(
            window.allocated_width() <= 960,
            "Wide manifests must not force a larger native frame: {}",
            window.allocated_width()
        );
        assert!(
            shell.sources.allocated_width() > window.allocated_width(),
            "The retained row must scroll inside its bounded viewport"
        );
        shell.release_views();
        window.close();
    }
}
