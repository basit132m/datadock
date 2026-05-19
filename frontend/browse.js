(function () {
  'use strict';

  // ── Theme toggle ──────────────────────────────────────────────────────────
  const body     = document.body;
  const themeBtn  = document.getElementById('br-theme-toggle');
  const themeIcon = themeBtn.querySelector('i');

  function applyTheme(dark) {
    body.classList.toggle('br-dark', dark);
    themeIcon.className = dark ? 'fa-solid fa-sun' : 'fa-solid fa-moon';
  }
  applyTheme(localStorage.getItem('lp-theme') === 'dark'); // share pref with landing page
  themeBtn.addEventListener('click', () => {
    const isDark = !body.classList.contains('br-dark');
    localStorage.setItem('lp-theme', isDark ? 'dark' : 'light');
    applyTheme(isDark);
  });

  // ── File type map (mirrors landing.js) ───────────────────────────────────
  const FILE_TYPES = {
    zip:  { fa: 'fa-file-zipper',     color: '#f59e0b', bg: '#fef3c7' },
    rar:  { fa: 'fa-file-zipper',     color: '#f59e0b', bg: '#fef3c7' },
    gz:   { fa: 'fa-file-zipper',     color: '#f59e0b', bg: '#fef3c7' },
    tar:  { fa: 'fa-file-zipper',     color: '#f59e0b', bg: '#fef3c7' },
    '7z': { fa: 'fa-file-zipper',     color: '#f59e0b', bg: '#fef3c7' },
    mp4:  { fa: 'fa-file-video',      color: '#ef4444', bg: '#fee2e2' },
    mkv:  { fa: 'fa-file-video',      color: '#ef4444', bg: '#fee2e2' },
    avi:  { fa: 'fa-file-video',      color: '#ef4444', bg: '#fee2e2' },
    mov:  { fa: 'fa-file-video',      color: '#ef4444', bg: '#fee2e2' },
    webm: { fa: 'fa-file-video',      color: '#ef4444', bg: '#fee2e2' },
    mp3:  { fa: 'fa-file-audio',      color: '#8b5cf6', bg: '#ede9fe' },
    wav:  { fa: 'fa-file-audio',      color: '#8b5cf6', bg: '#ede9fe' },
    flac: { fa: 'fa-file-audio',      color: '#8b5cf6', bg: '#ede9fe' },
    aac:  { fa: 'fa-file-audio',      color: '#8b5cf6', bg: '#ede9fe' },
    jpg:  { fa: 'fa-file-image',      color: '#0ea5e9', bg: '#e0f2fe' },
    jpeg: { fa: 'fa-file-image',      color: '#0ea5e9', bg: '#e0f2fe' },
    png:  { fa: 'fa-file-image',      color: '#0ea5e9', bg: '#e0f2fe' },
    gif:  { fa: 'fa-file-image',      color: '#0ea5e9', bg: '#e0f2fe' },
    webp: { fa: 'fa-file-image',      color: '#0ea5e9', bg: '#e0f2fe' },
    svg:  { fa: 'fa-file-image',      color: '#0ea5e9', bg: '#e0f2fe' },
    pdf:  { fa: 'fa-file-pdf',        color: '#dc2626', bg: '#fee2e2' },
    doc:  { fa: 'fa-file-word',       color: '#2563eb', bg: '#dbeafe' },
    docx: { fa: 'fa-file-word',       color: '#2563eb', bg: '#dbeafe' },
    xls:  { fa: 'fa-file-excel',      color: '#16a34a', bg: '#dcfce7' },
    xlsx: { fa: 'fa-file-excel',      color: '#16a34a', bg: '#dcfce7' },
    csv:  { fa: 'fa-file-csv',        color: '#16a34a', bg: '#dcfce7' },
    ppt:  { fa: 'fa-file-powerpoint', color: '#ea580c', bg: '#ffedd5' },
    pptx: { fa: 'fa-file-powerpoint', color: '#ea580c', bg: '#ffedd5' },
    txt:  { fa: 'fa-file-lines',      color: '#64748b', bg: '#f1f5f9' },
    exe:  { fa: 'fa-gear',            color: '#4b5563', bg: '#f3f4f6' },
    msi:  { fa: 'fa-gear',            color: '#4b5563', bg: '#f3f4f6' },
    dmg:  { fa: 'fa-compact-disc',    color: '#4b5563', bg: '#f3f4f6' },
    iso:  { fa: 'fa-compact-disc',    color: '#4b5563', bg: '#f3f4f6' },
    apk:  { fa: 'fa-mobile-screen',   color: '#16a34a', bg: '#dcfce7' },
    nsp:  { fa: 'fa-gamepad',         color: '#dc2626', bg: '#fee2e2' },
    xci:  { fa: 'fa-gamepad',         color: '#dc2626', bg: '#fee2e2' },
    rom:  { fa: 'fa-gamepad',         color: '#dc2626', bg: '#fee2e2' },
  };
  const DEFAULT_TYPE = { fa: 'fa-file', color: '#64748b', bg: '#f1f5f9' };

  function getType(name) {
    const ext = (name || '').split('.').pop().toLowerCase();
    return FILE_TYPES[ext] || DEFAULT_TYPE;
  }
  function getExt(name) {
    return (name || '').split('.').pop().toUpperCase() || 'FILE';
  }
  function fmtBytes(n) {
    if (!n) return '0 B';
    if (n >= 1e9) return (n / 1e9).toFixed(2) + ' GB';
    if (n >= 1e6) return (n / 1e6).toFixed(1) + ' MB';
    if (n >= 1e3) return (n / 1e3).toFixed(0) + ' KB';
    return n + ' B';
  }
  function fmtDate(iso) {
    if (!iso) return '—';
    return new Date(iso).toLocaleDateString('en-US', { year: 'numeric', month: 'short', day: 'numeric' });
  }

  // ── Build a file card ─────────────────────────────────────────────────────
  function buildCard(f) {
    const type    = getType(f.filename);
    const ext     = getExt(f.filename);
    const shareUrl = `${location.origin}/f/${f.share_id}`;

    const card = document.createElement('div');
    card.className = 'br-card';
    card.dataset.name = f.filename.toLowerCase();
    card.dataset.ext  = ext.toLowerCase();

    card.innerHTML = `
      <div class="br-card-icon" style="background:${type.bg};color:${type.color}">
        <i class="fa-solid ${type.fa}"></i>
      </div>
      <div class="br-card-info">
        <div class="br-card-name" title="${f.filename}">${f.filename}</div>
        <div class="br-card-meta">
          <span class="br-ext-badge">${ext}</span>
          <span class="br-card-size">${fmtBytes(f.file_size)}</span>
          <span class="br-card-date"><i class="fa-regular fa-calendar"></i> ${fmtDate(f.completed_at)}</span>
          <span class="br-card-dl"><i class="fa-solid fa-download"></i> ${(f.downloads || 0).toLocaleString()}</span>
        </div>
      </div>
      <div class="br-card-actions">
        <a href="${shareUrl}" class="br-btn-download" target="_blank">
          <i class="fa-solid fa-arrow-down"></i> Download
        </a>
        <button class="br-btn-copy" title="Copy share link" data-url="${shareUrl}">
          <i class="fa-solid fa-link"></i>
        </button>
      </div>`;

    card.querySelector('.br-btn-copy').addEventListener('click', function () {
      const url = this.dataset.url;
      navigator.clipboard.writeText(url).catch(() => {
        const ta = document.createElement('textarea');
        ta.value = url; ta.style.position = 'fixed'; ta.style.opacity = '0';
        document.body.appendChild(ta); ta.select();
        document.execCommand('copy'); ta.remove();
      });
      const orig = this.innerHTML;
      this.innerHTML = '<i class="fa-solid fa-check"></i>';
      this.classList.add('br-btn-copied');
      setTimeout(() => { this.innerHTML = orig; this.classList.remove('br-btn-copied'); }, 2000);
    });

    return card;
  }

  // ── Main ──────────────────────────────────────────────────────────────────
  let allFiles = [];

  async function init() {
    try {
      allFiles = await fetch('/api/public/files').then(r => r.ok ? r.json() : Promise.reject());
    } catch {
      document.getElementById('br-loading').hidden = true;
      document.getElementById('br-empty').hidden = false;
      return;
    }

    // Stats
    const totalSize = allFiles.reduce((s, f) => s + (f.file_size || 0), 0);
    const totalDl   = allFiles.reduce((s, f) => s + (f.downloads || 0), 0);
    document.getElementById('br-count').textContent = allFiles.length.toLocaleString();
    document.getElementById('br-size').textContent  = fmtBytes(totalSize);
    document.getElementById('br-dl').textContent    = totalDl.toLocaleString();
    document.getElementById('br-stats').hidden = false;

    renderGrid(allFiles);
  }

  function renderGrid(files) {
    const grid = document.getElementById('br-grid');
    document.getElementById('br-loading').hidden    = true;
    document.getElementById('br-empty').hidden      = true;
    document.getElementById('br-no-results').hidden = true;

    if (!files.length) {
      document.getElementById(allFiles.length ? 'br-no-results' : 'br-empty').hidden = false;
      grid.hidden = true;
      return;
    }

    grid.innerHTML = '';
    files.forEach(f => grid.appendChild(buildCard(f)));
    grid.hidden = false;
  }

  // ── Search ────────────────────────────────────────────────────────────────
  document.getElementById('br-search').addEventListener('input', function () {
    const q = this.value.trim().toLowerCase();
    if (!q) { renderGrid(allFiles); return; }
    renderGrid(allFiles.filter(f =>
      f.filename.toLowerCase().includes(q) ||
      f.filename.split('.').pop().toLowerCase().includes(q)
    ));
  });

  init();
})();
