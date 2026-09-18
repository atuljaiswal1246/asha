/* ============================================================
   Asha UI v3 — All JS (ws, mic, render, coding, memory)
   ============================================================ */

window.onerror = function(msg, src, line, col, err) {
  console.log('[JS-ERROR]', msg, src, line);
};

const WS_URL = (() => {
  if (window.JARVIS_WS) return window.JARVIS_WS;
  const https = location.protocol === 'https:';
  // https → wss://host/ws (reverse-proxy friendly); http → ws://host:7860.
  return (https ? 'wss://' : 'ws://') + location.hostname + (https ? '/ws' : ':7860');
})();
let ws = null, ac = null, playCtx = null, stream = null, muted = false, thinkingEl = null, ashaReady = false;
let _liveSessionId = null;

const chatEl = document.getElementById('chat');
const inner = document.getElementById('inner');
const statusEl = document.getElementById('status');
const dotEl = document.getElementById('dot');
const orbGreetingEl = document.getElementById('orbGreeting');
const orbStatusEl = document.getElementById('orbStatus');
// Asha is the single assistant identity: one name, one voice, one color.
const JARVIS_NAME = 'Asha';
const JARVIS_COLOR = '#2fd0ff';
let openBotEl = null, _openBotRaw = '';

/* ---- Status ---- */

function setStatus(state, text) {
  dotEl.className = 'dot ' + (state || '');
  statusEl.textContent = text || state || '';
}

function setOrbStatus(text) {
  const t = text || '';
  if (orbStatusEl) orbStatusEl.textContent = ORB_UI_COPY[t] || t;
  const orb = document.getElementById('orb');
  if (!orb) return;
  let state = 'idle';
  if (t.indexOf('listening') === 0) state = 'listening';
  else if (t.indexOf('speaking') === 0) state = 'speaking';
  else if (t.indexOf('coding') === 0 || t.indexOf('working') === 0) state = 'working';
  else if (t.indexOf('thinking') === 0 || t.indexOf('starting') === 0 || t.indexOf('reconnect') === 0) state = 'thinking';
  orb.classList.remove('state-idle', 'state-listening', 'state-thinking',
                       'state-speaking', 'state-working');
  orb.classList.add('state-' + state);
}

// Status microcopy shown under the orb — human phrases, driven by real state.
const ORB_UI_COPY = {
  'idle': 'tap the mic to talk',
  'listening…': 'I\u2019m listening\u2026',
  'speaking…': 'speaking\u2026',
  'thinking…': 'thinking it through\u2026',
  'coding…': 'on it \u2014 working\u2026',
  'starting up…': 'waking up\u2026',
  'reconnecting…': 'one moment\u2026',
};

function updateSidebarBranding() {
  const title = document.getElementById('sbTitle');
  const logo = document.getElementById('sbLogo');
  const recent = document.getElementById('sbRecentName');
  const orbName = document.getElementById('orbName');
  if (title) title.textContent = JARVIS_NAME;
  // Keep the logo image; only fall back to an initial if there is no image.
  if (logo) {
    logo.title = JARVIS_NAME;
    if (!logo.querySelector('img')) logo.textContent = 'J';
  }
  if (recent) recent.textContent = 'Chat with ' + JARVIS_NAME;
  if (orbName) orbName.textContent = JARVIS_NAME;
}

function updateGreeting() {
  if (!orbGreetingEl) return;
  orbGreetingEl.innerHTML = 'Hi there,<span class="orb-greeting-sub">How can I help you today?</span>';
}

function escapeHtml(s) {
  const d = document.createElement('div');
  d.textContent = s || '';
  return d.innerHTML;
}

function formatTime() {
  const d = new Date();
  const h = d.getHours();
  const m = d.getMinutes().toString().padStart(2, '0');
  const ampm = h >= 12 ? 'pm' : 'am';
  const h12 = h % 12 || 12;
  return h12 + ':' + m + ' ' + ampm;
}

/* ---- Bubbles ---- */

function bubble(role, html) {
  const row = document.createElement('div');
  row.className = 'msg ' + role;

  const wrap = document.createElement('div');
  wrap.className = 'bubble-wrap';

  const b = document.createElement('div');
  b.className = 'bubble';
  b.innerHTML = html;

  const ts = document.createElement('div');
  ts.className = 'ts';
  ts.textContent = formatTime();

  wrap.appendChild(b);
  wrap.appendChild(ts);

  const av = document.createElement('div');
  av.className = 'avatar';
  if (role === 'bot') {
    av.textContent = 'J';
    av.style.background = JARVIS_COLOR;
    row.appendChild(av);
    row.appendChild(wrap);
  } else {
    av.textContent = 'Y';
    row.appendChild(wrap);
    row.appendChild(av);
  }

  inner.appendChild(row);
  chatEl.scrollTop = chatEl.scrollHeight;
  return b;
}

function bubbleCoding(html) {
  const row = document.createElement('div');
  row.className = 'msg bot coding-result';

  const av = document.createElement('div');
  av.className = 'avatar';
  av.textContent = 'J';
  av.style.background = JARVIS_COLOR;

  const wrap = document.createElement('div');
  const label = document.createElement('div');
  label.className = 'coding-label';
  label.textContent = '\u26A1 ' + JARVIS_NAME;
  const b = document.createElement('div');
  b.className = 'bubble';
  b.innerHTML = html;
  const ts = document.createElement('div');
  ts.className = 'ts';
  ts.textContent = formatTime();
  wrap.appendChild(label);
  wrap.appendChild(b);
  wrap.appendChild(ts);
  row.appendChild(av);
  row.appendChild(wrap);
  inner.appendChild(row);
  chatEl.scrollTop = chatEl.scrollHeight;
  return b;
}

function thinking() {
  setBusy(true);
  if (!thinkingEl) {
    const row = document.createElement('div');
    row.className = 'msg bot thinking';
    thinkingEl = row;
    const av = document.createElement('div');
    av.className = 'avatar';
    av.textContent = 'J';
    av.style.background = JARVIS_COLOR;
    const b = document.createElement('div');
    b.className = 'bubble';
    b.textContent = JARVIS_NAME + ' is thinking ';
    const dots = document.createElement('span');
    dots.className = 'thinking-dots';
    for (let i = 0; i < 3; i++) { const d = document.createElement('span'); d.className = 'thinking-dot'; dots.appendChild(d); }
    b.appendChild(dots);
    row.appendChild(av);
    row.appendChild(b);
    inner.appendChild(row);
    chatEl.scrollTop = chatEl.scrollHeight;
  }
}

function stopThinking() {
  if (thinkingEl) { thinkingEl.remove(); thinkingEl = null; }
}

/* ---- Busy state: the send button becomes a stop button while Asha thinks,
   speaks, or works, so the user can always halt it. ---- */
let _busy = false;

function setBusy(on) {
  on = !!on;
  if (on === _busy) return;
  _busy = on;
  const b = document.getElementById('sendBtn');
  if (b) { b.classList.toggle('is-stop', on); b.title = on ? 'Stop' : 'Send'; }
}

function _refreshBusy() { setBusy(!!thinkingEl || playing); }

function stopEverything() {
  // Stop the voice turn (thinking/speaking) and any coding work.
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({type: 'stop'}));
  stopVoiceNow();
  stopThinking();
  clearBrainActivity();
  clearCoding(); clearProposal(); clearPermission();
  setStatus('live', 'ready \u2014 talk to me');
  setOrbStatus(micActive ? 'listening…' : 'idle');
  setBusy(false);
}

// ---- Brain activity (live "what the brain is doing", ChatGPT-style) ----
let brainActEl = null, brainActRaw = '', brainActLabel = null, brainActBody = null, brainActHead = null, brainActClearTimer = null, brainActSteps = null;

function _brainActElement() {
  if (brainActEl) return brainActEl;
  const row = document.createElement('div');
  row.className = 'msg bot brain-activity';
  const av = document.createElement('div');
  av.className = 'avatar';
  av.textContent = 'J';
  av.style.background = JARVIS_COLOR;
  const wrap = document.createElement('div');
  wrap.className = 'bubble-wrap';
  const b = document.createElement('div');
  b.className = 'bubble';

  brainActHead = document.createElement('div');
  brainActHead.className = 'ba-head';
  const spin = document.createElement('span');
  spin.className = 'ba-spin';
  brainActLabel = document.createElement('span');
  brainActLabel.className = 'ba-label';
  brainActHead.appendChild(spin);
  brainActHead.appendChild(brainActLabel);

  brainActBody = document.createElement('div');
  brainActBody.className = 'ba-body';

  brainActSteps = document.createElement('div');
  brainActSteps.className = 'ba-steps';

  b.appendChild(brainActHead);
  b.appendChild(brainActBody);
  b.appendChild(brainActSteps);
  wrap.appendChild(b);
  row.appendChild(av);
  row.appendChild(wrap);
  inner.appendChild(row);
  brainActEl = row;
  chatEl.scrollTop = chatEl.scrollHeight;
  return row;
}

function setBrainActLabel(text, color) {
  if (!brainActLabel) return;
  brainActLabel.textContent = text;
  if (color) brainActLabel.style.color = color;
  else brainActLabel.style.color = '';
}

function showBrainActivity(phase, text, tool, detail) {
  const p = phase || '';
  if (!p) return;
  if (brainActClearTimer) { clearTimeout(brainActClearTimer); brainActClearTimer = null; }
  _brainActElement();
  if (brainActRaw.length >= 6000) brainActRaw = brainActRaw.slice(-2000);
  if (p === 'start') {
    brainActRaw = '';
    if (brainActSteps) brainActSteps.innerHTML = '';
    const d = brainActHead.querySelector('.ba-detail');
    if (d) d.remove();
    setBrainActLabel('Asha is working\u2026', '#6c72e8');
    brainActEl.querySelector('.ba-head').classList.add('answering');
    orbFlash();
  } else if (p === 'reasoning' && text) {
    brainActRaw += text;
    setBrainActLabel('Asha is thinking…');
    brainActBody.textContent = brainActRaw;
    brainActBody.scrollTop = brainActBody.scrollHeight;
    orbFlash();
  } else if (p === 'tool') {
    setBrainActLabel('using ' + (tool || 'a tool') + '…');
    if (brainActSteps) {
      const line = document.createElement('div');
      line.className = 'ba-step';
      line.textContent = '• ' + (tool || 'tool') + (detail ? ' — ' + detail : '');
      brainActSteps.appendChild(line);
      brainActSteps.scrollTop = brainActSteps.scrollHeight;
    } else if (detail) {
      let dEl = brainActHead.querySelector('.ba-detail');
      if (!dEl) {
        dEl = document.createElement('span');
        dEl.className = 'ba-detail';
        brainActHead.appendChild(dEl);
      }
      dEl.textContent = detail;
    }
    brainActEl.querySelector('.ba-head').classList.add('answering');
    orbFlash();
  } else if (p === 'answering') {
    brainActEl.querySelector('.ba-head').classList.add('answering');
    setBrainActLabel('writing answer…', '#4ade80');
    brainActEl.classList.add('answered');
  } else if (p === 'done') {
    brainActEl.classList.add('answered', 'done');
    brainActEl.querySelector('.ba-head').classList.add('done');
    setBrainActLabel('answered', '#4ade80');
    brainActClearTimer = setTimeout(clearBrainActivity, 2500);
  }
  chatEl.scrollTop = chatEl.scrollHeight;
}

function clearBrainActivity() {
  if (brainActClearTimer) { clearTimeout(brainActClearTimer); brainActClearTimer = null; }
  if (brainActEl) { brainActEl.remove(); brainActEl = null; }
  brainActRaw = ''; brainActLabel = null; brainActBody = null; brainActHead = null;
}

function _closeOpenBot() {
  if (!openBotEl) return;
  openBotEl = null;
  _openBotRaw = '';
}

function _appendBotText(text) {
  if (!text) return;
  // Split on sentence boundaries so each sentence gets its own bubble.
  const parts = text.split(/(?<=[.!?])\s+/);
  for (const part of parts) {
    if (!part) continue;
    if (openBotEl && _openBotRaw.length > 0) {
      _closeOpenBot();
    }
    if (!openBotEl) {
      openBotEl = bubble('bot', escapeHtml(part));
      _openBotRaw = part;
    } else {
      _openBotRaw += part;
      openBotEl.innerHTML = escapeHtml(_openBotRaw);
    }
  }
  chatEl.scrollTop = chatEl.scrollHeight;
}

/* ---- Brain badge ---- */

function updateBrainBadge(provider, model) {
  const el = document.getElementById('brainBadge');
  if (!el) return;
  const isCloud = provider !== 'Local';
  el.className = 'badge ' + (isCloud ? 'cloud' : 'local');
  el.textContent = '● ' + provider.toLowerCase();
}

let _lastBrainProvider = 'Local', _lastBrainModel = '';
let _currentBrainTransport = 'opencode';

function showCoding() {
  const el = document.getElementById('brainBadge');
  if (!el) return;
  el.className = 'badge coding';
  el.textContent = '● ' + JARVIS_NAME + ' coding…';
}

function clearCoding() {
  updateBrainBadge(_lastBrainProvider, _lastBrainModel);
}

/* ---- Apply-gate proposal ---- */

let _currentProposalSession = null;

function showProposal(text, sessionId) {
  _currentProposalSession = sessionId;
  document.getElementById('proposalText').textContent = text;
  document.getElementById('proposal').classList.add('show');
  const wkT = document.getElementById('wkProposalText');
  const wkP = document.getElementById('wkProposal');
  if (wkT && wkP) { wkT.textContent = text; wkP.hidden = false; }
}

function clearProposal() {
  _currentProposalSession = null;
  document.getElementById('proposal').classList.remove('show');
  const wkP = document.getElementById('wkProposal');
  if (wkP) wkP.hidden = true;
}

function sendApprove() {
  if (!ws || ws.readyState !== WebSocket.OPEN || !_currentProposalSession) return;
  ws.send(JSON.stringify({type: 'approve', session_id: _currentProposalSession}));
  clearProposal();
}

function sendReject() {
  if (!ws || ws.readyState !== WebSocket.OPEN || !_currentProposalSession) return;
  ws.send(JSON.stringify({type: 'reject', session_id: _currentProposalSession}));
  clearProposal();
}

/* ---- Permission approval ---- */

let _currentPermID = null;

function showPermission(permID, label, detail) {
  _currentPermID = permID;
  document.getElementById('permLabel').textContent = label || 'Agent wants access';
  document.getElementById('permDetail').textContent = detail || '';
  document.getElementById('permission').classList.add('show');
}

function clearPermission() {
  _currentPermID = null;
  document.getElementById('permission').classList.remove('show');
}

function sendPermissionResponse(response, remember) {
  if (!ws || ws.readyState !== WebSocket.OPEN || !_currentPermID) return;
  ws.send(JSON.stringify({type: 'permission_response', permissionID: _currentPermID, response: response, remember: !!remember}));
  clearPermission();
}

/* ---- Memory panel ---- */

let _memoryEntries = [];

function openMemoryPanel() {
  document.getElementById('memoryPanel').classList.add('open');
  renderMemoryEntries();
}

function closeMemoryPanel() {
  document.getElementById('memoryPanel').classList.remove('open');
}

function renderMemoryEntries() {
  const list = document.getElementById('memoryList');
  if (!list) return;
  list.innerHTML = '';
  if (!_memoryEntries.length) {
    list.innerHTML = '<div class="memory-empty">No memories yet.</div>';
    return;
  }
  _memoryEntries.forEach((entry, i) => {
    const div = document.createElement('div');
    div.className = 'memory-entry';
    const hdr = document.createElement('div');
    hdr.className = 'memory-entry-header';
    const kind = document.createElement('span');
    kind.className = 'memory-kind ' + entry.kind;
    kind.textContent = entry.kind === 'user' ? 'about you' : 'shared';
    const acts = document.createElement('div');
    acts.className = 'memory-actions';
    const editBtn = document.createElement('button');
    editBtn.textContent = '\u270E';
    editBtn.title = 'Edit';
    editBtn.onclick = () => { toggleEdit(i); };
    const delBtn = document.createElement('button');
    delBtn.textContent = '\u2715';
    delBtn.title = 'Delete';
    delBtn.style.color = '#f85149';
    delBtn.onclick = () => { deleteMemoryEntry(entry.id); };
    acts.appendChild(editBtn); acts.appendChild(delBtn);
    hdr.appendChild(kind); hdr.appendChild(acts);
    const txt = document.createElement('div');
    txt.className = 'memory-text';
    txt.id = 'memText' + i;
    txt.textContent = entry.text;
    const editRow = document.createElement('div');
    editRow.className = 'memory-edit';
    editRow.id = 'memEdit' + i;
    const editInput = document.createElement('input');
    editInput.type = 'text';
    editInput.value = entry.text;
    editInput.id = 'memInput' + i;
    const saveBtn = document.createElement('button');
    saveBtn.textContent = 'Save';
    saveBtn.onclick = () => { saveMemoryEntry(entry.id, i); };
    editRow.appendChild(editInput); editRow.appendChild(saveBtn);
    div.appendChild(hdr); div.appendChild(txt); div.appendChild(editRow);
    list.appendChild(div);
  });
}

function toggleEdit(i) {
  const row = document.getElementById('memEdit' + i);
  if (row) row.classList.toggle('show');
}

function saveMemoryEntry(id, i) {
  const inp = document.getElementById('memInput' + i);
  if (!inp || !ws || ws.readyState !== WebSocket.OPEN) return;
  ws.send(JSON.stringify({type: 'memory', action: 'edit', id: id, text: inp.value}));
  _memoryEntries[i].text = inp.value;
  renderMemoryEntries();
}

function deleteMemoryEntry(id) {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  ws.send(JSON.stringify({type: 'memory', action: 'delete', id: id}));
  _memoryEntries = _memoryEntries.filter(e => e.id !== id);
  renderMemoryEntries();
}

function updateMemoryEntries(entries) {
  _memoryEntries = entries || [];
  if (document.getElementById('memoryPanel').classList.contains('open')) {
    renderMemoryEntries();
  }
}

/* ---- Audio / Mic ---- */

function audioToPCM16(buf) {
  const out = new Int16Array(buf.length);
  for (let i = 0; i < buf.length; i++) {
    const s = Math.max(-1, Math.min(1, buf[i]));
    out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
  }
  return out;
}

function sendAudio(f32) {
  if (!ws || ws.readyState !== WebSocket.OPEN || muted || playing) return;
  const g = 2;
  for (let i = 0; i < f32.length; i++) { f32[i] = f32[i] * g; if (f32[i] > 1) f32[i] = 1; else if (f32[i] < -1) f32[i] = -1; }
  const pcm = audioToPCM16(f32), bytes = new Uint8Array(pcm.buffer);
  let binary = ''; const CH = 8192;
  for (let i = 0; i < bytes.length; i += CH) binary += String.fromCharCode.apply(null, bytes.subarray(i, i + CH));
  ws.send(JSON.stringify({type: 'audio', data: btoa(binary)}));
}

let micActive = false, micTrack = null, micProc = null, micSrc = null, micStream = null, manualMicOff = false;
let micMeterFill = null, micMeterNum = null, micMeterWarn = null, micMeterEl = null;
let lastMeterUpdate = 0, highAmbientSince = 0;
let micAudioCount = 0, lastAudioTime = 0, micReacquireAttempts = 0, micLastReacquire = 0;
let micEnergyWindowMax = 0, micEnergyWindowStart = 0, micQuietSince = 0;
let postTTSTimer = null;
let micReacquireTimer = null, micReacquiring = false, micDownLogged = false;

function resetMicContext() {
  if (micTrack) { try { micTrack.onended = null; micTrack.onmute = null; } catch (e) {} micTrack.stop(); micTrack = null; }
  if (micStream) { micStream.getTracks().forEach(t => t.stop()); micStream = null; }
  if (micSrc) { try { micSrc.disconnect(); } catch (e) {} micSrc = null; }
  if (micProc) { try { micProc.disconnect(); micProc.onaudioprocess = null; } catch (e) {} micProc = null; }
  micActive = false;
  _refreshMicButtons();
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({type: 'audio_toggle', enabled: false}));
  document.body.classList.remove('mic-on');
  document.body.classList.remove('voice-mode');
  if (!micMeterEl) micMeterEl = document.getElementById('micMeter');
  if (micMeterEl) { micMeterEl.style.display = 'none'; micMeterEl.classList.remove('show'); }
  highAmbientSince = 0;
  if (micMeterWarn) micMeterWarn.classList.remove('show');
  const orb = document.getElementById('orb');
  if (orb) orb.style.setProperty('--l', '0');
  setStatus('live', 'connected');
  setOrbStatus('idle');
}

