import { createHash } from 'node:crypto';
import { copyFile, mkdir, readFile, writeFile } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const repo = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const output = resolve(repo, 'desktop/ui/public/vendor');
const publicRoot = resolve(repo, 'desktop/ui/public');
const sources = [
  'src/editor/bundle.js', 'src/editor/18.bundle.js',
  'src/editor/editor.worker.js', 'src/editor/8f3abbcbc983396e1f13.ttf',
  'src/assets/xterm/xterm.js', 'src/assets/xterm/xterm.css',
  'src/assets/xterm/xterm-addon-fit.js',
  'src/fonts/Montserrat/static/Montserrat-Regular.ttf',
  'src/fonts/Montserrat/static/Montserrat-Medium.ttf',
  'src/fonts/Montserrat/static/Montserrat-SemiBold.ttf',
  'src/fonts/Montserrat/static/Montserrat-Bold.ttf',
  'src/fonts/Montserrat/OFL.txt',
  ...['brand', 'project', 'add', 'save', 'reload', 'terminal', 'info', 'detach', 'file', 'clear']
    .map(name => `src/assets/icons/actions/${name}.svg`),
];
const records = [];
for (const source of sources) {
  const relative = source.slice(4);
  const destination = resolve(output, relative);
  const bytes = await readFile(resolve(repo, source));
  await mkdir(dirname(destination), { recursive: true });
  await copyFile(resolve(repo, source), destination);
  records.push({ source, destination: `vendor/${relative}`, bytes: bytes.length,
    sha256: createHash('sha256').update(bytes).digest('hex') });
  // The proven webpack bundle uses a page-relative public path for its lazy
  // C/C++ chunk, worker and codicon font. Keep those URLs valid without editing
  // minified code or changing the original repository resources.
  if (['src/editor/18.bundle.js', 'src/editor/editor.worker.js', 'src/editor/8f3abbcbc983396e1f13.ttf'].includes(source)) {
    const filename = source.split('/').at(-1);
    await copyFile(resolve(repo, source), resolve(publicRoot, filename));
    records.push({ source, destination: filename, bytes: bytes.length,
      sha256: createHash('sha256').update(bytes).digest('hex'), role: 'original-webpack-url' });
  }
}
// Notices recovered from integrity-checked npm archives matching the retained
// xterm JavaScript bytes. Preserve originals, including the CSS attribution.
for (const filename of ['xterm-5.3.0.LICENSE.txt', 'xterm-addon-fit-0.8.0.LICENSE.txt', 'xterm-css-5.3.0.NOTICE.txt']) {
  const source = `desktop/ui/notices/${filename}`;
  const destination = `vendor/notices/${filename}`;
  const bytes = await readFile(resolve(repo, source));
  await mkdir(dirname(resolve(publicRoot, destination)), { recursive: true });
  await copyFile(resolve(repo, source), resolve(publicRoot, destination));
  records.push({ source, destination, bytes: bytes.length,
    sha256: createHash('sha256').update(bytes).digest('hex'), role: 'matching-upstream-notice' });
}
await writeFile(resolve(output, 'manifest.json'), JSON.stringify({
  schemaVersion: 1, generatedBy: 'desktop/scripts/stage-assets.mjs',
  policy: 'Byte-identical copies of existing repository resources; no runtime network fetch.',
  files: records,
}, null, 2) + '\n');
console.log(`Staged ${records.length} existing resources without changing their bytes.`);
