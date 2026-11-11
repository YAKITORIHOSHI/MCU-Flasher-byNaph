# Editor, input, tickets and installation verification

Approved task: November 10, 2026. Shipping application: Python/PySide6.
Native Windows 11 verification is **pending**. The user selected available
Windows CI with that limitation recorded explicitly. Hosted Windows Server,
Linux mocks and software rendering do not establish Windows 11 desktop input,
mixed-monitor behavior, physical USB or every backup application's integration.

| Approved item | Implementation | Verification |
| --- | --- | --- |
| Windows assistant/coding CLI keeps keyboard input | Owned native queue handoff on input clicks/keyboard moves; activation and containers cannot take focus | `verify_native_input`, isolated native Windows CI; Windows 11 pending |
| Assistant right-side gap | Actual client bounds and xterm's measured scrollbar width | `verify_embedded_viewports`, `verify_assistant_startup`, `verify_responsive` |
| Create/edit ticket spinner | Retained hidden size; footer vertical alignment | `verify_owner_tickets`, captured create/edit busy states |
| Overloads in Monaco and sample viewer | Shared disposable LLVM parser; real includes/defines/flags from selected build hook | `verify_source_navigation` with real libclang; `verify_editor_features`, `verify_library_samples` |
| Header/library navigation | Explicit include graph; exact returned range; one locked temporary tab | Same editor checks; source saves reject external preview paths |
| Terminal spacing | Minimum header height and explicit close-button allocation | Responsive, native layouts and monitor matrix |
| Terminal/Notifications close marks | Explicit terminal tab painting on Windows/POSIX; shared dock close icon | `verify_svg_icons` renders themes and exercises close actions |
| Developer Access dropdowns/severity | Explicit paper popup mode; centered severity row | `verify_combo_popups`, `verify_owner_tickets`, native layouts |
| Remove custom categories | Serialized changes; fresh local/remote usage checks; reassignment required; stale assignments revalidated; no ticket rewrites | `verify_owner_ticket_service`, create/edit removal and retained drafts |
| Ticket ordering | Newest-created default; oldest/recently updated choices | Service timestamp and actual UI ordering checks |
| Backup/file locks | Configured sync roots use local owned build storage; bounded retries, atomic source saves, accurate read-only blocker diagnostics in an owned Windows job | `verify_file_access`, acknowledged-save race checks; external backup products remain environment-dependent |
| Upload unexpectedly disabled | Authoritative backend/live-worker/save/recovery guards, reason tooltips, post-save refresh | `verify_controls`, existing upload workers/logging/cache tests |
| Persistent Ubuntu install | Verified source staging; runtime prepared at final XDG location; one menu entry after complete setup | `verify_user_install`, `verify_ubuntu_bootstrap`, launcher handoff fixtures |
| Configurable pairing | Monaco language-defined pairing, wrapping, overtype/delete, escaping, indentation, undo and multiple cursors | Seven real offline Monaco integration checks; live Settings signal |
| Guidance and rollout | README, release notes, Ubuntu guide, canonical skills/wrappers and generated application guidance aligned | Syntax/diff checks; exact-commit archive and published-byte verification gates |

## Local evidence

Ubuntu 26.04.1 LTS AMD64, private application runtime. All GUI fixtures remove
DISPLAY/WAYLAND_DISPLAY and run offscreen, with isolated storage, mocked accounts,
hardware and installers. Real semantic tests use a separate disposable runtime
with the pinned libclang 18.1.1 wheel; no live runtime repair is used as a test.

- Ten real LLVM/conservative parser checks pass, covering overload argument
  types, scope, namespaces, members, templates, defaults, nested arguments,
  unsaved headers, Unicode ranges, actual defines, unsupported target fallback
  and retirement of owned parser processes at their deadline.
- Seven real offline Monaco checks pass for pairing, quoting, indentation,
  read-only previews, navigation history, stale replies and save races/off-Qt I/O.
- Nine installation checks pass, including no menu before complete validation,
  retained failed stages, changed source bytes, preference preservation, reuse
  without downgrade, no copied virtual environment and retained fixture-only
  home isolation when subsequent host checks reuse the installed application.
- Thirty-three developer portal checks pass, including new sort/removal,
  spinner geometry, severity alignment and wrapped compact layouts. Captures
  were inspected for new/edit busy footers, ticket cards and the paper list.