function stopMic() {
  manualMicOff = true;
  if (micReacquireTimer) { clearTimeout(micReacquireTimer); micReacquireTimer = null; }
  micAudioCount = 0; lastAudioTime = 0; micReacquireAttempts = 0;
  micEnergyWindowMax = 0; micEnergyWindowStart = 0; micQuietSince = 0;
  if (postTTSTimer) { clearTimeout(postTTSTimer); postTTSTimer = null; }
  resetMicContext();
}

function startMic() {
  if (micActive) return Promise.resolve(true);
  manualMicOff = false;
  return navigator.mediaDevices.getUserMedia({audio: {echoCancellation: true, noiseSuppression: true, autoGainControl: false, channelCount: 1}})
    .then(async s => {
      micActive = true; micStream = s; micTrack = s.getAudioTracks()[0];
      micDownLogged = false;
      _refreshMicButtons();
      micAudioCount = 0; lastAudioTime = performance.now();
      micEnergyWindowMax = 0; micEnergyWindowStart = 0; micQuietSince = 0;
      if (micTrack) {
        // A MediaStreamTrack is bound to the device it was acquired from:
        // unplugging a headset ends/mutes it (or macOS moves the default input)
        // while the UI keeps claiming it is listening. Re-acquire instead.
        micTrack.onended = () => micDeviceChanged('track ended');
        micTrack.onmute = () => micDeviceChanged('track muted');
      }
      if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({type: 'audio_toggle', enabled: true}));
      if (!ac || ac.state === 'closed') ac = new AudioContext({sampleRate: 16000});
      if (ac.state === 'suspended') { try { await ac.resume(); } catch (e) {} }
      micSrc = ac.createMediaStreamSource(s);
      if (micProc) { try { micProc.disconnect(); } catch {} }
      micProc = ac.createScriptProcessor(4096, 1, 1);
      const micSink = ac.createMediaStreamDestination();
      micProc.connect(micSink);
      micProc.onaudioprocess = e => {
        micAudioCount++; lastAudioTime = performance.now();
        const d = e.inputBuffer.getChannelData(0);
        sendAudio(d);
        let s = 0;
        for (let i = 0; i < d.length; i++) s += d[i] * d[i];
        const lvl = Math.min(1, Math.sqrt(s / d.length) * 6);
        trackMicEnergy(s);
        const orb = document.getElementById('orb');
        if (orb) orb.style.setProperty('--l', lvl.toFixed(3));
        const now = performance.now();
        if (now - lastMeterUpdate >= 100) {
          lastMeterUpdate = now;
          if (!micMeterFill) micMeterFill = document.getElementById('micMeterFill');
          if (!micMeterNum) micMeterNum = document.getElementById('micMeterNum');
          if (!micMeterWarn) micMeterWarn = document.getElementById('micMeterWarn');
          if (micMeterFill) {
            const pct = Math.round(lvl * 100);
            micMeterFill.style.width = pct + '%';
            micMeterFill.style.background = lvl < 0.5 ? '#3b5998' : lvl < 0.7 ? '#d29922' : '#3fb950';
          }
          if (micMeterNum) micMeterNum.textContent = Math.round(lvl * 100);
          if (micMeterWarn) {
            if (lvl >= 0.7) {
              if (!highAmbientSince) highAmbientSince = now;
              if (now - highAmbientSince > 3000) micMeterWarn.classList.add('show');
            } else {
              highAmbientSince = 0;
              micMeterWarn.classList.remove('show');
            }
          }
        }
      };
      micSrc.connect(micProc);
      document.body.classList.add('mic-on');
      document.body.classList.add('voice-mode');
      if (!micMeterEl) micMeterEl = document.getElementById('micMeter');
      if (micMeterEl) { micMeterEl.style.display = 'block'; micMeterEl.classList.add('show'); }
      setStatus('live', 'voice mode');
      setOrbStatus('listening…');
      return true;
    })
    .catch(e => {
      console.warn('[MIC] getUserMedia failed:', e && e.name, e && e.message);
      const denied = e && (e.name === 'NotAllowedError' || e.name === 'SecurityError');
      if (denied) {
        // Permission problems need the user; do not spam retries.
        _markMicDown(e && e.name, 'Microphone blocked \u2014 allow it in the browser', 'mic blocked');
        return false;
      }
      // Transient (device busy / not ready): the mic is still OFF, so say so,
      // retry shortly, never dead-end.
      _markMicDown((e && e.name) || 'error', 'mic off \u2014 reconnecting\u2026', 'mic off');
      setTimeout(() => { if (!micActive && !manualMicOff) startMic(); }, 3000);
      return false;
    });
}

/* ---- Mic survives device changes (headphones plugged/unplugged) ---- */

const MIC_DEVICE_DEBOUNCE_MS = 400;

// The track's device can vanish (unplug) or go quiet (mute) with no error at
// all. Stop claiming we're listening at once, then re-acquire after a short
// settle — device changes fire in bursts.
function micDeviceChanged(reason) {
  if (manualMicOff) return;
  if (!micActive && !micStream) return;
  micActive = false;
  _refreshMicButtons();
  if (micReacquireTimer) clearTimeout(micReacquireTimer);
  micReacquireTimer = setTimeout(() => {
    micReacquireTimer = null;
    reacquireMic(reason);
  }, MIC_DEVICE_DEBOUNCE_MS);
}

function reacquireMic(reason) {
  if (manualMicOff || micReacquiring) return;
  _restartMic(reason);
}

// One guarded restart path: tear the old graph down, open a fresh stream and
// re-wire. micReacquiring makes overlapping attempts impossible.
function _restartMic(reason) {
  micReacquiring = true;
  console.log('[MIC] ' + reason + ': re-acquiring microphone');
  hardResetMic();
  manualMicOff = false;
  Promise.resolve(startMic()).finally(() => { micReacquiring = false; });
}

// A dead/unavailable mic must never keep saying "listening". Turn the button
// and status off, and log the reason once (retries continue quietly).
function _markMicDown(reason, statusText, orbText) {
  micActive = false;
  _refreshMicButtons();
  setStatus('warn', statusText);
  setOrbStatus(orbText);
  document.body.classList.remove('mic-on');
  document.body.classList.remove('voice-mode');
  if (!micDownLogged) {
    micDownLogged = true;
    console.warn('[MIC] microphone is OFF (' + reason + '); will keep retrying');
  }
}

if (navigator.mediaDevices && navigator.mediaDevices.addEventListener) {
  navigator.mediaDevices.addEventListener('devicechange', () => micDeviceChanged('device change'));
}

let orbTimer = null;

function orbFlash() {
  const orb = document.getElementById('orb');
  if (orb) { orb.classList.add('active'); clearTimeout(orbTimer); orbTimer = setTimeout(() => orb.classList.remove('active'), 1600); }
}

setInterval(() => {
  if (!micActive && ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({type: 'audio_toggle', enabled: false}));
  if (micActive) micWatchdog();
}, 5000);

/* ---- Mic silence watchdog (ROADMAP #1 + KB-01 route-aware) ---- */

const MIC_SILENCE_MS = 2500;            // no frames at all for this long = real fault
const MIC_QUIET_THRESHOLD = 80;         // sound present → frames are alive
const MIC_ENERGY_WINDOW_MS = 1000;
const MIC_DIGITAL_SILENCE_LEVEL = 1;    // only near-zero = suspicious, not a quiet room
const MIC_DIGITAL_SILENCE_MS = 12000;   // sustained digital silence → gentle nudge
const MIC_MAX_REACQUIRE = 3;            // after this many fast tries, slow down (never give up)
const MIC_REACQUIRE_SLOW_MS = 15000;
const MIC_POSTTTS_CHECK_MS = 2000;

function trackMicEnergy(energy) {
  const now = performance.now();
  if (energy > micEnergyWindowMax) micEnergyWindowMax = energy;
  if (micEnergyWindowStart === 0) micEnergyWindowStart = now;
  if (now - micEnergyWindowStart < MIC_ENERGY_WINDOW_MS) return;
  const windowMax = micEnergyWindowMax;
  micEnergyWindowMax = 0;
  micEnergyWindowStart = 0;
  if (windowMax >= MIC_QUIET_THRESHOLD) {
    // Sound is coming through → the mic is alive; clear any recovery backoff.
    micQuietSince = 0;
    if (micReacquireAttempts > 0) {
      micReacquireAttempts = 0;
      if (orbStatusEl && orbStatusEl.textContent.indexOf('mic') >= 0) setOrbStatus('listening…');
    }
    return;
  }
  // Low energy while frames keep arriving is NORMAL (a quiet room is not a
  // fault) — never recover on it. Only sustained *digital* silence hints the
  // input may be dead, and even then it's a gentle nudge, never a dead-end.
  if (!micActive || playing) return;
  if (!micQuietSince) micQuietSince = now;
  if (now - micQuietSince >= MIC_DIGITAL_SILENCE_MS && windowMax < MIC_DIGITAL_SILENCE_LEVEL) {
    micQuietSince = 0;
    micRecovery('digital-silence');
  }
}

function micRecovery(trigger) {
  if (!micActive || playing) return;
  if (micReacquiring) return;
  if (postTTSTimer) { clearTimeout(postTTSTimer); postTTSTimer = null; }
  const now = performance.now();
  // After a few fast tries, keep going but slower — the user must never have
  // to tap anything (hands-free promise). Never a terminal "tap to restart".
  if (micReacquireAttempts >= MIC_MAX_REACQUIRE
      && now - micLastReacquire < MIC_REACQUIRE_SLOW_MS) {
    return;
  }
  micReacquireAttempts++;
  micLastReacquire = now;
  _restartMic(trigger + ' (attempt ' + micReacquireAttempts + ')');
}

function micWatchdog() {
  const now = performance.now();
  if ((now - lastAudioTime) <= MIC_SILENCE_MS) return;
  micRecovery('fully-dead');
}

function hardResetMic() {
  resetMicContext();
  if (ac && ac.state !== 'closed') { try { ac.close(); } catch (e) {} }
  ac = null;
  micEnergyWindowMax = 0; micEnergyWindowStart = 0; micQuietSince = 0;
}

function schedulePostTTSMicCheck() {
  if (!micActive) return;
  if (postTTSTimer) clearTimeout(postTTSTimer);
  postTTSTimer = setTimeout(() => {
    postTTSTimer = null;
    if (!micActive) return;
    const now = performance.now();
    const framesFresh = (now - lastAudioTime) <= MIC_SILENCE_MS;
    // Only a real fault (frames stopped) warrants recovery; low energy after
    // TTS is just a quiet room.
    if (!framesFresh) micRecovery('post-TTS re-acquire');
  }, MIC_POSTTTS_CHECK_MS);
}

/* ---- Audio playback ---- */

// playing  = a queue chunk is (or is about to be) audible — also gates mic capture
// voiceMuted = Work-only flag: mutes Asha's OUTPUT voice (drop TTS audio).
//              NOT the mic mute (muted) — that one stops SENDING. A separate
//              in-flight guard (speakGen) + current source handle let a reply
//              stop promptly mid-sentence, never leaving the orb stuck.
const playQueue = []; let playing = false, speakGen = 0, curSrc = null;

function voiceMutedActive() { return false; }

function ensurePlayCtx() {
  if (!playCtx || playCtx.state === 'closed') playCtx = new AudioContext({sampleRate: 16000});
  if (playCtx.state === 'suspended') { try { playCtx.resume(); } catch (e) {} }
  return playCtx;
}

function setTTSIdle() {
  playing = false;
  const orb = document.getElementById('orb');
  if (orb) orb.style.setProperty('--t', '0');
  setOrbStatus(micActive ? 'listening…' : 'idle');
  _refreshBusy();
  schedulePostTTSMicCheck();
}

function stopVoiceNow() {
  speakGen++;
  const s = curSrc; curSrc = null;
  if (s) { try { s.onended = null; s.stop(); } catch (e) {} }
  playQueue.length = 0;
  setTTSIdle();
}

function enqueuePlay(sampleRate, b64) {
  if (voiceMutedActive()) { stopVoiceNow(); return; }  // drop audio, stop in-flight
  const ctx = ensurePlayCtx();
  const bin = atob(b64), buf = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i);
  const ab = buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength);
  playQueue.push(ab);
  if (!playing) drain();
}

function drain() {
  if (voiceMutedActive()) { stopVoiceNow(); return; }
  const ab = playQueue.shift();
  if (!ab) {
    setTTSIdle();
    return;
  }
  playing = true;
  setBusy(true);
  setOrbStatus('speaking…');
  const gen = speakGen;
  const ctx = ensurePlayCtx();
  ctx.decodeAudioData(ab.slice(0), d => {
    if (voiceMutedActive()) { stopVoiceNow(); return; }
    if (gen !== speakGen) return;  // muted/stopped while decoding — state already reset
    const src = ctx.createBufferSource(); src.buffer = d;
    if (!bindOrbLevel(src, ctx)) src.connect(ctx.destination);
    curSrc = src;
    src.onended = () => { if (curSrc === src) curSrc = null; drain(); };
    src.start();
  }, () => { if (gen === speakGen) drain(); });
}

