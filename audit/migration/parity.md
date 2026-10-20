# Migration parity ledger

Latest continuation code: `8dbdb9d`. Final architecture requires native Rust
GUI widgets and Rust backend services, with Monaco/xterm embedded and Python only
for approved firmware tools. The GTK/Win32 fixtures and configured-index compile
campaign are recorded in [status](STATUS.md), [native evidence](native-rust-ui.json)
and [compile evidence](board-compile-campaign.json). The 67-capability ledger
remains **14 partial fixture subsets and 53 pending**, with no service retired.
The original Tauri evidence below remains historical and does not fulfill the
new native-widget requirement. Native input, complete backend parity and release
qualification remain open.

19 October 2026 · Asia/Manila · Legacy `1d00ff0` · Prototype checkpoint `4cf730b`

**The baseline behavior and performance audit remains incomplete.** This ledger records 67 capabilities: 14 have a partial fixture implementation and 53 remain pending. None is approved for legacy replacement.

[Detailed contracts and gates](parity.json) · [Original source/API/event/resource inventory](source-inventory.json) · [Reference amendments](reference-amendments.json) · [Reuse decisions](reuse.md)

The preserved structural baseline covers 300 source/launcher/verifier files, 280 Python files, 123 Python verifier files and 133 resource hashes, with zero AST parse failures. All 123 Python verifier files are mapped below, alongside five JavaScript references. Mapping scripts does not establish that every branch has been reviewed or tested. No live settings, credentials, sketches, protected caches/journals, hardware or cloud records were accessed for this audit.

## Evidence and remaining gates

- Local Ubuntu 26.04 x86_64 validation at `4cf730b` records **90 Rust tests, 40 frontend tests and 46 native fixture checks**. A fresh full local rerun at this exact revision passed. See [native fixture report](native-webview.json) and [checkpoint verification](verification.json). This Tauri run used a checkout-local Xvfb display and process-local Mesa settings; it is virtual-display fixture evidence, not physical GPU or full native-input qualification.
- At `4cf730b`, all five migration jobs passed: Ubuntu 24.04 AMD64/ARM64 and Windows Server 2022 native prototypes each passed 46 webview checks, and both Windows original-editor binding jobs passed 12 tests at 100% and 200%. The separate 14-job compatibility, four-job native-layout and native ARM64 workflows also passed at this exact revision. See [CI evidence](ci-results.json). Hosted results do not qualify Windows 10/11 desktops or physical Pi/USB behavior.
- Pure reference inputs now total **23,909**: serial codec 325, preference merge 1,335, scaffold 16, resource budgets 8,960, target transport 13,039 and ownership 234. The ownership subset includes 210 liveness cases, 17 port-label cases and seven deliberate structural/ambiguous-input refusals. Host admission, device workflows and live ownership integration remain pending.
- The target-transport subset produces **51,993 equivalent comparisons** and **163 explicit structural refusal comparisons** across four pure helpers. The refusals cover 64 non-object target fields and 99 truthy non-string protocols. Full `target_problem`, source/toolchain preparation, Arduino CLI proof, INI validation and all firmware/device execution remain pending. Input-case and function-comparison counts are different units.
- Eight fixture checks demonstrate OS file-lock interoperability with Python's settings lock, including contention and process exit. Six [native process evidence checks](process-identity.json) compare own/fixed-child PID/create time against psutil. Nine [registration interoperability checks](registration-interop.json) cover fresh scratch settings reads and evidence gathering under the shared lock, collision/refusal, release and preservation of unrelated data. These subsets passed locally and at the `4cf730b` Windows Server/Ubuntu AMD64/ARM64 checkpoints. Host `Unknown(ZombieOrDead)` denotes zombie uncertainty and must continue reserving resources. Point-in-time PID evidence is not a lease or source-write authority.
- The registry now uses private validated JSON fragments to retain huge integers, precise floating values and nested objects containing serde private-marker keys without converting opaque metadata through `Value`. Keys are serialized and composed with already validated value fragments. Correctly rounded `float_roundtrip` parsing preserves Python ownership timestamp comparisons. The 21 Rust registry tests supplement the nine process interoperability checks; they do not qualify generic untrusted serialization through Tauri's upstream-enabled `raw_value` feature.
- The legacy schema records one active sketch per PID and uses a strict **less than two seconds** creation-time tolerance. That tolerance is not unique-identity proof. The fixture planner enforces one workspace per process and refuses unverifiable current ownership or implicit replacement with another workspace. Production registration/activation and save-time revalidation remain pending. Package event/lease roots are per checkout while the Linux PlatformIO store is per user; shared store coordination, native root identity, TOCTOU/external-writer races, ACL/xattr preservation and crash durability continue to block user-project writes.
- The Tauri editor fixture preserves original BOM and mixed-EOL bytes on unchanged or undo-restored saves; edited content uses the model BOM/EOL and discloses normalization. Acknowledged PTY flow control allows one exact stream/sequence/byte chunk at a time and aborts failed consumers without replay. These are fixture subsets, not complete editor or terminal parity.
- The original Python/Qt page now moves focus out of the AI bar before hiding it, with visible-toggle and blur fallbacks if Monaco cannot take focus immediately. Its contained Qt child-viewport/DOM/model/focus/undo suite passed **10/10** at native XCB 100% and **10/10** at 200% using verified Mesa OpenGL llvmpipe software rendering. Qt also initialized NVIDIA Vulkan; physical NVIDIA 200% attempts were incomplete. See [editor evidence](editor-review.json). At `4cf730b`, the two new capture-path containment tests plus those ten UI cases passed **12/12 at each of 100% and 200%** on checkout-local Xvfb. The successful 200% run emitted Chromium shared-image-mailbox startup/teardown diagnostics, with no JavaScript/Python warning, deprecation or test failure. These results do not establish physical GPU performance or full keyboard/IME/clipboard behavior.
- Full Windows 10/11, Ubuntu 24.04/26.04 desktops, Raspberry Pi, driver/toolchain/physical USB behavior and installation/offline/repair/rollback require separate evidence. Windows Server and hosted ARM64 results do not substitute for these qualifications. Editor transfer still uses Tauri unstable reparenting of the whole fixture webview; production editor-only ownership, keyboard/IME/focus and recovery remain gates.
- Finish source/schema/failure traces and measured startup, process-tree RAM, idle CPU, latency and throughput baselines before declaring full parity or performance improvement. Preserve fixed Firebase identity/schema and OS-vault policy; deployed rules require separate validation.

