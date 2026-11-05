#!/usr/bin/env python3
"""Offline rendered Arduino-inspired C/C++ colors and retained editor state.

No live project, settings, AI, network or hardware transport is opened. Reuses
the memory-only Qt fixture; every source model and tab exists only in RAM.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from direct import verify_embedded_viewports as fixture
from PySide6.QtTest import QTest
from main.core.log_colors import contrast_ratio
from main.core.theme import glass_worst
from main.qt.editor_panel import editor_page_background
from main.qt.theme import css_color, glass_tokens

REPORT = []
CAPTURES = None
EXPECTED = {
    "light": {"foreground":"#4e5b61", "identifier":"#434f54",
        "comment":"#606d6f", "type":"#00767b", "function":"#b74700",
        "literal":"#005c5f", "control":"#5d7300", "directive":"#7b6654"},
    "default": {"foreground":"#dae3e3", "identifier":"#dae3e3",
        "comment":"#959fa0", "type":"#22b0b5", "function":"#f39c12",
        "literal":"#7fcbcd", "control":"#c688c1", "directive":"#65a2d8"},
    "solarized_dark": {"foreground":"#dae3e3", "identifier":"#dae3e3",
        "comment":"#959fa0", "type":"#22b0b5", "function":"#f39c12",
        "literal":"#7fcbcd", "control":"#c688c1", "directive":"#65a2d8"}
}

INO = '\n'.join((
    '#include <Arduino.h>',
    '// setup() and HIGH remain a comment',
    'const uint8_t ledPin = 13;',
    'String label = "HIGH setup()";',
    'void setup() {',
    '  pinMode(ledPin, OUTPUT);',
    '}',
    'void loop() {',
    '  if (true) digitalWrite(ledPin, HIGH);',
    '  delay(250);',
    '}',
))
CPP = INO + '\nconst char *raw = R"tag(setup() HIGH // literal)tag";'
C = '\n'.join((
    '#include <stdint.h>',
    '/* helper() and HIGH stay comments',
    '   and "helper()" stays inside the comment */',
    'static const uint8_t count = 13;',
    'const char *message = "HIGH helper()";',
    'void helper(void) {',
    '  if (true) return;',
    '}',
    'int class = 1;',
))


def alpha_rounded(color):
    """Chromium and Python round the same alpha differently past two places."""
    head, _, alpha = color.rpartition(",")
    return f"{head},{float(alpha.rstrip(') ')):.2f}"


def rgb(hex_color):
    return 'rgb(' + ', '.join(str(int(hex_color[i:i+2], 16)) for i in (1, 3, 5)) + ')'


class ArduinoPaletteChecks(fixture.EmbeddedViewportChecks):
    def test_arduino_rendered_tokens_and_retained_state(self):
        self.load("index.html")
        fixture.wait_for(lambda: self.js("!!window.editorInstance && !!window.editorCFamilyReady"),
                         "The offline editor did not initialize")
        self.js("window.fixtureSyntaxReady=false; editorCFamilyReady.then(()=>{window.fixtureSyntaxReady=true}); true")
        fixture.wait_for(lambda: self.js("window.fixtureSyntaxReady"), "C syntax did not initialize")
        sources = [{"name":"Palette.ino","language":"cpp","source":INO},
                   {"name":"Palette.cpp","language":"cpp","source":CPP},
                   {"name":"Palette.c","language":"c","source":C}]
        self.js("window.paletteSources=" + json.dumps(sources) + "; true")
        self.js(r"""(() => {
          const editor=editorInstance;
          window.paletteModels=paletteSources.map(file=>monaco.editor.createModel(
            file.source,file.language,monaco.Uri.file('/fixture/'+file.name)));
          document.querySelectorAll('#tab-bar .tab').forEach(tab=>tab.remove());
          window.paletteTabs=paletteSources.map(file=>{
            const tab=document.createElement('div');tab.className='tab';
            tab.textContent=file.name;document.getElementById('tab-bar').append(tab);return tab;
          });
          editor.setModel(paletteModels[0]);
          editor.updateOptions({readOnly:false,minimap:{enabled:false},cursorBlinking:'solid',smoothScrolling:false});
          setEditorFontSize(17);
          editor.executeEdits('palette-fixture',[{range:new monaco.Range(1,1,1,1),text:'// unsaved fixture\n'}]);
          editor.pushUndoStop();
          const dirty=document.createElement('span');dirty.className='dirty-dot';paletteTabs[0].append(dirty);
          window.paletteDirty=paletteModels[0].getValue();
          window.paletteVersions=paletteModels.map(model=>model.getAlternativeVersionId());
          window.paletteFont=editor.getOption(monaco.editor.EditorOption.fontInfo).fontFamily;
          return true;
        })()""")
        fixture.wait_for(lambda: self.js("monaco.editor.tokenize('void setup() {}','cpp')[0].some(t=>t.type.startsWith('keyword'))"),
                         "C++ tokenizer did not initialize")
        for mode in ("light", "default", "solarized_dark", "light"):
            colors = EXPECTED[mode]
            for index, file in enumerate(sources):
                with self.subTest(theme=mode, file=file["name"]):
                    self.js(f"editorInstance.setModel(paletteModels[{index}]);"
                            f"paletteTabs.forEach((tab,i)=>tab.classList.toggle('active',i==={index}));"
                            "editorInstance.setSelection(new monaco.Range(1,1,1,2));"
                            "editorInstance.setScrollTop(0);editorInstance.focus();"
                            "window.paletteFocused=document.activeElement;"
                            f"setEditorTheme({json.dumps(mode)});forceEditorLayout();"
                            "editorInstance.getModel().tokenization?.forceTokenization?.("
                            "editorInstance.getModel().getLineCount());true")
                    # A slow host paints after tokenizing: an untokenized line is a single span.
                    fixture.wait_for(lambda: self.js(
                        "[...document.querySelectorAll('.view-lines .view-line')]"
                        ".some(line=>line.textContent.includes('#include')&&line.querySelectorAll('span span').length>1)"),
                        "Monaco did not paint tokenized source")
                    QTest.qWait(100)
                    data = self.state(r"""
                      const editor=editorInstance, font=editor.getOption(monaco.editor.EditorOption.fontInfo);
                      const rendered=Array.from(document.querySelectorAll('.view-lines .view-line'))
                        .sort((a,b)=>parseFloat(a.style.top)-parseFloat(b.style.top)).map(line=>
                        Array.from(line.querySelectorAll('span[class]')).map(span=>({
                          text:span.textContent.replace(/\u00a0/g,' '), color:getComputedStyle(span).color})));
                      return {rendered, background:getComputedStyle(editor.getDomNode()).backgroundColor,
                        hostBackground:getComputedStyle(document.getElementById('editor-container')).backgroundColor,
                        bodyBackground:getComputedStyle(document.body).backgroundColor,
                        foreground:getComputedStyle(editor.getDomNode()).color,
                        same:paletteModels.includes(editor.getModel()),
                        retained:paletteModels.every((model,i)=>model.getAlternativeVersionId()===paletteVersions[i]),
                        dirty:paletteModels[0].getValue()===paletteDirty && !!paletteTabs[0].querySelector('.dirty-dot'),
                        tabs:Array.from(document.querySelectorAll('#tab-bar .tab')).map(tab=>tab.firstChild.textContent),
                        selection:editor.getSelection(),fontFamily:font.fontFamily,fontSize:font.fontSize,
                        focus:document.activeElement===paletteFocused,
                        cClass:monaco.editor.tokenize('int class = 1;','c')[0],
                        cppClass:monaco.editor.tokenize('class Widget {};','cpp')[0]};
                    """)
                    REPORT.append({"theme":mode,"file":file["name"],**data})
                    self.assertTrue(data["same"] and data["retained"] and data["dirty"] and data["focus"])
                    self.assertEqual(data["fontFamily"], self.js("paletteFont"))
                    self.assertEqual(data["fontSize"], 17)
                    self.assertEqual(data["selection"]["startColumn"], 1)
                    self.assertEqual(data["selection"]["endColumn"], 2)
                    self.assertEqual(data["tabs"], [f["name"] for f in sources])
                    # Monaco paints nothing over the glass, and the well holds the page's smoke.
                    self.assertEqual(data["background"], "rgba(0, 0, 0, 0)")
                    reading = css_color(glass_tokens(mode)["reading"]).replace("0.667", "0.667")
                    self.assertEqual(alpha_rounded(data["hostBackground"]), alpha_rounded(reading))
                    self.assertEqual(data["bodyBackground"], rgb(editor_page_background(mode)))
                    for category, ink in colors.items():
                        self.assertGreaterEqual(contrast_ratio(ink, glass_worst(mode)["reading"]), 4.5, category)
                    offset = 1 if index == 0 else 0
                    expected = [(1,"#include","directive"), (2,"setup()","comment"),
                        (3,"uint8_t","type"), (3,"ledPin","identifier"), (3,"13","literal"),
                        (4,"String","type"), (4,'HIGH setup()',"literal"), (5,"setup","function"),
                        (5,"(","identifier"), (5,"{","identifier"), (7,"}","identifier"),
                        (6,"pinMode","function"), (6,"OUTPUT","literal"), (9,"if","control"),
                        (9,"true","literal"), (9,"digitalWrite","function"), (9,"HIGH","literal")]
                    if index == 2:
                        expected = [(1,"#include","directive"), (2,"helper()","comment"),
                            (3,'"helper()"',"comment"), (4,"uint8_t","type"), (4,"13","literal"),
                            (5,"char","type"), (5,"HIGH helper()","literal"), (6,"helper","function"),
                            (6,"(","identifier"), (6,"{","identifier"), (8,"}","identifier"),
                            (7,"if","control"), (7,"true","literal"), (7,"return","control"),
                            (9,"class","identifier")]
                    if index == 1:
                        expected.append((12,"setup() HIGH // literal","literal"))
                    for line, word, category in expected:
                        spans = data["rendered"][line - 1 + offset]
                        matches = [span for span in spans if word in span["text"]]
                        self.assertTrue(matches, f"{file['name']}:{line}: {word!r} was not actually rendered: {spans}")
                        self.assertTrue(any(span["color"] == rgb(colors[category]) for span in matches),
                                        f"{file['name']}:{line}: {word!r} needs {category} {colors[category]}, got {matches}")
                    self.assertFalse(any(t["offset"] == 4 and t["type"].startswith("keyword") for t in data["cClass"]))
                    self.assertTrue(any(t["offset"] == 0 and t["type"].startswith("keyword") for t in data["cppClass"]))
                    if CAPTURES is not None and index == 0:
                        self.assertTrue(self.view.grab().save(str(CAPTURES / f"arduino-{mode}.png")))
        self.js("editorInstance.setModel(paletteModels[0]);paletteModels[0].undo();true")
        self.assertEqual(self.js("paletteModels[0].getValue()"), INO)
        self.js("paletteModels[0].redo();true")
        self.assertEqual(self.js("paletteModels[0].getValue()"), self.js("paletteDirty"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--render-dir", type=Path)
    args = parser.parse_args()
    for output in (args.report, args.render_dir):
        if output is not None and ROOT / "temp" not in output.resolve().parents:
            parser.error("Fixture outputs must remain below this checkout's temp directory")
    if args.render_dir:
        CAPTURES = args.render_dir.resolve()
        CAPTURES.mkdir(parents=True, exist_ok=True)
    suite = unittest.TestSuite([ArduinoPaletteChecks("test_arduino_rendered_tokens_and_retained_state")])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps({"successful":result.wasSuccessful(),"states":REPORT},indent=2)+'\n',encoding='utf-8')
    sys.exit(not result.wasSuccessful())
