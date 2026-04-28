'use strict';

// ── Constants ─────────────────────────────────────────────────────────────────
const CHUNK_SIZE   = 10 * 1024 * 1024;
const MAX_BYTES    = 10 * 1024 * 1024 * 1024;
const MAX_CONC     = 3;
const MAX_RETRIES  = 4;
const SAMPLE_BYTES = 512 * 1024;
const LS_KEY       = 'datadock_apikey';

// ── State ─────────────────────────────────────────────────────────────────────
let apiKey = localStorage.getItem(LS_KEY) || '';
let activeUploaders = [];
let uploadsChart = null;

// ── Helpers ───────────────────────────────────────────────────────────────────

function formatBytes(b) {
  if (!b || b === 0) return '0 B';
  if (b < 1024)       return b + ' B';
  if (b < 1048576)    return (b / 1024).toFixed(1) + ' KB';
  if (b < 1073741824) return (b / 1048576).toFixed(1) + ' MB';
  return (b / 1073741824).toFixed(2) + ' GB';
}

function formatDate(iso) {
  if (!iso) return '';
  return new Date(iso).toLocaleDateString('en-US', { year: 'numeric', month: 'short', day: 'numeric' });
}

const FILE_TYPES = {
  zip:  { fa: 'fa-file-zipper',     color: '#f59e0b' },
  rar:  { fa: 'fa-file-zipper',     color: '#f59e0b' },
  gz:   { fa: 'fa-file-zipper',     color: '#f59e0b' },
  tar:  { fa: 'fa-file-zipper',     color: '#f59e0b' },
  '7z': { fa: 'fa-file-zipper',     color: '#f59e0b' },
  mp4:  { fa: 'fa-file-video',      color: '#ef4444' },
  mkv:  { fa: 'fa-file-video',      color: '#ef4444' },
  avi:  { fa: 'fa-file-video',      color: '#ef4444' },
  mov:  { fa: 'fa-file-video',      color: '#ef4444' },
  webm: { fa: 'fa-file-video',      color: '#ef4444' },
  mp3:  { fa: 'fa-file-audio',      color: '#8b5cf6' },
  wav:  { fa: 'fa-file-audio',      color: '#8b5cf6' },
  flac: { fa: 'fa-file-audio',      color: '#8b5cf6' },
  aac:  { fa: 'fa-file-audio',      color: '#8b5cf6' },
  jpg:  { fa: 'fa-file-image',      color: '#0ea5e9' },
  jpeg: { fa: 'fa-file-image',      color: '#0ea5e9' },
  png:  { fa: 'fa-file-image',      color: '#0ea5e9' },
  gif:  { fa: 'fa-file-image',      color: '#0ea5e9' },
  webp: { fa: 'fa-file-image',      color: '#0ea5e9' },
  svg:  { fa: 'fa-file-image',      color: '#0ea5e9' },
  pdf:  { fa: 'fa-file-pdf',        color: '#dc2626' },
  doc:  { fa: 'fa-file-word',       color: '#2563eb' },
  docx: { fa: 'fa-file-word',       color: '#2563eb' },
  xls:  { fa: 'fa-file-excel',      color: '#16a34a' },
  xlsx: { fa: 'fa-file-excel',      color: '#16a34a' },
  csv:  { fa: 'fa-file-csv',        color: '#16a34a' },
  ppt:  { fa: 'fa-file-powerpoint', color: '#ea580c' },
  pptx: { fa: 'fa-file-powerpoint', color: '#ea580c' },
  txt:  { fa: 'fa-file-lines',      color: '#64748b' },
  md:   { fa: 'fa-file-lines',      color: '#64748b' },
  js:   { fa: 'fa-file-code',       color: '#6366f1' },
  ts:   { fa: 'fa-file-code',       color: '#6366f1' },
  py:   { fa: 'fa-file-code',       color: '#6366f1' },
  html: { fa: 'fa-file-code',       color: '#6366f1' },
  css:  { fa: 'fa-file-code',       color: '#6366f1' },
  json: { fa: 'fa-file-code',       color: '#6366f1' },
  exe:  { fa: 'fa-gear',            color: '#4b5563' },
  msi:  { fa: 'fa-gear',            color: '#4b5563' },
  dmg:  { fa: 'fa-compact-disc',    color: '#4b5563' },
  iso:  { fa: 'fa-compact-disc',    color: '#4b5563' },
  apk:  { fa: 'fa-mobile-screen',   color: '#16a34a' },
  nsp:  { fa: 'fa-gamepad',         color: '#dc2626' },
  xci:  { fa: 'fa-gamepad',         color: '#dc2626' },
  rom:  { fa: 'fa-gamepad',         color: '#dc2626' },
};