// Drive the speaking rings with real TTS amplitude: an analyser on the
// playback source reports live RMS into the orb's --t custom property.
function bindOrbLevel(src, ctx) {
  const orb = document.getElementById('orb');
  if (!orb) return false;
  const ana = ctx.createAnalyser();
  ana.fftSize = 256;
  src.connect(ana);
  ana.connect(ctx.destination);
  const data = new Uint8Array(ana.frequencyBinCount);
  const gen = speakGen;
  function tick() {
    if (gen !== speakGen) return;
    ana.getByteTimeDomainData(data);
    let sum = 0;
    for (let i = 0; i < data.length; i++) { const v = (data[i] - 128) / 128; sum += v * v; }
    const rms = Math.min(1, Math.sqrt(sum / data.length) * 4);
    orb.style.setProperty('--t', rms.toFixed(3));
    requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
  return true;
}

/* ---- File attachments (images, PDFs, text, code) ---- */

const MAX_ATTACH = 3, ATTACH_MAX_DIM = 1568;
let pendingAttach = []; // {dataUrl, w, h, mime, name} — images + docs, max 3

function _isImageMime(m) { return m && m.indexOf('image/') === 0; }

function renderAttachBar() {
  const bar = document.getElementById('attachBar');
  if (!bar) return;
  bar.innerHTML = '';
  pendingAttach.forEach((a, i) => {
    const wrap = document.createElement('div');
    wrap.className = 'attach-thumb-wrap';
    if (_isImageMime(a.mime)) {
      const img = document.createElement('img');
      img.className = 'attach-thumb';
      img.src = a.dataUrl;
      wrap.appendChild(img);
    } else {
      const icon = document.createElement('div');
      icon.className = 'attach-thumb attach-file-icon';
      const ext = (a.name || '').split('.').pop().toUpperCase();
      icon.innerHTML = '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><polyline points="14 2 14 8 20 8"/></svg><span>' + (ext || '?') + '</span>';
      wrap.appendChild(icon);
    }
    const x = document.createElement('button');
    x.className = 'attach-x';
    x.textContent = '×';
    x.title = 'Remove';
    x.onclick = () => { pendingAttach.splice(i, 1); renderAttachBar(); };
    wrap.appendChild(x);
    bar.appendChild(wrap);
  });
  bar.hidden = pendingAttach.length === 0;
}

// Called by Swift after NSOpenPanel returns (JS→Swift bridge).
// Items: [{dataUrl, name, w, h, mime}]
window._onFilePickerResult = function(items) {
  if (!items || !items.length) return;
  for (const it of items) {
    if (pendingAttach.length >= MAX_ATTACH) break;
    if (!it.dataUrl || !it.mime) continue;
    pendingAttach.push({dataUrl: it.dataUrl, w: it.w || 0, h: it.h || 0, mime: it.mime, name: it.name || ''});
  }
  renderAttachBar();
};

function addAttachFile(file) {
  if (pendingAttach.length >= MAX_ATTACH) return;
  if (!file || !file.type) return;
  const url = URL.createObjectURL(file);
  if (file.type.indexOf('image/') === 0) {
    const img = new Image();
    img.onload = () => {
      try {
        const scale = Math.min(1, ATTACH_MAX_DIM / Math.max(img.width, img.height));
        const w = Math.max(1, Math.round(img.width * scale));
        const h = Math.max(1, Math.round(img.height * scale));
        const cv = document.createElement('canvas');
        cv.width = w; cv.height = h;
        cv.getContext('2d').drawImage(img, 0, 0, w, h);
        pendingAttach.push({dataUrl: cv.toDataURL('image/jpeg', 0.85), w, h, mime: file.type, name: file.name || ''});
        renderAttachBar();
      } catch (e) {}
      URL.revokeObjectURL(url);
    };
    img.onerror = () => URL.revokeObjectURL(url);
    img.src = url;
  } else {
    const reader = new FileReader();
    reader.onload = () => {
      pendingAttach.push({dataUrl: reader.result, w: 0, h: 0, mime: file.type, name: file.name || ''});
      renderAttachBar();
    };
    reader.readAsDataURL(file);
    URL.revokeObjectURL(url);
  }
}

function wireAttachments() {
  const btn = document.getElementById('attachBtn');
  const textInput = document.getElementById('textInput');
  const hasBridge = !!(window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.openFilePicker);

  // Native file picker: WKWebView swallows <input type="file"> clicks silently,
  // so we call NSOpenPanel directly via a JS→Swift bridge.
  if (btn && hasBridge) {
    btn.addEventListener('click', () => {
      window.webkit.messageHandlers.openFilePicker.postMessage({multiple: true});
    });
  } else if (btn && !hasBridge) {
    // Fallback for plain browser (non-app): <input type="file"> works fine there.
    const input = document.getElementById('attachInput');
    if (input) {
      btn.addEventListener('click', () => input.click());
      input.addEventListener('change', () => {
        Array.from(input.files || []).forEach(addAttachFile);
        input.value = '';
      });
    }
  }
  if (textInput) {
    // Paste screenshots straight into the input.
    textInput.addEventListener('paste', e => {
      const items = (e.clipboardData && e.clipboardData.items) || [];
      for (const it of items) {
        if (it.kind === 'file' && it.type.indexOf('image/') === 0) {
          addAttachFile(it.getAsFile());
        }
      }
    });
  }
  // Drop ANYWHERE over the app attaches into chat (user requirement).
  const veil = document.getElementById('dropVeil');
  let dragDepth = 0;
  window.addEventListener('dragenter', e => {
    e.preventDefault();
    dragDepth++;
    if (veil) veil.hidden = false;
  });
  window.addEventListener('dragover', e => e.preventDefault());
  window.addEventListener('dragleave', e => {
    e.preventDefault();
    dragDepth = Math.max(0, dragDepth - 1);
    if (dragDepth === 0 && veil) veil.hidden = true;
  });
  window.addEventListener('drop', e => {
    e.preventDefault();
    dragDepth = 0;
    if (veil) veil.hidden = true;
    const files = (e.dataTransfer && e.dataTransfer.files) || [];
    Array.from(files).forEach(addAttachFile);
  });
}

/* ---- Send text ---- */

function sendText() {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  const inp = document.getElementById('textInput');
  let text = (inp.value || '').trim();
  const attach = pendingAttach.splice(0, MAX_ATTACH);
  renderAttachBar();
  const imgs = attach.filter(a => _isImageMime(a.mime));
  const files = attach.filter(a => !_isImageMime(a.mime));
  if (!text && (imgs.length || files.length)) text = files.length ? "Please analyze this file." : "What's in this image?";
  if (!text) return;
  inp.value = '';
  _autosizeInput();
  let html = escapeHtml(text);
  if (imgs.length) {
    html = imgs.map(a => '<img class="bubble-img" src="' + a.dataUrl + '">').join('') + html;
  }
  if (files.length) {
    html = files.map(a => '<div class="attach-bubble-file">' + (a.name || 'file') + '</div>').join('') + html;
  }
  bubble('user', html);
  thinking();
  setOrbStatus('thinking…');
  if (workBrainStatusEl) workBrainStatusEl.classList.add('thinking');
  document.querySelectorAll('.worker-empty').forEach(el => el.classList.add('thinking'));
  ws.send(JSON.stringify({type: 'text', data: text, images: imgs, files: files}));
}

/* ---- Composer: auto-grow + review for long / pasted messages ---- */

const COMPOSER_MAX_H = 200;

function _autosizeInput() {
  const el = document.getElementById('textInput');
  if (!el) return;
  el.style.height = 'auto';
  const h = Math.min(el.scrollHeight, COMPOSER_MAX_H);
  el.style.height = h + 'px';
  el.style.overflowY = el.scrollHeight > COMPOSER_MAX_H ? 'auto' : 'hidden';
  const pill = document.getElementById('inputPill');
  if (pill) pill.classList.toggle('multiline', el.scrollHeight > 44);
  const exp = document.getElementById('expandBtn');
  if (exp) {
    const v = el.value || '';
    exp.hidden = !(v.length > 240 || (v.includes('\n') && v.length > 120));
  }
}

function _openReview() {
  const el = document.getElementById('textInput');
  const overlay = document.getElementById('reviewMessage');
  const ta = document.getElementById('reviewText');
  if (!el || !overlay || !ta) return;
  ta.value = el.value;
  const meta = document.getElementById('reviewMeta');
  const text = el.value || '';
  if (meta) {
    meta.textContent = text.length + ' characters \u00b7 ' +
      text.split('\n').length + ' lines \u00b7 check it came through, then send.';
  }
  overlay.hidden = false;
  overlay.classList.add('open');
  ta.focus();
}

function _closeReview() {
  const overlay = document.getElementById('reviewMessage');
  if (!overlay) return;
  overlay.classList.remove('open');
  overlay.hidden = true;
}

function _pastedFeedback() {
  const el = document.getElementById('textInput');
  if (!el) return;
  _autosizeInput();
  const text = el.value || '';
  const lines = text.split('\n').length;
  // Big / structured paste: open the review modal so the user can verify it.
  if (text.length > 400 && (lines > 3 || text.length > 900)) _openReview();
}

/* ---- Theme: light by default, dark optional (persisted) ---- */
function _setTheme(t) {
  document.documentElement.setAttribute('data-theme', t);
  try { localStorage.setItem('asha.theme', t); } catch (e) {}
  const ic = document.getElementById('themeIcon');
  if (ic) {
    ic.innerHTML = t === 'dark'
      ? '<circle cx="12" cy="12" r="4"/><line x1="12" y1="2" x2="12" y2="4"/><line x1="12" y1="20" x2="12" y2="22"/><line x1="4.9" y1="4.9" x2="6.3" y2="6.3"/><line x1="17.7" y1="17.7" x2="19.1" y2="19.1"/><line x1="2" y1="12" x2="4" y2="12"/><line x1="20" y1="12" x2="22" y2="12"/><line x1="4.9" y1="19.1" x2="6.3" y2="17.7"/><line x1="17.7" y1="6.3" x2="19.1" y2="4.9"/>'
      : '<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/>';
  }
}
(function () {
  let saved = null;
  try { saved = localStorage.getItem('asha.theme'); } catch (e) {}
  _setTheme(saved === 'light' ? 'light' : 'dark');
})();
{
  const tb = document.getElementById('themeToggle');
  if (tb) tb.addEventListener('click', () => {
    const cur = document.documentElement.getAttribute('data-theme') || 'light';
    _setTheme(cur === 'dark' ? 'light' : 'dark');
  });
}

/* ---- WebSocket ---- */

function connect() {
  ws = new WebSocket(WS_URL);
  ws.binaryType = 'arraybuffer';
  ws.onopen = () => {
    ashaReady = false;
    setStatus('warn', 'Asha is loading…');
    setOrbStatus('starting up…');
    document.getElementById('textInput').placeholder = 'Ask ' + JARVIS_NAME + ' anything\u2026';
    updateBrainBadge('Local', '');
    updateGreeting();
    updateSidebarBranding();
    const inner = document.getElementById('inner');
    if (inner) inner.innerHTML = '';
    const ctxPills = document.getElementById('contextPills');
    if (ctxPills) ctxPills.innerHTML = '';
    const fileBar = document.getElementById('attachBar');
    if (fileBar) { fileBar.hidden = true; fileBar.innerHTML = ''; }
    clearCoding(); clearProposal(); clearPermission();
    if (micActive) { ws.send(JSON.stringify({type: 'audio_toggle', enabled: true})); }
  };
  ws.onclose = () => {
    ashaReady = false;
    setStatus('warn', 'reconnecting…');
    setOrbStatus('reconnecting…');
    if (micActive) {
      document.body.classList.remove('voice-mode');
      if (!micMeterEl) micMeterEl = document.getElementById('micMeter');
      if (micMeterEl) { micMeterEl.style.display = 'none'; micMeterEl.classList.remove('show'); }
    }
    setTimeout(connect, 1500);
  };
  ws.onerror = () => { setStatus('warn', 'connection error'); setOrbStatus('error'); };
  ws.onmessage = ev => {
    let m; try { m = JSON.parse(ev.data); } catch { return; }
    if (m.type === 'status') {
      if (m.state === 'ready') {
        ashaReady = true;
        clearCoding(); clearProposal(); clearPermission(); clearBrainActivity();
        document.getElementById('stopBtn').style.display = 'none';
        setBusy(false);
        setStatus('live', _brainOnboarded ? 'ready \u2014 talk to me' : 'follow the highlights to get started');
        setOrbStatus(micActive ? 'listening…' : 'idle');
        // During onboarding the mic stays OFF so Asha isn't hearing the room;
        // it is turned on when the user presses OK on the mic notice.
        if (!micActive && !manualMicOff && _brainOnboarded) { startMic(); }
      } else if (m.state === 'coding') {
        showCoding();
        document.getElementById('stopBtn').style.display = 'grid';
        setBusy(true);
        setStatus('warn', 'background coding…');
        setOrbStatus('coding…');
      } else if (m.state === 'reasoning') {
        setBusy(true);
        setStatus('thinking', 'thinking it through…');
        setOrbStatus('thinking…');
        showBrainActivity('reasoning', '');
      } else {
        ashaReady = false; clearCoding();
        document.getElementById('stopBtn').style.display = 'none';
        setStatus('warn', 'Asha is loading…');
        setOrbStatus('starting up…');
      }
    }
    else if (m.type === 'brain_activity') {
      showBrainActivity(m.phase, m.text, m.tool, m.detail);
    }
    else if (m.type === 'audio') { enqueuePlay(m.sampleRate, m.data); orbFlash(); }
    else if (m.type === 'bot_turn_end') {
      _closeOpenBot();
      clearBrainActivity();
      setBusy(false);
      setOrbStatus(micActive ? 'listening…' : 'idle');
    }
    else if (m.type === 'user_transcript') {
      _closeOpenBot();
      stopThinking(); clearBrainActivity();
      bubble('user', escapeHtml(m.text));
    }
    else if (m.type === 'bot_text' || m.type === 'bot_transcript') {
      stopThinking();
      _appendBotText(m.text);
      orbFlash();
    }
    else if (m.type === 'bot_image') {
      stopThinking();
      _closeOpenBot();
      bubble('bot', '<img class="bubble-img" src="' + escapeHtml(m.url) + '">' + escapeHtml(m.caption || ''));
      orbFlash();
    }
    else if (m.type === 'coding_result') {
      _closeOpenBot();
      stopThinking();
      bubbleCoding(escapeHtml(m.text));
      orbFlash();
    }
    else if (m.type === 'brain') {
      _lastBrainProvider = m.provider || 'Local'; _lastBrainModel = m.model || '';
      if (m.brain_transport) _currentBrainTransport = m.brain_transport;
      updateBrainBadge(_lastBrainProvider, _lastBrainModel);
      _lastBrainErrorSig = null;
    }
    else if (m.type === 'brain_error') {
      chatBrainError(m.kind, m.message, m.model);
    }
    else if (m.type === 'brain_provider_ok') {
      _currentBrainTransport = m.transport || 'opencode';
      _highlightCurrentProvider();
    }
    else if (m.type === 'brain_provider_error') {
      const err = document.getElementById('settingsError');
      if (err) { err.textContent = m.error || 'Failed to switch provider'; err.hidden = false; }
    }
    else if (m.type === 'brain_config') {
      _brainModel = m.model || null;
      _brainModels = m.models || [];
      _brainKeyPresent = !!m.key_present;
      _brainOnboarded = !!m.onboarded;
      _populateBrainModels();
      // Returning/onboarded user: make sure the mic comes on (in case the
      // config arrived after the ready status).
      if (_brainOnboarded && ashaReady && !micActive && !manualMicOff) startMic();
      _evalOnboarding();
    }
    else if (m.type === 'provider_config_ok') {
      if (m.config) {
        if (m.config.brain) { _cfgBrain = m.config.brain; }
        if (m.config.worker) { _cfgWorker = m.config.worker; }
      }
      if (Array.isArray(m.providers)) _cfgProviders = m.providers;
      _renderProviderConfig();
    }
    else if (m.type === 'provider_config_error') {
      const err = document.getElementById('settingsError');
      if (err) { err.textContent = m.error || 'Failed to update providers'; err.hidden = false; }
    }
    else if (m.type === 'provider_key_ok') {
      _setProviderKeyStatus(m.env, true,
        m.validated === true ? 'validated' :
        m.validated === false ? ('saved, ping failed' + (m.detail ? ': ' + m.detail : '')) :
        m.set ? 'saved' : 'removed');
      if (m.env === 'OPENCODE_API_KEY') {
        _hideSetupScreen();
        if (m.validated === null) {
          showToast('Key saved', 'ok');
        } else if (m.validated === false) {
          showToast('Key saved, but the check failed', 'err');
        }
      }
      if (_cpProvider && PROVIDER_ENV[_cpProvider.id] === m.env && m.set) {
        showToast('Connected ' + _cpProvider.name, 'ok');
        _closeConnectProvider();
        if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({type: 'catalog_get'}));
      }
    }
    else if (m.type === 'provider_key_error') {
      _setProviderKeyStatus(m.env, false, m.error || 'failed');
      if (m.env === 'OPENCODE_API_KEY') {
        _setupError(m.error || 'Could not save the key. Please try again.');
        showToast('Could not save key', 'err');
      }
      if (_cpProvider && PROVIDER_ENV[_cpProvider.id] === m.env) {
        const err = document.getElementById('cpError');
        if (err) { err.textContent = m.error || 'Could not save the key.'; err.hidden = false; }
        const btn = document.getElementById('cpContinue');
        if (btn) { btn.disabled = false; btn.textContent = 'Continue'; }
      }
    }
    else if (m.type === 'provider_custom_ok') {
      if (Array.isArray(m.providers)) _cfgProviders = m.providers;
      _renderCustomProviders();
      _renderProviderKeys();
      const stat = document.getElementById('cpStatus');
      if (stat) {
        stat.textContent = 'Saved.'; stat.hidden = false;
        stat.className = 'settings-key-detail ok';
      }
    }
    else if (m.type === 'provider_custom_error') {
      const stat = document.getElementById('cpStatus');
      if (stat) {
        stat.textContent = m.error || 'Failed.'; stat.hidden = false;
        stat.className = 'settings-key-detail err';
      }
    }
    else if (m.type === 'mcp_list') {
      _mcpServers = m.servers || [];
      if (m.featured) _mcpFeatured = m.featured;
      _renderDirectory();
    }
    else if (m.type === 'mcp_apps') {
      _mcpApps = m.apps || [];
      _renderDirectory();
    }
    else if (m.type === 'mcp_registry') {
      _mcpLoading = false;
      if (m.append && _mcpResults) _mcpResults = _mcpResults.concat(m.results || []);
      else _mcpResults = m.results || [];
      _mcpQuery = m.query || '';
      _mcpNext = m.next || '';
      _mcpTop = m.top || 0;
      _renderDirectory();
    }
    else if (m.type === 'mcp_tools') {
      const cards = [...document.querySelectorAll('.mcp-card')];
      const card = cards.find(c => (c.querySelector('.mcp-name') || {}).textContent === m.server);
      const tools = card && card.querySelector('.mcp-tools');
      if (tools) {
        const list = m.tools || [];
        const ok = list.filter(t => !t.error);
        const errs = list.filter(t => t.error);
        tools.textContent = ok.length
          ? ok.map(t => '• ' + (t.name || '') + (t.description ? ' — ' + t.description : '')).join('\n')
          : (errs[0] ? 'error: ' + errs[0].error : 'no tools');
      }
    }
    else if (m.type === 'mcp_error') {
      _mcpNotice(m.error || 'failed', false);
      showToast(m.error || 'MCP error', 'err');
      _renderDirectory();
    }
    else if (m.type === 'connector_guide') {
      _connectorRender(m);
    }
    else if (m.type === 'connector_result') {
      _connectorResult(m);
    }
    else if (m.type === 'board_state') {
      _board = m;
      if (m.project) _boardProject = m.project.id;
      _renderProjects();
      if (m.notice && !_boardNoticeShown) { showToast(m.notice, 'err'); _boardNoticeShown = true; }
    }
    else if (m.type === 'board_error') {
      showToast(m.error || 'Projects error', 'err');
    }
    else if (m.type === 'brain_usage') {
      const el = document.getElementById('settingsUsageText');
      const wrap = document.getElementById('settingsUsage');
      if (el && wrap) {
        const turns = m.turns != null ? m.turns : 0;
        const cost = m.cost_usd != null ? m.cost_usd : 0;
        el.textContent = turns + ' turns \u00b7 $' + cost.toFixed(4);
        wrap.hidden = false;
      }
    }
    else if (m.type === 'work_sessions') {
      const list = m.list || [];
      // clear all slots first
      for (let i = 0; i < SLOT_COUNT; i++) _clearSlot(i);
      // assign sessions to slots (max 3)
      const toAssign = list.slice(0, SLOT_COUNT);
      for (let i = 0; i < toAssign.length; i++) {
        const s = toAssign[i];
        _assignSlot(i, s.id, s.name, s.agent);
      }
    }
    else if (m.type === 'recent_sessions') {
      renderRecentSessions(m.list || []);
    }
    else if (m.type === 'session_messages') {
      renderSessionHistory(m.messages || []);
    }
    else if (m.type === 'projects') {
      projectList = m.list || [];
      projectCurrent = m.current || '';
      const el = document.getElementById('workProjectName');
      if (el) {
        el.textContent = projectCurrent ? _projBase(projectCurrent) : 'none';
        el.title = projectCurrent || '';
      }
      const chip = document.getElementById('projectBtnName');
      if (chip) chip.textContent = projectCurrent ? _projBase(projectCurrent) : 'Select project';
      const chipBtn = document.getElementById('projectBtn');
      if (chipBtn) chipBtn.classList.toggle('is-empty', !projectCurrent);
      _renderProjects();
    }
    else if (m.type === 'fs_listing') {
      _renderFsListing(m);
    }
    else if (m.type === 'fs_native_result') {
      const wasAdd = _projectNativePending;
      _projectNativePending = false;
      if (m.path) { _sendProject(m.path); }
      else if (m.error) {
        if (wasAdd) {
          _ppShowBrowse();   // native dialog unavailable → in-app browser
        } else {
          const dl = document.getElementById('projectDirList');
          if (dl) dl.innerHTML = '<div class="project-dir-empty">' + escapeHtml(m.error) + '</div>';
        }
      }
    }
    else if (m.type === 'workers') {
      workerConfigs = m.list || [];
      _renderWorkerConfigs();
    }
    else if (m.type === 'agents') {
      renderAgents(m);
    }
    else if (m.type === 'catalog') {
      catalog = m.providers || [];
    }
    else if (m.type === 'project_gate') {
      showToast(m.message || 'Pick a project to start coding');
      _openProjectPicker(document.getElementById('projectBtn') || document.getElementById('workProjectRow'));
    }
    else if (m.type === 'worker_activity') {
      const sid = m.worker_id;
      const s = slots.find(x => x.sessionId === sid);
      if (s) {
        s.activity = m.activity || [];
        s.status = m.status || s.status;
        s.el.querySelector('.worker-dot').className = 'worker-dot ' + s.status;
        if (m.title) { s.name = m.title; s.el.querySelector('.worker-name').textContent = m.title; }
        if (m.agent) { s.agent = m.agent; s.el.querySelector('.worker-agent').textContent = m.agent; }
        _renderFeed(s);
      }
    }
    else if (m.type === 'worker_update') {
      updateWorkerStatus(m.worker_id, m.status);
    }
    else if (m.type === 'worker_permission') {
      showWorkerPermission(m.worker_id, m.action, m.target);
    }
    else if (m.type === 'worker_permission_clear') {
      hideWorkerPermission(m.worker_id);
    }
    else if (m.type === 'brain_status') {
      if (workBrainStatusEl) {
        workBrainStatusEl.textContent = m.text || 'idle';
        workBrainStatusEl.classList.remove('thinking');
      }
      if (m.text && m.text !== 'idle') {
        document.querySelectorAll('.worker-empty').forEach(el => el.classList.remove('thinking'));
      }
    }
    else if (m.type === 'agent_part') { /* opencode handles its own timeline */ }
    else if (m.type === 'stop') {
      clearCoding(); clearProposal(); clearPermission();
      document.getElementById('stopBtn').style.display = 'none';
      setOrbStatus(micActive ? 'listening…' : 'idle');
      if (workBrainStatusEl) workBrainStatusEl.classList.remove('thinking');
      document.querySelectorAll('.worker-empty').forEach(el => el.classList.remove('thinking'));
    }
    else if (m.type === 'proposal') { showProposal(m.text || '', m.session_id || ''); }
    else if (m.type === 'memory') { updateMemoryEntries(m.entries || []); }
    else if (m.type === 'permission') { showPermission(m.permissionID || '', m.label || '', m.detail || ''); }
  };
}

setInterval(() => { if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({type: 'ping'})); }, 15000);

/* ---- Event bindings ---- */

function _toggleMic() {
  if (!ashaReady) { setStatus('warn', 'waiting for Asha to be ready…'); return; }
  micActive ? stopMic() : startMic();
  _refreshMicButtons();
}
function _refreshMicButtons() {
  const on = !!micActive;
  const cm = document.getElementById('composerMic');
  if (cm) cm.classList.toggle('on', on);
  const mb = document.getElementById('micBtn');
  if (mb) mb.classList.toggle('on', on);
}
document.getElementById('micBtn').onclick = _toggleMic;
{
  const cm = document.getElementById('composerMic');
  if (cm) cm.onclick = _toggleMic;
}
document.getElementById('stopBtn').onclick = stopEverything;
document.getElementById('proposalApprove').onclick = sendApprove;
document.getElementById('proposalReject').onclick = sendReject;
document.getElementById('permAllow').onclick = () => sendPermissionResponse('allow', false);
document.getElementById('permDeny').onclick = () => sendPermissionResponse('deny', false);
document.getElementById('memoryClose').onclick = closeMemoryPanel;
document.getElementById('memoryPanel').onclick = (e) => { if (e.target.id === 'memoryPanel') closeMemoryPanel(); };
document.getElementById('sendBtn').onclick = () => { if (_busy) stopEverything(); else sendText(); };
const _textInputEl = document.getElementById('textInput');
_textInputEl.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendText(); }
});
_textInputEl.addEventListener('input', _autosizeInput);
_textInputEl.addEventListener('paste', () => setTimeout(_pastedFeedback, 0));

document.getElementById('expandBtn').onclick = _openReview;
document.getElementById('reviewClose').onclick = _closeReview;
document.getElementById('reviewCancel').onclick = _closeReview;
document.getElementById('reviewPush').onclick = () => {
  const ta = document.getElementById('reviewText');
  const el = document.getElementById('textInput');
  if (el && ta) { el.value = ta.value; _autosizeInput(); }
  _closeReview();
  sendText();
};
document.getElementById('reviewMessage').addEventListener('click', e => {
  if (e.target.id === 'reviewMessage') _closeReview();
});
document.addEventListener('keydown', e => {
  const ov = document.getElementById('reviewMessage');
  if (e.key === 'Escape' && ov && !ov.hidden) _closeReview();
});
wireAttachments();

/* ---- Project selector (Brain + workers share one active project) ---- */

let projectList = [];
let projectCurrent = '';

function _projBase(p) {
  try { return p.split('/').filter(Boolean).pop() || p; } catch (_) { return p || ''; }
}

function _sendProject(path) {
  path = (path || '').trim();
  if (!path) return;
  _closeProjectPicker();
  if (!(ws && ws.readyState === WebSocket.OPEN)) return;
  if (_projectTarget && _projectTarget.kind === 'worker') {
    ws.send(JSON.stringify({type: 'worker_set', index: _projectTarget.index, project: path}));
    _setWorkerLocal(_projectTarget.index, { project: path, project_name: _projBase(path) });
  } else {
    ws.send(JSON.stringify({type: 'project_set', path}));
  }
}

let _fsCurrent = '', _fsParent = '';
let _projectNativePending = false;

function _fsGo(path) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({type: 'fs_list', path: path || ''}));
  }
  const dl = document.getElementById('projectDirList');
  if (dl) dl.innerHTML = '<div class="project-dir-empty">Loading\u2026</div>';
}

