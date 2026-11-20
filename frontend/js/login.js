/* Sign-in page for the perimeter dashboard. */
document.getElementById('login-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const btn = document.getElementById('login-btn');
  const err = document.getElementById('login-error');
  const user = document.getElementById('login-user').value.trim();
  const pass = document.getElementById('login-pass').value;
  btn.disabled = true;
  btn.textContent = 'Signing in…';
  err.style.display = 'none';
  try {
    const r = await fetch('/api/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({ email: user, password: pass }),
    });
    if (r.ok) {
      window.location.replace('index.html');
      return;
    }
    const data = await r.json().catch(() => ({}));
    err.textContent = r.status === 429
      ? (data.detail || 'Too many failed sign-ins — try again in 15 minutes')
      : 'Wrong user ID or password.';
  } catch (_) {
    err.textContent = 'Cannot reach the server.';
  }
  err.style.display = 'block';
  btn.disabled = false;
  btn.textContent = 'Sign In →';
});
