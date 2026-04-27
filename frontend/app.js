'use strict';

// ── Constants ─────────────────────────────────────────────────────────────────
const CHUNK_SIZE    = 10 * 1024 * 1024;  // 10 MB
const MAX_BYTES     = 10 * 1024 * 1024 * 1024;  // 10 GB
const MAX_CONC      = 3;   // parallel chunk uploads per file
const MAX_RETRIES   = 4;
const SAMPLE_BYTES  = 512 * 1024;  // fingerprint sample size
const LS_KEY        = 'datadock_apikey';

// ── State ─────────────────────────────────────────────────────────────────────
let apiKey = localStorage.getItem(LS_KEY) || '';
let activeUploaders = [];   // ChunkedUploader instances

// ── Helpers ───────────────────────────────────────────────────────────────────

function formatBytes(b) {
  if (b < 1024)         return b + ' B';
  if (b < 1048576)      return (b / 1024).toFixed(1) + ' KB';
  if (b < 1073741824)   return (b / 1048576).toFixed(1) + ' MB';
  return (b / 1073741824).toFixed(2) + ' GB';
}

function formatDate(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return d.toLocaleDateString() + ' ' + d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function fileIcon(name) {
  const ext = name.split('.').pop().toLowerCase();
  const icons = {
    zip: '🗜️', rar: '🗜️', gz: '🗜️', tar: '🗜️',
    mp4: '🎬', mkv: '🎬', avi: '🎬', mov: '🎬', webm: '🎬',
    mp3: '🎵', wav: '🎵', flac: '🎵', aac: '🎵',
    jpg: '🖼️', jpeg: '🖼️', png: '🖼️', gif: '🖼️', webp: '🖼️', svg: '🖼️',
    pdf: '📄', doc: '📝', docx: '📝', xls: '📊', xlsx: '📊',
    ppt: '📊', pptx: '📊', txt: '📄',
    exe: '⚙️', msi: '⚙️', dmg: '💿', iso: '💿',
  };
  return icons[ext] || '📦';
}

async function fingerprint(file) {
  const parts = [
    file.slice(0, SAMPLE_BYTES),
    file.slice(Math.max(0, file.size - SAMPLE_BYTES)),
    new Blob([`${file.size}:${file.name}`]),
  ];
  const buf = await new Blob(parts).arrayBuffer();
  const hash = await crypto.subtle.digest('SHA-256', buf);
  return Array.from(new Uint8Array(hash))
    .map(b => b.toString(16).padStart(2, '0')).join('');
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
    this.completedParts = new Map();  // partNumber → etag
    this.chunkProgress  = new Map();  // partNumber → bytes in-flight
    this._xhr      = null;
  }

  emit(name, detail = {}) {
    this.dispatchEvent(new CustomEvent(name, { detail }));
  }

  // ── Main entry ────────────────────────────────────────────────────────────────
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

    for (const p of init.completed_parts) {
      this.completedParts.set(p.part_number, p.etag);
    }

    const totalParts = Math.ceil(this.file.size / CHUNK_SIZE);
    if (init.resuming && this.completedParts.size > 0) {
      this.emit('resuming', {
        done: this.completedParts.size,
        total: totalParts,
      });
    }

    this.emit('status', { status: 'uploading' });
    this._emitProgress(totalParts);
    await this._uploadAllParts(totalParts);
    if (this.aborted) return;

    this.emit('status', { status: 'completing' });

    const result = await apiFetch('POST', `/api/upload/${this.uploadId}/complete`);

    this.emit('done', result);
  }

  // ── Upload all pending parts with concurrency ──────────────────────────────
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

    // POST chunk directly to our backend — backend forwards to B2 (no CORS needed)
    const result = await this._xhrPost(
      `/api/upload/${this.uploadId}/chunk/${partNum}`,
      blob, partNum, totalParts
    );

    this.completedParts.set(partNum, result.etag);
    this.chunkProgress.delete(partNum);
    this._emitProgress(totalParts);
  }

  // ── XHR POST chunk to backend with progress events ────────────────────────
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
        if (xhr.status >= 200 && xhr.status < 300) {
          resolve(JSON.parse(xhr.responseText));
        } else {
          reject(new Error(`HTTP ${xhr.status}`));
        }
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
      total: this.file.size,
      percent: Math.min(100, Math.round((bytes / this.file.size) * 100)),
    });
  }

  abort() {
    this.aborted = true;
    if (this._xhr) this._xhr.abort();
    if (this.uploadId) {
      apiFetch('DELETE', `/api/upload/${this.uploadId}`).catch(() => {});
    }
    this.emit('status', { status: 'aborted' });
  }
}

// ── UI ─────────────────────────────────────────────────────────────────────────

