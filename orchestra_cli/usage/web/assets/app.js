const grid = document.getElementById('provider-grid');
const refreshButton = document.getElementById('refresh-button');
const liveState = document.getElementById('live-state');
const updatedAt = document.getElementById('updated-at');
const recommendation = document.getElementById('recommendation');
const footerStatus = document.getElementById('footer-status');
const networkScope = document.getElementById('network-scope');

if (networkScope) {
  const host = window.location.hostname;
  networkScope.textContent = host.startsWith('100.') ? 'tailnet' : 'loopback';
}

const monograms = { minimax: 'MM', kimi: 'K', claude: 'CL', zai: 'Z', codex: 'CX' };
const statusLabels = {
  ok: 'Live', stale: 'Cached', auth_required: 'Login', not_configured: 'Setup', unavailable: 'Offline'
};

let loading = false;

const escapeHtml = (value) => String(value ?? '').replace(/[&<>"']/g, (char) => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
}[char]));

function percent(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return '—';
  return Number.isInteger(number) ? String(number) : number.toFixed(1);
}

function severity(remaining) {
  if (remaining <= 15) return 'critical';
  if (remaining <= 35) return 'watch';
  return 'healthy';
}

function burnLabel(rate) {
  if (rate == null || !Number.isFinite(Number(rate))) {
    return '<span class="burn-rate">sampling burn rate</span>';
  }
  const value = Number(rate);
  const tone = value >= 10 ? 'hot' : value >= 3 ? 'warm' : '';
  return `<span class="burn-rate ${tone}">−${escapeHtml(percent(value))}% / hr</span>`;
}

function durationUntil(value, action = 'resets') {
  const reset = new Date(value).getTime();
  if (!Number.isFinite(reset)) return 'reset unknown';
  let seconds = Math.max(0, Math.floor((reset - Date.now()) / 1000));
  if (seconds === 0) return action === 'expires' ? 'expired' : 'resetting now';
  const days = Math.floor(seconds / 86400); seconds %= 86400;
  const hours = Math.floor(seconds / 3600); seconds %= 3600;
  const minutes = Math.floor(seconds / 60);
  if (days) return `${action} in ${days}d ${hours}h`;
  if (hours) return `${action} in ${hours}h ${minutes}m`;
  return `${action} in ${minutes}m`;
}

function windowTemplate(window) {
  const remaining = Number(window.remaining_percent) || 0;
  const tone = severity(remaining);
  const reset = window.resets_at
    ? `<span class="quota-reset" data-reset="${escapeHtml(window.resets_at)}">reset pending</span>`
    : '<span class="quota-reset">reset unknown</span>';
  return `
    <div class="quota-row ${tone}">
      <div class="quota-header">
        <span class="quota-label"><strong>${escapeHtml(window.label)}</strong> · ${escapeHtml(window.scope)}</span>
        <span class="quota-percent">${escapeHtml(percent(remaining))}% left</span>
      </div>
      <div class="quota-track" role="progressbar" aria-label="${escapeHtml(window.label)} remaining"
        aria-valuemin="0" aria-valuemax="100" aria-valuenow="${escapeHtml(remaining)}">
        <div class="quota-fill" data-progress="${escapeHtml(remaining)}"></div>
      </div>
      <div class="quota-meta">${reset}${burnLabel(window.burn_rate_percent_per_hour)}</div>
    </div>`;
}

function resetCreditsTemplate(resetCredits) {
  if (!resetCredits) return '';
  const count = Number(resetCredits.available_count);
  if (!Number.isInteger(count) || count < 0) return '';
  // Match the spec: always show the slot. "0 reset credits available" when
  // empty; otherwise "N reset credit(s) available".
  const creditsLabel = count === 1 ? 'reset credit available' : 'reset credits available';
  const detail = resetCredits.title ? escapeHtml(resetCredits.title) : 'Rate-limit reset credit';
  const expiry = resetCredits.expires_at
    ? `<span data-expiry="${escapeHtml(resetCredits.expires_at)}">expiry pending</span>`
    : '<span>expiry unknown</span>';
  return `
    <div class="reset-credit ${count === 0 ? 'none' : ''}">
      <div class="reset-credit-count"><strong>${escapeHtml(count)}</strong> ${escapeHtml(creditsLabel)}</div>
      <div class="reset-credit-detail">${detail} · ${expiry}</div>
    </div>`;
}

function commandFor(provider) {
  if (provider.status === 'stale') return 'retry shortly';
  if (provider.id === 'claude') return 'claude → /login';
  if (provider.id === 'minimax' || provider.id === 'kimi' || provider.id === 'zai') return 'opencode auth login';
  return 'codex login';
}

