const $ = id => document.getElementById(id);
// Size the screen to the VISIBLE viewport, not 100vh/dvh. visualViewport
// .height excludes a phone browser's URL bar and updates as it hides, so
// the filters stay reachable and no black bar hangs off the bottom.
const setKH = () => document.documentElement.style.setProperty('--kh',
  ((window.visualViewport && window.visualViewport.height) ||
   window.innerHeight) + 'px');
setKH();
addEventListener('resize', setKH);
addEventListener('orientationchange', setKH);
if (window.visualViewport) {
  visualViewport.addEventListener('resize', setKH);
  visualViewport.addEventListener('scroll', setKH);
}

// manual fullscreen: floating ⛶ button (bottom-right). Hidden while
// fullscreen is active (exit via Esc / system gesture), when the API is
// unavailable, or when an installed PWA is already fullscreen.
const fsBtn = $('fs-btn');
const fsUpdate = () => fsBtn.classList.toggle('hidden',
  !document.fullscreenEnabled ||
  !!document.fullscreenElement ||
  matchMedia('(display-mode: fullscreen)').matches);
fsBtn.addEventListener('click', () =>
  document.documentElement.requestFullscreen({navigationUI: 'hide'})
    .catch(() => {}));
document.addEventListener('fullscreenchange', fsUpdate);
fsUpdate();

const M_BADGE = name => `/media/badge/${name}`;
const badgeUrl = (name, unlocked) => `${M_BADGE(name)}?locked=${unlocked ? 0 : 1}`;

let state = null;
let library = [];
let achFilter = 'all';

/* library browse: faceted INCLUSION filters + letter jump. Each facet's
   Set holds the SELECTED values; empty = no constraint (show all). Genre
   is comma-tokenised so "Action, Fighting, 2D" contributes three tokens.
   Persisted per device. */
const ALPHA = ['#', ...'ABCDEFGHIJKLMNOPQRSTUVWXYZ'];
const FKEYS = ['system', 'folder', 'genre', 'publisher'];
const sel = {system: new Set(), folder: new Set(),
             genre: new Set(), publisher: new Set()};
try {
  const saved = JSON.parse(localStorage.getItem('abox.filters') || '{}');
  for (const k of FKEYS) (saved[k] || []).forEach(v => sel[k].add(v));
} catch (e) { /* corrupt/empty -> defaults (all empty = show all) */ }
const saveFilters = () => localStorage.setItem('abox.filters',
  JSON.stringify(Object.fromEntries(FKEYS.map(k => [k, [...sel[k]]]))));
const gameGenres = g => (g.genre || '').split(',')
  .map(s => s.trim()).filter(Boolean);
let cdLaunched = false;    // launched a Mega CD game (cart leaves USB)
let forceLibrary = false;  // "Games" tapped while a game runs
let lastGameId = null;
let launching = null;      // {game, timer} while the loading screen shows

function layoutPresence() {
  const box = $('presence-text');
  const run = $('presence-run');
  run.classList.remove('scrolling');
  run.style.removeProperty('--presence-shift');
  run.style.removeProperty('--presence-duration');
  requestAnimationFrame(() => {
    const overflow = Math.ceil(run.scrollWidth - box.clientWidth);
    if (overflow <= 2) return;
    // Roughly 32px/s each way, plus four seconds shared between the pauses.
    run.style.setProperty('--presence-shift', `-${overflow}px`);
    run.style.setProperty('--presence-duration',
      `${Math.max(8, 4 + overflow / 16).toFixed(1)}s`);
    run.classList.add('scrolling');
  });
}

window.addEventListener('resize', layoutPresence);
if (document.fonts?.ready) document.fonts.ready.then(layoutPresence);

// status line shown in the library screen header when not actively playing
const IDLE_SUB = {
  'cd-session':    'Mega CD game running - no achievements; reconnects when you quit',
  'starting':      'Connecting to the console…',
  'logging-in':    'Signing in to RetroAchievements…',
  'login-failed':  'RA sign-in failed — check RA_USER / RA_PASS in daemon/.env',
  'offline':       'Cart offline — reconnecting automatically',
  'menu':          'At the EverDrive menu — pick a game to launch',
  'identifying':   'Identifying the running game…',
  'no-set':        'Unknown game — achievements unavailable',
  'core-inactive': 'Unknown game — achievements unavailable',
  'capture-invalid': 'Achievement capture lost writes — return to menu',
  'unsupported-region': 'PAL mode — achievements unavailable; use NTSC/60Hz',
  'region-changed': 'Region switch flipped mid-game — achievements stopped; return to menu on NTSC/60Hz',
  'ra-disabled':   'RA disabled — turn on the switch to earn achievements',
};

/* ================= screen state ================= */

function setScr(id) {
  document.querySelectorAll('.scr').forEach(s =>
    s.classList.toggle('active', s.id === id));
}

const normalPath = p => (p || '').replace(/\\/g, '/').replace(/^\/+/, '');
const sameGamePath = (a, b) => normalPath(a) === normalPath(b);

function launchingGameFor(game) {
  const found = library.find(g => sameGamePath(g.path, game.path));
  if (found) return found;
  const title = game.title || normalPath(game.path).split('/').pop()
    ?.replace(/\.[^.]+$/, '') || 'Game';
  return {title, path: game.path, system: game.system || 'md', stem: title, art: {}};
}

