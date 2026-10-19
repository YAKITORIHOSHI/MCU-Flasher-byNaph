# Existing resource reuse and provenance

Reviewed 19 October 2026 against migration code commit `4cf730b`. This is a
prototype provenance record. Public release packaging remains blocked on the
unresolved notices and original artifact provenance below.

## Byte preservation

`desktop/scripts/stage-assets.mjs` copies **22 unique existing source files into
25 web-resource destinations**. Three additional root-level copies of the C/C++
chunk, editor worker and codicon font preserve the original webpack URLs. The
stager changes neither minified source nor font/icon bytes. Generated copies are
ignored by Git and recreated by `pnpm --dir desktop/ui assets`.

The generated `desktop/ui/public/vendor/manifest.json` identifies every source
and destination with its size and SHA-256. All 25 destination hashes match their
source bytes and manifest entries in this review. No Monaco, xterm, font or icon
is downloaded at application runtime. The generated manifest describes the
current checkout bytes; a checkout's text line endings can change text hashes.

The tracked code audit, `audit/migration/source-inventory.json`, separately
preserves the original 300 source-file entries, 133 reusable-resource entries and
123 legacy verifier entries. Intentional amendments cover two production
surfaces (Tk dialog fitting and legacy editor focus), seven legacy verifiers and
CI dependencies; their changed hashes are recorded separately in
`audit/migration/reference-amendments.json`. All 22 staged asset sources remain
unchanged by these amendments. The edited legacy `src/editor/index.html` is a
behavior/theme reference, rather than one of the copied web-resource inputs.
Those are separate inventory sections, not a count of unique assets or executed
verification cases. Relative to the original `1d00ff0` Git blobs, all staged
binary/JavaScript/CSS/SVG source bytes are unchanged. `Montserrat/OFL.txt` has only
working-tree CRLF versus Git LF line endings; normalized license text matches.

## Reused resources

| Resource | Existing source | Evidence and treatment |
| --- | --- | --- |
| Monaco application bundle, C/C++ chunk and worker | `src/editor/bundle.js`, `18.bundle.js`, `editor.worker.js` | Unchanged existing artifacts, including the bundle's application initializer. The minimal host supplies its original DOM anchors; the Python/Qt bridge is absent. Models use public Monaco APIs through the new adapter. Exact Monaco/transitive package versions and original build inputs remain unverified. |
| Codicon font | `src/editor/8f3abbcbc983396e1f13.ttf` | SFNT metadata identifies family `codicon`, internal `Version 1.11`. This does **not** identify a Monaco or npm package release. The original artifact's applicable font/icon attribution remains to be resolved. |
| xterm renderer, CSS and fit addon | `src/assets/xterm/xterm.js`, `xterm.css`, `xterm-addon-fit.js` | Unchanged renderer assets. Exact renderer/addon package versions are unverified. Existing CSS preserves the complete MIT text and notices naming the xterm.js authors, Christopher Jeffrey and Fabrice Bellard. The prototype awaits local renderer consumption and caps the fixed child at 64 KiB. Native renderer-to-host acknowledgements now gate each fixed-child chunk after consumption. General interactive-session transport and input/capability parity remain pending. |
| Montserrat regular, medium, semibold and bold fonts | `src/fonts/Montserrat/static/` | Four existing font files plus the existing `OFL.txt` are copied unchanged. The regular font's name table reports `Version 9.000`; that metadata does not establish the original download/package identity. Its embedded license identifies SIL OFL 1.1. The supplied OFL file credits the Montserrat project and includes the full license. |
| Action marks | `src/assets/icons/actions/` | Ten existing SVGs: brand, project, add, save, reload, terminal, info, detach, file and clear. Theme ink is applied by CSS masks. Their geometry is unchanged. Application-owned assets remain under the repository's MIT license and copyright notice. |
| Theme values | `src/editor/index.html` | Default, light and Solarized editor values are translated into `src/themes.ts`. The full legacy page is the behavioral reference; it is not copied or executed in the prototype. |
| Native application icon | `src/assets/mcu_icon.ico` | Outside the 25-copy web manifest: the existing embedded PNG was extracted into `desktop/src-tauri/icons/icon.png`. `audit/migration/icon-provenance.json` records source and extracted-image hashes. It is an extraction, not a newly designed icon. |

## Outstanding release requirements

- The Monaco bundle and C/C++ chunk explicitly refer to `bundle.js.LICENSE.txt`
  and `18.bundle.js.LICENSE.txt`; neither companion exists in the current source
  tree. Recover the original matching notices and the original build/package
  lockfile or equivalent artifact provenance. Inspect bundled dependencies and
  embedded artwork as well as Monaco's top-level MIT notice.
- xterm JavaScript and the fit addon refer to source maps that are absent. A
  source map is not itself a license, and its absence does not establish a
  violation; the available files do not establish exact package provenance.
  Identify the original renderer/addon releases and retain their complete
  applicable copyright/license notices.
- Codicons must be reviewed separately from Monaco code. Current upstream
  Codicons distinguishes Creative Commons Attribution 4.0 content from MIT code.
  Confirm the notices applicable to this exact font artifact, preserve required
  attribution and license text, and record its provenance. Do not assume the
  surrounding Monaco MIT license covers every font/icon asset.
- Retain the existing Montserrat OFL text and applicable font copyright notices
  in packaged artifacts. Do not replace an artifact-specific notice with a
  current upstream notice merely because the projects have the same name.
- Inventory notices for the newly pinned Rust/npm dependencies and generated
  release bundle. Existing application MIT licensing does not replace any
  third-party requirements.

Upstream references identify current project policy, not the provenance of these
older copied bytes: [Monaco MIT license](https://github.com/microsoft/monaco-editor/blob/main/LICENSE.txt),
[xterm license](https://github.com/xtermjs/xterm.js/blob/master/LICENSE),
[Codicons legal notices](https://github.com/microsoft/vscode-codicons#legal-notices),
and [Montserrat OFL](https://github.com/JulietaUla/Montserrat/blob/master/OFL.txt).
The bundled [font license](../../src/fonts/Montserrat/OFL.txt) and
[application license](../../LICENSE) remain the local evidence.
