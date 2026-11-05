// A terminal page paints its own copy of its window's glass, because a web view cannot show
// what is behind it. main/qt/glass.py sends the numbers that keep the copy lined up.
(() => {
  const style = document.documentElement.style;
  window.setGlassBackdrop = (backdrop) => {
    for (const name of ['width', 'height', 'x', 'y']) {
      if (Number.isFinite(backdrop[name])) style.setProperty('--scene-' + name, backdrop[name] + 'px');
    }
  };
  window.setGlassSurface = (surface) => {
    style.setProperty('--frost', `url('${surface.frost}')`);
    style.setProperty('--fallback', surface.fallback);
    style.setProperty('--sheet', surface.sheet);
    style.setProperty('--reading', surface.reading);
    style.setProperty('--reading-rim', surface.readingRim);
  };
})();
