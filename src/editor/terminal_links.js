/*
 * Links, clipboard and pointer conveniences for the application's xterm pages.
 *
 * Shared by the native Linux terminal page (Qt WebChannel bridge) and the
 * Windows project terminal page (WebSocket bridge). The host supplies how a
 * link opens and how text reaches the clipboard; nothing here talks to a PTY
 * except through xterm's own paste(), which keeps bracketed paste intact.
 */
(function () {
  "use strict";

  const LINK_RE = /(?:https?:\/\/|www\.)[^\s"'<>`{}|\\^\u0000-\u001f\u007f]+/gi;
  const CLOSERS = { ")": "(", "]": "[", "}": "{", ">": "<" };

  function count(text, ch) {
    let total = 0;
    for (const value of text) if (value === ch) total += 1;
    return total;
  }

  /* Drop sentence punctuation and unbalanced closing brackets after a URL. */
  function trimLink(text) {
    let end = text.length;
    while (end > 0) {
      const ch = text[end - 1];
      if (".,;:!?'\"*".includes(ch)) { end -= 1; continue; }
      const opener = CLOSERS[ch];
      if (opener && count(text.slice(0, end), ch) > count(text.slice(0, end), opener)) { end -= 1; continue; }
      break;
    }
    return text.slice(0, end);
  }

  function target(text) {
    return /^www\./i.test(text) ? "https://" + text : text;
  }

  function isWebLink(url) {
    return /^https?:\/\/[^\s]+$/i.test(url);
  }

  /*
   * One logical line (a row plus the rows it wraps onto) with the buffer cell
   * of every UTF-16 unit, so wide characters and wrapped URLs map exactly.
   */
  function logicalLine(buffer, row) {
    let first = row;
    while (first > 0) {
      const line = buffer.getLine(first);
      if (!line || !line.isWrapped) break;
      first -= 1;
    }
    let last = row;
    for (;;) {
      const next = buffer.getLine(last + 1);
      if (!next || !next.isWrapped) break;
      last += 1;
    }
    let text = "";
    const cells = [];
    for (let y = first; y <= last && y - first < 64; y += 1) {
      const line = buffer.getLine(y);
      if (!line) break;
      for (let x = 0; x < line.length; x += 1) {
        const cell = line.getCell(x);
        if (!cell) continue;
        const width = cell.getWidth();
        if (width === 0) continue;
        const chars = cell.getChars() || " ";
        for (let unit = 0; unit < chars.length; unit += 1) {
          cells.push({ x: x + 1, y: y + 1, width });
        }
        text += chars;
      }
    }
    return { text, cells };
  }

  function linksOnRow(term, bufferLineNumber) {
    const buffer = term.buffer.active;
    const row = bufferLineNumber - 1;
    const { text, cells } = logicalLine(buffer, row);
    const found = [];
    LINK_RE.lastIndex = 0;
    let match;
    while ((match = LINK_RE.exec(text)) !== null) {
      const raw = trimLink(match[0]);
      if (raw.length < 8) continue;
      const start = cells[match.index];
      const end = cells[match.index + raw.length - 1];
      if (!start || !end) continue;
      if (start.y > bufferLineNumber || end.y < bufferLineNumber) continue;
      found.push({ text: raw, url: target(raw),
        range: { start: { x: start.x, y: start.y }, end: { x: end.x + end.width - 1, y: end.y } } });
    }
    return found;
  }

  /*
   * host.openLink(url)    open an http(s) URL outside the terminal
   * host.copyText(text)   place text on the clipboard
   * host.ctrlVPastes      optional: plain Ctrl+V pastes (Windows convention)
   * host.setPrimary(text) optional: X11/Wayland primary selection
   * host.pastePrimary()   optional: paste the primary selection (middle click)
   */
  window.mcuEnhanceTerminal = function (term, host) {
    if (!term || !host) return;
    let hovered = null;

    const open = (url) => {
      if (isWebLink(url)) host.openLink(url);
    };

    // OSC 8 hyperlinks written by programs: the same opener, never a prompt.
    term.options.linkHandler = {
      allowNonHttpProtocols: false,
      activate: (event, text) => open(text),
      hover: (event, text) => { hovered = text; term.element.title = "Open " + text; },
      leave: () => { hovered = null; term.element.title = ""; },
    };

    term.registerLinkProvider({
      provideLinks(bufferLineNumber, callback) {
        let links = [];
        try {
          links = linksOnRow(term, bufferLineNumber).map((link) => ({
            range: link.range,
            text: link.text,
            decorations: { pointerCursor: true, underline: true },
            activate: (event) => {
              if (event && event.button !== undefined && event.button !== 0) return;
              open(link.url);
            },
            hover: () => { hovered = link.url; term.element.title = "Open " + link.url; },
            leave: () => { hovered = null; term.element.title = ""; },
          }));
        } catch (error) {
          links = [];
        }
        callback(links.length ? links : undefined);
      },
    });

    const copySelection = () => {
      const text = term.getSelection();
      if (text) host.copyText(text);
      return Boolean(text);
    };

    term.attachCustomKeyEventHandler((event) => {
      if (event.type !== "keydown") return true;
      const key = (event.key || "").toLowerCase();
      const ctrl = event.ctrlKey || event.metaKey;
      if (ctrl && event.shiftKey && key === "c") {
        copySelection();
        event.preventDefault();
        return false;
      }
      if (ctrl && !event.shiftKey && !event.altKey && key === "c" && term.hasSelection()) {
        // With a selection Ctrl+C copies; without one it still interrupts.
        copySelection();
        term.clearSelection();
        event.preventDefault();
        return false;
      }
      // Ctrl+Shift+V and Shift+Insert paste everywhere. Plain Ctrl+V pastes only
      // where the host says so (Windows); on Linux it stays ^V for vim and nano.
      const paste = (event.shiftKey && !ctrl && key === "insert")
        || (ctrl && event.shiftKey && !event.altKey && key === "v")
        || (host.ctrlVPastes && ctrl && !event.shiftKey && !event.altKey && key === "v");
      if (paste) {
        // Let the browser deliver its native paste; xterm pastes it with
        // bracketed-paste markers when the program asked for them.
        return false;
      }
      if (ctrl && event.shiftKey && key === "a") {
        term.selectAll();
        event.preventDefault();
        return false;
      }
      return true;
    });

    if (typeof host.setPrimary === "function") {
      let pending = 0;
      term.onSelectionChange(() => {
        window.clearTimeout(pending);
        pending = window.setTimeout(() => {
          const text = term.getSelection();
          if (text) host.setPrimary(text);
        }, 120);
      });
    }

    if (typeof host.pastePrimary === "function" && term.element) {
      term.element.addEventListener("mousedown", (event) => {
        // Programs that track the mouse receive the middle button themselves.
        if (event.button !== 1 || (term.modes && term.modes.mouseTrackingMode !== "none")) return;
        event.preventDefault();
        event.stopPropagation();
        term.focus();
        host.pastePrimary();
      }, true);
    }

    window.mcuTerminalLinks = { linksOnRow: (row) => linksOnRow(term, row), trimLink };
    window.terminalContextState = () => ({
      selection: term.hasSelection(),
      link: hovered && isWebLink(hovered) ? hovered : "",
    });
    window.terminalCopy = () => copySelection();
    window.terminalPasteText = (text) => { if (text) term.paste(String(text)); term.focus(); };
    window.terminalSelectAll = () => term.selectAll();
    window.terminalOpenLink = (url) => open(url);
  };
})();