## Reference changes during the prototype

The baseline inventory remains unchanged. [Reference amendments](reference-amendments.json) records **21 unique amended paths: eight production sources, eleven legacy verifiers and two legacy CI files**, plus separately added migration workflows. Of the baseline rows, **281 of 300 source entries and 130 of 133 resource entries** remain byte-identical. The changed resource rows include the original editor HTML and compatibility CI; the **22 original runtime assets staged to 25 destinations remain byte-identical**.

These baseline hashes describe exact original **checkout bytes**, not an assertion that every inventory row equals its Git blob. Sixteen duplicated inventory rows have checkout line-ending variants, including mixed and doubled CRLF. Their current bytes still match the preserved original baseline hashes, and their content matches Git after removing CR. The original inventory is retained rather than regenerated to conceal these differences.

The first production change, `src/modules/tk_glass.py::DialogFit._fit`, stops repeated minsize/position writes for unchanged work-area/frame/client/minimum/scale inputs after Configure bursts. Changed inputs still refit, failed native requests remain retryable and the complete work-area matrix remains in place. The second change, `src/editor/index.html`, provides focus fallbacks before hiding the AI bar. Seven verifier amendments separately address queued completion, native paint/layout settling, captured-process exit races and the contained original-editor viewport/focus assertions. Passing amended fixtures do not constitute unchanged-reference equivalence or full migration approval.

## Feature map

Every entry remains pending for **full** parity. Partial means only the subset described in `parity.json`. Its detailed entries include acceptance scenarios, failure/race behavior, ownership, host requirements and retirement gates.

### Workspace

| ID and capability | Status / disposition | Legacy source and verifier references |
| --- | --- | --- |
| UI-01 Startup and background discovery | pending<br>behavior-preserving-port | `main/qt/main_window.py:173` `showEvent`<br>`main/web_bridge.py:644` `start_services`<br>`verify_runtime.py`, `verify_performance.py`, `verify_gui_lifetime.py` |
| UI-02 Glass themes, fonts, icons and readable controls | partial-fixture-only<br>host-adapter | `main/qt/theme.py:1`<br>`main/core/theme.py:1`<br>`src/modules/ui_palette.py:1`<br>`src/assets/icons/actions/add.svg:1`<br>`verify_theme_readability.py`, `verify_svg_icons.py`, `verify_panel_readability.py` |
| UI-03 Scaling, screen fit, splitters and compact rows | pending<br>behavior-preserving-port | `main/qt/responsive.py:1`<br>`main/qt/main_window.py:194` `resizeEvent`<br>`src/modules/ui_metrics.py:1`<br>`verify_responsive.py`, `verify_ui_layout.py`, `verify_controls.py` |
| UI-04 Detach and reattach live editor | partial-fixture-only<br>host-adapter | `main/qt/main_window.py:1484` `detach_editor`<br>`main/qt/main_window.py:1532` `attach_editor`<br>`main/qt/detached_editor.py:1`<br>`verify_runtime.py`, `verify_editor_review_ui.py` |
| UI-05 Shortcuts, actions and busy revalidation | pending<br>behavior-preserving-port | `main/qt/main_window.py:1183` `_shortcut_compile`<br>`main/qt/toolbar.py:1401` `ControlsBar`<br>`main/web_bridge.py:2477` `run_action`<br>`verify_actions.py`, `verify_controls.py`, `verify_cursor.py`, `verify_ubuntu_event_filters.py` |
| UI-06 Settings transactions and preferences | partial-fixture-only<br>behavior-preserving-port | `main/core/config.py:185` `_save_raw_config`<br>`main/core/config_store.py:23` `merge_changes`<br>`main/qt/settings_dialog.py:836` `_save_and_apply`<br>`verify_preferences.py`, `verify_sessions.py` |
| UI-07 Independent projects and cross-process activation | partial-fixture-only<br>behavior-preserving-port | `main/web_bridge.py:2670` `open_project_window`<br>`main/qt/project_activation.py:7` `install_activation_server`<br>`main/core/config.py:1`<br>`verify_sessions.py`, `verify_projects.py` |
| UI-08 Close, Stop and repair/restart handoff | pending<br>behavior-preserving-port | `main/qt/main_window.py:1743` `closeEvent`<br>`main/qt/main_window.py:1200` `restart_for_runtime_repair`<br>`main/web_bridge.py:9515` `stop_operation`<br>`verify_operation_close.py`, `verify_operation_lifecycle.py`, `verify_runtime.py` |
| UI-09 Notifications, history, telemetry and log budgets | pending<br>behavior-preserving-port | `main/qt/notif_panel.py:1`<br>`main/qt/signals.py:1`<br>`main/web_bridge.py:795` `emit`<br>`main/qt/garbage_collection.py:1`<br>`verify_notification_persistence.py`, `verify_notification_writes.py`, `verify_gui_lifetime.py`, `verify_qt_binding_lifetime.py` |