function buildUploadItem(file) {
  const id = 'item-' + Math.random().toString(36).slice(2);

  const el = document.createElement('div');
  el.className = 'upload-item';
  el.id = id;
  el.innerHTML = `
    <div class="upload-item-header">
      <div class="file-icon">${fileIcon(file.name)}</div>
      <div class="file-meta">
        <div class="file-name" title="${file.name}">${file.name}</div>
        <div class="file-size">${formatBytes(file.size)}</div>
      </div>
      <div class="item-actions">
        <span class="status-badge hashing">Hashing…</span>
        <button class="btn-sm danger abort-btn">Cancel</button>
      </div>
    </div>
    <div class="progress-wrap"><div class="progress-bar" style="width:0%"></div></div>
    <div class="progress-labels">
      <span class="progress-text">0%</span>
      <span class="speed-text"></span>
    </div>`;

  return { el, id };
}

function attachUploaderEvents(uploader, el) {
  const badge     = el.querySelector('.status-badge');
  const bar       = el.querySelector('.progress-bar');
  const pctText   = el.querySelector('.progress-text');
  const speedText = el.querySelector('.speed-text');
  const abortBtn  = el.querySelector('.abort-btn');
  let lastBytes = 0, lastTime = Date.now();

  abortBtn.addEventListener('click', () => uploader.abort());

  uploader.addEventListener('status', e => {
    const s = e.detail.status;
    badge.className = `status-badge ${s}`;
    badge.textContent = {
      hashing:      'Hashing…',
      initializing: 'Starting…',
      uploading:    'Uploading',
      resuming:     'Resuming…',
      completing:   'Finalizing…',
      done:         'Done',
      error:        'Error',
      aborted:      'Cancelled',
    }[s] || s;

    if (s === 'aborted') {
      abortBtn.remove();
      bar.classList.add('error');
    }
  });

  uploader.addEventListener('resuming', e => {
    badge.className = 'status-badge resuming';
    badge.textContent = `Resuming (${e.detail.done}/${e.detail.total} parts done)`;
  });

  uploader.addEventListener('progress', e => {
    const { uploaded, total, percent } = e.detail;
    bar.style.width = percent + '%';
    pctText.textContent = percent + '%';

    const now = Date.now();
    const dt = (now - lastTime) / 1000;
    if (dt >= 1) {
      const speed = (uploaded - lastBytes) / dt;
      speedText.textContent = formatBytes(Math.max(0, speed)) + '/s';
      lastBytes = uploaded;
      lastTime  = now;
    }
  });

  uploader.addEventListener('done', e => {
    const { download_url, filename } = e.detail;
    bar.style.width = '100%';
    bar.classList.add('done');
    badge.className = 'status-badge done';
    badge.textContent = 'Done';
    abortBtn.remove();
    pctText.textContent = '100%';
    speedText.textContent = '';

    const doneDiv = document.createElement('div');
    doneDiv.className = 'done-link';
    doneDiv.innerHTML = `
      <a href="${download_url}" target="_blank" rel="noopener">${filename}</a>
      <button class="copy-btn">Copy link</button>`;
    doneDiv.querySelector('.copy-btn').addEventListener('click', () => {
      navigator.clipboard.writeText(download_url);
      doneDiv.querySelector('.copy-btn').textContent = 'Copied!';
      setTimeout(() => { doneDiv.querySelector('.copy-btn').textContent = 'Copy link'; }, 2000);
    });
    el.appendChild(doneDiv);

    loadFileList();
  });
}

async function startUpload(file) {
  if (file.size > MAX_BYTES) {
    alert(`"${file.name}" is ${formatBytes(file.size)}, which exceeds the 10 GB limit.`);
    return;
  }

  const queue = document.getElementById('upload-queue');
  const { el } = buildUploadItem(file);
  queue.appendChild(el);
  showUploadSection();

  const uploader = new ChunkedUploader(file);
  activeUploaders.push(uploader);
  attachUploaderEvents(uploader, el);

  try {
    await uploader.start();
  } catch (err) {
    if (!uploader.aborted) {
      const badge = el.querySelector('.status-badge');
      badge.className = 'status-badge error';
      badge.textContent = 'Error';
      const bar = el.querySelector('.progress-bar');
      bar.classList.add('error');
      const speedText = el.querySelector('.speed-text');
      speedText.textContent = err.message;
      el.querySelector('.abort-btn')?.remove();
    }
  } finally {
    activeUploaders = activeUploaders.filter(u => u !== uploader);
  }
}

// ── File list ─────────────────────────────────────────────────────────────────

