const $ = id => document.getElementById(id);
let state = null;
let settings = {};
let discovery = {groups: [], buffers: [], meta: {}};
let saveTimer = null;
let toastTimer = null;
let pollTimer = null;
let busy = false;

const FILTER_DESCRIPTIONS = {
  raw: 'No smoothing or added lag. Useful as a baseline when comparing filters.',
  ema: 'A time-aware exponential low-pass. Predictable, but its lag rises as you smooth harder.',
  one_euro: 'Adaptive smoothing: calm near rest, more responsive during fast motion.',
  median_one_euro: 'Rejects isolated spikes before adaptive smoothing. Better outlier rejection, one extra sample of feel.'
};

function api() {
  if (!window.pywebview || !window.pywebview.api) throw new Error('The desktop WebView bridge is unavailable. Launch Gyromouse using start_windows.bat.');
  return window.pywebview.api;
}

function showToast(message, error = false) {
  const el = $('toast');
  el.textContent = message || 'Done.';
  el.classList.toggle('error', error);
  el.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), 3300);
}

function setBusy(value) {
  busy = value;
  $('connectButton').disabled = value;
  $('connectButton').textContent = value ? 'Connecting…' : 'Connect';
}

function mergeSettings(next) {
  if (!next) return;
  settings = {...settings, ...next};
  syncControls();
  updateContextualControls();
}

function populateOptions(el, options, selected, placeholder = null) {
  const previous = selected ?? el.value;
  el.innerHTML = '';
  if (placeholder !== null) {
    const option = document.createElement('option');
    option.value = '';
    option.textContent = placeholder;
    el.appendChild(option);
  }
  for (const item of options) {
    const option = document.createElement('option');
    if (typeof item === 'string') {
      option.value = item;
      option.textContent = item;
    } else {
      option.value = item.value;
      option.textContent = item.label;
    }
    el.appendChild(option);
  }
  if ([...el.options].some(o => o.value === String(previous ?? ''))) el.value = String(previous ?? '');
  else if (placeholder !== null) el.value = '';
  else if (el.options.length) el.selectedIndex = 0;
}

function populateDiscovery() {
  const groups = discovery.groups || [];
  const groupOptions = groups.map(g => ({value: g.key, label: g.label}));
  if (!groupOptions.length) groupOptions.push({value: 'auto', label: 'Manual mapping'});
  populateOptions($('source_group'), groupOptions, settings.source_group || groupOptions[0].value);
  const buffers = (discovery.buffers || []).map(b => ({value: b.name, label: b.size ? `${b.name}  ·  ${b.size}` : b.name}));
  for (const key of ['x_buffer', 'y_buffer', 'z_buffer']) populateOptions($(key), buffers, settings[key], 'Select buffer');
  populateOptions($('time_buffer'), buffers, settings.time_buffer, 'No timestamp · latest only');
  if (discovery.title) $('experimentName').textContent = discovery.title;
  const n = discovery.buffers?.length || 0;
  const meta = discovery.meta || {};
  const device = meta.device || meta.deviceName || meta.model || '';
  $('experimentDetail').textContent = `${n} buffers discovered${device ? ` · ${device}` : ''}`;
  $('networkState').textContent = 'Connected';
  $('networkDot').className = 'dot green';
  $('mappingBadge').textContent = `${groups.length || 'MANUAL'} GROUP${groups.length === 1 ? '' : 'S'}`;
  updateCaptureNote();
}

function syncControls() {
  for (const element of document.querySelectorAll('[data-setting]')) {
    const key = element.dataset.setting;
    if (!(key in settings)) continue;
    if (element.type === 'checkbox') element.checked = Boolean(settings[key]);
    else if (element.type === 'range') {
      element.value = settings[key];
      paintRange(element);
      updateRangeLabel(key, settings[key]);
    } else {
      const wanted = String(settings[key] ?? '');
      if ([...element.options].some(o => o.value === wanted)) element.value = wanted;
    }
  }
  $('phoneUrl').value = settings.phone_url || '';
  $('filterDescription').textContent = FILTER_DESCRIPTIONS[settings.filter] || FILTER_DESCRIPTIONS.one_euro;
  updateCaptureNote();
}

