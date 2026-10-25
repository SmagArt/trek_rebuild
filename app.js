// Страница trek_rebuild: сбор файлов и настроек, вызов воркера, отчёт и карта.
const $ = (id) => document.getElementById(id);
const worker = new Worker('worker.js?v=13');
const state = { fits: [], gpx: null, ready: false, busy: false, url: null, map: null };

// ── файлы ───────────────────────────────────────────────────────────────────

function readFile(f) {
  return f.arrayBuffer().then((b) => ({ name: f.name, data: new Uint8Array(b) }));
}

async function addFits(list) {
  for (const f of list) state.fits.push(await readFile(f));
  if (list.length) goal('fit_added', { files: list.length });
  renderFits();
}

function renderFits() {
  const ul = $('fitList');
  ul.innerHTML = '';
  state.fits.forEach((f, i) => {
    const li = document.createElement('li');
    li.innerHTML = `<span></span><button title="убрать" aria-label="убрать">×</button>`;
    li.firstChild.textContent = `${f.name} · ${(f.data.length / 1024).toFixed(0)} КБ`;
    li.lastChild.onclick = () => { state.fits.splice(i, 1); renderFits(); };
    ul.appendChild(li);
  });
  update();
}

function dropZone(zone, input, onFiles) {
  input.onchange = () => { onFiles(input.files); input.value = ''; };
  zone.addEventListener('dragover', (e) => { e.preventDefault(); zone.classList.add('over'); });
  zone.addEventListener('dragleave', () => zone.classList.remove('over'));
  zone.addEventListener('drop', (e) => {
    e.preventDefault();
    zone.classList.remove('over');
    onFiles(e.dataTransfer.files);
  });
}

dropZone($('dropFit'), $('fitInput'), addFits);
dropZone($('dropGpx'), $('gpxInput'), async (files) => {
  if (!files.length) return;
  state.gpx = await readFile(files[0]);
  goal('gpx_added');
  $('gpxName').textContent = state.gpx.name;
  update();
});

// ── настройки ───────────────────────────────────────────────────────────────

// закладки трасс: id встроенной трассы или '' — свой GPX
let tab = 'own';

function route() {
  if (tab === 'own') return '';
  return document.querySelector('input[name="preset"]:checked').value;
}

document.querySelectorAll('[role="tab"]').forEach((b) => {
  b.onclick = () => {
    tab = b.dataset.tab;
    document.querySelectorAll('[role="tab"]').forEach((x) => x.setAttribute('aria-selected', x === b));
    document.querySelectorAll('.panel').forEach((p) => p.classList.toggle('hidden', p.dataset.panel !== tab));
    update();
  };
});

function update() {
  const own = route() === '';
  const okFiles = state.fits.length > 0;
  const okRoute = !own || state.gpx;
  $('go').disabled = !state.ready || state.busy || !okFiles || !okRoute;
  if (state.ready && !state.busy) {
    $('status').textContent = !okFiles ? 'Добавь файл тренировки' : !okRoute ? 'Загрузи свой трек (GPX)' : '';
  }
}

// ── расчёт ──────────────────────────────────────────────────────────────────

$('go').onclick = () => {
  state.busy = true;
  update();
  $('status').textContent = 'Считаю…';
  const own = route() === '';
  goal('run', { route: route() || 'own', distance: document.querySelector('input[name="dist"]:checked').value });
  worker.postMessage({
    type: 'run',
    files: state.fits,
    gpx: own ? state.gpx : null,
    opts: {
      route: route(),
      distance: document.querySelector('input[name="dist"]:checked').value,
      reverse: own && $('reverse').checked,
      keep_spikes: $('keepSpikes').checked,
      ignore_gps: $('ignoreGps').checked,
      keep_live: $('keepLive').checked,
      laps: $('laps').value,
    },
  });
};

