//! Win32 controls own the fixture; only Monaco and xterm use WebView2 islands.
//! All HWNDs and COM objects remain on their creating UI thread.
use mcu_native_ui_probe::{
    asset_path, checked_scratch_root, parse_message, windows_navigation_allowed, Island,
    RendererMessage, CHANGED_SOURCE, SOURCE,
};
use raw_window_handle::{
    HandleError, HasWindowHandle, RawWindowHandle, Win32WindowHandle, WindowHandle,
};
use serde::Serialize;
use std::{
    borrow::Cow,
    cell::{Cell, RefCell},
    collections::{BTreeMap, VecDeque},
    ffi::c_void,
    num::NonZeroIsize,
    path::{Path, PathBuf},
    ptr::null_mut,
    rc::Rc,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
use windows_sys::Win32::{
    Foundation::{HWND, LPARAM, LRESULT, POINT, RECT, WPARAM},
    Graphics::Gdi::{
        GetMonitorInfoW, GetStockObject, MonitorFromWindow, ScreenToClient, COLOR_BTNFACE,
        DEFAULT_GUI_FONT, MONITORINFO, MONITOR_DEFAULTTONEAREST,
    },
    System::{
        LibraryLoader::GetModuleHandleW,
        ProcessStatus::{EnumProcessModules, GetModuleFileNameExW},
        Threading::GetCurrentProcess,
    },
    UI::{
        HiDpi::{
            GetDpiForWindow, SetProcessDpiAwarenessContext,
            DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2,
        },
        Input::KeyboardAndMouse::{EnableWindow, ReleaseCapture, SetCapture},
        WindowsAndMessaging::*,
    },
};
use wry::{
    http::Response, WebContext, WebView, WebViewBuilder, WebViewBuilderExtWindows,
    WebViewExtWindows,
};

const COMBO: usize = 101;
const COMPILE: usize = 102;
const DETACH: usize = 103;
const TIMER: usize = 1;
const CLASS: &str = "MCUNativeRustFixture";
const CSP: &str = "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; connect-src 'self'; worker-src 'self' blob:; object-src 'none'; frame-src 'none'; base-uri 'none'; form-action 'none'";

fn wide(value: &str) -> Vec<u16> {
    value.encode_utf16().chain(Some(0)).collect()
}
fn os_error() -> Box<dyn std::error::Error> {
    std::io::Error::last_os_error().into()
}

#[derive(Default)]
struct Events {
    main: Cell<HWND>,
    splitter: Cell<HWND>,
    layout: Cell<bool>,
    tick: Cell<bool>,
    closing: Cell<bool>,
    panic: Cell<bool>,
    dragging: Cell<bool>,
    split_y: Cell<Option<i32>>,
    commands: RefCell<VecDeque<usize>>,
}

impl Events {
    fn command(&self, value: usize) {
        let mut queue = self.commands.borrow_mut();
        if queue.len() >= 32 {
            self.panic.set(true);
        } else {
            queue.push_back(value);
        }
    }
}

// SAFETY: Windows calls this ABI on the creating UI thread. Each non-null
// GWLP_USERDATA is a shared pointer to the stable Box<Events> below; every HWND
// is destroyed before that Box. Only interior cells/short queue borrows are used
// here, never an App/WebView reference. Unwinding cannot cross the FFI boundary.
unsafe extern "system" fn window_proc(hwnd: HWND, msg: u32, w: WPARAM, l: LPARAM) -> LRESULT {
    match std::panic::catch_unwind(|| unsafe { dispatch_window_message(hwnd, msg, w, l) }) {
        Ok(value) => value,
        Err(_) => {
            // SAFETY: Posting quit affects only this UI thread's message loop.
            unsafe {
                PostQuitMessage(1);
            }
            0
        }
    }
}

// SAFETY: Called only by window_proc with Windows-provided message parameters.
unsafe fn dispatch_window_message(hwnd: HWND, msg: u32, w: WPARAM, l: LPARAM) -> LRESULT {
    // SAFETY: WM_NCCREATE supplies a valid CREATESTRUCTW for this callback only.
    // Its lpCreateParams was set to the stable Events Box by NativeWindows::new.
    unsafe {
        if msg == WM_NCCREATE {
            let create = &*(l as *const CREATESTRUCTW);
            SetWindowLongPtrW(hwnd, GWLP_USERDATA, create.lpCreateParams as isize);
            return 1;
        }
        let pointer = GetWindowLongPtrW(hwnd, GWLP_USERDATA) as *const Events;
        if pointer.is_null() {
            return DefWindowProcW(hwnd, msg, w, l);
        }
        let events = &*pointer;
        match msg {
            WM_SIZE => {
                events.layout.set(true);
                0
            }
            WM_TIMER if w == TIMER => {
                events.tick.set(true);
                0
            }
            WM_COMMAND => {
                let id = w & 0xffff;
                let notification = (w >> 16) & 0xffff;
                if (id == COMBO && notification == CBN_SELCHANGE as usize)
                    || (matches!(id, COMPILE | DETACH) && notification == BN_CLICKED as usize)
                {
                    events.command(id);
                }
                0
            }
            WM_CLOSE => {
                if hwnd == events.main.get() {
                    events.closing.set(true);
                    PostQuitMessage(0);
                } else {
                    events.command(DETACH);
                }
                0
            }
            WM_DPICHANGED => {
                // Windows owns this suggested RECT until the callback returns.
                let rect = &*(l as *const RECT);
                SetWindowPos(
                    hwnd,
                    null_mut(),
                    rect.left,
                    rect.top,
                    rect.right - rect.left,
                    rect.bottom - rect.top,
                    SWP_NOZORDER | SWP_NOACTIVATE,
                );
                events.layout.set(true);
                0
            }
            WM_GETMINMAXINFO if hwnd != events.splitter.get() => {
                let mut monitor = MONITORINFO {
                    cbSize: std::mem::size_of::<MONITORINFO>() as u32,
                    ..Default::default()
                };
                if GetMonitorInfoW(
                    MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST),
                    &mut monitor,
                ) != 0
                {
                    let limits = &mut *(l as *mut MINMAXINFO);
                    limits.ptMinTrackSize.x = (monitor.rcWork.right - monitor.rcWork.left) / 2;
                    limits.ptMinTrackSize.y = 320;
                }
                0
            }
            WM_SETCURSOR if hwnd == events.splitter.get() => {
                SetCursor(LoadCursorW(null_mut(), IDC_SIZENS));
                1
            }
            WM_LBUTTONDOWN if hwnd == events.splitter.get() => {
                events.dragging.set(true);
                SetCapture(hwnd);
                0
            }
            WM_LBUTTONUP if events.dragging.replace(false) => {
                ReleaseCapture();
                0
            }
            WM_MOUSEMOVE if events.dragging.get() => {
                let mut point = POINT::default();
                if GetCursorPos(&mut point) != 0
                    && ScreenToClient(events.main.get(), &mut point) != 0
                {
                    events.split_y.set(Some(point.y));
                    events.layout.set(true);
                }
                0
            }
            WM_NCDESTROY => {
                SetWindowLongPtrW(hwnd, GWLP_USERDATA, 0);
                DefWindowProcW(hwnd, msg, w, l)
            }
            _ => DefWindowProcW(hwnd, msg, w, l),
        }
    }
}