function _renderFsListing(m) {
  _fsCurrent = m.path || '';
  _fsParent = m.parent || '';
  const crumb = document.getElementById('projectCrumb');
  if (crumb) { crumb.textContent = _fsCurrent || '~'; crumb.title = _fsCurrent; }
  const up = document.getElementById('projectUpBtn');
  if (up) up.disabled = !_fsParent;
  const dl = document.getElementById('projectDirList');
  if (!dl) return;
  dl.innerHTML = '';
  if (m.error) {
    const e = document.createElement('div');
    e.className = 'project-dir-empty';
    e.textContent = m.error;
    dl.appendChild(e);
    return;
  }
  if (!m.entries || !m.entries.length) {
    const e = document.createElement('div');
    e.className = 'project-dir-empty';
    e.textContent = 'No subfolders here \u2014 you can still use this folder.';
    dl.appendChild(e);
    return;
  }
  for (const ent of m.entries) {
    const row = document.createElement('div');
    row.className = 'project-dir-item';
    const icon = document.createElement('span');
    icon.className = 'project-dir-icon';
    icon.innerHTML = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>';
    const nm = document.createElement('span');
    nm.className = 'project-dir-name';
    nm.textContent = ent.name;
    row.appendChild(icon);
    row.appendChild(nm);
    if (ent.git) {
      const g = document.createElement('span');
      g.className = 'project-git-badge';
      g.textContent = 'git';
      row.appendChild(g);
    }
    row.onclick = () => _fsGo(ent.path);
    dl.appendChild(row);
  }
  _positionProjectPicker();
}

function _projAvatar(name) {
  const palette = ['#6c72e8', '#e25555', '#d29922', '#3fb950', '#4c9be8', '#a05ad6', '#e07b39', '#2fa39a'];
  const s = String(name || '?');
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
  return palette[h % palette.length];
}

function _renderProjects(query) {
  const list = document.getElementById('projectList');
  if (!list) return;
  const q = (query || '').trim().toLowerCase();
  const items = (projectList || []).filter(p =>
    !q || (p.name || '').toLowerCase().includes(q) || (p.worktree || '').toLowerCase().includes(q));
  list.innerHTML = '';
  if (!items.length) {
    const e = document.createElement('div');
    e.className = 'pp-empty';
    e.textContent = q ? 'No matching projects' : 'No projects yet \u2014 add one';
    list.appendChild(e);
  } else {
    for (const p of items) {
      const row = document.createElement('div');
      row.className = 'pp-item' + (p.worktree === projectCurrent ? ' sel' : '');
      row.title = p.worktree || '';
      const av = document.createElement('span');
      av.className = 'pp-avatar';
      av.textContent = (p.name || '?').trim().charAt(0).toUpperCase() || '?';
      av.style.background = _projAvatar(p.name || p.worktree);
      const nm = document.createElement('span');
      nm.className = 'pp-name';
      nm.textContent = p.name || _projBase(p.worktree);
      row.appendChild(av);
      row.appendChild(nm);
      if (p.worktree === projectCurrent) {
        const ck = document.createElement('span');
        ck.className = 'pp-check';
        ck.innerHTML = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>';
        row.appendChild(ck);
      }
      row.onclick = () => _sendProject(p.worktree);
      list.appendChild(row);
    }
  }
  _positionProjectPicker();
}

function _ppShowList() {
  const lv = document.getElementById('ppListView');
  const bv = document.getElementById('ppBrowseView');
  if (lv) lv.hidden = false;
  if (bv) bv.hidden = true;
  const search = document.getElementById('projectSearch');
  if (search) { search.value = ''; search.focus(); }
  _renderProjects('');
}

function _ppShowBrowse() {
  const lv = document.getElementById('ppListView');
  const bv = document.getElementById('ppBrowseView');
  if (lv) lv.hidden = true;
  if (bv) bv.hidden = false;
  _fsGo(projectCurrent || '');
  _positionProjectPicker();
}

let _projectAnchor = null;
let _projectTarget = { kind: 'brain' };

function _positionProjectPicker() {
  const p = document.getElementById('projectPicker');
  if (!p || p.hidden || !_projectAnchor) return;
  const r = _projectAnchor.getBoundingClientRect();
  const pw = p.offsetWidth || 340;
  const ph = p.offsetHeight || 430;
  const below = r.top < window.innerHeight / 2;
  let left = Math.max(10, Math.min(r.left, window.innerWidth - pw - 10));
  let top = below ? (r.bottom + 8) : (r.top - ph - 8);
  if (top < 10) top = Math.min(r.bottom + 8, window.innerHeight - ph - 10);
  if (top + ph > window.innerHeight - 10) top = Math.max(10, window.innerHeight - ph - 10);
  p.style.left = left + 'px';
  p.style.top = top + 'px';
}

function _openProjectPicker(anchor, target) {
  const p = document.getElementById('projectPicker');
  if (!p) return;
  if (!p.hidden && _projectAnchor === anchor) { _closeProjectPicker(); return; }
  _projectAnchor = anchor || null;
  _projectTarget = target || { kind: 'brain' };
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({type: 'projects_get'}));
  p.hidden = false;
  _ppShowList();
  _positionProjectPicker();
}

function _closeProjectPicker() {
  const p = document.getElementById('projectPicker');
  if (p) p.hidden = true;
  _projectAnchor = null;
}

function openProjectPanel() {
  const anchor = document.getElementById('projectBtn') || document.getElementById('workProjectRow');
  _openProjectPicker(anchor);
}

{
  const rowEl = document.getElementById('workProjectRow');
  const chatBtn = document.getElementById('projectBtn');
  const openBtn = document.getElementById('projectOpenBtn');
  const pathInput = document.getElementById('projectPathInput');
  const upBtn = document.getElementById('projectUpBtn');
  const useBtn = document.getElementById('projectUseBtn');
  const nativeBtn = document.getElementById('projectNativeBtn');
  const addBtn = document.getElementById('projectAddBtn');
  const backBtn = document.getElementById('projectBackBtn');
  const search = document.getElementById('projectSearch');
  if (rowEl) rowEl.onclick = () => _openProjectPicker(rowEl);
  if (chatBtn) chatBtn.onclick = () => _openProjectPicker(chatBtn);
  if (search) {
    search.addEventListener('input', () => _renderProjects(search.value));
    search.addEventListener('keydown', e => {
      if (e.key === 'Enter') {
        const first = (projectList || []).find(p =>
          !search.value.trim() || (p.name || '').toLowerCase().includes(search.value.trim().toLowerCase()));
        if (first) _sendProject(first.worktree);
      }
    });
  }
  if (addBtn) addBtn.onclick = () => {
    // Add project → open the native folder dialog directly; if it can't run
    // (non-macOS / osascript missing) fall back to the in-app browser.
    _projectNativePending = true;
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({type: 'fs_pick_native'}));
  };
  if (backBtn) backBtn.onclick = _ppShowList;
  if (upBtn) upBtn.onclick = () => { if (_fsParent) _fsGo(_fsParent); };
  if (useBtn) useBtn.onclick = () => _sendProject(_fsCurrent);
  if (nativeBtn) nativeBtn.onclick = () => {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({type: 'fs_pick_native'}));
  };
  if (openBtn && pathInput) {
    const commit = () => _sendProject(pathInput.value);
    openBtn.onclick = commit;
    pathInput.addEventListener('keydown', e => { if (e.key === 'Enter') commit(); });
  }
  document.addEventListener('mousedown', e => {
    const p = document.getElementById('projectPicker');
    if (!p || p.hidden) return;
    if (p.contains(e.target) || (_projectAnchor && _projectAnchor.contains(e.target))) return;
    _closeProjectPicker();
  });
  document.addEventListener('keydown', e => {
    const p = document.getElementById('projectPicker');
    if (e.key === 'Escape' && p && !p.hidden) _closeProjectPicker();
  });
  window.addEventListener('resize', () => {
    const p = document.getElementById('projectPicker');
    if (p && !p.hidden) _positionProjectPicker();
  });
}
document.getElementById('sbMemoryLink').onclick = openMemoryPanel;
document.getElementById('sbSettingsLink').onclick = openSettings;

/* ---- MCP / Directory ---- */
let _mcpServers = [];
let _mcpApps = [];
let _mcpFeatured = [];
let _mcpResults = null;   // null until we load the catalog
let _mcpQuery = '';
let _mcpNext = '';        // registry cursor for the next page
let _mcpTop = 0;          // how many leading results are the curated top set
let _mcpLoading = false;   // true while a catalog search/browse is in flight
let _connectorGuide = null;  // the guide currently open (or null)
let _connectorToken = '';    // the token awaiting an "unverified" save

function openMcpScreen() {
  document.getElementById('mcpScreen').hidden = false;
  _mcpResults = null;
  _mcpLoading = true;
  _renderDirectory();
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'mcp', action: 'apps' }));
    ws.send(JSON.stringify({ type: 'mcp', action: 'get' }));
    if (!_mcpResults) ws.send(JSON.stringify({ type: 'mcp', action: 'browse', query: _mcpQuery, limit: 100 }));
  }
}
function closeMcpScreen() { document.getElementById('mcpScreen').hidden = true; }

function _mcpSend(payload) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'mcp', ...payload }));
}

/* ---- Projects board ---- */
let _board = null;                 // last board_state payload
let _boardProject = '';            // selected Project id
let _boardView = 'board';          // 'board' | 'table'
let _boardSort = { col: 'id', dir: 1 };
let _boardFilters = { text: '', owner: '', priority: '' };
let _boardAllProjects = false;
let _boardTick = null;
let _boardNoticeShown = false;
let _editingCard = null;
try { _boardView = localStorage.getItem('jarvis.boardView') === 'table' ? 'table' : 'board'; } catch (e) {}

function _boardSend(payload) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'mcp', screen: 'projects', ...payload }));
}

function openProjectsScreen() {
  document.getElementById('projectsScreen').hidden = false;
  _boardNoticeShown = false;
  _syncBoardViewChrome();
  _boardSend({ action: 'projects_list', project: _boardProject });
  if (_boardTick) clearInterval(_boardTick);
  _boardTick = setInterval(_tickLive, 1000);
}
function closeProjectsScreen() {
  document.getElementById('projectsScreen').hidden = true;
  const ed = document.getElementById('cardEditor');
  if (ed) ed.hidden = true;
  if (_boardTick) { clearInterval(_boardTick); _boardTick = null; }
}
function _setBoardView(view) {
  _boardView = view === 'table' ? 'table' : 'board';
  try { localStorage.setItem('jarvis.boardView', _boardView); } catch (e) {}
  _syncBoardViewChrome();
  _renderProjects();
}
function _syncBoardViewChrome() {
  document.querySelectorAll('#boardViewToggle .seg-btn').forEach(b => {
    b.classList.toggle('active', (b.dataset.view || '') === _boardView);
  });
  const cols = document.getElementById('boardColumns');
  const tbl = document.getElementById('boardTableWrap');
  if (cols) cols.hidden = _boardView !== 'board';
  if (tbl) tbl.hidden = _boardView !== 'table';
}

function _shortRepo(p) {
  const parts = String(p || '').split('/').filter(Boolean);
  return parts.length > 2 ? '…/' + parts.slice(-2).join('/') : (p || '');
}
function _countsText(counts) {
  const order = ['In progress', 'Waiting on you', 'Backlog', 'Blocked', 'Done'];
  const bits = order.filter(s => counts && counts[s]).map(s => counts[s] + ' ' + s.toLowerCase());
  return bits.length ? bits.join(' · ') : 'no cards';
}
function _elapsedOf(card) {
  const created = Number(card && card.created_at) || 0;
  return Math.max(0, Math.round(Date.now() / 1000 - created));
}
function _fmtElapsed(secs) {
  secs = Math.max(0, Math.round(secs));
  if (secs < 60) return secs + 's';
  const m = Math.floor(secs / 60), s = secs % 60;
  if (m < 60) return m + 'm ' + s + 's';
  return Math.floor(m / 60) + 'h ' + (m % 60) + 'm';
}

function _boardSelected() {
  if (!_board || !_board.projects) return null;
  return _board.projects.find(p => p.id === _boardProject) || _board.project || _board.projects[0] || null;
}
function _cardById(id) {
  const cards = (_board && _board.columns || []).reduce((a, c) => a.concat(c.cards || []), []);
  return cards.find(c => c.id === id) || null;
}
function _matchesFilters(card) {
  const f = _boardFilters;
  if (f.owner && card.owner !== f.owner) return false;
  if (f.priority && card.priority !== f.priority) return false;
  if (f.text) {
    const hay = [card.title, card.notes, card.owner, card.area, card.needs].join(' ').toLowerCase();
    if (hay.indexOf(f.text.toLowerCase()) === -1) return false;
  }
  return true;
}

function _renderProjects() {
  _renderProjectsList();
  _renderFilterChips();
  if (_boardView === 'table') _renderTable();
  else _renderBoardColumns();
}

function _renderProjectsList() {
  const box = document.getElementById('projectsListItems');
  if (!box) return;
  const projects = (_board && _board.projects) || [];
  if (!projects.length) { box.innerHTML = '<div class="projects-empty-mini">No projects yet.</div>'; return; }
  box.innerHTML = projects.map(p => {
    const active = p.id === _boardProject ? ' active' : '';
    const repo = p.repo ? '<div class="projects-item-repo">' + escapeHtml(_shortRepo(p.repo)) + '</div>' : '';
    return '<div class="projects-item' + active + '" data-project="' + escapeHtml(p.id) + '">'
      + '<div class="projects-item-name">' + escapeHtml(p.name) + '</div>' + repo
      + '<div class="projects-item-counts">' + escapeHtml(_countsText(p.counts)) + '</div></div>';
  }).join('');
  box.querySelectorAll('.projects-item').forEach(el => {
    el.onclick = () => {
      _boardProject = el.dataset.project;
      _boardSend({ action: 'board_get', project: _boardProject });
    };
  });
}

function _renderFilterChips() {
  const owners = {};
  const cards = (_board && _board.columns || []).reduce((a, c) => a.concat(c.cards || []), []);
  cards.forEach(c => { if (c.owner) owners[c.owner] = true; });
  const ownerBox = document.getElementById('boardOwnerChips');
  if (ownerBox) {
    ownerBox.innerHTML = Object.keys(owners).sort().map(o =>
      '<button class="projects-chip' + (_boardFilters.owner === o ? ' active' : '')
      + '" data-owner="' + escapeHtml(o) + '" type="button">' + escapeHtml(o) + '</button>').join('');
    ownerBox.querySelectorAll('.projects-chip').forEach(b => {
      b.onclick = () => { _boardFilters.owner = _boardFilters.owner === b.dataset.owner ? '' : b.dataset.owner; _renderProjects(); };
    });
  }
  const priBox = document.getElementById('boardPriorityChips');
  if (priBox) {
    const pris = (_board && _board.priorities) || ['P0', 'P1', 'P2', 'P3'];
    priBox.innerHTML = pris.map(p =>
      '<button class="projects-chip' + (_boardFilters.priority === p ? ' active' : '')
      + '" data-priority="' + escapeHtml(p) + '" type="button">' + escapeHtml(p) + '</button>').join('');
    priBox.querySelectorAll('.projects-chip').forEach(b => {
      b.onclick = () => { _boardFilters.priority = _boardFilters.priority === b.dataset.priority ? '' : b.dataset.priority; _renderProjects(); };
    });
  }
}

function _cardHtml(c) {
  const owner = c.owner ? '<span class="pc-chip pc-owner">' + escapeHtml(c.owner) + '</span>' : '';
  const pri = c.priority ? '<span class="pc-chip pc-pri pc-pri-' + escapeHtml(c.priority) + '">' + escapeHtml(c.priority) + '</span>' : '';
  const area = c.area ? '<span class="pc-area">' + escapeHtml(c.area) + '</span>' : '';
  const needs = c.needs ? '<div class="pc-needs">needs: ' + escapeHtml(c.needs) + '</div>' : '';
  const live = c.live ? '<span class="pc-live" data-created="' + (Number(c.created_at) || 0) + '">● ' + _fmtElapsed(_elapsedOf(c)) + '</span>' : '';
  const statuses = ((_board && _board.statuses) || []).map(s =>
    '<option value="' + escapeHtml(s) + '"' + (s === c.status ? ' selected' : '') + '>' + escapeHtml(s) + '</option>').join('');
  return '<div class="projects-card' + (c.live ? ' is-live' : '') + '" data-card="' + escapeHtml(c.id) + '">'
    + '<div class="pc-top">' + pri + area + live + '</div>'
    + '<div class="pc-title">' + escapeHtml(c.title) + '</div>' + needs
    + '<div class="pc-foot">' + owner
    + '<select class="card-move" data-card="' + escapeHtml(c.id) + '" title="Move to column">' + statuses + '</select>'
    + '</div></div>';
}

function _renderBoardColumns() {
  const box = document.getElementById('boardColumns');
  if (!box) return;
  const cols = (_board && _board.columns) || [];
  const empty = document.getElementById('boardEmpty');
  if (!cols.length) { box.innerHTML = ''; if (empty) { empty.hidden = false; empty.textContent = 'No project selected.'; } return; }
  box.innerHTML = cols.map(col => {
    const cards = (col.cards || []).filter(_matchesFilters);
    return '<div class="projects-col">'
      + '<div class="projects-col-head"><span>' + escapeHtml(col.name) + '</span>'
      + '<span class="projects-col-count">' + cards.length + '</span></div>'
      + '<div class="projects-col-cards">' + cards.map(_cardHtml).join('') + '</div>'
      + '<button class="projects-addcard" data-status="' + escapeHtml(col.name) + '" type="button">+ Add card</button>'
      + '</div>';
  }).join('');
  box.querySelectorAll('.projects-addcard').forEach(b => {
    b.onclick = () => _openCardEditor(null, b.dataset.status);
  });
  box.querySelectorAll('.projects-card').forEach(el => {
    el.onclick = (e) => {
      if (e.target.closest('.card-move, button, select, input')) return;
      _openCardEditor(_cardById(el.dataset.card));
    };
  });
  box.querySelectorAll('.card-move').forEach(sel => {
    sel.onchange = () => _boardSend({ action: 'card_move', card_id: sel.dataset.card, status: sel.value });
  });
}

function _tableRows() {
  if (!_board) return [];
  const sel = _boardSelected();
  let rows = _board.cards || [];
  if (!_boardAllProjects && sel) rows = rows.filter(r => r.project === sel.name);
  rows = rows.filter(r => _matchesFilters({ title: r.task, notes: r.note, owner: r.owner, area: '', needs: '' }));
  const col = _boardSort.col, dir = _boardSort.dir;
  rows = rows.slice().sort((a, b) => {
    let av = a[col] || '', bv = b[col] || '';
    if (col === 'id') { const an = parseInt(String(av).replace(/\D/g, ''), 10) || 0, bn = parseInt(String(bv).replace(/\D/g, ''), 10) || 0; return (an - bn) * dir; }
    return String(av).localeCompare(String(bv)) * dir;
  });
  return rows;
}

function _renderTable() {
  const tbl = document.getElementById('boardTable');
  if (!tbl) return;
  const defs = [['id', '#'], ['task', 'Task'], ['status', 'Status'], ['note', 'Note'], ['owner', 'Owner'], ['priority', 'Priority']];
  const rows = _tableRows();
  const head = defs.map(([k, label]) => {
    const arrow = _boardSort.col === k ? (_boardSort.dir > 0 ? ' ▲' : ' ▼') : '';
    return '<th data-col="' + k + '">' + label + arrow + '</th>';
  }).join('');
  const body = rows.map(r => {
    const statuses = ((_board && _board.statuses) || []).map(s =>
      '<option value="' + escapeHtml(s) + '"' + (s === r.status ? ' selected' : '') + '>' + escapeHtml(s) + '</option>').join('');
    return '<tr data-card="' + escapeHtml(r.id) + '">'
      + '<td class="pt-id">' + escapeHtml(r.id) + '</td>'
      + '<td class="pt-task">' + escapeHtml(r.task) + '</td>'
      + '<td><select class="tbl-status" data-card="' + escapeHtml(r.id) + '">' + statuses + '</select></td>'
      + '<td><input class="tbl-note" data-card="' + escapeHtml(r.id) + '" value="' + escapeHtml(r.note) + '" maxlength="120"></td>'
      + '<td>' + escapeHtml(r.owner) + '</td>'
      + '<td><span class="pc-chip pc-pri pc-pri-' + escapeHtml(r.priority) + '">' + escapeHtml(r.priority) + '</span></td>'
      + '</tr>';
  }).join('');
  tbl.innerHTML = '<thead><tr>' + head + '</tr></thead><tbody>' + body + '</tbody>';
  tbl.querySelectorAll('th').forEach(th => {
    th.onclick = () => {
      const col = th.dataset.col;
      if (_boardSort.col === col) _boardSort.dir *= -1;
      else _boardSort = { col, dir: 1 };
      _renderTable();
    };
  });
  tbl.querySelectorAll('.tbl-status').forEach(sel => {
    sel.onchange = () => _boardSend({ action: 'card_move', card_id: sel.dataset.card, status: sel.value });
  });
  tbl.querySelectorAll('.tbl-note').forEach(inp => {
    inp.onchange = () => _boardSend({ action: 'card_update', card_id: inp.dataset.card, notes: inp.value });
  });
}

