//! Win32 controls own the fixture; only Monaco and xterm use WebView2 islands.
//! All HWNDs and COM objects remain on their creating UI thread.
use mcu_native_ui_probe::workspace_design::{
    controls_layout, primary_layout, Theme, PRIMARY_ACTIONS, TOOL_TABS,
};
use mcu_native_ui_probe::workspace_icon::{icon_image, APPLICATION_ICON};
use mcu_native_ui_probe::workspace_project::{
    read_project, LoadedProject, CHECK_HEADER, CHECK_SOURCE,
};
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
    ffi::{c_void, OsString},
    num::NonZeroIsize,
    os::windows::ffi::OsStringExt,
    path::{Path, PathBuf},
    process::{Command, Stdio},
    ptr::null_mut,
    rc::Rc,
    sync::{
        atomic::{AtomicUsize, Ordering},
        mpsc, Arc,
    },
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
use windows_sys::Win32::{
    Foundation::{HANDLE, HWND, LPARAM, LRESULT, POINT, RECT, WPARAM},
    Graphics::Gdi::*,
    System::{
        Com::{CoInitializeEx, CoTaskMemFree, CoUninitialize, COINIT_APARTMENTTHREADED},
        LibraryLoader::GetModuleHandleW,
        ProcessStatus::{EnumProcessModules, GetModuleFileNameExW},
        Threading::GetCurrentProcess,
    },
    UI::{
        Controls::{CB_SETCUEBANNER, DRAWITEMSTRUCT, ODS_DISABLED, ODS_FOCUS, ODS_SELECTED},
        HiDpi::{
            GetDpiForWindow, SetProcessDpiAwarenessContext,
            DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2,
        },
        Input::KeyboardAndMouse::{EnableWindow, ReleaseCapture, SetCapture},
        Shell::{
            SHBrowseForFolderW, SHGetPathFromIDListEx, BIF_NEWDIALOGSTYLE, BIF_NONEWFOLDERBUTTON,
            BIF_RETURNONLYFSDIRS, BROWSEINFOW, GPFIDL_DEFAULT,
        },
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
const THEME: usize = 104;
const ACTION: usize = 120;
const PROJECT: usize = ACTION + 8;
// Keep all 256 source IDs disjoint from tool/action IDs.
const SOURCE_TAB: usize = 1000;
const SOURCE_MORE: usize = 403;
const TOOL: usize = 300;
const CLEAR: usize = 400;
const HIDE_EDITOR: usize = 401;
const HIDE_MONITORS: usize = 402;
const BRAND: usize = 500;
const MORE_ACTIONS: usize = 450;
const OPTIONS: usize = 451;
const THEME_ITEM: usize = 600;
const TIMER: usize = 1;
const CLASS: &str = "MCUNativeRustFixture";
const CSP: &str = "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; connect-src 'self'; worker-src 'self' blob:; object-src 'none'; frame-src 'none'; base-uri 'none'; form-action 'none'";

fn wide(value: &str) -> Vec<u16> {
    value.encode_utf16().chain(Some(0)).collect()
}
fn os_error() -> Box<dyn std::error::Error> {
    std::io::Error::last_os_error().into()
}

/// The owning UI thread balances both S_OK and S_FALSE initialization counts.
struct PickerApartment;
impl PickerApartment {
    fn initialize() -> Result<Self, String> {
        // SAFETY: No reserved pointer; the picker lives on this STA UI thread.
        let result = unsafe { CoInitializeEx(std::ptr::null(), COINIT_APARTMENTTHREADED as u32) };
        if result < 0 {
            Err(format!(
                "Folder picker COM initialization failed ({result:#x})."
            ))
        } else {
            Ok(Self)
        }
    }
}
impl Drop for PickerApartment {
    fn drop(&mut self) {
        // SAFETY: This guard follows one successful initialization on this thread.
        unsafe { CoUninitialize() }
    }
}

fn pick_project(owner: HWND) -> Result<Option<PathBuf>, String> {
    let _apartment = PickerApartment::initialize()?;
    let title = wide("Open sketch project in a new MCU Flasher window");
    let mut display = [0u16; 260];
    let info = BROWSEINFOW {
        hwndOwner: owner,
        pszDisplayName: display.as_mut_ptr(),
        lpszTitle: title.as_ptr(),
        ulFlags: BIF_RETURNONLYFSDIRS | BIF_NEWDIALOGSTYLE | BIF_NONEWFOLDERBUTTON,
        ..Default::default()
    };
    // SAFETY: Explicit UI action only. Owner, title, display and STA stay live
    // throughout the modal native call. No callback or renderer path is used.
    let selected = unsafe { SHBrowseForFolderW(&info) };
    if selected.is_null() {
        return Ok(None);
    }
    let mut path = vec![0u16; 32768];
    // SAFETY: Shell returned an owned PIDL; the bounded output is writable.
    // Release the PIDL on both extraction success and failure before COM ends.
    let result = unsafe {
        let result = SHGetPathFromIDListEx(
            selected,
            path.as_mut_ptr(),
            path.len() as u32,
            GPFIDL_DEFAULT,
        );
        CoTaskMemFree(selected.cast());
        result
    };
    if result == 0 {
        return Err("Selected folder has no supported filesystem path.".into());
    }
    let end = path
        .iter()
        .position(|unit| *unit == 0)
        .filter(|end| *end > 0)
        .ok_or("Selected folder path is empty or exceeds the native limit.")?;
    Ok(Some(PathBuf::from(OsString::from_wide(&path[..end]))))
}

fn project_arguments(selected: Option<&Path>, theme: Theme) -> Option<[OsString; 4]> {
    selected.map(|path| {
        [
            "--project".into(),
            path.as_os_str().to_owned(),
            "--theme".into(),
            theme.key().into(),
        ]
    })
}

/// Win32 icon decoding requires DWORD-aligned input; ICO offsets need not align.
fn aligned_icon_storage(image: &[u8]) -> Vec<u32> {
    image
        .chunks(4)
        .map(|chunk| {
            let mut word = [0u8; 4];
            word[..chunk.len()].copy_from_slice(chunk);
            u32::from_ne_bytes(word)
        })
        .collect()
}

struct ApplicationIcons {
    large: HICON,
    small: HICON,
}
impl ApplicationIcons {
    fn new() -> Result<Self, Box<dyn std::error::Error>> {
        let mut icons = Self {
            large: null_mut(),
            small: null_mut(),
        };
        for (target, size) in [(&mut icons.large, 32), (&mut icons.small, 16)] {
            let image = icon_image(APPLICATION_ICON, size)?;
            let storage = aligned_icon_storage(image);
            // SAFETY: Validated bounded embedded ICO payload and exact byte
            // length, copied into DWORD-aligned storage that remains alive
            // throughout this synchronous decode. Only trailing padding lies
            // beyond the byte count and is not passed as part of the payload;
            // no LR_SHARED means each returned HICON is exclusively owned.
            *target = unsafe {
                CreateIconFromResourceEx(
                    storage.as_ptr().cast(),
                    image.len() as u32,
                    1,
                    0x00030000,
                    size as i32,
                    size as i32,
                    0,
                )
            };
            if target.is_null() {
                return Err(os_error());
            }
        }
        Ok(icons)
    }
    fn apply(&self, hwnd: HWND) {
        // SAFETY: This owner retains both icons past every HWND and class using
        // them. No previous per-window icons were installed by this application.
        unsafe {
            SendMessageW(hwnd, WM_SETICON, ICON_BIG as usize, self.large as isize);
            SendMessageW(hwnd, WM_SETICON, ICON_SMALL as usize, self.small as isize);
        }
    }
}
impl Drop for ApplicationIcons {
    fn drop(&mut self) {
        // SAFETY: Unshared handles are released after class/window destruction,
        // or immediately on partial initialization before either uses them.
        unsafe {
            if !self.large.is_null() {
                DestroyIcon(self.large);
            }
            if !self.small.is_null() {
                DestroyIcon(self.small);
            }
        }
    }
}

fn fit_window(hwnd: HWND) {
    // SAFETY: Read only this owned window's nearest native work area, then fit
    // its entire physical frame; no toolkit/DPI factor is applied a second time.
    unsafe {
        let mut monitor = MONITORINFO {
            cbSize: std::mem::size_of::<MONITORINFO>() as u32,
            ..Default::default()
        };
        let mut frame = RECT::default();
        if GetMonitorInfoW(
            MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST),
            &mut monitor,
        ) != 0
            && GetWindowRect(hwnd, &mut frame) != 0
        {
            let work = monitor.rcWork;
            let dpi = GetDpiForWindow(hwnd).max(96) as i32;
            let width = (1100 * dpi / 96).min(work.right - work.left);
            let height = (740 * dpi / 96).min(work.bottom - work.top);
            SetWindowPos(
                hwnd,
                null_mut(),
                frame.left.clamp(work.left, work.right - width),
                frame.top.clamp(work.top, work.bottom - height),
                width,
                height,
                SWP_NOZORDER | SWP_NOACTIVATE,
            );
        }
    }
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
    theme: Cell<Theme>,
    surface: Cell<HBRUSH>,
    tool: Cell<usize>,
    source: Cell<usize>,
    commands: RefCell<VecDeque<usize>>,
}

fn color(theme: Theme, key: &str) -> u32 {
    let rgb = u32::from_str_radix(theme.color(key).trim_start_matches('#'), 16).unwrap_or(0);
    ((rgb >> 16) & 255) | (rgb & 0xff00) | ((rgb & 255) << 16)
}

// SAFETY: The caller provides a valid paint DC and rectangle for this UI thread.
// The two stack vertices remain live for the synchronous static gradient fill.
unsafe fn glass(dc: HDC, rect: RECT, theme: Theme, top: &str, bottom: &str) {
    unsafe {
        let vertex = |x, y, key| {
            let value = theme.rgb(key);
            TRIVERTEX {
                x,
                y,
                Red: ((value >> 16) as u16 & 255) << 8,
                Green: ((value >> 8) as u16 & 255) << 8,
                Blue: (value as u16 & 255) << 8,
                Alpha: 0,
            }
        };
        let vertices = [
            vertex(rect.left, rect.top, top),
            vertex(rect.right, rect.bottom, bottom),
        ];
        let mesh = GRADIENT_RECT {
            UpperLeft: 0,
            LowerRight: 1,
        };
        GradientFill(
            dc,
            vertices.as_ptr(),
            2,
            (&mesh as *const GRADIENT_RECT).cast(),
            1,
            GRADIENT_FILL_RECT_V,
        );
    }
}

// SAFETY: WM_DRAWITEM supplies a borrowed DRAWITEMSTRUCT and a live child HWND.
// GDI selections are restored before all temporary native objects are deleted.
unsafe fn draw_button(item: &DRAWITEMSTRUCT, events: &Events) {
    unsafe {
        let theme = events.theme.get();
        let id = item.CtlID as usize;
        let selected = id == SOURCE_TAB + events.source.get() || id == TOOL + events.tool.get();
        let key = "BG_MID";
        let background = if item.itemState & ODS_SELECTED != 0 {
            "BG_DARKEST"
        } else if selected {
            "BG_LIGHT"
        } else {
            key
        };
        glass(item.hDC, item.rcItem, theme, "BG_LIGHT", background);
        if id != BRAND {
            let rim = CreatePen(
                PS_SOLID,
                1,
                color(theme, if selected { "CYAN" } else { "BORDER" }),
            );
            let previous_pen = SelectObject(item.hDC, rim);
            let previous_brush = SelectObject(item.hDC, GetStockObject(HOLLOW_BRUSH));
            RoundRect(
                item.hDC,
                item.rcItem.left,
                item.rcItem.top,
                item.rcItem.right,
                item.rcItem.bottom,
                6,
                6,
            );
            SelectObject(item.hDC, previous_brush);
            SelectObject(item.hDC, previous_pen);
            DeleteObject(rim);
        }
        let old = SelectObject(item.hDC, SendMessageW(item.hwndItem, WM_GETFONT, 0, 0) as _);
        SetBkMode(item.hDC, TRANSPARENT as i32);
        let foreground = if item.itemState & ODS_DISABLED != 0 {
            "TEXT_DIM"
        } else {
            // The final portable-glass stylesheet overrides the earlier
            // semantic Compile/Upload fills and inks with bright text.
            "TEXT_BRIGHT"
        };
        SetTextColor(
            item.hDC,
            if id == BRAND {
                0x00bf_cf56 // Original fixed #56cfbf logo ink, in COLORREF order.
            } else {
                color(theme, foreground)
            },
        );
        let mut text = [0_u16; 128];
        let length = GetWindowTextW(item.hwndItem, text.as_mut_ptr(), text.len() as i32);
        let mut bounds = item.rcItem;
        if id == BRAND {
            // Original actions/brand.svg linear circuit-chip paths, rendered
            // with native GDI so the application chrome stays native.
            let scale = GetDpiForWindow(item.hwndItem).max(96) as i32;
            let unit = |value| value * scale / 96;
            let pen = CreatePen(PS_SOLID, unit(2).max(1), color(theme, "CYAN"));
            let previous = SelectObject(item.hDC, pen);
            for path in [
                &[
                    (7, 4),
                    (17, 4),
                    (20, 7),
                    (20, 17),
                    (17, 20),
                    (7, 20),
                    (4, 17),
                    (4, 7),
                    (7, 4),
                ][..],
                &[
                    (13, 7),
                    (8, 13),
                    (12, 13),
                    (11, 17),
                    (17, 10),
                    (13, 10),
                    (14, 7),
                ][..],
                &[(8, 1), (8, 4)][..],
                &[(12, 1), (12, 4)][..],
                &[(16, 1), (16, 4)][..],
                &[(8, 20), (8, 23)][..],
                &[(12, 20), (12, 23)][..],
                &[(16, 20), (16, 23)][..],
                &[(1, 8), (4, 8)][..],
                &[(1, 12), (4, 12)][..],
                &[(1, 16), (4, 16)][..],
                &[(20, 8), (23, 8)][..],
                &[(20, 12), (23, 12)][..],
                &[(20, 16), (23, 16)][..],
            ] {
                MoveToEx(
                    item.hDC,
                    unit(path[0].0 + 3),
                    unit(path[0].1 + 3),
                    null_mut(),
                );
                for &(x, y) in &path[1..] {
                    LineTo(item.hDC, unit(x + 3), unit(y + 3));
                }
            }
            SelectObject(item.hDC, previous);
            DeleteObject(pen);
            bounds.left += unit(31);
        }
        DrawTextW(
            item.hDC,
            text.as_ptr(),
            length,
            &mut bounds,
            (if id == BRAND { DT_LEFT } else { DT_CENTER })
                | DT_VCENTER
                | DT_SINGLELINE
                | DT_END_ELLIPSIS
                | DT_NOPREFIX,
        );
        if item.itemState & ODS_FOCUS != 0 {
            InflateRect(&mut bounds, -3, -3);
            DrawFocusRect(item.hDC, &bounds);
        }
        SelectObject(item.hDC, old);
    }
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
            WM_ERASEBKGND => {
                let mut bounds = RECT::default();
                GetClientRect(hwnd, &mut bounds);
                glass(w as HDC, bounds, events.theme.get(), "BG_MID", "BG_DARKEST");
                1
            }
            WM_DRAWITEM if l != 0 => {
                draw_button(&*(l as *const DRAWITEMSTRUCT), events);
                1
            }
            WM_CTLCOLORSTATIC | WM_CTLCOLOREDIT | WM_CTLCOLORLISTBOX => {
                SetTextColor(w as HDC, color(events.theme.get(), "TEXT"));
                SetBkColor(w as HDC, color(events.theme.get(), "BG_DARKEST"));
                events.surface.get() as LRESULT
            }
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
                if (matches!(id, COMBO | THEME) && notification == CBN_SELCHANGE as usize)
                    || (notification == BN_CLICKED as usize && id >= COMPILE)
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
                    let dpi = GetDpiForWindow(hwnd).max(96) as i32;
                    let logical_half_width = limits.ptMinTrackSize.x * 96 / dpi;
                    let minimum = if logical_half_width < 520 { 420 } else { 320 };
                    limits.ptMinTrackSize.y =
                        (minimum * dpi / 96).min(monitor.rcWork.bottom - monitor.rcWork.top);
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
    brand: HWND,
    actions: Vec<HWND>,
    captions: Vec<HWND>,
    selectors: Vec<HWND>,
    workspace: Vec<HWND>,
    source_tabs: Vec<HWND>,
    source_more: HWND,
    tools: Vec<HWND>,
    tool_heading: HWND,
    tool_output: HWND,
    clear: HWND,
    font: HFONT,
    font_resource: HANDLE,
    font_dpi: u32,
    more_actions: HWND,
    options: HWND,
    icons: ApplicationIcons,
}

impl NativeWindows {
    fn new(smoke: bool, workspace_smoke: bool) -> Result<Self, Box<dyn std::error::Error>> {
        // SAFETY: This function runs on the sole UI thread before WebView2
        // creation. All strings outlive their synchronous Win32 calls, every
        // child has a parent at creation, and the Events Box never moves.
        unsafe {
            SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);
            let instance = GetModuleHandleW(std::ptr::null());
            let class = wide(CLASS);
            let icons = ApplicationIcons::new()?;
            let definition = WNDCLASSW {
                lpfnWndProc: Some(window_proc),
                hInstance: instance,
                lpszClassName: class.as_ptr(),
                hCursor: LoadCursorW(null_mut(), IDC_ARROW),
                hIcon: icons.large,
                style: CS_HREDRAW | CS_VREDRAW,
                ..Default::default()
            };
            if RegisterClassW(&definition) == 0 {
                return Err(os_error());
            }
            let events = Box::<Events>::default();
            events.tool.set(if smoke { 5 } else { 0 });
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
            let main = create(
                "MCU Flasher by Naph — Native Rust workspace preview",
                null_mut(),
            );
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
                brand: null_mut(),
                actions: Vec::new(),
                captions: Vec::new(),
                selectors: Vec::new(),
                workspace: Vec::new(),
                source_tabs: Vec::new(),
                source_more: null_mut(),
                tools: Vec::new(),
                tool_heading: null_mut(),
                tool_output: null_mut(),
                clear: null_mut(),
                font: null_mut(),
                font_resource: null_mut(),
                font_dpi: 0,
                more_actions: null_mut(),
                options: null_mut(),
                icons,
            };
            let font_bytes =
                include_bytes!("../../../src/fonts/Montserrat/static/Montserrat-SemiBold.ttf");
            let mut count = 0;
            windows.font_resource = AddFontMemResourceEx(
                font_bytes.as_ptr().cast(),
                font_bytes.len() as u32,
                std::ptr::null(),
                &raw mut count,
            );
            windows.refresh_font();
            windows
                .events
                .surface
                .set(CreateSolidBrush(color(Theme::Glass, "BG_DARKEST")));
            windows.detached = create("Code Editor — MCU Flasher by Naph", main);
            if windows.detached.is_null() {
                return Err(os_error());
            }
            windows.icons.apply(main);
            windows.icons.apply(windows.detached);
            windows.brand = windows.child(
                "BUTTON",
                "MCU Flasher by Naph",
                BS_OWNERDRAW as u32,
                BRAND,
                main,
            )?;
            windows.label = windows.child("STATIC", "Board", 0, 0, main)?;
            windows.combo = windows.child(
                "COMBOBOX",
                "",
                CBS_DROPDOWNLIST as u32 | WS_TABSTOP,
                COMBO,
                main,
            )?;
            for (index, label) in PRIMARY_ACTIONS.iter().enumerate() {
                let id = if index == 0 { COMPILE } else { ACTION + index };
                let button =
                    windows.child("BUTTON", label, WS_TABSTOP | BS_OWNERDRAW as u32, id, main)?;
                if index == 0 {
                    windows.compile = button;
                }
                // Only the original isolated smoke exposes its callback probe.
                // Only Project has a real manual implementation. Fixtures must
                // never enter the native chooser or launch independent projects.
                EnableWindow(
                    button,
                    ((smoke && index == 0) || (!smoke && !workspace_smoke && index == 8)) as i32,
                );
                windows.actions.push(button);
            }
            windows.detach = windows.child(
                "BUTTON",
                "Detach Editor",
                WS_TABSTOP | BS_OWNERDRAW as u32,
                DETACH,
                main,
            )?;
            windows.more_actions = windows.child(
                "BUTTON",
                "Actions",
                WS_TABSTOP | BS_OWNERDRAW as u32,
                MORE_ACTIONS,
                main,
            )?;
            windows.options = windows.child(
                "BUTTON",
                "Options",
                WS_TABSTOP | BS_OWNERDRAW as u32,
                OPTIONS,
                main,
            )?;
            for label in ["Serial port", "Upload speed", "Workspace", "Theme"] {
                windows
                    .captions
                    .push(windows.child("STATIC", label, 0, 0, main)?);
            }
            for (index, choices) in [
                vec!["No device connected"],
                vec!["115200", "230400", "460800", "512000", "921600"],
                vec!["Glass", "Frosted Light", "Solarized Dark"],
            ]
            .iter()
            .enumerate()
            {
                let selector = windows.child(
                    "COMBOBOX",
                    "",
                    CBS_DROPDOWNLIST as u32 | WS_TABSTOP,
                    if index == 2 { THEME } else { 110 + index },
                    main,
                )?;
                for choice in choices {
                    SendMessageW(selector, CB_ADDSTRING, 0, wide(choice).as_ptr() as isize);
                }
                SendMessageW(selector, CB_SETCURSEL, if index == 1 { 2 } else { 0 }, 0);
                windows.selectors.push(selector);
            }
            for (index, label) in [
                "Timestamps",
                "Skip Compile",
                "Hide Editor",
                "Hide Monitors",
                "Settings",
                "AI Assistant",
            ]
            .iter()
            .enumerate()
            {
                let id = match index {
                    2 => HIDE_EDITOR,
                    3 => HIDE_MONITORS,
                    _ => 410 + index,
                };
                let style = if index < 2 {
                    BS_AUTOCHECKBOX as u32
                } else {
                    BS_OWNERDRAW as u32
                };
                windows.workspace.push(windows.child(
                    "BUTTON",
                    label,
                    style | WS_TABSTOP,
                    id,
                    main,
                )?);
                if !(2..4).contains(&index) {
                    EnableWindow(*windows.workspace.last().expect("created child"), 0);
                }
            }
            windows.source_tabs.push(windows.child(
                "BUTTON",
                if smoke {
                    "fixture.ino  •"
                } else {
                    "Untitled.ino"
                },
                WS_TABSTOP | BS_OWNERDRAW as u32,
                SOURCE_TAB,
                main,
            )?);
            windows.source_more = windows.child(
                "BUTTON",
                "Sources…",
                WS_TABSTOP | BS_OWNERDRAW as u32,
                SOURCE_MORE,
                main,
            )?;
            for (index, label) in TOOL_TABS.iter().enumerate() {
                windows.tools.push(windows.child(
                    "BUTTON",
                    label,
                    WS_TABSTOP | BS_OWNERDRAW as u32,
                    TOOL + index,
                    main,
                )?);
            }
            windows.tool_heading = windows.child(
                "STATIC",
                if smoke { "Terminal" } else { "Build Console" },
                0,
                0,
                main,
            )?;
            windows.tool_output = windows.child("EDIT", "Native Rust workspace preview\r\n\r\nSelect a tool tab to inspect its preview.\r\nSource edits stay in memory. Device, settings and cloud operations are unavailable.", ES_MULTILINE as u32 | ES_READONLY as u32 | WS_VSCROLL, 0, main)?;
            windows.clear = windows.child(
                "BUTTON",
                "Clear",
                WS_TABSTOP | BS_OWNERDRAW as u32,
                CLEAR,
                main,
            )?;
            windows.notice = windows.child(
                "STATIC",
                "Source edits stay in memory · device operations unavailable",
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
            SendMessageW(
                windows.combo,
                CB_SETCURSEL,
                if smoke { 0 } else { usize::MAX },
                0,
            );
            EnableWindow(windows.detach, 0);
            SendMessageW(
                windows.combo,
                CB_SETCUEBANNER,
                0,
                wide("Select Board").as_ptr() as isize,
            );
            if SetTimer(main, TIMER, 50, None) == 0 {
                return Err(os_error());
            }
            windows.events.layout.set(true);
            fit_window(main);
            fit_window(windows.detached);
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
            SendMessageW(hwnd, WM_SETFONT, self.font as usize, 1);
            Ok(hwnd)
        }
    }

    fn refresh_font(&mut self) {
        // SAFETY: The embedded font is process-private. New fonts replace the
        // old selection in all live child controls before the old HFONT is freed.
        unsafe {
            let dpi = GetDpiForWindow(self.main).max(96);
            if self.font_dpi == dpi {
                return;
            }
            let size = -(12 * dpi as i32 / 96);
            let font = CreateFontW(
                size,
                0,
                0,
                0,
                600,
                0,
                0,
                0,
                DEFAULT_CHARSET as u32,
                0,
                0,
                CLEARTYPE_QUALITY as u32,
                0,
                wide(if self.font_resource.is_null() {
                    "Segoe UI"
                } else {
                    "Montserrat SemiBold"
                })
                .as_ptr(),
            );
            if font.is_null() {
                return;
            }
            for parent in [self.main, self.detached] {
                if parent.is_null() {
                    continue;
                }
                let mut child = GetWindow(parent, GW_CHILD);
                while !child.is_null() {
                    SendMessageW(child, WM_SETFONT, font as usize, 1);
                    child = GetWindow(child, GW_HWNDNEXT);
                }
            }
            if !self.font.is_null() {
                DeleteObject(self.font);
            }
            self.font = font;
            self.font_dpi = dpi;
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
            DeleteObject(self.events.surface.get());
            DeleteObject(self.font);
            if !self.font_resource.is_null() {
                RemoveFontMemResourceEx(self.font_resource);
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

#[derive(Clone)]
struct SourceTab {
    id: String,
    name: String,
    revision: u64,
    dirty: bool,
}
impl SourceTab {
    fn new(id: String, name: String) -> Self {
        Self {
            id,
            name,
            revision: 0,
            dirty: false,
        }
    }
    fn label(&self) -> String {
        format!("{}{}", self.name, if self.dirty { " *" } else { "" })
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
    workspace_ready: bool,
    sources: Vec<SourceTab>,
    source_generation: u64,
    pending_tabs: Option<Vec<(String, String)>>,
    workspace_snapshot: Option<RendererMessage>,
}
fn receive(state: &RefCell<Evidence>, island: Island, body: &str) {
    let mut state = state.borrow_mut();
    match parse_message(island, body) {
        Ok(RendererMessage::DocumentsOpened { documents }) => {
            if let Some(entries) = state.pending_tabs.take() {
                if entries.iter().map(|(id, _)| id).eq(documents.iter()) {
                    state.sources = entries
                        .into_iter()
                        .map(|(id, name)| SourceTab::new(id, name))
                        .collect();
                    state.source_generation += 1;
                } else {
                    state.error = Some(
                        "The editor did not acknowledge the exact ordered native source IDs."
                            .into(),
                    );
                }
            }
        }
        Ok(message @ RendererMessage::WorkspaceSnapshot { .. }) => {
            if state.workspace_snapshot.replace(message).is_some() {
                state.error =
                    Some("The workspace snapshot queue exceeded its one-report budget.".into());
            }
        }
        Ok(RendererMessage::WorkspaceReady {}) => {
            state.workspace_ready = true;
            if state.sources.is_empty() {
                state
                    .sources
                    .push(SourceTab::new("untitled".into(), "Untitled.ino".into()));
            }
        }
        Ok(RendererMessage::DocumentChanged {
            document_id,
            edit_revision,
            dirty,
        }) => {
            if let Some(tab) = state.sources.iter_mut().find(|tab| tab.id == document_id) {
                if edit_revision > tab.revision {
                    tab.revision = edit_revision;
                    tab.dirty = dirty;
                }
            }
        }
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
    if state.error.is_some() {
        state.pending_tabs = None;
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
    smoke: bool,
) -> Result<WebView, Box<dyn std::error::Error>> {
    let page = if island == Island::Editor {
        "editor.html"
    } else {
        "terminal.html"
    };
    let initialization = format!(
        "window.__nativeFixture = {};",
        serde_json::json!({"source":SOURCE,"changed":CHANGED_SOURCE,"smoke":smoke})
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
    editor_visible: bool,
    monitors_visible: bool,
    workspace_initialized: bool,
    terminal_initialized: bool,
    source_labels: Vec<String>,
    source_generation: u64,
    project_path: Option<PathBuf>,
    project_worker: Option<(mpsc::Receiver<Result<LoadedProject, String>>, Instant)>,
    project_deadline: Option<Instant>,
    project_launch: Option<mpsc::Receiver<Result<(), String>>>,
    project_children: Arc<AtomicUsize>,
    workspace_smoke: bool,
    workspace_phase: u8,
    workspace_waiting: bool,
    workspace_checks: BTreeMap<String, bool>,
    smoke: bool,
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
fn show(hwnd: HWND, visible: bool) {
    // SAFETY: Callers provide owned, live native child windows on the UI thread.
    unsafe {
        ShowWindow(hwnd, if visible { SW_SHOW } else { SW_HIDE });
    }
}
fn text_width(owner: HWND, font: HFONT, text: &str) -> i32 {
    // SAFETY: The temporary DC belongs to this window; selected font and DC are
    // restored/released before returning the native physical text measurement.
    unsafe {
        let dc = GetDC(owner);
        let previous = SelectObject(dc, font);
        let mut bounds = RECT::default();
        let text = wide(text);
        DrawTextW(
            dc,
            text.as_ptr(),
            text.len() as i32 - 1,
            &mut bounds,
            DT_CALCRECT | DT_SINGLELINE,
        );
        SelectObject(dc, previous);
        ReleaseDC(owner, dc);
        bounds.right - bounds.left
    }
}

fn popup(owner: HWND, anchor: HWND, entries: &[(usize, &str, bool)]) -> Option<usize> {
    // SAFETY: The temporary menu owns copied strings. Only the explicit user
    // action enters its native modal loop, and it is destroyed before returning.
    unsafe {
        let menu = CreatePopupMenu();
        if menu.is_null() {
            return None;
        }
        for (id, label, enabled) in entries {
            AppendMenuW(
                menu,
                if *enabled {
                    MF_STRING
                } else {
                    MF_STRING | MF_GRAYED
                },
                *id,
                wide(label).as_ptr(),
            );
        }
        let mut frame = RECT::default();
        GetWindowRect(anchor, &mut frame);
        let selected = TrackPopupMenuEx(
            menu,
            TPM_RETURNCMD | TPM_NONOTIFY,
            frame.left,
            frame.bottom,
            owner,
            std::ptr::null(),
        );
        DestroyMenu(menu);
        (selected != 0).then_some(selected as usize)
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
    fn open_project(&mut self) {
        if self.smoke
            || self.workspace_smoke
            || self.project_launch.is_some()
            || self.native.events.closing.get()
        {
            return;
        }
        if self.project_children.load(Ordering::Relaxed) >= 16 {
            self.status("Close an independent project before opening another (16-window limit).");
            return;
        }
        let selected = match pick_project(self.native.main) {
            Ok(selected) => selected,
            Err(error) => {
                self.status(&error);
                return;
            }
        };
        let Some(arguments) =
            project_arguments(selected.as_deref(), self.native.events.theme.get())
        else {
            return;
        };
        // Recheck after the modal picker pumps native close events.
        if self.native.events.closing.get() {
            return;
        }
        let (sender, receiver) = mpsc::sync_channel(1);
        let children = self.project_children.clone();
        children.fetch_add(1, Ordering::Relaxed);
        let worker = std::thread::Builder::new()
            .name("native-project-window".into())
            .spawn(move || {
                // Spawn and wait stay off the GUI thread. Literal OsString arguments
                // never pass through a shell. Closing the parent never kills this
                // independent child; this detached worker only reaps its own child.
                let launched = std::env::current_exe().and_then(|exe| {
                    Command::new(exe)
                        .args(arguments)
                        .stdin(Stdio::null())
                        .stdout(Stdio::null())
                        .stderr(Stdio::null())
                        .spawn()
                });
                match launched {
                    Ok(mut child) => {
                        let _ = sender.send(Ok(()));
                        let _ = child.wait();
                    }
                    Err(error) => {
                        let _ =
                            sender.send(Err(format!("Project window could not start: {error}")));
                    }
                }
                children.fetch_sub(1, Ordering::Relaxed);
            });
        if worker.is_err() {
            self.project_children.fetch_sub(1, Ordering::Relaxed);
            self.status("Project worker could not start.");
        } else {
            self.project_launch = Some(receiver);
            self.status("Opening the selected project in an independent Rust window…");
        }
    }

    fn poll_project_launch(&mut self) {
        let Some(receiver) = &self.project_launch else {
            return;
        };
        match receiver.try_recv() {
            Ok(result) => {
                self.project_launch = None;
                match result {
                    Ok(()) => self.status(
                        "Independent project window started. Current editor buffers are retained.",
                    ),
                    Err(error) => self.status(&error),
                }
            }
            Err(mpsc::TryRecvError::Disconnected) => {
                self.project_launch = None;
                self.status("Project worker ended before reporting its launch result.");
            }
            Err(mpsc::TryRecvError::Empty) => {}
        }
    }

    fn status(&self, message: &str) {
        // SAFETY: This owned child copies the string synchronously on the UI thread.
        unsafe {
            SetWindowTextW(self.native.status, wide(message).as_ptr());
        }
    }

    fn select_source(&self, index: usize) -> Result<(), Box<dyn std::error::Error>> {
        let id = self
            .evidence
            .borrow()
            .sources
            .get(index)
            .map(|tab| tab.id.clone());
        if let Some(id) = id {
            self.editor.evaluate_script(&format!(
                "window.__mcuSelectDocument({});",
                serde_json::to_string(&id)?
            ))?;
            self.native.events.source.set(index);
            // SAFETY: Each source child is retained by the native owner.
            unsafe {
                for tab in &self.native.source_tabs {
                    InvalidateRect(*tab, std::ptr::null(), 1);
                }
            }
        }
        Ok(())
    }

    fn sync_sources(&mut self) -> Result<(), Box<dyn std::error::Error>> {
        let (generation, tabs) = {
            let evidence = self.evidence.borrow();
            (evidence.source_generation, evidence.sources.clone())
        };
        if generation != self.source_generation {
            // Allocate the complete replacement before retiring any old control.
            // These IDs are derived only from the native reader's acknowledged order.
            let mut controls = Vec::new();
            for (index, tab) in tabs.iter().enumerate() {
                match self.native.child(
                    "BUTTON",
                    &tab.label(),
                    WS_TABSTOP | BS_OWNERDRAW as u32,
                    SOURCE_TAB + index,
                    self.native.main,
                ) {
                    Ok(button) => controls.push(button),
                    Err(error) => {
                        // SAFETY: Only children created by this attempt are destroyed.
                        unsafe {
                            for button in controls {
                                DestroyWindow(button);
                            }
                        }
                        return Err(error);
                    }
                }
            }
            // SAFETY: Views are attached to their separate editor/terminal hosts;
            // source buttons own no WebView references or editor model resources.
            unsafe {
                for button in self.native.source_tabs.drain(..) {
                    DestroyWindow(button);
                }
            }
            self.native.source_tabs = controls;
            self.source_generation = generation;
            self.source_labels = tabs.iter().map(SourceTab::label).collect();
            self.select_source(0)?;
            self.project_deadline = None;
            self.status("Project opened. Source files are unchanged; edits stay in memory.");
            self.layout()?;
        }
        let mut changed = false;
        for (index, tab) in tabs.iter().enumerate() {
            let label = tab.label();
            if self.source_labels.get(index) != Some(&label) {
                if let Some(button) = self.native.source_tabs.get(index) {
                    // SAFETY: This child is live on the UI thread; Win32 copies its caption.
                    unsafe {
                        SetWindowTextW(*button, wide(&label).as_ptr());
                    }
                    if index < self.source_labels.len() {
                        self.source_labels[index] = label;
                    }
                    changed = true;
                }
            }
        }
        if changed {
            self.layout()?;
        }
        Ok(())
    }

    fn start_project_read(&mut self, selected: PathBuf) {
        if self.project_deadline.is_some() || self.project_worker.is_some() {
            return;
        }
        let (sender, receiver) = mpsc::sync_channel(1);
        // Only owned snapshot data crosses threads. No HWND, COM object or
        // live service/ownership handle leaves the native UI thread.
        std::thread::spawn(move || {
            let _ = sender.send(read_project(&selected));
        });
        let now = Instant::now();
        self.project_worker = Some((receiver, now));
        self.project_deadline = Some(now);
        self.status("Reading project sources…");
    }

    fn poll_project(&mut self) -> Result<(), Box<dyn std::error::Error>> {
        let outcome = self.project_worker.as_ref().map(|(receiver, started)| {
            if started.elapsed() > Duration::from_secs(20) {
                Ok(Err(
                    "The project read timed out. Existing editor buffers are unchanged.".into(),
                ))
            } else {
                receiver.try_recv()
            }
        });
        match outcome {
            Some(Ok(result)) => {
                self.project_worker = None;
                match result {
                    Ok((entries, script)) => {
                        self.evidence.borrow_mut().pending_tabs = Some(entries);
                        if let Err(error) = self.editor.evaluate_script(&script) {
                            self.evidence.borrow_mut().pending_tabs = None;
                            self.project_deadline = None;
                            self.evidence.borrow_mut().error = Some(error.to_string());
                        }
                    }
                    Err(error) => {
                        self.project_deadline = None;
                        self.evidence.borrow_mut().error = Some(error);
                    }
                }
            }
            Some(Err(mpsc::TryRecvError::Disconnected)) => {
                self.project_worker = None;
                self.project_deadline = None;
                self.evidence.borrow_mut().error =
                    Some("The project reader stopped before completing.".into());
            }
            _ => {}
        }
        if self
            .project_deadline
            .is_some_and(|started| started.elapsed() > Duration::from_secs(25))
        {
            self.project_deadline = None;
            self.evidence.borrow_mut().pending_tabs = None;
            self.evidence.borrow_mut().error = Some(
                "The editor did not acknowledge the project load. Existing buffers are retained."
                    .into(),
            );
        }
        if self.evidence.borrow().error.is_some() {
            self.evidence.borrow_mut().pending_tabs = None;
            self.project_worker = None;
            self.project_deadline = None;
            if !self.smoke && !self.workspace_smoke {
                if let Some(error) = self.evidence.borrow_mut().error.take() {
                    self.status(&error);
                }
            }
        }
        Ok(())
    }

    fn workspace_smoke_step(&mut self) -> Result<bool, Box<dyn std::error::Error>> {
        if self.source_generation == 0 || !self.terminal_initialized {
            return Ok(false);
        }
        let snapshot = self.evidence.borrow_mut().workspace_snapshot.take();
        if let Some(RendererMessage::WorkspaceSnapshot {
            phase,
            documents,
            same_models,
            dirty_retained,
            history_retained,
            selection_retained,
            theme,
            background,
        }) = snapshot
        {
            let palette = self.native.events.theme.get();
            let rgb = palette.rgb("BG_DARKEST");
            let expected = format!(
                "rgb({}, {}, {})",
                (rgb >> 16) & 255,
                (rgb >> 8) & 255,
                rgb & 255
            );
            self.workspace_checks.insert(
                format!("phase_{phase}_models"),
                self.workspace_waiting
                    && phase == self.workspace_phase
                    && documents == 2
                    && same_models,
            );
            self.workspace_checks.insert(
                format!("phase_{phase}_dirty_selection"),
                dirty_retained
                    && selection_retained
                    && self
                        .evidence
                        .borrow()
                        .sources
                        .iter()
                        .enumerate()
                        .any(|(index, tab)| {
                            tab.dirty
                                && tab.revision > 0
                                && self.source_labels.get(index) == Some(&tab.label())
                        }),
            );
            self.workspace_checks.insert(
                format!("phase_{phase}_theme"),
                theme == palette.key() && background == expected,
            );
            if phase == 3 {
                self.workspace_checks
                    .insert("undo_redo".into(), history_retained);
            }
            if !self.workspace_checks.values().all(|value| *value) {
                return Err("A native Win32 workspace assertion failed.".into());
            }
            if phase == 3 {
                self.workspace_checks
                    .insert("native_tools".into(), self.native.tools.len() == 7);
                self.workspace_checks.insert(
                    "native_source_tabs".into(),
                    self.native.source_tabs.len() == 2,
                );
                // SAFETY: Read only owned native children, never renderer HWNDs.
                let owned = unsafe {
                    self.native.source_tabs.iter().all(|button| {
                        GetParent(*button) == self.native.main
                            && class_name(*button).eq_ignore_ascii_case("Button")
                    })
                };
                self.workspace_checks
                    .insert("native_owned_source_controls".into(), owned);
                self.workspace_checks
                    .insert("no_python_application_service".into(), no_python_loaded());
                let unchanged = self.project_path.as_ref().is_some_and(|root| {
                    std::fs::read(root.join("Sketch.ino"))
                        .is_ok_and(|bytes| bytes == CHECK_SOURCE.as_bytes())
                        && std::fs::read(root.join("Config.h"))
                            .is_ok_and(|bytes| bytes == CHECK_HEADER.as_bytes())
                });
                self.workspace_checks
                    .insert("source_bytes_unchanged".into(), unchanged);
                return Ok(true);
            }
            self.workspace_phase += 1;
            self.workspace_waiting = false;
        }
        if !self.workspace_waiting {
            let selection = [0, 1, 2, 0][usize::from(self.workspace_phase)];
            // Exercise the same native theme and source-button dispatch as users.
            // SAFETY: Both messages address owned UI-thread controls.
            unsafe {
                SendMessageW(self.native.selectors[2], CB_SETCURSEL, selection, 0);
                SendMessageW(
                    self.native.source_tabs[usize::from(self.workspace_phase) % 2],
                    BM_CLICK,
                    0,
                    0,
                );
            }
            self.native.events.command(THEME);
            self.drain_commands()?;
            self.workspace_waiting = true;
            self.editor.evaluate_script(&format!("window.__mcuWorkspaceSnapshot({}).catch(error => window.ipc.postMessage(JSON.stringify({{kind:'error',message:String(error).slice(0,1024)}})));", self.workspace_phase))?;
        }
        Ok(false)
    }

    fn layout(&mut self) -> Result<(), Box<dyn std::error::Error>> {
        self.native.refresh_font();
        let n = &self.native;
        let rect = client(n.main)?;
        // Win32 client/work-area values are physical; scale only logical constants.
        let scale = unsafe { GetDpiForWindow(n.main) }.max(96) as f64 / 96.0;
        let px = |value: i32| (value as f64 * scale).round() as i32;
        let width = rect.right.max(1);
        let height = rect.bottom.max(1);
        let logical_width = (width as f64 / scale) as u32;
        let logical_height = (height as f64 / scale) as u32;
        let primary = primary_layout(logical_width, 226, 880);
        let controls = controls_layout(logical_width, logical_height, [232, 195, 120, 100], 232);
        let short = height < px(620);
        let compact_controls = controls.compact_actions || short;
        let compact_actions = primary.compact || short;
        let brand_width = text_width(n.main, n.font, primary.logo) + px(42);
        // SAFETY: This only changes the native caption; source models stay live.
        unsafe {
            SetWindowTextW(n.brand, wide(primary.logo).as_ptr());
        }
        let (mut x, mut y) = (brand_width + px(12), px(7));
        place(n.brand, px(8), y, brand_width, px(29));
        let mut actions = Vec::new();
        for (index, (button, label)) in n.actions.iter().zip(PRIMARY_ACTIONS).enumerate() {
            let visible = !compact_actions || !(2..8).contains(&index);
            show(*button, visible);
            if visible {
                actions.push((*button, *label));
            }
        }
        show(n.more_actions, compact_actions);
        if compact_actions {
            actions.insert(2, (n.more_actions, "Actions"));
        }
        for (button, label) in actions {
            let size = (text_width(n.main, n.font, label) + px(22)).max(px(62));
            if x + size > width - px(8) {
                x = px(8);
                y += px(34);
            }
            place(button, x, y, size, px(29));
            x += size + px(4);
        }
        y += px(38);
        x = px(8);
        let small = ((logical_width as i32 - 260) / 2).max(140);
        let board_width = if compact_controls {
            small.clamp(controls.board_min_width as i32, 232)
        } else {
            232
        };
        let port_width = if compact_controls {
            small.clamp(controls.port_min_width as i32, 232)
        } else {
            195
        };
        let labels = controls.show_labels.iter().any(|value| *value);
        let caption_height = if labels { px(18) } else { 0 };
        let controls_row = caption_height + px(32);
        let mut groups = vec![
            (n.label, n.combo, board_width),
            (n.captions[0], n.selectors[0], port_width),
            (n.captions[1], n.selectors[1], 120),
        ];
        show(n.captions[3], !compact_controls);
        show(n.selectors[2], !compact_controls);
        if !compact_controls {
            groups.push((n.captions[3], n.selectors[2], 170));
        }
        for (caption, selector, size) in groups {
            if x + px(size) > width - px(8) {
                x = px(8);
                y += controls_row;
            }
            show(caption, labels);
            place(caption, x, y, px(size), px(18));
            place(selector, x, y + caption_height, px(size), px(260));
            x += px(size + 8);
        }
        for button in n.workspace.iter().chain([&n.detach]) {
            show(*button, !compact_controls);
        }
        show(n.options, compact_controls);
        show(n.captions[2], labels);
        if compact_controls {
            if x + px(100) > width - px(8) {
                x = px(8);
                y += controls_row;
            }
            place(n.captions[2], x, y, px(100), px(18));
            place(n.options, x, y + caption_height, px(100), px(27));
            y += controls_row;
        } else {
            y += controls_row;
            x = px(8);
            place(n.captions[2], x, y, px(90), px(18));
            y += px(18);
            for (button, size) in [
                (n.workspace[0], 112),
                (n.workspace[1], 120),
                (n.detach, 140),
                (n.workspace[2], 115),
                (n.workspace[3], 135),
                (n.workspace[4], 84),
                (n.workspace[5], 120),
            ] {
                if x + px(size) > width - px(8) {
                    x = px(8);
                    y += px(31);
                }
                place(button, x, y, px(size), px(27));
                x += px(size + 4);
            }
            y += px(34);
        }
        // A single bounded row never expands the window for wide manifests.
        // All retained sources remain reachable through the native Sources menu.
        let more_width = px(102).min(width);
        let mut source_x = 0;
        let mut overflow = false;
        for (index, button) in n.source_tabs.iter().enumerate() {
            let label = self
                .source_labels
                .get(index)
                .map_or("Untitled.ino", String::as_str);
            let size = (text_width(n.main, n.font, label) + px(24)).clamp(px(100), px(240));
            let fits = !overflow && source_x + size <= width - more_width;
            show(*button, fits);
            if fits {
                place(*button, source_x, y, size, px(29));
                source_x += size + px(3);
            } else {
                overflow = true;
            }
        }
        show(n.source_more, overflow);
        place(n.source_more, width - more_width, y, more_width, px(29));
        self.body_top = y + px(29);
        let footer = px(if short { 24 } else { 46 });
        let available = (height - self.body_top - footer).max(2);
        let tool_widths: Vec<_> = TOOL_TABS
            .iter()
            .map(|label| text_width(n.main, n.font, label) + px(26))
            .collect();
        let (mut tool_rows, mut tab_x) = (1, 0);
        for size in &tool_widths {
            if tab_x + size > width {
                tool_rows += 1;
                tab_x = 0;
            }
            tab_x += size + px(2);
        }
        // Reserve every wrapped native tab row and active panel control before
        // allocating the editor. Scrollable output yields its space last.
        let tool_controls = px(tool_rows * 29 + 44);
        if let Some(y) = n.events.split_y.take() {
            self.split_ratio = ((y - self.body_top) as f64 / available as f64).clamp(0.2, 0.8);
        }
        let editor_height = if self.detached || !self.editor_visible {
            0
        } else if !self.monitors_visible {
            available
        } else {
            ((available as f64 * self.split_ratio) as i32)
                .min((available - tool_controls - px(24)).max(0))
        };
        place(n.editor_host, 0, self.body_top, width, editor_height);
        place(n.splitter, 0, self.body_top + editor_height, width, px(6));
        x = 0;
        y = self.body_top + editor_height + px(6);
        for (button, size) in n.tools.iter().zip(tool_widths) {
            if x + size > width {
                x = 0;
                y += px(29);
            }
            place(*button, x, y, size, px(29));
            x += size + px(2);
        }
        y += px(35);
        place(n.tool_heading, px(9), y + px(4), width - px(110), px(22));
        place(n.clear, width - px(83), y, px(73), px(27));
        y += px(32);
        let output_height = (height - y - footer).max(1);
        place(n.terminal_host, 0, y, width, output_height);
        place(
            n.tool_output,
            px(10),
            y + px(4),
            width - px(20),
            output_height - px(8),
        );
        place(n.notice, px(8), height - px(42), width - px(16), px(18));
        show(n.notice, !short);
        place(n.status, px(8), height - px(23), width - px(16), px(20));
        // SAFETY: Visibility changes keep the native widgets and component
        // sessions alive; model and terminal ownership never move with tabs.
        unsafe {
            ShowWindow(
                n.editor_host,
                if !self.detached && self.editor_visible {
                    SW_SHOW
                } else {
                    SW_HIDE
                },
            );
            ShowWindow(
                n.splitter,
                if self.monitors_visible {
                    SW_SHOW
                } else {
                    SW_HIDE
                },
            );
            for child in n.tools.iter().chain([&n.tool_heading, &n.clear]) {
                ShowWindow(
                    *child,
                    if self.monitors_visible {
                        SW_SHOW
                    } else {
                        SW_HIDE
                    },
                );
            }
            ShowWindow(
                n.terminal_host,
                if self.monitors_visible && n.events.tool.get() == 5 {
                    SW_SHOW
                } else {
                    SW_HIDE
                },
            );
            ShowWindow(
                n.tool_output,
                if self.monitors_visible && n.events.tool.get() != 5 {
                    SW_SHOW
                } else {
                    SW_HIDE
                },
            );
        }
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
                PROJECT => self.open_project(),
                COMBO => self.evidence.borrow_mut().combo_events += 1,
                COMPILE if self.smoke => {
                    self.evidence.borrow_mut().compile_events += 1;
                    // SAFETY: Static text is copied synchronously into this live label.
                    unsafe {
                        SetWindowTextW(self.native.status, wide("Rust callback passed. Firmware compilation remains a separate migration gate.").as_ptr());
                    }
                }
                id if (SOURCE_TAB..SOURCE_TAB + 256).contains(&id) => {
                    self.select_source(id - SOURCE_TAB)?
                }
                SOURCE_MORE => {
                    let labels: Vec<_> = self
                        .evidence
                        .borrow()
                        .sources
                        .iter()
                        .map(|tab| tab.label().replace('&', "&&"))
                        .collect();
                    let entries: Vec<_> = labels
                        .iter()
                        .enumerate()
                        .map(|(index, label)| (SOURCE_TAB + index, label.as_str(), true))
                        .collect();
                    if let Some(id) = popup(self.native.main, self.native.source_more, &entries) {
                        self.native.events.command(id);
                    }
                }
                DETACH
                    if self.evidence.borrow().editor.is_some()
                        || self.evidence.borrow().workspace_ready =>
                {
                    self.toggle_editor()?
                }
                MORE_ACTIONS => {
                    let entries: Vec<_> = PRIMARY_ACTIONS
                        .iter()
                        .enumerate()
                        .filter(|(index, _)| (2..=8).contains(index))
                        .map(|(index, label)| {
                            (
                                ACTION + index,
                                *label,
                                index == 8 && !self.smoke && !self.workspace_smoke,
                            )
                        })
                        .collect();
                    if let Some(id) = popup(self.native.main, self.native.more_actions, &entries) {
                        self.native.events.command(id);
                    }
                }
                OPTIONS => {
                    // Native menus preserve compact access without creating an
                    // independent application window or exposing service writes.
                    let ready = self.evidence.borrow().editor.is_some()
                        || self.evidence.borrow().workspace_ready;
                    let entries = [
                        (
                            DETACH,
                            if self.detached {
                                "Reattach Editor"
                            } else {
                                "Detach Editor"
                            },
                            ready,
                        ),
                        (
                            HIDE_EDITOR,
                            if self.editor_visible {
                                "Hide Editor"
                            } else {
                                "Show Editor"
                            },
                            true,
                        ),
                        (
                            HIDE_MONITORS,
                            if self.monitors_visible {
                                "Hide Monitors"
                            } else {
                                "Show Monitors"
                            },
                            true,
                        ),
                        (410, "Timestamps", false),
                        (411, "Skip Compile", false),
                        (414, "Settings", false),
                        (415, "AI Assistant", false),
                        (THEME_ITEM, "Glass", true),
                        (THEME_ITEM + 1, "Frosted Light", true),
                        (THEME_ITEM + 2, "Solarized Dark", true),
                    ];
                    if let Some(id) = popup(self.native.main, self.native.options, &entries) {
                        self.native.events.command(id);
                    }
                }
                id if (THEME_ITEM..THEME_ITEM + 3).contains(&id) => {
                    // SAFETY: Choose only an existing theme in the owned combo.
                    unsafe {
                        SendMessageW(self.native.selectors[2], CB_SETCURSEL, id - THEME_ITEM, 0);
                    }
                    self.native.events.command(THEME);
                }
                THEME => {
                    // SAFETY: Read a selection only from this owned combo. The
                    // replacement brush remains alive until every repaint ends.
                    unsafe {
                        let theme = match SendMessageW(self.native.selectors[2], CB_GETCURSEL, 0, 0)
                        {
                            1 => Theme::FrostedLight,
                            2 => Theme::SolarizedDark,
                            _ => Theme::Glass,
                        };
                        self.native.events.theme.set(theme);
                        let brush = CreateSolidBrush(color(theme, "BG_DARKEST"));
                        if !brush.is_null() {
                            DeleteObject(self.native.events.surface.replace(brush));
                        }
                        InvalidateRect(self.native.main, std::ptr::null(), 1);
                        InvalidateRect(self.native.detached, std::ptr::null(), 1);
                        for button in self
                            .native
                            .actions
                            .iter()
                            .chain(&self.native.tools)
                            .chain(&self.native.workspace)
                            .chain(&self.native.source_tabs)
                            .chain([
                                &self.native.source_more,
                                &self.native.detach,
                                &self.native.more_actions,
                                &self.native.options,
                                &self.native.brand,
                            ])
                        {
                            InvalidateRect(*button, std::ptr::null(), 1);
                        }
                        let palette = serde_json::json!({"background":theme.color("BG_DARKEST"),"foreground":theme.color("TEXT"),"accent":theme.color("CYAN"),"selection":theme.color("BG_LIGHT")});
                        let script = format!(
                            "window.__mcuApplyTheme?.({}, {});",
                            serde_json::to_string(theme.key())?,
                            serde_json::to_string(&palette)?
                        );
                        self.editor.evaluate_script(&script)?;
                        self.terminal.evaluate_script(&script)?;
                    }
                }
                HIDE_EDITOR | HIDE_MONITORS => {
                    let (visible, label, button) = if command == HIDE_EDITOR {
                        (&mut self.editor_visible, "Editor", self.native.workspace[2])
                    } else {
                        (
                            &mut self.monitors_visible,
                            "Monitors",
                            self.native.workspace[3],
                        )
                    };
                    *visible = !*visible;
                    // SAFETY: The native button copies its new caption.
                    unsafe {
                        SetWindowTextW(
                            button,
                            wide(&format!(
                                "{} {label}",
                                if *visible { "Hide" } else { "Show" }
                            ))
                            .as_ptr(),
                        );
                    }
                    self.layout()?;
                }
                CLEAR => {
                    // SAFETY: Display-only clear never affects subprocesses,
                    // serial writes, editor buffers or terminal command history.
                    unsafe {
                        SetWindowTextW(self.native.tool_output, wide("").as_ptr());
                    }
                    if self.native.events.tool.get() == 5 {
                        self.terminal
                            .evaluate_script("window.__mcuClearTerminal?.();")?;
                    }
                }
                id if (TOOL..TOOL + TOOL_TABS.len()).contains(&id) => {
                    let index = id - TOOL;
                    self.native.events.tool.set(index);
                    let text = match index {
                        0 => "Build output appears here when the Rust build service is qualified.",
                        1 => "No device connected. Serial Send is unavailable in this preview.",
                        2 => "No board selected. Device discovery is unavailable in this preview.",
                        3 => "No notifications in this isolated workspace.",
                        4 => "Syntax diagnostics are unavailable in this preview.",
                        5 => "The embedded terminal retains its session while switching tools.",
                        _ => "No AI changes in this isolated workspace.",
                    };
                    // SAFETY: These bounded fixture strings are copied by native
                    // controls. Changing tabs grants no service authority.
                    unsafe {
                        SetWindowTextW(self.native.tool_heading, wide(TOOL_TABS[index]).as_ptr());
                        SetWindowTextW(self.native.tool_output, wide(text).as_ptr());
                        for tab in &self.native.tools {
                            InvalidateRect(*tab, std::ptr::null(), 1);
                        }
                    }
                    self.layout()?;
                }
                id if (ACTION..ACTION + PRIMARY_ACTIONS.len()).contains(&id)
                    || (410..416).contains(&id) =>
                {
                    // SAFETY: Preview actions report a service gate; they never
                    // write files, settings, devices or accounts.
                    unsafe {
                        SetWindowTextW(self.native.status, wide("This action is unavailable in the isolated native workspace preview.").as_ptr());
                    }
                }
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

fn run_window(
    smoke: bool,
    profile: &Path,
    theme: Theme,
    project: Option<PathBuf>,
    workspace_smoke: bool,
) -> Result<(), Box<dyn std::error::Error>> {
    let started = Instant::now();
    let native = NativeWindows::new(smoke, workspace_smoke)?;
    let evidence = Rc::new(RefCell::new(Evidence::default()));
    let mut editor_context = WebContext::new(Some(profile.join("editor")));
    let mut terminal_context = WebContext::new(Some(profile.join("terminal")));
    let editor = create_view(
        &mut editor_context,
        native.editor_host,
        Island::Editor,
        evidence.clone(),
        smoke,
    )?;
    let terminal = create_view(
        &mut terminal_context,
        native.terminal_host,
        Island::Terminal,
        evidence.clone(),
        smoke,
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
        editor_visible: true,
        monitors_visible: true,
        workspace_initialized: false,
        terminal_initialized: false,
        source_labels: vec![if smoke {
            "fixture.ino  •".into()
        } else {
            "Untitled.ino".into()
        }],
        source_generation: 0,
        project_path: project,
        project_worker: None,
        project_deadline: None,
        project_launch: None,
        project_children: Arc::new(AtomicUsize::new(0)),
        workspace_smoke,
        workspace_phase: 0,
        workspace_waiting: false,
        workspace_checks: BTreeMap::new(),
        smoke,
    };
    // SAFETY: Select only an existing theme item in the owned native combo.
    unsafe {
        SendMessageW(
            app.native.selectors[2],
            CB_SETCURSEL,
            match theme {
                Theme::Glass => 0,
                Theme::FrostedLight => 1,
                Theme::SolarizedDark => 2,
            },
            0,
        );
    }
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
            // Native controls retain standard Tab/Shift+Tab navigation. WebView
            // child messages bypass dialog traversal so Monaco owns its keys.
            if GetParent(message.hwnd) == app.native.main
                && IsDialogMessageW(app.native.main, &message) != 0
            {
                continue;
            }
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
                e.editor.is_some() || e.workspace_ready
            };
            let workspace_ready = {
                let e = app.evidence.borrow();
                e.workspace_ready && e.terminal_ready && e.error.is_none()
            };
            if !smoke
                && workspace_ready
                && (!app.workspace_initialized || !app.terminal_initialized)
            {
                // SAFETY: Both components reported readiness; update this live
                // label once without claiming firmware or service availability.
                unsafe {
                    SetWindowTextW(
                        app.native.status,
                        wide("Native Rust workspace ready. Edits stay in memory.").as_ptr(),
                    );
                }
            }
            if app.evidence.borrow().workspace_ready && !app.workspace_initialized {
                app.workspace_initialized = true;
                app.native.events.command(THEME);
                if let Some(selected) = app.project_path.clone() {
                    app.start_project_read(selected);
                }
            }
            if app.evidence.borrow().terminal_ready && !app.terminal_initialized {
                app.terminal_initialized = true;
                app.native.events.command(THEME);
            }
            app.poll_project()?;
            app.poll_project_launch();
            if !smoke {
                app.sync_sources()?;
            }
            // SAFETY: Enabling an owned child does not transfer its ownership.
            unsafe {
                EnableWindow(app.native.detach, ready as i32);
            }
            if smoke || workspace_smoke {
                if started.elapsed() > Duration::from_secs(45) {
                    app.evidence.borrow_mut().error =
                        Some("Native Win32 smoke deadline expired.".into());
                }
                if app.evidence.borrow().error.is_none() {
                    match if workspace_smoke {
                        app.workspace_smoke_step()
                    } else {
                        app.smoke_step()
                    } {
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
    if workspace_smoke {
        let e = app.evidence.borrow();
        let error = e.error.clone().or_else(|| {
            (!done).then(|| "Native workspace closed before completing smoke checks.".into())
        });
        let passed = done
            && error.is_none()
            && app.workspace_checks.len() == 18
            && app.workspace_checks.values().all(|value| *value);
        let receipt = Receipt {
            schema_version: 1, passed, mode: "isolated_native_workspace_fixture", os: "windows",
            architecture: std::env::consts::ARCH, display_backend: "Win32",
            webview_version: wry::webview_version().unwrap_or_else(|_| "unavailable".into()),
            widget_types: app.native.source_tabs.iter().copied().map(class_name).collect(),
            checks: app.workspace_checks.clone(), error,
            limits: "Win32/WebView2 read-only Rust source-reader fixture; 45-second message-loop deadline; 20-second reader / 25-second acknowledgement deadlines; 16-KiB typed reports; owned two-file scratch only; no save, device, live ownership, settings, cloud or Python application service.",
        };
        println!("MCU_NATIVE_WORKSPACE={}", serde_json::to_string(&receipt)?);
        if !passed {
            return Err(receipt
                .error
                .unwrap_or_else(|| "A native Win32 workspace assertion failed.".into())
                .into());
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
    let mut smoke = false;
    let mut workspace_smoke = false;
    let mut theme = Theme::Glass;
    let mut project = None;
    let mut args = std::env::args_os().skip(1);
    let mut count = 0;
    while let Some(flag) = args.next() {
        count += 1;
        if count > 4 {
            return Err("Too many native preview options.".into());
        }
        match flag.to_str() {
            Some("--smoke") if !smoke => smoke = true,
            Some("--workspace-smoke") if !workspace_smoke => workspace_smoke = true,
            Some("--theme") => theme = Theme::parse(args.next().and_then(|value| value.into_string().ok()).as_deref().ok_or("--theme requires a value")?),
            Some("--project") if project.is_none() => {
                let selected = args.next().ok_or("--project requires a folder")?;
                if selected.is_empty() { return Err("--project requires a folder".into()); }
                project = Some(PathBuf::from(selected));
            }
            _ => return Err("Usage: mcu-native-ui-probe [--smoke | --workspace-smoke | --project FOLDER] [--theme NAME]".into()),
        }
    }
    if (smoke && project.is_some()) || (workspace_smoke && (smoke || project.is_some())) {
        return Err("Native smoke verification owns its source fixture.".into());
    }
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
    let result = (|| {
        if workspace_smoke {
            let root = profile.join("project café 日本語");
            std::fs::create_dir(&root)?;
            std::fs::write(root.join("Sketch.ino"), CHECK_SOURCE)?;
            std::fs::write(root.join("Config.h"), CHECK_HEADER)?;
            project = Some(root);
        }
        run_window(smoke, &profile, theme, project, workspace_smoke)
    })();
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

#[cfg(test)]
mod source_checks {
    use super::*;
    #[test]
    fn native_icon_buffer_aligns_unaligned_original_payload_and_preserves_bytes() {
        for size in [16, 32] {
            let image = icon_image(APPLICATION_ICON, size).expect("original payload");
            // Place the original bytes at a deliberately unaligned address,
            // independently of the compiler's include_bytes placement.
            let mut backing = vec![0u8; image.len() + 4];
            let start = (4 - backing.as_ptr() as usize % 4) % 4 + 1;
            backing[start..start + image.len()].copy_from_slice(image);
            let unaligned = &backing[start..start + image.len()];
            assert_eq!(unaligned.as_ptr() as usize % 4, 1);
            let storage = aligned_icon_storage(unaligned);
            assert_eq!(storage.as_ptr() as usize % 4, 0);
            let bytes: Vec<u8> = storage.iter().flat_map(|word| word.to_ne_bytes()).collect();
            assert_eq!(&bytes[..image.len()], image);
            assert_eq!(bytes.len(), image.len().div_ceil(4) * 4);
            assert!(bytes[image.len()..].iter().all(|byte| *byte == 0));
        }
    }
    #[test]
    fn cancelled_picker_has_no_launch_arguments() {
        assert!(project_arguments(None, Theme::Glass).is_none());
    }
    #[test]
    fn project_launch_preserves_literal_unicode_and_native_units() {
        let path = PathBuf::from(r"C:\project café 日本語 & $stuff\");
        let arguments = project_arguments(Some(&path), Theme::SolarizedDark).expect("selected");
        assert_eq!(
            arguments,
            [
                OsString::from("--project"),
                path.into_os_string(),
                OsString::from("--theme"),
                OsString::from("solarized_dark")
            ]
        );
        let native = PathBuf::from(OsString::from_wide(&[67, 58, 92, 0xd800, 120]));
        assert_eq!(
            project_arguments(Some(&native), Theme::Glass).unwrap()[1],
            native.as_os_str()
        );
    }
    #[test]
    fn original_icon_creates_owned_native_small_and_large_handles() {
        let icons = ApplicationIcons::new().expect("embedded original icon");
        assert!(!icons.large.is_null());
        assert!(!icons.small.is_null());
        assert_ne!(icons.large, icons.small);
    }
    fn changed(state: &RefCell<Evidence>, id: &str, revision: u64, dirty: bool) {
        receive(state, Island::Editor, &serde_json::json!({"kind":"document_changed", "document_id":id, "edit_revision":revision, "dirty":dirty}).to_string());
    }
    #[test]
    fn only_exact_ordered_ack_commits_native_source_names() {
        let state = RefCell::new(Evidence::default());
        receive(&state, Island::Editor, r#"{"kind":"workspace_ready"}"#);
        state.borrow_mut().pending_tabs = Some(vec![
            ("a".into(), "Sketch.ino".into()),
            ("b".into(), "Config.h".into()),
        ]);
        receive(
            &state,
            Island::Editor,
            r#"{"kind":"documents_opened","documents":["b","a"]}"#,
        );
        assert!(state.borrow().pending_tabs.is_none());
        assert_eq!(state.borrow().sources[0].id, "untitled");
        state.borrow_mut().error = None;
        state.borrow_mut().pending_tabs = Some(vec![
            ("a".into(), "Sketch.ino".into()),
            ("b".into(), "Config.h".into()),
        ]);
        receive(
            &state,
            Island::Editor,
            r#"{"kind":"documents_opened","documents":["a","b"]}"#,
        );
        assert_eq!(
            state
                .borrow()
                .sources
                .iter()
                .map(|tab| tab.id.as_str())
                .collect::<Vec<_>>(),
            ["a", "b"]
        );
        assert!(state.borrow().pending_tabs.is_none());
    }
    #[test]
    fn native_dirty_labels_accept_only_known_ids_and_newer_revisions() {
        let state = RefCell::new(Evidence::default());
        receive(&state, Island::Editor, r#"{"kind":"workspace_ready"}"#);
        changed(&state, "outside", 1, true);
        assert!(!state.borrow().sources[0].dirty);
        changed(&state, "untitled", 2, true);
        changed(&state, "untitled", 1, false);
        changed(&state, "untitled", 2, false);
        assert!(state.borrow().sources[0].dirty);
        changed(&state, "untitled", 3, false);
        assert!(!state.borrow().sources[0].dirty);
    }
    #[test]
    fn editor_refusal_clears_pending_and_retains_native_tabs_and_dirty_state() {
        let state = RefCell::new(Evidence::default());
        receive(&state, Island::Editor, r#"{"kind":"workspace_ready"}"#);
        changed(&state, "untitled", 1, true);
        state.borrow_mut().pending_tabs = Some(vec![("a".into(), "Sketch.ino".into())]);
        receive(
            &state,
            Island::Editor,
            r#"{"kind":"error","message":"unsaved edits are still open"}"#,
        );
        assert!(state.borrow().pending_tabs.is_none());
        assert_eq!(state.borrow().sources[0].id, "untitled");
        assert!(state.borrow().sources[0].dirty);
    }
}