function renderState() {
  if (!state) return;
  const conn = state.connection;
  const playing = conn === 'playing' && state.game;
  const cdPlaying = conn === 'cd-session' && state.game;
  const activeGame = cdPlaying ? launchingGameFor(state.game) : state.game;

  const activeKey = activeGame && (activeGame.id || normalPath(activeGame.path));
  if ((playing || cdPlaying) && activeKey !== lastGameId) forceLibrary = false;
  lastGameId = (playing || cdPlaying) ? activeKey : null;

  // A hardware/menu launch gets the same loading treatment as a web launch.
  // Wait for the authoritative ROM path so the correct box art is used.
  if (!launching && conn === 'identifying' && state.game?.path) {
    startLaunching(launchingGameFor(state.game), true);
    return;
  }

  if (launching && conn === 'identifying' && state.game?.path &&
      sameGamePath(state.game.path, launching.game.path))
    launching.seenIdentifying = true;

  // Ignore late snapshots from the previous game. The loader settles only
  // when the backend reports the path that was actually launched.
  const launchPathMatches = launching && state.game?.path &&
    sameGamePath(state.game.path, launching.game.path);
  if (launching && (conn === 'login-failed' ||
      (launching.seenIdentifying &&
      (launchPathMatches && ['playing', 'no-set', 'core-inactive',
        'unsupported-region', 'ra-disabled']
        .includes(conn))))) endLaunching();
  if (launching && conn === 'identifying' && state.game?.title)
    $('load-sub').textContent = `Identifying: ${state.game.title}`;

  const showGame = (playing || cdPlaying) && !forceLibrary;
  setScr(launching ? 'scr-loading' : showGame ? 'scr-game' : 'scr-lib');

  if (playing || cdPlaying) {
    $('gtitle').textContent = activeGame.title || '';
    const gameArt = cdPlaying ? faceUrl(activeGame, 'front') : activeGame.icon;
    if (gameArt) {
      $('art').src = gameArt;
      $('backdrop').style.backgroundImage = `url(${gameArt})`;
    }
    $('gmeta').textContent = cdPlaying ? 'Mega CD' : 'Mega Drive';
    document.querySelector('.console-icon').src = cdPlaying
      ? '/media/system/scd' : '/media/system/md';
    const modeChip = $('ra-mode-chip');
    const raAvailable = state.ra_availability === 'available';
    const raMode = state.ra_mode;
    let modeLabel, modeClass, modeAria;
    if (!raAvailable) {
      modeLabel = 'RA \u00b7 UNAVAILABLE';
      modeClass = 'unavailable';
      modeAria = state.ra_unavailable_reason === 'mega_cd'
        ? 'RetroAchievements unavailable for Mega CD'
        : 'RetroAchievements unavailable for this game';
    } else if (raMode === 'casual') {
      modeLabel = 'RA \u00b7 CASUAL';
      modeClass = 'casual';
      modeAria = 'RetroAchievements connected, Casual mode';
    } else if (raMode === 'hardcore') {
      modeLabel = 'RA \u00b7 HARDCORE';
      modeClass = 'hardcore';
      modeAria = 'RetroAchievements connected, Hardcore mode';
    } else {
      modeLabel = 'RA MODE UNKNOWN';
      modeClass = 'error';
      modeAria = 'RetroAchievements mode unknown';
    }
    $('chip-console').textContent = modeLabel;
    modeChip.classList.remove('casual', 'hardcore', 'unavailable', 'error');
    modeChip.classList.add(modeClass);
    modeChip.classList.toggle('connected', raAvailable);
    modeChip.querySelector('.dot').classList.toggle('g', raAvailable);
    modeChip.querySelector('.dot').classList.toggle('off', !raAvailable);
    modeChip.setAttribute('aria-label', modeAria);
    $('cd-game-note').classList.toggle('hidden', !cdPlaying);
    $('achhead').classList.toggle('hidden', cdPlaying);
    $('achrows').classList.toggle('hidden', cdPlaying);
    const presence = (state.rich_presence || '').trim();
    $('presence').classList.toggle('hidden', cdPlaying || !presence);
    if ($('presence-run').textContent !== presence) {
      $('presence-run').textContent = presence;
      layoutPresence();
    }
    $('presence-text').title = presence;
    if (!cdPlaying) {
      const s = state.summary || {};
      $('pcount').innerHTML = `<b>${s.unlocked ?? 0}</b> / ${s.total ?? 0} achievements`;
      $('ppts').innerHTML = `<b>${s.points ?? 0}</b> pts`;
      $('pbar').style.width = s.total ? (s.unlocked / s.total * 100) + '%' : '0%';
      renderAchList();
    }
  }

  // library header status
  if (state.cd_session) cdLaunched = true;
  else if (conn !== 'offline') cdLaunched = false;
  let sub;
  if (conn === 'offline' && cdLaunched)
    sub = 'Mega CD game running — no achievements; reconnects when you quit';
  else if (playing)
    sub = `Playing: ${state.game.title || ''} — pick another to switch`;
  else
    sub = IDLE_SUB[conn] || 'Pick a game to launch';
  $('lib-sub').textContent = sub;
  $('switch').classList.toggle('hidden', !state.user);
  // toggle only applies next launch: lock it while a session is active
  const sessionActive = conn === 'playing' || conn === 'identifying';
  $('toggle-input').disabled = sessionActive;
  $('switch').classList.toggle('locked', sessionActive);
  $('switch').title = sessionActive
    ? 'Return to the EverDrive menu to change achievements mode'
    : 'Achievements mode';
  // pill back to the achievements screen while browsing mid-game
  $('back-game').classList.toggle('hidden', !((playing || cdPlaying) && forceLibrary));
  if (playing || cdPlaying) $('back-game').textContent = `${activeGame.title} ▸`;

  if (state.toggle !== null && state.toggle !== undefined &&
      !$('switch').classList.contains('busy')) {
    $('toggle-input').checked = !!state.toggle;
    $('swlabel').textContent = state.toggle ? 'RA on' : 'RA off';
  }
}