function _tickLive() {
  document.querySelectorAll('.pc-live[data-created]').forEach(el => {
    const created = Number(el.dataset.created) || 0;
    el.textContent = '● ' + _fmtElapsed(Math.max(0, Math.round(Date.now() / 1000 - created)));
  });
}

function _openCardEditor(card, presetStatus) {
  _editingCard = card || { status: presetStatus || ((_board && _board.statuses || [])[0] || 'Backlog'), project_id: _boardProject };
  document.getElementById('cardEditorTitle').textContent = card ? 'Edit card' : 'New card';
  document.getElementById('ceTitle').value = card ? (card.title || '') : '';
  document.getElementById('ceArea').value = card ? (card.area || '') : '';
  document.getElementById('ceOwner').value = card ? (card.owner || '') : 'You';
  document.getElementById('ceNeeds').value = card ? (card.needs || '') : '';
  document.getElementById('ceNotes').value = card ? (card.notes || '') : '';
  const pri = document.getElementById('cePriority');
  pri.innerHTML = ((_board && _board.priorities) || ['P0', 'P1', 'P2', 'P3']).map(p =>
    '<option value="' + escapeHtml(p) + '"' + (card && p === card.priority ? ' selected' : '') + '>' + escapeHtml(p) + '</option>').join('');
  const st = document.getElementById('ceStatus');
  st.innerHTML = ((_board && _board.statuses) || []).map(s =>
    '<option value="' + escapeHtml(s) + '"' + ((card ? card.status : _editingCard.status) === s ? ' selected' : '') + '>' + escapeHtml(s) + '</option>').join('');
  document.getElementById('ceStatusMsg').hidden = true;
  document.getElementById('ceDelete').hidden = !card;
  document.getElementById('cardEditor').hidden = false;
  setTimeout(() => document.getElementById('ceTitle').focus(), 20);
}

function _closeCardEditor() {
  document.getElementById('cardEditor').hidden = true;
  _editingCard = null;
}

function _saveCardEditor() {
  const val = id => (document.getElementById(id).value || '').trim();
  const title = val('ceTitle');
  if (!title) { document.getElementById('ceStatusMsg').hidden = false; document.getElementById('ceStatusMsg').textContent = 'A title is required.'; return; }
  const fields = {
    title,
    area: val('ceArea'),
    owner: val('ceOwner'),
    priority: document.getElementById('cePriority').value,
    status: document.getElementById('ceStatus').value,
    needs: val('ceNeeds'),
    notes: val('ceNotes'),
  };
  if (_editingCard && _editingCard.id) _boardSend({ action: 'card_update', card_id: _editingCard.id, ...fields });
  else _boardSend({ action: 'card_add', project: _boardProject, ...fields });
  _closeCardEditor();
}

function _deleteCardEditor() {
  if (_editingCard && _editingCard.id) _boardSend({ action: 'card_remove', card_id: _editingCard.id });
  _closeCardEditor();
}

function _wireProjects() {
  const link = document.getElementById('sbProjectsLink');
  if (link) link.onclick = openProjectsScreen;
  const close = document.getElementById('projectsClose');
  if (close) close.onclick = closeProjectsScreen;
  const screen = document.getElementById('projectsScreen');
  if (screen) screen.onclick = (e) => { if (e.target.id === 'projectsScreen') closeProjectsScreen(); };
  document.querySelectorAll('#boardViewToggle .seg-btn').forEach(b => {
    b.onclick = () => _setBoardView(b.dataset.view);
  });
  const filter = document.getElementById('boardFilterText');
  if (filter) filter.oninput = () => { _boardFilters.text = filter.value.trim(); _renderProjects(); };
  const all = document.getElementById('boardAllProjects');
  if (all) all.onchange = () => { _boardAllProjects = all.checked; _renderProjects(); };
  const add = document.getElementById('projectAddBtn');
  if (add) add.onclick = () => { document.getElementById('projectNewForm').hidden = false; document.getElementById('newProjectName').focus(); };
  const cancelNew = document.getElementById('newProjectCancel');
  if (cancelNew) cancelNew.onclick = () => { document.getElementById('projectNewForm').hidden = true; };
  const saveNew = document.getElementById('newProjectSave');
  if (saveNew) saveNew.onclick = () => {
    const name = (document.getElementById('newProjectName').value || '').trim();
    if (!name) return;
    _boardSend({ action: 'project_add', name, repo: (document.getElementById('newProjectRepo').value || '').trim() });
    document.getElementById('newProjectName').value = '';
    document.getElementById('newProjectRepo').value = '';
    document.getElementById('projectNewForm').hidden = true;
  };
  const ceSave = document.getElementById('ceSave');
  if (ceSave) ceSave.onclick = _saveCardEditor;
  const ceCancel = document.getElementById('ceCancel');
  if (ceCancel) ceCancel.onclick = _closeCardEditor;
  const ceDelete = document.getElementById('ceDelete');
  if (ceDelete) ceDelete.onclick = _deleteCardEditor;
  const ceClose = document.getElementById('cardEditorClose');
  if (ceClose) ceClose.onclick = _closeCardEditor;
}
function _mcpSafeName(raw, fallback) {
  let n = (raw || fallback || 'server').split('/').pop()
    .replace(/[^A-Za-z0-9._-]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 48);
  if (!n) n = 'server';
  const taken = new Set(_mcpServers.map(s => s.name));
  if (taken.has(n)) { let i = 2; while (taken.has(n + '-' + i)) i++; n = n + '-' + i; }
  return n;
}
function _mcpNotice(msg, ok) {
  const st = document.getElementById('mcpStatus');
  if (st) { st.textContent = msg; st.hidden = false;
    st.className = 'settings-key-detail' + (ok ? ' ok' : ' err'); }
}
function _addMcpEntry(entry) {
  if (entry.transport === 'remote' && entry.url) {
    _mcpSend({ action: 'add', name: _mcpSafeName(entry.name, entry.id), url: entry.url, command: '', args: [] });
  } else if (entry.command) {
    _mcpSend({ action: 'add', name: _mcpSafeName(entry.name, entry.id), url: '',
      command: entry.command, args: entry.args || [] });
  } else { showToast('Nothing to add for this connector', 'err'); }
}

/* one directory card: icon, name, description, and its action */
function _dirCard(icon, name, desc, action, statusText, hint) {
  const card = document.createElement('div'); card.className = 'mcp-dir-card';
  if (hint) card.title = hint;
  const ic = document.createElement('div'); ic.className = 'mcp-dir-icon'; ic.textContent = icon || '🔌';
  const body = document.createElement('div'); body.className = 'mcp-dir-body';
  const nm = document.createElement('div'); nm.className = 'mcp-dir-name'; nm.textContent = name;
  const ds = document.createElement('div'); ds.className = 'mcp-dir-desc';
  ds.textContent = desc || '';
  body.appendChild(nm); body.appendChild(ds);
  if (statusText) {
    const st = document.createElement('div'); st.className = 'mcp-dir-status'; st.textContent = statusText;
    body.appendChild(st);
  }
  card.appendChild(ic); card.appendChild(body);
  if (action) card.appendChild(action);
  return card;
}
function _dirSection(grid, title) {
  const h = document.createElement('div'); h.className = 'mcp-dir-head'; h.textContent = title;
  grid.appendChild(h);
}
function _plusButton(label, onClick, disabled) {
  const b = document.createElement('button');
  b.className = 'mcp-dir-add';
  b.textContent = label;
  if (disabled) b.disabled = true; else b.onclick = onClick;
  return b;
}
function _mcpSlug(s) { return (s || '').toLowerCase().replace(/[^a-z0-9]/g, ''); }

/* The curated app a server name / catalog entry belongs to (if any). */
function _mcpAppFor(apps, ...values) {
  const keys = values.filter(Boolean).map(_mcpSlug);
  return apps.find(a => keys.includes(_mcpSlug(a.id)) || keys.includes(_mcpSlug(a.label)));
}

/* A connector that maps to a curated app renders once, on its app card. */
function _mcpOwnedServers(servers, apps) {
  return servers.filter(s => !_mcpAppFor(apps, s.name));
}
function _mcpOwnedCatalog(entries, apps) {
  return entries.filter(r => !_mcpAppFor(apps, r.name, r.id));
}
function _mcpAppAction(a) {
  if (a.auth === 'credential') {
    if (a.connected) {
      return { label: 'Update key', action: 'connector_setup', confirm: false, danger: false,
               status: a.unverified ? 'Saved · not verified' : 'Connected · tools are live',
               hint: 'Open the guided setup to replace ' + a.label + '’s access token.' };
    }
    return { label: 'Set up', action: 'connector_setup', confirm: false, danger: false,
             status: 'Needs an access token',
             hint: 'Open the step-by-step ' + a.label + ' setup.' };
  }
  if (a.connected && a.registered) {
    return { label: 'Disconnect', action: 'disconnect', confirm: true, danger: true,
             status: 'Connected · tools are live',
             hint: 'Disconnect ' + a.label + ' and delete the saved sign-in — you can reconnect later.' };
  }
  if (a.connected && !a.registered) {
    return { label: 'Finish setup', action: 'register', confirm: false, danger: false,
             status: 'Signed in · setup not finished',
             hint: 'Register ' + a.label + '’s tools so Asha can use them.' };
  }
  if (!a.connected && a.registered) {
    return { label: 'Sign in', action: 'connect', confirm: false, danger: false,
             status: 'Set up · not signed in',
             hint: 'Open Google sign-in for ' + a.label + '.' };
  }
  return { label: 'Add', action: 'connect', confirm: false, danger: false,
           status: 'Not connected',
           hint: 'Add ' + a.label + ': opens Google sign-in, then wires its tools.' };
}

function _renderDirectory() {
  const grid = document.getElementById('mcpGrid');
  if (!grid) return;
  grid.innerHTML = '';

  const names = new Set(_mcpServers.map(s => s.name));

  // 1) apps you can sign in to, and servers already wired up
  const mine = [];
  _mcpApps.forEach(a => {
    const spec = _mcpAppAction(a);
    const btn = document.createElement('button');
    btn.className = 'mcp-dir-action' + (spec.danger ? ' mcp-dir-danger' : '');
    btn.textContent = spec.label;
    if (spec.hint) btn.title = spec.hint;
    btn.onclick = () => {
      if (spec.action === 'connector_setup') {
        openConnectorGuide(a.id);
        return;
      }
      if (spec.confirm) {
        if (!window.confirm('Disconnect ' + a.label + '?\n\nThis deletes the saved sign-in and removes the connector. You can reconnect it later.')) {
          return;
        }
        btn.disabled = true;
        btn.textContent = '…';
        _mcpSend({ action: spec.action, connector: a.id, confirm: true });
        return;
      }
      btn.disabled = true;
      btn.textContent = '…';
      _mcpSend({ action: spec.action, connector: a.id });
    };
    mine.push(_dirCard(a.icon, a.label, a.description, btn, spec.status, spec.hint));
  });
  _mcpOwnedServers(_mcpServers, _mcpApps).forEach(s => {
    const desc = s.signed_in ? 'Connected' : (s.url ? 'Remote server' : 'Local server');
    let btn;
    if (s.url && !s.signed_in) {
      btn = _plusButton('+', () => {
        btn.disabled = true; btn.textContent = '…';
        _mcpSend({ action: 'signin', name: s.name });
      });
    } else {
      btn = _plusButton('✓', () => {
        _mcpSend({ action: 'test', name: s.name });
        showToast('Checking ' + s.name + '…', 'ok');
      }, true);
    }
    mine.push(_dirCard(s.url ? '🔗' : '🧩', s.name, desc, btn));
  });
  if (mine.length) {
    _dirSection(grid, 'Your apps');
    mine.forEach(c => grid.appendChild(c));
  }

  // 2) search results, else the curated featured set
  const list = _mcpOwnedCatalog(_mcpResults || _mcpFeatured, _mcpApps);
  if (list.length) {
    const topN = (_mcpResults && _mcpTop) ? _mcpTop : 0;
    _dirSection(grid, topN ? 'Top connectors' : (_mcpResults ? 'Results' : 'Featured'));
    list.forEach((r, i) => {
      if (topN && i === topN) _dirSection(grid, 'All connectors');
      const safe = _mcpSafeName(r.name, r.id);
      const added = names.has(safe);
      const signed = added && _mcpServers.some(s => s.name === safe && s.signed_in);
      const btn = _plusButton((added && (signed || !r.url)) ? '✓' : '+', () => {
        btn.disabled = true; btn.textContent = '…';
        if (r.transport === 'remote' && r.url) {
          _mcpSend({ action: 'add_signin', name: safe, url: r.url });   // add + sign in
        } else {
          _addMcpEntry(r);
        }
      }, added && (signed || !r.url));
      grid.appendChild(_dirCard(r.icon || (r.transport === 'remote' ? '🔗' : '🧩'),
        r.name, r.description, btn));
    });
  } else {
    const e = document.createElement('div');
    e.className = 'mcp-empty';
    if (_mcpLoading) e.textContent = 'Loading connectors…';
    else if (_mcpResults) e.textContent = 'No connectors found for “' + _mcpQuery + '”.';
    else e.textContent = 'No connectors to show yet.';
    grid.appendChild(e);
  }

  if (_mcpResults && (_mcpNext || _mcpResults.length)) {
    const foot = document.createElement('div');
    foot.className = 'mcp-dir-foot';
    const count = document.createElement('span');
    count.textContent = _mcpResults.length + (_mcpNext ? '+' : '') + ' connectors';
    foot.appendChild(count);
    if (_mcpNext) {
      const more = document.createElement('button');
      more.className = 'mcp-dir-showall';
      more.textContent = 'Show all';
      more.onclick = () => { more.disabled = true; more.textContent = 'Loading…'; _mcpShowAll(); };
      foot.appendChild(more);
    }
    grid.appendChild(foot);
  }
}

function _mcpSearch() {
  _mcpQuery = (document.getElementById('mcpSearch')?.value || '').trim();
  _mcpResults = null;
  _mcpLoading = true;
  _renderDirectory();
  _mcpSend({ action: 'search', query: _mcpQuery, limit: 100 });
}
function _mcpShowAll() {
  _mcpLoading = true;
  _mcpSend({ action: 'browse', query: _mcpQuery, cursor: _mcpNext, limit: 100 });
}
document.getElementById('sbMcpLink').onclick = openMcpScreen;
_wireProjects();
document.getElementById('mcpClose').onclick = closeMcpScreen;
document.getElementById('mcpScreen').onclick = (e) => { if (e.target.id === 'mcpScreen') closeMcpScreen(); };
document.getElementById('mcpSearchBtn').onclick = _mcpSearch;
document.getElementById('mcpSearch').addEventListener('keydown', e => { if (e.key === 'Enter') _mcpSearch(); });

document.getElementById('mcpAddBtn').onclick = () => {
  const val = id => (document.getElementById(id)?.value || '').trim();
  const name = val('mcpName'), cmdline = val('mcpCommand'), url = val('mcpUrl'), token = val('mcpToken');
  if (!name || (!cmdline && !url)) { _mcpNotice('Name and a command or URL are required.', false); return; }
  const parts = cmdline ? cmdline.split(/\s+/) : [];
  const headers = (url && token) ? { Authorization: 'Bearer ' + token } : undefined;
  const env = _mcpParseEnv(document.getElementById('mcpEnv')?.value || '');
  _mcpSend({ action: 'add', name, url, command: parts[0] || '', args: parts.slice(1), headers, env });
  ['mcpName', 'mcpCommand', 'mcpUrl', 'mcpToken', 'mcpEnv'].forEach(id => { const el = document.getElementById(id); if (el) el.value = ''; });
};
function _mcpParseEnv(raw) {
  const out = {};
  (raw || '').split('\n').forEach(line => {
    line = line.trim();
    if (!line || line.startsWith('#') || !line.includes('=')) return;
    const i = line.indexOf('=');
    const k = line.slice(0, i).trim();
    if (k) out[k] = line.slice(i + 1).trim();
  });
  return Object.keys(out).length ? out : undefined;
}

/* ---- Guided connector setup (Figma pilot) ---- */

function openConnectorGuide(id) {
  const screen = document.getElementById('connectorScreen');
  if (!screen) return;
  _connectorGuide = { id };
  _connectorToken = '';
  screen.hidden = false;
  document.getElementById('connectorTitle').textContent = 'Set up connector';
  document.getElementById('connectorIcon').textContent = '🔌';
  document.getElementById('connectorSummary').textContent = 'Loading…';
  document.getElementById('connectorWhy').textContent = '';
  document.getElementById('connectorSteps').innerHTML = '';
  document.getElementById('connectorFieldLabel').textContent = 'Access token';
  const inp = document.getElementById('connectorInput');
  inp.value = '';
  inp.placeholder = '';
  document.getElementById('connectorHint').textContent = '';
  document.getElementById('connectorSaveAnyway').hidden = true;
  document.getElementById('connectorRemove').hidden = true;
  const st = document.getElementById('connectorStatus');
  st.hidden = true;
  _mcpSend({ action: 'connector_get', connector: id });
}

function closeConnectorGuide() {
  const screen = document.getElementById('connectorScreen');
  if (screen) screen.hidden = true;
  _connectorGuide = null;
  _connectorToken = '';
}

function _connectorSetStatus(text, kind) {
  const st = document.getElementById('connectorStatus');
  st.textContent = text;
  st.hidden = false;
  st.className = 'settings-key-detail '
    + (kind === 'ok' ? 'ok' : kind === 'warn' ? 'connector-warn' : 'err');
}

function _connectorRender(m) {
  _connectorGuide = m;
  document.getElementById('connectorTitle').textContent = m.label || 'Connector';
  document.getElementById('connectorIcon').textContent = m.icon || '🔌';
  document.getElementById('connectorSummary').textContent = m.summary || '';
  document.getElementById('connectorWhy').textContent = m.why || '';
  const steps = document.getElementById('connectorSteps');
  steps.innerHTML = '';
  (m.steps || []).forEach(s => {
    const li = document.createElement('li');
    li.appendChild(document.createTextNode(s.text || ''));
    if (s.url) {
      li.appendChild(document.createTextNode(' '));
      const a = document.createElement('a');
      a.href = s.url;
      a.target = '_blank';
      a.rel = 'noopener';
      a.textContent = s.link_label || 'Open';
      a.className = 'connector-step-link';
      li.appendChild(a);
    }
    steps.appendChild(li);
  });
  document.getElementById('connectorFieldLabel').textContent = m.field_label || 'Access token';
  const inp = document.getElementById('connectorInput');
  inp.placeholder = m.field_placeholder || '';
  inp.value = '';
  document.getElementById('connectorHint').textContent = m.field_hint || '';
  const docs = document.getElementById('connectorDocs');
  if (m.docs_url) { docs.href = m.docs_url; docs.hidden = false; } else { docs.hidden = true; }
  document.getElementById('connectorRemove').hidden = !m.connected;
  const btn = document.getElementById('connectorTest');
  btn.disabled = false;
  btn.textContent = 'Test connection';
  if (m.connected) {
    _connectorSetStatus(m.unverified ? 'Saved · not verified' : 'Connected · tools are live',
      m.unverified ? 'warn' : 'ok');
  } else {
    const st = document.getElementById('connectorStatus');
    st.hidden = true;
  }
}

function _connectorTest() {
  if (!_connectorGuide || !_connectorGuide.id) return;
  const token = (document.getElementById('connectorInput').value || '').trim();
  if (!token) { _connectorSetStatus('Paste an access token first.', 'err'); return; }
  _connectorToken = token;
  const btn = document.getElementById('connectorTest');
  btn.disabled = true;
  btn.textContent = 'Testing…';
  document.getElementById('connectorSaveAnyway').hidden = true;
  _connectorSetStatus('Checking the token with Figma…', 'warn');
  _mcpSend({ action: 'connector_test', connector: _connectorGuide.id, token });
}

function _connectorSaveAnyway() {
  if (!_connectorGuide || !_connectorToken) return;
  const btn = document.getElementById('connectorSaveAnyway');
  btn.disabled = true;
  btn.textContent = 'Saving…';
  _mcpSend({ action: 'connector_save', connector: _connectorGuide.id, token: _connectorToken });
}

