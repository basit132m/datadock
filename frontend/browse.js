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
  applyTheme(localStorage.getItem('lp-theme') === 'dark');
  themeBtn.addEventListener('click', () => {
    const isDark = !body.classList.contains('br-dark');
    localStorage.setItem('lp-theme', isDark ? 'dark' : 'light');
    applyTheme(isDark);
  });

  // ── File type map ─────────────────────────────────────────────────────────
  const FILE_TYPES = {
    zip:  { fa: 'fa-file-zipper',     color: '#f59e0b', bg: '#fef3c7' },
    rar:  { fa: 'fa-file-zipper',     color: '#f59e0b', bg: '#fef3c7' },
    gz:   { fa: 'fa-file-zipper',     color: '#f59e0b', bg: '#fef3c7' },
    tar:  { fa: 'fa-file-zipper',     color: '#f59e0b', bg: '#fef3c7' },
    '7z': { fa: 'fa-file-zipper',     color: '#f59e0b', bg: '#fef3c7' },
    bz2:  { fa: 'fa-file-zipper',     color: '#f59e0b', bg: '#fef3c7' },
    mp4:  { fa: 'fa-file-video',      color: '#ef4444', bg: '#fee2e2' },
    mkv:  { fa: 'fa-file-video',      color: '#ef4444', bg: '#fee2e2' },
    avi:  { fa: 'fa-file-video',      color: '#ef4444', bg: '#fee2e2' },
    mov:  { fa: 'fa-file-video',      color: '#ef4444', bg: '#fee2e2' },
    webm: { fa: 'fa-file-video',      color: '#ef4444', bg: '#fee2e2' },
    mp3:  { fa: 'fa-file-audio',      color: '#8b5cf6', bg: '#ede9fe' },
    wav:  { fa: 'fa-file-audio',      color: '#8b5cf6', bg: '#ede9fe' },
    flac: { fa: 'fa-file-audio',      color: '#8b5cf6', bg: '#ede9fe' },
    aac:  { fa: 'fa-file-audio',      color: '#8b5cf6', bg: '#ede9fe' },
    ogg:  { fa: 'fa-file-audio',      color: '#8b5cf6', bg: '#ede9fe' },
    jpg:  { fa: 'fa-file-image',      color: '#0ea5e9', bg: '#e0f2fe' },
    jpeg: { fa: 'fa-file-image',      color: '#0ea5e9', bg: '#e0f2fe' },
    png:  { fa: 'fa-file-image',      color: '#0ea5e9', bg: '#e0f2fe' },
    gif:  { fa: 'fa-file-image',      color: '#0ea5e9', bg: '#e0f2fe' },
    webp: { fa: 'fa-file-image',      color: '#0ea5e9', bg: '#e0f2fe' },
    svg:  { fa: 'fa-file-image',      color: '#0ea5e9', bg: '#e0f2fe' },
    bmp:  { fa: 'fa-file-image',      color: '#0ea5e9', bg: '#e0f2fe' },
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
    pkg:  { fa: 'fa-gamepad',         color: '#dc2626', bg: '#fee2e2' },
  };
  const DEFAULT_TYPE = { fa: 'fa-file', color: '#64748b', bg: '#f1f5f9' };

  // ── Category map ──────────────────────────────────────────────────────────
  const CAT_EXTS = {
    video:    new Set(['mp4','mkv','avi','mov','webm']),
    audio:    new Set(['mp3','wav','flac','aac','ogg']),
    images:   new Set(['jpg','jpeg','png','gif','webp','svg','bmp']),
    archives: new Set(['zip','rar','gz','tar','7z','bz2']),
    docs:     new Set(['pdf','doc','docx','xls','xlsx','csv','ppt','pptx','txt']),
    software: new Set(['exe','msi','dmg','iso','apk']),
    games:    new Set(['nsp','xci','rom','pkg']),
  };
  const CAT_LABELS = {
    all:      { label: 'All',      icon: 'fa-layer-group' },
    video:    { label: 'Video',    icon: 'fa-film' },
    audio:    { label: 'Audio',    icon: 'fa-music' },
    images:   { label: 'Images',   icon: 'fa-image' },
    archives: { label: 'Archives', icon: 'fa-file-zipper' },
    docs:     { label: 'Docs',     icon: 'fa-file-lines' },
    software: { label: 'Software', icon: 'fa-gear' },
    games:    { label: 'Games',    icon: 'fa-gamepad' },
    other:    { label: 'Other',    icon: 'fa-file' },
  };

  function getExt(name) { return (name || '').split('.').pop().toLowerCase(); }
  function getCat(name) {
    const ext = getExt(name);
    for (const [cat, exts] of Object.entries(CAT_EXTS)) {
      if (exts.has(ext)) return cat;
    }
    return 'other';
  }
  function getType(name) { return FILE_TYPES[getExt(name)] || DEFAULT_TYPE; }
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

  // ── State ─────────────────────────────────────────────────────────────────
  const PAGE_SIZE = 60;
  const state = { search: '', category: 'all', sort: 'newest', page: 1, view: 'grid' };
  let allFiles = [];
  let filtered = [];

  // ── Sort & filter pipeline ────────────────────────────────────────────────
  const SORTERS = {
    newest:   (a, b) => new Date(b.completed_at) - new Date(a.completed_at),
    oldest:   (a, b) => new Date(a.completed_at) - new Date(b.completed_at),
    'name-az':(a, b) => a.filename.localeCompare(b.filename),
    'name-za':(a, b) => b.filename.localeCompare(a.filename),
    largest:  (a, b) => (b.file_size || 0) - (a.file_size || 0),
    smallest: (a, b) => (a.file_size || 0) - (b.file_size || 0),
  };

  function applyFilters() {
    const q   = state.search.toLowerCase();
    const cat = state.category;
    filtered = allFiles.filter(f => {
      if (q && !f.filename.toLowerCase().includes(q) && !getExt(f.filename).includes(q)) return false;
      if (cat !== 'all' && getCat(f.filename) !== cat) return false;
      return true;
    });
    filtered.sort(SORTERS[state.sort] || SORTERS.newest);
    state.page = 1;
    renderPage();
  }

  // ── Escape helper ─────────────────────────────────────────────────────────
  function esc(str) {
    return String(str || '').replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  // ── Copy helper ───────────────────────────────────────────────────────────
  function addCopy(btn, url) {
    btn.addEventListener('click', function () {
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
  }

  // ── Card builders ─────────────────────────────────────────────────────────
  function buildGridCard(f) {
    const type     = getType(f.filename);
    const ext      = getExt(f.filename).toUpperCase() || 'FILE';
    const shareUrl = `${location.origin}/f/${f.share_id}`;
    const card     = document.createElement('div');
    card.className = 'br-gc';
    card.innerHTML = `
      <div class="br-gc-icon" style="background:${type.bg};color:${type.color}">
        <i class="fa-solid ${type.fa}"></i>
      </div>
      <div class="br-gc-body">
        <div class="br-gc-name" title="${esc(f.filename)}">${esc(f.filename)}</div>
        <div class="br-gc-meta">
          <span class="br-ext-badge">${ext}</span>
          <span class="br-gc-size">${fmtBytes(f.file_size)}</span>
        </div>
      </div>
      <div class="br-gc-actions">
        <a href="${shareUrl}" class="br-btn-download" target="_blank" title="Download">
          <i class="fa-solid fa-arrow-down"></i>
        </a>
        <button class="br-btn-copy" title="Copy share link" data-url="${shareUrl}">
          <i class="fa-solid fa-link"></i>
        </button>
      </div>`;
    addCopy(card.querySelector('.br-btn-copy'), shareUrl);
    return card;
  }

  function buildListCard(f) {
    const type     = getType(f.filename);
    const ext      = getExt(f.filename).toUpperCase() || 'FILE';
    const shareUrl = `${location.origin}/f/${f.share_id}`;
    const card     = document.createElement('div');
    card.className = 'br-card';
    card.innerHTML = `
      <div class="br-card-icon" style="background:${type.bg};color:${type.color}">
        <i class="fa-solid ${type.fa}"></i>
      </div>
      <div class="br-card-info">
        <div class="br-card-name" title="${esc(f.filename)}">${esc(f.filename)}</div>
        <div class="br-card-meta">
          <span class="br-ext-badge">${ext}</span>
          <span class="br-card-size">${fmtBytes(f.file_size)}</span>
          <span class="br-card-date"><i class="fa-regular fa-calendar"></i> ${fmtDate(f.completed_at)}</span>
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
    addCopy(card.querySelector('.br-btn-copy'), shareUrl);
    return card;
  }

  // ── Render page ───────────────────────────────────────────────────────────
  const grid   = document.getElementById('br-grid');
  const pgEl   = document.getElementById('br-pagination');
  const resBar = document.getElementById('br-results-bar');

  function renderPage() {
    document.getElementById('br-loading').hidden    = true;
    document.getElementById('br-empty').hidden      = true;
    document.getElementById('br-no-results').hidden = true;

    if (!filtered.length) {
      grid.hidden = true;
      pgEl.hidden = true;
      resBar.textContent = '';
      document.getElementById(allFiles.length ? 'br-no-results' : 'br-empty').hidden = false;
      return;
    }

    const total = filtered.length;
    const pages = Math.ceil(total / PAGE_SIZE);
    const p     = Math.min(state.page, pages);
    const start = (p - 1) * PAGE_SIZE;
    const end   = Math.min(start + PAGE_SIZE, total);
    const slice = filtered.slice(start, end);

    grid.className = state.view === 'grid' ? 'br-grid br-grid-mode' : 'br-grid br-list-mode';
    grid.innerHTML = '';
    const builder = state.view === 'grid' ? buildGridCard : buildListCard;
    slice.forEach(f => grid.appendChild(builder(f)));
    grid.hidden = false;

    resBar.textContent = `Showing ${(start + 1).toLocaleString()}–${end.toLocaleString()} of ${total.toLocaleString()} file${total !== 1 ? 's' : ''}`;

    renderPagination(p, pages);
  }

  // ── Pagination ────────────────────────────────────────────────────────────
  function paginationRange(cur, total) {
    if (total <= 7) return Array.from({ length: total }, (_, i) => i + 1);
    const left  = Math.max(2, cur - 2);
    const right = Math.min(total - 1, cur + 2);
    const range = [1];
    if (left > 2) range.push('…');
    for (let i = left; i <= right; i++) range.push(i);
    if (right < total - 1) range.push('…');
    range.push(total);
    return range;
  }

  function renderPagination(cur, total) {
    if (total <= 1) { pgEl.hidden = true; return; }
    pgEl.hidden = false;
    pgEl.innerHTML = '';

    const prev = document.createElement('button');
    prev.className = 'br-page-btn';
    prev.innerHTML = '<i class="fa-solid fa-chevron-left"></i>';
    prev.disabled  = cur === 1;
    prev.addEventListener('click', () => { state.page = cur - 1; renderPage(); scrollTo(0, 0); });
    pgEl.appendChild(prev);

    paginationRange(cur, total).forEach(p => {
      if (p === '…') {
        const sp = document.createElement('span');
        sp.className = 'br-page-ellipsis'; sp.textContent = '…';
        pgEl.appendChild(sp);
      } else {
        const btn = document.createElement('button');
        btn.className = 'br-page-btn' + (p === cur ? ' br-page-active' : '');
        btn.textContent = p;
        btn.addEventListener('click', () => { state.page = p; renderPage(); scrollTo(0, 0); });
        pgEl.appendChild(btn);
      }
    });

    const next = document.createElement('button');
    next.className = 'br-page-btn';
    next.innerHTML = '<i class="fa-solid fa-chevron-right"></i>';
    next.disabled  = cur === total;
    next.addEventListener('click', () => { state.page = cur + 1; renderPage(); scrollTo(0, 0); });
    pgEl.appendChild(next);
  }

  // ── Category tabs ─────────────────────────────────────────────────────────
  function buildTabs(files) {
    const available = new Set(files.map(f => getCat(f.filename)));
    const tabsEl    = document.getElementById('br-tabs');
    tabsEl.innerHTML = '';

    const cats = ['all', ...Object.keys(CAT_EXTS).filter(c => available.has(c))];
    if (available.has('other')) cats.push('other');

    cats.forEach(cat => {
      const meta  = CAT_LABELS[cat] || { label: 'Other', icon: 'fa-file' };
      const count = cat === 'all' ? files.length : files.filter(f => getCat(f.filename) === cat).length;
      const btn   = document.createElement('button');
      btn.className  = 'br-tab' + (cat === state.category ? ' br-tab-active' : '');
      btn.dataset.cat = cat;
      btn.setAttribute('role', 'tab');
      btn.innerHTML = `<i class="fa-solid ${meta.icon}"></i> ${meta.label} <span class="br-tab-count">${count.toLocaleString()}</span>`;
      btn.addEventListener('click', () => {
        state.category = cat;
        tabsEl.querySelectorAll('.br-tab').forEach(b => b.classList.toggle('br-tab-active', b.dataset.cat === cat));
        applyFilters();
      });
      tabsEl.appendChild(btn);
    });
  }

  // ── View toggle ───────────────────────────────────────────────────────────
  document.getElementById('br-view-grid').addEventListener('click', function () {
    if (state.view === 'grid') return;
    state.view = 'grid';
    this.classList.add('br-view-active');
    document.getElementById('br-view-list').classList.remove('br-view-active');
    renderPage();
  });
  document.getElementById('br-view-list').addEventListener('click', function () {
    if (state.view === 'list') return;
    state.view = 'list';
    this.classList.add('br-view-active');
    document.getElementById('br-view-grid').classList.remove('br-view-active');
    renderPage();
  });

  // ── Sort ──────────────────────────────────────────────────────────────────
  document.getElementById('br-sort').addEventListener('change', function () {
    state.sort = this.value;
    applyFilters();
  });

  // ── Search (debounced) ────────────────────────────────────────────────────
  let searchTimer;
  document.getElementById('br-search').addEventListener('input', function () {
    clearTimeout(searchTimer);
    const q = this.value.trim();
    searchTimer = setTimeout(() => { state.search = q; applyFilters(); }, 200);
  });

  // ── Init ──────────────────────────────────────────────────────────────────
  async function init() {
    try {
      allFiles = await fetch('/api/public/files').then(r => r.ok ? r.json() : Promise.reject());
    } catch {
      document.getElementById('br-loading').hidden = true;
      document.getElementById('br-empty').hidden   = false;
      return;
    }

    const totalSize = allFiles.reduce((s, f) => s + (f.file_size || 0), 0);
    const totalDl   = allFiles.reduce((s, f) => s + (f.downloads  || 0), 0);
    document.getElementById('br-count').textContent = allFiles.length.toLocaleString();
    document.getElementById('br-size').textContent  = fmtBytes(totalSize);
    document.getElementById('br-dl').textContent    = totalDl.toLocaleString();
    document.getElementById('br-stats').hidden      = false;

    buildTabs(allFiles);
    document.getElementById('br-toolbar').hidden = false;
    applyFilters();
  }

  init();
})();