### Project

| ID and capability | Status / disposition | Legacy source and verifier references |
| --- | --- | --- |
| PRJ-01 Existing/new/recent/cloud selector and disposal | pending<br>behavior-preserving-port | `main/qt/project_dialog.py:1`<br>`main/web_bridge.py:2552` `open_project`<br>`main/web_bridge.py:2511` `validate_existing_project_folder`<br>`verify_projects.py`, `verify_cloud_sketch_ui.py`, `verify_project_file_lifecycle.py` |
| PRJ-02 Create-only scaffold and checked header includes | partial-fixture-only<br>behavior-preserving-port | `main/core/project_scaffold.py:15` `create_project_scaffold`<br>`main/web_bridge.py:2832` `create_project`<br>`verify_projects.py`, `verify_project_file_lifecycle.py` |
| PRJ-03 Root manifests, source reads and acknowledged saves | partial-fixture-only<br>behavior-preserving-port | `main/core/file_utils.py:1473` `get_project_root_source_files`<br>`main/web_bridge.py:1121` `read_file`<br>`main/web_bridge.py:1135` `save_file`<br>`main/qt/editor_panel.py:101` `finish_save_request`<br>`verify_storage_io.py`, `verify_cloud_editor_manifest.py`, `verify_runtime.py` |
| PRJ-04 Targeted Modify Add/Rename/Delete and AI context | pending<br>behavior-preserving-port | `main/web_bridge.py:2386` `add_project_file`<br>`main/web_bridge.py:2418` `rename_project_file`<br>`main/web_bridge.py:2449` `delete_project_file`<br>`main/web_bridge.py:2361` `_project_files_changed`<br>`src/editor/project_files.js:1`<br>`verify_project_file_lifecycle.py`, `verify_prompt_sources.py` |
| PRJ-05 Recent history, native pickers and folder opening | pending<br>behavior-preserving-port | `main/web_bridge.py:2867` `get_recent_projects`<br>`main/web_bridge.py:2871` `clear_recent_projects`<br>`main/web_bridge.py:2772` `open_file_picker`<br>`main/web_bridge.py:2880` `open_in_explorer`<br>`verify_projects.py`, `verify_actions.py` |
| PRJ-06 Metadata, generated instructions and safe Clean | pending<br>behavior-preserving-port | `main/core/file_utils.py:1238` `ensure_hidden_read_first_md`<br>`main/core/file_utils.py:1539` `hide_internal_project_metadata`<br>`main/web_bridge.py:9378` `clean_cache`<br>`verify_hygiene.py`, `verify_storage_io.py`, `verify_operation_lifecycle.py` |

### Editor

