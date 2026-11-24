// Command panel: every action is a click or a short line. The server (chatcmd.py) decides what is allowed.
(function () {
  const root = document.documentElement, LOG = document.getElementById('clog'), INP = document.getElementById('cin'), SUG = document.getElementById('csug');
  let msgs = [], hist = [], hp = 0, timer = null;
  try { msgs = JSON.parse(sessionStorage.getItem('chat') || '[]'); } catch (e) {}
  const store = () => { try { sessionStorage.setItem('chat', JSON.stringify(msgs.slice(-40))); } catch (e) {} };
  const E = (t, c, x) => { const e = document.createElement(t); if (c) e.className = c; if (x != null) e.textContent = x; return e; };
  const post = async (url, body) => (await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })).json();

  function chips(list, live) {
    const w = E('div'); let g = null, box = null;
    for (const c of list) {
      if (c.group && c.group !== g) { g = c.group; w.append(E('div', 'cgrp', g)); box = null; }
      if (!box) { box = E('div', 'cchips'); w.append(box); }
      const b = E('button', 'cchip', c.label); b.type = 'button'; b.disabled = !live;
      if (c.gu) b.append(E('small', '', c.gu));
      b.onclick = () => c.send != null ? send(c.send) : (INP.value = c.fill, INP.focus());
      box.append(b);
    }
    return w;
  }
  function draw(m, live) {
    const d = E('div', m.who === 'me' ? 'bub me' : 'bub bot');
    if (m.who === 'me') { d.textContent = m.text; LOG.append(d); return; }
    (m.say || []).forEach(x => d.append(E('div', '', x)));
    if (m.card) {
      const k = E('div', 'ccard'); k.append(E('b', '', m.card.title));
      for (const [a, b] of m.card.rows) { const r = E('div'); r.append(E('span', '', a), E('span', '', b)); k.append(r); }
      d.append(k);
    }
    if (m.link) { const a = E('a', 'btn', m.link.label); a.href = m.link.url; a.style.marginTop = '8px'; d.append(a); }
    if (m.confirm) {
      const w = E('div', 'cchips'); w.style.marginTop = '8px';
      const y = E('button', '', 'Confirm'), n = E('button', 'ghost', 'Cancel'); y.type = n.type = 'button'; y.disabled = n.disabled = !live;
      y.onclick = () => decide(m.confirm, true); n.onclick = () => decide(m.confirm, false); w.append(y, n); d.append(w);
    }
    if (m.chips && m.chips.length) d.append(chips(m.chips, live));
    LOG.append(d);
  }
  function paint() { LOG.textContent = ''; msgs.forEach((m, i) => draw(m, i === msgs.length - 1)); LOG.scrollTop = LOG.scrollHeight; }
  function add(m) { msgs.push(m); store(); paint(); }
  async function send(t) {
    if (!t.trim()) return; add({ who: 'me', text: t }); hist.push(t); hp = hist.length; INP.value = ''; SUG.textContent = '';
    const r = await post('/chat', { text: t }); r.who = 'bot'; add(r);
  }
  async function decide(token, yes) { add({ who: 'me', text: yes ? 'Confirm' : 'Cancel' }); const r = await post('/chat/confirm', { token, yes }); r.who = 'bot'; add(r); }
  async function hello() { const r = await post('/chat', { text: 'menu' }); r.who = 'bot'; add(r); }
  function open(on) {
    root.classList.toggle('chat-open', on); try { sessionStorage.setItem('chatopen', on ? '1' : ''); } catch (e) {}
    if (on) { if (!msgs.length) hello(); else paint(); INP.focus(); }
  }
  document.getElementById('chatbtn').onclick = () => open(true);
  document.getElementById('cclose').onclick = () => open(false);
  document.getElementById('cform').onsubmit = e => { e.preventDefault(); send(INP.value); };
  INP.addEventListener('input', () => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      const q = INP.value.trim(); SUG.textContent = ''; if (q.length < 1) return;
      const list = await (await fetch('/chat/suggest?q=' + encodeURIComponent(q))).json();
      for (const s of list) { const b = E('button', '', s.text.trim()); b.type = 'button'; b.append(E('small', '', s.hint)); b.onclick = () => { INP.value = s.text; INP.focus(); SUG.textContent = ''; }; SUG.append(b); }
    }, 150);
  });
  INP.addEventListener('keydown', e => {
    if (e.key === 'ArrowUp' && hist.length) { hp = Math.max(0, hp - 1); INP.value = hist[hp] || ''; e.preventDefault(); }
    if (e.key === 'ArrowDown' && hist.length) { hp = Math.min(hist.length, hp + 1); INP.value = hist[hp] || ''; e.preventDefault(); }
  });
  let was = false; try { was = !!sessionStorage.getItem('chatopen'); } catch (e) {}
  if (was) open(true);
})();