$('change-game').onclick = () => { forceLibrary = true; renderState(); };
$('back-game').onclick = () => { forceLibrary = false; renderState(); };
function setExpandIcon(on) {
  const button = $('expand');
  button.innerHTML = on
    ? '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M6 2v4H2M10 14v-4h4M6 6 2 2M10 10l4 4"/></svg>'
    : '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M6 2H2v4M10 14h4v-4M2 6l4-4M14 10l-4 4"/></svg>';
  button.title = on ? 'Collapse list' : 'Expand list';
  button.setAttribute('aria-label', on ? 'Collapse achievement list' : 'Expand achievement list');
}

setExpandIcon(false);
$('expand').onclick = () => {
  const on = $('scr-game').classList.toggle('expanded');
  setExpandIcon(on);
};

function endLaunching() {
  if (launching?.timer) clearTimeout(launching.timer);
  launching = null;
}

// spinning Mega CD disc markup (loading screen + launch modal)
function discHTML(g) {
  return `<div class="discwrap big"><div class="disc spin">
    <img src="${faceUrl(g, 'front')}" alt="" data-ph="${esc(g.title)}"></div></div>`;
}

function startLaunching(g, seenIdentifying = false) {
  endLaunching();
  $('load-title').textContent = g.title;
  $('load-sub').textContent = 'Resetting console…';
  $('loadbox').innerHTML = g.system === 'mcd' ? discHTML(g) : spinBoxHTML(g);
  launching = {game: g, seenIdentifying, timer: setTimeout(() => {
    endLaunching();
    $('msg').textContent = `still waiting on ${g.title} — check the console`;
    renderState();
  }, 45000)};
  renderState();
}

// Mega CD launch: a brief spinning-disc modal that auto-dismisses back to the
// library (the cart drops off USB while the disc runs, so no persistent loader).
let cdTimer = null;
function hideCdLaunch() {
  if (cdTimer) { clearTimeout(cdTimer); cdTimer = null; }
  $('cdlaunch').classList.add('hidden');
  $('cd-disc').innerHTML = '';   // stop the spin
}
function showCdLaunch(g) {
  if (cdTimer) clearTimeout(cdTimer);
  $('cd-disc').innerHTML = discHTML(g);
  $('cd-title').textContent = g.title;
  $('cdlaunch').classList.remove('hidden');
  cdTimer = setTimeout(hideCdLaunch, 4000);
}

/* ================= achievement list (in-screen, touch) ================= */

function renderAchList() {
  const list = state.achievements || [];
  const shown = list.filter(a =>
    achFilter === 'all' || (achFilter === 'unlocked') === !!a.unlocked);
  const wrap = $('achrows');
  wrap.innerHTML = '';
  for (const a of shown) {
    const row = document.createElement('div');
    row.className = 'arow' + (a.unlocked ? ' done' : '');
    row.dataset.aid = a.id;
    row.innerHTML = `
      <img class="ab" src="${badgeUrl(a.badge, a.unlocked)}" alt="" loading="lazy">
      <div class="atext">
        <div class="atitle">${a.title}</div>
        <div class="adesc">${a.description}</div>
      </div>
      <div class="apts"><b>${a.points}</b><span>pts</span></div>`;
    wrap.appendChild(row);
  }
}

$('achfilter').addEventListener('click', e => {
  if (e.target.tagName !== 'BUTTON') return;
  achFilter = e.target.dataset.f;
  document.querySelectorAll('#achfilter button').forEach(b =>
    b.classList.toggle('on', b === e.target));
  renderAchList();
});

/* ================= unlock takeover ================= */

/* unlock chime: synthesized rising two-note + sparkle (WebAudio -- no
   asset, works offline). Browsers require a user gesture before audio,
   so the context unlocks on first touch; on the Pi's own screen launch
   chromium with --autoplay-policy=no-user-gesture-required. */
let actx = null;
document.addEventListener('pointerdown', () => {
  if (!actx) {
    try { actx = new (window.AudioContext || window.webkitAudioContext)(); }
    catch { return; }
  }
  if (actx.state === 'suspended') actx.resume();
}, {passive: true});

function playUnlockChime() {
  if (!actx || actx.state !== 'running') return;
  const t0 = actx.currentTime + 0.02;
  const master = actx.createGain();
  master.gain.value = 0.28;
  master.connect(actx.destination);
  const notes = [
    {f: 659.25, t: 0,    d: 0.16, type: 'triangle', g: 1},     // E5
    {f: 987.77, t: 0.11, d: 0.55, type: 'triangle', g: 1},     // B5
    {f: 1975.5, t: 0.11, d: 0.45, type: 'sine',     g: 0.22},  // sparkle
  ];
  for (const n of notes) {
    const o = actx.createOscillator(), env = actx.createGain();
    o.type = n.type;
    o.frequency.value = n.f;
    env.gain.setValueAtTime(0, t0 + n.t);
    env.gain.linearRampToValueAtTime(n.g, t0 + n.t + 0.015);
    env.gain.exponentialRampToValueAtTime(0.001, t0 + n.t + n.d);
    o.connect(env); env.connect(master);
    o.start(t0 + n.t); o.stop(t0 + n.t + n.d + 0.05);
  }
}

/* How long the unlock celebration stays on screen (toast + medal
   animations in style.css read this via --toast-dur). */