worker.onmessage = (e) => {
  const m = e.data;
  if (m.type === 'ready') {
    state.ready = true;
    update();
  } else if (m.type === 'result') {
    state.busy = false;
    update();
    goal(m.report.ok ? 'rebuilt' : 'rebuild_failed', { route: route() || 'own' });
    show(m.report, m.bytes);
  } else if (m.type === 'error') {
    // не загрузился Pyodide с CDN — так видно, если у кого-то из РФ его режут
    goal(state.ready ? 'worker_error' : 'engine_load_failed');
    state.busy = false;
    $('status').innerHTML = '';
    const s = document.createElement('span');
    s.className = 'error';
    s.textContent = state.ready ? 'Ошибка: ' + m.error
      : 'Не загрузился расчётный модуль — проверь интернет и обнови страницу. (' + m.error + ')';
    $('status').appendChild(s);
    update();
  }
};
worker.postMessage({ type: 'init' });

// ── отчёт ───────────────────────────────────────────────────────────────────

const km = (x) => x.toFixed(2).replace('.', ',');

function show(rep, bytes) {
  const box = $('result');
  if (!rep.ok) {
    box.classList.add('hidden');
    $('status').innerHTML = '';
    const s = document.createElement('span');
    s.className = 'error';
    s.textContent = rep.error + (rep.bug ? ' — это ошибка программы, пришли файл автору.' : '');
    $('status').appendChild(s);
    return;
  }
  $('status').textContent = '';
  box.classList.remove('hidden');
  $('resTitle').textContent = `Готово · ${rep.date}, ${rep.start.slice(0, 5)}`;
  const changed = rep.watch_km > 0 && Math.abs(rep.watch_km - rep.result_km) >= 0.01;
  $('big').innerHTML = `
    <div><span>Дистанция</span><strong>${km(rep.result_km)} км</strong>${changed ? ` <s>${km(rep.watch_km)}</s>` : ''}</div>
    ${rep.sport === 2 ? `<div><span>Скорость</span><strong>${rep.speed ? String(rep.speed).replace('.', ',') : '—'}</strong> км/ч</div>`
      : `<div><span>Темп</span><strong>${rep.pace || '—'}</strong> /км</div>`}
    <div><span>Время в движении</span><strong>${rep.timer}</strong></div>`;

  const ul = $('msgs');
  ul.innerHTML = '';
  if (!rep.warnings.length && !rep.notes.length) {
    const li = document.createElement('li');
    li.className = 'n';
    li.textContent = 'Замечаний нет.';
    ul.appendChild(li);
  }
  rep.warnings.forEach((t) => addMsg(ul, 'w', '⚠ ' + t));
  rep.notes.forEach((t) => addMsg(ul, 'n', t));
  $('lapsInfo').textContent = `${rep.laps[0].toUpperCase()}${rep.laps.slice(1)}. В файле ${rep.records} точек.`;

  if (state.url) URL.revokeObjectURL(state.url);
  state.url = URL.createObjectURL(new Blob([bytes], { type: 'application/octet-stream' }));
  const a = $('download');
  a.href = state.url;
  a.download = rep.source.replace(/\.(fit|zip|gpx|tcx)$/i, '') + '_trek.fit';
  a.onclick = () => goal('downloaded');

  drawMap(rep);
  box.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function addMsg(ul, cls, text) {
  const li = document.createElement('li');
  li.className = cls;
  li.textContent = text;
  ul.appendChild(li);
}

function drawMap(rep) {
  const css = getComputedStyle(document.documentElement);
  if (!state.map) {
    state.map = L.map('map', { scrollWheelZoom: false });
    L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 19, attribution: '© OpenStreetMap',
    }).addTo(state.map);
    state.layers = L.layerGroup().addTo(state.map);
  }
  state.layers.clearLayers();
  if (rep.watch_gps.length > 1) {
    L.polyline(rep.watch_gps, { color: css.getPropertyValue('--gps').trim(), weight: 2, opacity: 0.8, dashArray: '4 4' })
      .addTo(state.layers);
  }
  const line = L.polyline(rep.track, { color: css.getPropertyValue('--track').trim(), weight: 4 }).addTo(state.layers);
  L.circleMarker(rep.track[0], { radius: 6, color: '#2b8a3e', fillOpacity: 1 }).addTo(state.layers).bindTooltip('старт');
  L.circleMarker(rep.track[rep.track.length - 1], { radius: 6, color: '#c92a2a', fillOpacity: 1 }).addTo(state.layers).bindTooltip('финиш');
  setTimeout(() => {
    state.map.invalidateSize();
    state.map.fitBounds(line.getBounds(), { padding: [16, 16] });
  }, 50);
}