function _connectorRemove() {
  if (!_connectorGuide || !_connectorGuide.id) return;
  const label = _connectorGuide.label || 'this connector';
  if (!window.confirm('Remove the saved ' + label + ' key?\n\nAsha will stop using it until you set it up again.')) return;
  _mcpSend({ action: 'connector_delete', connector: _connectorGuide.id });
}

function _connectorResult(m) {
  const btn = document.getElementById('connectorTest');
  btn.disabled = false;
  btn.textContent = 'Test connection';
  const saveBtn = document.getElementById('connectorSaveAnyway');
  saveBtn.disabled = false;
  saveBtn.textContent = 'Save anyway (unverified)';
  if (m.ok) {
    _connectorSetStatus(m.message || 'Connected.', m.unverified ? 'warn' : 'ok');
    document.getElementById('connectorInput').value = '';
    _connectorToken = '';
    saveBtn.hidden = true;
    document.getElementById('connectorRemove').hidden = false;
  } else if (m.network) {
    _connectorSetStatus(m.message || 'Could not reach Figma.', 'warn');
    saveBtn.hidden = false;
  } else {
    _connectorSetStatus(m.message || 'That did not work.', 'err');
    saveBtn.hidden = true;
  }
}

document.getElementById('connectorClose').onclick = closeConnectorGuide;
document.getElementById('connectorScreen').onclick = (e) => { if (e.target.id === 'connectorScreen') closeConnectorGuide(); };
document.getElementById('connectorTest').onclick = _connectorTest;
document.getElementById('connectorSaveAnyway').onclick = _connectorSaveAnyway;
document.getElementById('connectorRemove').onclick = _connectorRemove;
document.getElementById('connectorInput').addEventListener('keydown', e => { if (e.key === 'Enter') _connectorTest(); });


document.getElementById('settingsClose').onclick = closeSettings;
document.getElementById('settingsPanel').onclick = (e) => { if (e.target.id === 'settingsPanel') closeSettings(); };
const _settingsSeg = document.getElementById('settingsSeg');
if (_settingsSeg) _settingsSeg.addEventListener('click', e => {
  const btn = e.target.closest('.settings-seg-btn');
  if (!btn || btn.classList.contains('active')) return;
  brainProviderSelect(btn.dataset.provider);
});
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && document.getElementById('settingsPanel').classList.contains('open')) {
    closeSettings();
  }
});

/* ---- Quick-action chips ---- */

document.getElementById('orbChips').addEventListener('click', e => {
  const chip = e.target.closest('.orb-chip');
  if (!chip) return;
  const text = chip.dataset.text;
  if (!text) return;
  const inp = document.getElementById('textInput');
  inp.value = text;
  sendText();
});

/* ---- Work Mode (multi-worker dashboard) ---- */

const chatShell = document.querySelector('.app-shell');
const workAreaEl = document.getElementById('workArea');
const midColEl = document.getElementById('midCol');
const workerGridEl = document.getElementById('workerGrid');
const workBrainStatusEl = document.getElementById('workBrainStatus');
let inWorkMode = false;
const SLOT_COUNT = 3;
const slots = []; // {el, sessionId, name, agent, status, startTime, activity}
let workerConfigs = [];  // per-worker {index, project, project_name, model, model_name}

function _renderWorkerConfigs() {
  workerConfigs.forEach((w, i) => {
    const s = slots[i];
    if (!s) return;
    const nameEl = s.el.querySelector('.worker-name');
    if (nameEl) nameEl.textContent = w.name || ('Worker ' + (i + 1));
    s.el.querySelectorAll('.wc-chip').forEach(ch => {
      const t = ch.querySelector('.wc-text');
      if (!t) return;
      if (ch.dataset.field === 'project') t.textContent = w.project_name || 'project';
      else if (ch.dataset.field === 'model') t.textContent = w.model_name || w.model || 'model';
    });
  });
}

function _setWorkerLocal(index, patch) {
  if (!workerConfigs[index]) return;
  Object.assign(workerConfigs[index], patch);
  _renderWorkerConfigs();
}


function _setActiveSeg(mode) {
  const seg = document.getElementById('modeSeg');
  if (!seg) return;
  seg.querySelectorAll('.mode-seg-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.mode === mode);
  });
  const pill = document.getElementById('modePill');
  if (pill) pill.setAttribute('data-pos', mode);
}

function _formatElapsed(ms) {
  const s = Math.floor(ms / 1000);
  if (s < 60) return s + 's';
  const m = Math.floor(s / 60);
  return m + 'm ' + (s % 60) + 's';
}

function _toolIcon(name) {
  const icons = {
    read: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>',
    write: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>',
    edit: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>',
    bash: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/></svg>',
    glob: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>',
    grep: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>',
    webfetch: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg>',
    task: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><line x1="9" y1="9" x2="15" y2="15"/><line x1="15" y1="9" x2="9" y2="15"/></svg>',
  };
  return icons[name] || '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/></svg>';
}

function _createSlot(index) {
  const panel = document.createElement('div');
  panel.className = 'worker-panel';
  panel.dataset.slot = index;
  panel.innerHTML = `
    <div class="worker-header">
      <div class="worker-header-left">
        <span class="worker-dot"></span>
        <span class="worker-name">Worker ${index + 1}</span>
        <span class="worker-agent">idle</span>
      </div>
      <div class="worker-header-right">
        <span class="worker-time"></span>
      </div>
    </div>
    <div class="worker-config">
      <button class="wc-chip" data-field="project" type="button" title="Project folder for this worker">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>
        <span class="wc-text">project</span>
      </button>
      <button class="wc-chip" data-field="model" type="button" title="Model for this worker">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="5"/><path d="M12 8v8M8 12h8"/></svg>
        <span class="wc-text">model</span>
      </button>
    </div>
    <div class="worker-feed"></div>
    <div class="worker-empty">Waiting for activity&hellip;</div>`;
  workerGridEl.appendChild(panel);
  const slot = {
    el: panel,
    feed: panel.querySelector('.worker-feed'),
    empty: panel.querySelector('.worker-empty'),
    sessionId: null,
    name: `Worker ${index + 1}`,
    agent: 'idle',
    status: 'idle',
    startTime: null,
    activity: [],
  };
  const chips = panel.querySelectorAll('.wc-chip');
  chips.forEach(ch => {
    ch.addEventListener('click', () => {
      const field = ch.dataset.field;
      const target = { kind: 'worker', index: index };
      if (field === 'project') _openProjectPicker(ch, target);
      else if (field === 'model') _openModelPicker(target, ch);
    });
  });
  return slot;
}

function _renderFeed(slot) {
  const items = slot.activity;
  if (!items.length) {
    slot.empty.hidden = false;
    slot.feed.innerHTML = '';
    return;
  }
  slot.empty.hidden = true;
  // Build DOM for last N items (keep it fast)
  const MAX = 40;
  const recent = items.slice(-MAX);
  let html = '';
  for (const a of recent) {
    if (a.kind === 'tool') {
      const icon = _toolIcon(a.tool);
      const statusCls = a.status === 'completed' ? 'done' : a.status === 'running' ? 'spin' : a.status === 'error' ? 'err' : '';
      const label = a.tool === 'bash' || a.tool === 'bash.tool'
        ? (a.command || 'bash')
        : (a.file || a.tool);
      const short = label.length > 60 ? label.slice(-57) + '...' : label;
      const detail = a.text ? `<div class="feed-detail">${_esc(a.text.slice(0, 150))}</div>` : '';
      html += `<div class="feed-item tool-item ${statusCls}">
        <span class="feed-icon">${icon}</span>
        <span class="feed-label">${_esc(short)}</span>
        ${detail}
      </div>`;
    } else if (a.kind === 'text') {
      const snippet = a.text.length > 120 ? a.text.slice(0, 117) + '...' : a.text;
      html += `<div class="feed-item text-item"><span class="feed-text">${_esc(snippet)}</span></div>`;
    }
  }
  slot.feed.innerHTML = html;
  slot.feed.scrollTop = slot.feed.scrollHeight;
}

function _esc(s) { return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

function _assignSlot(slotIndex, sessionId, name, agent) {
  const s = slots[slotIndex];
  if (!s) return;
  s.sessionId = sessionId;
  s.name = name || 'Worker ' + (slotIndex + 1);
  s.agent = agent || 'opencode';
  s.status = 'running';
  s.startTime = Date.now();
  s.activity = [];
  s.el.querySelector('.worker-name').textContent = s.name;
  s.el.querySelector('.worker-agent').textContent = s.agent;
  s.el.querySelector('.worker-dot').className = 'worker-dot running';
  s.el.querySelector('.worker-time').textContent = '0s';
  if (workBrainStatusEl) workBrainStatusEl.classList.remove('thinking');
  document.querySelectorAll('.worker-empty').forEach(el => el.classList.remove('thinking'));
  _renderFeed(s);
}

function _clearSlot(slotIndex) {
  const s = slots[slotIndex];
  if (!s) return;
  s.sessionId = null;
  s.agent = 'idle';
  s.status = 'idle';
  s.startTime = null;
  s.activity = [];
  s.el.querySelector('.worker-name').textContent = 'Worker ' + (slotIndex + 1);
  s.el.querySelector('.worker-agent').textContent = 'idle';
  s.el.querySelector('.worker-dot').className = 'worker-dot';
  s.el.querySelector('.worker-time').textContent = '';
  _renderFeed(s);
}

function updateWorkerStatus(sessionId, status) {
  const s = slots.find(x => x.sessionId === sessionId);
  if (!s) return;
  s.status = status;
  s.el.querySelector('.worker-dot').className = 'worker-dot ' + status;
}

function showWorkerPermission(sessionId, action, target) {
  const s = slots.find(x => x.sessionId === sessionId);
  if (!s) return;
  hideWorkerPermission(sessionId);
  const feed = s.feed;
  const overlay = document.createElement('div');
  overlay.className = 'worker-permission';
  overlay.innerHTML = `
    <div class="worker-perm-title">${action === 'write' ? 'File Write' : 'File Read'} Requested</div>
    <div class="worker-perm-target">${target || ''}</div>
    <div class="worker-perm-actions">
      <button class="worker-perm-allow" data-decision="allow">Allow</button>
      <button class="worker-perm-deny" data-decision="deny">Deny</button>
    </div>`;
  overlay.querySelectorAll('button').forEach(btn => {
    btn.addEventListener('click', () => {
      const decision = btn.dataset.decision;
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({type: 'permission_response', worker_id: sessionId, decision}));
      }
      hideWorkerPermission(sessionId);
    });
  });
  feed.appendChild(overlay);
}

function hideWorkerPermission(sessionId) {
  const s = slots.find(x => x.sessionId === sessionId);
  if (!s) return;
  const existing = s.feed.querySelector('.worker-permission');
  if (existing) existing.remove();
}

function _createSlots() {
  if (slots.length) return;
  for (let i = 0; i < SLOT_COUNT; i++) {
    slots.push(_createSlot(i));
  }
}

function _destroySlots() {
  for (let i = slots.length - 1; i >= 0; i--) {
    slots[i].el.remove();
  }
  slots.length = 0;
}

// update elapsed timers every 5s
setInterval(() => {
  for (const s of slots) {
    if (s.sessionId && s.startTime) {
      const t = s.el.querySelector('.worker-time');
      if (t) t.textContent = _formatElapsed(Date.now() - s.startTime);
    }
  }
  document.querySelectorAll('.sb-agent-time, .sb-task-time').forEach(el => {
    const start = Number(el.dataset.start || 0);
    if (start) el.textContent = _formatElapsed(Date.now() - start);
  });
}, 5000);

function enterWorkMode() {
  if (inWorkMode) return;
  inWorkMode = true;
  _setActiveSeg('work');
  midColEl.classList.add('work-mode');
  document.body.classList.add('work-mode');
  workAreaEl.hidden = false;
  _createSlots();
  _renderWorkerConfigs();
  _refreshMicButtons();
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({type: 'work_mode', enabled: true}));
    ws.send(JSON.stringify({type: 'workers_get'}));
  }
}

function exitWorkMode() {
  if (!inWorkMode) return;
  inWorkMode = false;
  _setActiveSeg('chat');
  workAreaEl.hidden = true;
  midColEl.classList.remove('work-mode');
  document.body.classList.remove('work-mode');
  _destroySlots();
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({type: 'work_mode', enabled: false}));
  }
}

/* ---- Settings panel ---- */

function openSettings() {
  const panel = document.getElementById('settingsPanel');
  if (!panel) return;
  _settingsInit();
  _settingsPopulate();
  _highlightCurrentProvider();
  const err = document.getElementById('settingsError');
  if (err) err.hidden = true;
  const usage = document.getElementById('settingsUsage');
  if (usage) usage.hidden = true;
  panel.classList.add('open');
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({type: 'provider_config_get'}));
  }
}

let _settingsWired = false;
function _settingsInit() {
  if (_settingsWired) return;
  _settingsWired = true;
  const seg = document.getElementById('setThemeSeg');
  if (seg) seg.addEventListener('click', e => {
    const b = e.target.closest('.seg-btn');
    if (!b || !b.dataset.theme) return;
    _setTheme(b.dataset.theme);
    _settingsPopulate();
  });
  // Hardwired brain (2026-09-17): the settings Model row is read-only
  // text (setModelSub) — there is no Change button and no picker.
  const pb = document.getElementById('setProjectBtn');
  if (pb) pb.onclick = () => {
    closeSettings();
    const t = document.getElementById('projectBtn');
    _openProjectPicker(t || document.body);
  };
  _wireCustomProviderForm();
}

function _settingsPopulate() {
  const theme = document.documentElement.getAttribute('data-theme') || 'light';
  document.querySelectorAll('#setThemeSeg .seg-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.theme === theme));
  const modelEl = document.getElementById('brainModelBtnName');
  const msub = document.getElementById('setModelSub');
  if (msub) msub.textContent = (modelEl && modelEl.textContent) || _lastBrainModel || 'Not set';
  const psub = document.getElementById('setProjectSub');
  if (psub) psub.textContent = projectCurrent ? _projBase(projectCurrent) : 'None selected';
}

function closeSettings() {
  const panel = document.getElementById('settingsPanel');
  if (panel) panel.classList.remove('open');
  const err = document.getElementById('settingsError');
  if (err) err.hidden = true;
}

function _highlightCurrentProvider() {
  const seg = document.getElementById('settingsSeg');
  if (!seg) return;
  seg.querySelectorAll('.settings-seg-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.provider === _currentBrainTransport);
  });
}

/* ---- Provider config (brain + worker provider/model, from providers/) ---- */

let _cfgProviders = [];
let _cfgBrain = { provider: 'opencode', model: 'big-pickle' };
let _cfgWorker = { provider: 'opencode', model: 'big-pickle' };
let _cfgDirty = false;

/* API-key rows shown in Settings (env var -> provider short id). */
const _CFG_KEY_ROWS = [
  { env: 'OPENCODE_API_KEY', provider: 'opencode', label: 'OpenCode' },
  { env: 'OPENROUTER_API_KEY', provider: 'openrouter', label: 'OpenRouter' },
  { env: 'OPENAI_API_KEY', provider: 'openai', label: 'OpenAI' },
  { env: 'ANTHROPIC_API_KEY', provider: 'anthropic', label: 'Anthropic' },
  { env: 'GEMINI_API_KEY', provider: 'gemini', label: 'Gemini' },
  { env: 'XAI_API_KEY', provider: 'xai', label: 'xAI' },
];

function _cfgKeyProviderConfigured(provider) {
  const p = (_cfgProviders || []).find(x => x.provider_id === provider);
  return !!(p && p.configured);
}

function _renderProviderKeys() {
  const box = document.getElementById('cfgKeys');
  if (!box) return;
  box.innerHTML = '';
  _CFG_KEY_ROWS.forEach(row => {
    const configured = _cfgKeyProviderConfigured(row.provider);
    const wrap = document.createElement('div');
    wrap.className = 'settings-key-row';

    const lab = document.createElement('div');
    lab.className = 'settings-key-label';
    lab.textContent = row.label;
    const state = document.createElement('span');
    state.className = 'settings-key-state' + (configured ? ' on' : '');
    state.textContent = configured ? 'set' : 'no key';
    lab.appendChild(state);

    const inp = document.createElement('input');
    inp.type = 'password';
    inp.className = 'settings-key-input';
    inp.placeholder = configured ? '••••••••  (click to replace)' : 'paste API key';
    inp.autocomplete = 'off';

    const btn = document.createElement('button');
    btn.className = 'settings-key-save';
    btn.textContent = 'Save';

    const stat = document.createElement('div');
    stat.className = 'settings-key-detail';
    stat.hidden = true;

    inp.addEventListener('keydown', e => {
      if (e.key === 'Enter') save();
    });
    btn.addEventListener('click', save);

    function save() {
      const value = inp.value.trim();
      stat.hidden = true;
      if (!value) { return; }
      if (!ws || ws.readyState !== WebSocket.OPEN) return;
      btn.disabled = true; btn.textContent = '…';
      ws.send(JSON.stringify({
        type: 'provider_key_set',
        env: row.env,
        value: value,
      }));
      inp.value = '';
      inp.placeholder = '••••••••  (click to replace)';
      setTimeout(() => { btn.disabled = false; btn.textContent = 'Save'; }, 400);
    }

    wrap.appendChild(lab);
    wrap.appendChild(inp);
    wrap.appendChild(btn);
    wrap.appendChild(stat);
    box.appendChild(wrap);
  });
}

let _keyStatus = {};

function _setProviderKeyStatus(env, ok, detail) {
  _keyStatus[env] = { ok, detail };
  _applyKeyStatusToRow(env);
}

function _renderCustomProviders() {
  const box = document.getElementById('cfgCustom');
  if (!box) return;
  box.innerHTML = '';
  const customs = (_cfgProviders || []).filter(p => String(p.provider_id).startsWith('custom:'));
  if (!customs.length) {
    const empty = document.createElement('div');
    empty.className = 'settings-hint';
    empty.textContent = 'No custom providers yet.';
    box.appendChild(empty);
    return;
  }
  customs.forEach(p => {
    const wrap = document.createElement('div');
    wrap.className = 'settings-key-row';

    const lab = document.createElement('div');
    lab.className = 'settings-key-label';
    lab.textContent = p.name || p.provider_id;
    const state = document.createElement('span');
    state.className = 'settings-key-state' + (p.configured ? ' on' : '');
    state.textContent = p.configured ? 'set' : 'no key';
    lab.appendChild(state);

    const sub = document.createElement('div');
    sub.className = 'settings-key-detail';
    sub.textContent = p.base_url || '';

    const btn = document.createElement('button');
    btn.className = 'settings-key-save';
    btn.textContent = 'Remove';
    btn.addEventListener('click', () => {
      if (!ws || ws.readyState !== WebSocket.OPEN) return;
      ws.send(JSON.stringify({ type: 'provider_custom', action: 'remove', id: p.id }));
    });

    wrap.appendChild(lab);
    wrap.appendChild(sub);
    wrap.appendChild(btn);
    box.appendChild(wrap);
  });
}

function _wireCustomProviderForm() {
  const btn = document.getElementById('cpAdd');
  if (!btn || btn._wired) return;
  btn._wired = true;
  btn.addEventListener('click', () => {
    const val = id => (document.getElementById(id)?.value || '').trim();
    const name = val('cpName'), base = val('cpBase');
    const stat = document.getElementById('cpStatus');
    const show = (msg, ok) => {
      if (!stat) return;
      stat.textContent = msg; stat.hidden = false;
      stat.className = 'settings-key-detail' + (ok ? ' ok' : ' err');
    };
    if (!name || !base) { show('Name and base URL are required.', false); return; }
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    btn.disabled = true;
    ws.send(JSON.stringify({
      type: 'provider_custom', action: 'add', name, base_url: base,
      api_key: val('cpKey'), models: val('cpModels'),
    }));
    ['cpName', 'cpBase', 'cpKey', 'cpModels'].forEach(id => {
      const el = document.getElementById(id); if (el) el.value = '';
    });
    setTimeout(() => { btn.disabled = false; }, 400);
  });
}

function _applyKeyStatusToRow(env) {
  const box = document.getElementById('cfgKeys');
  if (!box) return;
  const row = _CFG_KEY_ROWS.find(r => r.env === env);
  if (!row) return;
  const rows = box.querySelectorAll('.settings-key-row');
  const idx = _CFG_KEY_ROWS.indexOf(row);
  const wrap = rows[idx];
  if (!wrap) return;
  const stat = wrap.querySelector('.settings-key-detail');
  if (stat) {
    const s = _keyStatus[env];
    stat.textContent = s.ok
      ? 'Saved' + (s.detail ? ' — ' + s.detail : '')
      : 'Error: ' + (s.detail || 'failed');
    stat.className = 'settings-key-detail' + (s.ok ? ' ok' : ' err');
    stat.hidden = false;
  }
}