function paintRange(el) {
  const min = Number(el.min || 0), max = Number(el.max || 100), val = Number(el.value || 0);
  const pct = max > min ? ((val - min) / (max - min)) * 100 : 0;
  el.style.background = `linear-gradient(90deg, #58d791 0%, #9df5bd ${pct}%, #2a3548 ${pct}%, #2a3548 100%)`;
}

function updateRangeLabel(key, value) {
  const node = $(`${key}Value`);
  if (!node) return;
  const n = Number(value);
  let v;
  if (key === 'poll_hz') v = `${Math.round(n)} req/s`;
  else if (['min_cutoff', 'ema_cutoff'].includes(key)) v = `${n.toFixed(key === 'min_cutoff' ? 2 : 1)} Hz`;
  else if (key === 'stationary_reset_ms') v = `${Math.round(n)} ms`;
  else if (key === 'max_speed') v = `${Math.round(n)} px/s`;
  else if (key === 'gain') v = `${Math.round(n).toLocaleString()}`;
  else if (key === 'direct_gain') v = `${Math.round(n)}`;
  else if (key === 'deadzone') v = n.toFixed(3);
  else if (key === 'response_curve' || key === 'friction' || key === 'stationary_threshold' || key === 'beta') v = n.toFixed(2);
  else if (key === 'click_z_threshold') v = n.toFixed(1);
  else if (key === 'click_xy_sum') v = `${Math.round(n)}`;
  else v = Number.isInteger(n) ? `${n}` : n.toFixed(2);
  node.textContent = v;
}

function updateContextualControls() {
  const isGyro = settings.signal_kind === 'gyroscope';
  const isIntegrate = settings.motion_mode === 'integrate' && !isGyro;
  $('gainControl').style.display = isIntegrate ? '' : 'none';
  $('frictionControl').style.display = isIntegrate ? '' : 'none';
  $('directGainControl').style.display = (settings.motion_mode === 'direct' || isGyro) ? '' : 'none';
  $('motionDescription').textContent = isGyro
    ? 'Gyroscope rate maps directly to cursor velocity; the acceleration integrator is bypassed.'
    : settings.motion_mode === 'integrate'
      ? 'Integrated acceleration creates momentum. Drag and stillness detection stop residual drift.'
      : 'Filtered signal maps directly to cursor velocity. Less inertial drift, but it behaves more like a tilt-rate control.';
  $('euroSettings').style.display = ['one_euro', 'median_one_euro'].includes(settings.filter) ? '' : 'none';
  $('emaSettings').style.display = settings.filter === 'ema' ? '' : 'none';
  $('trackingHint').textContent = state?.running ? 'Live. Stop tracking before changing the phone or changing sensor experiments.' : 'Start keeps cursor output paused for a short startup calibration if enabled.';
}

function updateCaptureNote() {
  const note = $('captureNote');
  const mode = $('capture_mode')?.value || settings.capture_mode || 'drain';
  const hasTime = Boolean($('time_buffer')?.value || settings.time_buffer);
  if (mode === 'drain' && hasTime) {
    note.classList.remove('warning');
    note.innerHTML = '<svg viewBox="0 0 16 16" fill="none"><circle cx="8" cy="8" r="6.2" stroke="currentColor" stroke-width="1.3"/><path d="M8 7.2v3.4M8 4.7v.1" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg><span>Drain mode requests samples newer than the previous phyphox timestamp. The finite ring buffer can still overwrite old data, so gaps and suspected overruns are counted.</span>';
  } else if (mode === 'drain') {
    note.classList.add('warning');
    note.innerHTML = '<svg viewBox="0 0 16 16" fill="none"><path d="M8 1.8 14.3 13H1.7L8 1.8Z" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/><path d="M8 5.4v3.4M8 11.1v.1" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg><span>No timestamp buffer is mapped. Gyromouse will fall back to latest-sample reads; map a monotonic experiment-time buffer to drain batches that accumulated between requests.</span>';
  } else {
    note.classList.add('warning');
    note.innerHTML = '<svg viewBox="0 0 16 16" fill="none"><path d="M8 1.8 14.3 13H1.7L8 1.8Z" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/><path d="M8 5.4v3.4M8 11.1v.1" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg><span>Latest mode has the least parsing overhead and latency, but it only reads the newest point. Intermediate sensor readings can be skipped if the request loop falls behind.</span>';
  }
}