function providerTemplate(provider) {
  const hasQuota = Array.isArray(provider.windows) && provider.windows.length > 0;
  const headroom = provider.headroom_percent;
  const cardTone = headroom == null ? '' : `status-${severity(Number(headroom))}`;
  const status = statusLabels[provider.status] || provider.status;
  const source = provider.source ? `via ${provider.source}` : 'credential source unavailable';
  const emptyTitle = provider.status === 'auth_required'
    ? 'Login needed'
    : provider.status === 'stale' ? 'Usage refreshing' : 'Usage unavailable';
  const body = hasQuota ? `
    <div class="headroom">
      <div class="headroom-number">${escapeHtml(percent(headroom))}<small>%</small></div>
      <div class="headroom-label">minimum coding headroom</div>
    </div>
    ${resetCreditsTemplate(provider.rate_limit_resets)}
    <div class="quota-list">${provider.windows.map(windowTemplate).join('')}</div>
  ` : `
    <div class="empty-state">
      <div class="empty-state-icon" aria-hidden="true">
        <svg viewBox="0 0 20 20"><path d="M10 3v8m0 3.3v.2"/><circle cx="10" cy="10" r="7.2"/></svg>
      </div>
      <h3>${emptyTitle}</h3>
      <p>${escapeHtml(provider.message || 'This provider did not return a quota snapshot.')}</p>
      <code class="empty-command">${escapeHtml(commandFor(provider))}</code>
    </div>`;
  return `
    <article class="provider-card ${cardTone}" data-provider="${escapeHtml(provider.id)}">
      <div class="provider-top">
        <div class="provider-monogram" aria-hidden="true">${escapeHtml(monograms[provider.id] || provider.name.slice(0, 2))}</div>
        <div class="provider-identity">
          <h3 class="provider-name">${escapeHtml(provider.name)}</h3>
          <div class="provider-plan">${escapeHtml(provider.plan || 'Coding plan')}</div>
        </div>
        <span class="status-pill ${escapeHtml(provider.status)}">${escapeHtml(status)}</span>
      </div>
      ${body}
      <div class="provider-footer" title="${escapeHtml(provider.message || '')}">
        <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 1.8 3.6 3.7v3.6c0 2.8 1.8 5.4 4.4 6.7 2.6-1.3 4.4-3.9 4.4-6.7V3.7L8 1.8Z"/></svg>
        <span>${escapeHtml(source)}</span>
      </div>
    </article>`;
}

function applyProgress() {
  document.querySelectorAll('[data-progress]').forEach((element) => {
    const value = Math.max(0, Math.min(100, Number(element.dataset.progress) || 0));
    requestAnimationFrame(() => { element.style.width = `${value}%`; });
  });
}

function render(data) {
  grid.innerHTML = data.providers.map(providerTemplate).join('');
  applyProgress();
  tickResets();

  const generated = new Date(data.generated_at);
  updatedAt.textContent = `Updated ${generated.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit', second: '2-digit' })}`;
  footerStatus.textContent = `${data.providers.filter((p) => p.windows?.length).length} of ${data.providers.length} providers reporting`;

  if (data.recommendation) {
    recommendation.classList.remove('is-loading');
    recommendation.innerHTML = `
      <div class="recommendation-label">Best runway</div>
      <div class="recommendation-value">${escapeHtml(data.recommendation.provider_name)}</div>
      <div class="recommendation-detail">${escapeHtml(percent(data.recommendation.headroom_percent))}% minimum headroom · route the next flexible run here</div>`;
  } else {
    recommendation.innerHTML = `
      <div class="recommendation-label">Best runway</div>
      <div class="recommendation-value">No signal</div>
      <div class="recommendation-detail">No provider returned usable plan quota.</div>`;
  }
}

function tickResets() {
  document.querySelectorAll('[data-reset]').forEach((element) => {
    element.textContent = durationUntil(element.dataset.reset);
  });
  document.querySelectorAll('[data-expiry]').forEach((element) => {
    element.textContent = durationUntil(element.dataset.expiry, 'expires');
  });
}

async function load({ force = false } = {}) {
  if (loading) return;
  loading = true;
  refreshButton.disabled = true;
  refreshButton.classList.add('is-loading');
  try {
    const response = await fetch(`/api/usage${force ? '?refresh=1' : ''}`, { cache: 'no-store' });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    render(await response.json());
    liveState.className = 'live-state online';
    liveState.innerHTML = '<span class="live-dot"></span> Live';
  } catch (error) {
    liveState.className = 'live-state offline';
    liveState.innerHTML = '<span class="live-dot"></span> Offline';
    footerStatus.textContent = 'Could not reach local quota service';
  } finally {
    loading = false;
    refreshButton.disabled = false;
    refreshButton.classList.remove('is-loading');
  }
}

refreshButton.addEventListener('click', () => load({ force: true }));
load({ force: true });
setInterval(() => load(), 90_000);
setInterval(tickResets, 1_000);