function fileIcon(name) {
  const ext = (name || '').split('.').pop().toLowerCase();
  const t = FILE_TYPES[ext] || { fa: 'fa-file', color: '#64748b' };
  return `<i class="fa-solid ${t.fa}" style="color:${t.color}"></i>`;
}

async function fingerprint(file) {
  const parts = [
    file.slice(0, SAMPLE_BYTES),
    file.slice(Math.max(0, file.size - SAMPLE_BYTES)),
    new Blob([`${file.size}:${file.name}`]),
  ];
  const buf = await new Blob(parts).arrayBuffer();
  const hash = await crypto.subtle.digest('SHA-256', buf);
  return Array.from(new Uint8Array(hash)).map(b => b.toString(16).padStart(2, '0')).join('');
}

async function apiFetch(method, path, body = null) {
  const opts = { method, headers: { 'X-API-Key': apiKey } };
  if (body !== null && method !== 'GET') {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || `HTTP ${res.status}`);
  }
  return res.json();
}

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

// ── ChunkedUploader ────────────────────────────────────────────────────────────

class ChunkedUploader extends EventTarget {
  constructor(file) {
    super();
    this.file      = file;
    this.uploadId  = null;
    this.aborted   = false;
    this.completedParts = new Map();
    this.chunkProgress  = new Map();
    this._xhr      = null;
  }

  emit(name, detail = {}) {
    this.dispatchEvent(new CustomEvent(name, { detail }));
  }

  async start() {
    this.emit('status', { status: 'hashing' });
    const hash = await fingerprint(this.file);

    this.emit('status', { status: 'initializing' });
    const init = await apiFetch('POST', '/api/upload/init', {
      filename:     this.file.name,
      file_size:    this.file.size,
      file_hash:    hash,
      content_type: this.file.type || 'application/octet-stream',
    });

    this.uploadId = init.upload_id;
    for (const p of (init.completed_parts || [])) {
      this.completedParts.set(p.part_number, p.etag);
    }

    const totalParts = Math.ceil(this.file.size / CHUNK_SIZE);
    if (init.resuming && this.completedParts.size > 0) {
      this.emit('resuming', { done: this.completedParts.size, total: totalParts });
    }

    this.emit('status', { status: 'uploading' });
    this._emitProgress(totalParts);
    await this._uploadAllParts(totalParts);
    if (this.aborted) return;

    this.emit('status', { status: 'completing' });
    const result = await apiFetch('POST', `/api/upload/${this.uploadId}/complete`);
    this.emit('done', result);
  }

  async _uploadAllParts(totalParts) {
    const pending = [];
    for (let i = 1; i <= totalParts; i++) {
      if (!this.completedParts.has(i)) pending.push(i);
    }
    if (!pending.length) return;

    let qi = 0;
    const workers = Array.from({ length: Math.min(MAX_CONC, pending.length) }, async () => {
      while (qi < pending.length && !this.aborted) {
        const pn = pending[qi++];
        await this._uploadPartRetry(pn, totalParts);
      }
    });
    await Promise.all(workers);
  }