- Existing runtime, controls, responsive, performance, shutdown, package browser,
  glass, interface size, taskbar, renderer depth, assistant startup, viewport,
  incremental builds, upload workers/logging, board cache, bootstrap/update,
  storage, syntax navigation, cursor, terminal startup and Ubuntu assistant/PTY
  checks pass in the focused regression batches.

`verify_ubuntu_launcher` retains three baseline failures referring to removed
root `MCU_Flasher` executable entries. A clean HEAD export reproduces all three;
the public executable entries remain under `LAUNCHER/`. They are not attributed
to this update. The new persistent-install/setup fixtures pass independently.

## Hosted setup continuation

The corrected Ubuntu first-run fixture preserves terminal versus desktop launch
mode through the persistent-install handoff, observes the final installed runtime,
and verifies the All Apps entry. The available native ARM64 run completed real
Bootstrap, actual Monaco/workspace startup and an ESP32 Compile-button build
(291,504 firmware bytes). This uses an owned disposable application/store and no
physical device. The subsequent [native ARM64 run on both Ubuntu LTS releases](https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph/actions/runs/42874900026)
passes all host, workspace, sample-viewer, terminal, cold-setup and real compile
gates. Keyboard-oriented sample fixtures explicitly activate their owned window
before injecting shortcuts.

The older Windows Python 3.11/Qt readability fixture reproduces a native access
violation on the baseline as well as this branch. Preserve that platform result
separately from the new close-glyph and keyboard-queue checks.

## Release gates

Use `.github/workflows/editor-improvements.yml` for Windows Python 3.11/3.14 and
Ubuntu 24.04/26.04 checks. The Windows fixture creates its own foreign-process
window, performs actual input-queue transfer and verifies injected keys reach the
clicked editor/settings/terminal input without reaching the embedded input.
It runs only in isolated GitHub Actions and reports the actual Windows version.
The native terminal protocol verifier runs separately.

The private Windows runtime includes the verified AMD64 libclang wheel and its
license. Its wheel SHA-256 is
`4dd2d3b82fab35e2bf9ca717d7b63ac990a3519c7e312f19fa8e86dcc712f7fb`.
Package committed defaults through `direct/build_release.py`, retain the complete
private runtime, validate all seven LFS installer receipts directly from committed Git blobs
(even when git archive expands their data) and every archived file,
and preserve the existing public asset until its staged replacement is verified.
The source commit, final archive manifest and published size/SHA-256 form the
release receipt; none of these checks qualifies native Windows 11 by inference.

## November 11 follow-up review

The earlier gates (native_input, cursor, source_navigation, editor_features,
file_access, user_install, owner tickets, library_samples, controls, svg_icons,
detached_actions, terminal_scroll, release_sources, app_update and
bootstrap_updates) were rerun and passed before further changes. Real use still
showed four gaps that those fixtures could not see:

| Report | Cause | Change and evidence |
| --- | --- | --- |
| Developer Access updates never reached installed copies | The uploaded v11 ZIP was packed by hand and recorded `7710ca7`, a commit amended before pushing. Ubuntu installs copied the same tracked record. GitHub's comparison answered 404, so every update check ended "unavailable". The changes also existed only on the feature branch. | Records carry `committed_at`; 404/422 falls back to that date; installs from a checkout record HEAD; the tracked record names a published commit; the ZIP is rebuilt by `build_release.py`; `main` carries the work. `verify_app_update`, `verify_user_install`, `verify_release_sources` |
| Empty "Choose a declaration" box | Monaco's definition provider (called on Ctrl+hover) opened the chooser even with no results, and nearly every result was marked ambiguous because headers contain include guards | Provider has no side effects; one candidate opens directly; includes and installed-library declarations resolve before compiling. `verify_source_navigation`, `verify_editor_features` |
| Invisible terminal close mark | The selected tab's mark used chip ink while the POSIX session bar painted its selected tab dark; the earlier fixture tested a stand-in dock widget | Shared `terminal_tab_rules`; contrast measured against the painted tab in all themes. `verify_svg_icons` |
| Frozen editor spinner | Six-core PCs run in resource-constrained mode, which removed every CSS animation | The loading spinner is exempt; decorative motion still stops. `verify_editor_features` |

Native Windows 11 verification of keyboard routing remains pending, as before.
