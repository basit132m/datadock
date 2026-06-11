(function () {
  'use strict';

  // ── Theme ─────────────────────────────────────────────────────────────────
  const body     = document.body;
  const themeBtn  = document.getElementById('hp-theme-toggle');
  const themeIcon = themeBtn.querySelector('i');

  function applyTheme(dark) {
    body.classList.toggle('hp-dark', dark);
    themeIcon.className = dark ? 'fa-solid fa-sun' : 'fa-solid fa-moon';
  }
  applyTheme(localStorage.getItem('lp-theme') === 'dark');
  themeBtn.addEventListener('click', () => {
    const isDark = !body.classList.contains('hp-dark');
    localStorage.setItem('lp-theme', isDark ? 'dark' : 'light');
    applyTheme(isDark);
  });

  // ── Header scroll shadow ──────────────────────────────────────────────────
  const header = document.getElementById('hp-header');
  window.addEventListener('scroll', () => {
    header.classList.toggle('hp-header-scrolled', window.scrollY > 10);
  }, { passive: true });

  // ── Helpers ───────────────────────────────────────────────────────────────
  function fmtBytes(n) {
    if (!n) return '0 B';
    if (n >= 1e12) return (n / 1e12).toFixed(1) + ' TB';
    if (n >= 1e9)  return (n / 1e9).toFixed(1) + ' GB';
    if (n >= 1e6)  return (n / 1e6).toFixed(1) + ' MB';
    if (n >= 1e3)  return (n / 1e3).toFixed(0) + ' KB';
    return n + ' B';
  }
  function fmtNum(n) {
    if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
    if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K';
    return n.toLocaleString();
  }

  // ── Animated counter ──────────────────────────────────────────────────────
  function animateCount(el, target, formatter) {
    const duration = 1200;
    const start    = performance.now();
    function step(now) {
      const t   = Math.min((now - start) / duration, 1);
      const val = Math.round(easeOut(t) * target);
      el.textContent = formatter(val);
      if (t < 1) requestAnimationFrame(step);
    }
    requestAnimationFrame(step);
  }
  function easeOut(t) { return 1 - Math.pow(1 - t, 3); }

  // ── File type map (for preview cards) ────────────────────────────────────
  const FILE_TYPES = {
    zip:  { fa: 'fa-file-zipper',     color: '#f59e0b', bg: '#fef3c7' },
    rar:  { fa: 'fa-file-zipper',     color: '#f59e0b', bg: '#fef3c7' },
    gz:   { fa: 'fa-file-zipper',     color: '#f59e0b', bg: '#fef3c7' },
    tar:  { fa: 'fa-file-zipper',     color: '#f59e0b', bg: '#fef3c7' },
    '7z': { fa: 'fa-file-zipper',     color: '#f59e0b', bg: '#fef3c7' },
    mp4:  { fa: 'fa-file-video',      color: '#ef4444', bg: '#fee2e2' },
    mkv:  { fa: 'fa-file-video',      color: '#ef4444', bg: '#fee2e2' },
    avi:  { fa: 'fa-file-video',      color: '#ef4444', bg: '#fee2e2' },
    mp3:  { fa: 'fa-file-audio',      color: '#8b5cf6', bg: '#ede9fe' },
    wav:  { fa: 'fa-file-audio',      color: '#8b5cf6', bg: '#ede9fe' },
    flac: { fa: 'fa-file-audio',      color: '#8b5cf6', bg: '#ede9fe' },
    jpg:  { fa: 'fa-file-image',      color: '#0ea5e9', bg: '#e0f2fe' },
    png:  { fa: 'fa-file-image',      color: '#0ea5e9', bg: '#e0f2fe' },
    gif:  { fa: 'fa-file-image',      color: '#0ea5e9', bg: '#e0f2fe' },
    webp: { fa: 'fa-file-image',      color: '#0ea5e9', bg: '#e0f2fe' },
    pdf:  { fa: 'fa-file-pdf',        color: '#dc2626', bg: '#fee2e2' },
    doc:  { fa: 'fa-file-word',       color: '#2563eb', bg: '#dbeafe' },
    docx: { fa: 'fa-file-word',       color: '#2563eb', bg: '#dbeafe' },
    exe:  { fa: 'fa-gear',            color: '#4b5563', bg: '#f3f4f6' },
    iso:  { fa: 'fa-compact-disc',    color: '#4b5563', bg: '#f3f4f6' },
    nsp:  { fa: 'fa-gamepad',         color: '#dc2626', bg: '#fee2e2' },
    xci:  { fa: 'fa-gamepad',         color: '#dc2626', bg: '#fee2e2' },
  };
  const DEFAULT_TYPE = { fa: 'fa-file', color: '#64748b', bg: '#f1f5f9' };

  function getType(name) {
    const ext = (name || '').split('.').pop().toLowerCase();
    return FILE_TYPES[ext] || DEFAULT_TYPE;
  }
  function getExt(name) {
    return (name || '').split('.').pop().toUpperCase() || 'FILE';
  }

  // ── Load public files ─────────────────────────────────────────────────────
  async function init() {
    let files = [];
    try {
      files = await fetch('/api/public/files').then(r => r.ok ? r.json() : Promise.reject());
    } catch {
      document.getElementById('hp-preview-loading').hidden = true;
      document.getElementById('hp-preview-empty').hidden   = false;
      return;
    }

    // Stats
    if (files.length) {
      const totalSize = files.reduce((s, f) => s + (f.file_size || 0), 0);
      const statsEl   = document.getElementById('hp-stats');

      document.getElementById('hp-stat-files').textContent = fmtNum(files.length);
      document.getElementById('hp-stat-size').textContent  = fmtBytes(totalSize);
      statsEl.hidden = false;

      // Animate stats when visible
      const observer = new IntersectionObserver(entries => {
        if (entries[0].isIntersecting) {
          animateCount(document.getElementById('hp-stat-files'), files.length, fmtNum);
          observer.disconnect();
        }
      }, { threshold: 0.5 });
      observer.observe(statsEl);
    }

    // Preview grid — latest 8 files
    const preview = files.slice(0, 8);
    if (!preview.length) {
      document.getElementById('hp-preview-loading').hidden = true;
      document.getElementById('hp-preview-empty').hidden   = false;
      return;
    }

    const grid = document.getElementById('hp-preview-grid');
    preview.forEach(f => grid.appendChild(buildPreviewCard(f)));
    document.getElementById('hp-preview-loading').hidden = true;
    grid.hidden = false;

    // Reveal cards with stagger
    grid.querySelectorAll('.hp-pcard').forEach((el, i) => {
      el.style.animationDelay = `${i * 50}ms`;
      el.classList.add('hp-pcard-in');
    });
  }

  function esc(str) {
    return String(str || '').replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function buildPreviewCard(f) {
    const type     = getType(f.filename);
    const ext      = getExt(f.filename);
    const shareUrl = `${location.origin}/f/${f.share_id}`;
    const card     = document.createElement('a');
    card.className = 'hp-pcard';
    card.href      = shareUrl;
    card.target    = '_blank';
    card.rel       = 'noopener';
    card.innerHTML = `
      <div class="hp-pcard-icon" style="background:${type.bg};color:${type.color}">
        <i class="fa-solid ${type.fa}"></i>
      </div>
      <div class="hp-pcard-body">
        <div class="hp-pcard-name" title="${esc(f.filename)}">${esc(f.filename)}</div>
        <div class="hp-pcard-meta">
          <span class="hp-pcard-ext">${ext}</span>
          <span class="hp-pcard-size">${fmtBytes(f.file_size)}</span>
        </div>
      </div>
      <div class="hp-pcard-dl"><i class="fa-solid fa-arrow-down"></i></div>`;
    return card;
  }

  init();
})();
