'use strict';

// ── Constants ─────────────────────────────────────────────────────────────────
const CHUNK_SIZE   = 10 * 1024 * 1024;
const MAX_BYTES    = 500 * 1024 * 1024 * 1024; // 500 GB frontend guard
const MAX_CONC     = 6;   // parallel chunk uploads — uses full available bandwidth
const MAX_RETRIES  = 6;   // retries per chunk before giving up
const SAMPLE_BYTES = 512 * 1024;
const LS_KEY       = 'datadock_apikey';

// ── State ─────────────────────────────────────────────────────────────────────
let apiKey = localStorage.getItem(LS_KEY) || '';
let userRole = 'admin';
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
    this._xhr           = null;
    this._paused        = false;
    this._pauseWaiters  = [];
  }

  emit(name, detail = {}) {
    this.dispatchEvent(new CustomEvent(name, { detail }));
  }

  pause() {
    if (this.aborted || this._paused) return;
    this._paused = true;
    this.emit('status', { status: 'paused' });
  }

  resume() {
    if (!this._paused) return;
    this._paused = false;
    this._pauseWaiters.splice(0).forEach(r => r());
    this.emit('status', { status: 'uploading' });
  }

  _waitIfPaused() {
    if (!this._paused) return Promise.resolve();
    return new Promise(r => this._pauseWaiters.push(r));
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
      await this._waitIfPaused();   // hold here while offline
      if (this.aborted) throw new Error('Aborted');
      try {
        await this._uploadPart(partNum, totalParts);
        return;
      } catch (err) {
        if (this.aborted) throw err;
        if (attempt === MAX_RETRIES - 1) throw err;
        // If we're offline, pause and wait for reconnect instead of sleeping
        if (!navigator.onLine) {
          this.pause();
          await this._waitIfPaused();
        } else {
          await sleep(1000 * 2 ** attempt);
        }
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
      paused: 'Connection lost — reconnecting…',
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
    const [stats, recent] = await Promise.all([
      apiFetch('GET', '/api/stats'),
      apiFetch('GET', '/api/files?limit=6'),
    ]);
    document.getElementById('stat-files').textContent     = stats.total_files.toLocaleString();
    document.getElementById('stat-storage').textContent   = formatBytes(stats.total_size);
    document.getElementById('stat-downloads').textContent = stats.total_downloads.toLocaleString();
    renderChart(stats.uploads_per_day);
    _renderRecentFiles(recent);
  } catch (e) {
    console.error('Dashboard error:', e);
  }
  // Refresh report badge count
  if (userRole === 'admin') {
    apiFetch('GET', '/api/admin/reports?status=open')
      .then(d => {
        const badge = document.getElementById('rpt-nav-badge');
        if (badge) { badge.hidden = !d.open_count; badge.textContent = d.open_count; }
      })
      .catch(() => {});
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

function _renderRecentFiles(files) {
  const el = document.getElementById('recent-files');
  if (!files.length) {
    el.innerHTML = '<p style="padding:1rem;color:var(--muted);font-size:.875rem">No files yet. Upload something!</p>';
    return;
  }
  el.innerHTML = files.map(f => `
    <div class="recent-file-row">
      <span class="rf-icon">${fileIcon(f.filename)}</span>
      <span class="rf-name">${f.filename}</span>
      <span class="rf-size">${formatBytes(f.file_size)}</span>
      <span class="rf-date">${formatDate(f.completed_at)}</span>
    </div>`).join('');
}

// ── File list ─────────────────────────────────────────────────────────────────

function _buildFileRow(f, canDelete, showUploader = false) {
  const meta = providerMeta(f.storage_name || '');
  const uploaderCell = showUploader
    ? `<td><span class="uploader-chip">${f.uploaded_by || '<span style="color:var(--muted)">—</span>'}</span></td>`
    : '';
  const tr = document.createElement('tr');
  tr.innerHTML = `
    <td><span class="tf-icon">${fileIcon(f.filename)}</span><span class="tf-name">${f.filename}</span></td>
    <td>${formatBytes(f.file_size)}</td>
    <td><span class="sp-chip" title="${f.storage_name || 'Default (env)'}"><i class="fa-solid ${meta.icon}" style="color:${meta.color}"></i> ${f.storage_name || 'Default'}</span></td>
    ${uploaderCell}
    <td>${formatDate(f.completed_at)}</td>
    <td>${(f.views || 0).toLocaleString()}</td>
    <td>${(f.downloads || 0).toLocaleString()}</td>
    <td class="tf-actions">
      ${f.share_id ? `<button class="btn-share">Share</button>` : ''}
      <button class="btn-direct">Direct</button>
      ${canDelete ? '<button class="btn-delete">Delete</button>' : ''}
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

  if (canDelete) {
    tr.querySelector('.btn-delete').addEventListener('click', async () => {
      if (!confirm(`Delete "${f.filename}"?`)) return;
      try {
        await apiFetch('DELETE', `/api/files/${f.id}`);
        tr.remove();
        loadDashboard();
      } catch (err) { alert('Delete failed: ' + err.message); }
    });
  }

  return tr;
}

function _renderFilesTable(container, files, canDelete, showUploader = false) {
  const table = document.createElement('table');
  table.className = 'files-table';
  table.innerHTML = `
    <thead>
      <tr>
        <th>File</th><th>Size</th><th>Storage</th>
        ${showUploader ? '<th>Uploaded by</th>' : ''}
        <th>Date</th><th>Views</th><th>DLs</th><th>Actions</th>
      </tr>
    </thead>
    <tbody></tbody>`;
  const tbody = table.querySelector('tbody');
  files.forEach(f => tbody.appendChild(_buildFileRow(f, canDelete, showUploader)));
  container.appendChild(table);
}

function _buildFolderCard(displayName, files, canDelete = true, showUploader = false) {
  const totalSize = files.reduce((s, f) => s + (f.file_size || 0), 0);

  const card = document.createElement('div');
  card.className = 'folder-card';
  card.innerHTML = `
    <div class="folder-header">
      <div class="folder-title">
        <i class="fa-solid fa-folder folder-icon"></i>
        <span class="folder-name">${displayName}</span>
        <span class="folder-meta">${files.length} file${files.length !== 1 ? 's' : ''} &middot; ${formatBytes(totalSize)}</span>
      </div>
      <i class="fa-solid fa-chevron-right folder-chevron"></i>
    </div>
    <div class="folder-body" hidden></div>`;

  const header   = card.querySelector('.folder-header');
  const body     = card.querySelector('.folder-body');
  const folderIc = card.querySelector('.folder-icon');
  const chevron  = card.querySelector('.folder-chevron');
  let expanded = false;
  let rendered = false;

  header.addEventListener('click', () => {
    expanded = !expanded;
    body.hidden = !expanded;
    folderIc.className = `fa-solid ${expanded ? 'fa-folder-open' : 'fa-folder'} folder-icon`;
    chevron.className  = `fa-solid ${expanded ? 'fa-chevron-down' : 'fa-chevron-right'} folder-chevron`;
    if (expanded && !rendered) {
      rendered = true;
      _renderFilesTable(body, files, canDelete, showUploader);
    }
  });

  return card;
}

function _buildAllFilesFolder() {
  const card = document.createElement('div');
  card.className = 'folder-card folder-card-all';
  card.innerHTML = `
    <div class="folder-header">
      <div class="folder-title">
        <i class="fa-solid fa-folder-tree folder-icon-all"></i>
        <span class="folder-name">All Files</span>
        <span class="folder-meta">All team uploads</span>
      </div>
      <i class="fa-solid fa-chevron-right folder-chevron"></i>
    </div>
    <div class="folder-body" hidden></div>`;

  const header   = card.querySelector('.folder-header');
  const body     = card.querySelector('.folder-body');
  const folderIc = card.querySelector('.folder-icon-all');
  const chevron  = card.querySelector('.folder-chevron');
  let expanded = false;
  let rendered = false;

  header.addEventListener('click', async () => {
    expanded = !expanded;
    body.hidden = !expanded;
    chevron.className = `fa-solid ${expanded ? 'fa-chevron-down' : 'fa-chevron-right'} folder-chevron`;

    if (expanded && !rendered) {
      rendered = true;
      body.innerHTML = '<p style="padding:1rem 1.1rem;color:var(--muted);font-size:.875rem">Loading…</p>';
      try {
        const all = await apiFetch('GET', '/api/files?all=1');
        body.innerHTML = '';
        if (!all.length) {
          body.innerHTML = '<p style="padding:1rem 1.1rem;color:var(--muted);font-size:.875rem;text-align:center">No files yet.</p>';
        } else {
          // Update meta badge with real count
          card.querySelector('.folder-meta').textContent =
            `${all.length} file${all.length !== 1 ? 's' : ''} · ${formatBytes(all.reduce((s, f) => s + (f.file_size || 0), 0))}`;
          _renderFilesTable(body, all, false, true); // no delete, show uploader
        }
      } catch (e) {
        body.innerHTML = `<p style="color:var(--danger);padding:1rem">Failed: ${e.message}</p>`;
        rendered = false; // allow retry on next click
        expanded = false;
        body.hidden = true;
        chevron.className = 'fa-solid fa-chevron-right folder-chevron';
      }
    }
  });

  return card;
}

async function loadFileList() {
  const list = document.getElementById('file-list');
  try {
    const files = await apiFetch('GET', '/api/files');
    list.innerHTML = '';

    // "All Files" folder is visible to everyone (admin + members)
    list.appendChild(_buildAllFilesFolder());

    if (userRole === 'admin') {
      if (!files.length) return;
      // Group by named uploader only — unattributed files appear in All Files above
      const groups = new Map();
      for (const f of files) {
        const key = f.uploaded_by || null;
        if (!key) continue; // skip unattributed — covered by All Files
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push(f);
      }
      const sorted = [...groups.entries()].sort(([a], [b]) => a.localeCompare(b));
      for (const [name, groupFiles] of sorted) {
        list.appendChild(_buildFolderCard(name, groupFiles));
      }
    } else {
      // Members: also show their own "My Files" folder
      list.appendChild(_buildFolderCard(
        'My Files', files, false, false
      ));
    }
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

// ── Import persistence (survive page refresh) ─────────────────────────────────

const LS_IMPORTS_KEY = 'datadock_pending_imports';

function _saveImport(upload_id, filename, total, url) {
  const list = JSON.parse(localStorage.getItem(LS_IMPORTS_KEY) || '[]');
  if (!list.find(i => i.upload_id === upload_id)) {
    list.push({ upload_id, filename, total, url });
    localStorage.setItem(LS_IMPORTS_KEY, JSON.stringify(list));
  }
}

function _removeImport(upload_id) {
  const list = JSON.parse(localStorage.getItem(LS_IMPORTS_KEY) || '[]');
  localStorage.setItem(LS_IMPORTS_KEY, JSON.stringify(list.filter(i => i.upload_id !== upload_id)));
}

function _attachImportPoll(upload_id, filename, total, url, el) {
  const badge    = el.querySelector('.status-badge');
  const bar      = el.querySelector('.progress-bar');
  const pct      = el.querySelector('.pct-text');
  const spd      = el.querySelector('.spd-text');
  const abortBtn = el.querySelector('.abort-btn');

  let lastBytes = 0, lastTime = Date.now();
  let cancelled = false;
  let failStreak = 0;

  abortBtn.addEventListener('click', async () => {
    if (cancelled) return;
    cancelled = true;
    clearInterval(poll);
    _removeImport(upload_id);
    abortBtn.remove();
    badge.className = 'status-badge aborted';
    badge.textContent = 'Cancelled';
    bar.classList.add('error');
    spd.textContent = '';
    try { await apiFetch('DELETE', `/api/upload/${upload_id}`); } catch {}
  });

  const poll = setInterval(async () => {
    if (cancelled) { clearInterval(poll); return; }
    let prog;
    try {
      prog = await apiFetch('GET', `/api/import/${upload_id}/status`);
      if (failStreak > 0) {
        failStreak = 0;
        badge.className = 'status-badge uploading';
        badge.textContent = 'Downloading…';
      }
    } catch {
      failStreak++;
      if (failStreak >= 3) {
        badge.className = 'status-badge paused';
        badge.textContent = 'Connection lost — reconnecting…';
      }
      return;
    }

    const { status, bytes_done = 0, total: tot = (total || 0), error } = prog;

    // Update filename label if server detected a different name
    if (prog.filename && prog.filename !== filename) {
      const nameEl = el.querySelector('.file-name');
      if (nameEl) nameEl.textContent = prog.filename;
    }

    const pctVal = tot ? Math.min(100, Math.round((bytes_done / tot) * 100)) : 0;
    bar.style.width = pctVal + '%';
    pct.textContent = tot ? pctVal + '%' : formatBytes(bytes_done);

    const now = Date.now(), dt = (now - lastTime) / 1000;
    if (dt >= 1 && bytes_done > lastBytes) {
      spd.textContent = formatBytes((bytes_done - lastBytes) / dt) + '/s';
      lastBytes = bytes_done; lastTime = now;
    }

    if (status === 'analyzing') {
      badge.className = 'status-badge initializing';
      badge.textContent = 'Scanning page…';
      pct.textContent = 'Opening download page with browser…';
    } else if (status === 'importing') {
      badge.className = 'status-badge uploading';
      badge.textContent = 'Downloading…';
    } else if (status === 'completing') {
      badge.className = 'status-badge completing';
      badge.textContent = 'Finalizing…';
      bar.style.width = '98%';
    } else if (status === 'completed') {
      clearInterval(poll);
      _removeImport(upload_id);
      abortBtn.remove();
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
    } else if (status === 'failed' || status === 'aborted') {
      clearInterval(poll);
      _removeImport(upload_id);
      abortBtn.remove();
      if (status === 'aborted') return;
      bar.classList.add('error');
      badge.className = 'status-badge error';
      badge.textContent = 'Failed';
      spd.textContent = '';
      const errDiv = document.createElement('div');
      errDiv.className = 'import-fail-msg';
      errDiv.innerHTML = `<i class="fa-solid fa-circle-exclamation"></i> ${error || 'Unknown error'}`;
      el.appendChild(errDiv);

      if (error && (error.includes('IP-locked') || error.includes('403'))) {
        const relayBtn = document.createElement('button');
        relayBtn.className = 'btn-primary relay-btn';
        relayBtn.innerHTML = '<i class="fa-solid fa-share-nodes"></i> Try Browser Fetch';
        relayBtn.addEventListener('click', () => {
          errDiv.remove();
          relayBtn.remove();
          bar.classList.remove('error');
          startBrowserRelay(url, filename, el);
        });
        el.appendChild(relayBtn);
      }
    }
  }, 2000);
}

function resumePendingImports() {
  const list = JSON.parse(localStorage.getItem(LS_IMPORTS_KEY) || '[]');
  if (!list.length) return;
  const queue = document.getElementById('import-queue');
  const wrap  = document.getElementById('upload-queue-wrap');
  list.forEach(({ upload_id, filename, total, url }) => {
    const el = buildImportItem(filename, total);
    const badge = el.querySelector('.status-badge');
    badge.className = 'status-badge resuming';
    badge.textContent = 'Reconnecting…';
    queue.prepend(el);
    wrap.hidden = false;
    _attachImportPoll(upload_id, filename, total, url, el);
  });
}

async function startImport(url, filename) {
  const errEl = document.getElementById('import-error');
  const queue = document.getElementById('import-queue');
  const wrap  = document.getElementById('upload-queue-wrap');
  errEl.textContent = '';

  let initData;
  try {
    initData = await apiFetch('POST', '/api/import', { url, filename: filename || null });
  } catch (e) {
    errEl.textContent = e.message;
    return;
  }

  const { upload_id, filename: detectedName, total } = initData;
  _saveImport(upload_id, detectedName, total, url);

  const el = buildImportItem(detectedName, total);
  queue.prepend(el);
  wrap.hidden = false;
  _attachImportPoll(upload_id, detectedName, total, url, el);
}

async function startBrowserRelay(url, filename, el) {
  const badge    = el.querySelector('.status-badge');
  const bar      = el.querySelector('.progress-bar');
  const pct      = el.querySelector('.pct-text');
  const spd      = el.querySelector('.spd-text');
  const actions  = el.querySelector('.item-actions');

  // Add cancel button
  const abortBtn = document.createElement('button');
  abortBtn.className = 'btn-sm danger abort-btn';
  abortBtn.textContent = 'Cancel';
  actions.appendChild(abortBtn);

  const controller = new AbortController();
  let relayUploadId = null;
  let aborted = false;

  abortBtn.addEventListener('click', async () => {
    if (aborted) return;
    aborted = true;
    controller.abort();
    abortBtn.remove();
    badge.className = 'status-badge aborted';
    badge.textContent = 'Cancelled';
    bar.classList.add('error');
    spd.textContent = '';
    if (relayUploadId) {
      try { await apiFetch('DELETE', `/api/upload/${relayUploadId}`); } catch {}
    }
  });

  badge.className = 'status-badge uploading';
  badge.textContent = 'Browser Fetch…';
  bar.style.width = '0%';
  spd.textContent = '';

  let resp;
  try {
    resp = await fetch(url, { signal: controller.signal });
    if (!resp.ok) throw new Error(`Server returned HTTP ${resp.status}`);
  } catch (e) {
    if (aborted) return;
    badge.className = 'status-badge error';
    badge.textContent = 'Failed';
    bar.classList.add('error');
    const errDiv = document.createElement('div');
    errDiv.className = 'import-fail-msg';
    const msg = e.toString().toLowerCase();
    if (msg.includes('failed to fetch') || msg.includes('networkerror') || msg.includes('cors')) {
      errDiv.innerHTML = `<i class="fa-solid fa-ban"></i> CORS blocked: The CDN doesn't allow browser direct access. Please download the file manually and re-upload it here.`;
    } else {
      errDiv.innerHTML = `<i class="fa-solid fa-circle-exclamation"></i> ${e.message}`;
    }
    el.appendChild(errDiv);
    return;
  }

  const ct = (resp.headers.get('content-type') || 'application/octet-stream').split(';')[0].trim();
  const relayFilename = filename ||
    (resp.headers.get('content-disposition') || '').match(/filename="?([^";\r\n]+)"?/i)?.[1] ||
    url.split('?')[0].split('/').pop() || 'downloaded_file';
  const contentLength = parseInt(resp.headers.get('content-length') || '0') || 0;

  let initData;
  try {
    initData = await apiFetch('POST', '/api/upload/relay-init', { filename: relayFilename, content_type: ct });
  } catch (e) {
    badge.className = 'status-badge error';
    badge.textContent = 'Failed';
    bar.classList.add('error');
    const errDiv = document.createElement('div');
    errDiv.className = 'import-fail-msg';
    errDiv.innerHTML = `<i class="fa-solid fa-circle-exclamation"></i> ${e.message}`;
    el.appendChild(errDiv);
    return;
  }

  relayUploadId = initData.upload_id;
  const { upload_id } = initData;
  const reader = resp.body.getReader();
  let buf = new Uint8Array(0);
  let partNumber = 0;
  let totalBytes = 0;
  let lastTime = Date.now(), lastBytes = 0;

  const uploadPart = async (data, pn) => {
    for (let attempt = 0; attempt < MAX_RETRIES; attempt++) {
      if (aborted) throw new Error('Aborted');
      try {
        const r = await fetch(`/api/upload/${upload_id}/chunk/${pn}`, {
          method: 'POST',
          headers: { 'X-API-Key': apiKey, 'Content-Type': 'application/octet-stream' },
          body: data,
        });
        if (!r.ok) throw new Error(`Chunk ${pn} failed: HTTP ${r.status}`);
        return;
      } catch (e) {
        if (aborted) throw e;
        if (attempt === MAX_RETRIES - 1) throw e;
        await sleep(1000 * (attempt + 1));
      }
    }
  };

  try {
    while (true) {
      if (aborted) break;
      const { done, value } = await reader.read();
      if (value) {
        const merged = new Uint8Array(buf.length + value.length);
        merged.set(buf);
        merged.set(value, buf.length);
        buf = merged;
        totalBytes += value.length;

        const now = Date.now();
        if (contentLength) {
          const pctVal = Math.min(99, Math.round((totalBytes / contentLength) * 100));
          bar.style.width = pctVal + '%';
          pct.textContent = pctVal + '%';
        } else {
          pct.textContent = formatBytes(totalBytes);
        }
        const dt = (now - lastTime) / 1000;
        if (dt >= 1 && totalBytes > lastBytes) {
          spd.textContent = formatBytes((totalBytes - lastBytes) / dt) + '/s';
          lastBytes = totalBytes; lastTime = now;
        }

        while (buf.length >= CHUNK_SIZE) {
          partNumber++;
          const chunk = buf.slice(0, CHUNK_SIZE);
          buf = buf.slice(CHUNK_SIZE);
          await uploadPart(chunk, partNumber);
        }
      }
      if (done) break;
    }

    if (!aborted && buf.length > 0) {
      partNumber++;
      await uploadPart(buf, partNumber);
    }

    if (aborted) return;

    if (partNumber === 0) throw new Error('Downloaded file was empty');

    badge.className = 'status-badge completing';
    badge.textContent = 'Finalizing…';
    bar.style.width = '98%';
    abortBtn.remove();

    const result = await apiFetch('POST', `/api/upload/${upload_id}/complete`, { actual_size: totalBytes });

    bar.style.width = '100%';
    bar.classList.add('success');
    badge.className = 'status-badge done';
    badge.textContent = 'Done';
    pct.textContent = '100%';
    spd.textContent = '';

    const doneRow = document.createElement('div');
    doneRow.className = 'done-row';
    doneRow.innerHTML = `
      <button class="done-link-btn" data-url="${window.location.origin}${result.share_url}"><i class="fa-solid fa-link"></i> Copy Share Link</button>
      <button class="done-direct-btn" data-url="${result.direct_url}"><i class="fa-solid fa-download"></i> Copy Direct Link</button>`;
    doneRow.querySelector('.done-link-btn').addEventListener('click', e => {
      navigator.clipboard.writeText(e.currentTarget.dataset.url);
      e.currentTarget.innerHTML = '<i class="fa-solid fa-check"></i> Copied!';
      setTimeout(() => { e.currentTarget.innerHTML = '<i class="fa-solid fa-link"></i> Copy Share Link'; }, 2000);
    });
    doneRow.querySelector('.done-direct-btn').addEventListener('click', e => {
      navigator.clipboard.writeText(e.currentTarget.dataset.url);
      e.currentTarget.innerHTML = '<i class="fa-solid fa-check"></i> Copied!';
      setTimeout(() => { e.currentTarget.innerHTML = '<i class="fa-solid fa-download"></i> Copy Direct Link'; }, 2000);
    });
    el.appendChild(doneRow);
    loadDashboard();

  } catch (e) {
    if (aborted) return;
    abortBtn.remove();
    badge.className = 'status-badge error';
    badge.textContent = 'Failed';
    bar.classList.add('error');
    const errDiv = document.createElement('div');
    errDiv.className = 'import-fail-msg';
    errDiv.innerHTML = `<i class="fa-solid fa-circle-exclamation"></i> ${e.message}`;
    el.appendChild(errDiv);
  }
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