const TOAST_MS = 8000;
document.documentElement.style.setProperty('--toast-dur', TOAST_MS + 'ms');

/* Celebrations run one at a time through a FIFO queue — #toast is a
   single shared overlay, so concurrent unlocks must never touch it. */
const toastQueue = [];
let toastActive = false;

function enqueueUnlock(u) {
  // the list reflects the unlock immediately; only the takeover queues
  const row = document.querySelector(`.arow[data-aid="${u.id}"]`);
  if (row) {
    row.classList.add('done', 'pop');
    row.querySelector('.ab').src = badgeUrl(u.badge, true);
  }
  toastQueue.push(u);
  if (!toastActive) showNextToast();
}

function showNextToast() {
  const u = toastQueue.shift();
  if (!u) { toastActive = false; return; }
  toastActive = true;
  playUnlockChime();
  $('tmedal').src = badgeUrl(u.badge, true);
  $('tname').textContent = u.title;
  $('tpts').textContent = `+${u.points} points`;
  const t = $('toast');
  t.classList.remove('show'); void t.offsetWidth; t.classList.add('show');
  setTimeout(showNextToast, TOAST_MS);
}

/* ================= PWA: install + push notifications ================= */
// Service workers/notifications need a secure context. Plain
// http://<lan-ip> isn't one -- open the box's https:// URL instead
// (install the box CA once so it's trusted).

async function registerSW() {
  if (!('serviceWorker' in navigator)) return null;
  try { return await navigator.serviceWorker.register('/sw.js'); }
  catch (e) { console.warn('sw register failed', e); return null; }
}
const swReady = registerSW();

function b64ToU8(b64) {
  const pad = '='.repeat((4 - b64.length % 4) % 4);
  const raw = atob((b64 + pad).replace(/-/g, '+').replace(/_/g, '/'));
  return Uint8Array.from(raw, c => c.charCodeAt(0));
}

async function enableNotifications() {
  const bell = $('bell');
  try {
    if (!window.isSecureContext)
      throw new Error('needs a secure context — open the https:// address instead');
    const reg = await swReady;
    if (!reg) throw new Error('service worker unavailable');
    if (await Notification.requestPermission() !== 'granted')
      throw new Error('permission denied');
    const {publicKey} = await (await fetch('/api/push/vapid')).json();
    const sub = await reg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: b64ToU8(publicKey)});
    let r = await fetch('/api/push/subscribe', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(sub.toJSON())});
    if (!r.ok) throw new Error('subscribe failed');
    bell.classList.add('on');
    $('msg').textContent = 'unlock notifications on — sending a test…';
    $('msg').classList.remove('err');
    await fetch('/api/push/test', {method: 'POST'});
  } catch (err) {
    $('msg').textContent = 'notifications: ' + err.message;
    $('msg').classList.add('err');
  }
}
$('bell').onclick = enableNotifications;
// reflect an existing subscription in the bell
swReady.then(async reg => {
  if (reg && await reg.pushManager.getSubscription())
    $('bell').classList.add('on');
});

/* ================= websocket ================= */

function connect() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const ws = new WebSocket(`${proto}//${location.host}/ws`);
  ws.onmessage = e => {
    const msg = JSON.parse(e.data);
    if (msg.type === 'state') { state = msg; renderState(); }
    else if (msg.type === 'unlock') enqueueUnlock(msg);
  };
  ws.onclose = () => setTimeout(connect, 2000);
}
connect();

/* ================= achievements toggle ================= */

const TOGGLE_TIMEOUT_MS = 300000;

$('toggle-input').addEventListener('change', async e => {
  const on = e.target.checked;
  $('switch').classList.add('busy');
  $('swlabel').textContent = '…';
  $('msg').textContent = 'switching cores in every game folder over USB…';
  $('msg').classList.remove('err');
  const ctrl = new AbortController();
  const killer = setTimeout(() => ctrl.abort(), TOGGLE_TIMEOUT_MS);
  try {
    const r = await fetch('/api/toggle', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({on}), signal: ctrl.signal});
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || r.statusText);
    $('msg').textContent = j.message + ' — applies next launch';
    $('swlabel').textContent = on ? 'RA on' : 'RA off';
  } catch (err) {
    // revert the switch: the cart state didn't change (or is unknown)
    e.target.checked = !on;
    $('swlabel').textContent = e.target.checked ? 'RA on' : 'RA off';
    $('msg').textContent = err.name === 'AbortError'
      ? 'took too long — switch reverted; check the cart is connected and try again'
      : 'failed: ' + err.message;
    $('msg').classList.add('err');
  } finally {
    clearTimeout(killer);
    $('switch').classList.remove('busy');
  }
});

/* ================= game library ================= */

function faceUrl(g, face) {
  // version tag = chosen LaunchBox image id (or 'lr' for the libretro
  // fallback): busts the browser's day-long art cache exactly when the
  // selected artwork changes, and never otherwise
  const v = ((g.art || {})[face] || 'lr2').slice(0, 8);
  return `/api/art/${g.system}/${face}/${encodeURIComponent(g.stem)}?v=${v}`;
}

