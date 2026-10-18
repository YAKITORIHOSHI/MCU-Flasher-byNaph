# Existing resource reuse

The build stages the following existing resources byte-for-byte. Generated copies
are ignored by Git; `pnpm assets` recreates them from this checkout. The generated
`public/vendor/manifest.json` records source paths, sizes, and SHA-256 values.
No Monaco or xterm package is downloaded at application runtime.

| Resource | Existing source | Treatment |
| --- | --- | --- |
| Monaco application bundle, C/C++ chunk, worker, codicon font | `src/editor/` | Unchanged. Minimal host supplies the bundle's existing DOM anchors. Its Python/Qt bridge is not installed; models are owned through public Monaco APIs. |
| xterm renderer, CSS, fit addon | `src/assets/xterm/` | Unchanged; controlled demonstration output only, with bounded scrollback and consumption acknowledgement. |
| Montserrat | `src/fonts/Montserrat/` | Existing regular/medium/semibold/bold fonts. SIL Open Font License copied into generated assets. |
| Action marks | `src/assets/icons/actions/` | Existing original SVG family, used as masks with theme-derived ink. |
| Theme values | `src/editor/index.html` | Default, light and Solarized editor palette values translated into typed theme definitions. |

Monaco Editor and xterm.js are MIT-licensed upstream projects. Their vendored
artifacts do not currently carry complete source/version/license provenance in
this repository. This prototype does not infer an exact version from minified
bytes. **Release packaging is gated on resolving the original artifact versions,
retaining the corresponding complete copyright/license notices, and reviewing
all transitive notices.** Existing repository licensing continues to apply to
application-owned resources. This document records reuse, not release clearance.

Upstream references: https://github.com/microsoft/monaco-editor/blob/main/LICENSE
and https://github.com/xtermjs/xterm.js/blob/master/LICENSE.