// ── Storage Providers ─────────────────────────────────────────────────────────

function providerMeta(endpointUrl) {
  const u = (endpointUrl || '').toLowerCase();
  if (u.includes('backblazeb2'))          return { label: 'Backblaze B2',  icon: 'fa-database',  color: '#f59e0b' };
  if (u.includes('r2.cloudflarestorage')) return { label: 'Cloudflare R2', icon: 'fa-cloud',     color: '#f97316' };
  if (u.includes('wasabisys'))            return { label: 'Wasabi',        icon: 'fa-droplet',   color: '#16a34a' };
  if (u.includes('amazonaws'))            return { label: 'Amazon S3',     icon: 'fa-server',    color: '#f59e0b' };
  if (u.includes('bunnycdn') || u.includes('b-cdn.net')) return { label: 'Bunny CDN', icon: 'fa-bolt', color: '#8b5cf6' };
  return { label: 'Custom S3', icon: 'fa-server', color: '#64748b' };
}

async function loadStoragePage() {
  loadBandwidthStats();
  const list = document.getElementById('storage-list');
  try {
    const providers = await apiFetch('GET', '/api/admin/storage');
    if (!providers.length) {
      list.innerHTML = `<div class="sp-empty"><i class="fa-solid fa-database"></i><p>No storage providers added yet.</p><p class="sp-empty-sub">Add one above — all new uploads will use the default provider. Existing files using environment variables continue to work.</p></div>`;
      return;
    }
    list.innerHTML = '';
    // Populate fallback provider dropdown (exclude the provider being edited)
  const sel = document.getElementById('sp-fallback-provider');
  const currentEdit = document.getElementById('storage-edit-id').value;
  sel.innerHTML = '<option value="">— None —</option>';
  providers.forEach(p => {
    if (p.id !== currentEdit) {
      const opt = document.createElement('option');
      opt.value = p.id;
      opt.textContent = p.name;
      sel.appendChild(opt);
    }
  });

  providers.forEach(p => list.appendChild(buildProviderCard(p)));
  } catch (e) {
    list.innerHTML = `<p style="color:var(--danger);padding:1rem">Failed: ${e.message}</p>`;
  }
}

function buildProviderCard(p) {
  const meta = providerMeta(p.endpoint_url);
  const card = document.createElement('div');
  card.className = 'sp-card';
  card.innerHTML = `
    <div class="sp-card-header">
      <div class="sp-icon" style="color:${meta.color};background:${meta.color}18">
        <i class="fa-solid ${meta.icon}"></i>
      </div>
      <div class="sp-info">
        <div class="sp-name">${p.name}
          ${p.is_default ? '<span class="sp-badge sp-default-badge"><i class="fa-solid fa-star"></i> Default</span>' : ''}
          ${!p.active ? '<span class="sp-badge sp-inactive-badge">Inactive</span>' : ''}
        </div>
        <div class="sp-detail"><i class="fa-solid fa-link"></i> ${p.endpoint_url}</div>
        <div class="sp-detail"><i class="fa-solid fa-box-archive"></i> ${p.bucket_name} · <span class="sp-type">${meta.label}</span> · <strong>${p.file_count || 0}</strong> files${p.fallback_provider_id ? ' · <i class="fa-solid fa-arrow-right" style="color:var(--muted)"></i> <span style="color:var(--muted);font-size:.78rem">fallback set</span>' : ''}</div>
        ${p.bandwidth_cap_gb ? `
        <div class="sp-bw-wrap">
          <div class="sp-bw-bar-bg"><div class="sp-bw-bar${p.cap_exceeded ? ' sp-bw-exceeded' : ''}" style="width:${Math.min(100, p.bandwidth_pct || 0)}%"></div></div>
          <span class="sp-bw-label ${p.cap_exceeded ? 'sp-bw-exceeded-txt' : ''}">
            ${p.cap_exceeded
              ? `<i class="fa-solid fa-triangle-exclamation"></i> Cap exceeded — routing via fallback CDN`
              : `${p.bandwidth_used_gb} GB / ${p.bandwidth_cap_gb} GB used this month`}
          </span>
        </div>` : ''}
      </div>
    </div>
    <div class="sp-actions">
      <button class="sp-btn sp-btn-test"><i class="fa-solid fa-plug-circle-check"></i> Test</button>
      ${!p.is_default ? `<button class="sp-btn sp-btn-default"><i class="fa-solid fa-star"></i> Set Default</button>` : ''}
      <button class="sp-btn sp-btn-edit"><i class="fa-solid fa-pen"></i> Edit</button>
      <button class="sp-btn sp-btn-delete"><i class="fa-solid fa-trash"></i> Delete</button>
    </div>`;

  card.querySelector('.sp-btn-test').addEventListener('click', async btn => {
    const b = card.querySelector('.sp-btn-test');
    b.disabled = true; b.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Testing…';
    try {
      const res = await apiFetch('POST', `/api/admin/storage/${p.id}/test`);
      b.innerHTML = res.ok
        ? '<i class="fa-solid fa-circle-check"></i> Connected!'
        : `<i class="fa-solid fa-circle-xmark"></i> Failed`;
      b.style.color = res.ok ? 'var(--success)' : 'var(--danger)';
      if (!res.ok) alert('Connection failed: ' + res.error);
    } catch (e) { b.innerHTML = '<i class="fa-solid fa-circle-xmark"></i> Error'; }
    setTimeout(() => { b.disabled = false; b.style.color = ''; b.innerHTML = '<i class="fa-solid fa-plug-circle-check"></i> Test'; }, 3000);
  });

  card.querySelector('.sp-btn-edit').addEventListener('click', () => fillEditForm(p));

  if (!p.is_default) {
    card.querySelector('.sp-btn-default').addEventListener('click', async () => {
      await apiFetch('POST', `/api/admin/storage/${p.id}/set-default`);
      loadStoragePage();
    });
  }

  card.querySelector('.sp-btn-delete').addEventListener('click', async () => {
    if (!confirm(`Delete "${p.name}"? This cannot be undone.`)) return;
    try {
      await apiFetch('DELETE', `/api/admin/storage/${p.id}`);
      loadStoragePage();
    } catch (e) { alert(e.message); }
  });

  return card;
}