const esc = s => s.replace(/&/g, '&amp;').replace(/</g, '&lt;')
                  .replace(/>/g, '&gt;').replace(/"/g, '&quot;');

// Any img with data-ph swaps to a styled placeholder when its art 404s.
// Delegated in capture phase (error doesn't bubble); inline onerror
// strings kept breaking on quotes in game titles.
document.addEventListener('error', e => {
  const im = e.target;
  if (im.tagName !== 'IMG' || !im.dataset) return;
  if (im.dataset.slide !== undefined) {           // media slide: drop it
    const sl = im.closest('.mslide');
    if (sl) sl.remove();
  } else if (im.dataset.ph !== undefined) {       // cover: placeholder
    const s = document.createElement('span');
    s.className = 'ph';
    s.textContent = im.dataset.ph;
    im.replaceWith(s);
  }
}, true);

// Many LaunchBox back scans still include the spine strip on their right
// edge (the flattened insert). When the loaded back is wider than the
// 150:210 face, crop it to the back only and reuse the strip as the spine
// texture -- unless a dedicated spine scan already claimed the face.
document.addEventListener('load', e => {
  const im = e.target;
  if (im.tagName !== 'IMG' || !im.classList.contains('bimg')) return;
  const face = 150 / 210, ar = im.naturalWidth / im.naturalHeight;
  if (ar < 0.78) return;                       // plain back scan
  im.classList.add('wide');                    // back face: crop off the strip
  // ar > 1 means a full unwrapped insert (back|spine|front), not back+spine:
  // the right strip would be the front cover, so leave the procedural spine
  if (ar > 1) return;
  const box = im.closest('.gamebox');
  for (const sp of box ? box.querySelectorAll('.spine') : []) {
    if (sp.classList.contains('has-spine')) continue;
    sp.style.backgroundImage = `url('${im.src}')`;
    sp.style.backgroundPosition = '100% 50%';
    // scale so the strip beyond the 150:210 back exactly fills the 22px face
    sp.style.backgroundSize = `${22 / (1 - face / ar)}px 100%`;
    sp.classList.add('has-spine');
  }
}, true);

// flat cover for the library grid (fast: one lazy img, no 3D transforms)
function cartHTML(g) {
  const front = faceUrl(g, 'front');
  const shape = g.system === 'mcd' ? 'flatdisc' : 'flatbox';
  return `<div class="${shape}">
    <img src="${front}" loading="lazy" alt="" data-ph="${esc(g.title)}">
  </div>`;
}

// full spinning 3D case (front/back/spine) -- now the LAUNCH loading art
function spinBoxHTML(g) {
  const front = faceUrl(g, 'front');
  const safeTitle = esc(g.title);
  const spineFace = side => `<div class="face spine ${side}">
      <span class="sptitle">${safeTitle}</span>
      <img class="spimg" src="${faceUrl(g, 'spine')}" alt=""
        onerror="var s=this.closest('.spine');if(!s.style.backgroundImage)s.classList.remove('has-spine');this.remove()"
        onload="this.closest('.spine').classList.add('has-spine')">
    </div>`;
  return `<div class="boxwrap big"><div class="gamebox spin">
    <div class="face edge t"></div><div class="face edge b"></div>
    <div class="face back">
      <div class="bart" style="background-image:url('${front}')"></div>
      <img class="bimg" src="${faceUrl(g, 'back')}" alt="" onerror="this.remove()">
    </div>
    ${spineFace('l')}${spineFace('r')}
    <div class="face front">
      <img src="${front}" alt="" data-ph="${safeTitle}">
    </div>
  </div></div>`;
}

const LONGPRESS_MS = 550;

const sortTitle = g => (g.title || g.stem || '').toString();
const firstLetter = g => {
  const c = sortTitle(g).trim().charAt(0).toUpperCase();
  return c >= 'A' && c <= 'Z' ? c : '#';
};
const libVisible = () => library.filter(g =>
  (!sel.system.size    || sel.system.has(g.system)) &&
  (!sel.folder.size    || sel.folder.has(g.folder)) &&
  (!sel.publisher.size || sel.publisher.has(g.publisher)) &&
  (!sel.genre.size     || gameGenres(g).some(t => sel.genre.has(t))));

function gcard(g, i) {
  const card = document.createElement('div');
  card.className = 'gcard';
  card.dataset.i = i;   // index into sortedGames (delegated grid handlers)
  card.innerHTML = cartHTML(g) + `
    <div class="ginfo">
      <span class="sysflag ${g.system}">${g.system === 'mcd' ? 'Mega CD' : 'Mega Drive'}${g.year ? ' · ' + g.year : ''}</span>
      <span class="t">${g.title}</span>
      <span class="d">${g.description || ''}</span>
    </div>`;
  return card;
}

/* Cards are built ONCE per library load (buildLibrary); filter changes only
   toggle .hidden (applyFilters) instead of tearing the grid down. Card
   interaction is delegated to #grid, so no per-card listeners. */
let sortedGames = [];   // alphabetical; card order in the grid matches

// tap = modal (Launch / Details); LONG-PRESS = launch immediately
{
  const grid = $('grid');
  const gameOf = card => sortedGames[card.dataset.i];
  let timer = null, fired = false, pressCard = null;
  const cancel = () => { clearTimeout(timer); timer = null; pressCard = null; };
  grid.addEventListener('pointerdown', e => {
    const card = e.target.closest('.gcard');
    if (!card) return;
    pressCard = card;
    fired = false;
    timer = setTimeout(() => {
      fired = true;
      if (navigator.vibrate) navigator.vibrate(30);
      launch(gameOf(card));
    }, LONGPRESS_MS);
  });
  grid.addEventListener('pointerup', e => {
    const card = e.target.closest('.gcard');
    const pressed = pressCard;
    cancel();
    if (card && card === pressed && !fired) openModal(gameOf(card));
  });
  // pointerleave doesn't bubble; pointerout + relatedTarget check is the
  // delegated equivalent (only cancel when the pointer truly left the card)
  grid.addEventListener('pointerout', e => {
    const card = e.target.closest('.gcard');
    if (card && card === pressCard && !card.contains(e.relatedTarget)) cancel();
  });
  grid.addEventListener('pointercancel', cancel);
  grid.addEventListener('contextmenu', e => {
    if (e.target.closest('.gcard')) e.preventDefault();
  });
}

function buildLibrary() {
  const grid = $('grid');
  grid.innerHTML = '';
  sortedGames = [...library].sort((a, b) =>
    sortTitle(a).localeCompare(sortTitle(b), undefined, {sensitivity: 'base'}));
  let curLetter = null;
  sortedGames.forEach((g, i) => {
    const L = firstLetter(g);
    if (L !== curLetter) {           // alphabetical section header (spans row)
      curLetter = L;
      const h = document.createElement('div');
      h.className = 'lib-sec';
      h.id = 'sec-' + L;
      h.textContent = L;
      grid.appendChild(h);
    }
    grid.appendChild(gcard(g, i));
  });
  applyFilters();
}

function applyFilters() {
  const grid = $('grid');
  const visible = new Set(libVisible());
  const present = new Set();
  let shown = 0;
  let sec = null, secHas = false;
  for (const el of grid.children) {
    if (el.classList.contains('lib-sec')) {
      if (sec) sec.classList.toggle('hidden', !secHas);
      sec = el;
      secHas = false;
      continue;
    }
    if (!el.classList.contains('gcard')) continue;
    const g = sortedGames[el.dataset.i];
    const show = visible.has(g);
    el.classList.toggle('hidden', !show);
    if (show) {
      shown++;
      secHas = true;
      present.add(firstLetter(g));
    }
  }
  if (sec) sec.classList.toggle('hidden', !secHas);
  let empty = grid.querySelector('.empty');
  if (!shown) {
    if (!empty) {
      empty = document.createElement('div');
      empty.className = 'empty';
      grid.appendChild(empty);
    }
    empty.textContent = library.length
      ? 'No games match the current filters.'
      : 'No games indexed yet — tap ↻ Rescan.';
  } else if (empty) empty.remove();
  refreshLetterMenu(present);
  updateBadges();
}

/* ---- faceted filter bar (inclusion: ticked = shown; empty = all) ---- */

// distinct [value,label] pairs from a plucker returning raw values per game
function distinct(pluck) {
  const s = new Set();
  for (const g of library) for (const v of pluck(g)) if (v) s.add(v);
  return [...s].sort((a, b) => a.localeCompare(b)).map(v => [v, v]);
}

const FACETS = [
  {key: 'system', label: 'System', opts: () =>
    [['md', 'Mega Drive'], ['mcd', 'Mega CD']]
      .filter(([v]) => library.some(g => g.system === v))},
  {key: 'folder', label: 'Folder', opts: () => distinct(g => [g.folder])},
  {key: 'genre', label: 'Genre', search: true, opts: () => distinct(gameGenres)},
  {key: 'publisher', label: 'Publisher', search: true,
   opts: () => distinct(g => [g.publisher])},
];

function facetDropdownHTML(facet, options) {
  const rows = options.map(([v, label]) =>
    `<button class="checkrow ${sel[facet.key].has(v) ? 'on' : ''}" ` +
    `data-val="${encodeURIComponent(v)}">${esc(label)}</button>`).join('');
  const search = facet.search
    ? `<input class="fdrop-search" placeholder="Search ${esc(facet.label.toLowerCase())}…">`
    : '';
  return `<div class="fdrop" data-facet="${facet.key}">
    <button class="fdrop-btn" data-toggle>${facet.label}<span class="fbadge"></span><b>▾</b></button>
    <div class="fdrop-menu">${search}
      <div class="fdrop-actions"><button data-act="all">All</button><button data-act="clear">Clear</button></div>
      <div class="fdrop-opts">${rows}</div>
    </div>
  </div>`;
}

function buildFilterBar() {
  // drop any selections whose values no longer exist in the library
  for (const facet of FACETS) {
    const live = new Set(facet.opts().map(([v]) => v));
    for (const v of [...sel[facet.key]]) if (!live.has(v)) sel[facet.key].delete(v);
  }
  saveFilters();
  let html = '';
  for (const facet of FACETS) {
    const options = facet.opts();
    if (options.length) html += facetDropdownHTML(facet, options);
  }
  html += `<div class="fdrop letter">
    <button class="fdrop-btn" data-toggle>A–Z<b>▾</b></button>
    <div class="fdrop-menu"><div class="fdrop-opts letters" id="letter-opts"></div></div>
  </div>`;
  html += `<button class="fdrop-clear" data-clearall>Clear all</button>`;
  $('filterbar').innerHTML = html;
  updateBadges();
}

function refreshLetterMenu(present) {
  const box = document.getElementById('letter-opts');
  if (!box) return;
  box.innerHTML = ALPHA.filter(L => present.has(L))
    .map(L => `<button class="checkrow" data-jump="${L}">${L}</button>`).join('')
    || '<div class="fempty">—</div>';
}

function updateBadges() {
  const bar = $('filterbar');
  bar.querySelectorAll('.fdrop[data-facet]').forEach(drop => {
    const b = drop.querySelector('.fbadge');
    if (b) b.textContent = sel[drop.dataset.facet].size || '';
  });
  const ca = bar.querySelector('[data-clearall]');
  if (ca) ca.classList.toggle('active', FKEYS.some(k => sel[k].size));
}

function refreshChecks() {
  $('filterbar').querySelectorAll('.fdrop[data-facet]').forEach(drop => {
    const key = drop.dataset.facet;
    drop.querySelectorAll('.checkrow[data-val]').forEach(row =>
      row.classList.toggle('on', sel[key].has(decodeURIComponent(row.dataset.val))));
  });
  updateBadges();
}

function closeAllDrops() {
  $('filterbar').querySelectorAll('.fdrop.open').forEach(d => d.classList.remove('open'));
}

/* ---- game modal ---- */
let modalGame = null;

function openModal(g) {
  modalGame = g;
  const box = document.querySelector('.mbox');
  box.innerHTML = `<img id="m-cover" src="${faceUrl(g, 'front')}" alt=""
    data-ph="${esc(g.title)}">`;
  $('m-sys').textContent = g.system === 'mcd' ? 'Mega CD' : 'Mega Drive';
  $('m-title').textContent = g.title;
  $('m-meta').textContent = [g.year, g.developer, g.genre]
    .filter(Boolean).join(' · ');
  $('m-note').textContent = g.system === 'mcd'
    ? 'Mega CD: no achievements — the cart leaves USB while it runs.' : '';
  buildMedia(g);
  $('m-desc').textContent = g.description ||
    'No description available for this game.';
  $('m-details').classList.remove('hidden');
  const lb = $('m-launch');
  lb.disabled = !cartOnline();
  lb.textContent = lb.disabled ? 'Cart offline' : '▶ Launch';
  $('modal').classList.remove('hidden');
}

function closeModal() {
  $('modal').classList.add('hidden');
  $('m-media').innerHTML = '';  // stops any playing video
  modalGame = null;
}

// swipeable media strip: video first (tap to play), then LaunchBox
// screenshots, then libretro title/in-game shots. Broken images take
// their whole slide with them (data-slide on the delegated handler).
function ytId(url) {
  const m = (url || '').match(/(?:youtu\.be\/|[?&]v=|\/embed\/)([\w-]{11})/);
  return m ? m[1] : null;
}

function buildMedia(g) {
  const row = $('m-media');
  row.innerHTML = '';
  const slides = [];
  const yt = ytId(g.video);
  if (yt) slides.push(`<div class="mslide video" data-yt="${yt}">
      <img src="https://img.youtube.com/vi/${yt}/hqdefault.jpg" alt="">
      <span class="playbtn">▶</span></div>`);
  for (const f of g.shots || [])
    slides.push(`<div class="mslide"><img src="/api/lbimg/${encodeURIComponent(f)}" loading="lazy" alt="" data-slide="1"></div>`);
  slides.push(`<div class="mslide"><img src="${faceUrl(g, 'title')}" loading="lazy" alt="" data-slide="1"></div>`);
  slides.push(`<div class="mslide"><img src="${faceUrl(g, 'snap')}" loading="lazy" alt="" data-slide="1"></div>`);
  row.innerHTML = slides.join('');
  row.classList.toggle('hidden', !slides.length);
  row.querySelectorAll('.mslide.video').forEach(sl => {
    sl.onclick = () => {
      sl.innerHTML = `<iframe src="https://www.youtube.com/embed/${sl.dataset.yt}?autoplay=1&playsinline=1"
        referrerpolicy="strict-origin-when-cross-origin"
        allow="autoplay; encrypted-media" allowfullscreen></iframe>`;
      sl.onclick = null;
    };
  });

  resetPager();
}

/* Slideshow gallery. CSS scroll-snap (mandatory + snap-stop:always) owns
   the paging -- one image per swipe, no overshoot -- so there's no JS
   "settle" fighting the fling. This wires the dots + prev/next arrows to
   the native scroll position. Bound ONCE against the persistent #m-media
   (buildMedia just repopulates slides + calls resetPager), so listeners
   never stack across modal opens. */
function slideCount() {
  return $('m-media').querySelectorAll('.mslide').length;
}
function slideW() {
  return $('m-media').clientWidth || 1;
}
function curSlide() {
  return Math.round($('m-media').scrollLeft / slideW());
}
function paintDots(cur = curSlide()) {
  const n = slideCount();
  $('m-dots').innerHTML = Array.from({length: n}, (_, k) =>
    `<i class="${k === cur ? 'on' : ''}" data-i="${k}"></i>`).join('');
  const solo = n < 2;
  $('m-dots').classList.toggle('hidden', solo);
  $('m-prev').classList.toggle('hidden', solo);
  $('m-next').classList.toggle('hidden', solo);
}
function goSlide(i) {
  const n = slideCount();
  const clamped = Math.max(0, Math.min(n - 1, i));
  $('m-media').scrollTo({left: clamped * slideW(), behavior: 'smooth'});
  paintDots(clamped);
}
function resetPager() {
  $('m-media').scrollTo({left: 0});
  paintDots(0);
}
function setupPager() {
  const row = $('m-media');
  // active dot follows the native scroll (snap does the settling)
  row.addEventListener('scroll', () => paintDots(), {passive: true});
  $('m-prev').onclick = () => goSlide(curSlide() - 1);
  $('m-next').onclick = () => goSlide(curSlide() + 1);
  $('m-dots').addEventListener('click', e => {
    const dot = e.target.closest('i[data-i]');
    if (dot) goSlide(+dot.dataset.i);
  });
  // broken slides remove themselves async (404 handler): clamp + repaint
  new MutationObserver(() => {
    const cur = Math.min(curSlide(), slideCount() - 1);
    row.scrollTo({left: Math.max(0, cur) * slideW()});
    paintDots();
  }).observe(row, {childList: true});
  // left/right arrow keys page while the modal is open
  document.addEventListener('keydown', e => {
    if (!$('modal').classList.contains('hidden') && slideCount() > 1) {
      if (e.key === 'ArrowLeft') goSlide(curSlide() - 1);
      else if (e.key === 'ArrowRight') goSlide(curSlide() + 1);
    }
  });
}
setupPager();

$('m-close').onclick = closeModal;
$('modal').addEventListener('pointerdown', e => {
  if (e.target === $('modal')) closeModal();  // tap outside the card
});
$('m-launch').onclick = () => {
  const g = modalGame;
  closeModal();
  if (g) launch(g);
};

const cartOnline = () => state && !['offline', 'starting', 'logging-in',
                                    'login-failed'].includes(state.connection);

async function launch(g) {
  if ($('switch').classList.contains('busy')) {
    $('msg').textContent = 'achievements switch is being applied — wait for it to finish';
    $('msg').classList.add('err');
    return;
  }
  // No local Mega CD block: the daemon knows whether the cart is actually
  // off the bus and answers 409 with the message when a disc really plays.
  if (!cartOnline()) {
    $('msg').textContent = 'cart offline — power the console on to launch games';
    $('msg').classList.add('err');
    return;
  }
  $('msg').textContent = '';
  $('msg').classList.remove('err');
  if (g.system !== 'mcd') {
    forceLibrary = false;
    startLaunching(g);   // spinning-box loading screen, immediately
  } else {
    showCdLaunch(g);     // brief spinning-disc modal, auto-dismisses
    $('msg').textContent = `launching ${g.title}…`;
  }
  try {
    const r = await fetch('/api/launch', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({path: g.path})});
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || r.statusText);
    if (g.system === 'mcd') {
      cdLaunched = true;
      // the persistent "Mega CD game running…" status already lives in
      // lib-sub (reactive on state.cd_session) -- don't duplicate it here.
      $('msg').textContent = '';
    } else {
      $('load-sub').textContent = 'Console reset — waiting for the game to boot…';
    }
  } catch (err) {
    endLaunching();
    if (g.system === 'mcd') hideCdLaunch();
    $('msg').textContent = 'launch failed: ' + err.message;
    $('msg').classList.add('err');
    renderState();
  } finally {
    $('grid').classList.remove('dim');
  }
}