async function connectPhone() {
  if (busy) return;
  setBusy(true);
  try {
    const result = await api().connect($('phoneUrl').value.trim());
    if (!result.ok) throw new Error(result.message || 'Connection failed.');
    const all = result.state;
    state = all;
    settings = {...settings, ...(all.settings || {})};
    discovery = all.discovery || discovery;
    populateDiscovery();
    syncControls();
    renderState(all);
    showToast(result.message || 'Connected to phyphox.');
  } catch (err) {
    $('networkState').textContent = 'Connection failed';
    $('networkDot').className = 'dot red';
    showToast(err.message || String(err), true);
  } finally {
    setBusy(false);
  }
}

async function toggleTracking() {
  if (busy) return;
  try {
    if (state?.running) {
      const result = await api().stop_tracking();
      showToast(result.message || 'Tracking stopped.');
    } else {
      $('trackingButton').disabled = true;
      $('trackingButton').innerHTML = '<span class="dot" style="background:#102c1d"></span><span>Starting…</span>';
      const result = await api().start_tracking();
      if (!result.ok) throw new Error(result.message || 'Could not start tracking.');
      showToast(result.message || 'Tracking started.');
    }
    await refreshState();
  } catch (err) {
    showToast(err.message || String(err), true);
  } finally {
    $('trackingButton').disabled = false;
    renderState(state || {});
  }
}

async function calibrate() {
  try {
    const result = await api().calibrate();
    if (!result.ok) throw new Error(result.message || 'Calibration could not start.');
    showToast(result.message || 'Calibration started. Keep the phone still.');
    await refreshState();
  } catch (err) { showToast(err.message || String(err), true); }
}

async function resetMotion() {
  try {
    const result = await api().reset_velocity();
    showToast(result.message || 'Motion state reset.');
  } catch (err) { showToast(err.message || String(err), true); }
}

function readElementValue(el) {
  if (el.type === 'checkbox') return el.checked;
  if (el.dataset.type === 'number' || el.type === 'range' || el.type === 'number') return Number(el.value);
  return el.value;
}

function queueSettingSave(key, value) {
  settings[key] = value;
  updateContextualControls();
  if (['capture_mode', 'time_buffer'].includes(key)) updateCaptureNote();
  clearTimeout(saveTimer);
  saveTimer = setTimeout(async () => {
    try {
      const result = await api().update_settings({[key]: value});
      if (!result.ok) throw new Error(result.message || 'Could not save settings.');
      if (result.settings) settings = {...settings, ...result.settings};
      if (key === 'source_group') {
        const latest = await api().get_state();
        state = latest;
        settings = {...settings, ...(latest.settings || {})};
        discovery = latest.discovery || discovery;
        populateDiscovery();
        syncControls();
      }
      updateContextualControls();
      updateCaptureNote();
    } catch (err) { showToast(err.message || String(err), true); }
  }, 120);
}

function bindControls() {
  for (const el of document.querySelectorAll('[data-setting]')) {
    const key = el.dataset.setting;
    if (el.type === 'range') {
      el.addEventListener('input', () => {
        const value = Number(el.value);
        paintRange(el);
        updateRangeLabel(key, value);
        settings[key] = value;
        updateContextualControls();
      });
      el.addEventListener('change', () => queueSettingSave(key, Number(el.value)));
    } else if (el.type === 'checkbox') {
      el.addEventListener('change', () => queueSettingSave(key, el.checked));
    } else {
      el.addEventListener('change', () => queueSettingSave(key, el.value));
    }
  }
  $('phoneUrl').addEventListener('keydown', e => { if (e.key === 'Enter') connectPhone(); });
}