function fillEditForm(p) {
  document.getElementById('storage-edit-id').value   = p.id;
  document.getElementById('sp-name').value           = p.name;
  document.getElementById('sp-endpoint').value       = p.endpoint_url;
  document.getElementById('sp-key-id').value         = p.key_id;
  document.getElementById('sp-app-key').value        = p.application_key;
  document.getElementById('sp-bucket').value         = p.bucket_name;
  document.getElementById('sp-public-url').value     = p.public_base_url || '';
  document.getElementById('sp-bw-cap').value              = p.bandwidth_cap_gb || '';
  document.getElementById('sp-fallback-provider').value   = p.fallback_provider_id || '';
  document.getElementById('sp-default').checked      = !!p.is_default;
  document.getElementById('storage-form-title').textContent = 'Edit Storage Provider';
  document.getElementById('storage-submit-btn').innerHTML   = '<i class="fa-solid fa-floppy-disk"></i> Save Changes';
  document.getElementById('storage-cancel-btn').style.display = '';
  document.getElementById('sp-name').scrollIntoView({ behavior: 'smooth', block: 'center' });
}

function initStorageForm() {
  const form    = document.getElementById('storage-form');
  const msgEl   = document.getElementById('storage-form-msg');
  const cancelBtn = document.getElementById('storage-cancel-btn');

  cancelBtn.addEventListener('click', () => {
    form.reset();
    document.getElementById('storage-edit-id').value = '';
    document.getElementById('storage-form-title').textContent = 'Add Storage Provider';
    document.getElementById('storage-submit-btn').innerHTML = '<i class="fa-solid fa-plus"></i> Add Provider';
    cancelBtn.style.display = 'none';
    msgEl.textContent = '';
  });

  form.addEventListener('submit', async e => {
    e.preventDefault();
    msgEl.textContent = '';
    const editId = document.getElementById('storage-edit-id').value;
    const body = {
      name:            document.getElementById('sp-name').value.trim(),
      endpoint_url:    document.getElementById('sp-endpoint').value.trim(),
      key_id:          document.getElementById('sp-key-id').value.trim(),
      application_key: document.getElementById('sp-app-key').value.trim(),
      bucket_name:     document.getElementById('sp-bucket').value.trim(),
      public_base_url:   document.getElementById('sp-public-url').value.trim() || null,
      bandwidth_cap_gb:     parseFloat(document.getElementById('sp-bw-cap').value) || null,
      fallback_provider_id: document.getElementById('sp-fallback-provider').value || null,
      is_default:        document.getElementById('sp-default').checked ? 1 : 0,
      active: 1,
    };
    try {
      if (editId) {
        await apiFetch('PATCH', `/api/admin/storage/${editId}`, body);
        msgEl.style.color = 'var(--success)'; msgEl.textContent = 'Saved!';
      } else {
        await apiFetch('POST', '/api/admin/storage', body);
        msgEl.style.color = 'var(--success)'; msgEl.textContent = 'Provider added!';
      }
      form.reset();
      document.getElementById('storage-edit-id').value = '';
      document.getElementById('storage-form-title').textContent = 'Add Storage Provider';
      document.getElementById('storage-submit-btn').innerHTML = '<i class="fa-solid fa-plus"></i> Add Provider';
      cancelBtn.style.display = 'none';
      setTimeout(() => { msgEl.textContent = ''; }, 3000);
      loadStoragePage();
    } catch (err) {
      msgEl.style.color = 'var(--danger)'; msgEl.textContent = err.message;
    }
  });
}

// ── Download Redirect URL ─────────────────────────────────────────────────────

async function loadRedirectUrlSetting() {
  try {
    const s = await apiFetch('GET', '/api/settings');
    document.getElementById('redirect-url-input').value    = s.redirect_url    || '';
    document.getElementById('popup-url-input').value       = s.popup_url       || '';
    document.getElementById('monetag-head-input').value    = s.monetag_head    || '';
    document.getElementById('monetag-banner-input').value  = s.monetag_banner  || '';
    document.getElementById('monetag-side-input').value    = s.monetag_side    || '';
  } catch {}
}

async function saveSetting(field, value, msgId, inputId) {
  const msg = document.getElementById(msgId);
  msg.textContent = '';
  // Always send all fields together so saving one never clears the others
  const body = {
    redirect_url:   document.getElementById('redirect-url-input').value.trim()   || null,
    popup_url:      document.getElementById('popup-url-input').value.trim()       || null,
    monetag_head:   document.getElementById('monetag-head-input').value.trim()   || null,
    monetag_banner: document.getElementById('monetag-banner-input').value.trim() || null,
    monetag_side:   document.getElementById('monetag-side-input').value.trim()   || null,
    [field]:        value || null,
  };
  try {
    await apiFetch('POST', '/api/admin/settings', body);
    msg.style.color = 'var(--success)';
    msg.textContent = value ? 'Saved!' : 'Cleared.';
    document.getElementById(inputId).value = value || '';
    setTimeout(() => { msg.textContent = ''; }, 3000);
  } catch (e) {
    msg.style.color = 'var(--danger)';
    msg.textContent = e.message;
  }
}

function initRedirectUrlForm() {
  // Redirect URL
  document.getElementById('redirect-url-save').addEventListener('click', () => {
    saveSetting('redirect_url', document.getElementById('redirect-url-input').value.trim(), 'redirect-url-msg', 'redirect-url-input');
  });
  document.getElementById('redirect-url-clear').addEventListener('click', () => {
    saveSetting('redirect_url', '', 'redirect-url-msg', 'redirect-url-input');
  });
  document.getElementById('redirect-url-input').addEventListener('keydown', e => {
    if (e.key === 'Enter') document.getElementById('redirect-url-save').click();
  });

  // Popup URL
  document.getElementById('popup-url-save').addEventListener('click', () => {
    saveSetting('popup_url', document.getElementById('popup-url-input').value.trim(), 'popup-url-msg', 'popup-url-input');
  });
  document.getElementById('popup-url-clear').addEventListener('click', () => {
    saveSetting('popup_url', '', 'popup-url-msg', 'popup-url-input');
  });
  document.getElementById('popup-url-input').addEventListener('keydown', e => {
    if (e.key === 'Enter') document.getElementById('popup-url-save').click();
  });

  // Monetag head script
  document.getElementById('monetag-head-save').addEventListener('click', () => {
    saveSetting('monetag_head', document.getElementById('monetag-head-input').value.trim(), 'monetag-head-msg', 'monetag-head-input');
  });
  document.getElementById('monetag-head-clear').addEventListener('click', () => {
    saveSetting('monetag_head', '', 'monetag-head-msg', 'monetag-head-input');
  });

  // Monetag banner script
  document.getElementById('monetag-banner-save').addEventListener('click', () => {
    saveSetting('monetag_banner', document.getElementById('monetag-banner-input').value.trim(), 'monetag-banner-msg', 'monetag-banner-input');
  });
  document.getElementById('monetag-banner-clear').addEventListener('click', () => {
    saveSetting('monetag_banner', '', 'monetag-banner-msg', 'monetag-banner-input');
  });

  // Monetag side script
  document.getElementById('monetag-side-save').addEventListener('click', () => {
    saveSetting('monetag_side', document.getElementById('monetag-side-input').value.trim(), 'monetag-side-msg', 'monetag-side-input');
  });
  document.getElementById('monetag-side-clear').addEventListener('click', () => {
    saveSetting('monetag_side', '', 'monetag-side-msg', 'monetag-side-input');
  });
}

// ── Ad Manager ────────────────────────────────────────────────────────────────