| ID and capability | Status / disposition | Legacy source and verifier references |
| --- | --- | --- |
| ED-01 Offline Monaco bundle, workers and font | partial-fixture-only<br>host-adapter | `src/editor/index.html:1`<br>`src/editor/bundle.js:1`<br>`src/editor/18.bundle.js:1`<br>`src/editor/editor.worker.js:1`<br>`verify_editor_resources.js`, `verify_editor_review_ui.py` |
| ED-02 Models, tab order, dirty snapshots and renderer recovery | partial-fixture-only<br>host-adapter | `main/qt/editor_panel.py:407` `begin_renderer_load`<br>`main/qt/editor_panel.py:382` `update_renderer_buffer`<br>`main/qt/editor_panel.py:418` `complete_renderer_recovery`<br>`src/editor/project_files.js:1`<br>`verify_editor_recovery.js`, `verify_runtime.py`, `verify_project_file_lifecycle.py` |
| ED-03 Autosave, reload and language preferences | pending<br>host-adapter | `main/qt/editor_panel.py:1012` `trigger_save_all`<br>`main/qt/editor_panel.py:1055` `trigger_reload`<br>`src/editor/index.html:1`<br>`verify_editor_preferences.js`, `verify_editor_c.py` |
| ED-04 Coalesced layout, worker replies and diagnostic navigation | pending<br>host-adapter | `main/qt/editor_panel.py:1261` `force_layout`<br>`main/qt/editor_panel.py:1173` `goto_diagnostic`<br>`main/qt/editor_panel.py:1185` `goto_location`<br>`src/editor/index.html:1`<br>`verify_editor_layout_scheduling.js`, `verify_syntax_navigation.py`, `verify_editor_review_ui.py` |
| ED-05 Find All over dirty root snapshots | pending<br>behavior-preserving-port | `main/qt/project_search.py:1`<br>`main/qt/editor_panel.py:1206` `show_project_search`<br>`verify_project_search.py`, `verify_slow_storage_flow.py` |
| ED-06 AI diff bars, review decisions and visibility | pending<br>host-adapter | `main/core/ai_review.py:41` `build_ai_line_diff`<br>`src/editor/index.html:1`<br>`main/qt/ai_changes_panel.py:1`<br>`verify_ai_line_diff.py`, `verify_editor_review_ui.py` |

### Analysis

| ID and capability | Status / disposition | Legacy source and verifier references |
| --- | --- | --- |
| SYN-01 Bounded syntax checks and source snapshots | pending<br>behavior-preserving-port | `src/syntax_checker.py:1`<br>`main/qt/editor_panel.py:273` `realtime_check_syntax`<br>`main/web_bridge.py:9607` `realtime_check_syntax`<br>`verify_syntax_checker.py`, `verify_syntax_navigation.py`, `verify_performance.py` |
| SYN-02 Target declarations, GPIO/library compatibility and ranking | pending<br>behavior-preserving-port | `main/core/board_compat.py:318` `detect_board_compatibility`<br>`main/web_bridge.py:2355` `filter_compatible_devices`<br>`main/core/target_profile.py:11` `target_problem`<br>`verify_board_declarations.py`, `verify_board_families.py`, `verify_board_search.py`, `verify_target_resolution.py` |

### Target

| ID and capability | Status / disposition | Legacy source and verifier references |
| --- | --- | --- |
| TGT-01 Dynamic installed and downloaded board catalog | pending<br>behavior-preserving-port | `main/core/board_catalog.py:1269` `load_dynamic_boards`<br>`main/web_bridge.py:681` `refresh_board_catalog`<br>`verify_board_declarations.py`, `verify_board_index_targets.py`, `verify_board_search.py` |
| TGT-02 All declared frameworks and programmer transports | partial-fixture-only<br>behavior-preserving-port | `main/core/target_profile.py:11` `target_problem`<br>`main/core/target_profile.py:73` `requires_upload_port`<br>`main/core/target_profile.py:100` `upload_configuration`<br>`verify_target_resolution.py`, `verify_board_families.py`, `verify_board_preparation.py` |
| TGT-03 Board picker, recents, custom mapping and baud | pending<br>behavior-preserving-port | `main/qt/board_dialog.py:1`<br>`main/web_bridge.py:3165` `select_board`<br>`main/web_bridge.py:3197` `set_board_framework`<br>`main/core/target_profile.py:84` `serial_upload_speed`<br>`verify_controls.py`, `verify_custom_board_dialog.py`, `verify_board_search.py`, `verify_upload_logging.py`, `verify_arduino_board_chooser.py` |
| TGT-04 Explicit source-bound Arduino CLI backend | pending<br>behavior-preserving-port | `main/core/arduino_backend.py:154` `run_arduino_operation`<br>`src/modules/arduino_board_selection.py:1`<br>`src/modules/arduino_cli_support.py:1`<br>`verify_arduino_board_selection.py`, `verify_arduino_source_targets.py`, `verify_arduino_source_namespace.py`, `verify_arduino_fallback.py`, `verify_ubuntu_arduino_cli.py` |
| TGT-05 Custom board preparation and full coverage report | pending<br>behavior-preserving-port | `src/modules/board_preparation.py:181` `preparation_plan`<br>`src/modules/bootstrap_board_coverage.py:107` `report_coverage`<br>`verify_board_preparation.py`, `verify_bootstrap_board_coverage.py`, `verify_board_index_targets.py` |

### Build

