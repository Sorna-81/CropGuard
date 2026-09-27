/* CropGuard frontend integration. Authentication uses sessionStorage and backend bearer tokens. */
(() => {
  'use strict';

  const TOKEN_KEY = 'cropguard.accessToken';
  const REFRESH_KEY = 'cropguard.refreshToken';
  const state = { user: null, crops: [], scans: [], alerts: [], knowledge: [], activeCase: null, activeScan: null, editingCrop: null };
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
  const token = () => sessionStorage.getItem(TOKEN_KEY);

  function toast(message, type = 'info') {
    const host = $('#toastHost');
    const el = document.createElement('div');
    el.className = `toast align-items-center text-bg-${type === 'error' ? 'danger' : type === 'success' ? 'success' : 'dark'} border-0`;
    el.setAttribute('role', 'status');
    el.innerHTML = '<div class="d-flex"><div class="toast-body"></div><button type="button" class="btn-close btn-close-white me-2 m-auto" data-bs-dismiss="toast" aria-label="Close"></button></div>';
    $('.toast-body', el).textContent = message;
    host.appendChild(el);
    if (window.bootstrap?.Toast) {
      const instance = new bootstrap.Toast(el, { delay: 4500 });
      el.addEventListener('hidden.bs.toast', () => el.remove(), { once: true });
      instance.show();
    } else {
      el.classList.add('show');
      setTimeout(() => el.remove(), 4500);
    }
  }

  function clearSession(message) {
    sessionStorage.removeItem(TOKEN_KEY);
    sessionStorage.removeItem(REFRESH_KEY);
    sessionStorage.removeItem('cropguard.passwordResetPending');
    state.user = null;
    renderNav();
    if (location.hash !== '#login') location.hash = '#login';
    if (message) toast(message, 'error');
  }

  // One request wrapper handles JSON, multipart, auth, status codes and network failures.
  const api = {
    async request(path, options = {}) {
      const rawResponse = Boolean(options.raw);
      const fetchOptions = { ...options };
      delete fetchOptions.raw;
      const headers = new Headers(options.headers || {});
      if (token()) headers.set('Authorization', `Bearer ${token()}`);
      const isForm = options.body instanceof FormData || options.body instanceof URLSearchParams;
      if (options.body && !isForm && typeof options.body !== 'string') {
        headers.set('Content-Type', 'application/json');
        // fetchOptions was copied above; serialize into the options actually sent.
        fetchOptions.body = JSON.stringify(options.body);
      }
      let response;
      try {
        response = await fetch(path, { ...fetchOptions, headers });
      } catch (_) {
        throw new Error('Cannot reach CropGuard. Check that the application is running and try again.');
      }
      const noRefreshPaths = ['/api/auth/refresh', '/api/auth/login', '/api/auth/register', '/api/auth/logout', '/api/auth/password-reset'];
      if (response.status === 401 && sessionStorage.getItem(REFRESH_KEY) && !noRefreshPaths.includes(path)) {
        try {
          const refreshed = await fetch('/api/auth/refresh', { method: 'POST', body: new URLSearchParams({ refresh_token: sessionStorage.getItem(REFRESH_KEY) }) });
          const session = refreshed.ok ? await refreshed.json() : null;
          if (session?.access_token) {
            sessionStorage.setItem(TOKEN_KEY, session.access_token);
            if (session.refresh_token) sessionStorage.setItem(REFRESH_KEY, session.refresh_token);
            headers.set('Authorization', `Bearer ${session.access_token}`);
            response = await fetch(path, { ...fetchOptions, headers });
          }
        } catch (_) { /* handled as an expired session below */ }
      }
      let data = null;
      if (!rawResponse || !response.ok) try { data = await response.json(); } catch (_) { /* non-JSON error */ }
      if (response.status === 401) {
        clearSession('Your session has expired. Please log in again.');
        const error = new Error('Please log in to continue.'); error.handled = true; throw error;
      }
      if (response.status === 403) throw new Error(data?.message || 'Your account does not have access to this page.');
      if (response.status === 404) throw new Error(data?.message || 'That record could not be found. It may have been removed.');
      if (response.status === 422) throw new Error(data?.message || 'Check the required fields and try again.');
      if (response.status >= 500) throw new Error(data?.message || 'CropGuard could not complete that request. Please try again.');
      if (!response.ok) throw new Error(data?.message || `Request failed (${response.status}).`);
      return rawResponse ? response : data;
    },
    get(path) { return this.request(path); },
    post(path, body) { return this.request(path, { method: 'POST', body }); },
    put(path, body) { return this.request(path, { method: 'PUT', body }); },
    delete(path) { return this.request(path, { method: 'DELETE' }); }
  };

  async function loadPrivateImage(path, image) {
    try {
      const response = await api.request(path, { raw: true });
      image.src = URL.createObjectURL(await response.blob());
      image.hidden = false;
    } catch (_) { image.hidden = true; }
  }
  async function downloadReport(scanId) {
    const response = await api.request(`/api/scans/${scanId}/report.pdf`, { raw: true });
    const objectUrl = URL.createObjectURL(await response.blob());
    const link = document.createElement('a'); link.href = objectUrl; link.download = `cropguard-scan-${scanId}.pdf`;
    document.body.appendChild(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
  }

  function showPage(name) {
    $$('.app-view').forEach((page) => page.classList.toggle('active', page.id === `page-${name}`));
    $$('[data-nav]').forEach((link) => link.classList.toggle('active', link.getAttribute('href') === `#${name}`));
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  const roleHome = () => state.user?.role === 'EXPERT' ? 'expert' : state.user?.role === 'ADMIN' ? 'admin' : 'dashboard';
  const restricted = (role) => !state.user || !role.split(',').includes(state.user.role);

  function renderNav() {
    const loggedIn = Boolean(state.user && token());
    $('#navAuth').classList.toggle('d-none', loggedIn);
    $('#navUser').classList.toggle('d-none', !loggedIn);
    $('#navUser').classList.toggle('d-flex', loggedIn);
    $('#navIdentity').textContent = loggedIn ? `${state.user.email} · ${state.user.role}` : '';
    $$('[data-farmer]').forEach((el) => { el.hidden = !loggedIn || state.user.role !== 'FARMER'; });
    $$('[data-expert]').forEach((el) => { el.hidden = !loggedIn || !['EXPERT', 'ADMIN'].includes(state.user.role); });
    $$('[data-admin]').forEach((el) => { el.hidden = !loggedIn || state.user.role !== 'ADMIN'; });
    const profile = $('#profileNav');
    if (loggedIn && state.user.role === 'FARMER' && !profile) {
      const li = document.createElement('li'); li.className = 'nav-item'; li.id = 'profileNav'; li.dataset.farmer = '';
      li.innerHTML = '<a class="nav-link" href="#profile" data-nav data-i18n="Profile">Profile</a>';
      $('#mainNav').appendChild(li);
    } else if ((!loggedIn || state.user.role !== 'FARMER') && profile) profile.remove();
    window.CropGuardI18n?.apply(window.CropGuardI18n.selected());
  }

  function navigate() {
    let name = (location.hash || '#landing').slice(1);
    if (!$(`#page-${CSS.escape(name)}`)) name = 'landing';
    const authPages = ['login', 'register', 'landing'];
    if (!authPages.includes(name) && !state.user) {
      location.hash = '#login';
      toast('Please log in to open your workspace.', 'info');
      return;
    }
    if (state.user && state.user.role !== 'FARMER' && ['dashboard', 'profile', 'crops', 'scan', 'history', 'alerts'].includes(name)) {
      location.hash = `#${roleHome()}`;
      return;
    }
    const page = $(`#page-${CSS.escape(name)}`);
    const pageRole = page?.dataset.restricted;
    if (pageRole && restricted(pageRole)) {
      toast('Your account does not have access to that workspace.', 'error');
      location.hash = state.user ? `#${roleHome()}` : '#login';
      return;
    }
    if (state.user?.role === 'FARMER' && ['expert', 'admin'].includes(name)) {
      location.hash = '#dashboard'; return;
    }
    showPage(name);
    const loaders = { dashboard: loadDashboard, profile: loadProfile, crops: loadCrops, scan: loadScanPage, history: loadHistory, alerts: loadAlerts, expert: loadExpert, admin: loadAdmin };
    if (loaders[name]) loaders[name]().catch(showError);
    const collapse = $('#appNav');
    if (collapse.classList.contains('show') && window.bootstrap?.Collapse) bootstrap.Collapse.getOrCreateInstance(collapse).hide();
  }

  function showError(error) { if (!error?.handled) toast(error?.message || 'Something went wrong. Please try again.', 'error'); }
  const dateText = (value) => value ? new Date(value).toLocaleString() : '—';
  const confidenceText = (value) => Number.isFinite(Number(value)) ? `${(Number(value) * 100).toFixed(1)}%` : '—';
  const riskClass = (value) => `risk-${String(value || 'low').toLowerCase()}`;
  const listValue = (value) => Array.isArray(value) ? value : (() => { try { return JSON.parse(value || '[]'); } catch (_) { return []; } })();

  $('#registerForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    const password = $('#registerPassword').value;
    if (password.length < 10) return toast('Choose a password with at least 10 characters.', 'error');
    try {
      const body = new URLSearchParams({ email: $('#registerEmail').value.trim(), password, full_name: $('#registerName').value.trim() });
      const result = await api.post('/api/auth/register', body);
      if (!result.access_token) {
        toast(result.message || 'Check your email to confirm your account, then sign in.', 'success');
        location.hash = '#login';
        return;
      }
      sessionStorage.setItem(TOKEN_KEY, result.access_token);
      if (result.refresh_token) sessionStorage.setItem(REFRESH_KEY, result.refresh_token);
      state.user = result.user;
      renderNav(); toast('Your farmer account is ready.', 'success'); location.hash = '#dashboard';
    } catch (error) { showError(error); }
  });

  $('#loginForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    try {
      const body = new URLSearchParams({ email: $('#loginEmail').value.trim(), password: $('#loginPassword').value });
      const result = await api.post('/api/auth/login', body);
      sessionStorage.setItem(TOKEN_KEY, result.access_token);
      if (result.refresh_token) sessionStorage.setItem(REFRESH_KEY, result.refresh_token);
      state.user = result.user; renderNav(); toast('Signed in successfully.', 'success'); location.hash = `#${roleHome()}`;
    } catch (error) { showError(error); }
  });

  $('#showPasswordReset').addEventListener('click', () => {
    $('#passwordResetForm').hidden = !$('#passwordResetForm').hidden;
    if ($('#loginEmail').value) $('#resetEmail').value = $('#loginEmail').value;
  });
  $('#passwordResetForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    try {
      const result = await api.post('/api/auth/password-reset', new URLSearchParams({ email: $('#resetEmail').value.trim() }));
      toast(result.message || 'If the address is registered, reset instructions will be sent.', 'success');
    } catch (error) { showError(error); }
  });
  $('#updatePasswordForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    const password = $('#newPassword').value;
    if (password.length < 10) return toast('Choose a password with at least 10 characters.', 'error');
    try {
      const result = await api.post('/api/auth/update-password', { password });
      sessionStorage.removeItem('cropguard.passwordResetPending');
      $('#updatePasswordForm').hidden = true; $('#loginForm').hidden = false;
      toast(result.message || 'Your password has been updated.', 'success');
      location.hash = `#${roleHome()}`;
    } catch (error) { showError(error); }
  });

  $('#logoutBtn').addEventListener('click', async () => {
    try { if (token()) await api.post('/api/auth/logout', {}); } catch (_) { /* local logout still clears the token */ }
    sessionStorage.removeItem(TOKEN_KEY); state.user = null; renderNav();
    sessionStorage.removeItem(REFRESH_KEY);
    location.hash = '#landing'; toast('You are signed out.', 'success');
  });

  async function loadDashboard() {
    if (state.user.role !== 'FARMER') { location.hash = `#${roleHome()}`; return; }
    const [profile, crops, scans, alerts] = await Promise.all([api.get('/api/profile'), api.get('/api/crops'), api.get('/api/scans'), api.get('/api/alerts')]);
    state.crops = crops; state.scans = scans; state.alerts = alerts;
    $('#dashboardGreeting').textContent = profile.full_name ? `Good to see you, ${profile.full_name}. Here are your latest field observations.` : 'Complete your profile and add a crop to begin.';
    $('#myCropCount').textContent = crops.length;
    $('#myScanCount').textContent = scans.length;
    $('#myAlertCount').textContent = alerts.filter((a) => !a.is_read).length;
    $('#myHighRiskCount').textContent = scans.filter((s) => s.risk_level === 'HIGH').length;
    $('#dashboardCrops').innerHTML = crops.length ? crops.slice(0, 6).map((crop) => `<div class="col-md-6"><div class="card crop-card h-100 p-3"><div class="fw-bold">${esc(crop.name)}</div><div class="small text-muted">${esc(crop.variety || 'Variety not specified')} · ${esc(crop.growth_stage || 'Stage not specified')}</div><div class="small mt-2">${esc(crop.location || 'Location not specified')}</div><a class="btn btn-sm btn-outline-success mt-3" href="#crops">View crop</a></div></div>`).join('') : '<div class="col-12"><p class="text-muted">No crops yet. Add your first crop to start tracking it.</p><a href="#crops" class="btn btn-outline-success">Add a crop</a></div>';
    $('#dashboardScans').innerHTML = scans.length ? scans.slice(0, 5).map((scan) => `<div class="d-flex justify-content-between gap-2 border-bottom py-2"><div><div class="fw-semibold">${esc(scan.name)}</div><div class="small text-muted">${esc(scan.kind)} · ${dateText(scan.created_at)}</div></div><span class="risk-pill ${riskClass(scan.risk_level)}">${esc(scan.risk_level || '—')}</span></div>`).join('') : '<p class="text-muted">No data yet. Your completed crop scans will appear here.</p>';
    updateAlertBadge(alerts);
  }

  function updateAlertBadge(alerts) {
    const count = alerts.filter((item) => !item.is_read).length;
    const badge = $('#alertBadge'); badge.textContent = count; badge.hidden = count === 0;
  }

  async function loadProfile() {
    const [profile, user] = await Promise.all([api.get('/api/profile'), api.get('/api/auth/me')]);
    const values = { profileName: profile.full_name, profileEmail: user.email, profileMobile: profile.mobile, profileLanguage: profile.language || 'English', profileState: profile.state, profileDistrict: profile.district, profileVillage: profile.village, profileArea: profile.farm_area, profileSoil: profile.soil_type, profileIrrigation: profile.irrigation_type };
    Object.entries(values).forEach(([id, value]) => { const input = $(`#${id}`); if (input) input.value = value ?? ''; });
    $('#languageSwitcher').value = values.profileLanguage;
    window.CropGuardI18n?.apply(values.profileLanguage);
  }

  $('#profileForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    const area = $('#profileArea').value;
    if (area && Number(area) < 0) return toast('Farm area cannot be negative.', 'error');
    const body = { full_name: $('#profileName').value.trim(), mobile: $('#profileMobile').value.trim(), language: $('#profileLanguage').value, state: $('#profileState').value.trim(), district: $('#profileDistrict').value.trim(), village: $('#profileVillage').value.trim(), farm_area: area ? Number(area) : null, soil_type: $('#profileSoil').value.trim(), irrigation_type: $('#profileIrrigation').value.trim() };
    try { await api.put('/api/profile', body); toast('Profile saved.', 'success'); }
    catch (error) { showError(error); }
  });

  $('#languageSwitcher').value = window.CropGuardI18n?.selected() || 'English';
  $('#languageSwitcher').addEventListener('change', async (event) => {
    const language = event.target.value;
    window.CropGuardI18n?.apply(language);
    if ($('#profileLanguage')) $('#profileLanguage').value = language;
    if (state.user?.role === 'FARMER') {
      try { await api.put('/api/profile', { language }); }
      catch (error) { showError(error); }
    }
  });
  $('#profileLanguage')?.addEventListener('change', (event) => {
    $('#languageSwitcher').value = event.target.value;
    window.CropGuardI18n?.apply(event.target.value);
  });

  function resetCropForm() {
    $('#cropForm').reset(); $('#cropEditId').value = ''; state.editingCrop = null;
    $('#cropFormHeading').textContent = 'Add a crop'; $('#cropEditor').hidden = true;
  }
  function cropBody() {
    const area = $('#cropArea').value;
    if (area && Number(area) < 0) throw new Error('Crop area cannot be negative.');
    return { name: $('#cropName').value.trim(), variety: $('#cropVariety').value.trim(), sowing_date: $('#cropSowingDate').value || null, growth_stage: $('#cropGrowthStage').value || null, area: area ? Number(area) : null, location: $('#cropLocation').value.trim(), soil_type: $('#cropSoil').value.trim(), irrigation_type: $('#cropIrrigation').value.trim() };
  }
  async function loadCrops() {
    state.crops = await api.get('/api/crops');
    $('#cropList').innerHTML = state.crops.length ? state.crops.map((crop) => `<div class="col-md-6 col-xl-4"><article class="card panel-card crop-card h-100 p-4"><div class="d-flex justify-content-between gap-2"><h2 class="h5">${esc(crop.name)}</h2><span class="badge text-bg-light">${esc(crop.growth_stage || 'Stage not set')}</span></div><dl class="row small mb-3"><dt class="col-5">Variety</dt><dd class="col-7">${esc(crop.variety || '—')}</dd><dt class="col-5">Sown</dt><dd class="col-7">${esc(crop.sowing_date || '—')}</dd><dt class="col-5">Area</dt><dd class="col-7">${esc(crop.area ?? '—')}</dd><dt class="col-5">Location</dt><dd class="col-7">${esc(crop.location || '—')}</dd></dl><div class="mt-auto d-flex flex-wrap gap-2"><button class="btn btn-sm btn-success" data-action="scan" data-id="${crop.id}">Scan</button><button class="btn btn-sm btn-outline-secondary" data-action="timeline" data-id="${crop.id}">Health timeline</button><button class="btn btn-sm btn-outline-secondary" data-action="edit" data-id="${crop.id}">Edit</button><button class="btn btn-sm btn-outline-danger" data-action="delete" data-id="${crop.id}">Delete</button></div></article></div>`).join('') : '<div class="col-12"><div class="card panel-card p-4 text-muted">No data yet. Add a crop to connect scans and health history.</div></div>';
    populateCropSelect();
  }
  function populateCropSelect() {
    const select = $('#scanCrop'); if (!select) return;
    const old = select.value;
    select.innerHTML = '<option value="">Choose one of your crops</option>' + state.crops.map((crop) => `<option value="${crop.id}">${esc(crop.name)}${crop.variety ? ` · ${esc(crop.variety)}` : ''}</option>`).join('');
    if (state.crops.some((c) => String(c.id) === old)) select.value = old;
  }

  $('#newCropBtn').addEventListener('click', () => { resetCropForm(); $('#cropEditor').hidden = false; $('#cropName').focus(); });
  $('#cancelCropBtn').addEventListener('click', resetCropForm);
  $('#cropForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    try {
      const body = cropBody();
      if (!body.name) return toast('Enter the crop name.', 'error');
      const id = $('#cropEditId').value;
      if (id) await api.put(`/api/crops/${id}`, body); else await api.post('/api/crops', body);
      resetCropForm(); await loadCrops(); toast(id ? 'Crop updated.' : 'Crop added.', 'success');
    } catch (error) { showError(error); }
  });
  $('#cropList').addEventListener('click', async (event) => {
    const button = event.target.closest('button[data-action]'); if (!button) return;
    const crop = state.crops.find((item) => item.id === Number(button.dataset.id)); if (!crop) return;
    if (button.dataset.action === 'scan') { location.hash = '#scan'; setTimeout(() => { $('#scanCrop').value = String(crop.id); $('#scanLocation').value = crop.location || ''; }, 0); }
    if (button.dataset.action === 'edit') {
      $('#cropEditor').hidden = false; $('#cropFormHeading').textContent = `Edit ${crop.name}`; $('#cropEditId').value = crop.id;
      const values = { cropName: crop.name, cropVariety: crop.variety, cropSowingDate: crop.sowing_date, cropGrowthStage: crop.growth_stage, cropArea: crop.area, cropLocation: crop.location, cropSoil: crop.soil_type, cropIrrigation: crop.irrigation_type };
      Object.entries(values).forEach(([id, value]) => { $(`#${id}`).value = value ?? ''; }); state.editingCrop = crop.id;
      $('#cropEditor').scrollIntoView({ behavior: 'smooth' });
    }
    if (button.dataset.action === 'delete' && confirm(`Delete ${crop.name}? Its associated crop scans will remain in your history.`)) {
      try { await api.delete(`/api/crops/${crop.id}`); await loadCrops(); toast('Crop deleted.', 'success'); }
      catch (error) { showError(error); }
    }
    if (button.dataset.action === 'timeline') await showCropTimeline(crop);
  });

  async function showCropTimeline(crop) {
    const [scans, risk] = await Promise.all([api.get('/api/scans'), api.get(`/api/risk/${crop.id}`)]);
    const matches = scans.filter((scan) => scan.crop_id === crop.id);
    const panel = $('#cropTimeline'); panel.hidden = false;
    panel.innerHTML = `<div class="d-flex justify-content-between flex-wrap"><div><h2 class="h5">${esc(crop.name)} health timeline</h2><p>Current estimated risk: <span class="risk-pill ${riskClass(risk.risk_level)}">${esc(risk.risk_level)} · ${risk.risk_score}/100</span></p><p class="small text-muted">${risk.reasons.map(esc).join(' · ')}</p></div><button class="btn-close" aria-label="Close timeline" id="closeTimeline"></button></div><div>${matches.length ? matches.map((scan) => `<div class="timeline-item"><div class="fw-semibold">${esc(scan.name)} <span class="badge text-bg-light">${esc(scan.kind)}</span></div><div class="small text-muted">${dateText(scan.created_at)} · Confidence ${confidenceText(scan.confidence)} · ${esc(scan.risk_level || 'Risk unavailable')}</div></div>`).join('') : '<p class="text-muted">No data yet for this crop.</p>'}</div>`;
    $('#closeTimeline').addEventListener('click', () => { panel.hidden = true; });
    panel.scrollIntoView({ behavior: 'smooth' });
  }

  $('#scanImage').addEventListener('change', () => {
    const file = $('#scanImage').files[0];
    $('#scanPreview').hidden = true;
    if (!file) return;
    if (file.size > 5 * 1024 * 1024) { $('#scanImage').value = ''; return toast('Image must be 5 MB or smaller.', 'error'); }
    if (!['image/jpeg', 'image/png', 'image/webp'].includes(file.type)) { $('#scanImage').value = ''; return toast('Choose a JPG, PNG, or WEBP image.', 'error'); }
    $('#scanPreview').src = URL.createObjectURL(file); $('#scanPreview').hidden = false;
  });
  $('#scanCrop').addEventListener('change', () => {
    const crop = state.crops.find((item) => String(item.id) === $('#scanCrop').value);
    if (crop) $('#scanLocation').value = crop.location || '';
  });
  $('#checkWeatherBtn').addEventListener('click', async () => {
    const locationName = $('#scanLocation').value.trim();
    if (!locationName) return toast('Enter a village or district to check local weather.', 'info');
    $('#scanWeatherInfo').textContent = 'Checking local weather...';
    try {
      const result = await api.get(`/api/weather?location=${encodeURIComponent(locationName)}`);
      $('#scanWeatherInfo').textContent = result.available
        ? `${result.weather_data.location}: ${result.weather_data.temperature} °C, humidity ${result.weather_data.humidity}%, ${result.weather_data.conditions}.`
        : result.message || 'Weather is unavailable.';
    } catch (error) { $('#scanWeatherInfo').textContent = error.message; }
  });
  async function loadScanPage() {
    state.crops = await api.get('/api/crops'); populateCropSelect();
    $('#scanResult').hidden = true;
    $('#scanMessage').className = 'alert alert-info';
    $('#scanMessage').textContent = state.crops.length ? 'Choose a crop and upload a clear image.' : 'Add a crop before scanning.';
  }
  $('#scanForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    const file = $('#scanImage').files[0], crop = state.crops.find((item) => String(item.id) === $('#scanCrop').value);
    if (!crop) return toast('Select a registered crop first.', 'error');
    if (!file) return toast('Choose an image to analyze.', 'error');
    const form = new FormData(); form.append('image', file); form.append('cropType', crop.name); form.append('crop_id', crop.id); form.append('location', $('#scanLocation').value.trim()); form.append('detection_type', $('#scanType').value);
    const progress = $('#scanProgress'); progress.hidden = false; $('#analyzeCropBtn').disabled = true;
    const steps = $$('[data-progress]', progress); steps.forEach((step) => { step.classList.remove('text-success', 'fw-bold'); });
    let active = 0; steps[0]?.classList.add('fw-bold');
    const timer = setInterval(() => { steps[active]?.classList.remove('fw-bold'); active = Math.min(active + 1, steps.length - 1); steps[active]?.classList.add('fw-bold'); }, 1100);
    try {
      const result = await api.post('/api/scans', form);
      if (result.status === 'unavailable') {
        $('#scanResult').hidden = false;
        $('#scanResult').innerHTML = `<div class="alert alert-warning mb-0"><h2 class="h5">AI model is currently unavailable.</h2><p class="mb-0">${esc(result.message)}</p></div>`;
        $('#scanMessage').textContent = 'No prediction was created and no scan was saved.';
        return;
      }
      steps.forEach((step) => { step.classList.remove('fw-bold'); step.classList.add('text-success'); });
      state.activeScan = result; renderScanResult(result);
      $('#scanMessage').className = 'alert alert-success'; $('#scanMessage').textContent = 'Analysis complete. The scan was saved to your history.';
      toast('Scan saved.', 'success');
    } catch (error) { $('#scanMessage').className = 'alert alert-danger'; $('#scanMessage').textContent = error.message; }
    finally { clearInterval(timer); $('#analyzeCropBtn').disabled = false; setTimeout(() => { progress.hidden = true; }, 500); }
  });

  function recommendationSection(title, entries) {
    const list = listValue(entries);
    return `<div class="col-md-6"><h3 class="h6">${esc(title)}</h3>${list.length ? `<ul>${list.map((item) => `<li>${esc(item)}</li>`).join('')}</ul>` : '<p class="small text-muted">No information available.</p>'}</div>`;
  }
  function renderScanResult(scan) {
    const info = scan.disease_info || {}, weather = scan.weather;
    const risk = scan.risk_level?.toUpperCase() || 'UNAVAILABLE';
    $('#scanResult').hidden = false;
    $('#scanResult').innerHTML = `<div class="d-flex justify-content-between flex-wrap gap-2"><div><span class="badge text-bg-light">${esc(scan.type)}</span><h2 class="h3 mt-2 mb-1">${esc(scan.disease_name)}</h2><div class="text-muted">${esc(scan.crop_type || 'Crop')}</div></div><span class="risk-pill ${riskClass(risk)}">${esc(risk)} risk · ${Number(scan.risk_score ?? 0)}/100</span></div><div class="row g-3 my-3"><div class="col-6"><div class="small text-muted">Confidence</div><strong>${confidenceText(scan.confidence)}</strong></div><div class="col-6"><div class="small text-muted">Severity estimate</div><strong>${esc(scan.severity || '—')}</strong></div></div>${scan.confidence_warning ? `<div class="alert alert-warning"><strong>Low-confidence result</strong><p class="mb-2">${esc(scan.confidence_warning)}</p><button class="btn btn-sm btn-outline-dark" id="requestReviewBtn">Request Expert Review</button></div>` : ''}<p class="small text-muted">${esc(scan.severity_basis || '')}</p><hr><h3 class="h6">Risk factors</h3><ul>${(scan.risk_reasons || []).map((reason) => `<li>${esc(reason)}</li>`).join('') || '<li>Risk factors are unavailable.</li>'}</ul><h3 class="h6">Weather</h3>${weather ? `<p>${esc(weather.location)} · ${esc(weather.temperature)} °C · Humidity ${esc(weather.humidity)}% · ${esc(weather.conditions)} · Wind ${esc(weather.wind_speed)} m/s${weather.rainfall != null ? ` · Rain ${esc(weather.rainfall)} mm` : ''}</p>` : '<p class="text-muted">Weather unavailable. Risk was calculated without weather data.</p>'}<div class="row">${recommendationSection('Symptoms', info.symptoms)}${recommendationSection('Prevention', info.prevention)}${recommendationSection('Cultural management', info.cultural_management)}${recommendationSection('Biological management', info.biological_management)}${recommendationSection('Chemical management', info.chemical_management)}</div><p class="small text-muted mb-0">Follow locally registered product labels and consult your agricultural officer. Recommendations are general information.</p>`;
    $('#requestReviewBtn')?.addEventListener('click', async () => {
      try { const response = await api.post(`/api/scans/${scan.prediction_id}/expert-review`, { note: 'Please review this low-confidence scan.' }); toast(`Expert review requested (${response.status}).`, 'success'); $('#requestReviewBtn').disabled = true; }
      catch (error) { showError(error); }
    });
  }

  async function loadHistory() {
    const [scans, crops] = await Promise.all([api.get('/api/scans'), api.get('/api/crops')]);
    state.scans = scans; state.crops = crops;
    $('#historyRows').innerHTML = scans.map((scan) => {
      const crop = crops.find((item) => item.id === scan.crop_id);
      const status = scan.review_status || 'Saved';
      return `<tr><td>${esc(dateText(scan.created_at))}</td><td>${esc(crop?.name || 'Crop removed')}</td><td>${esc(scan.name)}</td><td>${esc(scan.kind)}</td><td>${confidenceText(scan.confidence)}</td><td>${esc(scan.severity || '—')}</td><td><span class="risk-pill ${riskClass(scan.risk_level)}">${esc(scan.risk_level || '—')}</span></td><td>${esc(status)}</td><td><button class="btn btn-sm btn-outline-success" data-open-scan="${scan.id}">Details</button></td></tr>`;
    }).join('');
    $('#historyEmpty').hidden = scans.length > 0;
  }
  $('#historyRows').addEventListener('click', async (event) => {
    const button = event.target.closest('[data-open-scan]'); if (!button) return;
    try {
      const scan = await api.get(`/api/scans/${button.dataset.openScan}`);
      const knowledgeRoute = scan.kind === 'pest' ? '/api/pests' : '/api/diseases';
      const knowledge = await api.get(knowledgeRoute);
      const info = knowledge.find((item) => item.name.toLowerCase() === scan.name.toLowerCase()) || {};
      const panel = $('#historyDetail'); panel.hidden = false;
      const weather = listValue(scan.weather);
      const reviews = scan.expert_reviews || [];
      panel.innerHTML = `<div class="d-flex justify-content-between"><h2 class="h5">${esc(scan.name)} · ${esc(scan.kind)}</h2><button class="btn-close" id="closeHistoryDetail" aria-label="Close"></button></div><p>Confidence ${confidenceText(scan.confidence)} · Severity ${esc(scan.severity || '—')} · Risk ${esc(scan.risk_level || '—')}</p><p class="small text-muted">${esc(dateText(scan.created_at))}</p><h3 class="h6">Weather and risk factors</h3><p>${weather && Object.keys(weather).length ? esc(JSON.stringify(weather)) : 'Weather unavailable.'}</p><ul>${listValue(scan.risk_reasons).map((r) => `<li>${esc(r)}</li>`).join('') || '<li>No risk factors recorded.</li>'}</ul><div class="row">${recommendationSection('Symptoms', info.symptoms)}${recommendationSection('Prevention', info.prevention)}${recommendationSection('Cultural management', info.cultural_management)}${recommendationSection('Biological management', info.biological_management)}${recommendationSection('Chemical management', info.chemical_management)}</div><h3 class="h6">Expert review</h3>${reviews.length ? reviews.map((review) => `<div class="border rounded p-3 mb-2"><strong>${esc(review.status)}</strong><p class="mb-1">${esc(review.response || review.request_note || 'Awaiting expert response')}</p><small class="text-muted">${esc(dateText(review.reviewed_at || review.created_at))}</small></div>`).join('') : '<p class="text-muted">No expert review requested.</p>'}${Number(scan.confidence) < 0.55 && !reviews.length ? `<button class="btn btn-outline-success" id="historyReviewBtn">Request Expert Review</button>` : ''}`;
      panel.insertAdjacentHTML('afterbegin', `<div class="d-flex justify-content-end gap-2 mb-2"><button class="btn btn-sm btn-outline-success" id="historyReportBtn">Download PDF report</button></div><img id="historyScanImage" class="img-fluid rounded my-2" alt="Uploaded crop scan" hidden style="max-height:320px">`);
      loadPrivateImage(`/api/scans/${scan.id}/image`, $('#historyScanImage'));
      $('#historyReportBtn').addEventListener('click', () => downloadReport(scan.id).catch(showError));
      $('#closeHistoryDetail').addEventListener('click', () => { panel.hidden = true; }); panel.scrollIntoView({ behavior: 'smooth' });
      $('#historyReviewBtn')?.addEventListener('click', async () => {
        try { await api.post(`/api/scans/${scan.id}/expert-review`, { note: 'Please review this low-confidence scan.' }); $('#historyReviewBtn').disabled = true; $('#historyReviewBtn').textContent = 'Review requested'; toast('Expert review requested.', 'success'); await loadHistory(); }
        catch (error) { showError(error); }
      });
    } catch (error) { showError(error); }
  });

  async function loadAlerts() {
    const [alerts, crops] = await Promise.all([api.get('/api/alerts'), api.get('/api/crops')]);
    state.alerts = alerts; state.crops = crops; updateAlertBadge(alerts);
    $('#alertsList').innerHTML = alerts.map((alert) => {
      const crop = crops.find((item) => item.id === alert.crop_id);
      const level = alert.message.toLowerCase().includes('high') ? 'HIGH' : 'ALERT';
      return `<div class="col-lg-6"><article class="card panel-card p-4 h-100 ${alert.is_read ? '' : 'border-start border-warning border-4'}"><div class="d-flex justify-content-between"><h2 class="h5">${esc(level)} crop alert</h2><span class="badge ${alert.is_read ? 'text-bg-secondary' : 'text-bg-warning'}">${alert.is_read ? 'Read' : 'Unread'}</span></div><p>${esc(alert.message)}</p><div class="small text-muted">${esc(crop?.name || 'Crop')} · ${esc(dateText(alert.created_at))}</div>${alert.is_read ? '' : `<button class="btn btn-sm btn-outline-success mt-3 align-self-start" data-read-alert="${alert.id}">Mark as read</button>`}</article></div>`;
    }).join('');
    $('#alertsEmpty').hidden = alerts.length > 0;
  }
  $('#alertsList').addEventListener('click', async (event) => {
    const button = event.target.closest('[data-read-alert]'); if (!button) return;
    try { await api.put(`/api/alerts/${button.dataset.readAlert}/read`, {}); await loadAlerts(); toast('Alert marked as read.', 'success'); }
    catch (error) { showError(error); }
  });

  async function loadExpert() {
    const cases = await api.get('/api/expert/cases');
    const pending = cases;
    $('#expertCases').innerHTML = pending.map((item) => `<div class="col-md-6 col-xl-4"><article class="card panel-card p-4 h-100"><span class="badge text-bg-warning align-self-start">Pending</span><h2 class="h5 mt-2">${esc(item.name)}</h2><p class="text-muted">${esc(item.kind)} · Confidence ${confidenceText(item.confidence)}</p><button class="btn btn-outline-success mt-auto" data-expert-case="${item.id}">Review case</button></article></div>`).join('');
    $('#expertEmpty').hidden = pending.length > 0;
    $$('[data-expert-case]', $('#expertCases')).forEach((button) => {
      const item = pending.find((entry) => String(entry.id) === button.dataset.expertCase);
      if (item?.status === 'reviewed') { button.textContent = 'View review'; const badge = button.closest('article').querySelector('.badge'); badge.textContent = 'Reviewed'; badge.className = 'badge text-bg-success align-self-start'; }
      else if (item?.status === 'assigned') { const badge = button.closest('article').querySelector('.badge'); badge.textContent = 'Assigned'; }
    });
  }
  $('#expertCases').addEventListener('click', async (event) => {
    const button = event.target.closest('[data-expert-case]'); if (!button) return;
    try { const item = await api.get(`/api/expert/cases/${button.dataset.expertCase}`); state.activeCase = item; await renderExpertCase(item); }
    catch (error) { showError(error); }
  });
  async function renderExpertCase(item) {
    const panel = $('#expertCaseDetail'); panel.hidden = false;
    const weather = listValue(item.weather), risk = item.risk_level || '—';
    panel.innerHTML = `<div class="d-flex justify-content-between"><div><span class="badge text-bg-light">${esc(item.kind)}</span><h2 class="h4 mt-2">${esc(item.name)}</h2></div><button class="btn-close" id="closeExpertCase" aria-label="Close case"></button></div><p>AI confidence: ${confidenceText(item.confidence)} · Risk: ${esc(risk)}</p><div class="alert alert-info">The backend does not currently expose stored upload images to expert clients. Image preview is unavailable through the implemented API.</div><h3 class="h6">Weather at scan</h3><p>${weather && Object.keys(weather).length ? esc(JSON.stringify(weather)) : 'Weather data unavailable.'}</p><h3 class="h6">Farmer note</h3><p>${esc(item.request_note || 'No note provided.')}</p><form id="expertReviewForm"><label class="form-label" for="expertResponse">Review and guidance</label><textarea id="expertResponse" class="form-control mb-3" rows="5" maxlength="4000" required placeholder="Provide clear, practical guidance. Refer the farmer to local agricultural services where needed."></textarea><button class="btn btn-success">Submit review</button></form>`;
    panel.querySelector('.alert.alert-info')?.remove();
    const scanImage = document.createElement('img'); scanImage.id = 'expertScanImage'; scanImage.className = 'img-fluid rounded mb-3'; scanImage.alt = 'Farmer crop scan'; scanImage.hidden = true; scanImage.style.maxHeight = '360px';
    panel.insertBefore(scanImage, panel.children[1] || null);
    await loadPrivateImage(`/api/scans/${item.scan_id}/image`, scanImage);
    const factors = listValue(item.risk_reasons);
    if (factors.length) panel.insertAdjacentHTML('beforeend', `<h3 class="h6">Risk factors</h3><ul>${factors.map((factor) => `<li>${esc(factor)}</li>`).join('')}</ul>`);
    const previousScans = item.previous_scans || [];
    panel.insertAdjacentHTML('beforeend', `<h3 class="h6 mt-3">Previous crop scans</h3>${previousScans.length ? `<ul>${previousScans.map((scan) => `<li>${esc(dateText(scan.created_at))}: ${esc(scan.kind)} — ${esc(scan.name)}, confidence ${confidenceText(scan.confidence)}, risk ${esc(scan.risk_level || '—')}</li>`).join('')}</ul>` : '<p class="small text-muted">No previous scans for this crop.</p>'}`);
    if (item.status === 'reviewed') {
      $('#expertReviewForm')?.remove();
      panel.insertAdjacentHTML('beforeend', `<h3 class="h6">Submitted expert review</h3><p>${esc(item.response || 'No review text recorded.')}</p><small class="text-muted">${esc(dateText(item.reviewed_at))}</small>`);
    }
    $('#closeExpertCase').addEventListener('click', () => { panel.hidden = true; });
    $('#expertReviewForm').addEventListener('submit', async (event) => {
      event.preventDefault();
      try { await api.post(`/api/expert/cases/${item.id}/review`, { response: $('#expertResponse').value.trim() }); toast('Expert review submitted.', 'success'); panel.hidden = true; await loadExpert(); }
      catch (error) { showError(error); }
    });
    panel.scrollIntoView({ behavior: 'smooth' });
  }

  async function loadAdmin() {
    const [dashboard, farmers, scans] = await Promise.all([api.get('/api/admin/dashboard'), api.get('/api/admin/farmers'), api.get('/api/admin/scans')]);
    $('#adminMetrics').innerHTML = [['Farmers', dashboard.farmers], ['Crops', dashboard.crops], ['Saved scans', dashboard.scans], ['Pending reviews', dashboard.pending_reviews]].map(([label, value]) => `<div class="col-6 col-lg-3"><div class="card stat-card p-3"><div class="text-muted">${esc(label)}</div><div class="stat-number">${Number(value) || 0}</div></div></div>`).join('');
    $('#adminFarmers').innerHTML = farmers.map((f) => `<tr><td>${esc(f.full_name || '—')}</td><td>${esc(f.email)}</td><td>${esc(f.district || '—')}</td></tr>`).join('');
    $('#adminScans').innerHTML = scans.slice(0, 30).map((s) => `<tr><td>${esc(dateText(s.created_at))}</td><td>${esc(s.name)}</td><td>${esc(s.kind)}</td><td>${esc(s.risk_level || '—')}</td></tr>`).join('');
    $('#adminFarmersEmpty').hidden = farmers.length > 0; $('#adminScansEmpty').hidden = scans.length > 0;
    const levels = ['HIGH', 'MEDIUM', 'LOW'];
    const riskCounts = dashboard.risk_distribution || {};
    const detectionCounts = dashboard.detection_distribution || [];
    const reviewCounts = dashboard.review_status || {};
    const recentActivity = dashboard.recent_activity || [];
    $('#adminAnalytics p').textContent = 'Aggregated from stored scans, expert cases, and audit events.';
    $('#analyticsSummary').innerHTML = `<div class="w-100"><strong>Risk distribution</strong><div class="d-flex gap-3 flex-wrap mt-2">${levels.map((level) => `<span class="risk-pill ${riskClass(level)}">${level}: ${Number(riskCounts[level]) || 0}</span>`).join('')}</div><strong class="d-block mt-3">Stored detection classes</strong><div class="d-flex gap-2 flex-wrap mt-2">${detectionCounts.length ? detectionCounts.map((item) => `<span class="badge text-bg-light">${esc(item.kind)} · ${esc(item.name)}: ${Number(item.total) || 0}</span>`).join('') : '<span class="text-muted">No data yet.</span>'}</div><strong class="d-block mt-3">Expert review status</strong><div class="d-flex gap-2 flex-wrap mt-2">${Object.keys(reviewCounts).length ? Object.entries(reviewCounts).map(([status, count]) => `<span class="badge text-bg-light">${esc(status)}: ${Number(count) || 0}</span>`).join('') : '<span class="text-muted">No data yet.</span>'}</div><strong class="d-block mt-3">Recent activity</strong>${recentActivity.length ? `<ul class="small mt-2">${recentActivity.map((event) => `<li>${esc(dateText(event.created_at))} · ${esc(event.action)} · ${esc(event.entity_type)} ${esc(event.entity_id || '')}</li>`).join('')}</ul>` : '<p class="small text-muted mt-2">No data yet.</p>'}</div>`;
    await loadKnowledge();
  }

  async function loadKnowledge() {
    const kind = $('#knowledgeKind').value; state.knowledge = await api.get(`/api/${kind}`);
    $('#knowledgeSelect').innerHTML = state.knowledge.map((entry) => `<option value="${entry.id}">${esc(entry.name)}</option>`).join('');
    $('#knowledgeList').innerHTML = state.knowledge.length ? state.knowledge.map((item) => `<div class="col-md-6"><div class="border rounded p-3"><strong>${esc(item.name)}</strong><div class="small text-muted">${esc(item.scientific_name || 'Scientific name not provided')}</div></div></div>`).join('') : '<p class="text-muted">No data yet.</p>';
  }
  $('#knowledgeKind').addEventListener('change', () => loadKnowledge().catch(showError));
  $('#newKnowledgeBtn').addEventListener('click', () => {
    $('#knowledgeForm').reset(); $('#knowledgeId').value = ''; $('#knowledgeForm').hidden = false; $('#knowledgeName').focus();
  });
  $('#editKnowledgeBtn').addEventListener('click', () => {
    const item = state.knowledge.find((entry) => String(entry.id) === $('#knowledgeSelect').value); if (!item) return toast('Choose a knowledge entry first.', 'info');
    $('#knowledgeId').value = item.id; $('#knowledgeName').value = item.name || ''; $('#knowledgeScientific').value = item.scientific_name || '';
    $('#knowledgeSymptoms').value = listValue(item.symptoms).join('\n'); $('#knowledgePrevention').value = listValue(item.prevention).join('\n');
    $('#knowledgeCultural').value = listValue(item.cultural_management).join('\n'); $('#knowledgeBiological').value = listValue(item.biological_management).join('\n'); $('#knowledgeChemical').value = listValue(item.chemical_management).join('\n');
    $('#knowledgeForm').hidden = false;
  });
  $('#cancelKnowledgeBtn').addEventListener('click', () => { $('#knowledgeForm').hidden = true; });
  $('#knowledgeForm').addEventListener('submit', async (event) => {
    event.preventDefault(); const kind = $('#knowledgeKind').value, id = $('#knowledgeId').value;
    const lines = (selector) => $(selector).value.split('\n').map((x) => x.trim()).filter(Boolean);
    const body = { name: $('#knowledgeName').value.trim(), scientific_name: $('#knowledgeScientific').value.trim(), symptoms: lines('#knowledgeSymptoms'), prevention: lines('#knowledgePrevention'), cultural_management: lines('#knowledgeCultural'), biological_management: lines('#knowledgeBiological'), chemical_management: lines('#knowledgeChemical') };
    try { if (id) await api.put(`/api/admin/${kind}/${id}`, body); else await api.post(`/api/admin/${kind}`, body); $('#knowledgeForm').hidden = true; await loadKnowledge(); toast('Knowledge entry saved.', 'success'); }
    catch (error) { showError(error); }
  });
  $('#deleteKnowledgeBtn').addEventListener('click', async () => {
    const kind = $('#knowledgeKind').value, id = $('#knowledgeSelect').value, item = state.knowledge.find((entry) => String(entry.id) === id);
    if (!item || !confirm(`Delete the ${item.name} knowledge entry?`)) return;
    try { await api.delete(`/api/admin/${kind}/${id}`); await loadKnowledge(); toast('Knowledge entry deleted.', 'success'); }
    catch (error) { showError(error); }
  });

  async function restoreSession() {
    if (!token()) { renderNav(); navigate(); return; }
    try {
      state.user = await api.get('/api/auth/me'); renderNav();
      if (sessionStorage.getItem('cropguard.passwordResetPending') === '1') {
        $('#loginForm').hidden = true; $('#passwordResetForm').hidden = true; $('#updatePasswordForm').hidden = false;
        location.hash = '#login'; navigate(); return;
      }
      if (['', '#landing', '#login', '#register'].includes(location.hash)) location.hash = `#${roleHome()}`; else navigate();
    }
    catch (_) { sessionStorage.removeItem(TOKEN_KEY); state.user = null; renderNav(); location.hash = '#login'; toast('Please log in to continue.', 'info'); }
  }
  function captureRecoverySession() {
    const values = new URLSearchParams((location.hash || '').slice(1));
    if (values.get('type') !== 'recovery' || !values.get('access_token')) return;
    sessionStorage.setItem(TOKEN_KEY, values.get('access_token'));
    if (values.get('refresh_token')) sessionStorage.setItem(REFRESH_KEY, values.get('refresh_token'));
    sessionStorage.setItem('cropguard.passwordResetPending', '1');
    history.replaceState(null, '', `${location.pathname}${location.search}#login`);
  }
  window.addEventListener('hashchange', navigate);
  captureRecoverySession();
  restoreSession();
})();