async function loadAdsPage() {
  loadRedirectUrlSetting();
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

// ── Team Key Management ───────────────────────────────────────────────────────

async function loadTeamPage() {
  const el = document.getElementById('key-list');
  el.innerHTML = '<p style="padding:1.5rem;color:var(--muted);font-size:.875rem;text-align:center">Loading…</p>';
  let keys;
  try { keys = await apiFetch('GET', '/api/admin/keys'); }
  catch (e) { el.innerHTML = `<p style="padding:1.5rem;color:var(--danger);font-size:.875rem;text-align:center">${e.message}</p>`; return; }

  if (!keys.length) {
    el.innerHTML = '<p style="padding:1.5rem;color:var(--muted);font-size:.875rem;text-align:center">No team keys yet. Generate one above.</p>';
    return;
  }
  el.innerHTML = `
    <table class="files-table">
      <thead><tr>
        <th>Name</th><th>Access Level</th><th>Key</th><th>Status</th><th>Created</th><th>Actions</th>
      </tr></thead>
      <tbody>
        ${keys.map(k => `
          <tr>
            <td class="tf-name">${k.name}</td>
            <td>${k.role === 'admin'
              ? '<span class="team-role team-role-admin">Admin</span>'
              : '<span class="team-role team-role-member">Member</span>'}</td>
            <td><code class="team-key-masked">${k.key}</code></td>
            <td>${k.active
              ? '<span class="ad-on"><i class="fa-solid fa-circle-check"></i> Active</span>'
              : '<span class="ad-off"><i class="fa-solid fa-circle-xmark"></i> Revoked</span>'}</td>
            <td style="color:var(--muted);font-size:.8rem">${formatDate(k.created_at)}</td>
            <td><div class="tf-actions">
              <button class="btn-sm" onclick="regenerateTeamKey('${k.id}')">Regenerate</button>
              <button class="btn-sm ${k.active ? 'danger' : ''}" onclick="toggleTeamKey('${k.id}',${!k.active})">
                ${k.active ? 'Revoke' : 'Enable'}
              </button>
              <button class="btn-sm danger" onclick="deleteTeamKey('${k.id}')">Delete</button>
            </div></td>
          </tr>`).join('')}
      </tbody>
    </table>`;
}

async function toggleTeamKey(id, active) {
  try { await apiFetch('PATCH', `/api/admin/keys/${id}`, { active }); loadTeamPage(); }
  catch (e) { alert(e.message); }
}

async function regenerateTeamKey(id) {
  if (!confirm('Regenerate this key? The old key stops working immediately and the member must use the new one.')) return;
  try {
    const result = await apiFetch('POST', `/api/admin/keys/${id}/regenerate`);
    document.getElementById('key-reveal-value').textContent = result.key;
    document.getElementById('key-reveal-box').hidden = false;
    document.getElementById('key-reveal-box').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    loadTeamPage();
  } catch (e) { alert(e.message); }
}

async function deleteTeamKey(id) {
  if (!confirm('Delete this key? The member will lose access immediately.')) return;
  try { await apiFetch('DELETE', `/api/admin/keys/${id}`); loadTeamPage(); }
  catch (e) { alert(e.message); }
}

function initKeyForm() {
  const form    = document.getElementById('key-form');
  const msg     = document.getElementById('key-form-msg');
  const reveal  = document.getElementById('key-reveal-box');
  const revVal  = document.getElementById('key-reveal-value');
  const revCopy = document.getElementById('key-reveal-copy');

  form.addEventListener('submit', async e => {
    e.preventDefault();
    const name = document.getElementById('key-name').value.trim();
    const role = document.getElementById('key-role').value;
    if (!name) return;
    msg.textContent = '';
    try {
      const result = await apiFetch('POST', '/api/admin/keys', { name, role });
      revVal.textContent = result.key;
      reveal.hidden = false;
      reveal.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      document.getElementById('key-name').value = '';
      msg.style.color = 'var(--success)';
      msg.textContent = 'Key created!';
      loadTeamPage();
    } catch (err) {
      msg.style.color = 'var(--danger)';
      msg.textContent = err.message;
    }
    setTimeout(() => { msg.textContent = ''; }, 4000);
  });

  revCopy.addEventListener('click', () => {
    navigator.clipboard.writeText(revVal.textContent);
    revCopy.innerHTML = '<i class="fa-solid fa-check"></i> Copied!';
    setTimeout(() => { revCopy.innerHTML = '<i class="fa-solid fa-copy"></i> Copy Key'; }, 2000);
  });
}

async function changeMasterKey() {
  if (!confirm('This will immediately invalidate your current admin key. You will be logged out and must use the new key. Continue?')) return;
  const btn = document.getElementById('change-admin-key-btn');
  btn.disabled = true;
  try {
    const result = await apiFetch('POST', '/api/admin/change-master-key');
    const revealBox = document.getElementById('admin-key-reveal-box');
    const revealVal = document.getElementById('admin-key-reveal-value');
    const revealCopy = document.getElementById('admin-key-reveal-copy');
    const msgEl = document.getElementById('admin-key-logout-msg');
    revealVal.textContent = result.new_key;
    revealBox.hidden = false;
    revealBox.scrollIntoView({ behavior: 'smooth', block: 'nearest' });

    revealCopy.onclick = () => {
      navigator.clipboard.writeText(result.new_key);
      revealCopy.innerHTML = '<i class="fa-solid fa-check"></i> Copied!';
      setTimeout(() => { revealCopy.innerHTML = '<i class="fa-solid fa-copy"></i> Copy Key'; }, 2000);
    };

    let secs = 15;
    msgEl.textContent = `Logging out in ${secs} seconds…`;
    const countdown = setInterval(() => {
      secs--;
      if (secs <= 0) {
        clearInterval(countdown);
        localStorage.removeItem(LS_KEY);
        apiKey = '';
        location.reload();
      } else {
        msgEl.textContent = `Logging out in ${secs} second${secs !== 1 ? 's' : ''}…`;
      }
    }, 1000);
  } catch (e) {
    alert('Failed: ' + e.message);
    btn.disabled = false;
  }
}

// ── Bandwidth Analytics ────────────────────────────────────────────────────────

let bwChart      = null;
let bwActiveDays = 30;

async function loadBandwidthStats() {
  const statsEl     = document.getElementById('bw-stats');
  const providersEl = document.getElementById('bw-providers');
  statsEl.innerHTML = '<p style="color:var(--muted);font-size:.875rem;grid-column:1/-1;text-align:center"><i class="fa-solid fa-spinner fa-spin"></i> Loading…</p>';
  providersEl.innerHTML = '';

  let data;
  try {
    data = await apiFetch('GET', `/api/admin/bandwidth?days=${bwActiveDays}`);
  } catch (e) {
    statsEl.innerHTML = `<p style="color:var(--danger);grid-column:1/-1">${escHtml(e.message)}</p>`;
    return;
  }

  const avgPerDl = data.period_downloads > 0
    ? formatBytes(Math.round(data.period_bytes / data.period_downloads))
    : '—';

  statsEl.innerHTML = `
    <div class="stat-card">
      <div class="stat-icon" style="background:linear-gradient(135deg,#6366f1,#4f46e5);color:#fff;box-shadow:0 4px 12px rgba(99,102,241,.35)"><i class="fa-solid fa-arrow-right-arrow-left"></i></div>
      <div><div class="stat-label">Bandwidth Served</div><div class="stat-value">${formatBytes(data.period_bytes)}</div></div>
    </div>
    <div class="stat-card">
      <div class="stat-icon" style="background:linear-gradient(135deg,#10b981,#059669);color:#fff;box-shadow:0 4px 12px rgba(16,185,129,.35)"><i class="fa-solid fa-download"></i></div>
      <div><div class="stat-label">Downloads</div><div class="stat-value">${data.period_downloads.toLocaleString()}</div></div>
    </div>
    <div class="stat-card">
      <div class="stat-icon" style="background:linear-gradient(135deg,#f59e0b,#d97706);color:#fff;box-shadow:0 4px 12px rgba(245,158,11,.35)"><i class="fa-solid fa-scale-balanced"></i></div>
      <div><div class="stat-label">Avg per Download</div><div class="stat-value">${avgPerDl}</div></div>
    </div>
    <div class="stat-card">
      <div class="stat-icon" style="background:linear-gradient(135deg,#8b5cf6,#7c3aed);color:#fff;box-shadow:0 4px 12px rgba(139,92,246,.35)"><i class="fa-solid fa-database"></i></div>
      <div><div class="stat-label">All-Time Total</div><div class="stat-value">${formatBytes(data.alltime_bytes)}</div></div>
    </div>`;

  // Daily bar chart
  const ctx = document.getElementById('bw-chart').getContext('2d');
  if (bwChart) bwChart.destroy();
  bwChart = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: data.daily.map(d => d.date),
      datasets: [{
        label: 'Bandwidth',
        data: data.daily.map(d => d.bytes),
        backgroundColor: 'rgba(99,102,241,0.75)',
        borderRadius: 4,
        borderSkipped: false,
      }],
    },
    options: {
      responsive: true,
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: c => formatBytes(c.parsed.y) + ' served',
            afterLabel: (c) => {
              const d = data.daily[c.dataIndex];
              return d.downloads + ' download' + (d.downloads !== 1 ? 's' : '');
            },
          },
        },
      },
      scales: {
        y: {
          beginAtZero: true,
          ticks: { callback: v => formatBytes(v), font: { size: 11 } },
          grid: { color: 'rgba(0,0,0,.05)' },
        },
        x: { ticks: { font: { size: 11 } } },
      },
    },
  });

  // Provider breakdown
  if (!data.providers.length) {
    providersEl.innerHTML = '<p style="color:var(--muted);font-size:.85rem;text-align:center;padding:.75rem 0">No data for this period.</p>';
    return;
  }

  const totalBytes = data.period_bytes || 1;
  providersEl.innerHTML = `
    <div style="font-size:.75rem;font-weight:700;color:var(--muted);letter-spacing:.04em;margin-bottom:.75rem;text-transform:uppercase">Provider Breakdown</div>
    ${data.providers.map(p => {
      const pct = Math.max(1, Math.round((p.bytes / totalBytes) * 100));
      return `
        <div class="bw-provider-row">
          <div class="bw-provider-name">${escHtml(p.name)}</div>
          <div class="bw-bar-track"><div class="bw-bar-fill" style="width:${pct}%"></div></div>
          <div class="bw-pct">${pct}%</div>
          <div class="bw-bytes">${formatBytes(p.bytes)}</div>
          <div class="bw-dl-cnt"><i class="fa-solid fa-download" style="font-size:.65rem"></i> ${p.downloads.toLocaleString()}</div>
        </div>`;
    }).join('')}`;
}

// ── Downloads Analytics ───────────────────────────────────────────────────────

let dlTimeChart   = null;
let dlDeviceChart = null;
let dlActiveDays  = 7;

function countryFlag(code) {
  if (!code || code.length !== 2 || code === '??') return '🌍';
  const a = code.toUpperCase().charCodeAt(0) - 65 + 0x1F1E6;
  const b = code.toUpperCase().charCodeAt(1) - 65 + 0x1F1E6;
  return String.fromCodePoint(a, b);
}

function dlEmpty(msg) {
  return `<p class="dl-empty">${msg}</p>`;
}

async function loadDownloadsPage() {
  document.querySelectorAll('.dl-period-btn').forEach(b => {
    b.classList.toggle('active', parseInt(b.dataset.days) === dlActiveDays);
  });

  ['dl-stat-total','dl-stat-ips','dl-stat-countries','dl-stat-avg'].forEach(id => {
    document.getElementById(id).textContent = '…';
  });
  ['dl-countries','dl-os','dl-top-files'].forEach(id => {
    document.getElementById(id).innerHTML = dlEmpty('Loading…');
  });

  let data;
  try {
    data = await apiFetch('GET', `/api/analytics/downloads?days=${dlActiveDays}`);
  } catch (e) {
    ['dl-countries','dl-os','dl-top-files'].forEach(id => {
      document.getElementById(id).innerHTML = dlEmpty('Failed to load data.');
    });
    return;
  }

  document.getElementById('dl-stat-total').textContent     = data.total.toLocaleString();
  document.getElementById('dl-stat-ips').textContent       = data.unique_ips.toLocaleString();
  document.getElementById('dl-stat-countries').textContent = data.unique_countries.toLocaleString();
  const avg = dlActiveDays > 0 ? (data.total / dlActiveDays).toFixed(1) : '0';
  document.getElementById('dl-stat-avg').textContent = avg;

  // Downloads over time — line chart
  const timeCtx = document.getElementById('dl-time-chart').getContext('2d');
  if (dlTimeChart) dlTimeChart.destroy();
  dlTimeChart = new Chart(timeCtx, {
    type: 'line',
    data: {
      labels: data.per_day.map(d => d.date),
      datasets: [{
        label: 'Downloads',
        data: data.per_day.map(d => d.count),
        borderColor: '#6366f1',
        backgroundColor: 'rgba(99,102,241,.12)',
        fill: true,
        tension: 0.4,
        pointRadius: dlActiveDays <= 7 ? 4 : 2,
        pointBackgroundColor: '#6366f1',
      }],
    },
    options: {
      responsive: true,
      plugins: { legend: { display: false }, tooltip: { mode: 'index', intersect: false } },
      scales: {
        y: { beginAtZero: true, ticks: { precision: 0 }, grid: { color: 'rgba(0,0,0,.04)' } },
        x: { grid: { display: false }, ticks: { maxTicksLimit: dlActiveDays <= 7 ? 7 : 10 } },
      },
    },
  });

  // Device types — doughnut
  const deviceCtx = document.getElementById('dl-device-chart').getContext('2d');
  if (dlDeviceChart) dlDeviceChart.destroy();
  if (data.devices.length) {
    const DEVICE_COLORS = { Desktop: '#6366f1', Mobile: '#059669', Tablet: '#d97706', Unknown: '#94a3b8' };
    dlDeviceChart = new Chart(deviceCtx, {
      type: 'doughnut',
      data: {
        labels: data.devices.map(d => d.type),
        datasets: [{
          data: data.devices.map(d => d.count),
          backgroundColor: data.devices.map(d => DEVICE_COLORS[d.type] || '#94a3b8'),
          borderWidth: 3,
          borderColor: '#fff',
        }],
      },
      options: {
        responsive: true,
        cutout: '62%',
        plugins: {
          legend: { position: 'bottom', labels: { font: { size: 12, family: 'Inter' }, padding: 12 } },
        },
      },
    });
  }

  // Top countries
  const countriesEl = document.getElementById('dl-countries');
  if (!data.top_countries.length) {
    countriesEl.innerHTML = dlEmpty('No download data yet for this period.');
  } else {
    const max = data.top_countries[0].count;
    countriesEl.innerHTML = data.top_countries.map(c => `
      <div class="dl-row">
        <span class="dl-flag">${countryFlag(c.country_code)}</span>
        <span class="dl-row-label">${c.country || 'Unknown'}</span>
        <div class="dl-bar-wrap"><div class="dl-bar" style="width:${Math.round(c.count / max * 100)}%"></div></div>
        <span class="dl-row-count">${c.count.toLocaleString()}</span>
      </div>`).join('');
  }

  // OS breakdown
  const osEl = document.getElementById('dl-os');
  if (!data.os_breakdown.length) {
    osEl.innerHTML = dlEmpty('No download data yet for this period.');
  } else {
    const maxOs = data.os_breakdown[0].count;
    const OS_ICONS = {
      Windows: 'fa-windows', macOS: 'fa-apple', iOS: 'fa-apple',
      Android: 'fa-android', Linux: 'fa-linux', ChromeOS: 'fa-chrome', Unknown: 'fa-circle-question',
    };
    osEl.innerHTML = data.os_breakdown.map(o => `
      <div class="dl-row">
        <span class="dl-os-icon"><i class="fa-brands ${OS_ICONS[o.os] || 'fa-circle-question'}"></i></span>
        <span class="dl-row-label">${o.os}</span>
        <div class="dl-bar-wrap"><div class="dl-bar dl-bar-os" style="width:${Math.round(o.count / maxOs * 100)}%"></div></div>
        <span class="dl-row-count">${o.count.toLocaleString()}</span>
      </div>`).join('');
  }

  // Top files
  const filesEl = document.getElementById('dl-top-files');
  if (!data.top_files.length) {
    filesEl.innerHTML = dlEmpty('No download data yet for this period.');
  } else {
    filesEl.innerHTML = `
      <table class="files-table">
        <thead><tr>
          <th style="width:2rem">#</th>
          <th>Filename</th>
          <th style="width:9rem">Downloads</th>
        </tr></thead>
        <tbody>
          ${data.top_files.map((f, i) => `
            <tr>
              <td style="color:var(--muted);font-size:.8rem;font-weight:600">${i + 1}</td>
              <td>${fileIcon(f.filename)} <span class="tf-name">${f.filename}</span></td>
              <td><span class="dl-count-badge">${f.count.toLocaleString()}</span></td>
            </tr>`).join('')}
        </tbody>
      </table>`;
  }

}

// ── Access Requests ───────────────────────────────────────────────────────────

let reqActiveFilter = '';

