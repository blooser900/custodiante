/*
 * Shared sign-in for the manager pages (dashboard and rules).
 *
 * The pages themselves hold no data: everything sensitive comes from the
 * token-protected API, so this script only has to collect the token, attach
 * it to requests, and ask again when the server says it is wrong.
 *
 * The token lives in sessionStorage: it disappears when the tab closes and
 * is not shared with other tabs, which suits a shared-secret alpha.
 */
(function () {
  const KEY = 'custodiante_manager_token';
  let overlay = null;

  const token = () => sessionStorage.getItem(KEY) || '';

  function showLogin(message) {
    const fresh = !overlay;
    if (!overlay) {
      overlay = document.createElement('div');
      overlay.style.cssText =
        'position:fixed;inset:0;z-index:1000;display:flex;align-items:center;' +
        'justify-content:center;padding:1rem;background:var(--paper,#EEF2EA);';
      overlay.innerHTML = `
        <form id="maForm" style="width:100%;max-width:22rem;font-family:'IBM Plex Sans',system-ui,sans-serif;color:var(--ink,#1E2A22);">
          <h1 style="font-family:'Source Serif 4',serif;font-size:1.5rem;font-weight:600;margin:0 0 0.35rem;">Manager sign-in</h1>
          <p style="margin:0 0 1.25rem;font-size:0.9rem;color:var(--ink-soft,#4B5A4F);">Enter the access token to review and decide payments.</p>
          <label for="maToken" style="display:block;font-size:0.82rem;color:var(--ink-soft,#4B5A4F);margin-bottom:0.25rem;">Token</label>
          <input id="maToken" type="password" autocomplete="off" required
            style="width:100%;box-sizing:border-box;font:inherit;padding:0.55rem 0.65rem;border:1px solid var(--paper-line,#C9D3C3);border-radius:3px;background:#fff;" />
          <p id="maMsg" role="alert" style="min-height:1.3rem;margin:0.6rem 0;font-size:0.85rem;color:var(--rust,#8B3A2B);"></p>
          <button type="submit" style="font:inherit;font-weight:500;padding:0.5rem 1.1rem;border:0;border-radius:3px;background:var(--ink,#1E2A22);color:var(--paper,#EEF2EA);cursor:pointer;">Sign in</button>
        </form>`;
      document.body.appendChild(overlay);
      overlay.querySelector('#maForm').addEventListener('submit', onSubmit);
    }
    overlay.querySelector('#maMsg').textContent = message || '';  // textContent: never HTML
    // Focus only when the wall first appears: later calls (e.g. a background
    // refresh failing) must not pull focus away from someone typing.
    if (fresh) overlay.querySelector('#maToken').focus();
  }

  async function onSubmit(ev) {
    ev.preventDefault();
    const typed = overlay.querySelector('#maToken').value.trim();
    const msg = overlay.querySelector('#maMsg');
    msg.textContent = 'Checking…';
    try {
      // Any manager route works as a credential check.
      const res = await fetch('/api/stats', { headers: { Authorization: 'Bearer ' + typed } });
      if (res.ok) {
        sessionStorage.setItem(KEY, typed);
        location.reload();
        return;
      }
      if (res.status === 503) {
        const body = await res.json().catch(() => ({}));
        msg.textContent = body.detail || 'Manager access is not configured on the server.';
      } else {
        msg.textContent = 'Invalid token.';
      }
    } catch (_) {
      msg.textContent = 'Could not reach the server.';
    }
  }

  // fetch() that carries the token and falls back to the sign-in screen on 401.
  async function authedFetch(url, options = {}) {
    const headers = { ...(options.headers || {}), Authorization: 'Bearer ' + token() };
    const res = await fetch(url, { ...options, headers });
    if (res.status === 401) {
      sessionStorage.removeItem(KEY);
      // Only explain "expired" if the wall was not already up (a rejected
      // sign-in attempt has its own message and must not be overwritten).
      if (!overlay) showLogin('Session expired or invalid token.');
      const err = new Error('Not authenticated');
      err.code = 'AUTH';  // callers use this to stop polling quietly
      throw err;
    }
    return res;
  }

  // True when a token is stored; otherwise shows the sign-in screen.
  function ensure() {
    if (token()) return true;
    showLogin('');
    return false;
  }

  function logout() {
    sessionStorage.removeItem(KEY);
    location.reload();
  }

  window.ManagerAuth = { fetch: authedFetch, ensure, logout };
})();