async function loadFileList() {
  const list = document.getElementById('file-list');
  try {
    const files = await apiFetch('GET', '/api/files');
    list.innerHTML = '';
    for (const f of files) {
      const row = document.createElement('div');
      row.className = 'file-row';
      row.dataset.id = f.id;
      row.innerHTML = `
        <div class="file-icon">${fileIcon(f.filename)}</div>
        <div class="file-meta">
          <div class="file-name">${f.filename}</div>
          <div class="file-date">${formatBytes(f.file_size)} · ${formatDate(f.completed_at)}</div>
        </div>
        <div class="file-row-actions">
          <button class="btn-sm copy-dl-btn">Copy link</button>
          <a href="${f.download_url}" download class="btn-sm" style="text-decoration:none">Download</a>
          <button class="btn-sm danger del-btn">Delete</button>
        </div>`;

      row.querySelector('.copy-dl-btn').addEventListener('click', () => {
        navigator.clipboard.writeText(f.download_url);
        row.querySelector('.copy-dl-btn').textContent = 'Copied!';
        setTimeout(() => { row.querySelector('.copy-dl-btn').textContent = 'Copy link'; }, 2000);
      });

      row.querySelector('.del-btn').addEventListener('click', async () => {
        if (!confirm(`Delete "${f.filename}"?`)) return;
        try {
          await apiFetch('DELETE', `/api/files/${f.id}`);
          row.remove();
        } catch (e) {
          alert('Delete failed: ' + e.message);
        }
      });

      list.appendChild(row);
    }
  } catch (e) {
    list.innerHTML = `<p style="color:var(--danger);font-size:.85rem">Failed to load files: ${e.message}</p>`;
  }
}

// ── Tab switching ─────────────────────────────────────────────────────────────

function showTab(tab) {
  document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
  document.getElementById(tab + '-tab').classList.add('active');
  document.querySelector(`[data-tab="${tab}"]`).classList.add('active');
  if (tab === 'files') loadFileList();
}

function showUploadSection() {
  const queue = document.getElementById('upload-queue');
  if (!document.getElementById('upload-queue-title').hidden) return;
  document.getElementById('upload-queue-title').hidden = false;
}

// ── Auth ──────────────────────────────────────────────────────────────────────

async function checkAuth() {
  try {
    await apiFetch('POST', '/api/auth/verify');
    return true;
  } catch {
    return false;
  }
}

async function initAuth() {
  const overlay = document.getElementById('auth-overlay');

  if (apiKey) {
    const ok = await checkAuth();
    if (ok) {
      overlay.style.display = 'none';
      return;
    }
    localStorage.removeItem(LS_KEY);
    apiKey = '';
  }

  overlay.style.display = 'flex';

  const form    = document.getElementById('auth-form');
  const input   = document.getElementById('auth-input');
  const errEl   = document.getElementById('auth-error');
  const btn     = document.getElementById('auth-btn');

  form.addEventListener('submit', async e => {
    e.preventDefault();
    const key = input.value.trim();
    if (!key) return;
    btn.disabled = true;
    btn.textContent = 'Verifying…';
    errEl.textContent = '';

    apiKey = key;
    const ok = await checkAuth();
    if (ok) {
      localStorage.setItem(LS_KEY, key);
      overlay.style.display = 'none';
      loadFileList();
    } else {
      apiKey = '';
      errEl.textContent = 'Invalid API key. Try again.';
      input.select();
    }
    btn.disabled = false;
    btn.textContent = 'Unlock';
  });
}

// ── Boot ──────────────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', async () => {
  await initAuth();

  // Show API key hint
  if (apiKey) {
    const badge = document.getElementById('key-badge');
    badge.textContent = 'Key: ' + apiKey.slice(0, 4) + '••••';
    badge.hidden = false;
  }

  // Nav tabs
  document.querySelectorAll('.nav-btn[data-tab]').forEach(btn => {
    btn.addEventListener('click', () => showTab(btn.dataset.tab));
  });

  // Drop zone
  const zone  = document.getElementById('drop-zone');
  const input = document.getElementById('file-input');

  zone.addEventListener('click', () => input.click());

  zone.addEventListener('dragover', e => {
    e.preventDefault();
    zone.classList.add('drag-over');
  });
  zone.addEventListener('dragleave', () => zone.classList.remove('drag-over'));
  zone.addEventListener('drop', e => {
    e.preventDefault();
    zone.classList.remove('drag-over');
    [...e.dataTransfer.files].forEach(startUpload);
  });

  input.addEventListener('change', () => {
    [...input.files].forEach(startUpload);
    input.value = '';
  });

  // Upload queue title hidden by default
  document.getElementById('upload-queue-title').hidden = true;
  document.getElementById('upload-queue').addEventListener('DOMSubtreeModified', () => {
    const q = document.getElementById('upload-queue');
    document.getElementById('upload-queue-title').hidden = q.children.length === 0;
  });
});