async function loadRequestsPage() {
  const listEl = document.getElementById('req-list');
  listEl.innerHTML = '<p style="padding:1.5rem;color:var(--muted);font-size:.875rem;text-align:center">Loading…</p>';

  let data;
  try {
    const qs = reqActiveFilter ? `?status=${reqActiveFilter}` : '';
    data = await apiFetch('GET', `/api/admin/access-requests${qs}`);
  } catch (e) {
    listEl.innerHTML = `<p style="padding:1.5rem;color:var(--danger);font-size:.875rem;text-align:center">${e.message}</p>`;
    return;
  }

  // Update stats
  document.getElementById('req-stat-pending').textContent  = data.requests.filter(r => r.status === 'pending').length;
  document.getElementById('req-stat-approved').textContent = data.requests.filter(r => r.status === 'approved').length;
  document.getElementById('req-stat-rejected').textContent = data.requests.filter(r => r.status === 'rejected').length;
  updateReqBadge(data.pending_count);

  if (!data.requests.length) {
    listEl.innerHTML = '<p style="padding:1.5rem;color:var(--muted);font-size:.875rem;text-align:center">No requests found.</p>';
    return;
  }

  const statusBadge = s => {
    if (s === 'pending')  return '<span class="team-role" style="background:#fef3c7;color:#92400e">Pending</span>';
    if (s === 'approved') return '<span class="team-role team-role-member" style="background:#dcfce7;color:#166534">Approved</span>';
    return '<span class="team-role" style="background:#fee2e2;color:#991b1b">Rejected</span>';
  };

  listEl.innerHTML = `
    <table class="files-table">
      <thead><tr>
        <th>Name</th><th>Email</th><th>Reason</th><th>Status</th><th>Date</th><th>Actions</th>
      </tr></thead>
      <tbody>
        ${data.requests.map(r => `
          <tr>
            <td class="tf-name">${r.name}</td>
            <td style="font-size:.82rem">${r.email}</td>
            <td style="font-size:.8rem;color:var(--muted);max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
                title="${r.reason || ''}">${r.reason || '—'}</td>
            <td>${statusBadge(r.status)}</td>
            <td style="color:var(--muted);font-size:.8rem">${formatDate(r.created_at)}</td>
            <td><div class="tf-actions">
              ${r.status === 'pending' ? `
                <button class="btn-primary-sm" onclick="approveRequest('${r.id}')">
                  <i class="fa-solid fa-key"></i> Approve &amp; Generate Key
                </button>
                <button class="btn-sm danger" onclick="rejectRequest('${r.id}')">Reject</button>
              ` : ''}
              <button class="btn-sm danger" onclick="deleteRequest('${r.id}')">Delete</button>
            </div></td>
          </tr>`).join('')}
      </tbody>
    </table>`;
}

function updateReqBadge(count) {
  const badge = document.getElementById('req-nav-badge');
  if (!badge) return;
  if (count > 0) {
    badge.textContent = count > 99 ? '99+' : count;
    badge.hidden = false;
  } else {
    badge.hidden = true;
  }
}

async function approveRequest(id) {
  if (!confirm('Approve this request and generate a member API key?')) return;
  try {
    const result = await apiFetch('POST', `/api/admin/access-requests/${id}/approve`);
    document.getElementById('req-key-value').textContent   = result.key;
    document.getElementById('req-key-for-name').textContent  = result.name;
    document.getElementById('req-key-for-email').textContent = result.email;
    const revealEl = document.getElementById('req-key-reveal');
    revealEl.hidden = false;
    revealEl.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    loadRequestsPage();
  } catch (e) { alert(e.message); }
}

async function rejectRequest(id) {
  if (!confirm('Reject this request?')) return;
  try { await apiFetch('POST', `/api/admin/access-requests/${id}/reject`); loadRequestsPage(); }
  catch (e) { alert(e.message); }
}

async function deleteRequest(id) {
  if (!confirm('Delete this request permanently?')) return;
  try { await apiFetch('DELETE', `/api/admin/access-requests/${id}`); loadRequestsPage(); }
  catch (e) { alert(e.message); }
}

function initRequestsPage() {
  document.getElementById('req-key-copy').addEventListener('click', function () {
    const val = document.getElementById('req-key-value').textContent;
    navigator.clipboard.writeText(val).catch(() => {});
    this.innerHTML = '<i class="fa-solid fa-check"></i> Copied!';
    setTimeout(() => { this.innerHTML = '<i class="fa-solid fa-copy"></i> Copy Key'; }, 2500);
  });

  document.querySelectorAll('.req-filter-btn').forEach(btn => {
    btn.addEventListener('click', function () {
      document.querySelectorAll('.req-filter-btn').forEach(b => b.classList.remove('req-filter-active'));
      this.classList.add('req-filter-active');
      reqActiveFilter = this.dataset.status;
      loadRequestsPage();
    });
  });
}

// ── Support ───────────────────────────────────────────────────────────────────

let _supPollTimer = null;
let _supAdminFilter = '';
let _memberDrafts      = {};   // msgId -> draft text
let _adminDrafts       = {};   // msgId -> draft text
let _memberImgDrafts   = {};   // msgId -> { file, previewUrl }
let _adminImgDrafts    = {};   // msgId -> { file, previewUrl }
let _initMsgImg        = null; // { file, previewUrl } for the new-message form

function supStatusBadge(status) {
  const map = {
    open:    { bg: '#fef3c7', color: '#92400e', label: 'Open' },
    replied: { bg: '#dbeafe', color: '#1e40af', label: 'Replied' },
    closed:  { bg: '#dcfce7', color: '#166534', label: 'Closed' },
  };
  const s = map[status] || { bg: '#f1f5f9', color: '#475569', label: status };
  return `<span style="display:inline-block;padding:.15rem .55rem;border-radius:.75rem;font-size:.72rem;font-weight:600;background:${s.bg};color:${s.color}">${s.label}</span>`;
}

