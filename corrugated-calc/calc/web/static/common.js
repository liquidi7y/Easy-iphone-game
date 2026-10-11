// Общи функции за екраните: заявки с CSRF заглавка, пренасочване към вход, навигация по роля.
const CALC = (() => {
  const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
  let me = null;

  async function api(path, opts = {}) {
    const headers = { 'X-Requested-With': 'calc', ...(opts.json !== undefined ? {'Content-Type':'application/json'} : {}) };
    const method = opts.method || ((opts.json !== undefined || opts.body) ? 'POST' : 'GET');
    const r = await fetch(path, { method, headers, body: opts.json !== undefined ? JSON.stringify(opts.json) : opts.body });
    if (r.status === 401) { location.href = '/login?next=' + encodeURIComponent(location.pathname + location.search); throw new Error('Необходим е вход'); }
    const ct = r.headers.get('content-type') || '';
    const data = ct.includes('json') ? await r.json() : (opts.raw ? r : null);
    if (r.status === 403 && data && data.detail === 'Необходима е смяна на паролата') {
      location.href = '/login?change=1&next=' + encodeURIComponent(location.pathname); throw new Error(data.detail);
    }
    if (!r.ok) throw new Error(data && data.detail ? (typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail)) : r.statusText);
    return opts.raw ? r : data;
  }

  async function blob(path) {
    const r = await fetch(path, { headers: { 'X-Requested-With': 'calc' } });
    if (r.status === 401) { location.href = '/login'; throw new Error('Необходим е вход'); }
    if (!r.ok) throw new Error(r.statusText);
    return r.blob();
  }

  const LINKS = [
    ['/', 'Калкулатор', ['sales','sales_manager','technologist','administrator']],
    ['/quotations', 'Оферти', ['sales','sales_manager','administrator','read_only','technologist']],
    ['/imports', 'Ценоразписи', ['sales','sales_manager','technologist','administrator','read_only']],
    ['/admin', 'Администриране', ['administrator','technologist','sales_manager']],
  ];
  const ROLE = { sales:'Търговец', sales_manager:'Търговски мениджър', technologist:'Технолог', administrator:'Администратор', read_only:'Само четене' };

  async function nav(el) {
    me = await api('/api/v1/me');
    el.innerHTML = LINKS.filter(l => l[2].includes(me.role)).map(([href, label]) =>
      `<a href="${href}" ${location.pathname === href ? 'aria-current="page"' : ''}>${label}</a>`).join('') +
      `<span class="who">${esc(me.display_name)} · ${ROLE[me.role] || me.role}</span><button type="button" class="sec small" id="logoutBtn">Изход</button>`;
    document.getElementById('logoutBtn').onclick = async () => { await api('/api/v1/auth/logout', { json: {} }).catch(() => {}); location.href = '/login'; };
    return me;
  }

  return { api, blob, nav, esc, get me() { return me; } };
})();
