(function () {
  'use strict';

  const shareId = location.pathname.split('/').filter(Boolean).pop();

  // ── Theme toggle ──────────────────────────────────────────────────────────
  const body = document.body;
  const themeBtn  = document.getElementById('lp-theme-toggle');
  const themeIcon = themeBtn.querySelector('i');

  function applyTheme(dark) {
    body.classList.toggle('lp-dark', dark);
    themeIcon.className = dark ? 'fa-solid fa-sun' : 'fa-solid fa-moon';
  }

  applyTheme(localStorage.getItem('lp-theme') === 'dark');

  themeBtn.addEventListener('click', () => {
    const isDark = !body.classList.contains('lp-dark');
    localStorage.setItem('lp-theme', isDark ? 'dark' : 'light');
    applyTheme(isDark);
  });

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
    const [fileRes, adsRes, settingsRes] = await Promise.allSettled([
      fetch(`/api/f/${shareId}`).then(r => r.ok ? r.json() : Promise.reject()),
      fetch('/api/ads').then(r => r.ok ? r.json() : []),
      fetch('/api/settings').then(r => r.ok ? r.json() : {}),
    ]);

    const ads = (adsRes.status === 'fulfilled' && Array.isArray(adsRes.value)) ? adsRes.value : [];
    if (ads.length) renderAds(ads);

    if (fileRes.status !== 'fulfilled') {
      document.getElementById('lc-loading').hidden = true;
      document.getElementById('lc-error').hidden = false;
      return;
    }

    const settings   = (settingsRes.status === 'fulfilled' && settingsRes.value) ? settingsRes.value : {};
    const redirectUrl = settings.redirect_url || null;
    const popupUrl    = settings.popup_url    || null;

    const d = fileRes.value;
    const type = getType(d.filename);

    document.title = `DataDock – ${d.filename}`;

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
    document.getElementById('lp-dl-size').textContent = fmtBytes(d.file_size);

    // ── Download button — token-gated, no static href ─────────────────────
    const dlBtn = document.getElementById('lp-download-btn');

    function dlBtnState(icon, label, sub) {
      dlBtn.innerHTML =
        `<span class="lp-dl-icon"><i class="fa-solid ${icon}"></i></span>` +
        `<span class="lp-dl-text"><span class="lp-dl-primary">${label}</span>` +
        (sub ? `<span class="lp-dl-size">${sub}</span>` : '') +
        `</span>`;
    }

    const origHTML = dlBtn.innerHTML;

    async function triggerDownload() {
      dlBtn.disabled = true;
      dlBtnState('fa-spinner fa-spin', 'Preparing download…', '');
      try {
        const res = await fetch(`/api/f/${shareId}/token`, { method: 'POST' });
        if (!res.ok) throw new Error('Token error');
        const { token } = await res.json();
        location.href = `/api/f/${shareId}/download?token=${token}`;
      } catch {
        dlBtnState('fa-circle-exclamation', 'Try again', '');
        setTimeout(() => { dlBtn.innerHTML = origHTML; dlBtn.disabled = false; }, 2500);
        return;
      }
      setTimeout(() => { dlBtn.innerHTML = origHTML; dlBtn.disabled = false; }, 3000);
    }

    // ── Pop-up ad: first click ANYWHERE on the page opens the popup URL ─────
    let popupClickEvent = null; // the Event that triggered the popup
    if (popupUrl) {
      document.addEventListener('click', function popupHandler(e) {
        popupClickEvent = e;
        document.removeEventListener('click', popupHandler, true);
        window.open(popupUrl, '_blank', 'noopener,noreferrer');
      }, true); // capture phase — fires before any element handler
    }

    // ── Download button ───────────────────────────────────────────────────────
    let redirectDone = !redirectUrl;
    dlBtn.addEventListener('click', async e => {
      e.preventDefault();

      // If this click just fired the pop-up handler, don't also trigger download/redirect
      if (e === popupClickEvent) return;

      if (!redirectDone) {
        redirectDone = true;
        window.open(redirectUrl, '_blank', 'noopener,noreferrer');
        dlBtnState('fa-arrow-up-right-from-square', 'Click again to download', 'Ad opened in new tab');
        dlBtn.style.background = 'linear-gradient(135deg, #059669 0%, #10b981 100%)';
        return;
      }
      dlBtn.style.background = '';
      await triggerDownload();
    });

    // ── File preview ──────────────────────────────────────────────────────────
    const PREVIEW_IMAGES = new Set(['jpg','jpeg','png','gif','webp','svg','bmp','ico','avif']);
    const PREVIEW_VIDEO  = new Set(['mp4','webm','mov']);
    const PREVIEW_AUDIO  = new Set(['mp3','wav','ogg','flac','aac','m4a','opus']);
    const PREVIEW_PDF    = new Set(['pdf']);
    const PREVIEW_TEXT   = new Set(['txt','md','js','ts','jsx','tsx','py','rb','go','rs','java',
                                    'c','cpp','h','cs','php','sh','bash','html','css','json',
                                    'xml','yaml','yml','toml','ini','conf','log','csv','sql']);

    function getPreviewType(name) {
      const ext = (name || '').split('.').pop().toLowerCase();
      if (PREVIEW_IMAGES.has(ext)) return 'image';
      if (PREVIEW_VIDEO.has(ext))  return 'video';
      if (PREVIEW_AUDIO.has(ext))  return 'audio';
      if (PREVIEW_PDF.has(ext))    return 'pdf';
      if (PREVIEW_TEXT.has(ext))   return 'text';
      return null;
    }

    async function loadPreview(filename) {
      const type = getPreviewType(filename);
      if (!type) return;

      const card  = document.getElementById('lp-preview');
      const body  = document.getElementById('lp-preview-body');
      const label = document.getElementById('lp-preview-label');
      const previewUrl = `/api/f/${shareId}/preview`;

      label.textContent = type.toUpperCase();
      card.hidden = false;

      if (type === 'image') {
        const el = document.createElement('img');
        el.src = previewUrl;
        el.alt = filename;
        el.className = 'lp-preview-img';
        el.onload  = () => { body.innerHTML = ''; body.appendChild(el); };
        el.onerror = () => { body.innerHTML = '<p class="lp-preview-err"><i class="fa-solid fa-circle-exclamation"></i> Preview unavailable</p>'; };

      } else if (type === 'video') {
        const el = document.createElement('video');
        el.src = previewUrl;
        el.controls = true;
        el.preload = 'metadata';
        el.className = 'lp-preview-video';
        el.onerror = () => { body.innerHTML = '<p class="lp-preview-err"><i class="fa-solid fa-circle-exclamation"></i> Preview unavailable for this video format</p>'; };
        body.innerHTML = ''; body.appendChild(el);

      } else if (type === 'audio') {
        const el = document.createElement('audio');
        el.src = previewUrl;
        el.controls = true;
        el.preload = 'metadata';
        el.className = 'lp-preview-audio';
        el.onerror = () => { body.innerHTML = '<p class="lp-preview-err"><i class="fa-solid fa-circle-exclamation"></i> Preview unavailable for this audio format</p>'; };
        body.innerHTML = ''; body.appendChild(el);

      } else if (type === 'pdf') {
        const el = document.createElement('iframe');
        el.src = previewUrl;
        el.className = 'lp-preview-pdf';
        el.title = filename;
        body.innerHTML = ''; body.appendChild(el);

      } else if (type === 'text') {
        try {
          const res = await fetch(`/api/f/${shareId}/preview-text`);
          if (!res.ok) throw new Error();
          const text = await res.text();
          const truncated = res.headers.get('X-Preview-Truncated') === '1';

          const pre = document.createElement('pre');
          pre.className = 'lp-preview-text';
          pre.textContent = text;

          const wrap = document.createElement('div');
          wrap.appendChild(pre);
          if (truncated) {
            const note = document.createElement('p');
            note.className = 'lp-preview-truncated';
            note.innerHTML = '<i class="fa-solid fa-circle-info"></i> Showing first 50 KB — download to see the full file.';
            wrap.appendChild(note);
          }
          body.innerHTML = ''; body.appendChild(wrap);
        } catch {
          body.innerHTML = '<p class="lp-preview-err"><i class="fa-solid fa-circle-exclamation"></i> Could not load preview</p>';
        }
      }
    }

    loadPreview(d.filename);

    // ── Copy share link button ─────────────────────────────────────────────
    const copyBtn = document.getElementById('lp-copy-btn');
    copyBtn.addEventListener('click', async () => {
      const url = location.href;
      try {
        await navigator.clipboard.writeText(url);
      } catch {
        const ta = document.createElement('textarea');
        ta.value = url; ta.style.position = 'fixed'; ta.style.opacity = '0';
        document.body.appendChild(ta); ta.select();
        document.execCommand('copy'); ta.remove();
      }
      copyBtn.classList.add('copied');
      copyBtn.querySelector('i').className = 'fa-solid fa-check';
      copyBtn.querySelector('span').textContent = 'Link Copied!';
      setTimeout(() => {
        copyBtn.classList.remove('copied');
        copyBtn.querySelector('i').className = 'fa-solid fa-link';
        copyBtn.querySelector('span').textContent = 'Copy Share Link';
      }, 2500);
    });

    document.getElementById('lc-loading').hidden = true;
    document.getElementById('lc-card').hidden = false;
    document.getElementById('lp-features').hidden = false;
  }

  init();
})();