| ID and capability | Status / disposition | Legacy source and verifier references |
| --- | --- | --- |
| BLD-01 Save-before-build, exact host workspace and library inputs | pending<br>behavior-preserving-port | `main/web_bridge.py:3831` `compile_sketch`<br>`main/web_bridge.py:1734` `_board_workspace_dir`<br>`main/core/arduino_inputs.py:1`<br>`verify_upload_workers.py`, `verify_board_build_cache.py`, `verify_arduino_library_inputs.py` |
| BLD-02 Content-bound receipts and Skip Compile | pending<br>behavior-preserving-port | `main/core/compiled_cache.py:1`<br>`main/web_bridge.py:1586` `check_can_skip_compile_for_upload`<br>`main/web_bridge.py:1547` `update_skip_compile_availability`<br>`verify_board_build_cache.py`, `verify_storage_io.py`, `verify_arduino_library_inputs.py` |
| BLD-03 CPU/RAM/storage policy and host admission | partial-fixture-only<br>behavior-preserving-port | `main/core/build_resources.py:22` `_resource_safe_worker_count`<br>`main/core/storage_resources.py:1`<br>`src/modules/runtime_resources.py:1`<br>`verify_performance.py`, `verify_storage_resources.py`, `verify_storage_io.py`, `verify_slow_storage_flow.py` |
| BLD-04 Logs, progress, warning filter, Copy and auto-scroll | pending<br>behavior-preserving-port | `main/core/upload_log.py:1`<br>`main/qt/console_panel.py:1`<br>`main/qt/log_buffer.py:1`<br>`verify_build_console.py`, `verify_builder_output.py`, `verify_build_log_routing.py`, `verify_upload_logging.py`, `verify_ubuntu_logging.py` |
| BLD-05 Session-safe cancellation, drainage and process ownership | pending<br>behavior-preserving-port | `main/web_bridge.py:3789` `_begin_operation_session`<br>`main/web_bridge.py:9515` `stop_operation`<br>`main/core/process_drain.py:1`<br>`verify_operation_lifecycle.py`, `verify_operation_close.py`, `verify_process_drain.py` |
| BLD-06 Windows short paths and native host tool adapters | pending<br>behavior-preserving-port | `main/core/toolchain.py:1`<br>`main/platforms/windows.py:236` `compiler_working_directory`<br>`main/platforms/ubuntu.py:1`<br>`verify_windows_build_paths.py`, `verify_platforms.py`, `verify_windows_maintenance.py` |
| BLD-07 PlatformIO Python guards and builder compatibility | pending<br>retain-tool-runtime | `src/modules/offline_runtime.py:1`<br>`src/modules/offline_platformio.py:1`<br>`src/modules/windows_tool_paths.py:1`<br>`src/modules/mbed_compat.py:1`<br>`src/modules/zephyr_compat.py:1`<br>`verify_offline.py`, `verify_mbed_compat.py`, `verify_zephyr_compat.py`, `verify_bootstrap_builders.py` |

### Hardware

| ID and capability | Status / disposition | Legacy source and verifier references |
| --- | --- | --- |
| HW-01 Upload dispatch and port reservation | pending<br>behavior-preserving-port | `main/web_bridge.py:6578` `upload_sketch`<br>`main/platforms/windows.py:833` `use_native_upload`<br>`main/platforms/ubuntu.py:113` `use_native_upload`<br>`verify_upload_workers.py`, `verify_board_families.py`, `verify_platforms.py` |
| HW-02 Device loss and declared bootloader USB handoff | pending<br>behavior-preserving-port | `main/core/connection_loss.py:1`<br>`main/web_bridge.py:3797` `_serial_handoff_pending`<br>`verify_connection_loss.py`, `verify_upload_workers.py`, `verify_ubuntu_resets.py` |
| HW-03 DTR/RTS reboot and baud-change reset | pending<br>behavior-preserving-port | `main/web_bridge.py:8386` `reset_mcu`<br>`main/web_bridge.py:3675` `pulse_dtr_reset`<br>`main/web_bridge.py:3227` `set_reset_on_baud_change`<br>`verify_ubuntu_resets.py`, `verify_controls.py`, `verify_actions.py` |
| HW-04 Soft recovery, hard erase and reset cache locks | pending<br>behavior-preserving-port | `main/web_bridge.py:8948` `soft_reset`<br>`main/web_bridge.py:8577` `hard_reset`<br>`main/core/constants.py:131` `board_reset_capabilities`<br>`main/core/config.py:58` `_try_acquire_reset_cache_lock`<br>`soft_reset/README.md:1`<br>`verify_ubuntu_resets.py`, `verify_board_families.py`, `verify_operation_lifecycle.py` |

### Serial