async function loadLibrary(refresh) {
  const btn = $('refresh');
  if (refresh) {
    btn.classList.add('busy');
    $('msg').textContent = 'scanning SD card…';
    $('msg').classList.remove('err');
  }
  try {
    const r = await fetch('/api/games' + (refresh ? '/refresh' : ''),
                          refresh ? {method: 'POST'} : {});
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || r.statusText);
    library = j.games || [];
    if (refresh) $('msg').textContent = `${library.length} games indexed`;
    buildFilterBar();
    buildLibrary();
    renderState(); // resolve artwork for an active Mega CD session
  } catch (err) {
    $('msg').textContent = 'library: ' + err.message;
    $('msg').classList.add('err');
  } finally {
    btn.classList.remove('busy');
  }
}

$('refresh').onclick = () => loadLibrary(true);

/* faceted filter bar: open/close dropdowns, tick values, per-group and
   global clear, letter jump. Delegated so it survives buildFilterBar. */
$('filterbar').addEventListener('click', e => {
  const toggle = e.target.closest('[data-toggle]');
  if (toggle) {
    const drop = toggle.closest('.fdrop');
    const open = drop.classList.contains('open');
    closeAllDrops();
    drop.classList.toggle('open', !open);
    e.stopPropagation();          // don't let the click-away close it again
    return;
  }
  if (e.target.closest('[data-clearall]')) {
    for (const k of FKEYS) sel[k].clear();
    saveFilters(); refreshChecks(); applyFilters();
    return;
  }
  const act = e.target.closest('[data-act]');
  if (act) {
    const facet = FACETS.find(f => f.key === act.closest('.fdrop').dataset.facet);
    if (act.dataset.act === 'clear') sel[facet.key].clear();
    else facet.opts().forEach(([v]) => sel[facet.key].add(v));
    saveFilters(); refreshChecks(); applyFilters();
    return;
  }
  const jump = e.target.closest('[data-jump]');
  if (jump) {
    const secEl = document.getElementById('sec-' + jump.dataset.jump);
    if (secEl) {
      // scroll the grid's OWN scroller, never scrollIntoView -- the latter
      // also scrolls ancestors (the page root, taller than the visible
      // viewport), which shoves the header off-screen with no way back.
      const grid = $('grid');
      grid.scrollTo({top: secEl.offsetTop - grid.offsetTop - 8,
                     behavior: 'smooth'});
    }
    closeAllDrops();
    return;
  }
  const row = e.target.closest('.checkrow[data-val]');
  if (row) {
    const key = row.closest('.fdrop').dataset.facet;
    const v = decodeURIComponent(row.dataset.val);
    if (sel[key].has(v)) sel[key].delete(v); else sel[key].add(v);
    row.classList.toggle('on', sel[key].has(v));
    saveFilters(); updateBadges(); applyFilters();
    return;
  }
});
// live search within a facet menu
$('filterbar').addEventListener('input', e => {
  const s = e.target.closest('.fdrop-search');
  if (!s) return;
  const q = s.value.toLowerCase();
  s.closest('.fdrop-menu').querySelectorAll('.checkrow[data-val]').forEach(o =>
    o.style.display = o.textContent.toLowerCase().includes(q) ? '' : 'none');
});
// tap outside closes any open dropdown
document.addEventListener('click', e => {
  if (!e.target.closest('#filterbar')) closeAllDrops();
});

loadLibrary(false);