function formatDateTime(iso) {
  if (!iso) return '';
  return new Date(iso).toLocaleString('en-US', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

async function loadSupportPage() {
  if (userRole === 'admin') {
    document.getElementById('support-admin-view').hidden = false;
    document.getElementById('support-member-view').hidden = true;
    await loadAdminSupportList();
  } else {
    document.getElementById('support-admin-view').hidden = true;
    document.getElementById('support-member-view').hidden = false;
    await loadMemberThreads();
  }
}

function supImgHtml(url) {
  if (!url) return '';
  return `<a href="${url}" target="_blank" rel="noopener"><img src="${url}" class="sup-bubble-img" alt="attachment"></a>`;
}

function renderConversation(m, isAdmin) {
  const bubbles = [];
  // First bubble: original member message
  bubbles.push(`
    <div class="sup-bubble sup-bubble-member">
      <div class="sup-bubble-meta">${isAdmin ? `<i class="fa-solid fa-user"></i> ${escHtml(m.member_name)}` : '<i class="fa-solid fa-user"></i> You'} · ${formatDateTime(m.created_at)}</div>
      <div class="sup-bubble-body">${escHtml(m.body)}</div>
      ${supImgHtml(m.attachment_url)}
    </div>`);
  // Follow-up replies in order
  (m.replies || []).forEach(r => {
    const isAdminBubble = r.sender === 'admin';
    bubbles.push(`
      <div class="sup-bubble ${isAdminBubble ? 'sup-bubble-admin' : 'sup-bubble-member'}">
        <div class="sup-bubble-meta">${isAdminBubble ? '<i class="fa-solid fa-shield-halved"></i> Admin' : (isAdmin ? `<i class="fa-solid fa-user"></i> ${escHtml(m.member_name)}` : '<i class="fa-solid fa-user"></i> You')} · ${formatDateTime(r.created_at)}</div>
        <div class="sup-bubble-body">${escHtml(r.body)}</div>
        ${supImgHtml(r.attachment_url)}
      </div>`);
  });
  return bubbles.join('');
}

async function loadMemberThreads() {
  const el = document.getElementById('support-thread-list');
  try {
    const msgs = await apiFetch('GET', '/api/support/messages');
    if (!msgs.length) {
      el.innerHTML = '<p style="padding:1.5rem;color:var(--muted);font-size:.875rem;text-align:center">No messages yet. Use the form above to contact admin.</p>';
      return;
    }
    el.innerHTML = msgs.map(m => `
      <div class="sup-thread">
        <div class="sup-thread-head">
          <span class="sup-thread-subject">${escHtml(m.subject)}</span>
          ${supStatusBadge(m.status)}
          <span class="sup-thread-time">${formatDateTime(m.created_at)}</span>
          <button class="btn-sm danger sup-delete-btn" onclick="memberDeleteSupportMsg('${m.id}')"><i class="fa-solid fa-trash"></i></button>
        </div>
        <div class="sup-conversation">${renderConversation(m, false)}</div>
        ${m.status === 'replied' ? `
          <div class="sup-member-reply-form">
            <textarea id="sup-mreply-input-${m.id}" rows="3" placeholder="Reply to admin…" style="width:100%;box-sizing:border-box;resize:vertical;padding:.5rem .75rem;border:1.5px solid var(--border);border-radius:.45rem;font-family:inherit;font-size:.85rem;background:var(--surface);color:var(--text)"></textarea>
            <div id="sup-mreply-imgpreview-${m.id}" class="sup-img-preview" style="display:none">
              <img id="sup-mreply-thumb-${m.id}" class="sup-img-thumb" src="" alt="preview">
              <button type="button" class="sup-img-clear" onclick="clearSupImg('member','${m.id}')"><i class="fa-solid fa-xmark"></i></button>
            </div>
            <div style="display:flex;gap:.6rem;margin-top:.5rem;align-items:center">
              <button class="btn-primary-sm" onclick="sendMemberReply('${m.id}')"><i class="fa-solid fa-paper-plane"></i> Send Reply</button>
              <label class="sup-attach-btn" title="Attach image">
                <i class="fa-solid fa-image"></i>
                <input type="file" accept="image/*" style="display:none" onchange="selectSupImg('member','${m.id}',this)">
              </label>
              <span id="sup-mreply-msg-${m.id}" class="ad-form-msg"></span>
            </div>
          </div>
        ` : m.status === 'open' && (m.replies || []).length > 0 ? `
          <p class="sup-awaiting"><i class="fa-solid fa-clock"></i> Awaiting admin reply…</p>
        ` : ''}
      </div>`).join('');

    // Restore text drafts and image previews, wire up input listeners
    msgs.forEach(m => {
      if (m.status !== 'replied') return;
      const ta = document.getElementById(`sup-mreply-input-${m.id}`);
      if (ta) {
        if (_memberDrafts[m.id]) ta.value = _memberDrafts[m.id];
        ta.addEventListener('input', () => { _memberDrafts[m.id] = ta.value; });
      }
      const imgDraft = _memberImgDrafts[m.id];
      if (imgDraft) {
        const preview = document.getElementById(`sup-mreply-imgpreview-${m.id}`);
        const thumb   = document.getElementById(`sup-mreply-thumb-${m.id}`);
        if (preview && thumb) { thumb.src = imgDraft.previewUrl; preview.style.display = 'flex'; }
      }
    });
  } catch (e) {
    el.innerHTML = `<p style="padding:1.5rem;color:var(--danger);font-size:.875rem;text-align:center">${e.message}</p>`;
  }
}

async function loadAdminSupportList() {
  const el = document.getElementById('support-admin-list');
  try {
    const url = _supAdminFilter ? `/api/admin/support/messages?status=${_supAdminFilter}` : '/api/admin/support/messages';
    const msgs = await apiFetch('GET', url);

    const counts = { open: 0, replied: 0, closed: 0 };
    msgs.forEach(m => { if (counts[m.status] !== undefined) counts[m.status]++; });
    document.getElementById('sup-stat-open').textContent    = counts.open;
    document.getElementById('sup-stat-replied').textContent = counts.replied;
    document.getElementById('sup-stat-closed').textContent  = counts.closed;
    updateSupportBadge(counts.open);

    if (!msgs.length) {
      el.innerHTML = '<p style="padding:1.5rem;color:var(--muted);font-size:.875rem;text-align:center">No messages.</p>';
      return;
    }
    el.innerHTML = msgs.map(m => `
      <div class="sup-admin-msg" id="sup-msg-${m.id}">
        <div class="sup-admin-head">
          <span class="sup-admin-who"><i class="fa-solid fa-user"></i> ${escHtml(m.member_name)}</span>
          <span class="sup-thread-subject">${escHtml(m.subject)}</span>
          ${supStatusBadge(m.status)}
          <span class="sup-thread-time">${formatDateTime(m.created_at)}</span>
          ${m.status !== 'closed' ? `<button class="btn-sm" onclick="closeSupportMsg('${m.id}')"><i class="fa-solid fa-xmark"></i> Close</button>` : ''}
          <button class="btn-sm danger" onclick="adminDeleteSupportMsg('${m.id}')"><i class="fa-solid fa-trash"></i> Delete</button>
        </div>
        <div class="sup-conversation">${renderConversation(m, true)}</div>
        ${m.status !== 'closed' ? `
          <div class="sup-reply-form" id="sup-reply-form-${m.id}">
            <textarea id="sup-reply-input-${m.id}" rows="3" placeholder="Write a reply…" style="width:100%;box-sizing:border-box;resize:vertical;padding:.5rem .75rem;border:1.5px solid var(--border);border-radius:.45rem;font-family:inherit;font-size:.8rem;background:var(--surface);color:var(--text)"></textarea>
            <div id="sup-reply-imgpreview-${m.id}" class="sup-img-preview" style="display:none">
              <img id="sup-reply-thumb-${m.id}" class="sup-img-thumb" src="" alt="preview">
              <button type="button" class="sup-img-clear" onclick="clearSupImg('admin','${m.id}')"><i class="fa-solid fa-xmark"></i></button>
            </div>
            <div style="display:flex;gap:.6rem;margin-top:.5rem;align-items:center">
              <button class="btn-primary-sm" onclick="sendSupportReply('${m.id}')"><i class="fa-solid fa-paper-plane"></i> Reply</button>
              <label class="sup-attach-btn" title="Attach image">
                <i class="fa-solid fa-image"></i>
                <input type="file" accept="image/*" style="display:none" onchange="selectSupImg('admin','${m.id}',this)">
              </label>
              <span id="sup-reply-msg-${m.id}" class="ad-form-msg"></span>
            </div>
          </div>
        ` : ''}
      </div>`).join('');

    // Restore text drafts, image previews, wire up input listeners
    msgs.forEach(m => {
      if (m.status === 'closed') return;
      const ta = document.getElementById(`sup-reply-input-${m.id}`);
      if (ta) {
        if (_adminDrafts[m.id]) ta.value = _adminDrafts[m.id];
        ta.addEventListener('input', () => { _adminDrafts[m.id] = ta.value; });
      }
      const imgDraft = _adminImgDrafts[m.id];
      if (imgDraft) {
        const preview = document.getElementById(`sup-reply-imgpreview-${m.id}`);
        const thumb   = document.getElementById(`sup-reply-thumb-${m.id}`);
        if (preview && thumb) { thumb.src = imgDraft.previewUrl; preview.style.display = 'flex'; }
      }
    });
  } catch (e) {
    el.innerHTML = `<p style="padding:1.5rem;color:var(--danger);font-size:.875rem;text-align:center">${e.message}</p>`;
  }
}

async function uploadSupImg(file) {
  const form = new FormData();
  form.append('file', file);
  const res = await fetch('/api/support/upload-image', {
    method: 'POST',
    headers: { 'X-API-Key': apiKey },
    body: form,
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || `Upload failed: HTTP ${res.status}`);
  }
  return (await res.json()).url;
}

function selectSupImg(side, msgId, input) {
  const file = input.files[0];
  if (!file) return;
  const previewUrl = URL.createObjectURL(file);
  const store = side === 'admin' ? _adminImgDrafts : _memberImgDrafts;
  store[msgId] = { file, previewUrl };
  const prefix = side === 'admin' ? 'sup-reply' : 'sup-mreply';
  const preview = document.getElementById(`${prefix}-imgpreview-${msgId}`);
  const thumb   = document.getElementById(`${prefix}-thumb-${msgId}`);
  if (preview && thumb) { thumb.src = previewUrl; preview.style.display = 'flex'; }
}

function clearSupImg(side, msgId) {
  const store = side === 'admin' ? _adminImgDrafts : _memberImgDrafts;
  if (store[msgId]) { URL.revokeObjectURL(store[msgId].previewUrl); delete store[msgId]; }
  const prefix = side === 'admin' ? 'sup-reply' : 'sup-mreply';
  const preview = document.getElementById(`${prefix}-imgpreview-${msgId}`);
  if (preview) preview.style.display = 'none';
  const thumb = document.getElementById(`${prefix}-thumb-${msgId}`);
  if (thumb) thumb.src = '';
}

function escHtml(str) {
  return String(str || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

async function sendSupportReply(msgId) {
  const input   = document.getElementById(`sup-reply-input-${msgId}`);
  const msgEl   = document.getElementById(`sup-reply-msg-${msgId}`);
  const reply   = (input ? input.value : '').trim();
  const imgDraft = _adminImgDrafts[msgId];
  if (!reply && !imgDraft) return;
  msgEl.textContent = '';
  try {
    let attachment_url = null;
    if (imgDraft) {
      msgEl.textContent = 'Uploading image…';
      attachment_url = await uploadSupImg(imgDraft.file);
      clearSupImg('admin', msgId);
    }
    await apiFetch('POST', `/api/admin/support/messages/${msgId}/reply`, { reply: reply || '📎', attachment_url });
    delete _adminDrafts[msgId];
    if (input) input.value = '';
    msgEl.style.color = 'var(--success)';
    msgEl.textContent = 'Sent!';
    await sleep(1200);
    await loadAdminSupportList();
  } catch (e) {
    msgEl.style.color = 'var(--danger)';
    msgEl.textContent = e.message;
  }
}

async function sendMemberReply(msgId) {
  const input    = document.getElementById(`sup-mreply-input-${msgId}`);
  const msgEl    = document.getElementById(`sup-mreply-msg-${msgId}`);
  const btn      = input ? input.closest('.sup-member-reply-form').querySelector('button') : null;
  const reply    = (input ? input.value : '').trim();
  const imgDraft = _memberImgDrafts[msgId];
  if (!reply && !imgDraft) return;
  if (btn) btn.disabled = true;
  msgEl.textContent = '';
  try {
    let attachment_url = null;
    if (imgDraft) {
      msgEl.textContent = 'Uploading image…';
      attachment_url = await uploadSupImg(imgDraft.file);
      clearSupImg('member', msgId);
    }
    await apiFetch('POST', `/api/support/messages/${msgId}/reply`, { reply: reply || '📎', attachment_url });
    delete _memberDrafts[msgId];
    msgEl.style.color = 'var(--success)';
    msgEl.textContent = 'Reply sent!';
    if (input) input.value = '';
    await sleep(1200);
    await loadMemberThreads();
  } catch (e) {
    if (btn) btn.disabled = false;
    msgEl.style.color = 'var(--danger)';
    msgEl.textContent = e.message;
  }
}

async function closeSupportMsg(msgId) {
  if (!confirm('Close this support message?')) return;
  try {
    await apiFetch('POST', `/api/admin/support/messages/${msgId}/close`);
    await loadAdminSupportList();
  } catch (e) { alert(e.message); }
}

async function adminDeleteSupportMsg(msgId) {
  if (!confirm('Permanently delete this conversation? This cannot be undone.')) return;
  try {
    await apiFetch('DELETE', `/api/admin/support/messages/${msgId}`);
    delete _adminDrafts[msgId];
    await loadAdminSupportList();
  } catch (e) { alert(e.message); }
}

async function memberDeleteSupportMsg(msgId) {
  if (!confirm('Delete this conversation?')) return;
  try {
    await apiFetch('DELETE', `/api/support/messages/${msgId}`);
    delete _memberDrafts[msgId];
    await loadMemberThreads();
  } catch (e) { alert(e.message); }
}

function updateSupportBadge(count) {
  const badge = document.getElementById('support-nav-badge');
  if (!badge) return;
  if (count > 0) {
    badge.textContent = count > 99 ? '99+' : count;
    badge.hidden = false;
  } else {
    badge.hidden = true;
  }
}

function initSupportPage() {
  const form = document.getElementById('support-form');
  if (form) {
    form.addEventListener('submit', async e => {
      e.preventDefault();
      const subject = document.getElementById('support-subject').value.trim();
      const body    = document.getElementById('support-body').value.trim();
      const msgEl   = document.getElementById('support-form-msg');
      const btn     = document.getElementById('support-submit-btn');
      if (!subject || !body) return;
      btn.disabled = true;
      msgEl.textContent = '';
      try {
        let attachment_url = null;
        if (_initMsgImg) {
          msgEl.textContent = 'Uploading image…';
          attachment_url = await uploadSupImg(_initMsgImg.file);
          URL.revokeObjectURL(_initMsgImg.previewUrl);
          _initMsgImg = null;
          const prev = document.getElementById('support-init-imgpreview');
          if (prev) prev.style.display = 'none';
        }
        await apiFetch('POST', '/api/support/messages', { subject, body, attachment_url });
        document.getElementById('support-subject').value = '';
        document.getElementById('support-body').value    = '';
        msgEl.style.color = 'var(--success)';
        msgEl.textContent = 'Message sent! Admin will reply soon.';
        setTimeout(() => { msgEl.textContent = ''; }, 4000);
        await loadMemberThreads();
      } catch (err) {
        msgEl.style.color = 'var(--danger)';
        msgEl.textContent = err.message;
      }
      btn.disabled = false;
    });
  }

  document.querySelectorAll('.sup-filter-btn').forEach(btn => {
    btn.addEventListener('click', function () {
      document.querySelectorAll('.sup-filter-btn').forEach(b => b.classList.remove('sup-filter-active'));
      this.classList.add('sup-filter-active');
      _supAdminFilter = this.dataset.status;
      loadAdminSupportList();
    });
  });

  // Poll every 10 seconds for live feel
  if (_supPollTimer) clearInterval(_supPollTimer);
  _supPollTimer = setInterval(() => {
    if (userRole === 'admin') {
      // Always refresh badge count even when not on support page
      apiFetch('GET', '/api/admin/support/messages/count')
        .then(d => updateSupportBadge(d.open || 0))
        .catch(() => {});
      const page = document.getElementById('page-support');
      if (page && page.classList.contains('active')) loadAdminSupportList();
    } else {
      const page = document.getElementById('page-support');
      if (page && page.classList.contains('active')) loadMemberThreads();
    }
  }, 10000);
}

// ── Duplicates ────────────────────────────────────────────────────────────────

async function loadDuplicatesPage() {
  const list = document.getElementById('dup-list');
  const statsDiv = document.getElementById('dup-stats');
  list.innerHTML = '<p style="padding:2rem;color:var(--muted);font-size:.875rem;text-align:center"><i class="fa-solid fa-spinner fa-spin"></i> Scanning for duplicates…</p>';
  statsDiv.style.display = 'none';

  let data;
  try {
    data = await apiFetch('GET', '/api/admin/duplicates');
  } catch (e) {
    list.innerHTML = `<p style="padding:2rem;color:var(--danger);text-align:center">${e.message}</p>`;
    return;
  }

  if (!data.groups.length) {
    list.innerHTML = `
      <div style="text-align:center;padding:3rem 2rem;color:var(--muted)">
        <i class="fa-solid fa-circle-check" style="font-size:2.5rem;color:#22c55e;display:block;margin-bottom:.75rem"></i>
        <strong style="color:var(--text)">No duplicates found!</strong><br>
        <span style="font-size:.875rem">Your storage is clean — every file is unique.</span>
      </div>`;
    return;
  }

  // Stats
  document.getElementById('dup-stat-groups').textContent  = data.total_groups.toLocaleString();
  document.getElementById('dup-stat-wasted').textContent  = formatBytes(data.total_wasted_bytes);
  document.getElementById('dup-stat-files').textContent   = data.total_duplicate_files.toLocaleString();
  statsDiv.style.display = '';

  list.innerHTML = '';

  data.groups.forEach(group => {
    const canonicalShareId = group.files[0].share_id; // first = most downloads

    const card = document.createElement('div');
    card.className = 'card';
    card.style.marginBottom = '1.5rem';

    // Group header
    const hdr = document.createElement('div');
    hdr.className = 'card-header';
    hdr.style.cssText = 'display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:.5rem';
    hdr.innerHTML = `
      <span>
        <i class="fa-solid fa-copy" style="color:#f59e0b;margin-right:.4rem"></i>
        <strong>${group.count} copies</strong>
        <span style="color:var(--muted);font-size:.8rem;margin-left:.75rem">
          · <span style="color:#ef4444">${formatBytes(group.wasted_bytes)} wasted</span>
          · hash: <code style="font-size:.75rem">${group.file_hash.slice(0, 16)}…</code>
        </span>
      </span>`;
    card.appendChild(hdr);

    // Table
    const wrap = document.createElement('div');
    wrap.style.overflowX = 'auto';
    const table = document.createElement('table');
    table.className = 'files-table';
    table.innerHTML = `
      <thead>
        <tr>
          <th>File</th>
          <th>Uploader</th>
          <th>Uploaded</th>
          <th>Downloads</th>
          <th>Views</th>
          <th>Share Link</th>
          <th>Action</th>
        </tr>
      </thead>
      <tbody></tbody>`;
    const tbody = table.querySelector('tbody');

    group.files.forEach((file, idx) => {
      const isKeep = idx === 0;
      const tr = document.createElement('tr');
      if (isKeep) tr.style.background = 'rgba(34,197,94,.06)';

      const date = file.completed_at ? new Date(file.completed_at).toLocaleDateString() : '—';
      tr.innerHTML = `
        <td class="tf-name" style="max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${file.filename}">
          ${isKeep ? '<i class="fa-solid fa-shield-halved" style="color:#22c55e;margin-right:.35rem" title="Recommended to keep"></i>' : ''}
          ${file.filename}
          ${isKeep ? '<span style="font-size:.68rem;font-weight:700;background:#dcfce7;color:#166534;padding:.15rem .4rem;border-radius:4px;margin-left:.4rem">KEEP</span>' : ''}
        </td>
        <td style="white-space:nowrap">${file.uploaded_by}</td>
        <td style="white-space:nowrap">${date}</td>
        <td style="white-space:nowrap">${file.downloads.toLocaleString()}</td>
        <td style="white-space:nowrap">${file.views.toLocaleString()}</td>
        <td><a href="/f/${file.share_id}" target="_blank" style="font-size:.8rem;font-family:monospace">/f/${file.share_id}</a></td>
        <td style="white-space:nowrap;display:flex;gap:.4rem;flex-wrap:wrap">
          ${isKeep
            ? ''
            : `<button class="btn-danger btn-sm dup-merge-btn"
                data-file-id="${file.id}"
                data-share-id="${file.share_id}"
                data-redirect-to="${canonicalShareId}"
                data-filename="${file.filename}">
                <i class="fa-solid fa-code-merge"></i> Delete &amp; Redirect
              </button>`}
          <button class="btn-sm dup-ignore-btn"
            data-file-id="${file.id}"
            data-filename="${file.filename}">
            <i class="fa-solid fa-eye-slash"></i> Not a Duplicate
          </button>
        </td>`;
      tbody.appendChild(tr);
    });

    wrap.appendChild(table);
    card.appendChild(wrap);
    list.appendChild(card);
  });

  // Wire up merge buttons
  list.querySelectorAll('.dup-merge-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      const { fileId, shareId, redirectTo, filename } = btn.dataset;
      if (!confirm(
        `Delete duplicate and redirect its links?\n\n` +
        `File: ${filename}\n` +
        `Share link /f/${shareId} will permanently redirect to /f/${redirectTo}\n\n` +
        `• The file will be removed from storage\n` +
        `• All existing shared links will continue to work (redirected)\n\n` +
        `This cannot be undone.`
      )) return;

      btn.disabled = true;
      btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i>';
      try {
        await apiFetch('POST', `/api/admin/files/${fileId}/merge`, { redirect_to: redirectTo });
        loadDuplicatesPage();
      } catch (e) {
        btn.disabled = false;
        btn.innerHTML = '<i class="fa-solid fa-code-merge"></i> Delete &amp; Redirect';
        alert('Error: ' + e.message);
      }
    });
  });

  // Wire up "Not a Duplicate" (ignore) buttons
  list.querySelectorAll('.dup-ignore-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      const { fileId, filename } = btn.dataset;
      if (!confirm(`Mark "${filename}" as Not a Duplicate?\n\nThis file will be hidden from the duplicates scanner. You can undo this from the Ignored Files section below.`)) return;
      btn.disabled = true;
      btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i>';
      try {
        await apiFetch('POST', `/api/admin/files/${fileId}/ignore-duplicate`);
        loadDuplicatesPage();
      } catch (e) {
        btn.disabled = false;
        btn.innerHTML = '<i class="fa-solid fa-eye-slash"></i> Not a Duplicate';
        alert('Error: ' + e.message);
      }
    });
  });

  // Ignored files section
  await renderIgnoredFiles(list);
}