struct NativeWindows {
    events: Box<Events>,
    main: HWND,
    detached: HWND,
    label: HWND,
    combo: HWND,
    compile: HWND,
    detach: HWND,
    notice: HWND,
    status: HWND,
    splitter: HWND,
    editor_host: HWND,
    terminal_host: HWND,
    detached_host: HWND,
}

impl NativeWindows {
    fn new() -> Result<Self, Box<dyn std::error::Error>> {
        // SAFETY: This function runs on the sole UI thread before WebView2
        // creation. All strings outlive their synchronous Win32 calls, every
        // child has a parent at creation, and the Events Box never moves.
        unsafe {
            SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);
            let instance = GetModuleHandleW(std::ptr::null());
            let class = wide(CLASS);
            let definition = WNDCLASSW {
                lpfnWndProc: Some(window_proc),
                hInstance: instance,
                lpszClassName: class.as_ptr(),
                hCursor: LoadCursorW(null_mut(), IDC_ARROW),
                hbrBackground: (COLOR_BTNFACE + 1) as _,
                style: CS_HREDRAW | CS_VREDRAW,
                ..Default::default()
            };
            if RegisterClassW(&definition) == 0 {
                return Err(os_error());
            }
            let events = Box::<Events>::default();
            let pointer = (&*events as *const Events).cast::<c_void>();
            let create = |title: &str, parent: HWND| {
                CreateWindowExW(
                    0,
                    class.as_ptr(),
                    wide(title).as_ptr(),
                    WS_OVERLAPPEDWINDOW | WS_CLIPCHILDREN,
                    CW_USEDEFAULT,
                    CW_USEDEFAULT,
                    1100,
                    740,
                    parent,
                    null_mut(),
                    instance,
                    pointer,
                )
            };
            let main = create("MCU Flasher — Native Rust GUI fixture", null_mut());
            if main.is_null() {
                UnregisterClassW(class.as_ptr(), instance);
                return Err(os_error());
            }
            events.main.set(main);
            let mut windows = Self {
                events,
                main,
                detached: null_mut(),
                label: null_mut(),
                combo: null_mut(),
                compile: null_mut(),
                detach: null_mut(),
                notice: null_mut(),
                status: null_mut(),
                splitter: null_mut(),
                editor_host: null_mut(),
                terminal_host: null_mut(),
                detached_host: null_mut(),
            };
            windows.detached = create("Fixture editor — native Rust", main);
            if windows.detached.is_null() {
                return Err(os_error());
            }
            windows.label = windows.child("STATIC", "Controls", 0, 0, main)?;
            windows.combo = windows.child(
                "COMBOBOX",
                "",
                CBS_DROPDOWNLIST as u32 | WS_TABSTOP,
                COMBO,
                main,
            )?;
            windows.compile =
                windows.child("BUTTON", "Compile fixture", WS_TABSTOP, COMPILE, main)?;
            windows.detach = windows.child("BUTTON", "Detach editor", WS_TABSTOP, DETACH, main)?;
            windows.notice = windows.child(
                "STATIC",
                "Isolated fixture · no hardware, live projects or settings",
                0,
                0,
                main,
            )?;
            windows.status =
                windows.child("STATIC", "Loading offline editor and terminal…", 0, 0, main)?;
            windows.editor_host = windows.child("STATIC", "", WS_CLIPCHILDREN, 0, main)?;
            windows.terminal_host = windows.child("STATIC", "", WS_CLIPCHILDREN, 0, main)?;
            windows.detached_host =
                windows.child("STATIC", "", WS_CLIPCHILDREN, 0, windows.detached)?;
            windows.splitter = windows.child(CLASS, "", WS_BORDER, 0, main)?;
            windows.events.splitter.set(windows.splitter);
            for board in ["Fixture board A", "Fixture board B"] {
                SendMessageW(
                    windows.combo,
                    CB_ADDSTRING,
                    0,
                    wide(board).as_ptr() as isize,
                );
            }
            SendMessageW(windows.combo, CB_SETCURSEL, 0, 0);
            EnableWindow(windows.detach, 0);
            if SetTimer(main, TIMER, 50, None) == 0 {
                return Err(os_error());
            }
            windows.events.layout.set(true);
            ShowWindow(main, SW_SHOW);
            Ok(windows)
        }
    }

    fn child(
        &self,
        class: &str,
        text: &str,
        style: u32,
        id: usize,
        parent: HWND,
    ) -> Result<HWND, Box<dyn std::error::Error>> {
        // SAFETY: The native owner and Events Box remain live on the UI thread.
        // CreateWindowExW and WM_SETFONT consume/copy their arguments synchronously.
        unsafe {
            let pointer = (&*self.events as *const Events).cast::<c_void>();
            let hwnd = CreateWindowExW(
                0,
                wide(class).as_ptr(),
                wide(text).as_ptr(),
                WS_CHILD | WS_VISIBLE | WS_CLIPSIBLINGS | style,
                0,
                0,
                1,
                1,
                parent,
                id as _,
                GetModuleHandleW(std::ptr::null()),
                pointer,
            );
            if hwnd.is_null() {
                return Err(os_error());
            }
            SendMessageW(
                hwnd,
                WM_SETFONT,
                GetStockObject(DEFAULT_GUI_FONT) as usize,
                1,
            );
            Ok(hwnd)
        }
    }
}

