(function () {
  'use strict';

  const shareId = location.pathname.split('/').filter(Boolean).pop();

  // ── File type map ──────────────────────────────────────────────────────────
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
    md:   { fa: 'fa-file-lines',      color: '#64748b', bg: '#f1f5f9' },
    js:   { fa: 'fa-file-code',       color: '#6366f1', bg: '#eef2ff' },
    ts:   { fa: 'fa-file-code',       color: '#6366f1', bg: '#eef2ff' },
    py:   { fa: 'fa-file-code',       color: '#6366f1', bg: '#eef2ff' },
    html: { fa: 'fa-file-code',       color: '#6366f1', bg: '#eef2ff' },
    css:  { fa: 'fa-file-code',       color: '#6366f1', bg: '#eef2ff' },
    json: { fa: 'fa-file-code',       color: '#6366f1', bg: '#eef2ff' },
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

  // ── Helpers ────────────────────────────────────────────────────────────────
  function fmtBytes(n) {
    if (!n) return '0 B';
    if (n >= 1e9) return (n / 1e9).toFixed(2) + ' GB';
    if (n >= 1e6) return (n / 1e6).toFixed(1) + ' MB';
    if (n >= 1e3) return (n / 1e3).toFixed(0) + ' KB';
    return n + ' B';
  }

  function fmtDate(iso) {
    if (!iso) return 'Unknown';
    return new Date(iso).toLocaleDateString('en-US', { year: 'numeric', month: 'short', day: 'numeric' });
  }

  function extLabel(name) {
    const ext = (name || '').split('.').pop().toUpperCase();
    return ext || 'FILE';
  }

  // ── Ad rendering ──────────────────────────────────────────────────────────
  function renderAds(ads) {
    const banners = ads.filter(a => a.type === 'banner');
    const buttons = ads.filter(a => a.type === 'button');
    const mid = Math.ceil(banners.length / 2);

    banners.slice(0, mid).forEach(ad => document.getElementById('ads-top').appendChild(makeBanner(ad)));
    banners.slice(mid).forEach(ad => document.getElementById('ads-bottom').appendChild(makeBanner(ad)));
    buttons.forEach(ad => document.getElementById('ads-buttons').appendChild(makeAdBtn(ad)));
  }

  function makeBanner(ad) {
    const a = document.createElement('a');
    a.href = ad.link_url;
    a.target = '_blank';
    a.rel = 'noopener noreferrer';
    a.className = 'ad-banner';
    if (ad.image_url) {
      const img = document.createElement('img');
      img.src = ad.image_url;
      img.alt = ad.label;
      img.loading = 'lazy';
      a.appendChild(img);
    } else {
      a.textContent = ad.label;
      a.classList.add('ad-banner-text');
    }
    return a;
  }

  function makeAdBtn(ad) {
    const a = document.createElement('a');
    a.href = ad.link_url;
    a.target = '_blank';
    a.rel = 'noopener noreferrer';
    a.className = 'ad-btn';
    a.innerHTML = `<i class="fa-solid fa-arrow-up-right-from-square"></i> ${ad.label}`;
    return a;
  }

  // ── Main init ──────────────────────────────────────────────────────────────
  async function init() {
    const [fileRes, adsRes] = await Promise.allSettled([
      fetch(`/api/f/${shareId}`).then(r => r.ok ? r.json() : Promise.reject()),
      fetch('/api/ads').then(r => r.ok ? r.json() : []),
    ]);

    if (adsRes.status === 'fulfilled' && Array.isArray(adsRes.value)) {
      renderAds(adsRes.value);
    }

    if (fileRes.status !== 'fulfilled') {
      document.getElementById('lc-loading').hidden = true;
      document.getElementById('lc-error').hidden = false;
      return;
    }

    const d = fileRes.value;
    const type = getType(d.filename);

    document.title = `DataDock – ${d.filename}`;

    // Icon circle
    const iconEl = document.getElementById('lp-file-icon');
    iconEl.className = `fa-solid ${type.fa} lp-file-icon`;
    const wrapEl = document.getElementById('lp-icon-wrap');
    wrapEl.style.background = type.bg;
    wrapEl.style.color = type.color;

    document.getElementById('lp-filename').textContent = d.filename;
    document.getElementById('lp-size').textContent = fmtBytes(d.file_size);
    document.getElementById('lp-date').textContent = fmtDate(d.completed_at);
    document.getElementById('lp-type').textContent = extLabel(d.filename);
    document.getElementById('lp-views').textContent = (d.views || 0).toLocaleString();
    document.getElementById('lp-downloads').textContent = (d.downloads || 0).toLocaleString();
    document.getElementById('lp-download-btn').href = `/api/f/${shareId}/download`;

    document.getElementById('lc-loading').hidden = true;
    document.getElementById('lc-card').hidden = false;
  }

  init();
})();