function _reapplyProviderKeyStatus() {
  Object.keys(_keyStatus).forEach(_applyKeyStatusToRow);
}

function _cfgProviderOptions(select, selected) {
  if (!select) return;
  select.innerHTML = '';
  (_cfgProviders || []).forEach(p => {
    const opt = document.createElement('option');
    opt.value = p.provider_id;
    opt.textContent = p.name + (p.configured ? '' : ' (no key)');
    if (p.configured) opt.selected = (p.provider_id === selected);
    select.appendChild(opt);
  });
}

function _cfgModelOptions(select, providerId, selected) {
  if (!select) return;
  const p = (_cfgProviders || []).find(x => x.provider_id === providerId);
  select.innerHTML = '';
  const models = (p && p.models) || [];
  if (!models.length) {
    const opt = document.createElement('option');
    opt.value = selected || 'big-pickle';
    opt.textContent = selected || 'big-pickle';
    select.appendChild(opt);
    return;
  }
  let found = false;
  models.forEach(m => {
    const opt = document.createElement('option');
    opt.value = m.id;
    opt.textContent = m.id;
    if (m.id === selected) { opt.selected = true; found = true; }
    select.appendChild(opt);
  });
  if (!found && selected) {
    const opt = document.createElement('option');
    opt.value = selected;
    opt.textContent = selected;
    opt.selected = true;
    select.appendChild(opt);
  }
}

function _renderProviderConfig() {
  _cfgProviderOptions(document.getElementById('cfgBrainProvider'), _cfgBrain.provider);
  _cfgModelOptions(document.getElementById('cfgBrainModel'), _cfgBrain.provider, _cfgBrain.model);
  _cfgProviderOptions(document.getElementById('cfgWorkerProvider'), _cfgWorker.provider);
  _cfgModelOptions(document.getElementById('cfgWorkerModel'), _cfgWorker.provider, _cfgWorker.model);
  _renderProviderKeys();
  _reapplyProviderKeyStatus();
  _renderCustomProviders();
}

function _sendProviderConfig() {
  if (_cfgDirty) return;
  _cfgDirty = true;
  const brainProvider = document.getElementById('cfgBrainProvider').value;
  const brainModel = document.getElementById('cfgBrainModel').value;
  const workerProvider = document.getElementById('cfgWorkerProvider').value;
  const workerModel = document.getElementById('cfgWorkerModel').value;
  if (!ws || ws.readyState !== WebSocket.OPEN) { _cfgDirty = false; return; }
  ws.send(JSON.stringify({
    type: 'provider_config_set',
    brain_provider: brainProvider,
    brain_model: brainModel,
    worker_provider: workerProvider,
    worker_model: workerModel,
  }));
  setTimeout(() => { _cfgDirty = false; }, 300);
}

function _onCfgProviderChange(role) {
  const providerSel = document.getElementById(role === 'brain' ? 'cfgBrainProvider' : 'cfgWorkerProvider');
  const modelSel = document.getElementById(role === 'brain' ? 'cfgBrainModel' : 'cfgWorkerModel');
  const p = (_cfgProviders || []).find(x => x.provider_id === providerSel.value);
  const preferred = (p && p.default_model) || 'big-pickle';
  const current = role === 'brain' ? _cfgBrain.model : _cfgWorker.model;
  _cfgModelOptions(modelSel, providerSel.value, current && modelSel.querySelector('option[value="' + current + '"]') ? current : preferred);
  _sendProviderConfig();
}

function _initProviderConfigUI() {
  ['cfgBrainProvider', 'cfgWorkerProvider'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.addEventListener('change', () => _onCfgProviderChange(id === 'cfgBrainProvider' ? 'brain' : 'worker'));
  });
  ['cfgBrainModel', 'cfgWorkerModel'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.addEventListener('change', _sendProviderConfig);
  });
}

function brainProviderSelect(provider) {
  if (!provider || !ws || ws.readyState !== WebSocket.OPEN) return;
  ws.send(JSON.stringify({type: 'brain_provider', provider: provider}));
  closeSettings();
}

/* ---- Mode toggle ---- */

const modeSeg = document.getElementById('modeSeg');
if (modeSeg) {
  modeSeg.addEventListener('click', e => {
    const btn = e.target.closest('.mode-seg-btn');
    if (!btn || btn.classList.contains('active')) return;
    if (btn.dataset.mode === 'work') enterWorkMode();
    else exitWorkMode();
  });
}

/* ---- Recent sessions + New chat ---- */

const RECENT_DOTS = ['#2fd0ff', '#4ade80', '#d29922', '#3fb950', '#e25555'];

// The agents roster can be collapsed; the choice is remembered.
(function initAgentsToggle() {
  const section = document.getElementById('sbAgents');
  const btn = document.getElementById('sbAgentsToggle');
  if (!section || !btn) return;
  let collapsed = false;
  try { collapsed = localStorage.getItem('asha.agentsCollapsed') === '1'; } catch (e) {}
  const apply = () => {
    section.classList.toggle('collapsed', collapsed);
    btn.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
  };
  apply();
  btn.addEventListener('click', () => {
    collapsed = !collapsed;
    try { localStorage.setItem('asha.agentsCollapsed', collapsed ? '1' : '0'); } catch (e) {}
    apply();
  });
})();

function renderAgents(msg) {
  const section = document.getElementById('sbAgents');
  const listEl = document.getElementById('sbAgentsList');
  const countEl = document.getElementById('sbAgentsCount');
  if (!section || !listEl) return;
  const all = (msg && msg.agents) || [];
  // The roster is PERMANENT: every agent is always listed, idle ones muted, so
  // the user can see who is there. The count in the heading is how many are
  // live right now ("blocked" counts — it still holds a slot, waiting on a file
  // another agent is editing).
  const live = all.filter(a => a.status === 'working' || a.status === 'blocked'
    || a.status === 'verifying');
  if (countEl) countEl.textContent = String(live.length);
  section.hidden = false;
  if (!all.length) {
    listEl.innerHTML = '<div class="sb-agent-idle">no agents</div>';
    return;
  }
  listEl.innerHTML = '';
  all.forEach(a => {
    const working = a.status === 'working';
    const blocked = a.status === 'blocked';
    const verifying = a.status === 'verifying';
    const verified = a.status === 'verified';
    const needsFix = a.status === 'needs_fix';
    const liveRow = working || blocked || verifying;
    const label = a.name || a.id || 'agent';
    const title = a.title || '';
    const row = document.createElement('div');
    row.className = 'sb-agent-item';
    if (blocked) row.classList.add('sb-agent-item--blocked');
    if (verified) row.classList.add('sb-agent-item--verified');
    if (needsFix) row.classList.add('sb-agent-item--needs-fix');
    if (!liveRow && !needsFix) row.classList.add('sb-agent-item--idle');
    const dot = document.createElement('span');
    dot.className = 'sb-agent-dot';
    if (verifying) dot.style.background = 'var(--accent)';
    else if (needsFix) dot.style.background = 'var(--amber)';
    else if (working) dot.style.background = (a.color || 'var(--accent)');
    else dot.style.background = 'var(--dim)';
    const info = document.createElement('div');
    info.className = 'sb-agent-info';
    const name = document.createElement('div');
    name.className = 'sb-agent-name';
    if (title) {
      name.textContent = label + ' - ';
      const titleSpan = document.createElement('span');
      titleSpan.className = 'sb-agent-title';
      titleSpan.textContent = title;
      name.appendChild(titleSpan);
    } else {
      name.textContent = label;
    }
    const note = document.createElement('div');
    note.className = 'sb-agent-note';
    if (verifying) note.textContent = `checking ${label}'s work`;
    else if (verified) note.textContent = 'verified';
    else if (needsFix) note.textContent = 'needs fix';
    else if (working || blocked) note.textContent = a.note || a.brief_title || a.status;
    else note.textContent = 'idle';
    info.appendChild(name);
    info.appendChild(note);
    const time = document.createElement('span');
    time.className = 'sb-agent-time';
    if (a.started_at) time.dataset.start = String(a.started_at * 1000);
    time.textContent = liveRow && a.seconds != null
      ? _formatElapsed(a.seconds * 1000) : '';
    row.appendChild(dot);
    row.appendChild(info);
    row.appendChild(time);
    listEl.appendChild(row);
  });

  // The live WORK, under the roster: each active task's title + elapsed. A
  // verified task disappears by itself (the server stops sending it). Status
  // only — no clicks, no chat channel.
  const tasks = (msg && msg.tasks) || [];
  if (tasks.length) {
    const head = document.createElement('div');
    head.className = 'sb-task-head';
    head.textContent = 'Working on';
    listEl.appendChild(head);
    tasks.forEach(t => {
      const row = document.createElement('div');
      row.className = 'sb-task-item';
      const title = document.createElement('div');
      title.className = 'sb-task-title';
      title.textContent = t.label || t.note || t.title || t.status || 'working';
      title.title = t.title || '';
      const time = document.createElement('span');
      time.className = 'sb-task-time';
      if (t.started_at) time.dataset.start = String(t.started_at * 1000);
      time.textContent = _formatElapsed((t.seconds || 0) * 1000);
      row.appendChild(title);
      row.appendChild(time);
      listEl.appendChild(row);
    });
  }
}

function _relTime(iso) {
  if (!iso) return '';
  const t = new Date(iso).getTime();
  if (isNaN(t)) return '';
  const s = Math.max(1, Math.floor((Date.now() - t) / 1000));
  if (s < 60) return s + 's ago';
  const m = Math.floor(s / 60);
  if (m < 60) return m + 'min ago';
  const h = Math.floor(m / 60);
  if (h < 24) return h + 'h ago';
  const d = Math.floor(h / 24);
  if (d < 7) return d === 1 ? 'yesterday' : d + ' days ago';
  return new Date(iso).toLocaleDateString();
}

function renderRecentSessions(list) {
  const wrap = document.getElementById('sbRecentList');
  if (!wrap) return;
  if (!list.length) {
    wrap.innerHTML = '<div class="sb-recent-empty">No sessions yet.</div>';
    return;
  }
  const liveEntry = list.find(s => s.is_live);
  _liveSessionId = liveEntry ? liveEntry.session_id : null;
  wrap.innerHTML = '';
  list.forEach((s, i) => {
    const item = document.createElement('div');
    item.className = 'sb-recent-item' + (s.is_live ? ' sb-recent-live' : '');
    item.title = s.is_live ? 'Current conversation' : 'Open this conversation';
    const dot = document.createElement('div');
    dot.className = 'sb-recent-dot';
    if (s.is_live) {
      dot.classList.add('sb-recent-dot-live');
    } else {
      dot.style.background = RECENT_DOTS[i % RECENT_DOTS.length];
    }
    const info = document.createElement('div');
    info.className = 'sb-recent-info';
    const name = document.createElement('div');
    name.className = 'sb-recent-name';
    name.textContent = s.title || 'Chat';
    if (s.is_live) name.classList.add('sb-recent-name-live');
    const meta = document.createElement('div');
    meta.className = 'sb-recent-meta';
    meta.textContent = s.is_live ? 'Now' : _relTime(s.last_time);
    info.appendChild(name);
    info.appendChild(meta);
    item.appendChild(dot);
    item.appendChild(info);
    item.addEventListener('click', () => loadRecentSession(s.session_id));
    wrap.appendChild(item);
  });
}

function loadRecentSession(sessionId) {
  if (!sessionId) return;
  if (sessionId === _liveSessionId) {
    const chat = document.getElementById('chat');
    if (chat) chat.scrollTop = chat.scrollHeight;
    return;
  }
  const inner = document.getElementById('inner');
  if (inner) inner.innerHTML = '';
  clearCoding(); clearProposal(); clearPermission();
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({type: 'session_open', session_id: sessionId}));
  }
}

function renderSessionHistory(messages) {
  const inner = document.getElementById('inner');
  if (!inner) return;
  inner.innerHTML = '';
  stopThinking();
  (messages || []).forEach(msg => {
    if (!msg) return;
    const text = (msg.text || '').trim();
    if (!text) return;
    if (msg.role === 'user') {
      _closeOpenBot();
      bubble('user', escapeHtml(text));
    } else {
      _appendBotText(text);
    }
  });
  _closeOpenBot();
  const chat = document.getElementById('chat');
  if (chat) chat.scrollTop = chat.scrollHeight;
}

function newChat() {
  if (inWorkMode) exitWorkMode();
  const inner = document.getElementById('inner');
  if (inner) inner.innerHTML = '';
  const ctxPills = document.getElementById('contextPills');
  if (ctxPills) ctxPills.innerHTML = '';
  const fileBar = document.getElementById('attachBar');
  if (fileBar) { fileBar.hidden = true; fileBar.innerHTML = ''; }
  clearCoding(); clearProposal(); clearPermission();
  document.getElementById('stopBtn').style.display = 'none';
  const input = document.getElementById('textInput');
  if (input) { input.value = ''; input.placeholder = 'Ask Asha anything\u2026'; }
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({type: 'new_chat'}));
  }
}

const sbNewChat = document.getElementById('sbNewChat');
if (sbNewChat) sbNewChat.addEventListener('click', newChat);

/* ---- Brain onboarding + selector strip ---- */

let _brainModel = null;
let _brainModels = [];
let _brainKeyPresent = false;
let _brainOnboarded = false;
// none | key | mic | done
let _onboardStep = 'none';
let _noticeTimer = null;

let _pickerActiveIdx = -1;
let _pickerFiltered = [];

// Which models the user hid via Manage models (persisted per browser).
let _modelPrefs = { disabled: new Set() };

function _modelPrefsLoad() {
  try {
    const raw = JSON.parse(localStorage.getItem('asha.modelPrefs') || '{}');
    _modelPrefs.disabled = new Set(Array.isArray(raw.disabled) ? raw.disabled : []);
  } catch (e) {
    _modelPrefs.disabled = new Set();
  }
}

function _modelPrefsSave() {
  try {
    localStorage.setItem('asha.modelPrefs', JSON.stringify({disabled: [..._modelPrefs.disabled]}));
  } catch (e) { /* ignore */ }
}

function _isModelEnabled(m) {
  return !_modelPrefs.disabled.has(m.id);
}

function _brainModelById(id) {
  return _brainModels.find(m => m.id === id) || null;
}

function _populateBrainModels() {
  // Hardwired brain (2026-09-17): the strip shows the fixed model as
  // read-only text — there is no picker for the brain. (The modelPicker
  // overlay below still serves the worker picker.)
  const btnName = document.getElementById('brainModelBtnName');
  const md = _brainModel ? _brainModelById(_brainModel) : null;
  if (btnName) btnName.textContent = md ? md.name : (_brainModel || 'DeepSeek V4.1 Flash');
}

let catalog = [];  // [{id, name, models:[{id,name,free?}]}] — providers

function _modelsForTarget() {
  if (_modelTarget && _modelTarget.kind === 'worker') {
    const w = workerConfigs[_modelTarget.index] || {};
    const groups = (catalog || []).map(p => ({
      name: p.name,
      models: (p.models || [])
        .filter(m => !_modelPrefs.disabled.has(m.id))
        .map(m => ({ id: m.id, name: m.name || m.id, free: !!m.free, provider: p.id })),
    }));
    return { groups, selectedId: w.model || '', selectedProvider: w.provider || '' };
  }
  const order = [];
  const by = {};
  _brainModels.forEach(m => {
    if (!_isModelEnabled(m)) return;
    const g = m.group || 'Models';
    if (!by[g]) { by[g] = []; order.push(g); }
    by[g].push({ id: m.id, name: m.name || m.id, free: !!m.free });
  });
  const groups = order.map(g => ({ name: g, models: by[g] }));
  return { groups, selectedId: _brainModel || '', selectedProvider: '' };
}

function _renderModelList(query) {
  const list = document.getElementById('modelPickerList');
  if (!list) return;
  const q = (query || '').trim().toLowerCase();
  const { groups, selectedId, selectedProvider } = _modelsForTarget();
  _pickerFiltered = [];
  list.innerHTML = '';
  let any = false;
  groups.forEach(g => {
    const models = g.models.filter(m =>
      !q || (m.name || m.id).toLowerCase().includes(q) || m.id.toLowerCase().includes(q));
    if (!models.length) return;
    any = true;
    const h = document.createElement('div');
    h.className = 'mp-group';
    h.textContent = g.name;
    list.appendChild(h);
    models.forEach(m => {
      const isSel = m.id === selectedId &&
        (!m.provider || !selectedProvider || m.provider === selectedProvider);
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'mp-item' + (isSel ? ' sel' : '');
      b.dataset.id = m.id;
      b.dataset.provider = m.provider || '';
      b.innerHTML = '<span class="mp-item-label">' +
        '<span class="mp-item-name"></span>' +
        (m.free ? '<span class="mp-free">Free</span>' : '') +
        '</span>' +
        '<svg class="mp-check" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>';
      b.querySelector('.mp-item-name').textContent = m.name || m.id;
      b.addEventListener('click', () => _selectBrainModel(m.id, m.provider));
      list.appendChild(b);
      _pickerFiltered.push(b);
    });
  });
  if (!any) {
    const e = document.createElement('div');
    e.className = 'mp-empty';
    e.textContent = 'No models found';
    list.appendChild(e);
    _pickerActiveIdx = -1;
    return;
  }
  _pickerActiveIdx = _pickerFiltered.findIndex(b => b.dataset.id === selectedId);
}

let _modelTarget = { kind: 'brain' };
let _modelAnchor = null;

function _selectBrainModel(id, provider) {
  if (!id) return;
  if (_modelTarget && _modelTarget.kind === 'worker') {
    const idx = _modelTarget.index;
    const w = workerConfigs[idx] || {};
    const nextProvider = provider || w.provider || 'opencode';
    let name = id;
    (catalog || []).forEach(p => (p.models || []).forEach(m => {
      if (m.id === id && (!provider || p.id === provider)) name = m.name || id;
    }));
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({
        type: 'worker_set', index: idx, provider: nextProvider, model: id,
      }));
    }
    _setWorkerLocal(idx, { provider: nextProvider, model: id, model_name: name });
    _closeModelPicker();
    return;
  }
  // Hardwired brain (2026-09-17): the user cannot choose the brain model,
  // so the brain branch is a no-op. (The worker branch above still works.)
  _closeModelPicker();
  return;
}

function _positionModelPicker() {
  const picker = document.getElementById('modelPicker');
  const btn = _modelAnchor || document.getElementById('brainModelBtn');
  if (!picker || !btn) return;
  const r = btn.getBoundingClientRect();
  const pw = picker.offsetWidth || 320;
  const ph = picker.offsetHeight || 360;
  let left = Math.max(10, Math.min(r.left, window.innerWidth - pw - 10));
  let top = r.top - ph - 8;
  if (top < 10) top = r.bottom + 8;
  if (top + ph > window.innerHeight - 10) top = Math.max(10, window.innerHeight - ph - 10);
  picker.style.left = left + 'px';
  picker.style.top = top + 'px';
}

function _openModelPicker(target, anchor) {
  const picker = document.getElementById('modelPicker');
  const search = document.getElementById('modelPickerSearch');
  if (!picker) return;
  if (!picker.hidden && _modelAnchor === anchor) { _closeModelPicker(); return; }
  _modelTarget = target || { kind: 'brain' };
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({type: 'catalog_get'}));
  _modelAnchor = anchor || document.getElementById('brainModelBtn');
  if (search) search.value = '';
  _renderModelList('');
  picker.hidden = false;
  picker.classList.add('open');
  _positionModelPicker();
  if (search) search.focus();
}

function _closeModelPicker() {
  const picker = document.getElementById('modelPicker');
  if (!picker) return;
  picker.classList.remove('open');
  picker.hidden = true;
  _modelAnchor = null;
}

function _pickerMove(delta) {
  if (!_pickerFiltered.length) return;
  if (_pickerActiveIdx < 0) _pickerActiveIdx = delta > 0 ? -1 : 0;
  _pickerActiveIdx = (_pickerActiveIdx + delta + _pickerFiltered.length) % _pickerFiltered.length;
  _pickerFiltered.forEach((b, i) => b.classList.toggle('active', i === _pickerActiveIdx));
  const b = _pickerFiltered[_pickerActiveIdx];
  if (b) b.scrollIntoView({ block: 'nearest' });
}