  async _uploadPartRetry(partNum, totalParts) {
    for (let attempt = 0; attempt < MAX_RETRIES; attempt++) {
      try {
        await this._uploadPart(partNum, totalParts);
        return;
      } catch (err) {
        if (this.aborted) throw err;
        if (attempt === MAX_RETRIES - 1) throw err;
        await sleep(1000 * 2 ** attempt);
      }
    }
  }

  async _uploadPart(partNum, totalParts) {
    const start = (partNum - 1) * CHUNK_SIZE;
    const end   = Math.min(start + CHUNK_SIZE, this.file.size);
    const blob  = this.file.slice(start, end);

    const result = await this._xhrPost(
      `/api/upload/${this.uploadId}/chunk/${partNum}`,
      blob, partNum, totalParts
    );

    this.completedParts.set(partNum, result.etag);
    this.chunkProgress.delete(partNum);
    this._emitProgress(totalParts);
  }

  _xhrPost(url, blob, partNum, totalParts) {
    return new Promise((resolve, reject) => {
      if (this.aborted) return reject(new Error('Aborted'));

      const xhr = new XMLHttpRequest();
      xhr.open('POST', url);
      xhr.setRequestHeader('X-API-Key', apiKey);
      xhr.setRequestHeader('Content-Type', 'application/octet-stream');

      xhr.upload.addEventListener('progress', e => {
        if (e.lengthComputable) {
          this.chunkProgress.set(partNum, e.loaded);
          this._emitProgress(totalParts);
        }
      });

      xhr.addEventListener('load', () => {
        if (xhr.status >= 200 && xhr.status < 300) resolve(JSON.parse(xhr.responseText));
        else reject(new Error(`HTTP ${xhr.status}`));
      });

      xhr.addEventListener('error',  () => reject(new Error('Network error')));
      xhr.addEventListener('abort',  () => reject(new Error('Aborted')));
      xhr.send(blob);
      this._xhr = xhr;
    });
  }

  _emitProgress(totalParts) {
    let bytes = 0;
    for (const [pn] of this.completedParts) {
      const s = (pn - 1) * CHUNK_SIZE;
      bytes += Math.min(s + CHUNK_SIZE, this.file.size) - s;
    }
    for (const [, b] of this.chunkProgress) bytes += b;
    this.emit('progress', {
      uploaded: bytes,
      total:    this.file.size,
      percent:  Math.min(100, Math.round((bytes / this.file.size) * 100)),
    });
  }

  abort() {
    this.aborted = true;
    if (this._xhr) this._xhr.abort();
    if (this.uploadId) apiFetch('DELETE', `/api/upload/${this.uploadId}`).catch(() => {});
    this.emit('status', { status: 'aborted' });
  }
}

// ── Upload UI ─────────────────────────────────────────────────────────────────

function buildUploadItem(file) {
  const el = document.createElement('div');
  el.className = 'upload-item';
  el.innerHTML = `
    <div class="upload-item-header">
      <div class="file-icon-badge">${fileIcon(file.name)}</div>
      <div class="file-meta">
        <div class="file-name" title="${file.name}">${file.name}</div>
        <div class="file-size-label">${formatBytes(file.size)}</div>
      </div>
      <div class="item-actions">
        <span class="status-badge hashing">Hashing…</span>
        <button class="btn-sm danger abort-btn">Cancel</button>
      </div>
    </div>
    <div class="progress-wrap"><div class="progress-bar" style="width:0%"></div></div>
    <div class="progress-labels">
      <span class="pct-text">0%</span>
      <span class="spd-text"></span>
    </div>`;
  return el;
}

