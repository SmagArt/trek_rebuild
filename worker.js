// Фоновый поток: Pyodide + движок trek_rebuild. Страница не подвисает,
// пока Python грузится (~10 МБ при первом заходе) и считает.
const PYODIDE = 'https://cdn.jsdelivr.net/pyodide/v0.27.7/full/';
const PKG = ['__init__.py', 'fit.py', 'geo.py', 'engine.py', 'routes.py', 'web.py',
             'routes/naberezhnaya.gpx', 'routes/coferan.gpx'];
const VERSION = '5';   // поднимать при смене файлов пакета — сбрасывает кэш браузера

importScripts(PYODIDE + 'pyodide.js');

let ready = null;

async function boot() {
  const py = await loadPyodide({ indexURL: PYODIDE });
  const root = '/home/pyodide/trek_rebuild';
  py.FS.mkdirTree(root + '/routes');
  await Promise.all(PKG.map(async (p) => {
    const r = await fetch('trek_rebuild/' + p + '?v=' + VERSION);
    if (!r.ok) throw new Error('не загрузился ' + p + ' (' + r.status + ')');
    py.FS.writeFile(root + '/' + p, new Uint8Array(await r.arrayBuffer()));
  }));
  py.runPython('import sys; sys.path.insert(0, "/home/pyodide"); from trek_rebuild import web');
  return py;
}

self.onmessage = async (e) => {
  const msg = e.data;
  try {
    if (!ready) ready = boot();
    const py = await ready;
    if (msg.type === 'init') {
      const presets = py.runPython('web.presets_json()');
      self.postMessage({ type: 'ready', presets: JSON.parse(presets) });
      return;
    }
    if (msg.type === 'run') {
      const web = py.globals.get('web');
      const files = py.toPy(msg.files.map((f) => [f.name, f.data]));
      const gpx = msg.gpx ? py.toPy(msg.gpx.data) : null;
      const pyFiles = py.runPython('lambda fs: [(n, bytes(d)) for n, d in fs]')(files);
      const pyGpx = gpx ? py.runPython('bytes')(gpx) : null;
      const res = web.run(pyFiles, msg.gpx ? msg.gpx.name : null, pyGpx, JSON.stringify(msg.opts));
      const report = JSON.parse(res.get(0));
      const out = res.get(1);
      const bytes = out ? out.toJs() : null;
      [files, gpx, pyFiles, pyGpx, res, out, web].forEach((x) => x && x.destroy && x.destroy());
      self.postMessage({ type: 'result', report, bytes }, bytes ? [bytes.buffer] : []);
    }
  } catch (err) {
    ready = msg.type === 'init' ? null : ready;
    self.postMessage({ type: 'error', error: String(err && err.message || err) });
  }
};
