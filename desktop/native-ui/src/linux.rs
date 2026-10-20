use crate::linux_workspace::WorkspaceShell;
use gtk::{glib, prelude::*};
use mcu_native_ui_probe::workspace_design::Theme;
use mcu_native_ui_probe::{
    asset_path, checked_scratch_root, parse_message, Island, RendererMessage, CHANGED_SOURCE,
    SOURCE,
};
use serde::Serialize;
use std::{
    borrow::Cow,
    cell::{Cell, RefCell},
    collections::BTreeMap,
    path::PathBuf,
    rc::Rc,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
use wry::{
    http::Response, WebContext, WebView, WebViewBuilder, WebViewBuilderExtUnix, WebViewExtUnix,
};
type WorkspaceEvents = Rc<dyn Fn(&RendererMessage)>;

const DEADLINE: Duration = Duration::from_secs(45);
const CSP: &str = "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; connect-src 'self'; worker-src 'self' blob:; object-src 'none'; frame-src 'none'; base-uri 'none'; form-action 'none'";

#[derive(Default)]
struct ProbeState {
    error: Option<String>,
    editor: Option<EditorEvidence>,
    terminal_ready: bool,
    expected_phase: u8,
    combo_events: u32,
    compile_events: u32,
    checks: BTreeMap<String, bool>,
    workspace_ready: bool,
    workspace_events: Option<WorkspaceEvents>,
}

struct EditorEvidence {
    model_id: String,
    history_retained: bool,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct Receipt {
    schema_version: u8,
    passed: bool,
    mode: &'static str,
    os: &'static str,
    architecture: &'static str,
    display_backend: String,
    webview_version: String,
    widget_types: Vec<String>,
    checks: BTreeMap<String, bool>,
    error: Option<String>,
    limits: &'static str,
}

fn bytes(path: &str) -> (&'static str, &'static [u8]) {
    match path {
        "editor.html" => (
            "text/html; charset=utf-8",
            include_bytes!("../assets/editor.html"),
        ),
        "terminal.html" => (
            "text/html; charset=utf-8",
            include_bytes!("../assets/terminal.html"),
        ),
        "probe-editor.js" => (
            "text/javascript",
            include_bytes!("../assets/probe-editor.js"),
        ),
        "probe-terminal.js" => (
            "text/javascript",
            include_bytes!("../assets/probe-terminal.js"),
        ),
        "probe.css" => ("text/css", include_bytes!("../assets/probe.css")),
        "bundle.js" => (
            "text/javascript",
            include_bytes!("../../../src/editor/bundle.js"),
        ),
        "18.bundle.js" => (
            "text/javascript",
            include_bytes!("../../../src/editor/18.bundle.js"),
        ),
        "editor.worker.js" => (
            "text/javascript",
            include_bytes!("../../../src/editor/editor.worker.js"),
        ),
        "8f3abbcbc983396e1f13.ttf" => (
            "font/ttf",
            include_bytes!("../../../src/editor/8f3abbcbc983396e1f13.ttf"),
        ),
        "xterm.js" => (
            "text/javascript",
            include_bytes!("../../../src/assets/xterm/xterm.js"),
        ),
        "xterm.css" => (
            "text/css",
            include_bytes!("../../../src/assets/xterm/xterm.css"),
        ),
        "xterm-addon-fit.js" => (
            "text/javascript",
            include_bytes!("../../../src/assets/xterm/xterm-addon-fit.js"),
        ),
        _ => ("text/plain", b"Unavailable offline fixture asset."),
    }
}

fn create_view(
    context: &mut WebContext,
    container: &gtk::Box,
    island: Island,
    state: Rc<RefCell<ProbeState>>,
    smoke: bool,
) -> Result<WebView, Box<dyn std::error::Error>> {
    let page = match island {
        Island::Editor => "editor.html",
        Island::Terminal => "terminal.html",
    };
    let url = format!("mcu-probe://localhost/{page}");
    let allowed_navigation = url.clone();
    let initialization = format!(
        "window.__nativeFixture = {};",
        serde_json::json!({"source": SOURCE, "changed": CHANGED_SOURCE, "smoke": smoke})
    );
    Ok(WebViewBuilder::new_with_web_context(context)
        .with_id(match island {
            Island::Editor => "editor-island",
            Island::Terminal => "terminal-island",
        })
        .with_url(&url)
        .with_initialization_script(&initialization)
        .with_devtools(false)
        .with_navigation_handler(move |target| target == allowed_navigation)
        .with_new_window_req_handler(|_, _| wry::NewWindowResponse::Deny)
        .with_download_started_handler(|_, _| false)
        .with_custom_protocol("mcu-probe".into(), |_, request| {
            let path = asset_path(&request.uri().to_string());
            let (mime, content) = bytes(path.unwrap_or("missing"));
            let mut response = Response::new(Cow::Borrowed(content));
            *response.status_mut() = if path.is_some() {
                wry::http::StatusCode::OK
            } else {
                wry::http::StatusCode::NOT_FOUND
            };
            response
                .headers_mut()
                .insert("content-type", wry::http::HeaderValue::from_static(mime));
            response.headers_mut().insert(
                "content-security-policy",
                wry::http::HeaderValue::from_static(CSP),
            );
            response.headers_mut().insert(
                "cache-control",
                wry::http::HeaderValue::from_static("no-store"),
            );
            response
        })
        .with_ipc_handler(move |request| receive(&state, island, request.body()))
        .build_gtk(container)?)
}

fn receive(state: &RefCell<ProbeState>, island: Island, body: &str) {
    if let Ok(
        message @ (RendererMessage::Error { .. } | RendererMessage::TerminalSnapshot { .. }),
    ) = parse_message(island, body)
    {
        let handler = state.borrow().workspace_events.clone();
        if let Some(handler) = handler {
            handler(&message);
        }
    }
    if let Ok(
        message @ (RendererMessage::WorkspaceReady {}
        | RendererMessage::DocumentChanged { .. }
        | RendererMessage::DocumentsOpened { .. }
        | RendererMessage::WorkspaceSnapshot { .. }),
    ) = parse_message(island, body)
    {
        let handler = state.borrow().workspace_events.clone();
        if matches!(message, RendererMessage::WorkspaceReady {}) {
            state.borrow_mut().workspace_ready = true;
        }
        if let Some(handler) = handler {
            handler(&message);
        }
        return;
    }
    let mut state = state.borrow_mut();
    if state.error.is_some() {
        return;
    }
    match parse_message(island, body) {
        Ok(
            RendererMessage::WorkspaceReady {}
            | RendererMessage::DocumentChanged { .. }
            | RendererMessage::DocumentsOpened { .. }
            | RendererMessage::WorkspaceSnapshot { .. },
        ) => {}
        Err(error) => state.error = Some(error),
        Ok(RendererMessage::Error { message }) => {
            state.error = Some(format!("{island:?}: {message}"))
        }
        Ok(RendererMessage::TerminalSnapshot { text, .. }) => {
            let valid = text.contains("Native Rust fixture") && text.contains("café 日本語 λ");
            state.checks.insert("offline_xterm_unicode".into(), valid);
            state.terminal_ready = valid;
            if !valid {
                state.error = Some("Offline xterm content did not match the fixture.".into());
            }
        }
        Ok(RendererMessage::EditorSnapshot {
            phase,
            model_id,
            text,
            dirty,
            same_model,
            can_undo,
            history_retained,
            selection,
            worker_messages,
            cpp_loaded,
        }) => {
            let identity = state
                .editor
                .as_ref()
                .map(|prior| prior.model_id == model_id)
                .unwrap_or(true);
            if phase != state.expected_phase
                || text != CHANGED_SOURCE
                || !dirty
                || !same_model
                || !can_undo
                || !identity
                || selection != [2, 1, 2, 5]
                || worker_messages == 0
                || !cpp_loaded
            {
                state.error = Some(format!("Monaco fixture state changed at phase {phase}."));
                return;
            }
            for name in [
                "offline_monaco",
                "offline_cpp_chunk",
                "offline_monaco_worker",
                "dirty_model_retained",
                "selection_retained",
            ] {
                state.checks.insert(name.into(), true);
            }
            state.editor = Some(EditorEvidence {
                model_id,
                history_retained,
            });
        }
    }
}

fn fixture_directory() -> Result<PathBuf, Box<dyn std::error::Error>> {
    let repo = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
        .canonicalize()?;
    let root = checked_scratch_root(&repo)?;
    let nanos = SystemTime::now().duration_since(UNIX_EPOCH)?.as_nanos();
    let directory = root.join(format!("native-ui-profile-{}-{nanos}", std::process::id()));
    std::fs::create_dir(&directory)?;
    Ok(directory)
}

pub fn run() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let mut smoke = false;
    let mut workspace_smoke = false;
    let mut theme = Theme::Glass;
    let mut project = None;
    let mut args = args.iter();
    while let Some(flag) = args.next() {
        match flag.as_str() {
            "--smoke" => smoke = true,
            "--workspace-smoke" => workspace_smoke = true,
            "--theme" => theme = Theme::parse(args.next().ok_or("--theme requires a value")?),
            "--project" => {
                project = Some(PathBuf::from(
                    args.next().ok_or("--project requires a folder")?,
                ))
            }
            _ => {
                return Err(
                    "Usage: mcu-native-ui-probe [--smoke] [--theme NAME] [--project FOLDER]".into(),
                )
            }
        }
    }
    if smoke && project.is_some() {
        return Err("The smoke does not read a user project".into());
    }
    let profile = fixture_directory()?;
    if workspace_smoke {
        if smoke || project.is_some() {
            return Err("Workspace verification owns its source fixture".into());
        }
        let root = profile.join("project");
        std::fs::create_dir(&root)?;
        std::fs::write(
            root.join("Sketch.ino"),
            crate::linux_workspace::CHECK_SOURCE,
        )?;
        std::fs::write(root.join("Config.h"), crate::linux_workspace::CHECK_HEADER)?;
        project = Some(root);
    }
    // Process-local GTK/WebKit caches belong to this owned fixture too.
    // No user HOME or shell profile is changed.
    for (name, child) in [
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_CACHE_HOME", "cache"),
        ("XDG_DATA_HOME", "data"),
    ] {
        let directory = profile.join(child);
        std::fs::create_dir(&directory)?;
        std::env::set_var(name, directory);
    }
    std::env::set_var("GIO_USE_VFS", "local");
    let fonts = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../src/fonts/Montserrat/static")
        .canonicalize()?;
    let escaped = fonts
        .to_string_lossy()
        .replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;");
    let fontconfig = profile.join("fonts.conf");
    std::fs::write(&fontconfig, format!("<?xml version=\"1.0\"?><!DOCTYPE fontconfig SYSTEM \"urn:fontconfig:fonts.dtd\"><fontconfig><include ignore_missing=\"yes\">/etc/fonts/fonts.conf</include><dir>{escaped}</dir></fontconfig>"))?;
    std::env::set_var("FONTCONFIG_FILE", &fontconfig);
    let result = run_window(smoke, &profile, theme, project, workspace_smoke);
    // Only this process's atomically created private profile is eligible for removal.
    let cleanup = std::fs::remove_dir_all(&profile);
    result?;
    cleanup?;
    Ok(())
}

fn run_window(
    smoke: bool,
    profile: &std::path::Path,
    theme: Theme,
    project: Option<PathBuf>,
    workspace_smoke: bool,
) -> Result<(), Box<dyn std::error::Error>> {
    gtk::init()?;
    let state = Rc::new(RefCell::new(ProbeState::default()));
    let window = gtk::Window::new(gtk::WindowType::Toplevel);
    window.set_title("MCU Flasher by Naph — Rust development workspace");
    let icon_loader = gtk::gdk_pixbuf::PixbufLoader::new();
    icon_loader.write(include_bytes!("../../../src/assets/mcu_icon.png"))?;
    icon_loader.close()?;
    let application_icon = icon_loader
        .pixbuf()
        .ok_or("Application icon is unavailable")?;
    window.set_icon(Some(&application_icon));
    window.set_default_size(1100, 740);
    if let Some(monitor) = gtk::gdk::Display::default()
        .and_then(|display| display.primary_monitor().or_else(|| display.monitor(0)))
    {
        let area = monitor.workarea();
        window.set_size_request(area.width() / 2, 320);
        window.set_default_size(1100.min(area.width() - 64), 740.min(area.height() - 64));
    }
    let owner = window.as_ptr() as usize;
    let boards = gtk::ComboBoxText::new();
    boards.append_text("Fixture board A");
    boards.append_text("Fixture board B");
    boards.set_active(Some(0));
    let compile = gtk::Button::with_label("Compile fixture");
    compile.set_tooltip_text(Some("Exercises a native Rust callback. Firmware compilation is not enabled in this GUI feasibility fixture."));
    let detach = gtk::Button::with_label("Detach editor");
    detach.set_sensitive(false);
    let editor_box = gtk::Box::new(gtk::Orientation::Vertical, 0);
    let terminal_box = gtk::Box::new(gtk::Orientation::Vertical, 0);
    let status = gtk::Label::new(Some("Loading offline editor and terminal…"));
    status.set_xalign(0.0);
    status.set_margin_start(8);
    status.set_margin_end(8);
    status.set_margin_top(6);
    status.set_margin_bottom(6);
    status.set_ellipsize(gtk::pango::EllipsizeMode::End);
    let workspace = WorkspaceShell::new(
        &window,
        &boards,
        &compile,
        &detach,
        &editor_box,
        &terminal_box,
        &status,
        smoke,
        theme,
        project,
        workspace_smoke,
    )?;
    let split = workspace.split.clone();
    let workspace_events = workspace.clone();
    state.borrow_mut().workspace_events =
        Some(Rc::new(move |message| workspace_events.receive(message)));
    window.add(&workspace.body);
    // Present the native workspace before constructing web engine services.
    window.show_all();
    let mut editor_context = WebContext::new(Some(profile.join("editor")));
    let mut terminal_context = WebContext::new(Some(profile.join("terminal")));
    let editor = Rc::new(create_view(
        &mut editor_context,
        &editor_box,
        Island::Editor,
        state.clone(),
        smoke,
    )?);
    let terminal = Rc::new(create_view(
        &mut terminal_context,
        &terminal_box,
        Island::Terminal,
        state.clone(),
        smoke,
    )?);
    workspace.bind_views(editor.clone(), terminal.clone());
    if workspace_smoke {
        let shell = Rc::downgrade(&workspace);
        glib::timeout_add_local_once(Duration::from_secs(30), move || {
            if let Some(shell) = shell.upgrade() {
                if !shell.check_passed() {
                    gtk::main_quit();
                }
            }
        });
    }
    let editor_widget = editor.webview();
    let terminal_widget = terminal.webview();
    let editor_pointer = editor_widget.as_ptr() as usize;
    let terminal_pointer = terminal_widget.as_ptr() as usize;
    let detached_window = gtk::Window::new(gtk::WindowType::Toplevel);
    detached_window.set_title("Fixture editor — native Rust");
    detached_window.set_icon(Some(&application_icon));
    detached_window.set_default_size(840, 560);
    let detached_box = gtk::Box::new(gtk::Orientation::Vertical, 0);
    detached_window.add(&detached_box);
    let detached = Rc::new(Cell::new(false));
    let transfers = Rc::new(Cell::new(0_u8));
    {
        let state = state.clone();
        boards.connect_changed(move |_| state.borrow_mut().combo_events += 1);
    }
    {
        let state = state.clone();
        let status = status.clone();
        compile.connect_clicked(move |_| {
            state.borrow_mut().compile_events += 1;
            status.set_text(
                "Rust callback passed. Firmware compile remains a separate backend migration gate.",
            );
        });
    }
    {
        let editor = editor.clone();
        let editor_box = editor_box.clone();
        let detached_box = detached_box.clone();
        let detached_window = detached_window.clone();
        let detached = detached.clone();
        let state = state.clone();
        let transfers = transfers.clone();
        detach.connect_clicked(move |button| {
            let target = if detached.get() {
                &editor_box
            } else {
                &detached_box
            };
            if let Err(error) = editor.reparent(target) {
                state.borrow_mut().error = Some(error.to_string());
                return;
            }
            if editor.webview().parent().as_ref() != Some(target.upcast_ref()) {
                state.borrow_mut().error =
                    Some("Editor did not acquire the expected native parent.".into());
                return;
            }
            transfers.set(transfers.get().saturating_add(1));
            detached.set(!detached.get());
            if detached.get() {
                detached_window.show_all();
                button.set_label("Reattach editor");
            } else {
                detached_window.hide();
                button.set_label("Detach editor");
            }
        });
    }
    {
        let detach = detach.clone();
        let detached = detached.clone();
        detached_window.connect_delete_event(move |_, _| {
            if detached.get() {
                detach.emit_clicked();
            }
            glib::Propagation::Stop
        });
    }
    window.connect_delete_event(|_, _| {
        if gtk::main_level() > 0 {
            gtk::main_quit();
        }
        glib::Propagation::Proceed
    });
    window.show_all();
    let started = Instant::now();
    let finished = Rc::new(Cell::new(false));
    let receipt = Rc::new(RefCell::new(None::<Receipt>));
    let baseline_model = Rc::new(RefCell::new(None::<String>));
    let display_backend = gtk::gdk::Display::default()
        .map(|display| display.type_().name().to_owned())
        .unwrap_or_else(|| "unavailable".into());
    let widget_types = vec![
        glib::ObjectExt::type_(&window).name().into(),
        boards.type_().name().into(),
        compile.type_().name().into(),
        status.type_().name().into(),
        split.type_().name().into(),
    ];
    let timer = {
        let workspace = workspace.clone();
        let state = state.clone();
        let window = window.clone();
        let editor = editor.clone();
        let terminal = terminal.clone();
        let boards = boards.clone();
        let compile = compile.clone();
        let detach = detach.clone();
        let status = status.clone();
        let finished = finished.clone();
        let receipt = receipt.clone();
        let baseline_model = baseline_model.clone();
        glib::timeout_add_local(Duration::from_millis(50), move || {
            if finished.get() {
                return glib::ControlFlow::Break;
            }
            let ready = {
                let state = state.borrow();
                if smoke {
                    state.editor.is_some() && state.terminal_ready
                } else {
                    state.workspace_ready
                }
            };
            if ready {
                detach.set_sensitive(true);
            }
            if !smoke {
                if let Some(error) = &state.borrow().error {
                    status.set_text(error);
                    detach.set_sensitive(false);
                } else if ready {
                    if !workspace.is_loading() {
                        status.set_text("Native Rust workspace ready. Edits remain in memory.");
                    }
                    finished.set(true);
                    return glib::ControlFlow::Break;
                }
                return glib::ControlFlow::Continue;
            }
            if started.elapsed() > DEADLINE {
                state.borrow_mut().error = Some("Native GUI smoke deadline expired.".into());
            }
            if ready && state.borrow().error.is_none() {
                let phase = state.borrow().expected_phase;
                if phase == 0 {
                    boards.set_active(Some(1));
                    compile.emit_clicked();
                    let mut state = state.borrow_mut();
                    state.checks.insert(
                        "native_gtk_controls".into(),
                        boards.parent().is_some()
                            && compile.parent().is_some()
                            && status.parent().is_some(),
                    );
                    let combo = state.combo_events == 1;
                    let clicked = state.compile_events == 1;
                    state.checks.insert("native_combo_callback".into(), combo);
                    state
                        .checks
                        .insert("native_button_callback".into(), clicked);
                    state.checks.insert(
                        "separate_webviews".into(),
                        editor_pointer != terminal_pointer && editor.id() != terminal.id(),
                    );
                    *baseline_model.borrow_mut() = state
                        .editor
                        .as_ref()
                        .map(|evidence| evidence.model_id.clone());
                }
                let mut current = state.borrow_mut();
                let model_retained = baseline_model.borrow().as_ref()
                    == current.editor.as_ref().map(|evidence| &evidence.model_id);
                if !model_retained {
                    current.error =
                        Some("Monaco model identity changed across native reparent.".into());
                }
                current.checks.insert(
                    "same_editor_widget".into(),
                    editor.webview().as_ptr() as usize == editor_pointer,
                );
                current.checks.insert(
                    "native_owner_retained".into(),
                    window.as_ptr() as usize == owner,
                );
                current.checks.insert(
                    "terminal_widget_retained".into(),
                    terminal.webview().as_ptr() as usize == terminal_pointer
                        && terminal.webview().parent().as_ref() == Some(terminal_box.upcast_ref()),
                );
                if phase == 7 {
                    let history = current
                        .editor
                        .as_ref()
                        .is_some_and(|evidence| evidence.history_retained);
                    current.checks.insert("undo_redo_retained".into(), history);
                    current.checks.insert(
                        "three_editor_only_reparents".into(),
                        !detached.get() && transfers.get() == 6,
                    );
                    current.checks.insert("typed_ipc_refuses_owner_forgery".into(), parse_message(Island::Terminal, r#"{"kind":"terminal_snapshot","text":"x","columns":80,"rows":10,"owner":"forged"}"#).is_err());
                    let no_python = std::fs::read_to_string("/proc/self/maps")
                        .is_ok_and(|maps| !maps.to_ascii_lowercase().contains("libpython"));
                    current
                        .checks
                        .insert("no_python_application_service".into(), no_python);
                    if !current.checks.values().all(|passed| *passed) {
                        current.error = Some("A native-widget smoke assertion failed.".into());
                    }
                    finished.set(true);
                } else {
                    current.expected_phase += 1;
                    current.editor = None;
                    let next = current.expected_phase;
                    drop(current);
                    if next <= 6 {
                        detach.emit_clicked();
                    }
                    if let Err(error) = editor.evaluate_script(&format!("window.__nativeSnapshot({next}).catch(error => window.ipc.postMessage(JSON.stringify({{kind:'error',message:String(error).slice(0,1024)}})));")) { state_error(&state, error.to_string()); }
                }
            }
            let failed = state.borrow().error.is_some();
            if failed || finished.get() {
                let state = state.borrow();
                *receipt.borrow_mut() = Some(Receipt { schema_version: 1, passed: !failed, mode: "isolated_native_widget_fixture", os: std::env::consts::OS, architecture: std::env::consts::ARCH, display_backend: display_backend.clone(), webview_version: wry::webview_version().unwrap_or_else(|_| "unavailable".into()), widget_types: widget_types.clone(), checks: state.checks.clone(), error: state.error.clone(), limits: "Linux GTK3 receipt; 45-second smoke; 16-KiB IPC; embedded fixed assets; no live source/settings/cloud/hardware or firmware compile; Windows has a separate adapter and receipt; native IME/mixed-monitor qualification remains open" });
                gtk::main_quit();
                glib::ControlFlow::Break
            } else {
                glib::ControlFlow::Continue
            }
        })
    };
    gtk::main();
    if !finished.get() && state.borrow().error.is_none() {
        timer.remove();
    }
    detached_window.close();
    window.close();
    workspace.release_views();
    state.borrow_mut().workspace_events = None;
    if workspace_smoke && !workspace.check_passed() {
        return Err("Native workspace/source verification failed".into());
    }
    if smoke {
        let result = receipt
            .borrow_mut()
            .take()
            .ok_or("Native fixture closed before producing evidence.")?;
        println!("MCU_NATIVE_PROBE={}", serde_json::to_string(&result)?);
        if !result.passed {
            return Err(result
                .error
                .unwrap_or_else(|| "Native smoke failed.".into())
                .into());
        }
    }
    Ok(())
}

fn state_error(state: &RefCell<ProbeState>, error: String) {
    state.borrow_mut().error = Some(error);
}
