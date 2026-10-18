import type { Monaco } from './vendor-types.ts';
export type ThemeName = 'default' | 'light' | 'solarized';
// Palette values are retained from src/editor/index.html's custom Monaco themes.
export const palettes = {
  default: { background: '#101b2a', foreground: '#e3edf6', cursor: '#80d9ce',
    selectionBackground: '#324e69', lineNumber: '#6b7d94', keyword: '80d9ce', comment: 'adc0d3', string: '98c379', number: 'd19a66' },
  light: { background: '#ffffff', foreground: '#24292f', cursor: '#0969da',
    selectionBackground: '#add6ff', lineNumber: '#8c959f', keyword: 'cf222e', comment: '6e7781', string: '0a3069', number: '0550ae' },
  solarized: { background: '#002b36', foreground: '#ffffff', cursor: '#39c5bb',
    selectionBackground: '#073642', lineNumber: '#586e75', keyword: '859900', comment: '718c93', string: '2aa198', number: 'd33682' },
};
export function defineThemes(monaco: Monaco): void {
  for (const [name, palette] of Object.entries(palettes)) {
    monaco.editor.defineTheme(`mcu-${name}`, { base: name === 'light' ? 'vs' : 'vs-dark', inherit: true,
      rules: [{ token: 'comment', foreground: palette.comment, fontStyle: 'italic' },
        { token: 'keyword', foreground: palette.keyword, fontStyle: 'bold' },
        { token: 'string', foreground: palette.string }, { token: 'number', foreground: palette.number }],
      colors: { 'editor.background': palette.background, 'editor.foreground': palette.foreground,
        'editorCursor.foreground': palette.cursor, 'editor.selectionBackground': palette.selectionBackground,
        'editorLineNumber.foreground': palette.lineNumber, 'editorLineNumber.activeForeground': palette.cursor,
        'editorGutter.background': palette.background } });
  }
}
