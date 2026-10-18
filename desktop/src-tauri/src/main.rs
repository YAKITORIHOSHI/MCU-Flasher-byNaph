use mcu_contracts::{
    DocumentRequest, SaveDocumentRequest, ScaffoldOptions, ServiceError, WorkspaceId,
};
use mcu_core::ProjectService;
use serde::Serialize;
use std::path::PathBuf;
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc, Mutex,
};
use tauri::{ipc::Channel, Manager, State, WebviewWindow};

struct PrototypeState {
    projects: Arc<Mutex<ProjectService>>,
    fixture_parent: PathBuf,
    fixture_project: PathBuf,
    streaming: Arc<AtomicBool>,
    verification: bool,
    probe_done: Arc<AtomicBool>,
    report_path: PathBuf,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct PrototypeInfo {
    protocol_version: u32,
    mode: &'static str,
    fixture_parent: String,
    fixture_project: String,
    host: HostInfo,
    gates: Vec<&'static str>,
}

#[derive(Serialize)]
struct HostInfo {
    os: &'static str,
    architecture: &'static str,
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct FixtureEvent {
    r#type: &'static str,
    sequence: u32,
    #[serde(skip_serializing_if = "Option::is_none")]
    data: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    bytes: Option<usize>,
    #[serde(skip_serializing_if = "Option::is_none")]
    exit_code: Option<u32>,
}

fn project_lock_error() -> ServiceError {
    ServiceError {
        code: mcu_contracts::ErrorCode::Io,
        message:
            "The project service is unavailable after a failed operation. Reopen the prototype."
                .into(),
        retryable: false,
    }
}

#[tauri::command]
fn prototype_info(state: State<'_, PrototypeState>) -> PrototypeInfo {
    PrototypeInfo {
        protocol_version: mcu_contracts::PROTOCOL_VERSION,
        mode: "fixture_only",
        fixture_parent: state.fixture_parent.to_string_lossy().into_owned(),
        fixture_project: state.fixture_project.to_string_lossy().into_owned(),
        host: HostInfo {
            os: std::env::consts::OS,
            architecture: std::env::consts::ARCH,
        },
        gates: vec![
            "Editor detach with retained undo is unverified",
            "Windows and ARM64 native qualification pending",
            "Toolchain, serial, cloud and hardware parity pending",
            "Legacy/new process leases are not integrated",
        ],
    }
}

async fn project_operation<T: Send + 'static>(
    projects: Arc<Mutex<ProjectService>>,
    owner: String,
    operation: impl FnOnce(&mut ProjectService, &str) -> Result<T, ServiceError> + Send + 'static,
) -> Result<T, ServiceError> {
    tauri::async_runtime::spawn_blocking(move || {
        let mut service = projects.lock().map_err(|_| project_lock_error())?;
        operation(&mut service, &owner)
    })
    .await
    .map_err(|_| project_lock_error())?
}

#[tauri::command]
async fn open_project(
    window: WebviewWindow,
    state: State<'_, PrototypeState>,
    path: String,
) -> Result<mcu_contracts::WorkspaceRegistration, ServiceError> {
    project_operation(
        Arc::clone(&state.projects),
        window.label().into(),
        move |service, owner| service.register_project(owner, PathBuf::from(path)),
    )
    .await
}

#[tauri::command]
async fn project_manifest(
    window: WebviewWindow,
    state: State<'_, PrototypeState>,
    workspace_id: WorkspaceId,
) -> Result<mcu_contracts::SourceManifest, ServiceError> {
    project_operation(
        Arc::clone(&state.projects),
        window.label().into(),
        move |service, owner| service.manifest(owner, &workspace_id),
    )
    .await
}

#[tauri::command]
async fn read_document(
    window: WebviewWindow,
    state: State<'_, PrototypeState>,
    request: DocumentRequest,
) -> Result<mcu_contracts::DocumentSnapshot, ServiceError> {
    project_operation(
        Arc::clone(&state.projects),
        window.label().into(),
        move |service, owner| {
            service.read_document(owner, &request.workspace_id, &request.document_id)
        },
    )
    .await
}

#[tauri::command]
async fn save_document(
    window: WebviewWindow,
    state: State<'_, PrototypeState>,
    request: SaveDocumentRequest,
) -> Result<mcu_contracts::SaveAcknowledgement, ServiceError> {
    project_operation(
        Arc::clone(&state.projects),
        window.label().into(),
        move |service, owner| service.save_document(owner, request),
    )
    .await
}

#[tauri::command]
async fn create_project(
    window: WebviewWindow,
    state: State<'_, PrototypeState>,
    parent_path: String,
    options: ScaffoldOptions,
) -> Result<mcu_contracts::WorkspaceRegistration, ServiceError> {
    project_operation(
        Arc::clone(&state.projects),
        window.label().into(),
        move |service, owner| service.create_project(owner, PathBuf::from(parent_path), options),
    )
    .await
}

#[tauri::command]
async fn stream_fixture(
    state: State<'_, PrototypeState>,
    on_event: Channel<FixtureEvent>,
) -> Result<mcu_host::StreamSummary, String> {
    if state.streaming.swap(true, Ordering::AcqRel) {
        return Err("A controlled PTY probe is already running".into());
    }
    let active = Arc::clone(&state.streaming);
    tauri::async_runtime::spawn_blocking(move || {
        struct Reset(Arc<AtomicBool>);
        impl Drop for Reset {
            fn drop(&mut self) {
                self.0.store(false, Ordering::Release);
            }
        }
        let _reset = Reset(active);
        let executable = std::env::current_exe().map_err(|error| error.to_string())?;
        let mut sequence = 0;
        on_event
            .send(FixtureEvent {
                r#type: "started",
                sequence,
                data: None,
                bytes: None,
                exit_code: None,
            })
            .map_err(|error| error.to_string())?;
        let result = mcu_host::stream_fixture(&executable, |data, bytes| {
            sequence += 1;
            on_event
                .send(FixtureEvent {
                    r#type: "output",
                    sequence,
                    data: Some(data),
                    bytes: Some(bytes),
                    exit_code: None,
                })
                .map_err(|error| error.to_string())
        })?;
        sequence += 1;
        on_event
            .send(FixtureEvent {
                r#type: "finished",
                sequence,
                data: None,
                bytes: Some(result.bytes),
                exit_code: Some(result.exit_code),
            })
            .map_err(|error| error.to_string())?;
        Ok(result)
    })
    .await
    .map_err(|error| error.to_string())?
}

/// Bounded test evidence endpoint. It is inactive during ordinary preview use
/// and can write only the one native-probe report chosen by the trusted host.
#[tauri::command]
fn report_probe(
    app: tauri::AppHandle,
    window: WebviewWindow,
    state: State<'_, PrototypeState>,
    report: serde_json::Value,
) -> Result<(), String> {
    if !state.verification || window.label() != "main" || state.probe_done.load(Ordering::Acquire) {
        return Err("Native probe reporting is inactive".into());
    }
    let bytes = serde_json::to_vec(&report).map_err(|error| error.to_string())?;
    if bytes.len() > 32_768 || !report.is_object() {
        return Err("Invalid native probe report".into());
    }
    let checks = report.get("checks").and_then(|v| v.as_object());
    let passed = report.get("nativeAdapter").and_then(|v| v.as_bool()) == Some(true)
        && report
            .get("errors")
            .and_then(|v| v.as_array())
            .is_some_and(|errors| errors.is_empty())
        && checks.is_some_and(|checks| {
            checks.len() >= 12 && checks.values().all(|v| v.as_bool() == Some(true))
        });
    let evidence = serde_json::json!({"schemaVersion":1,"native":true,"passed":passed,
        "host":{"os":std::env::consts::OS,"architecture":std::env::consts::ARCH},
        "frontend":report});
    std::fs::write(
        &state.report_path,
        serde_json::to_vec_pretty(&evidence).map_err(|error| error.to_string())?,
    )
    .map_err(|error| error.to_string())?;
    state.probe_done.store(true, Ordering::Release);
    app.exit(if passed { 0 } else { 1 });
    Ok(())
}

/// Move the *same* webview out and back. The renderer asserts model identity,
/// dirty contents, view state and undo/redo afterwards. This establishes only
/// the native mechanism; splitting editor chrome into a child view is pending.
#[tauri::command]
async fn verify_webview_transfer(
    app: tauri::AppHandle,
    webview: tauri::Webview,
    state: State<'_, PrototypeState>,
) -> Result<serde_json::Value, String> {
    if !state.verification || webview.label() != "main" {
        return Err("Window transfer is restricted to native verification".into());
    }
    tauri::async_runtime::spawn_blocking(move || {
        let original = app
            .get_window("main")
            .ok_or("Original prototype window is unavailable")?;
        let size = original.inner_size().map_err(|e| e.to_string())?;
        let target = tauri::WindowBuilder::new(&app, "migration-detach-proof")
            .title("MCU Flasher — live editor transfer probe")
            .inner_size(900.0, 640.0)
            .visible(false)
            .build()
            .map_err(|e| e.to_string())?;
        let transfer = (|| -> Result<(), String> {
            for _ in 0..3 {
                webview.reparent(&target).map_err(|e| e.to_string())?;
                webview
                    .set_position(tauri::PhysicalPosition::new(0, 0))
                    .map_err(|e| e.to_string())?;
                webview
                    .set_size(target.inner_size().map_err(|e| e.to_string())?)
                    .map_err(|e| e.to_string())?;
                std::thread::sleep(std::time::Duration::from_millis(60));
                webview.reparent(&original).map_err(|e| e.to_string())?;
                webview
                    .set_position(tauri::PhysicalPosition::new(0, 0))
                    .map_err(|e| e.to_string())?;
                webview.set_size(size).map_err(|e| e.to_string())?;
            }
            Ok(())
        })();
        // On partial failure, attempt to restore ownership before closing the
        // temporary host. Never recreate the renderer or serialize its model.
        if transfer.is_err() {
            let _ = webview.reparent(&original);
            let _ = webview.set_size(size);
        }
        let _ = target.close();
        transfer?;
        Ok(serde_json::json!({"cycles":3,"sameWebview":true,"unstableApi":true}))
    })
    .await
    .map_err(|error| error.to_string())?
}

fn main() {
    if std::env::args().nth(1).as_deref() == Some(mcu_host::FIXTURE_CHILD_ARGUMENT) {
        if mcu_host::write_fixture(std::io::stdout()).is_err() {
            std::process::exit(1);
        }
        return;
    }
    if let Err(error) = run() {
        eprintln!("MCU Flasher prototype: {error}");
        std::process::exit(1);
    }
}

fn run() -> Result<(), Box<dyn std::error::Error>> {
    let verification = std::env::args().any(|arg| arg == "--verify-prototype");
    let executable = std::env::current_exe()?.canonicalize()?;
    let root = executable
        .ancestors()
        .find(|path| {
            path.join("mcu_flash_gui.py").is_file()
                && path.join("desktop/Cargo.toml").is_file()
                && path.join("src/editor/bundle.js").is_file()
        })
        .ok_or("This fixture prototype must run from its MCU Flasher checkout")?
        .to_path_buf();
    let fixture_name = if verification {
        format!("native-probe-{}", std::process::id())
    } else {
        "native-prototype".into()
    };
    let fixture_parent = root.join("audit/migration/scratch").join(&fixture_name);
    // Reject pre-existing aliases before creating a disposable boundary. This
    // remains fixture-only; adversarial filesystem race qualification is pending.
    let mut checked = root.clone();
    for name in ["audit", "migration", "scratch", fixture_name.as_str()] {
        checked.push(name);
        match std::fs::symlink_metadata(&checked) {
            Ok(info) if info.is_dir() && !info.file_type().is_symlink() => {}
            Ok(_) => {
                return Err("The prototype fixture path contains an alias or non-directory".into())
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                std::fs::create_dir(&checked)?
            }
            Err(error) => return Err(error.into()),
        }
    }
    let mut projects = ProjectService::fixture(&root, &fixture_parent)?;
    let fixture_project = fixture_parent.join("CompatibilitySketch");
    if !fixture_project.exists() {
        projects.create_project(
            "main",
            &fixture_parent,
            ScaffoldOptions {
                name: "CompatibilitySketch".into(),
                include_h: true,
                include_cpp: true,
                template: mcu_contracts::ScaffoldTemplate::Bare,
            },
        )?;
    }
    let report_path = root.join("audit/migration/native-webview.json");
    let probe_done = Arc::new(AtomicBool::new(false));
    let watchdog_done = Arc::clone(&probe_done);
    let watchdog_report = report_path.clone();
    let mut context = tauri::generate_context!();
    if verification {
        context.config_mut().app.windows[0].url =
            tauri::WebviewUrl::App("index.html?verify=1".into());
    }
    tauri::Builder::default()
        .manage(PrototypeState { projects: Arc::new(Mutex::new(projects)), fixture_parent, fixture_project,
            streaming: Arc::new(AtomicBool::new(false)), verification, probe_done, report_path })
        .setup(move |app| {
            if verification {
                let handle = app.handle().clone();
                std::thread::spawn(move || {
                    for _ in 0..180 {
                        if watchdog_done.load(Ordering::Acquire) { return; }
                        std::thread::sleep(std::time::Duration::from_millis(250));
                    }
                    let _ = std::fs::write(watchdog_report,
                        r#"{"passed":false,"native":true,"errors":["Native webview probe timed out before reporting"]}"#);
                    handle.exit(1);
                });
            }
            Ok(())
        })
        .on_page_load(move |webview, payload| {
            if verification && payload.event() == tauri::webview::PageLoadEvent::Finished {
                let _ = webview.eval(r#"(() => {
                  let attempts = 0;
                  const poll = setInterval(async () => {
                    if (typeof window.__MCU_PROTOTYPE_PROBE__ === 'function') {
                      clearInterval(poll);
                      let report;
                      try { report = await window.__MCU_PROTOTYPE_PROBE__(); }
                      catch (error) { report = {passed:false,native:true,errors:[String(error)]}; }
                      window.__TAURI_INTERNALS__.invoke('report_probe', {report});
                    } else if (++attempts > 400) {
                      clearInterval(poll);
                      window.__TAURI_INTERNALS__.invoke('report_probe', {report:{passed:false,native:true,errors:['Frontend probe did not initialize']}});
                    }
                  }, 50);
                })();"#);
            }
        })
        .invoke_handler(tauri::generate_handler![prototype_info, open_project, project_manifest,
            read_document, save_document, create_project, stream_fixture, report_probe, verify_webview_transfer])
        .run(context)?;
    Ok(())
}