function _initModelPicker() {
  // Hardwired brain (2026-09-17): #brainModelBtn is read-only text, not a
  // trigger — nothing to wire for the brain. Search/manage below still serve
  // the worker picker.
  const search = document.getElementById('modelPickerSearch');
  const manage = document.getElementById('modelPickerManage');
  if (search) {
    search.addEventListener('input', () => _renderModelList(search.value));
    search.addEventListener('keydown', e => {
      if (e.key === 'ArrowDown') { e.preventDefault(); _pickerMove(1); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); _pickerMove(-1); }
      else if (e.key === 'Enter') {
        e.preventDefault();
        const idx = _pickerActiveIdx >= 0 ? _pickerActiveIdx : 0;
        const b = _pickerFiltered[idx];
        if (b) _selectBrainModel(b.dataset.id);
      } else if (e.key === 'Escape') { e.preventDefault(); _closeModelPicker(); }
    });
  }
  if (manage) manage.addEventListener('click', () => { _closeModelPicker(); _openManageModels(); });
  document.addEventListener('mousedown', e => {
    const picker = document.getElementById('modelPicker');
    const trigger = document.getElementById('brainModelBtn');
    if (!picker || picker.hidden) return;
    if (picker.contains(e.target) || (trigger && trigger.contains(e.target))) return;
    _closeModelPicker();
  });
  window.addEventListener('resize', () => {
    const picker = document.getElementById('modelPicker');
    if (picker && !picker.hidden) _positionModelPicker();
  });
}

/* ---- Manage models dialog ---- */

function _openManageModels() {
  const m = document.getElementById('manageModels');
  if (!m) return;
  const s = document.getElementById('mmSearch');
  if (s) s.value = '';
  _renderManageList('');
  m.hidden = false;
  m.classList.add('open');
  if (s) s.focus();
}

function _closeManageModels() {
  const m = document.getElementById('manageModels');
  if (!m) return;
  m.classList.remove('open');
  m.hidden = true;
}

function _renderManageList(query) {
  const list = document.getElementById('mmList');
  if (!list) return;
  const q = (query || '').trim().toLowerCase();
  list.innerHTML = '';
  const rerender = () => _renderManageList(document.getElementById('mmSearch').value);
  let any = false;
  (catalog || []).forEach(p => {
    const models = (p.models || []).filter(m =>
      !q || (m.name || m.id).toLowerCase().includes(q) || m.id.toLowerCase().includes(q));
    if (!models.length) return;
    any = true;
    const grow = document.createElement('div');
    grow.className = 'mm-group';
    const left = document.createElement('div');
    left.className = 'mm-group-left';
    left.innerHTML = '<svg class="mm-group-icon" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="12 2 2 7 12 12 22 7 12 2"/><polyline points="2 17 12 22 22 17"/><polyline points="2 12 12 17 22 12"/></svg><span></span>';
    left.querySelector('span').textContent = p.name;
    grow.appendChild(left);
    const allOn = models.every(m => !_modelPrefs.disabled.has(m.id));
    grow.appendChild(_makeSwitch(allOn, () => {
      if (allOn) models.forEach(m => _modelPrefs.disabled.add(m.id));
      else models.forEach(m => _modelPrefs.disabled.delete(m.id));
      _modelPrefsSave();
      rerender();
      _populateBrainModels();
    }));
    list.appendChild(grow);
    models.forEach(m => {
      const row = document.createElement('div');
      row.className = 'mm-model';
      const nm = document.createElement('span');
      nm.className = 'mm-model-name';
      nm.textContent = m.name || m.id;
      row.appendChild(nm);
      row.appendChild(_makeSwitch(!_modelPrefs.disabled.has(m.id), () => {
        if (_modelPrefs.disabled.has(m.id)) _modelPrefs.disabled.delete(m.id);
        else _modelPrefs.disabled.add(m.id);
        _modelPrefsSave();
        rerender();
        _populateBrainModels();
      }));
      list.appendChild(row);
    });
  });
  if (!any) {
    const e = document.createElement('div');
    e.className = 'mm-empty';
    e.textContent = 'No models found';
    list.appendChild(e);
  }
}

function _makeSwitch(on, onClick) {
  const b = document.createElement('button');
  b.type = 'button';
  b.className = 'mm-switch' + (on ? ' on' : '');
  b.setAttribute('role', 'switch');
  b.setAttribute('aria-checked', on ? 'true' : 'false');
  b.addEventListener('click', onClick);
  return b;
}

const _EYE_OPEN = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7-11-7-11-7z"/><circle cx="12" cy="12" r="3"/></svg>';
const _EYE_OFF = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>';

let _setupMode = 'onboard';

function _showSetupScreen(mode) {
  _setupMode = mode || 'onboard';
  const s = document.getElementById('setupScreen');
  if (!s) return;
  const title = document.getElementById('setupTitle');
  const sub = document.getElementById('setupSub');
  const close = document.getElementById('setupClose');
  const inp = document.getElementById('setupKeyInput');
  const eye = document.getElementById('setupEye');
  const btn = document.getElementById('setupContinue');
  const helperBody = document.getElementById('setupHelperBody');
  const helperBtn = document.getElementById('setupHelperBtn');
  if (title) title.textContent = _setupMode === 'connect' ? 'Connect provider' : 'Welcome to Asha';
  if (sub) {
    sub.textContent = _setupMode === 'connect'
      ? 'Add or replace your OpenCode key. It stays on this computer.'
      : 'Paste your OpenCode key to get started. It stays on this computer.';
  }
  if (close) close.hidden = _setupMode !== 'connect';
  if (inp) { inp.value = ''; inp.type = 'password'; }
  if (eye) { eye.classList.remove('on'); eye.innerHTML = _EYE_OPEN; eye.title = 'Show key'; }
  if (helperBody) helperBody.hidden = true;
  if (helperBtn) helperBtn.classList.remove('open');
  _setupError('');
  if (btn) { btn.disabled = true; btn.textContent = 'Continue'; }
  s.hidden = false;
  s.classList.add('open');
  if (inp) inp.focus();
}

function _hideSetupScreen() {
  const s = document.getElementById('setupScreen');
  if (!s) return;
  s.classList.remove('open');
  s.hidden = true;
}

function _setupError(msg) {
  const err = document.getElementById('setupError');
  const btn = document.getElementById('setupContinue');
  if (err) { err.textContent = msg || ''; err.hidden = !msg; }
  if (btn) { btn.disabled = false; btn.textContent = 'Continue'; }
}

function _setupSubmit() {
  const inp = document.getElementById('setupKeyInput');
  const btn = document.getElementById('setupContinue');
  const value = (inp && inp.value || '').trim();
  if (!value) return;
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    _setupError('Still connecting… please try again in a moment.');
    return;
  }
  _setupError('');
  if (btn) { btn.disabled = true; btn.textContent = 'Checking your key…'; }
  ws.send(JSON.stringify({type: 'provider_key_set', env: 'OPENCODE_API_KEY', value}));
}

function _initSetupScreen() {
  const inp = document.getElementById('setupKeyInput');
  const btn = document.getElementById('setupContinue');
  const eye = document.getElementById('setupEye');
  const helperBtn = document.getElementById('setupHelperBtn');
  const helperBody = document.getElementById('setupHelperBody');
  const close = document.getElementById('setupClose');
  if (inp) {
    inp.addEventListener('input', () => { if (btn) btn.disabled = !inp.value.trim(); });
    inp.addEventListener('keydown', e => { if (e.key === 'Enter') _setupSubmit(); });
  }
  if (btn) btn.addEventListener('click', _setupSubmit);
  if (eye) eye.addEventListener('click', () => {
    if (!inp) return;
    const show = inp.type === 'password';
    inp.type = show ? 'text' : 'password';
    eye.classList.toggle('on', show);
    eye.innerHTML = show ? _EYE_OFF : _EYE_OPEN;
    eye.title = show ? 'Hide key' : 'Show key';
  });
  if (helperBtn && helperBody) helperBtn.addEventListener('click', () => {
    helperBody.hidden = !helperBody.hidden;
    helperBtn.classList.toggle('open', !helperBody.hidden);
  });
  if (close) close.addEventListener('click', _hideSetupScreen);
}

let _toastTimer = null;

function showToast(text, kind) {
  let t = document.getElementById('ashaToast');
  if (!t) {
    t = document.createElement('div');
    t.id = 'ashaToast';
    t.className = 'toast';
    document.body.appendChild(t);
  }
  t.className = 'toast' + (kind ? ' ' + kind : '');
  t.textContent = text;
  t.hidden = false;
  requestAnimationFrame(() => t.classList.add('show'));
  if (_toastTimer) clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => {
    t.classList.remove('show');
    setTimeout(() => { t.hidden = true; }, 250);
  }, 2800);
}

let _lastBrainErrorSig = null;

function chatBrainError(kind, message, modelId) {
  const md = modelId ? _brainModelById(modelId) : null;
  const name = md ? (md.name || modelId) : '';
  const body = message || 'Something went wrong talking to the model.';
  const sig = kind + '|' + name + '|' + body;
  if (sig === _lastBrainErrorSig) return;   // don't repeat the same error
  _lastBrainErrorSig = sig;

  const row = document.createElement('div');
  row.className = 'msg brain-error';
  const wrap = document.createElement('div');
  wrap.className = 'bubble-wrap';
  const b = document.createElement('div');
  b.className = 'bubble';

  const icon = document.createElement('span');
  icon.className = 'be-icon';
  icon.innerHTML = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>';
  const txt = document.createElement('span');
  txt.className = 'be-text';
  txt.innerHTML = (name ? '<b>' + name + '</b> &mdash; ' : '') + body;
  b.appendChild(icon);
  b.appendChild(txt);

  // Hardwired brain (2026-09-17): there is no model to choose, so only
  // auth errors get an action (connect a provider). Rate-limit/unavailable
  // errors are plain text — retrying happens on the next turn.
  if (kind === 'auth') {
    const action = document.createElement('button');
    action.type = 'button';
    action.className = 'be-action';
    action.textContent = 'Connect provider';
    action.onclick = () => {
      _showSetupScreen('connect');
    };
    b.appendChild(action);
  }

  const ts = document.createElement('div');
  ts.className = 'ts';
  ts.textContent = formatTime();
  wrap.appendChild(b);
  wrap.appendChild(ts);
  row.appendChild(wrap);
  inner.appendChild(row);
  chatEl.scrollTop = chatEl.scrollHeight;
}

function _initManageModels() {
  const m = document.getElementById('manageModels');
  const s = document.getElementById('mmSearch');
  const c = document.getElementById('mmConnect');
  const x = document.getElementById('mmClose');
  if (s) s.addEventListener('input', () => _renderManageList(s.value));
  if (c) c.addEventListener('click', () => { _closeManageModels(); _openConnectProvider(true); });
  if (x) x.addEventListener('click', _closeManageModels);
  if (m) m.addEventListener('mousedown', e => { if (e.target === m) _closeManageModels(); });
  document.addEventListener('keydown', e => {
    const mm = document.getElementById('manageModels');
    if (e.key === 'Escape' && mm && !mm.hidden) _closeManageModels();
  });
}

/* ---- Connect provider (pick a provider, paste its key) ---- */

const PROVIDER_ENV = {
  'opencode': 'OPENCODE_API_KEY',
  'opencode-go': 'SUPERVISOR_API_KEY',
  'openrouter': 'OPENROUTER_API_KEY',
  'openai': 'OPENAI_API_KEY',
  'anthropic': 'ANTHROPIC_API_KEY',
  'gemini': 'GEMINI_API_KEY',
  'groq': 'GROQ_API_KEY',
  'xai': 'XAI_API_KEY',
};
let _cpProvider = null;
let _cpFromManage = false;

function _openConnectProvider(fromManage) {
  const m = document.getElementById('connectProvider');
  if (!m) return;
  _cpFromManage = !!fromManage;
  _cpShowList();
  m.hidden = false;
  m.classList.add('open');
}
function _closeConnectProvider() {
  const m = document.getElementById('connectProvider');
  if (m) { m.classList.remove('open'); m.hidden = true; }
  _cpProvider = null;
  _cpFromManage = false;
}
function _cpShowList() {
  const lv = document.getElementById('cpListView');
  const kv = document.getElementById('cpKeyView');
  if (lv) lv.hidden = false;
  if (kv) kv.hidden = true;
  const back = document.getElementById('cpListBack');
  if (back) back.hidden = !_cpFromManage;
  const s = document.getElementById('cpSearch');
  if (s) s.value = '';
  _cpRenderList('');
  if (s) s.focus();
}
const CP_POPULAR = ['opencode', 'opencode-go', 'anthropic', 'openai'];

function _cpRenderList(query) {
  const list = document.getElementById('cpList');
  if (!list) return;
  const q = (query || '').trim().toLowerCase();
  list.innerHTML = '';
  const provs = (catalog || []).filter(p => PROVIDER_ENV[p.id])
    .filter(p => !q || (p.name || '').toLowerCase().includes(q));
  const sections = q
    ? [['', provs]]
    : [['Popular', provs.filter(p => CP_POPULAR.includes(p.id))],
       ['Other', provs.filter(p => !CP_POPULAR.includes(p.id))]];
  sections.forEach(([title, items]) => {
    if (!items.length) return;
    if (title) {
      const h = document.createElement('div');
      h.className = 'cp-section';
      h.textContent = title;
      list.appendChild(h);
    }
    items.forEach(p => {
      const row = document.createElement('div');
      row.className = 'cp-item';
      const av = document.createElement('span');
      av.className = 'pp-avatar';
      av.textContent = (p.name || '?').trim().charAt(0).toUpperCase() || '?';
      av.style.background = _projAvatar(p.name);
      const nm = document.createElement('span');
      nm.className = 'pp-name';
      nm.textContent = p.name;
      const ch = document.createElement('span');
      ch.className = 'cp-chev';
      ch.innerHTML = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 6 15 12 9 18"/></svg>';
      row.appendChild(av); row.appendChild(nm); row.appendChild(ch);
      row.onclick = () => _cpShowKey(p);
      list.appendChild(row);
    });
  });
}
function _cpShowKey(p) {
  _cpProvider = p;
  const lv = document.getElementById('cpListView');
  const kv = document.getElementById('cpKeyView');
  if (lv) lv.hidden = true;
  if (kv) kv.hidden = false;
  const mk = document.getElementById('cpMark');
  if (mk) { mk.textContent = (p.name || '?').trim().charAt(0).toUpperCase() || '?'; mk.style.background = _projAvatar(p.name); }
  const kn = document.getElementById('cpKeyName'); if (kn) kn.textContent = p.name;
  const t = document.getElementById('cpTitle'); if (t) t.textContent = 'Connect ' + p.name;
  const sub = document.getElementById('cpSub'); if (sub) sub.textContent = 'Enter your ' + p.name + ' API key to use its models.';
  const lbl = document.getElementById('cpKeyLabel'); if (lbl) lbl.textContent = p.name + ' API key';
  const inp = document.getElementById('cpKeyInput'); if (inp) inp.value = '';
  const err = document.getElementById('cpError'); if (err) err.hidden = true;
  const btn = document.getElementById('cpContinue'); if (btn) { btn.disabled = false; btn.textContent = 'Continue'; }
  if (inp) inp.focus();
}
function _cpContinue() {
  if (!_cpProvider) return;
  const env = PROVIDER_ENV[_cpProvider.id];
  const inp = document.getElementById('cpKeyInput');
  const val = (inp && inp.value || '').trim();
  if (!env || !val) return;
  const err = document.getElementById('cpError'); if (err) err.hidden = true;
  const btn = document.getElementById('cpContinue'); if (btn) { btn.disabled = true; btn.textContent = 'Saving\u2026'; }
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({type: 'provider_key_set', env, value: val}));
}
function _initConnectProvider() {
  const m = document.getElementById('connectProvider');
  const s = document.getElementById('cpSearch');
  const back = document.getElementById('cpBack');
  const listBack = document.getElementById('cpListBack');
  const closeBtn = document.getElementById('cpClose');
  const closeBtn2 = document.getElementById('cpClose2');
  const cont = document.getElementById('cpContinue');
  const inp = document.getElementById('cpKeyInput');
  if (s) s.addEventListener('input', () => _cpRenderList(s.value));
  if (back) back.addEventListener('click', _cpShowList);
  if (listBack) listBack.addEventListener('click', () => {
    const from = _cpFromManage;
    _closeConnectProvider();
    if (from) _openManageModels();
  });
  if (closeBtn) closeBtn.addEventListener('click', _closeConnectProvider);
  if (closeBtn2) closeBtn2.addEventListener('click', _closeConnectProvider);
  if (cont) cont.addEventListener('click', _cpContinue);
  if (inp) inp.addEventListener('keydown', e => { if (e.key === 'Enter') _cpContinue(); });
  if (m) m.addEventListener('mousedown', e => { if (e.target === m) _closeConnectProvider(); });
  document.addEventListener('keydown', e => {
    const cp = document.getElementById('connectProvider');
    if (e.key === 'Escape' && cp && !cp.hidden) _closeConnectProvider();
  });
}

let _spotTargetEl = null;

function _spotEls() {
  const overlay = document.getElementById('spotOnboard');
  if (!overlay) return null;
  return {
    overlay,
    hole: document.getElementById('spotHole'),
    notice: document.getElementById('spotNotice'),
    text: document.getElementById('spotText'),
    ok: document.getElementById('spotOk'),
  };
}

function _showSpot(text, showOk, targetSel) {
  const els = _spotEls();
  if (!els || !els.overlay) return;
  els.text.innerHTML = text;
  els.ok.hidden = !showOk;
  els.ok.disabled = false;
  els.overlay.classList.add('open');
  els.overlay.hidden = false;
  _spotTargetEl = targetSel ? document.querySelector(targetSel) : null;
  if (_spotTargetEl) _positionSpot(_spotTargetEl);
}

function _positionSpot(target) {
  const els = _spotEls();
  if (!els) return;
  const { hole, notice } = els;
  const r = target.getBoundingClientRect();
  const pad = 14;
  hole.classList.remove('spot-hole--focus');
  void hole.offsetWidth;
  Object.assign(hole.style, {
    left: (r.left - pad) + 'px',
    top: (r.top - pad) + 'px',
    width: (r.width + pad * 2) + 'px',
    height: (r.height + pad * 2) + 'px',
  });
  hole.classList.add('spot-hole--focus');
  notice.style.visibility = 'hidden';
  notice.style.left = '0px';
  notice.style.top = '0px';
  const nw = notice.offsetWidth, nh = notice.offsetHeight;
  let nx = r.left + r.width / 2 - nw / 2;
  nx = Math.max(12, Math.min(nx, window.innerWidth - nw - 12));
  let ny = r.top - pad - 12 - nh;
  const below = ny < 8;
  if (below) {
    ny = r.bottom + pad + 12;
    notice.classList.add('spot-notice--below');
  } else {
    notice.classList.remove('spot-notice--below');
  }
  notice.style.visibility = '';
  notice.style.left = nx + 'px';
  notice.style.top = ny + 'px';
}

function _closeSpot() {
  const els = _spotEls();
  if (els && els.overlay) {
    els.overlay.classList.remove('open');
    els.overlay.hidden = true;
  }
  _spotTargetEl = null;
  if (_noticeTimer) { clearTimeout(_noticeTimer); _noticeTimer = null; }
}

function _advanceToMic() {
  if (_noticeTimer) { clearTimeout(_noticeTimer); _noticeTimer = null; }
  _onboardStep = 'mic';
  _showSpot('Asha talks out loud &mdash; make sure your microphone is on, then press <b>OK</b> to begin.', true, '#micBtn');
}

function _evalOnboarding() {
  if (_brainOnboarded) { _onboardStep = 'done'; _closeSpot(); return; }
  if (_onboardStep === 'done') { _closeSpot(); return; }
  if (_onboardStep === 'mic') return;
  if (!_brainKeyPresent) {
    _onboardStep = 'key';
    _closeSpot();
    _showSetupScreen('onboard');
    return;
  }
  // No model or thinking step: both are hardwired (2026-09-17), so there is
  // nothing to pick and the flow can never stall waiting for a selection.
  // Not onboarded yet (e.g. reloaded mid-flow) → resume at the mic notice.
  _advanceToMic();
}

function _initBrainStrip() {
  const ok = document.getElementById('spotOk');
  if (ok) ok.addEventListener('click', () => {
    // No model gate: the brain is hardwired, so OK always completes
    // onboarding here (mic notice) instead of waiting for a selection.
    if (_onboardStep === 'done') { _closeSpot(); return; }
    _onboardStep = 'done';
    _brainOnboarded = true;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({type: 'onboarding_done'}));
    }
    _closeSpot();
    // Onboarding complete → Asha greets and can now hear you.
    if (!micActive && !manualMicOff) startMic();
  });
  window.addEventListener('resize', () => {
    const ov = document.getElementById('spotOnboard');
    if (ov && !ov.hidden && _spotTargetEl) _positionSpot(_spotTargetEl);
  });
}

/* ---- Boot ---- */

_modelPrefsLoad();
_initProviderConfigUI();
_initSetupScreen();
_initModelPicker();
_initManageModels();
_initConnectProvider();
_initBrainStrip();
connect();
