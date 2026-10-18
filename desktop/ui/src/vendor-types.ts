import type { TextModel } from './document-store.ts';
export interface Editor {
  getModel(): TextModel | null; setModel(model: TextModel | null): void;
  updateOptions(options: Record<string, unknown>): void;
  saveViewState(): unknown; restoreViewState(state: unknown): void;
  layout(): void; focus(): void;
}
export interface Monaco {
  Uri: { parse(uri: string): unknown };
  editor: {
    createModel(content: string, language: string, uri?: unknown): TextModel;
    defineTheme(name: string, theme: Record<string, unknown>): void;
    setTheme(name: string): void;
    tokenize(text: string, language: string): { type: string; offset: number; language: string }[][];
    createDiffEditor(element: HTMLElement, options?: Record<string, unknown>): {
      setModel(model: { original: TextModel; modified: TextModel }): void;
      getLineChanges(): unknown[] | null;
      dispose(): void;
    };
  };
}
export interface Terminal {
  options: { theme: Record<string, string>; scrollback: number };
  loadAddon(addon: unknown): void; open(element: HTMLElement): void;
  write(data: string, callback?: () => void): void; clear(): void; dispose(): void;
}
export interface TerminalFit { fit(): void; dispose(): void }
declare global {
  interface Window {
    monaco?: Monaco; editorInstance?: Editor;
    Terminal?: new(options: Record<string, unknown>) => Terminal;
    FitAddon?: { FitAddon: new() => TerminalFit };
    MonacoEnvironment?: { getWorker?: (moduleId: string, label: string) => Worker | Promise<Worker> };
  }
}