function attachUploaderEvents(uploader, el) {
  const badge    = el.querySelector('.status-badge');
  const bar      = el.querySelector('.progress-bar');
  const pctText  = el.querySelector('.pct-text');
  const spdText  = el.querySelector('.spd-text');
  const abortBtn = el.querySelector('.abort-btn');
  let lastBytes = 0, lastTime = Date.now();

  abortBtn.addEventListener('click', () => uploader.abort());

  uploader.addEventListener('status', e => {
    const s = e.detail.status;
    badge.className = `status-badge ${s}`;
    badge.textContent = {
      hashing: 'Hashing…', initializing: 'Starting…', uploading: 'Uploading',
      resuming: 'Resuming…', completing: 'Finalizing…', done: 'Done',
      error: 'Error', aborted: 'Cancelled',
    }[s] || s;
    if (s === 'aborted') { abortBtn.remove(); bar.classList.add('error'); }
  });

  uploader.addEventListener('resuming', e => {
    badge.className = 'status-badge resuming';
    badge.textContent = `Resuming (${e.detail.done}/${e.detail.total} parts)`;
  });

  uploader.addEventListener('progress', e => {
    const { uploaded, percent } = e.detail;
    bar.style.width = percent + '%';
    pctText.textContent = percent + '%';
    const now = Date.now(), dt = (now - lastTime) / 1000;
    if (dt >= 1) {
      spdText.textContent = formatBytes(Math.max(0, (uploaded - lastBytes) / dt)) + '/s';
      lastBytes = uploaded; lastTime = now;
    }
  });

  uploader.addEventListener('done', e => {
    const { share_url, direct_url, filename } = e.detail;
    bar.style.width = '100%';
    bar.classList.add('success');
    badge.className = 'status-badge done';
    badge.textContent = 'Done';
    abortBtn.remove();
    pctText.textContent = '100%';
    spdText.textContent = '';

    const doneRow = document.createElement('div');
    doneRow.className = 'done-row';
    doneRow.innerHTML = `
      <button class="done-link-btn" data-url="${window.location.origin}${share_url}">🔗 Copy Share Link</button>
      <button class="done-direct-btn" data-url="${direct_url}">⬇️ Copy Direct Link</button>`;

    doneRow.querySelector('.done-link-btn').addEventListener('click', e => {
      navigator.clipboard.writeText(e.target.dataset.url);
      e.target.textContent = '✓ Copied!';
      setTimeout(() => { e.target.textContent = '🔗 Copy Share Link'; }, 2000);
    });
    doneRow.querySelector('.done-direct-btn').addEventListener('click', e => {
      navigator.clipboard.writeText(e.target.dataset.url);
      e.target.textContent = '✓ Copied!';
      setTimeout(() => { e.target.textContent = '⬇️ Copy Direct Link'; }, 2000);
    });

    el.appendChild(doneRow);
    loadDashboard();
  });
}

async function startUpload(file) {
  if (file.size > MAX_BYTES) {
    alert(`"${file.name}" exceeds the 10 GB limit.`);
    return;
  }
  const queue = document.getElementById('upload-queue');
  const wrap  = document.getElementById('upload-queue-wrap');
  const el = buildUploadItem(file);
  queue.appendChild(el);
  wrap.hidden = false;

  const uploader = new ChunkedUploader(file);
  activeUploaders.push(uploader);
  attachUploaderEvents(uploader, el);

  try {
    await uploader.start();
  } catch (err) {
    if (!uploader.aborted) {
      el.querySelector('.status-badge').className = 'status-badge error';
      el.querySelector('.status-badge').textContent = 'Error';
      el.querySelector('.progress-bar').classList.add('error');
      el.querySelector('.spd-text').textContent = err.message;
      el.querySelector('.abort-btn')?.remove();
    }
  } finally {
    activeUploaders = activeUploaders.filter(u => u !== uploader);
  }
}

// ── Dashboard ─────────────────────────────────────────────────────────────────