async function refreshState() {
  try {
    const next = await api().get_state();
    state = next;
    if (next.settings) settings = {...settings, ...next.settings};
    if (next.discovery?.buffers?.length && !discovery.buffers?.length) {
      discovery = next.discovery;
      populateDiscovery();
    }
    renderState(next);
  } catch (err) {
    if (!String(err.message || '').includes('bridge is unavailable')) console.error(err);
  }
}

function fmt(n, digits = 1) {
  const v = Number(n);
  return Number.isFinite(v) ? v.toFixed(digits) : '—';
}

function formatVector(values) {
  return (values || [0,0,0]).map(v => Number(v || 0).toFixed(3)).join(' / ');
}

function renderState(next) {
  state = next || state || {};
  const stats = state.stats || {};
  const isRunning = Boolean(state.running);
  const isConnected = Boolean(state.connected || discovery.buffers?.length);
  $('statusLabel').textContent = isRunning ? 'Tracking live' : isConnected ? 'Connected' : 'Not connected';
  $('statusDot').className = `dot ${isRunning ? 'green' : isConnected ? 'amber' : ''}`;
  $('trackingButton').classList.toggle('stop', isRunning);
  $('trackingButton').innerHTML = isRunning
    ? '<svg viewBox="0 0 20 20" fill="none"><rect x="5" y="5" width="10" height="10" rx="1.5" fill="currentColor"/></svg><span>Stop tracking</span>'
    : '<svg viewBox="0 0 20 20" fill="none"><path d="M7 4.6v10.8L15.5 10 7 4.6Z" fill="currentColor"/></svg><span>Start tracking</span>';
  $('trackingHint').textContent = state.message || (isRunning ? 'Live sensor acquisition is running.' : 'Connect, then start; hold still during the brief startup calibration.');
  $('footerStatus').textContent = state.error ? `Last issue: ${state.error}` : state.message || 'Ready for a phone connection.';
  $('sensorHz').textContent = stats.sensor_hz > 0 ? fmt(stats.sensor_hz, 0) : '—';
  $('sensorFoot').textContent = !settings.time_buffer ? 'Receive rate · no timestamp buffer mapped' : stats.samples ? `${Math.round(stats.samples).toLocaleString()} samples processed this session` : 'Actual sensor timestamp rate';
  $('pollHz').textContent = stats.poll_hz > 0 ? fmt(stats.poll_hz, 0) : '—';
  $('rttMs').textContent = stats.rtt_ms >= 0 ? `${fmt(stats.rtt_ms, 1)} ms` : '— ms';
  $('gapCount').textContent = String(stats.gap_count || 0);
  $('gapFoot').textContent = `Estimated missing frames: ${stats.missing_estimate || 0}`;
  $('gapIcon').className = `metric-icon ${(stats.gap_count || 0) > 10 ? 'red' : (stats.gap_count || 0) > 0 ? 'amber' : 'green'}`;
  $('moveHz').textContent = stats.move_hz > 0 ? fmt(stats.move_hz, 0) : (settings.mouse_enabled ? '0' : 'OFF');
  $('moveFoot').textContent = state.mouse_error || (settings.mouse_enabled ? 'Cursor injection rate' : 'Mouse output disabled');
  $('chartStatus').textContent = isRunning ? (stats.calibrating ? 'CALIBRATING' : stats.measuring ? 'LIVE' : 'PHYPhOX PAUSED') : 'WAITING FOR DATA';
  $('rawValues').textContent = formatVector(stats.raw);
  $('biasValues').textContent = formatVector(stats.bias);
  $('filteredValues').textContent = formatVector(stats.filtered);
  $('overrunValue').textContent = `${stats.buffer_overruns || 0} suspected overruns`;
  $('signalReadout').textContent = `x ${fmt(stats.filtered?.[0] || 0,3)} · y ${fmt(stats.filtered?.[1] || 0,3)} · z ${fmt(stats.filtered?.[2] || 0,3)}`;
  $('networkState').textContent = isConnected ? 'Connected' : 'Offline';
  $('networkDot').className = `dot ${isConnected ? 'green' : ''}`;
  if (discovery.title && discovery.title !== 'Not connected') $('experimentName').textContent = discovery.title;
  if (stats.calibrating) $('trackingHint').textContent = `Calibrating baseline · ${stats.calibration_samples || 0} samples collected. Hold still.`;
  else if (isRunning && state.message) $('trackingHint').textContent = state.message;
  drawChart(stats.chart || []);
  updateContextualControls();
}

