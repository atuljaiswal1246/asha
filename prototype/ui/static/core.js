/* Jarvis core UI — talks to jarvis_core.py over /ws and the /api/* endpoints. */
(() => {
  const $ = id => document.getElementById(id);
  const log = $('log'), state = $('state'), modelSel = $('model');
  let ws = null, running = false, models = [];

  function line(text, cls) {
    const d = document.createElement('div');
    d.className = 'msg' + (cls ? ' ' + cls : '');
    d.textContent = text;
    log.appendChild(d);
    log.scrollTop = log.scrollHeight;
    return d;
  }

  async function loadModels() {
    try {
      models = await (await fetch('/api/models')).json();
    } catch { models = []; }
    const saved = localStorage.getItem('jarvis.model') || '';
    modelSel.innerHTML = '';
    const groups = {};
    models.forEach(m => { (groups[m.group || 'Models'] ||= []).push(m); });
    Object.keys(groups).forEach(g => {
      const og = document.createElement('optgroup'); og.label = g;
      groups[g].forEach(m => {
        const o = document.createElement('option');
        o.value = m.id; o.textContent = m.name || m.id;
        if (m.id === saved) o.selected = true;
        og.appendChild(o);
      });
      modelSel.appendChild(og);
    });
    if (!modelSel.value && models[0]) modelSel.value = models[0].id;
  }

  function connect() {
    ws = new WebSocket((location.protocol === 'https:' ? 'wss' : 'ws') + '://' + location.host + '/ws');
    ws.onopen = () => { state.textContent = 'ready'; };
    ws.onclose = () => { state.textContent = 'disconnected'; setRunning(false); setTimeout(connect, 1500); };
    ws.onmessage = e => {
      const m = JSON.parse(e.data);
      if (m.type === 'step') line('→ ' + m.name, 'step');
      else if (m.type === 'token') line(m.text, 'tok');
      else if (m.type === 'done') {
        line('\n' + (m.text || ''), 'done');
        line('[ ' + (m.steps ?? '?') + ' steps · ' + (m.files || []).length + ' file(s) ]', 'step');
        setRunning(false);
      } else if (m.type === 'error') { line('⚠ ' + m.message, 'err'); setRunning(false); }
      else if (m.type === 'status') { state.textContent = m.state; if (m.state === 'idle') setRunning(false); }
    };
  }

  function setRunning(v) { running = v; $('send').disabled = v; $('stop').disabled = !v; }

  function send() {
    const t = $('t'); const text = t.value.trim();
    if (!text || running) return;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    line(text, 'user');
    ws.send(JSON.stringify({ type: 'task', request: text, model: modelSel.value }));
    t.value = ''; setRunning(true);
  }

  $('send').onclick = send;
  $('stop').onclick = () => { if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'cancel' })); };
  $('t').addEventListener('keydown', e => { if (e.key === 'Enter') send(); });
  modelSel.onchange = () => localStorage.setItem('jarvis.model', modelSel.value);

  /* ---- settings ---- */
  const scrim = $('scrim'), panel = $('panel');
  $('settingsBtn').onclick = () => { scrim.classList.add('open'); panel.classList.add('open'); loadSettings(); };
  $('closePanel').onclick = () => { scrim.classList.remove('open'); panel.classList.remove('open'); };
  scrim.onclick = $('closePanel').onclick;

  const KEY_ROWS = [
    ['OPENCODE_API_KEY', 'OpenCode'], ['SUPERVISOR_API_KEY', 'OpenCode Go'],
    ['OPENROUTER_API_KEY', 'OpenRouter'], ['OPENAI_API_KEY', 'OpenAI'],
    ['ANTHROPIC_API_KEY', 'Anthropic'], ['GEMINI_API_KEY', 'Gemini'],
    ['GROQ_API_KEY', 'Groq'], ['XAI_API_KEY', 'xAI'],
  ];

  async function loadSettings() {
    let providers = [];
    try { providers = await (await fetch('/api/providers')).json(); } catch {}
    const configured = pid => !!(providers.find(p => p.provider_id === pid) || {}).configured;
    const keyMap = { OPENCODE_API_KEY: 'opencode', SUPERVISOR_API_KEY: 'opencode-go',
      OPENROUTER_API_KEY: 'openrouter', OPENAI_API_KEY: 'openai', ANTHROPIC_API_KEY: 'anthropic',
      GEMINI_API_KEY: 'gemini', GROQ_API_KEY: 'groq', XAI_API_KEY: 'xai' };
    const box = $('keys'); box.innerHTML = '';
    KEY_ROWS.forEach(([env, label]) => {
      const on = configured(keyMap[env]);
      const row = document.createElement('div'); row.className = 'row';
      row.innerHTML = '<label>' + label + ' <span class="' + (on ? 'set' : 'noset') + '">' +
        (on ? 'set' : 'no key') + '</span></label>';
      const inp = document.createElement('input'); inp.type = 'password'; inp.placeholder = on ? '•••• (replace)' : 'paste key';
      const btn = document.createElement('button'); btn.textContent = 'Save';
      btn.onclick = async () => {
        if (!inp.value.trim()) return;
        btn.disabled = true;
        try { await fetch('/api/key', { method: 'POST', headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ env, value: inp.value.trim() }) }); inp.value = ''; loadSettings(); loadModels(); }
        finally { btn.disabled = false; }
      };
      row.appendChild(inp); row.appendChild(btn); box.appendChild(row);
    });
    // custom providers
    const cbox = $('customs'); cbox.innerHTML = '';
    providers.filter(p => String(p.provider_id).startsWith('custom:')).forEach(p => {
      const c = document.createElement('div'); c.className = 'card';
      const t = document.createElement('div'); t.textContent = p.name + ' — ' + p.base_url;
      const b = document.createElement('button'); b.className = 'ghost'; b.textContent = 'Remove';
      b.onclick = async () => { await fetch('/api/providers/custom/' + p.id, { method: 'DELETE' }); loadSettings(); loadModels(); };
      c.appendChild(t); c.appendChild(b); cbox.appendChild(c);
    });
  }

  $('cpAdd').onclick = async () => {
    const body = { name: $('cpName').value.trim(), base_url: $('cpBase').value.trim(),
      api_key: $('cpKey').value.trim(), models: $('cpModels').value.trim() };
    const st = $('cpStat');
    if (!body.name || !body.base_url) { st.textContent = 'Name and base URL are required.'; return; }
    const r = await fetch('/api/providers/custom', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) });
    const j = await r.json();
    st.textContent = j.error ? ('Error: ' + j.error) : 'Saved.';
    if (!j.error) { ['cpName', 'cpBase', 'cpKey', 'cpModels'].forEach(id => $(id).value = ''); loadSettings(); loadModels(); }
  };

  loadModels().then(connect);
})();