async function loadDashboard() {
  try {
    const stats = await apiFetch('GET', '/api/stats');
    document.getElementById('stat-files').textContent     = stats.total_files.toLocaleString();
    document.getElementById('stat-storage').textContent   = formatBytes(stats.total_size);
    document.getElementById('stat-downloads').textContent = stats.total_downloads.toLocaleString();
    renderChart(stats.uploads_per_day);
    await loadRecentFiles();
  } catch (e) {
    console.error('Dashboard error:', e);
  }
}

function renderChart(data) {
  const ctx = document.getElementById('uploads-chart').getContext('2d');
  if (uploadsChart) uploadsChart.destroy();
  uploadsChart = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: data.map(d => d.date),
      datasets: [{
        label: 'Files',
        data: data.map(d => d.count),
        backgroundColor: 'rgba(99,102,241,0.85)',
        borderRadius: 6,
        borderSkipped: false,
      }],
    },
    options: {
      responsive: true,
      plugins: { legend: { display: false } },
      scales: {
        y: { beginAtZero: true, ticks: { stepSize: 1, precision: 0 }, grid: { color: '#e2e8f0' } },
        x: { grid: { display: false } },
      },
    },
  });
}

async function loadRecentFiles() {
  const el = document.getElementById('recent-files');
  const files = await apiFetch('GET', '/api/files');
  const recent = files.slice(0, 6);
  if (!recent.length) {
    el.innerHTML = '<p style="padding:1rem;color:var(--muted);font-size:.875rem">No files yet. Upload something!</p>';
    return;
  }
  el.innerHTML = recent.map(f => `
    <div class="recent-file-row">
      <span class="rf-icon">${fileIcon(f.filename)}</span>
      <span class="rf-name">${f.filename}</span>
      <span class="rf-size">${formatBytes(f.file_size)}</span>
      <span class="rf-date">${formatDate(f.completed_at)}</span>
    </div>`).join('');
}

// ── File list ─────────────────────────────────────────────────────────────────