function drawChart(series) {
  const canvas = $('signalChart');
  const ctx = canvas.getContext('2d');
  const rect = canvas.getBoundingClientRect();
  const dpr = Math.max(1, window.devicePixelRatio || 1);
  const w = Math.max(300, Math.round(rect.width * dpr));
  const h = Math.max(120, Math.round(rect.height * dpr));
  if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
  ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = '#0b111a'; ctx.fillRect(0, 0, w, h);
  const pad = {l: 18*dpr, r: 10*dpr, t: 12*dpr, b: 13*dpr};
  const cw = w-pad.l-pad.r, ch=h-pad.t-pad.b;
  ctx.strokeStyle = '#1b293a'; ctx.lineWidth = 1*dpr;
  for (let i=0; i<=4; i++) {
    const y = pad.t + ch*i/4;
    ctx.beginPath(); ctx.moveTo(pad.l,y); ctx.lineTo(w-pad.r,y); ctx.stroke();
  }
  for (let i=0; i<=8; i++) {
    const x = pad.l + cw*i/8;
    ctx.beginPath(); ctx.moveTo(x,pad.t); ctx.lineTo(x,h-pad.b); ctx.stroke();
  }
  if (!series.length) {
    ctx.fillStyle = '#687990'; ctx.font = `${12*dpr}px system-ui`;
    ctx.fillText('Waiting for sensor samples…', pad.l+12*dpr, pad.t+ch/2);
    return;
  }
  const values = series.flatMap(row => Array.isArray(row) ? row : []);
  let max = Math.max(0.12, ...values.map(v => Math.abs(Number(v)||0)));
  max *= 1.12;
  const colors = ['#8dbdff','#70e5aa','#b5a1ff'];
  for (let axis=0; axis<3; axis++) {
    ctx.beginPath(); ctx.strokeStyle = colors[axis]; ctx.lineWidth = 1.7*dpr; ctx.lineJoin='round'; ctx.lineCap='round';
    let started = false;
    series.forEach((row,i) => {
      if (!Array.isArray(row) || row.length < 3) return;
      const x = pad.l + (series.length <= 1 ? 0 : i/(series.length-1)*cw);
      const v = Math.max(-max,Math.min(max,Number(row[axis])||0));
      const y = pad.t + ch/2 - (v/max)*(ch/2);
      if (!started) { ctx.moveTo(x,y); started=true; } else ctx.lineTo(x,y);
    });
    ctx.stroke();
  }
  ctx.strokeStyle = '#2c3b4f'; ctx.setLineDash([3*dpr,4*dpr]); ctx.beginPath(); ctx.moveTo(pad.l,pad.t+ch/2); ctx.lineTo(w-pad.r,pad.t+ch/2); ctx.stroke(); ctx.setLineDash([]);
}

let readyStarted = false;
function onReady() {
  if (readyStarted) return;
  readyStarted = true;
  bindControls();
  try {
    api().bootstrap().then(result => {
      state = result;
      settings = result.settings || {};
      discovery = result.discovery || discovery;
      populateDiscovery();
      syncControls();
      renderState(result);
      if (settings.phone_url) $('phoneUrl').value = settings.phone_url;
      scheduleStatePoll();
    }).catch(err => {
      $('footerStatus').textContent = err.message || String(err);
      scheduleStatePoll();
    });
  } catch (err) {
    $('footerStatus').textContent = 'Run the app using the desktop launcher to enable mouse control.';
  }
}

function scheduleStatePoll() {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(async () => {
    await refreshState();
    scheduleStatePoll();
  }, 130);
}

window.addEventListener('pywebviewready', onReady);
window.addEventListener('resize', () => drawChart(state?.stats?.chart || []));
if (window.pywebview && window.pywebview.api) onReady();
