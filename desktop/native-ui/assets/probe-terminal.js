(() => {
  'use strict';
  const fail = error => window.ipc.postMessage(JSON.stringify({ kind: 'error', message: String(error?.message ?? error).slice(0, 1024) }));
  try {
    const terminal = new window.Terminal({ scrollback: 100, fontSize: 14, theme: { background: '#17212b', foreground: '#e5edf5' } });
    const fit = new window.FitAddon.FitAddon();
    terminal.loadAddon(fit);
    terminal.open(document.getElementById('terminal'));
    const resize = () => fit.fit();
    new ResizeObserver(resize).observe(document.getElementById('terminal'));
    resize();
    terminal.write('\u001b[36mNative Rust fixture\u001b[0m\r\nOffline xterm: café 日本語 λ\r\nNo firmware command or device write is enabled.\r\n', () => {
      const lines = [];
      for (let row = 0; row < terminal.buffer.active.length; row++) lines.push(terminal.buffer.active.getLine(row).translateToString(true));
      window.ipc.postMessage(JSON.stringify({ kind: 'terminal_snapshot', text: lines.join('\n'), columns: terminal.cols, rows: terminal.rows }));
    });
  } catch (error) { fail(error); }
})();