async function loadFileList() {
  const list = document.getElementById('file-list');
  try {
    const files = await apiFetch('GET', '/api/files');
    if (!files.length) {
      list.innerHTML = '<p style="padding:1.5rem;color:var(--muted);font-size:.875rem;text-align:center">No files yet.</p>';
      return;
    }

    const table = document.createElement('table');
    table.className = 'files-table';
    table.innerHTML = `
      <thead>
        <tr>
          <th>File</th><th>Size</th><th>Date</th>
          <th>Views</th><th>Downloads</th><th>Actions</th>
        </tr>
      </thead>
      <tbody></tbody>`;
    list.innerHTML = '';
    list.appendChild(table);
    const tbody = table.querySelector('tbody');

    files.forEach(f => {
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td><span class="tf-icon">${fileIcon(f.filename)}</span><span class="tf-name">${f.filename}</span></td>
        <td>${formatBytes(f.file_size)}</td>
        <td>${formatDate(f.completed_at)}</td>
        <td>${(f.views || 0).toLocaleString()}</td>
        <td>${(f.downloads || 0).toLocaleString()}</td>
        <td class="tf-actions">
          ${f.share_id ? `<button class="btn-share">Share</button>` : ''}
          <button class="btn-direct">Direct</button>
          <button class="btn-delete">Delete</button>
        </td>`;

      if (f.share_id) {
        const shareUrl = `${window.location.origin}/f/${f.share_id}`;
        tr.querySelector('.btn-share').addEventListener('click', e => {
          navigator.clipboard.writeText(shareUrl);
          e.target.textContent = 'Copied!';
          setTimeout(() => { e.target.textContent = 'Share'; }, 2000);
        });
      }

      tr.querySelector('.btn-direct').addEventListener('click', e => {
        navigator.clipboard.writeText(f.direct_url);
        e.target.textContent = 'Copied!';
        setTimeout(() => { e.target.textContent = 'Direct'; }, 2000);
      });

      tr.querySelector('.btn-delete').addEventListener('click', async e => {
        if (!confirm(`Delete "${f.filename}"?`)) return;
        try {
          await apiFetch('DELETE', `/api/files/${f.id}`);
          tr.remove();
          loadDashboard();
        } catch (err) { alert('Delete failed: ' + err.message); }
      });

      tbody.appendChild(tr);
    });
  } catch (e) {
    list.innerHTML = `<p style="color:var(--danger);padding:1rem">Failed: ${e.message}</p>`;
  }
}

// ── URL Import ────────────────────────────────────────────────────────────────

function buildImportItem(filename, total) {
  const el = document.createElement('div');
  el.className = 'upload-item';
  el.innerHTML = `
    <div class="upload-item-header">
      <div class="file-icon-badge"><i class="fa-solid fa-link" style="color:#6366f1"></i></div>
      <div class="file-meta">
        <div class="file-name" title="${filename}">${filename}</div>
        <div class="file-size-label">${total ? formatBytes(total) : 'Size unknown'}</div>
      </div>
      <div class="item-actions">
        <span class="status-badge initializing">Connecting…</span>
      </div>
    </div>
    <div class="progress-wrap"><div class="progress-bar" style="width:0%"></div></div>
    <div class="progress-labels">
      <span class="pct-text">0%</span>
      <span class="spd-text"></span>
    </div>`;
  return el;
}

async function startImport(url, filename) {
  const errEl  = document.getElementById('import-error');
  const queue  = document.getElementById('import-queue');
  errEl.textContent = '';

  let initData;
  try {
    initData = await apiFetch('POST', '/api/import', { url, filename: filename || null });
  } catch (e) {
    errEl.textContent = e.message;
    return;
  }

  const { upload_id, filename: detectedName, total } = initData;
  const el    = buildImportItem(detectedName, total);
  const badge = el.querySelector('.status-badge');
  const bar   = el.querySelector('.progress-bar');
  const pct   = el.querySelector('.pct-text');
  const spd   = el.querySelector('.spd-text');
  queue.prepend(el);

  let lastBytes = 0, lastTime = Date.now();

  const poll = setInterval(async () => {
    let prog;
    try { prog = await apiFetch('GET', `/api/import/${upload_id}/status`); }
    catch { return; }

    const { status, bytes_done = 0, total: tot = total, error } = prog;

    // Update progress bar
    const pctVal = tot ? Math.min(100, Math.round((bytes_done / tot) * 100)) : 0;
    bar.style.width = pctVal + '%';
    pct.textContent = tot ? pctVal + '%' : formatBytes(bytes_done);

    // Speed
    const now = Date.now(), dt = (now - lastTime) / 1000;
    if (dt >= 1 && bytes_done > lastBytes) {
      spd.textContent = formatBytes((bytes_done - lastBytes) / dt) + '/s';
      lastBytes = bytes_done; lastTime = now;
    }

    if (status === 'importing') {
      badge.className = 'status-badge uploading';
      badge.textContent = 'Downloading…';
    } else if (status === 'completing') {
      badge.className = 'status-badge completing';
      badge.textContent = 'Finalizing…';
      bar.style.width = '98%';
    } else if (status === 'completed') {
      clearInterval(poll);
      bar.style.width = '100%';
      bar.classList.add('success');
      badge.className = 'status-badge done';
      badge.textContent = 'Done';
      pct.textContent = '100%';
      spd.textContent = '';

      const doneRow = document.createElement('div');
      doneRow.className = 'done-row';
      doneRow.innerHTML = `
        <button class="done-link-btn" data-url="${window.location.origin}${prog.share_url}">🔗 Copy Share Link</button>
        <button class="done-direct-btn" data-url="${prog.direct_url}">⬇️ Copy Direct Link</button>`;
      doneRow.querySelector('.done-link-btn').addEventListener('click', e => {
        navigator.clipboard.writeText(e.target.dataset.url);
        e.target.textContent = '✓ Copied!';
        setTimeout(() => { e.target.textContent = '🔗 Copy Share Link'; }, 2000);
      });
      doneRow.querySelector('.done-direct-btn').addEventListener('click', e => {
        navigator.clipboard.writeText(e.target.dataset.url);
        e.target.textContent = '✓ Copied!';
        setTimeout(() => { e.target.textContent = '⬇️ Copy Direct Link'; }, 2000);
      });
      el.appendChild(doneRow);
      loadDashboard();
    } else if (status === 'failed') {
      clearInterval(poll);
      bar.classList.add('error');
      badge.className = 'status-badge error';
      badge.textContent = 'Failed';
      spd.textContent = error || 'Unknown error';
    }
  }, 2000);
}

function initImportForm() {
  document.getElementById('import-btn').addEventListener('click', () => {
    const url      = document.getElementById('import-url-input').value.trim();
    const filename = document.getElementById('import-name-input').value.trim();
    if (!url) {
      document.getElementById('import-error').textContent = 'Please enter a URL.';
      return;
    }
    document.getElementById('import-url-input').value  = '';
    document.getElementById('import-name-input').value = '';
    startImport(url, filename);
  });
  document.getElementById('import-url-input').addEventListener('keydown', e => {
    if (e.key === 'Enter') document.getElementById('import-btn').click();
  });
}

// ── Ad Manager ────────────────────────────────────────────────────────────────

async function loadAdsPage() {
  const list = document.getElementById('ad-list');
  try {
    const ads = await apiFetch('GET', '/api/admin/ads');
    if (!ads.length) {
      list.innerHTML = '<p style="padding:1.5rem;color:var(--muted);font-size:.875rem;text-align:center">No ads yet. Add one above.</p>';
      return;
    }
    list.innerHTML = '';
    const table = document.createElement('table');
    table.className = 'files-table';
    table.innerHTML = `
      <thead>
        <tr><th>Type</th><th>Label</th><th>Link</th><th>Order</th><th>Status</th><th>Actions</th></tr>
      </thead>
      <tbody></tbody>`;
    list.appendChild(table);
    const tbody = table.querySelector('tbody');

    ads.forEach(ad => {
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td><span class="ad-type-badge ad-type-${ad.type}">${ad.type}</span></td>
        <td class="tf-name" style="max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${ad.label}">${ad.label}</td>
        <td style="max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"><a href="${ad.link_url}" target="_blank" rel="noopener" style="color:var(--primary)">${ad.link_url}</a></td>
        <td>${ad.display_order}</td>
        <td><span class="ad-status ${ad.active ? 'ad-on' : 'ad-off'}">${ad.active ? 'Active' : 'Paused'}</span></td>
        <td class="tf-actions">
          <button class="btn-share toggle-btn">${ad.active ? 'Pause' : 'Enable'}</button>
          <button class="btn-delete delete-btn">Delete</button>
        </td>`;

      tr.querySelector('.toggle-btn').addEventListener('click', async () => {
        try {
          await apiFetch('PATCH', `/api/admin/ads/${ad.id}`, { ...ad, active: ad.active ? 0 : 1 });
          loadAdsPage();
        } catch (e) { alert('Failed: ' + e.message); }
      });

      tr.querySelector('.delete-btn').addEventListener('click', async () => {
        if (!confirm(`Delete ad "${ad.label}"?`)) return;
        try {
          await apiFetch('DELETE', `/api/admin/ads/${ad.id}`);
          loadAdsPage();
        } catch (e) { alert('Failed: ' + e.message); }
      });

      tbody.appendChild(tr);
    });
  } catch (e) {
    list.innerHTML = `<p style="color:var(--danger);padding:1rem">Failed: ${e.message}</p>`;
  }
}

