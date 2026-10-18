/** Reuse the existing worker bytes through a host-independent, local blob URL.
 * Custom desktop URL schemes differ between WebView2 and WebKitGTK; bundled
 * resources are fetched from the app origin, never from a CDN or remote page.
 */
export const workerEvidence = { created: 0, messages: 0, errors: 0 };

export function installMonacoWorkerHost(): void {
  let source: Promise<string> | null = null;
  const owned = new Map<Worker, string>();
  const environment = window.MonacoEnvironment ?? {};
  environment.getWorker = async (_moduleId: string, label: string): Promise<Worker> => {
    if (owned.size >= 4) throw new Error('The prototype editor worker budget is exhausted.');
    source ??= fetch('/editor.worker.js').then(async response => {
      if (!response.ok) throw new Error('The reused editor worker is unavailable. Rebuild local assets.');
      return response.text();
    });
    const script = await source;
    if (owned.size >= 4) throw new Error('The prototype editor worker budget is exhausted.');
    const url = URL.createObjectURL(new Blob([script], { type: 'text/javascript' }));
    let worker: Worker;
    try { worker = new Worker(url, { name: label }); }
    catch (error) { URL.revokeObjectURL(url); throw error; }
    workerEvidence.created += 1;
    owned.set(worker, url);
    worker.addEventListener('message', () => { workerEvidence.messages += 1; });
    worker.addEventListener('error', () => { workerEvidence.errors += 1; });
    const terminate = worker.terminate.bind(worker);
    worker.terminate = () => { URL.revokeObjectURL(url); owned.delete(worker); terminate(); };
    return worker;
  };
  window.MonacoEnvironment = environment;
  window.addEventListener('pagehide', () => { for (const worker of [...owned.keys()]) worker.terminate(); }, { once: true });
}