async function renderIgnoredFiles(container) {
  let ignored;
  try { ignored = await apiFetch('GET', '/api/admin/duplicates/excluded'); }
  catch { return; }
  if (!ignored.length) return;

  const section = document.createElement('div');
  section.style.marginTop = '2rem';

  const toggle = document.createElement('button');
  toggle.className = 'btn-sm';
  toggle.style.marginBottom = '1rem';
  toggle.innerHTML = `<i class="fa-solid fa-eye-slash"></i> Show ${ignored.length} ignored file${ignored.length !== 1 ? 's' : ''}`;

  const body = document.createElement('div');
  body.hidden = true;

  const card = document.createElement('div');
  card.className = 'card';
  const hdr = document.createElement('div');
  hdr.className = 'card-header';
  hdr.innerHTML = '<i class="fa-solid fa-eye-slash" style="color:var(--muted);margin-right:.4rem"></i> Ignored Files (excluded from duplicate detection)';
  card.appendChild(hdr);

  const wrap = document.createElement('div');
  wrap.style.overflowX = 'auto';
  const table = document.createElement('table');
  table.className = 'files-table';
  table.innerHTML = `
    <thead><tr><th>File</th><th>Uploader</th><th>Uploaded</th><th>Share Link</th><th>Action</th></tr></thead>
    <tbody></tbody>`;
  const tbody = table.querySelector('tbody');

  ignored.forEach(file => {
    const tr = document.createElement('tr');
    const date = file.completed_at ? new Date(file.completed_at).toLocaleDateString() : '—';
    tr.innerHTML = `
      <td class="tf-name" style="max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${file.filename}">${file.filename}</td>
      <td>${file.uploaded_by}</td>
      <td style="white-space:nowrap">${date}</td>
      <td><a href="/f/${file.share_id}" target="_blank" style="font-size:.8rem;font-family:monospace">/f/${file.share_id}</a></td>
      <td>
        <button class="btn-sm dup-unignore-btn" data-file-id="${file.id}" data-filename="${file.filename}">
          <i class="fa-solid fa-rotate-left"></i> Restore to Scanner
        </button>
      </td>`;
    tbody.appendChild(tr);
  });

  wrap.appendChild(table);
  card.appendChild(wrap);
  body.appendChild(card);

  toggle.addEventListener('click', () => {
    body.hidden = !body.hidden;
    toggle.innerHTML = body.hidden
      ? `<i class="fa-solid fa-eye-slash"></i> Show ${ignored.length} ignored file${ignored.length !== 1 ? 's' : ''}`
      : `<i class="fa-solid fa-eye"></i> Hide ignored files`;
  });

  body.querySelectorAll('.dup-unignore-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i>';
      try {
        await apiFetch('POST', `/api/admin/files/${btn.dataset.fileId}/unignore-duplicate`);
        loadDuplicatesPage();
      } catch (e) {
        btn.disabled = false;
        btn.innerHTML = '<i class="fa-solid fa-rotate-left"></i> Restore to Scanner';
        alert('Error: ' + e.message);
      }
    });
  });

  section.appendChild(toggle);
  section.appendChild(body);
  container.appendChild(section);
}

// ── File Manager ──────────────────────────────────────────────────────────────

let fmAllFiles = [];
let fmPage = 1;
let fmView = 'grid';
let fmDeleteTarget = null;
let fmRenameTarget = null;
const FM_PER_PAGE = 30;

const FM_CATS = {
  archive: ['zip','rar','gz','tar','7z','bz2','xz','tgz','lz4','zst'],
  video:   ['mp4','mkv','avi','mov','webm','flv','wmv','m4v','ts','vob'],
  audio:   ['mp3','wav','flac','aac','ogg','m4a','wma','opus','aiff'],
  image:   ['jpg','jpeg','png','gif','webp','svg','bmp','tiff','ico','avif','heic'],
  doc:     ['pdf','doc','docx','xls','xlsx','csv','ppt','pptx','txt','md','rtf','odt','ods'],
  app:     ['exe','msi','dmg','iso','apk','nsp','xci','rom','deb','pkg','run','bin','ipa'],
};

function fmGetCat(filename) {
  const ext = (filename || '').split('.').pop().toLowerCase();
  for (const [cat, exts] of Object.entries(FM_CATS)) {
    if (exts.includes(ext)) return cat;
  }
  return 'other';
}

async function loadFileManagerPage() {
  const grid    = document.getElementById('fm-grid');
  const statusEl = document.getElementById('fm-status');
  grid.innerHTML = '<p style="padding:2rem;color:var(--muted);font-size:.875rem;text-align:center"><i class="fa-solid fa-spinner fa-spin"></i> Loading files…</p>';
  statusEl.textContent = '';

  try {
    fmAllFiles = await apiFetch('GET', '/api/files');
  } catch (e) {
    grid.innerHTML = `<p style="padding:2rem;color:#ef4444;text-align:center"><i class="fa-solid fa-triangle-exclamation"></i> ${escHtml(e.message)}</p>`;
    return;
  }

  fmPage = 1;

  if (!document.getElementById('fm-search')._fmInit) {
    document.getElementById('fm-search')._fmInit = true;

    document.getElementById('fm-search').addEventListener('input', () => { fmPage = 1; renderFmGrid(); });
    document.getElementById('fm-sort').addEventListener('change', () => { fmPage = 1; renderFmGrid(); });

    document.querySelectorAll('#fm-tabs .fm-tab').forEach(btn => {
      btn.addEventListener('click', () => {
        document.querySelectorAll('#fm-tabs .fm-tab').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        fmPage = 1;
        renderFmGrid();
      });
    });

    document.getElementById('fm-view-grid').addEventListener('click', () => {
      fmView = 'grid';
      document.getElementById('fm-view-grid').classList.add('active');
      document.getElementById('fm-view-list').classList.remove('active');
      document.getElementById('fm-grid').classList.remove('list-view');
      renderFmGrid();
    });

    document.getElementById('fm-view-list').addEventListener('click', () => {
      fmView = 'list';
      document.getElementById('fm-view-list').classList.add('active');
      document.getElementById('fm-view-grid').classList.remove('active');
      document.getElementById('fm-grid').classList.add('list-view');
      renderFmGrid();
    });

    document.querySelectorAll('#fm-redirect-opts .fm-radio-opt').forEach(opt => {
      opt.addEventListener('click', () => {
        document.querySelectorAll('#fm-redirect-opts .fm-radio-opt').forEach(o => o.classList.remove('selected'));
        opt.classList.add('selected');
        opt.querySelector('input[type=radio]').checked = true;
        const val = opt.dataset.val;
        document.getElementById('fm-redirect-file-panel').style.display = val === 'file' ? '' : 'none';
        document.getElementById('fm-redirect-url-panel').style.display  = val === 'url'  ? '' : 'none';
      });
    });

    document.getElementById('fm-redirect-file-search').addEventListener('input', fmSearchRedirectFiles);
  }

  renderFmGrid();
}

function fmGetFiltered() {
  const search = (document.getElementById('fm-search').value || '').toLowerCase().trim();
  const cat    = (document.querySelector('#fm-tabs .fm-tab.active') || {}).dataset?.cat ?? '';
  const sort   = document.getElementById('fm-sort').value || 'newest';

  let files = fmAllFiles.filter(f => {
    if (search && !f.filename.toLowerCase().includes(search)) return false;
    if (cat && fmGetCat(f.filename) !== cat) return false;
    return true;
  });

  files.sort((a, b) => {
    if (sort === 'newest')    return new Date(b.completed_at) - new Date(a.completed_at);
    if (sort === 'oldest')    return new Date(a.completed_at) - new Date(b.completed_at);
    if (sort === 'largest')   return b.file_size - a.file_size;
    if (sort === 'smallest')  return a.file_size - b.file_size;
    if (sort === 'name')      return a.filename.localeCompare(b.filename);
    if (sort === 'downloads') return (b.downloads || 0) - (a.downloads || 0);
    return 0;
  });

  return files;
}

function renderFmGrid() {
  const grid       = document.getElementById('fm-grid');
  const statusEl   = document.getElementById('fm-status');
  const pagination = document.getElementById('fm-pagination');

  const filtered   = fmGetFiltered();
  const total      = filtered.length;
  const totalPages = Math.ceil(total / FM_PER_PAGE) || 1;
  if (fmPage > totalPages) fmPage = totalPages;

  const slice = filtered.slice((fmPage - 1) * FM_PER_PAGE, fmPage * FM_PER_PAGE);

  statusEl.textContent = total === fmAllFiles.length
    ? `${total.toLocaleString()} files`
    : `${total.toLocaleString()} of ${fmAllFiles.length.toLocaleString()} files`;

  if (!slice.length) {
    grid.innerHTML = '<p style="padding:2rem;color:var(--muted);font-size:.875rem;text-align:center"><i class="fa-solid fa-magnifying-glass"></i> No files match your search</p>';
    pagination.innerHTML = '';
    return;
  }

  grid.innerHTML = slice.map(f => {
    const uploaderStr = f.uploaded_by
      ? `<span><i class="fa-solid fa-user" style="font-size:.68rem"></i> ${escHtml(f.uploaded_by)}</span>`
      : '';
    const openBtn    = f.share_url
      ? `<a href="${f.share_url}" target="_blank" class="btn-primary-sm" title="Open file page"><i class="fa-solid fa-arrow-up-right-from-square"></i></a>`
      : '';
    const renameBtn   = `<button class="btn-sm" onclick="openFmRenameModal('${f.id}')" title="Rename"><i class="fa-solid fa-pen-to-square"></i></button>`;
    const renameBtnSm = `<button class="btn-sm" onclick="openFmRenameModal('${f.id}')" title="Rename"><i class="fa-solid fa-pen-to-square"></i></button>`;
    const delBtn      = `<button class="btn-danger" onclick="openFmDeleteModal('${f.id}')"><i class="fa-solid fa-trash-can"></i> Delete</button>`;
    const delBtnSm    = `<button class="btn-danger" style="padding:.35rem .65rem" onclick="openFmDeleteModal('${f.id}')"><i class="fa-solid fa-trash-can"></i></button>`;

    if (fmView === 'list') {
      return `
        <div class="fm-card fm-card-list">
          <div class="fm-card-icon">${fileIcon(f.filename)}</div>
          <div class="fm-card-body">
            <div class="fm-card-title">${escHtml(f.filename)}</div>
            <div class="fm-card-meta">
              <span>${formatBytes(f.file_size)}</span>
              ${uploaderStr}
              <span>${formatDate(f.completed_at)}</span>
              <span><i class="fa-solid fa-eye" style="font-size:.68rem"></i> ${f.views}</span>
              <span><i class="fa-solid fa-download" style="font-size:.68rem"></i> ${f.downloads}</span>
            </div>
          </div>
          <div class="fm-card-actions">${openBtn}${renameBtnSm}${delBtnSm}</div>
        </div>`;
    }

    return `
      <div class="fm-card">
        <div class="fm-card-head">
          <div class="fm-card-icon">${fileIcon(f.filename)}</div>
          <div style="min-width:0;flex:1">
            <div class="fm-card-title">${escHtml(f.filename)}</div>
            <div class="fm-card-meta">
              <span>${formatBytes(f.file_size)}</span>
              ${uploaderStr}
            </div>
          </div>
        </div>
        <div class="fm-card-stats">
          <span><i class="fa-solid fa-eye" style="font-size:.7rem"></i> ${f.views} views</span>
          <span><i class="fa-solid fa-download" style="font-size:.7rem"></i> ${f.downloads} dl</span>
          <span style="margin-left:auto">${formatDate(f.completed_at)}</span>
        </div>
        <div class="fm-card-actions">
          ${f.share_url ? `<a href="${f.share_url}" target="_blank" class="btn-primary-sm" style="flex:1;text-align:center"><i class="fa-solid fa-arrow-up-right-from-square"></i> Open</a>` : ''}
          ${renameBtn}
          ${delBtn}
        </div>
      </div>`;
  }).join('');

  if (totalPages <= 1) { pagination.innerHTML = ''; return; }

  let pHtml = `<button class="fm-page-btn" onclick="fmGoPage(${fmPage - 1})" ${fmPage <= 1 ? 'disabled' : ''}>&#8249; Prev</button>`;
  const start = Math.max(1, fmPage - 2);
  const end   = Math.min(totalPages, fmPage + 2);
  if (start > 1) pHtml += `<button class="fm-page-btn" onclick="fmGoPage(1)">1</button>${start > 2 ? '<span style="color:var(--muted);padding:0 .25rem">…</span>' : ''}`;
  for (let p = start; p <= end; p++) {
    pHtml += `<button class="fm-page-btn${p === fmPage ? ' active' : ''}" onclick="fmGoPage(${p})">${p}</button>`;
  }
  if (end < totalPages) pHtml += `${end < totalPages - 1 ? '<span style="color:var(--muted);padding:0 .25rem">…</span>' : ''}<button class="fm-page-btn" onclick="fmGoPage(${totalPages})">${totalPages}</button>`;
  pHtml += `<button class="fm-page-btn" onclick="fmGoPage(${fmPage + 1})" ${fmPage >= totalPages ? 'disabled' : ''}>Next &#8250;</button>`;
  pagination.innerHTML = pHtml;
}

