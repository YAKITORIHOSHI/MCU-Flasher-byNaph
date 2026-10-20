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
    sync::mpsc,
    time::{Duration, Instant},
};
use wry::WebView;

use mcu_native_ui_probe::workspace_project::read_project;
pub use mcu_native_ui_probe::workspace_project::{CHECK_HEADER, CHECK_SOURCE};
type TabStates = BTreeMap<String, (gtk::Label, String, u64, gtk::Button)>;

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
    project_path: Option<PathBuf>,
    loading: Cell<bool>,
    pending_tabs: RefCell<Option<Vec<(String, String)>>>,
    self_check: bool,
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
        for text in PRIMARY_ACTIONS {
            if *text == "Compile" {
                header.insert(compile, -1);
            } else if *text == "Project" {
                header.insert(&project, -1);
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
            project_path,
            loading: Cell::new(false),
            pending_tabs: RefCell::new(None),
            self_check,
            check_phase: Cell::new(0),
            check_result: Cell::new(None),
            checks: RefCell::new(BTreeMap::new()),
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
    pub fn release_views(&self) {
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
            RendererMessage::TerminalSnapshot { .. } => {
                if let Err(error) = self.apply_theme(self.theme.get()) {
                    self.status.set_text(&error.to_string());
                }
            }
            RendererMessage::Error { message } => {
                self.loading.set(false);
                self.pending_tabs.borrow_mut().take();
                self.status.set_text(message);
                if self.self_check {
                    self.finish_check(false);
                }
            }
            RendererMessage::DocumentsOpened { documents } => {
                let matches =
                    self.pending_tabs.borrow().as_ref().is_some_and(|entries| {
                        entries.iter().map(|(id, _)| id).eq(documents.iter())
                    });
                if matches {
                    if let Some(entries) = self.pending_tabs.borrow_mut().take() {
                        self.set_tabs(&entries);
                    }
                    self.loading.set(false);
                    self.status.set_text(
                        "Project opened. Source files are unchanged; edits stay in memory.",
                    );
                    if self.self_check {
                        self.request_snapshot(0);
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
                    shell.pending_tabs.borrow_mut().take();
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
                shell.pending_tabs.borrow_mut().take();
                shell
                    .status
                    .set_text("The project read timed out. Existing editor buffers are unchanged.");
                if shell.self_check {
                    shell.finish_check(false);
                }
                return glib::ControlFlow::Break;
            }
            match receiver.try_recv() {
                Ok(Ok((entries, script))) => {
                    if let Some(editor) = shell.editor.borrow().as_ref() {
                        *shell.pending_tabs.borrow_mut() = Some(entries);
                        if let Err(error) = editor.evaluate_script(&script) {
                            shell.pending_tabs.borrow_mut().take();
                            shell.loading.set(false);
                            shell.status.set_text(&error.to_string());
                        }
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
