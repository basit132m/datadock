(function () {
  'use strict';

  // ── Theme toggle ──────────────────────────────────────────────────────────
  const body     = document.body;
  const themeBtn  = document.getElementById('rq-theme-toggle');
  const themeIcon = themeBtn.querySelector('i');

  function applyTheme(dark) {
    body.classList.toggle('rq-dark', dark);
    themeIcon.className = dark ? 'fa-solid fa-sun' : 'fa-solid fa-moon';
  }
  applyTheme(localStorage.getItem('lp-theme') === 'dark');
  themeBtn.addEventListener('click', () => {
    const isDark = !body.classList.contains('rq-dark');
    localStorage.setItem('lp-theme', isDark ? 'dark' : 'light');
    applyTheme(isDark);
  });

  // ── Form submit ───────────────────────────────────────────────────────────
  const form   = document.getElementById('rq-form');
  const errEl  = document.getElementById('rq-error');
  const btnEl  = document.getElementById('rq-submit');
  const btnInner = document.getElementById('rq-submit-inner');

  function showError(msg) {
    errEl.textContent = msg;
    errEl.hidden = false;
  }
  function clearError() {
    errEl.hidden = true;
    errEl.textContent = '';
  }
  function setLoading(on) {
    btnEl.disabled = on;
    btnInner.innerHTML = on
      ? '<i class="fa-solid fa-circle-notch fa-spin"></i> Submitting…'
      : '<i class="fa-solid fa-paper-plane"></i> Submit Request';
  }

  form.addEventListener('submit', async e => {
    e.preventDefault();
    clearError();

    const name    = document.getElementById('rq-name').value.trim();
    const email   = document.getElementById('rq-email').value.trim();
    const reason  = document.getElementById('rq-reason').value.trim();
    const website = document.getElementById('rq-website').value; // honeypot

    if (!name)  return showError('Please enter your full name.');
    if (!email) return showError('Please enter your email address.');
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) return showError('Please enter a valid email address.');

    setLoading(true);
    try {
      const res = await fetch('/api/access-requests', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, email, reason: reason || null, website: website || null }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Submission failed.');

      document.getElementById('rq-success-name').textContent  = name;
      document.getElementById('rq-success-email').textContent = email;
      document.getElementById('rq-card').hidden    = true;
      document.getElementById('rq-success').hidden = false;
    } catch (err) {
      showError(err.message);
      setLoading(false);
    }
  });
})();