| ID and capability | Status / disposition | Legacy source and verifier references |
| --- | --- | --- |
| SER-01 Port discovery and explicit cross-window selection | pending<br>behavior-preserving-port | `main/web_bridge.py:2966` `refresh_ports`<br>`main/web_bridge.py:3129` `select_port`<br>`main/platforms/ubuntu_ports.py:1`<br>`verify_serial_reader.py`, `verify_ubuntu_ports.py`, `verify_controls.py` |
| SER-02 Incremental decoder and generation-scoped reader | partial-fixture-only<br>behavior-preserving-port | `main/core/serial_stream.py:7` `SerialTextDecoder`<br>`main/web_bridge.py:3270` `_start_serial_monitor`<br>`main/qt/signals.py:1`<br>`verify_serial_reader.py`, `verify_serial_delivery.py`, `verify_serial_view.py` |
| SER-03 Bounded one-attempt Send and input retention | partial-fixture-only<br>behavior-preserving-port | `main/core/serial_send.py:8` `SerialSendQueue`<br>`main/web_bridge.py:3772` `serial_send`<br>`verify_serial_send.py`, `verify_serial_delivery.py` |
| SER-04 Display, ANSI clear, pause, wrap and upload coexistence | pending<br>behavior-preserving-port | `main/qt/serial_panel.py:1`<br>`main/web_bridge.py:3638` `_stop_serial_monitor`<br>`main/web_bridge.py:374` `set_baud_rate`<br>`verify_serial_view.py`, `verify_serial_wrap.py`, `verify_serial_delivery.py`, `verify_connection_loss.py` |

### Terminal

| ID and capability | Status / disposition | Legacy source and verifier references |
| --- | --- | --- |
| PTY-01 Reused xterm renderer, fit and control protocol | partial-fixture-only<br>host-adapter | `src/editor/terminal.html:1`<br>`src/editor/opencode.html:1`<br>`src/assets/xterm/xterm.js:1`<br>`verify_terminal_scroll.py`, `verify_terminal_startup.py`, `verify_posix_terminal_state.py` |
| PTY-02 Native PTY/process tree and consumed-output flow control | partial-fixture-only<br>behavior-preserving-port | `main/platforms/ubuntu_pty_process.py:1`<br>`main/platforms/ubuntu_pty_supervisor.py:1`<br>`main/qt/posix_terminal_panel.py:141` `consumed`<br>`src/modules/project_terminal.py:1`<br>`verify_ubuntu_pty_lifecycle.py`, `verify_native_pty_close.py`, `verify_terminal.py`, `verify_posix_terminal_state.py` |
| PTY-03 Multiple shells/sessions, focus and Clear | pending<br>behavior-preserving-port | `main/qt/terminal_panel.py:694` `add_session`<br>`main/qt/posix_terminal_panel.py:251` `add_session`<br>`src/modules/project_terminal.py:1`<br>`verify_terminal.py`, `verify_terminal_startup.py`, `verify_terminal_scroll.py` |

### Assistant

| ID and capability | Status / disposition | Legacy source and verifier references |
| --- | --- | --- |
| AI-01 OpenCode native runtime and assistant panel | pending<br>behavior-preserving-port | `main/qt/ai_panel.py:1`<br>`main/qt/posix_ai_panel.py:1`<br>`main/platforms/ubuntu_opencode.py:1`<br>`src/modules/dedicated_AI.py:1`<br>`verify_ubuntu_ai_panel.py`, `verify_ubuntu_opencode.py`, `verify_ubuntu_opencode_setup.py` |
| AI-02 Prompt provenance, AI watcher and protected recovery | pending<br>behavior-preserving-port | `src/modules/ai_prompt_context.py:1`<br>`main/core/ai_review.py:856` `AIEditWatcher`<br>`main/core/file_utils.py:829` `AIEditBackupStore`<br>`verify_prompt_sources.py`, `verify_ai_changes.py`, `verify_project_file_lifecycle.py` |
| AI-03 Accept/Reject, Undo/Redo and bounded history | pending<br>behavior-preserving-port | `main/qt/editor_panel.py:497` `accept_ai_edit`<br>`main/qt/editor_panel.py:522` `reject_ai_edit`<br>`main/qt/editor_panel.py:561` `undo_ai_edit_decision`<br>`main/qt/ai_changes_panel.py:1`<br>`verify_ai_changes.py`, `verify_ai_line_diff.py`, `verify_editor_review_ui.py` |

### Cloud