impl Drop for NativeWindows {
    fn drop(&mut self) {
        // SAFETY: Views are dropped before this owner. Child callbacks can still
        // use Events until both native trees are destroyed; the Box drops last.
        unsafe {
            if !self.main.is_null() {
                KillTimer(self.main, TIMER);
            }
            if !self.detached.is_null() {
                DestroyWindow(self.detached);
            }
            if !self.main.is_null() {
                DestroyWindow(self.main);
            }
            UnregisterClassW(wide(CLASS).as_ptr(), GetModuleHandleW(std::ptr::null()));
        }
    }
}

struct Parent(HWND);
impl HasWindowHandle for Parent {
    fn window_handle(&self) -> Result<WindowHandle<'_>, HandleError> {
        let hwnd = NonZeroIsize::new(self.0 as isize).ok_or(HandleError::Unavailable)?;
        // SAFETY: Parent is used synchronously while its HWND is owned and live
        // on the UI thread. The returned borrow cannot outlive this wrapper.
        Ok(unsafe {
            WindowHandle::borrow_raw(RawWindowHandle::Win32(Win32WindowHandle::new(hwnd)))
        })
    }
}

#[derive(Default)]
struct Evidence {
    error: Option<String>,
    editor: Option<(String, bool)>,
    terminal_ready: bool,
    phase: u8,
    combo_events: u32,
    compile_events: u32,
    checks: BTreeMap<String, bool>,
}
fn receive(state: &RefCell<Evidence>, island: Island, body: &str) {
    let mut state = state.borrow_mut();
    if state.error.is_some() {
        return;
    }
    match parse_message(island, body) {
        Err(error) => state.error = Some(error),
        Ok(RendererMessage::Error { message }) => {
            state.error = Some(format!("{island:?}: {message}"))
        }
        Ok(RendererMessage::TerminalSnapshot { text, .. }) => {
            let valid = text.contains("Native Rust fixture") && text.contains("café 日本語 λ");
            state.checks.insert("offline_xterm_unicode".into(), valid);
            state.terminal_ready = valid;
            if !valid {
                state.error = Some("Offline xterm text differs from the fixture.".into());
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
            if phase != state.phase
                || text != CHANGED_SOURCE
                || !dirty
                || !same_model
                || !can_undo
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
            state.editor = Some((model_id, history_retained));
        }
    }
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
    host: HWND,
    island: Island,
    state: Rc<RefCell<Evidence>>,
) -> Result<WebView, Box<dyn std::error::Error>> {
    let page = if island == Island::Editor {
        "editor.html"
    } else {
        "terminal.html"
    };
    let initialization = format!(
        "window.__nativeFixture = {};",
        serde_json::json!({"source":SOURCE,"changed":CHANGED_SOURCE})
    );
    Ok(WebViewBuilder::new_with_web_context(context)
        .with_id(if island == Island::Editor {
            "editor-island"
        } else {
            "terminal-island"
        })
        .with_url(format!("mcu-probe://localhost/{page}"))
        .with_https_scheme(true)
        .with_devtools(false)
        .with_browser_accelerator_keys(false)
        .with_default_context_menus(false)
        .with_initialization_script(initialization)
        .with_navigation_handler(move |uri| windows_navigation_allowed(island, &uri))
        .with_new_window_req_handler(|_, _| wry::NewWindowResponse::Deny)
        .with_download_started_handler(|_, _| false)
        .with_custom_protocol("mcu-probe".into(), |_, request| {
            let path = asset_path(&request.uri().to_string());
            let (mime, content) = bytes(path.unwrap_or("missing"));
            Response::builder()
                .status(if path.is_some() { 200 } else { 404 })
                .header("content-type", mime)
                .header("content-security-policy", CSP)
                .header("cache-control", "no-store")
                .body(Cow::Borrowed(content))
                .expect("Static response headers")
        })
        .with_ipc_handler(move |request| receive(&state, island, request.body()))
        .build_as_child(&Parent(host))?)
}

struct App {
    // Drop order is deliberate: views, then browser contexts, then HWND trees.
    editor: WebView,
    terminal: WebView,
    _contexts: [WebContext; 2],
    native: NativeWindows,
    evidence: Rc<RefCell<Evidence>>,
    detached: bool,
    transfers: u8,
    baseline_model: Option<String>,
    editor_hwnd: HWND,
    terminal_hwnd: HWND,
    split_ratio: f64,
    body_top: i32,
}

fn client(hwnd: HWND) -> Result<RECT, Box<dyn std::error::Error>> {
    let mut rect = RECT::default();
    // SAFETY: All callers pass an owned, live UI-thread HWND and writable RECT.
    if unsafe { GetClientRect(hwnd, &mut rect) } == 0 {
        return Err(os_error());
    }
    Ok(rect)
}
fn place(hwnd: HWND, x: i32, y: i32, width: i32, height: i32) {
    // SAFETY: UI-thread owned children; dimensions are nonnegative physical pixels.
    unsafe {
        MoveWindow(hwnd, x, y, width.max(1), height.max(1), 1);
    }
}
fn view_bounds(view: &WebView, host: HWND) -> Result<(), Box<dyn std::error::Error>> {
    let bounds = client(host)?;
    view.set_bounds(wry::Rect {
        position: wry::dpi::PhysicalPosition::new(0, 0).into(),
        size: wry::dpi::PhysicalSize::new(bounds.right.max(1) as u32, bounds.bottom.max(1) as u32)
            .into(),
    })?;
    Ok(())
}
fn class_name(hwnd: HWND) -> String {
    let mut buffer = [0_u16; 128];
    // SAFETY: Fixed writable UTF-16 buffer; HWND belongs to the UI thread.
    let size = unsafe { GetClassNameW(hwnd, buffer.as_mut_ptr(), buffer.len() as i32) };
    String::from_utf16_lossy(&buffer[..size.max(0) as usize])
}

impl App {
    fn layout(&mut self) -> Result<(), Box<dyn std::error::Error>> {
        let n = &self.native;
        let rect = client(n.main)?;
        // Win32 client/work-area values are physical; scale only logical constants.
        let scale = unsafe { GetDpiForWindow(n.main) }.max(96) as f64 / 96.0;
        let px = |value: i32| (value as f64 * scale).round() as i32;
        let width = rect.right.max(1);
        let height = rect.bottom.max(1);
        let compact = width < px(720);
        let row = px(34);
        place(n.label, px(8), px(12), px(70), px(24));
        place(
            n.combo,
            px(84),
            px(8),
            (width - px(100)).min(px(240)),
            px(260),
        );
        let action_y = if compact { row + px(8) } else { px(8) };
        place(
            n.compile,
            if compact { px(8) } else { px(340) },
            action_y,
            px(142),
            px(28),
        );
        place(
            n.detach,
            if compact { px(160) } else { width - px(174) },
            action_y,
            px(166),
            px(28),
        );
        let notice_y = if compact { row * 2 } else { row } + px(8);
        place(n.notice, px(8), notice_y, width - px(16), px(22));
        self.body_top = notice_y + px(26);
        let available = (height - self.body_top - px(32)).max(2);
        if let Some(y) = n.events.split_y.take() {
            self.split_ratio = ((y - self.body_top) as f64 / available as f64).clamp(0.2, 0.8);
        }
        let editor_height = if self.detached {
            0
        } else {
            (available as f64 * self.split_ratio) as i32
        };
        place(n.editor_host, 0, self.body_top, width, editor_height);
        place(n.splitter, 0, self.body_top + editor_height, width, px(6));
        place(
            n.terminal_host,
            0,
            self.body_top + editor_height + px(6),
            width,
            available - editor_height - px(6),
        );
        place(n.status, px(8), height - px(26), width - px(16), px(22));
        let detached = client(n.detached)?;
        place(n.detached_host, 0, 0, detached.right, detached.bottom);
        view_bounds(
            &self.editor,
            if self.detached {
                n.detached_host
            } else {
                n.editor_host
            },
        )?;
        view_bounds(&self.terminal, n.terminal_host)
    }

    fn toggle_editor(&mut self) -> Result<(), Box<dyn std::error::Error>> {
        let target = if self.detached {
            self.native.editor_host
        } else {
            self.native.detached_host
        };
        self.editor.reparent(target as isize)?;
        // SAFETY: Wry exposes its live child HWND; its owner/native hosts remain live.
        if unsafe { GetParent(self.editor.hwnd().0) } != target {
            return Err("WebView2 editor acquired the wrong native parent.".into());
        }
        self.detached = !self.detached;
        self.transfers = self.transfers.saturating_add(1);
        // SAFETY: These are owned UI-thread windows. Reparent keeps both views
        // and the original main-window owner alive; only explicit Detach shows it.
        unsafe {
            ShowWindow(
                self.native.detached,
                if self.detached { SW_SHOW } else { SW_HIDE },
            );
            SetWindowTextW(
                self.native.detach,
                wide(if self.detached {
                    "Reattach editor"
                } else {
                    "Detach editor"
                })
                .as_ptr(),
            );
        }
        self.layout()
    }

    fn drain_commands(&mut self) -> Result<(), Box<dyn std::error::Error>> {
        let commands: Vec<_> = self.native.events.commands.borrow_mut().drain(..).collect();
        for command in commands {
            match command {
                COMBO => self.evidence.borrow_mut().combo_events += 1,
                COMPILE => {
                    self.evidence.borrow_mut().compile_events += 1;
                    // SAFETY: Static text is copied synchronously into this live label.
                    unsafe {
                        SetWindowTextW(self.native.status, wide("Rust callback passed. Firmware compilation remains a separate migration gate.").as_ptr());
                    }
                }
                DETACH if self.evidence.borrow().editor.is_some() => self.toggle_editor()?,
                _ => {}
            }
        }
        Ok(())
    }

    fn smoke_step(&mut self) -> Result<bool, Box<dyn std::error::Error>> {
        let ready = {
            let e = self.evidence.borrow();
            e.editor.is_some() && e.terminal_ready
        };
        if !ready {
            return Ok(false);
        }
        let phase = self.evidence.borrow().phase;
        if phase == 0 {
            // SAFETY: Exercise actual Win32 button dispatch and the combo's
            // documented parent notification. Keyboard routing is a separate gate.
            unsafe {
                SendMessageW(self.native.combo, CB_SETCURSEL, 1, 0);
                SendMessageW(
                    self.native.main,
                    WM_COMMAND,
                    COMBO | ((CBN_SELCHANGE as usize) << 16),
                    self.native.combo as isize,
                );
                SendMessageW(self.native.compile, BM_CLICK, 0, 0);
            }
            self.drain_commands()?;
            self.baseline_model = self.evidence.borrow().editor.as_ref().map(|e| e.0.clone());
        }
        {
            let mut e = self.evidence.borrow_mut();
            if e.editor.as_ref().map(|value| &value.0) != self.baseline_model.as_ref() {
                return Err(
                    "Monaco model identity changed across the native editor transfer.".into(),
                );
            }
            // SAFETY: All handles remain owned by App on this UI thread.
            unsafe {
                e.checks.insert(
                    "native_win32_controls".into(),
                    [
                        self.native.combo,
                        self.native.compile,
                        self.native.status,
                        self.native.splitter,
                    ]
                    .iter()
                    .all(|handle| GetParent(*handle) == self.native.main),
                );
                e.checks.insert(
                    "same_editor_widget".into(),
                    self.editor.hwnd().0 == self.editor_hwnd,
                );
                e.checks.insert(
                    "terminal_widget_retained".into(),
                    self.terminal.hwnd().0 == self.terminal_hwnd
                        && GetParent(self.terminal_hwnd) == self.native.terminal_host,
                );
                e.checks.insert(
                    "native_owner_retained".into(),
                    IsWindow(self.native.main) != 0
                        && GetWindowLongPtrW(self.native.main, GWLP_USERDATA)
                            == (&*self.native.events as *const Events) as isize,
                );
            }
            e.checks.insert(
                "separate_webviews".into(),
                self.editor_hwnd != self.terminal_hwnd && self.editor.id() != self.terminal.id(),
            );
            let combo_callback = e.combo_events == 1;
            let button_callback = e.compile_events == 1;
            e.checks
                .insert("native_combo_callback".into(), combo_callback);
            e.checks
                .insert("native_button_callback".into(), button_callback);
            if phase == 7 {
                let history = e.editor.as_ref().is_some_and(|value| value.1);
                e.checks.insert("undo_redo_retained".into(), history);
                e.checks.insert(
                    "three_editor_only_reparents".into(),
                    !self.detached && self.transfers == 6,
                );
                e.checks.insert("typed_ipc_refuses_owner_forgery".into(), parse_message(Island::Terminal, r#"{"kind":"terminal_snapshot","text":"x","columns":80,"rows":10,"owner":"forged"}"#).is_err());
                e.checks
                    .insert("no_python_application_service".into(), no_python_loaded());
                return Ok(true);
            }
        }
        let next = phase + 1;
        // Reparent before requesting the next renderer report. The native button
        // feeds the same bounded command queue as an actual user click.
        if next <= 6 {
            // SAFETY: A synchronous click only queues a native action; no App
            // pointer is published to WndProc, so this cannot alias &mut self.
            unsafe {
                SendMessageW(self.native.detach, BM_CLICK, 0, 0);
            }
            self.drain_commands()?;
        }
        {
            let mut e = self.evidence.borrow_mut();
            e.phase = next;
            e.editor = None;
        }
        self.editor.evaluate_script(&format!("window.__nativeSnapshot({next}).catch(error => window.ipc.postMessage(JSON.stringify({{kind:'error',message:String(error).slice(0,1024)}})));"))?;
        Ok(false)
    }
}

fn no_python_loaded() -> bool {
    let mut modules = [null_mut(); 1024];
    let mut needed = 0;
    // SAFETY: This reads only the current process module list into fixed buffers.
    // Oversized/unavailable metadata fails the check rather than guessing.
    unsafe {
        let process = GetCurrentProcess();
        if EnumProcessModules(
            process,
            modules.as_mut_ptr(),
            std::mem::size_of_val(&modules) as u32,
            &mut needed,
        ) == 0
            || needed as usize > std::mem::size_of_val(&modules)
        {
            return false;
        }
        for module in modules
            .iter()
            .take(needed as usize / std::mem::size_of::<HWND>())
        {
            let mut name = [0_u16; 1024];
            let count = GetModuleFileNameExW(process, *module, name.as_mut_ptr(), name.len() as u32)
                as usize;
            if count == 0 || count >= name.len() {
                return false;
            }
            let path = String::from_utf16_lossy(&name[..count]).to_ascii_lowercase();
            if path
                .rsplit(['\\', '/'])
                .next()
                .is_some_and(|leaf| leaf.starts_with("python") && leaf.ends_with(".dll"))
            {
                return false;
            }
        }
    }
    true
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct Receipt {
    schema_version: u8,
    passed: bool,
    mode: &'static str,
    os: &'static str,
    architecture: &'static str,
    display_backend: &'static str,
    webview_version: String,
    widget_types: Vec<String>,
    checks: BTreeMap<String, bool>,
    error: Option<String>,
    limits: &'static str,
}

fn run_window(smoke: bool, profile: &Path) -> Result<(), Box<dyn std::error::Error>> {
    let started = Instant::now();
    let native = NativeWindows::new()?;
    let evidence = Rc::new(RefCell::new(Evidence::default()));
    let mut editor_context = WebContext::new(Some(profile.join("editor")));
    let mut terminal_context = WebContext::new(Some(profile.join("terminal")));
    let editor = create_view(
        &mut editor_context,
        native.editor_host,
        Island::Editor,
        evidence.clone(),
    )?;
    let terminal = create_view(
        &mut terminal_context,
        native.terminal_host,
        Island::Terminal,
        evidence.clone(),
    )?;
    let editor_hwnd = editor.hwnd().0;
    let terminal_hwnd = terminal.hwnd().0;
    let mut app = App {
        editor,
        terminal,
        _contexts: [editor_context, terminal_context],
        native,
        evidence,
        detached: false,
        transfers: 0,
        baseline_model: None,
        editor_hwnd,
        terminal_hwnd,
        split_ratio: 0.65,
        body_top: 0,
    };
    app.layout()?;
    let mut done = false;
    loop {
        let mut message = MSG::default();
        // SAFETY: This is the UI thread's native message pump, with initialized
        // output storage. Wry/WebView2 receive their messages on the same thread.
        let outcome = unsafe { GetMessageW(&mut message, null_mut(), 0, 0) };
        if outcome < 0 {
            return Err(os_error());
        }
        if outcome == 0 {
            break;
        }
        unsafe {
            TranslateMessage(&message);
            DispatchMessageW(&message);
        }
        app.drain_commands()?;
        if app.native.events.panic.get() {
            app.evidence.borrow_mut().error =
                Some("Native callback queue exceeded its budget.".into());
        }
        if app.native.events.layout.replace(false) {
            app.layout()?;
        }
        if app.native.events.tick.replace(false) {
            let ready = {
                let e = app.evidence.borrow();
                e.editor.is_some() && e.terminal_ready
            };
            // SAFETY: Enabling an owned child does not transfer its ownership.
            unsafe {
                EnableWindow(app.native.detach, ready as i32);
            }
            if smoke {
                if started.elapsed() > Duration::from_secs(45) {
                    app.evidence.borrow_mut().error =
                        Some("Native Win32 smoke deadline expired.".into());
                }
                if app.evidence.borrow().error.is_none() {
                    match app.smoke_step() {
                        Ok(value) => done = value,
                        Err(error) => app.evidence.borrow_mut().error = Some(error.to_string()),
                    }
                }
                if done || app.evidence.borrow().error.is_some() {
                    break;
                }
            }
        }
    }
    if smoke {
        let e = app.evidence.borrow();
        let error = e.error.clone().or_else(|| {
            (!done).then(|| "Native fixture closed before completing smoke checks.".into())
        });
        let passed = done
            && error.is_none()
            && e.checks.len() == 17
            && e.checks.values().all(|value| *value);
        let receipt = Receipt { schema_version: 1, passed, mode: "isolated_native_widget_fixture", os: "windows",
            architecture: std::env::consts::ARCH, display_backend: "Win32", webview_version: wry::webview_version().unwrap_or_else(|_| "unavailable".into()),
            widget_types: [app.native.main, app.native.combo, app.native.compile, app.native.status, app.native.splitter].into_iter().map(class_name).collect(),
            checks: e.checks.clone(), error, limits: "Win32/WebView2 feasibility fixture; 45-second message-loop deadline; 16-KiB typed IPC; no live app services or hardware. Native Windows execution, IME/clipboard/focus, accessibility and mixed-monitor DPI remain separate qualification gates." };
        println!("MCU_NATIVE_PROBE={}", serde_json::to_string(&receipt)?);
        if !passed {
            return Err(receipt
                .error
                .unwrap_or_else(|| "A native Win32 assertion failed.".into())
                .into());
        }
    }
    Ok(())
}

pub fn run() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<_> = std::env::args().skip(1).collect();
    let smoke = match args.as_slice() {
        [] => false,
        [flag] if flag == "--smoke" => true,
        _ => return Err("Usage: mcu-native-ui-probe [--smoke]".into()),
    };
    let repo = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
        .canonicalize()?;
    let scratch = checked_scratch_root(&repo)?;
    let nanos = SystemTime::now().duration_since(UNIX_EPOCH)?.as_nanos();
    let profile = scratch.join(format!(
        "native-ui-win-profile-{}-{nanos}",
        std::process::id()
    ));
    std::fs::create_dir(&profile)?;
    // A process-local inherited override must not redirect WebView2's explicit
    // private data-directory request into a user's normal browser profile.
    std::env::remove_var("WEBVIEW2_USER_DATA_FOLDER");
    let result = run_window(smoke, &profile);
    // WebView2 may release profile files shortly after its controllers close.
    // Cleanup is bounded and restricted to this atomically created directory.
    let mut cleanup = std::fs::remove_dir_all(&profile);
    for _ in 0..20 {
        if cleanup.is_ok() {
            break;
        }
        std::thread::sleep(Duration::from_millis(100));
        cleanup = std::fs::remove_dir_all(&profile);
    }
    if let Err(error) = &cleanup {
        eprintln!(
            "Owned WebView2 fixture profile cleanup failed at {}: {error}",
            profile.display()
        );
    }
    result?;
    cleanup?;
    Ok(())
}