function fmGoPage(p) {
  fmPage = p;
  renderFmGrid();
  document.getElementById('main').scrollTop = 0;
}

function openFmDeleteModal(id) {
  fmDeleteTarget = fmAllFiles.find(f => f.id === id) || null;
  if (!fmDeleteTarget) return;
  document.getElementById('fm-modal-filename').textContent = fmDeleteTarget.filename;
  document.getElementById('fm-modal-err').textContent = '';
  document.querySelectorAll('#fm-redirect-opts .fm-radio-opt').forEach(o => o.classList.remove('selected'));
  document.querySelector('#fm-redirect-opts .fm-radio-opt[data-val="none"]').classList.add('selected');
  document.querySelector('#fm-redirect-opts input[value="none"]').checked = true;
  document.getElementById('fm-redirect-file-panel').style.display = 'none';
  document.getElementById('fm-redirect-url-panel').style.display  = 'none';
  document.getElementById('fm-redirect-file-search').value        = '';
  document.getElementById('fm-redirect-file-results').innerHTML   = '';
  document.getElementById('fm-redirect-file-id').value            = '';
  document.getElementById('fm-redirect-file-selected').textContent = '';
  document.getElementById('fm-redirect-url-input').value          = '';
  const btn = document.getElementById('fm-modal-confirm');
  btn.disabled = false;
  btn.innerHTML = '<i class="fa-solid fa-trash-can"></i> Delete File';
  document.getElementById('fm-delete-modal').style.display = 'flex';
}

function closeFmDeleteModal() {
  document.getElementById('fm-delete-modal').style.display = 'none';
  fmDeleteTarget = null;
}

function openFmRenameModal(id) {
  fmRenameTarget = fmAllFiles.find(f => f.id === id) || null;
  if (!fmRenameTarget) return;
  const input = document.getElementById('fm-rename-input');
  input.value = fmRenameTarget.filename;
  document.getElementById('fm-rename-err').textContent = '';
  const btn = document.getElementById('fm-rename-confirm');
  btn.disabled = false;
  btn.innerHTML = '<i class="fa-solid fa-floppy-disk"></i> Save';
  document.getElementById('fm-rename-modal').style.display = 'flex';
  setTimeout(() => { input.focus(); input.select(); }, 50);
}

function closeFmRenameModal() {
  document.getElementById('fm-rename-modal').style.display = 'none';
  fmRenameTarget = null;
}

async function confirmFmRename() {
  if (!fmRenameTarget) return;
  const input = document.getElementById('fm-rename-input');
  const errEl = document.getElementById('fm-rename-err');
  const btn   = document.getElementById('fm-rename-confirm');
  const newName = input.value.trim();
  errEl.textContent = '';

  if (!newName) { errEl.textContent = 'Filename cannot be empty.'; return; }
  if (newName === fmRenameTarget.filename) { closeFmRenameModal(); return; }

  btn.disabled = true;
  btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Saving…';

  try {
    const res = await apiFetch('POST', `/api/admin/files/${fmRenameTarget.id}/rename`, { filename: newName });
    const targetId = fmRenameTarget.id;
    fmAllFiles = fmAllFiles.map(f => f.id === targetId ? { ...f, filename: res.filename } : f);
    closeFmRenameModal();
    renderFmGrid();
  } catch (e) {
    errEl.textContent = e.message;
    btn.disabled = false;
    btn.innerHTML = '<i class="fa-solid fa-floppy-disk"></i> Save';
  }
}


function fmSearchRedirectFiles() {
  const q       = document.getElementById('fm-redirect-file-search').value.toLowerCase().trim();
  const results = document.getElementById('fm-redirect-file-results');
  if (!q) { results.innerHTML = ''; return; }

  const matches = fmAllFiles
    .filter(f => f.id !== fmDeleteTarget?.id && f.filename.toLowerCase().includes(q))
    .slice(0, 8);

  if (!matches.length) {
    results.innerHTML = '<p style="padding:.5rem .75rem;font-size:.78rem;color:var(--muted)">No files found</p>';
    return;
  }

  results.innerHTML = matches.map(f => `
    <div class="fm-file-result" onclick="fmSelectRedirectFile('${f.share_id}',${JSON.stringify(f.filename)})">
      ${fileIcon(f.filename)}
      <span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${escHtml(f.filename)}</span>
      <span class="fm-file-result-size">${formatBytes(f.file_size)}</span>
    </div>`).join('');
}

function fmSelectRedirectFile(shareId, filename) {
  document.getElementById('fm-redirect-file-id').value            = shareId;
  document.getElementById('fm-redirect-file-selected').textContent = '✓ Selected: ' + filename;
  document.getElementById('fm-redirect-file-search').value        = filename;
  document.getElementById('fm-redirect-file-results').innerHTML   = '';
}

async function confirmFmDelete() {
  if (!fmDeleteTarget) return;
  const btn   = document.getElementById('fm-modal-confirm');
  const errEl = document.getElementById('fm-modal-err');
  errEl.textContent = '';

  const checked      = document.querySelector('#fm-redirect-opts input[name="fm-redirect"]:checked');
  const redirectType = checked ? checked.value : 'none';

  const body = { redirect_type: redirectType };
  if (redirectType === 'file') {
    const shareId = document.getElementById('fm-redirect-file-id').value.trim();
    if (!shareId) { errEl.textContent = 'Please select a target file first.'; return; }
    body.redirect_to_share_id = shareId;
  } else if (redirectType === 'url') {
    const url = document.getElementById('fm-redirect-url-input').value.trim();
    if (!url) { errEl.textContent = 'Please enter a redirect URL.'; return; }
    body.redirect_to_url = url;
  }

  btn.disabled = true;
  btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Deleting…';

  try {
    const targetId = fmDeleteTarget.id;
    await apiFetch('POST', `/api/admin/files/${targetId}/delete`, body);
    fmAllFiles = fmAllFiles.filter(f => f.id !== targetId);
    closeFmDeleteModal();
    renderFmGrid();
  } catch (e) {
    errEl.textContent = e.message;
    btn.disabled = false;
    btn.innerHTML = '<i class="fa-solid fa-trash-can"></i> Delete File';
  }
}

// ── File Reports ──────────────────────────────────────────────────────────────

const RPT_REASON_LABELS = {
  not_downloading:  'Not Downloading',
  link_expired:     'Link / Token Expired',
  corrupt_or_wrong: 'File Corrupt or Wrong',
  other:            'Other',
};
const RPT_REASON_COLORS = {
  not_downloading:  '#ef4444',
  link_expired:     '#f59e0b',
  corrupt_or_wrong: '#8b5cf6',
  other:            '#64748b',
};

let rptActiveFilter = '';

async function loadFileReportsPage() {
  const listEl = document.getElementById('rpt-list');
  listEl.innerHTML = '<p style="padding:1.5rem;color:var(--muted);font-size:.875rem;text-align:center"><i class="fa-solid fa-spinner fa-spin"></i> Loading…</p>';

  if (!document.getElementById('rpt-list')._rptInit) {
    document.getElementById('rpt-list')._rptInit = true;
    document.querySelectorAll('.rpt-filter-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        rptActiveFilter = btn.dataset.status;
        document.querySelectorAll('.rpt-filter-btn').forEach(b => b.classList.remove('rpt-filter-active'));
        btn.classList.add('rpt-filter-active');
        loadFileReportsPage();
      });
    });
  }

  let data;
  try {
    data = await apiFetch('GET', `/api/admin/reports?status=${rptActiveFilter}`);
  } catch (e) {
    listEl.innerHTML = `<p style="padding:1.5rem;color:var(--danger)">${escHtml(e.message)}</p>`;
    return;
  }

  document.getElementById('rpt-stat-open').textContent     = data.open_count.toLocaleString();
  document.getElementById('rpt-stat-resolved').textContent = data.resolved_count.toLocaleString();

  // Update nav badge
  const badge = document.getElementById('rpt-nav-badge');
  if (badge) {
    badge.hidden = data.open_count === 0;
    badge.textContent = data.open_count;
  }

  if (!data.reports.length) {
    listEl.innerHTML = '<p style="padding:1.5rem;color:var(--muted);font-size:.875rem;text-align:center">No reports found.</p>';
    return;
  }

  listEl.innerHTML = data.reports.map(r => {
    const color = RPT_REASON_COLORS[r.reason] || '#64748b';
    const label = RPT_REASON_LABELS[r.reason] || r.reason;
    const isOpen = r.status === 'open';
    return `
      <div class="rpt-row" id="rpt-${r.id}">
        <div class="rpt-row-top">
          <span class="rpt-badge" style="background:${color}18;color:${color};border-color:${color}40">
            <i class="fa-solid fa-flag"></i> ${label}
          </span>
          <span class="rpt-filename" title="${escHtml(r.filename)}">${escHtml(r.filename)}</span>
          <span class="rpt-time">${formatDate(r.created_at)}</span>
          ${r.share_id ? `<a href="/f/${r.share_id}" target="_blank" class="btn-sm" style="font-size:.74rem;padding:.2rem .5rem">
            <i class="fa-solid fa-arrow-up-right-from-square"></i>
          </a>` : ''}
        </div>
        ${r.message ? `<div class="rpt-message"><i class="fa-solid fa-quote-left" style="font-size:.7rem;color:var(--muted)"></i> ${escHtml(r.message)}</div>` : ''}
        <div class="rpt-row-foot">
          ${r.ip ? `<span class="rpt-ip"><i class="fa-solid fa-network-wired" style="font-size:.7rem"></i> ${escHtml(r.ip)}</span>` : ''}
          <span class="rpt-status-badge ${isOpen ? 'rpt-open' : 'rpt-resolved'}">${isOpen ? 'Open' : 'Resolved'}</span>
          <button class="btn-sm ${isOpen ? 'rpt-resolve-btn' : 'rpt-reopen-btn'}"
            onclick="${isOpen ? `rptResolve('${r.id}')` : `rptReopen('${r.id}')`}"
            style="margin-left:auto">
            ${isOpen
              ? '<i class="fa-solid fa-circle-check"></i> Resolve'
              : '<i class="fa-solid fa-rotate-left"></i> Reopen'}
          </button>
        </div>
      </div>`;
  }).join('');
}

async function rptResolve(id) {
  await apiFetch('POST', `/api/admin/reports/${id}/resolve`);
  loadFileReportsPage();
}

async function rptReopen(id) {
  await apiFetch('POST', `/api/admin/reports/${id}/reopen`);
  loadFileReportsPage();
}

// ── Page navigation ───────────────────────────────────────────────────────────

const ADMIN_PAGES = ['dashboard', 'ads', 'storage', 'downloads', 'team', 'requests', 'duplicates', 'filemanager', 'filereports'];
// 'support' is visible to both roles — intentionally not in ADMIN_PAGES

function applyRoleUI() {
  ADMIN_PAGES.forEach(page => {
    const btn = document.querySelector(`.nav-item[data-page="${page}"]`);
    if (btn) btn.style.display = userRole === 'admin' ? '' : 'none';
  });
}

function showPage(page) {
  // Redirect members away from admin-only pages
  if (userRole !== 'admin' && ADMIN_PAGES.includes(page)) page = 'upload';
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(b => b.classList.remove('active'));
  document.getElementById(`page-${page}`).classList.add('active');
  const navBtn = document.querySelector(`[data-page="${page}"]`);
  if (navBtn) navBtn.classList.add('active');
  if (page === 'dashboard') loadDashboard();
  if (page === 'files')     loadFileList();
  if (page === 'ads')       loadAdsPage();
  if (page === 'storage')   loadStoragePage();
  if (page === 'downloads') loadDownloadsPage();
  if (page === 'team')      loadTeamPage();
  if (page === 'requests')   loadRequestsPage();
  if (page === 'support')    loadSupportPage();
  if (page === 'duplicates')   loadDuplicatesPage();
  if (page === 'filemanager')  loadFileManagerPage();
  if (page === 'filereports')  loadFileReportsPage();
}

// ── Auth ──────────────────────────────────────────────────────────────────────

async function checkAuth() {
  try {
    const data = await apiFetch('POST', '/api/auth/verify');
    userRole = data.role || 'admin';
    return true;
  } catch { return false; }
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
  resumePendingImports();
  initAdForm();
  initRedirectUrlForm();
  initStorageForm();
  initKeyForm();
  initRequestsPage();
  initSupportPage();
  applyRoleUI();

  document.querySelectorAll('.dl-period-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      dlActiveDays = parseInt(btn.dataset.days);
      loadDownloadsPage();
    });
  });

  document.querySelectorAll('.bw-period-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      bwActiveDays = parseInt(btn.dataset.days);
      document.querySelectorAll('.bw-period-btn').forEach(b => b.classList.toggle('active', b === btn));
      loadBandwidthStats();
    });
  });

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

  window.addEventListener('offline', () => activeUploaders.forEach(u => u.pause()));
  window.addEventListener('online',  () => activeUploaders.forEach(u => u.resume()));

  showPage(userRole === 'admin' ? 'dashboard' : 'upload');
}

// ── Boot ──────────────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', async () => {
  if (apiKey) {
    // Optimistic: show panel immediately without waiting for auth round-trip.
    // The panel renders with assumed admin role; background verify corrects it.
    userRole = 'admin';
    document.getElementById('auth-overlay').style.display = 'none';
    initApp();
    checkAuth().then(valid => {
      if (!valid) { localStorage.removeItem(LS_KEY); apiKey = ''; location.reload(); }
      else applyRoleUI(); // correct role if actually a member key
    });
  } else {
    await initAuth();
    if (apiKey) initApp();
  }
});