function initAdForm() {
  const form     = document.getElementById('ad-form');
  const typeEl   = document.getElementById('ad-type');
  const imageWrap = document.getElementById('ad-image-wrap');
  const msgEl    = document.getElementById('ad-form-msg');

  typeEl.addEventListener('change', () => {
    imageWrap.style.opacity = typeEl.value === 'banner' ? '1' : '0.4';
  });

  form.addEventListener('submit', async e => {
    e.preventDefault();
    msgEl.textContent = '';
    const body = {
      type:          document.getElementById('ad-type').value,
      label:         document.getElementById('ad-label').value.trim(),
      image_url:     document.getElementById('ad-image-url').value.trim() || null,
      link_url:      document.getElementById('ad-link-url').value.trim(),
      active:        document.getElementById('ad-active').checked ? 1 : 0,
      display_order: parseInt(document.getElementById('ad-order').value, 10) || 0,
    };
    try {
      await apiFetch('POST', '/api/admin/ads', body);
      form.reset();
      document.getElementById('ad-active').checked = true;
      msgEl.style.color = 'var(--success)';
      msgEl.textContent = 'Ad added!';
      setTimeout(() => { msgEl.textContent = ''; }, 3000);
      loadAdsPage();
    } catch (err) {
      msgEl.style.color = 'var(--danger)';
      msgEl.textContent = err.message;
    }
  });
}

