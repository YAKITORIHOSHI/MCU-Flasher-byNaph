/* Decides when the assistant's own interface is really on screen.
 *
 * Shared by the Linux/macOS page (opencode.html) and the Windows page
 * (dedicated_AI.py). It reads xterm's parsed screen, never raw PTY bytes:
 * capability queries, graphics probes and sequences split across reads all look
 * like text to a byte filter, and none of them ever becomes a screen cell.
 */
(function (root) {
  "use strict";

  const MINIMUM_CELLS = 8;           // A lone cursor or stray reply is not an interface.
  const SETTLE_MS = 350;             // Output stopped, so a complete frame has landed.
  const BUSY_PAINT_MS = 1500;        // An animated interface never goes quiet.
  const PLAIN_TEXT_QUIET_MS = 8000;  // Plain text then silence: a question or error to read.

  // Full-screen interfaces switch to the alternate screen or capture the mouse.
  // ConPTY consoles forward the mouse request but keep the alternate screen to
  // themselves, so Windows depends on the second test.
  function hasTakenOver(term) {
    if (term.buffer.active.type === "alternate") return true;
    const mode = term.modes && term.modes.mouseTrackingMode;
    return Boolean(mode) && mode !== "none";
  }

  function paintedCells(term) {
    const buffer = term.buffer.active;
    let cells = 0;
    for (let row = 0; row < term.rows; row += 1) {
      const line = buffer.getLine(buffer.viewportY + row);
      if (line) cells += line.translateToString(true).replace(/\s/g, "").length;
    }
    return cells;
  }

  const realClock = {
    now: () => Date.now(),
    setTimeout: (callback, delay) => setTimeout(callback, delay),
    clearTimeout: id => clearTimeout(id),
  };

  function create(term, onReady, clock = realClock) {
    let finished = false;
    let timer = null;
    let firstPaint = null;
    let lastOutput = clock.now();

    function arm(delay) {
      if (timer === null) timer = clock.setTimeout(evaluate, delay);
    }

    function finish(reason) {
      finished = true;
      onReady(reason);
    }

    function evaluate() {
      timer = null;
      if (finished) return;
      const now = clock.now();
      const quiet = now - lastOutput;
      const cells = paintedCells(term);
      if (hasTakenOver(term)) {
        // An unpainted interface decides nothing until its next output arrives.
        if (cells < MINIMUM_CELLS) {
          firstPaint = null;
          return;
        }
        if (firstPaint === null) firstPaint = now;
        if (quiet >= SETTLE_MS || now - firstPaint >= BUSY_PAINT_MS) {
          finish("interface");
        } else {
          arm(Math.max(16, SETTLE_MS - quiet));
        }
        return;
      }
      firstPaint = null;
      if (cells === 0) return;
      if (quiet >= PLAIN_TEXT_QUIET_MS) {
        finish("text");
      } else {
        arm(SETTLE_MS);
      }
    }

    return {
      /* Call after xterm has parsed a chunk of output. */
      observe() {
        if (finished) return;
        lastOutput = clock.now();
        arm(SETTLE_MS);
      },
      /* Call when a fresh session starts in this terminal. */
      reset() {
        if (timer !== null) clock.clearTimeout(timer);
        timer = null;
        finished = false;
        firstPaint = null;
        lastOutput = clock.now();
      },
    };
  }

  root.AssistantReadiness = {create, hasTakenOver, paintedCells};
})(this);