| ID and capability | Status / disposition | Legacy source and verifier references |
| --- | --- | --- |
| CLD-01 Fixed provider, auth and native credential store | pending<br>behavior-preserving-port | `main/core/firebase_provider.py:1`<br>`main/core/credential_store.py:36` `CredentialStore`<br>`main/core/cloud_sketch_service.py:575` `sign_in`<br>`main/core/cloud_sketch_service.py:620` `create_account`<br>`main/core/cloud_sketch_service.py:701` `sign_out`<br>`verify_cloud_configuration.py`, `verify_cloud_sketch_service.py`, `verify_offline_mode.py` |
| CLD-02 Cloud list/revisions/rename/delete and upload duplicates | pending<br>behavior-preserving-port | `main/core/cloud_sketch_service.py:759` `list_sketches`<br>`main/core/cloud_sketch_service.py:1012` `rename_sketch`<br>`main/core/cloud_sketch_service.py:1032` `delete_sketch`<br>`main/core/cloud_sketch_service.py:1041` `upload_project`<br>`main/core/cloud_sketch_service.py:1106` `list_revisions`<br>`verify_cloud_sketch_service.py`, `verify_cloud_sketch_ui.py`, `verify_cloud_rules.js` |
| CLD-03 Independent linked copies and explicit Push/Pull | pending<br>behavior-preserving-port | `main/core/cloud_sketch_service.py:896` `working_directory`<br>`main/core/cloud_sketch_service.py:1084` `push_project`<br>`main/core/cloud_sketch_service.py:1121` `pull_project`<br>`main/qt/cloud_sketch_panel.py:1`<br>`verify_cloud_sketch_service.py`, `verify_cloud_sketch_ui.py`, `verify_ubuntu_cloud_workflow.py` |
| CLD-04 Strict full-model Pull refresh and Retry lock | pending<br>host-adapter | `main/qt/editor_panel.py:818` `reload_cloud_snapshot`<br>`main/qt/editor_panel.py:105` `finish_cloud_reload`<br>`main/web_bridge.py:1108` `get_cloud_project_files`<br>`verify_cloud_editor_manifest.py`, `verify_runtime.py`, `verify_ubuntu_cloud_workflow.py` |

### Developer Portal

| ID and capability | Status / disposition | Legacy source and verifier references |
| --- | --- | --- |
| DEV-01 Five-click entry, local key and authenticated identity | pending<br>behavior-preserving-port | `main/qt/toolbar.py:54` `_DeveloperLogoLabel`<br>`main/core/owner_tickets.py:370` `authenticate`<br>`main/core/owner_tickets.py:344` `configure_local_access`<br>`main/core/owner_tickets.py:462` `logout`<br>`verify_owner_tickets.py`, `verify_owner_ticket_service.py`, `verify_offline_mode.py` |
| DEV-02 Tickets, categories, dashboard and isolated cache | pending<br>behavior-preserving-port | `main/core/owner_tickets.py:570` `create_ticket`<br>`main/core/owner_tickets.py:616` `update_ticket`<br>`main/core/owner_tickets.py:645` `delete_ticket`<br>`main/core/owner_tickets.py:297` `add_ticket_category`<br>`main/core/owner_tickets.py:670` `_ticket_cache_path`<br>`verify_owner_tickets.py`, `verify_owner_ticket_service.py` |
| DEV-03 Intentional neobrutalist portal surfaces | pending<br>host-adapter | `main/qt/owner_ticket_style.py:1`<br>`main/qt/owner_ticket_dialog.py:1`<br>`verify_owner_tickets.py`, `verify_ui_layout.py`, `verify_svg_icons.py` |

### Resources

| ID and capability | Status / disposition | Legacy source and verifier references |
| --- | --- | --- |
| DL-01 Cached catalogs, details, versions and latest search | pending<br>behavior-preserving-port | `src/modules/arduino_lib_req.py:1560` `BrowseTab`<br>`src/modules/arduino_lib_req.py:1526` `_read_library_catalog`<br>`src/modules/arduino_lib_req.py:1536` `_read_board_catalog`<br>`src/modules/browser_loading.py:1`<br>`verify_browser_loading.py`, `verify_catalog_io.py`, `verify_board_search.py` |
| DL-02 Additional indexes/custom package associations | pending<br>behavior-preserving-port | `src/modules/arduino_lib_req.py:154` `parse_additional_board_urls`<br>`src/modules/arduino_lib_req.py:98` `_normalize_board_manager_url`<br>`src/modules/board_preparation.py:1`<br>`verify_board_index_targets.py`, `verify_custom_board_dialog.py`, `verify_board_preparation.py` |
| DL-03 Checksummed resumable downloads and safe promotion | pending<br>behavior-preserving-port | `src/modules/arduino_lib_req.py:930` `_download_package_archive`<br>`src/modules/arduino_lib_req.py:895` `_verify_download`<br>`src/modules/arduino_lib_req.py:1204` `_extract_archive`<br>`src/modules/arduino_lib_req.py:1330` `_promote_directory`<br>`verify_download_interruption.py`, `verify_bootstrap_archives.py`, `verify_ubuntu_download_manager.py` |
| DL-04 Installed automatic extracted-folder update and samples | pending<br>behavior-preserving-port | `src/modules/arduino_lib_req.py:1905` `InstalledTab`<br>`src/modules/arduino_lib_req.py:2515` `_update_item`<br>`src/modules/arduino_lib_req.py:2305` `_open_example`<br>`src/modules/arduino_lib_req.py:2499` `_open_folder`<br>`src/modules/arduino_lib_req.py:2529` `_delete_item`<br>`verify_library_samples.py`, `verify_browser_loading.py`, `verify_controls.py` |
| DL-05 Glass downloader tabs, scrolling and direct URL/Drive | pending<br>host-adapter | `src/modules/tk_glass.py:1`<br>`src/modules/tk_notebook.py:1`<br>`src/modules/tk_material_list.py:1`<br>`src/modules/downloader.py:1`<br>`verify_downloader_appearance.py`, `verify_downloader_performance.py`, `verify_responsive.py` |