// ── Page navigation ───────────────────────────────────────────────────────────

function showPage(page) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(b => b.classList.remove('active'));
  document.getElementById(`page-${page}`).classList.add('active');
  document.querySelector(`[data-page="${page}"]`).classList.add('active');
  if (page === 'dashboard') loadDashboard();
  if (page === 'files')     loadFileList();
  if (page === 'ads')       loadAdsPage();
}

// ── Auth ──────────────────────────────────────────────────────────────────────

async function checkAuth() {
  try { await apiFetch('POST', '/api/auth/verify'); return true; }
  catch { return false; }
}

async function initAuth() {
  const overlay = document.getElementById('auth-overlay');

  if (apiKey) {
    if (await checkAuth()) { overlay.style.display = 'none'; return; }
    localStorage.removeItem(LS_KEY);
    apiKey = '';
  }

  overlay.style.display = 'flex';

  const form  = document.getElementById('auth-form');
  const input = document.getElementById('auth-input');
  const errEl = document.getElementById('auth-error');
  const btn   = document.getElementById('auth-btn');

  form.addEventListener('submit', async e => {
    e.preventDefault();
    const key = input.value.trim();
    if (!key) return;
    btn.disabled = true; btn.textContent = 'Verifying…'; errEl.textContent = '';

    apiKey = key;
    if (await checkAuth()) {
      localStorage.setItem(LS_KEY, key);
      overlay.style.display = 'none';
      initApp();
    } else {
      apiKey = '';
      errEl.textContent = 'Invalid API key.';
      input.select();
    }
    btn.disabled = false; btn.textContent = 'Unlock';
  });
}

function initApp() {
  const badgeText = document.getElementById('key-badge-text');
  if (badgeText) badgeText.textContent = apiKey.slice(0, 4) + '••••';

  document.querySelectorAll('.nav-item[data-page]').forEach(btn => {
    btn.addEventListener('click', () => showPage(btn.dataset.page));
  });
  initImportForm();
  initAdForm();

  document.getElementById('upload-shortcut').addEventListener('click', () => showPage('upload'));

  const zone  = document.getElementById('drop-zone');
  const input = document.getElementById('file-input');
  zone.addEventListener('click', () => input.click());
  zone.addEventListener('dragover', e => { e.preventDefault(); zone.classList.add('drag-over'); });
  zone.addEventListener('dragleave', () => zone.classList.remove('drag-over'));
  zone.addEventListener('drop', e => {
    e.preventDefault(); zone.classList.remove('drag-over');
    [...e.dataTransfer.files].forEach(startUpload);
  });
  input.addEventListener('change', () => { [...input.files].forEach(startUpload); input.value = ''; });

  showPage('dashboard');
}

// ── Boot ──────────────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', async () => {
  await initAuth();
  if (apiKey) initApp();
});