### Packages

| ID and capability | Status / disposition | Legacy source and verifier references |
| --- | --- | --- |
| PKG-01 Store leases, jobs, reports and progress UI | pending<br>behavior-preserving-port | `src/modules/package_jobs.py:286` `package_store_lease`<br>`src/modules/package_jobs.py:79` `publish_event`<br>`main/qt/package_progress.py:1`<br>`main/qt/package_coverage.py:1`<br>`verify_package_jobs.py`, `verify_platformio_locks.py`, `verify_package_progress.py`, `verify_package_coverage.py` |

### Setup

| ID and capability | Status / disposition | Legacy source and verifier references |
| --- | --- | --- |
| SET-01 Host admission and private runtime boundary | pending<br>behavior-preserving-port | `src/modules/ubuntu_release.py:1`<br>`src/modules/ubuntu_architecture.py:1`<br>`src/modules/private_python_guard.py:1`<br>`src/modules/runtime_resources.py:1`<br>`verify_ubuntu_release.py`, `verify_ubuntu_architecture.py`, `verify_private_runtimes.py`, `verify_ubuntu_first_run.py`, `verify_application_guard.py`, `verify_ubuntu_runtime.py` |
| SET-02 Online preparation then offline installed-only work | pending<br>retain-tool-runtime | `src/modules/offline_bootstrap.py:1`<br>`src/modules/offline_mode.py:1`<br>`src/modules/offline_runtime.py:1`<br>`direct/offline-packages.json:1`<br>`verify_offline.py`, `verify_offline_mode.py`, `verify_bootstrap_cold_cache.py`, `verify_bootstrap_seed.py` |
| SET-03 Bootstrap stages, archives, cancellation and certification | pending<br>behavior-preserving-port | `src/modules/bootstrap_platformio.py:1`<br>`src/modules/bootstrap_dispatch.py:1`<br>`src/modules/bootstrap_seed.py:1`<br>`src/modules/bootstrap_arduino_sources.py:1`<br>`verify_bootstrap_builders.py`, `verify_bootstrap_interruption.py`, `verify_bootstrap_processes.py`, `verify_bootstrap_archives.py`, `verify_bootstrap_promotion.py`, `verify_bootstrap_arduino_sources.py`, `verify_bootstrap_download_failure.py`, `verify_bootstrap_headers.py`, `verify_bootstrap_updates.py` |
| SET-04 Native installers, repair, Fresh and maintenance | pending<br>behavior-preserving-port | `direct/windows/run.vbs:1`<br>`direct/ubuntu/setup.py:1`<br>`direct/ubuntu/system_setup.py:1`<br>`cleaner/ubuntu/maintenance.py:1`<br>`src/modules/launcher.py:1`<br>`verify_windows_board_setup.py`, `verify_windows_maintenance.py`, `verify_ubuntu_maintenance.py`, `verify_ubuntu_system_setup.py`, `verify_ubuntu_arm_bootstrap.py`, `verify_bootstrap_native.py`, `verify_bootstrap_pip_paths.py`, `verify_ubuntu_bootstrap.py` |
| SET-05 Packaging, launcher, CI and rollback qualification | pending<br>host-adapter | `direct/build_release.py:1`<br>`direct/ubuntu/build_launcher.sh:1`<br>`.github/workflows/compatibility.yml:1`<br>`.github/workflows/ubuntu-arm64.yml:1`<br>`.github/workflows/ui-layout.yml:1`<br>`verify_ubuntu_launcher.py`, `verify_ubuntu_first_run.py`, `verify_platforms.py`, `verify_ui_layout.py` |

## Completion rule

A feature can replace its original only after matching success/refusal/cancellation/contention/stale-reply fixtures, required native host/transport behavior, and safe coexistence/rollback. Keep the legacy application available until all required entries pass. Approved PlatformIO-integrated Python stays in the private firmware-tool runtime; temporary application adapters need an explicit removal gate.

The inventory includes public backend/editor APIs, event names, renderer interface candidates and discovered verifier case names. Static AST/regex cannot prove dynamic routes or complete behavioral equivalence. The four initial physical boards and absence of a confirmed external programmer do not reduce supported-board/framework/programmer parity.

Reference-verifier follow-up `f952325` handles declared native ESP32 legacy and
unified compiler layouts after the Espressif32 8 package rename;9 synthetic and
16 target-resolution checks pass. Actual fresh native setup/ESP32 Compile/runtime passes on both ARM64 LTS hosts;
all 14 compatibility jobs pass. Exact source receipts remain in ci-results.json. Rust/Tauri source remains 1131c7e.
