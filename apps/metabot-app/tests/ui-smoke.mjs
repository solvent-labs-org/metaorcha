// UI smoke test for index.html: static checks only, no browser, no network.
// Run: node tests/ui-smoke.mjs
//
// It asserts:
//   1. every element id the UI contract relies on exists exactly once;
//   2. the last <script> block parses (node --check on the extracted text);
//   3. every fetch() that POSTs sends X-MetaChat: 1 and Content-Type: application/json
//      (all POSTs go through one helper, api());
//   4. no `innerHTML =` assignment takes a variable; the non-empty assignments are the
//      chat-bubble markdown and the constant brand mark (allowlisted, justified below);
//   5. no http:// or https:// URL except the SVG namespace literal (vendor loads are root-relative);
//   6. the hygiene rules of contract section 11: esc() escapes the single quote, the
//      bracketed-paste end sequence is stripped before a paste reaches the terminal, a
//      ResizeObserver watches #termPanel and #term, textContent is the way receipt / CLI /
//      prompt text is rendered, the localStorage keys stay fc* and the named ones, no
//      literal desk names, the vendored fonts are loaded with the token, the vault name;
//   7. the section 13 hotfixes kept from main: palette, slash commands, aliases,
//      sendSize on open / switch / fit, REACH_TXT wording;
//   8. sections 14-23 as far as a static read can prove them: the 22.1 tokens and the
//      22.5 contrast matrix in both schemes, the font-size floor, every var(--x) defined,
//      the 44 px tap targets (height and width for icon buttons), the 22.5 frame (activity
//      bar, side bar, tabs, 760 px canvas, right panel, status line), portrait sheets,
//      reduced motion, the update hint, the New desk sheet, /new and /all, the Desktop
//      header words, user-mode hiding in CSS, the mark, the allowed copy, no banned words,
//      the app named MetaBot where shown;
//   9. (child process) tests/ui-smoke-dom.mjs runs the script under a fake DOM
//      against the contract's JSON shapes: tree, tabs, pages, panel, receipt viewer, composer;
//  10. section 25.4 (U1–U7) as far as a static read can prove it: the guided sign-in card with no
//      timed write, the account picker per engine, the Extend page, workflows from a task, the engine
//      chip from /api/extend, one-tap keys through /api/keys (never for a typed yes), the five starts.
import { readFileSync, writeFileSync, unlinkSync, existsSync } from 'node:fs';
import { execFileSync, spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, '..', 'src', 'metabot_app');
const html = readFileSync(join(root, 'index.html'), 'utf8');

let failures = 0, passes = 0, skipped = 0;
function ok(cond, what) {
  if (cond) { passes++; console.log('ok   ' + what); }
  else { failures++; console.log('FAIL ' + what); }
}

// ---- 1. ids ----------------------------------------------------------------
const IDS = [
  // the frame (22.5): activity bar, side bar, tabs, header, canvas, right panel, status line
  'app', 'main', 'abar', 'side', 'sideHead', 'sideTitle', 'addBtn', 'sideRefresh', 'sideTools', 'sideSearch', 'sideBody',
  'chat', 'tabs', 'tabFill', 'hmark', 'dot', 'wsName', 'reach', 'deskBtn', 'termBtn', 'moreBtn', 'menu',
  'centre', 'lane', 'banner', 'log', 'composer', 'cwrap', 'palette', 'plusMenu', 'f', 'modeRow', 'modeMsg', 'modeRun',
  'plusBtn', 'text', 'runPill', 'send', 'page', 'dock', 'dockHead', 'dockTitle', 'dockExpand', 'dockClose', 'dockBody',
  'termBar', 'termBarName', 'termBarOpen', 'split', 'termPanel', 'termHead', 'tname', 'tdot',
  'tlCopy', 'tlCli', 'tlY', 'tlDock', 'tlG', 'tlR', 'termErr', 'term',
  'statusBar', 'sbStreams', 'sbRuns', 'sbNeed', 'sbVnc', 'sbBuild', 'sbMode',
  // the desktop panel and the sheets
  'deskPanel', 'deskBar', 'deskStatus', 'deskReload', 'deskClose', 'deskFrame',
  'deskSheet', 'sheetTitle', 'sheetCls', 'sheetEquals', 'sheetTerm', 'sheetKeys', 'scrim',
  'spawnSheet', 'spawnKinds', 'spawnAccount', 'spawnCwd', 'spawnPath', 'spawnLabel', 'spawnErr', 'spawnGo',
  'runSheet', 'runChips', 'chipEngine', 'chipAccount', 'chipProject', 'chipMode', 'chipCost', 'runEquals', 'chipSuggest', 'runWhy',
  'cliSheet', 'cliTitle', 'cliRows', 'copySheet', 'copyTitle', 'copyList', 'toast',
];
for (const id of IDS) {
  const n = (html.match(new RegExp('\\sid="' + id + '"', 'g')) || []).length;
  ok(n === 1, 'id "' + id + '" present once (found ' + n + ')');
}
// ids the pages create at runtime (looked up by $()) must be created somewhere
for (const id of ['runsList', 'runsProject', 'runsToday', 'runsTotal', 'sdkRaw', 'runIds', 'acctForm', 'acctNew', 'wfNew', 'wfForm', 'modForm', 'abMode', 'abSpace',
                  'wfTaskText', 'wfFolder', 'wfDraft', 'signinCode']) {
  ok(html.includes("id: '" + id + "'") || html.includes('id="' + id + '"'), 'runtime id "' + id + '" is created');
}
// retired with the 22.5 frame: the module strip, the rail, the dock minimise/position buttons, the header rename, the demo toggle, versions in the status bar
for (const id of ['modStrip', 'rail', 'railHead', 'railBtn', 'desks', 'armedN', 'dockMin', 'dockPos', 'dockIcon', 'renBtn', 'demoToggle', 'sbVer', 'railFoot', 'receiptView', 'catOut']) {
  ok(!html.includes('id="' + id + '"') && !html.includes("id: '" + id + "'"), 'retired id "' + id + '" is gone');
}

// ---- 2. the last <script> block parses ------------------------------------
const blocks = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]);
ok(blocks.length >= 1, 'found ' + blocks.length + ' <script> block(s)');
const js = blocks[blocks.length - 1];
const tmp = join(process.env.TMPDIR || '/tmp', 'metabot-ui-smoke-' + process.pid + '.js');
writeFileSync(tmp, js);
try {
  execFileSync(process.execPath, ['--check', tmp], { stdio: 'pipe' });
  ok(true, 'node --check passes on the last <script> block');
} catch (e) {
  ok(false, 'node --check: ' + String(e.stderr || e.message).split('\n')[0]);
} finally {
  try { unlinkSync(tmp); } catch (e) {}
}

// ---- 3. every POST carries the CSRF headers --------------------------------
const fetchCalls = [];
let idx = 0;
while ((idx = js.indexOf('fetch(', idx)) !== -1) {
  let depth = 0, i = idx + 5;
  for (; i < js.length; i++) {
    if (js[i] === '(') depth++;
    else if (js[i] === ')') { depth--; if (depth === 0) break; }
  }
  fetchCalls.push(js.slice(idx, i + 1));
  idx = i;
}
ok(fetchCalls.length > 0, fetchCalls.length + ' fetch() call(s) found');
const posts = fetchCalls.filter(c => /method\s*:\s*['"]POST['"]/.test(c));
ok(posts.length === 1, 'exactly one POST site, the api() helper (found ' + posts.length + ')');
for (const c of posts) {
  ok(/['"]X-MetaChat['"]\s*:\s*['"]1['"]/.test(c), 'POST sends X-MetaChat: 1');
  ok(/['"]Content-Type['"]\s*:\s*['"]application\/json['"]/.test(c), 'POST sends Content-Type: application/json');
}
ok(!/XMLHttpRequest|navigator\.sendBeacon/.test(js), 'no XMLHttpRequest / sendBeacon');
const gets = fetchCalls.filter(c => !/method\s*:/.test(c));
for (const c of gets) ok(/tok\(/.test(c), 'GET uses tok(): ' + c.slice(0, 60).replace(/\s+/g, ' '));
for (const route of ['/api/send', '/api/pane', '/api/spawn', '/api/run', '/api/kill', '/api/accounts',
                     '/api/cli/read', '/api/cli/desk', '/api/mb/verify', '/api/sdk/verify', '/api/modules/reload',
                     '/api/alias', '/api/keys', '/api/modules/new']) {
  ok(new RegExp("api\\('" + route.replace(/\//g, '\\/') + "'").test(js), 'write route goes through api(): ' + route);
}
// timeouts and an in-flight guard on the polls (finding ui-network-12)
ok(/function abortIn\(ms\)/.test(js) && /new AbortController\(\)/.test(js) && /signal: a\.signal/.test(js), 'api() and getJ() carry an AbortController timeout');
ok(/const INFLIGHT = \{\}/.test(js) && /if \(INFLIGHT\[key\]\) return INFLIGHT\[key\]/.test(js), 'getJ() skips a tick while the previous call is pending');

// ---- 4. innerHTML discipline ----------------------------------------------
const assigns = [...js.matchAll(/([A-Za-z_$][\w$.()' ]*)\.innerHTML\s*=\s*([^;\n]+)/g)];
const allowed = a => a[2].trim() === "''" || (a[1].trim() === 'bodyDiv' && a[2].trim() === 'md(body)') || a[2].trim() === 'MARK_SVG';
const bad = assigns.filter(a => !allowed(a));
ok(assigns.length > 0, assigns.length + ' innerHTML assignment(s) inspected');
ok(bad.length === 0, 'no innerHTML assignment outside the allowlist' + (bad.length ? ': ' + bad.map(a => a[0]).join(' | ') : ''));
const RISKY = /receipt|stdout|stderr|prompt|raw|argv|pc\b|tab\b|env\b|meta\b|catalog|CATALOG|verdict|label|run_id|answer|final/;
ok(!assigns.some(a => RISKY.test(a[2])), 'no innerHTML assignment names a receipt / CLI / prompt / catalogue value');
ok(/const MARK_SVG = '<svg class="mark"[^']*<\/svg>';/.test(js), 'MARK_SVG is one string literal (the inline mark), no concatenation');
ok(!/insertAdjacentHTML|outerHTML\s*=|document\.write\(/.test(js), 'no insertAdjacentHTML / outerHTML / document.write');
ok(/document\.createTextNode\(String\(c\)\)/.test(js), 'h() appends strings as text nodes');
ok(/textContent/.test(js) && (js.match(/text:/g) || []).length > 80, 'receipt / CLI / prompt text goes in through textContent (text: attrs)');
// the icon set is built with createElementNS, never markup (22.1)
ok(/document\.createElementNS\(SVG_NS, 'svg'\)/.test(js) && /document\.createElementNS\(SVG_NS, 'path'\)/.test(js), 'icons are built with createElementNS');
ok(!/innerHTML\s*=\s*[^;]*<svg/.test(js.replace(/const MARK_SVG = '[^']*';/, '')), 'no SVG string is assigned as markup besides MARK_SVG');

// ---- 5. no external URLs ---------------------------------------------------
const SVG_NS_LITERAL = "const SVG_NS = 'http://www.w3.org/2000/svg';";
ok(html.includes(SVG_NS_LITERAL), 'the SVG namespace is the one named literal');
const urls = html.replace(SVG_NS_LITERAL, '').match(/https?:\/\/[^\s"'<>)]+/g) || [];
ok(urls.length === 0, 'no http(s):// URL in the page besides the SVG namespace' + (urls.length ? ': ' + urls.join(', ') : ''));
const srcs = [...html.matchAll(/\b(?:src|href)=["']([^"']+)["']/g)].map(m => m[1]);
ok(srcs.every(s => s.startsWith('/vendor/') || s === '#' || s === 'about:blank'), 'every src/href is vendor-relative, # or about:blank: ' + srcs.join(' '));
for (const v of ['/vendor/xterm.js', '/vendor/addon-fit.js', '/vendor/xterm.css', '/vendor/mbverify.js', '/vendor/novnc/vnc.html', '/vendor/fonts/']) {
  ok(html.includes(v), 'loads ' + v);
}

// ---- 6. section 11 hygiene -------------------------------------------------
ok(/const esc = s => String\(s\)\.replace\(\/\[&<>"'\]\/g/.test(js), "esc() escapes & < > \" and '");
ok(/\\x1b\\\[20\[01\]~/.test(js) || /\\x1b\\\[201~/.test(js), 'the bracketed-paste end sequence is stripped (cleanText)');
ok(/function termPaste\([\s\S]*?cleanText\(/.test(js), 'termPaste() runs cleanText() before the write');
ok(/function termPaste\([\s\S]*?whenConnected\(name, \(\) => termWriteTo\(name,/.test(js), 'termPaste() writes to the desk it was meant for (termWriteTo)');
ok(/CTRL_RE = \/\[\\x00-\\x08\\x0b-\\x1f\\x7f\]\/g/.test(js), 'control characters are stripped');
ok(/new ResizeObserver\([\s\S]{0,200}\)\.observe\(panel\)/.test(js) && /const panel = \$\('termPanel'\)/.test(js), 'ResizeObserver on #termPanel refits the terminal');
ok(/for \(const k in TERMS\) TERMS\[k\]\.el\.classList\.remove\('live'\)/.test(js), 'exactly one desk terminal is live at a time');
ok(/indexedDB\.open\('metachat-vault', 1\)/.test(js) && /createObjectStore\('receipts'\)/.test(js), "IndexedDB vault 'metachat-vault' store 'receipts'");
ok(/MBVerify\.verify\(bytes\)/.test(js) && /\/api\/mb\/receipt\//.test(js), 'tablet check runs MBVerify.verify on the receipt bytes');
ok(/checked on this tablet with code this PC served/.test(js), 'the honest label is present');
ok(/e\.code === 4001 \|\| e\.code === 4004/.test(js), 'close codes 4001 / 4004 stop the reconnect loop');
ok(/e\.code >= 4000 && e\.code < 5000/.test(js) && /rec\.stale = true/.test(js), 'every other 4xxx code is final: stale, reconnect on the next reveal (finding ui-network-10)');
ok(/rec\.term\.dispose\(\)/.test(js), 'a killed or vanished desk disposes its xterm (finding ui-state-8)');
ok(/function termVisible\(/.test(js) || /const termVisible = /.test(js), 'a hidden terminal does not reconnect (termVisible)');
// localStorage keys: fc* only, and the named ones
const lsKeys = new Set([...js.matchAll(/localStorage\.([A-Za-z_]\w*)/g)].map(m => m[1]));
const KNOWN = new Set(['fcDesk', 'fcAliases', 'fcSeen', 'fcSheetH', 'fcTermW', 'fcDemo', 'fcDock', 'fcMode', 'fcSetup', 'fcScheme', 'fcSide', 'fcTabs', 'fcTree', 'fcRecents']);
ok([...lsKeys].every(k => KNOWN.has(k)), 'localStorage keys are ' + [...lsKeys].sort().join(', '));
for (const k of ['fcScheme', 'fcSide', 'fcTabs', 'fcTree', 'fcRecents', 'fcDock', 'fcMode', 'fcSetup']) ok(lsKeys.has(k), 'key ' + k + ' is used');
ok(!lsKeys.has('fcGroups'), 'fcGroups (the rail groups) is retired');
ok(!/localStorage\.(getItem|setItem)\(/.test(js), 'no localStorage.getItem/setItem with a computed key');
ok(/function readJSON\(key, fallback\)/.test(js) && /readJSON\('fcAliases'/.test(js) && /readJSON\('fcDock'/.test(js) && /readJSON\('fcTabs'/.test(js), 'stored JSON is read through readJSON() (finding ui-network-11)');
ok(/META\.default_desk/.test(js) && !/default_desk\s*[:=]\s*['"][^'"]+['"]/.test(js), 'default desk comes from /api/meta, never a literal in the page');
// fonts: four mono files, loaded through tok(); the pixel display face is gone
for (const f of ['IBMPlexMono-latin-400.woff2', 'IBMPlexMono-latin-500.woff2', 'IBMPlexMono-latin-600.woff2', 'FragmentMono-latin-400.woff2']) {
  ok(js.includes("'" + f + "'") && existsSync(join(root, 'vendor', 'fonts', f)), 'font ' + f + ' is referenced and present in vendor/fonts/');
}
ok(!/Geist/.test(html) && !existsSync(join(root, 'vendor', 'fonts', 'GeistPixel-Line.woff2')), 'no Geist face: not referenced, file removed');
ok(/tok\('\/vendor\/fonts\/' \+ file\)/.test(js), 'font URLs carry the token');
ok(!/visualViewport/.test(js), 'no visualViewport JS');
ok(/\.hidden \{ display:none !important; \}/.test(html) && (html.match(/class="sheet hidden"/g) || []).length === 5, 'one .hidden rule, five .sheet drawers (desk, spawn, run, cli, copy)');
ok(/const SHEETS = \['copySheet', 'spawnSheet', 'cliSheet', 'deskSheet', 'runSheet'\]/.test(js), 'the sheet manager knows all five sheets');
for (const s of ['/api/run', 'engines.list', 'read-only', "'/api/projects'", "'/api/accounts'"]) ok(js.includes(s), 'composer Run mode references ' + s);
for (const s of ["'/api/modules'", "'/api/catalog'", '/api/cli/read', '/api/cli/desk', 'params_ui']) ok(js.includes(s), 'module/catalogue wiring references ' + s);
ok(/getJ\('\/api\/tree'\)/.test(js) && /TREE_404 = true/.test(js) && /function buildTree\(\)/.test(js), 'the tree reads GET /api/tree and falls back to a client build on 404 (22.3)');

// ---- 7. section 13 hotfixes kept from main ----------------------------------
ok(/function sendSize\(rec\)/.test(js) && /type: ?'resize'/.test(js), 'sendSize(rec) sends the {type:resize} control frame');
ok(/rec\.ws\.onopen = \(\) => \{[\s\S]*?sendSize\(rec\)/.test(js), 'sendSize on ws open');
ok(/function switchTerm\(desk\)[\s\S]*?requestAnimationFrame\(\(\) => \{[^\n]*sendSize\(rec\)/.test(js), 'sendSize on switch');
ok(/function fitCur\(\)[\s\S]*?sendSize\(rec\)/.test(js), 'sendSize on every fit');
ok(/new ResizeObserver\([\s\S]{0,200}\)\.observe\(\$\('term'\)\)/.test(js), 'ResizeObserver on #term');
ok(/t\.onResize\(/.test(js), 'xterm onResize is kept as well');
ok(/REACH_TXT = \{inbox: 'live', pane: 'chat → terminal · not armed', idle: 'no CLI running', none: 'not armed · no desk here'\}/.test(js), "REACH_TXT wording from main (idle: 'no CLI running')");
ok(/async function fetchAliases\(\)/.test(js) && /'\/api\/aliases'/.test(js) && /aliases = Object\.assign\(\{\}, a\.aliases\)/.test(js), 'fetchAliases() reads GET /api/aliases and replaces, never merges (finding ui-network-7)');
ok(/async function setAlias\(name, alias\)[\s\S]*?const r = await api\('\/api\/alias'[\s\S]*?if \(!r\.ok\)/.test(js), 'setAlias() awaits the POST and reverts on failure');
ok((js.match(/\.slice\(0, 48\)/g) || []).length >= 2, 'alias 48-char cap on rename and /rename');
for (const c of ['handoff', 'to', 'all', 'new', 'arm', 'start', 'rename', 'desks', 'run', 'a2a', 'mcp', 'desktop']) ok(new RegExp("^\\s+" + c + ":\\s+\\{hint:", 'm').test(js), 'slash command /' + c + ' is in SLASH');
ok(/function palette\(\)/.test(js) && /function slash\(v\)/.test(js) && /function quoteName\(n\)/.test(js) && /function prow\(/.test(js), 'palette(), slash(), quoteName(), prow() exist');
ok(/e\.key === 'Escape'/.test(js) && /e\.key === 'Enter' && !e\.shiftKey/.test(js), 'Esc closes the palette, Enter picks or submits');
ok(/function hidePal\(\)[^\n]*composerPlaceholder\(\)/.test(js), 'the placeholder is restored whenever the palette closes (finding ui-state-10)');
ok(/ev\.via === 'desk'/.test(js), "via:'desk' events are rendered");
ok(!/'floor'/i.test(js) && /treeNode\('ended', 'Ended'/.test(js) && /treeNode\('inbox', String\(t\.inbox\.label/.test(js), "B1: the tree groups are Ended and the inbox adapter's own bucket, under its label; no floor group");
ok(/let SUBMITTING = false/.test(js) && /if \(SUBMITTING\) return;/.test(js), 'one submit at a time (finding ui-state-1)');
ok(/const normMsg = /.test(js) && /function settlePending\(ev\)/.test(js) && /async function refill\(\)[\s\S]*?if \(settlePending\(ev\)\) return;/.test(js), 'optimistic bubbles settle on the for-prefixed echo and survive a refill (findings ui-state-3/4)');

// ---- 8. sections 14-23: what a static read can prove -------------------------
const css = (html.match(/<style>([\s\S]*?)<\/style>/) || [, ''])[1].replace(/\/\*[\s\S]*?\*\//g, '');
const rules = [...css.matchAll(/([^{}]+)\{([^{}]*)\}/g)].map(m => ({ sel: m[1].trim(), body: m[2] }));
// 22.6 tokens: two schemes, Apple system greys, one blue, iMessage bubbles; the values the page ships
const block = name => (css.match(new RegExp(name.replace(/[.]/g, '\\.') + '\\s*\\{([^}]*)\\}')) || [, ''])[1];
const tokensOf = b => Object.fromEntries([...b.matchAll(/(--[a-z0-9-]+)\s*:\s*([^;]+);/g)].map(m => [m[1], m[2].trim()]));
const LIGHT = tokensOf(block(':root')), DARK = Object.assign({}, LIGHT, tokensOf(block('html.dark')));
for (const t of ['--bg', '--surface', '--side', '--line', '--txt', '--muted', '--faint', '--hover', '--selected', '--you', '--you-ink', '--you-dim', '--in', '--eye', '--tl-r', '--tl-y', '--tl-g', '--accent', '--accent-fill', '--accent-ink', '--focus', '--ok', '--bad', '--warn', '--term-bg', '--term-fg'])
  ok(LIGHT[t] && tokensOf(block('html.dark'))[t], 'token ' + t + ' defined in both schemes');
ok(LIGHT['--bg'] === '#F2F2F7' && LIGHT['--surface'] === '#FFFFFF' && LIGHT['--txt'] === '#1C1C1E' && LIGHT['--you'] === '#0062CC' && LIGHT['--in'] === '#E5E5EA' && LIGHT['--accent-ink'] === '#FFFFFF', 'light (22.6): system grey ground, blue bubble, grey incoming bubble, white ink on the fill');
ok(DARK['--bg'] === '#17171A' && DARK['--surface'] === '#232326' && DARK['--txt'] === '#F5F5F7' && DARK['--you'] === '#0A6AD9' && DARK['--accent'] === '#409CFF' && DARK['--accent-ink'] === '#FFFFFF', 'dark (22.6): near-black ground, blue bubble, the lighter blue for links');
ok(LIGHT['--eye'] === '#FF5F1F' && DARK['--eye'] === '#FF5F1F' && /\.mark-eye \{ fill:var\(--eye\)/.test(css), 'the mark keeps its orange eyes in both schemes');
ok(/\.msg\.in \.body \{[^}]*background:var\(--in\)[^}]*border-radius:18px 18px 18px 5px/.test(css), 'incoming messages are grey bubbles with the tail corner');
ok(/\.msg\.out \.card \{[^}]*background:var\(--you\)[^}]*color:var\(--you-ink\)[^}]*border-radius:18px 18px 5px 18px/.test(css), 'your messages are blue bubbles with white text and the tail corner');
ok(/#termHead::before \{[^}]*var\(--tl-r\)[^}]*var\(--tl-y\)[^}]*var\(--tl-g\)/.test(css) && /#tname \{[^}]*text-align:center/.test(css), 'the terminal bar has the three window dots and a centred title');
ok(/else if \(s !== 'light'\) s = 'dark';/.test(html.split('<style>')[0]), 'no stored scheme opens dark (22.6)');
ok(!/--paper|--ink\b|--panel|--bright|--out\b/.test(css), 'no --paper / --ink / --panel tokens from the old theme');
ok(/^\s*html\.dark\s*\{/m.test(css) && /<html lang="en">/.test(html) && /localStorage\.fcScheme/.test(html.split('<style>')[0]), 'the scheme class is set on <html> before paint from fcScheme');
ok(/prefers-color-scheme: dark/.test(html), 'auto follows prefers-color-scheme');
// Desktop full screen (2026-11-09: noVNC's own button threw 'Fullscreen is not supported' in the WebView)
ok(/<button class="tbtn" id="deskFull"/.test(html), 'the Desktop bar has its own Full screen button');
ok(/body\.deskfull #deskPanel \{[^}]*position:fixed[^}]*inset:0/.test(css), 'full screen is the page covering the app, not the fullscreen API');
ok(/noVNC_fullscreen_button\{display:none/.test(html) && /function hideVncFullscreen\(/.test(html), "noVNC's own fullscreen button is hidden inside the viewer");
ok(/function setDeskFull\(on\)/.test(html) && /function parkDesk\(\) \{[^}]*setDeskFull\(false\)/.test(html), 'parking the Desktop leaves full screen');
// the 22.5 contrast matrix, computed (WCAG 2.x relative luminance)
const hex = s => { const m = /^#([0-9a-f]{6})$/i.exec(String(s).trim()); return m ? [0, 2, 4].map(i => parseInt(m[1].slice(i, i + 2), 16) / 255) : null; };
const lum = c => { const f = v => v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2]); };
const ratio = (a, b) => { const la = lum(hex(a)), lb = lum(hex(b)); return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05); };
for (const [name, T] of [['light', LIGHT], ['dark', DARK]]) {
  const grounds = ['--bg', '--side', '--surface', '--hover', '--selected', '--in'];
  for (const fg of ['--you-ink', '--you-dim']) { const r = ratio(T[fg], T['--you']); ok(r >= 4.5, name + ': ' + fg + ' on --you = ' + r.toFixed(2) + ' (≥ 4.5)'); }
  for (const fg of ['--txt', '--muted', '--accent']) for (const bg of grounds) {
    const r = ratio(T[fg], T[bg]); ok(r >= 4.5, name + ': ' + fg + ' on ' + bg + ' = ' + r.toFixed(2) + ' (≥ 4.5)');
  }
  for (const fg of ['--ok', '--bad', '--warn']) for (const bg of grounds) {
    const r = ratio(T[fg], T[bg]); ok(r >= 3, name + ': ' + fg + ' on ' + bg + ' = ' + r.toFixed(2) + ' (≥ 3)');
  }
  const ink = ratio(T['--accent-ink'], T['--accent-fill']); ok(ink >= 4.5, name + ': --accent-ink on --accent-fill = ' + ink.toFixed(2) + ' (≥ 4.5)');
  const ln = ratio(T['--line'], T['--bg']); ok(ln >= 1.3, name + ': --line on --bg = ' + ln.toFixed(2) + ' (≥ 1.3)');
  const tf = ratio(T['--term-fg'], T['--term-bg']); ok(tf >= 7, name + ': terminal fg on bg = ' + tf.toFixed(2) + ' (≥ 7)');
}
// every var(--x) the page uses is defined (or set at runtime with a fallback)
const RUNTIME_VARS = new Set(['--sheet-h', '--dock-w', '--fx', '--fy']);
const usedVars = new Set([...html.matchAll(/var\((--[a-z0-9-]+)/g)].map(m => m[1]));
const undefinedVars = [...usedVars].filter(v => !(v in LIGHT) && !RUNTIME_VARS.has(v));
ok(undefinedVars.length === 0, 'every var(--x) is defined in :root' + (undefinedVars.length ? ' — missing: ' + undefinedVars.join(', ') : ''));
ok(/#dock \{[^}]*width:var\(--dock-w, 40%\)/.test(css) && /height:var\(--sheet-h,46%\)/.test(css), 'runtime vars carry fallbacks');
// colour literals outside the token blocks (22.1): the shadows and the two xterm palettes are the exceptions
const bodyCss = css.replace(/:root\s*\{[^}]*\}/, '').replace(/html\.dark\s*\{[^}]*\}/, '');
const literals = (bodyCss.match(/#[0-9a-f]{3,8}\b/gi) || []).filter(x => !/^#0000$/i.test(x));
ok(literals.length === 0, 'no colour literal in the CSS outside the token blocks' + (literals.length ? ': ' + literals.join(' ') : ''));
// the font-size floor (21.1 / finding ui-layout-11): nothing under 11 px; 11 px is the activity-bar label alone
const small = [];
for (const r of rules) for (const m of r.body.matchAll(/font(?:-size)?\s*:[^;]*?\b(\d+(?:\.\d+)?)px/g)) { const px = parseFloat(m[1]); if (px < 12 && !(px === 11 && r.sel === '.ab')) small.push(r.sel + ' ' + px + 'px'); }
ok(small.length === 0, 'no text under 12 px except the 11 px activity-bar label' + (small.length ? ': ' + small.join(', ') : ''));
ok(/\.ab \{[^}]*font:500 11px/.test(css), 'the activity-bar label is 11 px');
// the 44 px rule: height on every named tap target, width on the icon-only ones (finding ui-layout-4)
const covered = new Set(), wide = new Set();
for (const r of rules) {
  const mh = /(?:min-height|height)\s*:\s*(\d+)px/.exec(r.body), mw = /(?:min-width|width)\s*:\s*(\d+)px/.exec(r.body);
  if (mh && parseInt(mh[1], 10) >= 44) r.sel.split(',').map(s => s.trim()).forEach(s => covered.add(s));
  if (mw && parseInt(mw[1], 10) >= 44) r.sel.split(',').map(s => s.trim()).forEach(s => wide.add(s));
}
for (const s of ['.tab', '.tab .tx', '.hbtn', '.ibtn', '.mode', '.dbtn', '.tbtn', '.cbtn', '.abtn', '.rrow', '.tn', '.srow', '.cline', '#palette .prow', '#send', '#reach', '#plusBtn', '.spawnrow', '#chipMode', '#runChips select', '#runChips input', '.acts button', '.mi', 'details summary', '.sheetHead button', '#sheetKeys button', '.sheetOpts select', '.sheetOpts input', '.acctForm input', '.acctForm select', '#sideSearch', '#abMode', '#text', '#runPill', '#toast button', '#termBar', '#deskBar',
                 '.wk', '.sw', '.xn', '.start', '.codeRow input', '.mkcard input', '.mkcard select', '.wfTask textarea', '#chipSuggest'])
  ok(covered.has(s), '44 px tap target (height): ' + s);
ok(/\.sbtn \{[^}]*min-height:28px;[^}]*min-width:44px/.test(css), 'the status-line mode pill fills its 28 px row and is 44 px wide (the row height is the contract\'s)');
for (const s of ['.hbtn', '.ibtn', '.dbtn', '.tbtn', '.cbtn', '.abtn', '.sbtn', '.sheetHead button', '.acts button', '#send', '#plusBtn', '.tab .tx', '#abMode', '#sheetKeys button', '#toast button', '.wk', '.sw'])
  ok(wide.has(s), '44 px tap target (width): ' + s);
ok(/\.ab \{[^}]*width:64px;[^}]*min-height:56px/.test(css), 'activity items are 64 px wide and at least 56 px tall');
ok(/\.ab span \{[^}]*-webkit-line-clamp:2/.test(css) && !/\.ab span \{[^}]*nowrap/.test(css), 'activity labels wrap onto two lines instead of cutting a word');
ok(/\.acctForm input \{[^}]*min-width:14em/.test(css), 'the new-account name keeps 14 em so the typed name is not clipped');
ok(/<datalist id="modelList">/.test(html), 'the model chip has a datalist to hang the engine models on');
ok(/'grok-4\.7-low', 'grok-4\.7-medium', 'grok-4\.7-high', 'grok-4\.7-xhigh'/.test(html), 'the cursor tiers are the model chip suggestions when the engine lists none');
ok(/<span id="chipAccountLabel">login<\/span>/.test(html), 'the account chip label is addressable for the cursor rename');
ok(/\.sheet \.grip \{[^}]*min-height:24px/.test(css), 'the sheet grip is a 24 px handle');
// the 22.5 frame
ok(/#abar \{[^}]*width:64px/.test(css) && /#side \{[^}]*width:280px/.test(css) && /#side\.collapsed \{[^}]*width:0/.test(css), 'activity bar 64 px, side bar 280 px, collapsible');
ok(/#page > \* \{[^}]*max-width:760px/.test(css) && /#log > \* \{[^}]*max-width:760px/.test(css), 'the canvas is a 760 px column (pages and the lane)');
ok(/const MAX_TABS = 8/.test(js) && /localStorage\.fcTabs = JSON\.stringify\(TABS\.slice\(0, MAX_TABS\)\)/.test(js), 'at most 8 tabs, persisted in fcTabs');
ok(/\.tab\.on::after \{[^}]*background:var\(--accent\)/.test(css) && !/\.tab \{[^}]*text-transform/.test(css), 'quiet tabs: an accent underline, no caps');
ok(/function dockOpen\(\)/.test(js) && /DOCK\.state === 'open' \|\| DOCK\.state === 'expanded'/.test(js) && !/'min'/.test(js), 'the right panel is open | expanded | closed (no minimised state)');
ok(/function renderDock\(mod\) \{[^\n]*\n\s*if \(!mod \|\| !dockOpen\(\)/.test(js), 'renderDock() is a no-op for a closed panel (finding ui-state-7)');
ok(!/function setDockPos\(/.test(js) && /function setTermPos\(/.test(js) && /function applyTermPos\(\)[\s\S]*?Math\.min\(Math\.max\(0, DOCK\.term\.fx/.test(js), 'the terminal keeps its positions with clamped floats; the panel does not float (finding ui-layout-9)');
ok(/#termPanel\.float \{[^}]*max-width:calc\(100vw - 16px\)/.test(css) && /max-height:calc\(100dvh - 44px\)/.test(css), 'a floating terminal is clamped in CSS too');
ok(/#statusBar \{[^}]*height:28px/.test(css) && /id="sbStreams"/.test(html) && /id="sbVnc"/.test(html) && /id="sbBuild"/.test(html), 'one 28 px status line: streams · runs · needs you · :99 · build · mode');
ok(/#termBar \{[^}]*min-height:44px/.test(css) && /body\.user[^{]*#termBar/.test(css), 'the terminal bar is a 44 px tap target, hidden in user mode');
ok(/#lane \{[^}]*min-width:360px/.test(css), 'the lane never drops under 360 px (finding ui-layout-5)');
ok(/#wsName \{[^}]*min-width:120px/.test(css) && /\.tn \.tnm \{[^}]*-webkit-line-clamp:2/.test(css) && /#palette \.pk \{[^}]*text-overflow:ellipsis/.test(css), 'long names: header name reserved 120 px, tree names clamp to two lines, palette keys ellipsise (finding ui-layout-6)');
ok(/#palette \{[^}]*position:absolute/.test(css) && /bottom:calc\(100% \+ 8px\)/.test(css) && /#log \{[^}]*min-height:96px/.test(css), 'the palette sits above the composer out of flow (finding ui-layout-8)');
// portrait (21.2): the side bar is a left sheet with a scrim, the panel a bottom sheet, the terminal a bottom sheet
ok(/@media \(max-width:900px\) \{[\s\S]*?#side \{[^}]*position:fixed[\s\S]*?body\.sideOpen #side \{[^}]*translateX\(0\)/.test(css), 'portrait: the side bar is a left sheet opened by body.sideOpen');
ok(/@media \(max-width:900px\) \{[\s\S]*?#dock \{[^}]*position:fixed[^}]*bottom:0[^}]*height:62%/.test(css), 'portrait: the right panel becomes a bottom sheet (62%)');
ok(/function closeSideSheet\(/.test(js) && /function switchDesk\(name\)[\s\S]*?closeSideSheet\(\)/.test(js), 'a desk pick closes the portrait side sheet (finding ui-layout-1)');
ok(/@media \(prefers-reduced-motion: reduce\)/.test(css) && /transition:[^;]*\.15s/.test(css), '150 ms transitions, off under reduced motion');
ok(/\.msg \{[^}]*animation:rise/.test(css) && /@keyframes rise/.test(css), 'messages rise in');
// user mode hides the terminal panel, its header buttons, the splitter and the bar in CSS too
ok(/body\.user #termPanel[^{]*\{[^}]*display:none/.test(css), 'body.user hides #termPanel');
ok(/body\.user[^{]*#termBtn[^{]*\{[^}]*display:none/.test(css), 'body.user hides #termBtn');
ok(/body\.user[^{]*#split[^{]*\{[^}]*display:none/.test(css), 'body.user hides #split');
ok(/body\.user[^{]*\.dev-only[^{]*\{[^}]*display:none/.test(css), 'body.user hides .dev-only');
ok(/function fail\(msg\)[\s\S]*?if \(document\.body\.classList\.contains\('user'\)\) banner\(msg\);/.test(js), 'fail() goes to the lane banner in user mode, never un-hides the terminal (finding ui-network-6)');
// the mark, the eyes, the empty canvas
ok(/<header>[\s\S]*?<svg class="mark" id="hmark"[\s\S]*?mark-eye[\s\S]*?<\/svg>[\s\S]*?<b id="wsName">/.test(html), 'the octagon mark sits in the header before the desk name');
ok(/\.mark-rim \{ fill:var\(--mark-rim\)/.test(css) && /\.mark-plate \{ fill:var\(--mark-plate\)/.test(css) && /\.mark-eye \{ fill:var\(--eye\)/.test(css), 'mark classes are coloured by tokens');
ok(/--mark-plate:#050404/.test(css) && /--mark-rim:#302f2f/.test(css) && /--mark-plate:#141210/.test(css) && /--mark-rim:#3c3b39/.test(css), 'mark tokens from vendor/brand/tokens.json (light and dark)');
ok(/\.working \.mark-eye \{ animation:eyeblink 1s/.test(css) && /@keyframes eyeblink/.test(css), 'eyes blink once per second on a working desk');
ok(js.includes("'What should we get started with?'") && !js.includes("'It does the work. You keep the record.'"), 'the empty lane opens with a question, not the website slogan');
ok(/const STARTS = \[/.test(js) && ['Sign in an account', 'Desks side by side', 'Run with a receipt', 'Make a workflow', 'Extend'].every(t => js.includes("['" + t + "', ")), 'the empty canvas offers the five starts of 25.4 U7 (they replace the three pills)');
ok(!/class: 'pill', text: 'Check a receipt'/.test(js), 'the three empty-canvas pills are gone');
// 23: the app is MetaBot where shown; machine keys unchanged
ok(/<title>MetaBot<\/title>/.test(html) && /<b id="wsName">MetaBot<\/b>/.test(html) && /app: 'MetaBot'/.test(js), 'the page is titled MetaBot');
const shownMetaChat = (js.replace(/X-MetaChat|metachat-vault|metachat-gui/g, '').match(/['"][^'"\n]*MetaChat[^'"\n]*['"]/g) || []);
ok(shownMetaChat.length === 0, 'no shown string says MetaChat (machine keys X-MetaChat / metachat-vault / metachat-gui stay)' + (shownMetaChat.length ? ': ' + shownMetaChat.join(' | ') : ''));
ok(/'MetaBot · on Metaorcha'/.test(js), 'About says MetaBot · on Metaorcha');
ok(/'MetaBot updated — reload'/.test(js) && /label: 'Reload', sticky: true/.test(js) && /function noteBuild\(b\)/.test(js) && /es\.addEventListener\('hello'/.test(js), 'the update hint: build compared on boot and reopen, one sticky toast with Reload, never an automatic reload');
ok(/function streamWatchdog\(\)/.test(js) && /es\.readyState === 2/.test(js) && /Math\.min\(30000, 1000 \* Math\.pow\(2, ES_RETRY\)\)/.test(js) && /setInterval\(streamWatchdog, 10000\)/.test(js), 'the stream watchdog reopens a closed EventSource with backoff capped at 30 s (finding ui-network-4)');
ok(/es\.onmessage = e => \{[\s\S]*?try \{ ev = JSON\.parse\(e\.data\); \} catch/.test(js) && /typeof ev\.message !== 'string'/.test(js), 'stream payloads are parsed defensively (finding ui-network-5)');
ok(/async function recoverLoaders\(\)/.test(js) && /'PC unreachable — retrying'/.test(js), 'loaders recover after an outage and the status line says PC unreachable (finding ui-network-3)');
ok(/'This tablet is signed out of the PC \(bad token\) — relaunch the MetaBot app\.'/.test(js), 'a rotated token shows the relaunch banner');
// 21.3: the New desk sheet, /all, the Workflows page, the Desktop header
ok((html.match(/class="spawnrow(?: on)?" data-kind="(claude|cursor|shell|run)"/g) || []).length === 4, 'the New desk sheet has four kinds: claude, cursor, shell, run');
ok(html.includes("'User view: no terminals or developer tools'") && !html.includes('User view: chats, runs, receipts, accounts'), 'the user-view toast names what it hides (it listed four modules while six showed)');
ok(/#f\.run #runPill \{[^}]*order:-1[^}]*flex:1 1 100%/.test(html) && /#f \{[^}]*flex-wrap:wrap/.test(html), 'Run mode: the settings pill wraps onto its own line above the task box (the box was 180 px wide on the tablet)');
ok(/\.krow \.v\.short \{[^}]*white-space:nowrap/.test(html), 'a short check value keeps on one line (it split as pa/ss on the tablet)');
ok(!/arms itself/.test(html), 'the New desk sheet never claims a Claude desk arms itself: nothing arms it, chat is typed into its prompt');
ok(/id="spawnPath"[^>]*enterkeyhint="go"/.test(html) && /id="spawnLabel"[^>]*enterkeyhint="go"/.test(html), 'the New desk path and label fields show Go on the keyboard (the Open desk button sits under it)');
ok(/id="spawnErr"/.test(html) && /err\.textContent = 'label: a-z, 0-9 and -, up to 40 characters'/.test(js) && /err\.textContent = 'path: absolute, no \.\.'/.test(js), 'spawn errors are inline');
ok(/j\.status === 409 && \/taken\|exists\/i\.test\(j\.error \|\| ''\) && o\.fallback !== false/.test(js) && /signInDesk\([\s\S]*?\{fallback: false[,}]/.test(js), 'the spawn fallback runs for a name clash alone, never for a sign-in desk (finding ui-state-5)');
ok(/function sendAll\(c\)/.test(js) && /ALL_CONFIRM/.test(js) && /'send to ' \+ ALL_CONFIRM\.targets\.length/.test(js), '/all stops at a confirm row naming the count, then posts per desk');
ok(/function newWorkflowForm\(/.test(js) && /CATALOG\['workflow\.new'\]/.test(js) && /'metabot workflow new '/.test(js), 'Workflows → New uses workflow.new when present, else stages metabot workflow new in a shell desk');
for (const s of ["'display down'", "'bridge refused'", "'noVNC did not load — reload the page or reinstall the app'", "deskStatusSet(':99 · ' + (wh ? wh + ' · ' : '') + 'connected', 'ok')", 'id="deskReload"']) ok(html.includes(s), 'Desktop header word: ' + s);
ok(/>Retry<\/button>/.test(html), 'the Desktop header has a Retry button');
ok(/enable --now metachat-gui'/.test(js) && !/enable --now fc-gui/.test(js), 'the install line names metachat-gui');
ok(/unit === 'fc-gui'/.test(js), 'a display.unit still named fc-gui gets the migration hint');
// keyboard (21.2)
ok(/e\.key === 'Escape'\) \{\n\s*if \(!pal\.classList\.contains\('hidden'\)\) \{ hidePal\(\); return; \}/.test(js), 'Esc closes the top-most layer in order');
ok(/String\(e\.key\)\.toLowerCase\(\) === 'k'/.test(js) && /e\.key === '`'/.test(js) && /e\.ctrlKey && e\.key === 'Tab'/.test(js), 'Ctrl+K, Ctrl+` and Ctrl+Tab are handled');
// modules: modes default table and a generic icon
ok(/const MODE_DEFAULTS = \{/.test(js) && /sdk: \['dev'\]/.test(js) && /function userHiddenId\(id\)/.test(js), 'manifests without modes fall back to the default table');
ok(/return ICONS\[id\] \? id : 'module'/.test(js), 'a manifest without a usable icon gets the generic one');
// receipts: the honest states, the third state, the same bytes to the PC
for (const s of ['tablet check unavailable', 'could not read the input', 'invalid: schema, chain or signature', 'PC check unavailable', 'Retry PC check', 'No check ran']) ok(js.includes(s), 'receipt copy: ' + s);
ok(/function headline\(el, pc, t\)/.test(js) && /if \(!pc\.checked && !t\.checked\)/.test(js), 'the headline has three states per side (finding ui-network-2)');
ok(/raw_b64: enc/.test(js) && /function b64\(bytes\)/.test(js), 'the SDK check posts raw_b64 of the same bytes (finding ui-network-8)');
ok(/await Promise\.race\(\[VERIFIER_READY, new Promise\(res => setTimeout\(res, 8000\)\)\]\)/.test(js), 'the tablet check waits for the verifier module (finding ui-network-13)');
for (const s of ['One entry per tool call: hashes of its arguments and output. The raw values never enter it.',
                 'A SHA-256 hash chain over the steps, in order.', 'A Merkle root over the same steps.',
                 'Ed25519 over a digest of every other field. The public key travels with it.',
                 'The verifier is open and runs offline. Checking a receipt asks nothing of us.',
                 'A write waits for approval unless you allowed it in advance.']) ok(js.includes(s), 'allowed copy line present: ' + s.slice(0, 40) + '…');
// 21.6 clutter rules a static read can see
ok(!/text-transform\s*:\s*uppercase/.test(css), 'no ALL-CAPS heads (text-transform)');
ok(/toastT = setTimeout\(\(\) => el\.classList\.remove\('show'\), ms \|\| 2400\)/.test(js), 'toasts last 2.4 s by default');
ok(/function showActs\(d, ev, body\)/.test(js) && !/function bubble\(ev\)[\s\S]*?class: 'acts'[\s\S]*?return d;/.test(js), 'quick actions render on tap, not under every bubble');
ok(/function appendBubble\(ev\)[\s\S]*?t - LAST_SHOWN_T >= 300/.test(js), 'one time divider per 5-minute group');
ok(!/[▶⧉↻✓✕⤢⤡⇄☰✎⚙▣]/.test(js.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/[^\n]*/g, '').replace(/text: s\.success \? '✓' : '✗'/, '').replace(/'[✓✗—] '/g, '').replace(/' · ✓ '/, '')), 'no decorative glyphs in labels (the step table ✓/✗, the check-state glyph and the delivered tick are data)');
ok(/function deskState\(d\)/.test(js) && /return 'needs you'/.test(js) && /return 'working'/.test(js) && /return 'not armed'/.test(js) && /'typed '/.test(js), "desk rows carry one state word: needs you · working · typed N min ago · not armed");
ok(/\.msg\.out \.card \{[^}]*background:var\(--you\)/.test(css) && /\.msg\.in \.body \{[^}]*padding/.test(css) && !/\.msg\.in \{[^}]*background:var\(--surface\)/.test(css), 'your messages are a blue bubble, desk replies a grey one (22.6)');
// banned pitch words (the public-copy rule), on the markup and on every shown string
const banned = html.match(/\b(nobody|trustless|the first|the only|first-ever|only one)\b/gi) || [];
ok(banned.length === 0, 'no banned pitch words (first/only/nobody/trustless)' + (banned.length ? ': ' + banned.join(', ') : ''));
const shown = [...js.matchAll(/'((?:[^'\\\n]|\\.)*)'/g)].map(m => m[1]).concat([...html.split('<script>')[0].matchAll(/>([^<>{}]+)</g)].map(m => m[1]));
const badShown = shown.filter(s => /\b(first|only|nobody|trustless)\b/i.test(s.replace(/read-only|dev-only/gi, '')));
ok(badShown.length === 0, 'no shown string uses first / only / nobody / trustless' + (badShown.length ? ': ' + badShown.slice(0, 6).join(' | ') : ''));
ok(!/\b(he|his|him)\b/.test(html), 'they/them: no he / his / him in the page');
ok(/function renderSettings\([\s\S]*?verOf\(/.test(js) && !/sbVer/.test(js), 'versions are rendered in renderSettings and nowhere else');
ok(/let UMODE = /.test(js) && /localStorage\.fcMode/.test(js) && /\|\| 'user'/.test(js), 'fcMode user|dev, default user on a fresh device');
ok(/function setUMode\(/.test(js) && /classList\.toggle\('user'/.test(js), 'setUMode() toggles body.user');
ok(/function renderSetup\(body\)/.test(js) && /localStorage\.fcSetup/.test(js) && /openPage\('setup'\)/.test(js), 'the set-up steps are a page, opened on a fresh device, dismissable (fcSetup)');
ok(/'equals '/.test(js), 'user mode shows the command as a quiet "equals …" line');
ok(/function dragDock\(/.test(js) && /setPointerCapture/.test(js), 'terminal header drag handler with pointer capture');
ok(/pending/.test(js) && /cid/.test(js), 'optimistic send with a client id');
ok(/class: 'rrow skel'/.test(js) && /class: 'skel'/.test(js) && /@keyframes shimmer/.test(css), 'skeleton rows with a shimmer');
ok(/navigator\.vibrate\(10\)/.test(js) && /navigator\.vibrate\(40\)/.test(js), 'haptics on send (10) and on needs-you (40)');
ok(/\.tn\.working \.dot \{ animation:shimmer/.test(css), 'working shimmer on the tree dot');
ok(/function copyText\(s\)[\s\S]*?prevFocus\.focus\(\)/.test(js) && /el\.addEventListener\('mouseup'/.test(js) && !/onSelectionChange\(/.test(js), 'terminal copy happens on mouseup and gives the focus back (finding ui-state-12)');
ok(/const RAIL_LP = \{t: null, key: ''\}/.test(js) && /body\.addEventListener\('pointerdown'/.test(js), 'the tree long-press timer lives outside the rows, with delegated listeners (finding ui-state-6)');
ok(/pullToRefresh\(page, /.test(js), 'pull-to-refresh is measured on the page, the real scroller (finding ui-state-9)');

// ---- 10. section 25.4: U1–U7 as far as a static read can prove them ------------------------
// a function's source, by brace matching from its declaration (enough for the page's own functions)
const fnBody = name => {
  const m = new RegExp('(?:async )?function ' + name + '\\(').exec(js); if (!m) return '';
  let i = js.indexOf('{', js.indexOf(')', m.index)), depth = 0, j = i;
  for (; j < js.length; j++) { if (js[j] === '{') depth++; else if (js[j] === '}') { depth--; if (depth === 0) break; } }
  return js.slice(m.index, j + 1);
};
// U1: the guided sign-in card; the page never types into a desk on a timer
const sIn = fnBody('signInDesk');
ok(sIn && /signin: true/.test(sIn) && /fallback: false/.test(sIn), 'U1: Sign in spawns {kind, account, signin:true} and never falls back to another name');
ok(sIn && !/setTimeout|\/login|termWriteTo|whenConnected/.test(sIn), 'U1: signInDesk() types nothing into the desk and starts no timer (the 6 s /login write is gone)');
ok(!/termWriteTo\(name, '\/login\\n'\)/.test(js), 'U1: no /login write bound to a timer anywhere');
ok(/function renderSignin\(body\)/.test(js) && /async function signinRefresh\(\)/.test(js), 'U1: the card renders from the desk row and refreshes from /api/desks');
for (const s of ['Enter keeps Dark', 'Use your Claude subscription', 'Open the sign-in page, approve, paste the code it shows',
                 'The sign-in folder is empty', 'Open sign-in page', 'Copy link', 'Send code', 'Open a desk on it', 'Close the sign-in desk', 'signed in'])
  ok(js.includes(s), 'U1 copy: ' + s);
ok(/const SIGNIN_HOSTS = \['claude\.com', 'claude\.ai', 'platform\.claude\.com', 'console\.anthropic\.com', 'cursor\.com'\]/.test(js), 'U1: the page shows a link for the five hosts the server keeps, nothing else');
ok(/h\('a', \{class: 'abtn primary', href: url, target: '_blank', rel: 'noopener noreferrer'/.test(js), 'U1: the sign-in link is <a href target=_blank rel=noopener noreferrer> (the APK opens it in the browser)');
ok(/const body = \{name, keys\};/.test(js) && /body\.ask_line = /.test(js) && /api\('\/api\/keys', body\)/.test(js) && /api\('\/api\/keys', \{name, text: code\}\)/.test(js), 'U1/U6: keys go as {name, keys:[…]} (with the ask_line they answer, review 1.2) and the code as {name, text}');
// U2: the account picker per engine; a folder for Claude and Cursor; home for Shell alone
const accOpts = fnBody('spawnAccountOptions'), dirOpts = fnBody('spawnFolderOptions');
ok(/kind === 'cursor'/.test(accOpts) && /defaults/.test(accOpts), 'U2: the account picker lists the default and the named accounts of the kind (Claude and Cursor)');
ok(/kind === 'shell' \? \[\['', 'home'\]\]/.test(dirOpts) && /META\.default_folder/.test(dirOpts), 'U2: home is offered for Shell alone; meta.default_folder joins the folders');
ok(/function activeFolder\(\)/.test(js) && /'choose a project folder'/.test(js), 'U2: the folder is preselected from the active project and required for Claude and Cursor');
// U3: the Extend page
ok(/plugins: \['user', 'dev'\]/.test(js), 'U3: Extend (module plugins) shows in user and developer modes');
ok(/function renderExtend\(body, mod\)/.test(js) && /getJ\('\/api\/extend' \+ \(fresh \? '\?fresh=1' : ''\)\)/.test(js), 'U3: the Extend page reads GET /api/extend (?fresh=1 on Refresh)');
ok(/'a bridge runs outside the gate: it is a program you wrote'/.test(js), 'U3: the bridge line, verbatim');
for (const s of ["'metabot mail'", "'metabot hook pre-tool-use'", "'metabot mcp serve'"]) ok(js.includes(s), 'U3: between harnesses line ' + s);
ok(/const pinnedOf = [^\n]*\.slice\(0, 12\)/.test(js), 'U3: an installed plugin shows its pinned hash, the leading 12 hex');
for (const id of ['plugin.new', 'plugin.new-bridge', 'plugin.import-skills', 'plugin.marketplace.add', 'plugin.marketplace.remove', 'plugin.install',
                  'plugin.enable', 'plugin.disable', 'plugin.remove', 'plugin.enable-bridges', 'plugin.enable-servers', 'plugin.enable-hooks', 'doctor.models'])
  ok(js.includes("'" + id + "'"), 'U3: Extend calls catalogue id ' + id);
ok(/'ext-' \+ key/.test(js) && ['make', 'installed', 'markets', 'engines', 'app', 'between'].every(k => js.includes("['" + k + "', ")), 'U3: six sections: Make · Installed · Marketplaces · Engines · This app · Between harnesses');
ok(/'between', 'Between harnesses', 'dev'/.test(js), 'U3: Between harnesses is developer-mode');
// U4: workflows from a task
for (const id of ['propose', 'workflow.new', 'workflow.new-ladder', 'workflow.new-office', 'workflow.approve', 'workflow.run', 'workflow.list', 'serve'])
  ok(js.includes("'" + id + "'"), 'U4: workflows from a task use ' + id);
// review 1.2 (RV-D): MetaBot schedules only a workflow with a schedule and a ceiling; no Keep entry stores either, so Schedule stays off and says why
ok(!js.includes("'workflow.schedule', {ref") && js.includes('Schedule is off: MetaBot schedules a workflow that holds a schedule and a ceiling'), 'RV-D: the From a task card never opens workflow.schedule for a kept workflow, and says why');
ok(/function proposeDraft\(/.test(js) && /JSON\.parse\(r\.stdout\)/.test(fnBody('proposeDraft')), 'U4: Propose parses the JSON proposal defensively');
ok(/'Start serve'/.test(js) && /serve\.running/.test(js), 'U4: the routines block says whether serve runs and offers Start serve');
// U5: the engine chip from /api/extend, local marked, Suggest
ok(/loadExtend\(/.test(fnBody('loadEngines')) && /engines\.list/.test(fnBody('loadEngines')), 'U5: the engine chip lists /api/extend engines, engines.list stays the fallback');
ok(/isLocal\(engine\)/.test(fnBody('renderRunPill')) && /' · local · works offline'/.test(js), 'U5: local engines are marked, spend reads $0 (local) for them');
ok(/id: 'suggest'/.test(fnBody('suggestEngine')), 'U5: Suggest reads the suggest entry and fills the engine');
// U6: one-tap keys through /api/keys; a typed yes and a secret go to the terminal
const ak = fnBody('askKind');
ok(/cls === 'destructive'\) return 'yes'/.test(ak), "U6: a destructive ask takes no one-tap key (21.7 item 2)");
ok(/line: \[\['y', \['y', 'enter'\]\], \['n', \['n', 'enter'\]\]\]/.test(js) && /select: \[\['Enter', \['enter'\]\], \['Esc', \['esc'\]\]\]/.test(js), 'U6: a line prompt gets y/n + Enter, a select menu gets Enter or Esc');
ok(!/\['yes'/.test(js), 'U6: no key list ever sends the word yes');
ok(/sudo/.test(fnBody('termAsk')) && /'Answer in the terminal'/.test(js), 'U6: sudo and secrets open the terminal instead');

// ---- 9. runtime exercise under a fake DOM (tests/ui-smoke-dom.mjs, own process) ----
console.log('\n--- runtime: tests/ui-smoke-dom.mjs ---');
const dom = spawnSync(process.execPath, [join(here, 'ui-smoke-dom.mjs'), root], { encoding: 'utf8' });
process.stdout.write(dom.stdout || '');
if (dom.stderr) process.stdout.write(dom.stderr);
if (dom.status === 2) { console.log('SKIP runtime exercise skipped (see the line above)'); skipped++; }
else ok(dom.status === 0, 'runtime exercise passes (exit ' + dom.status + ')');
console.log('\n--- runtime: tests/ui-smoke-dom.mjs (fcMode=dev persisted) ---');
const dev = spawnSync(process.execPath, [join(here, 'ui-smoke-dom.mjs'), root], { encoding: 'utf8', env: Object.assign({}, process.env, { MC_UI_BOOT: 'dev' }) });
process.stdout.write(dev.stdout || '');
if (dev.stderr) process.stdout.write(dev.stderr);
if (dev.status === 2) { console.log('SKIP developer boot exercise skipped (see the line above)'); skipped++; }
else ok(dev.status === 0, 'developer boot exercise passes (exit ' + dev.status + ')');

console.log('\nstatic+runtime: ' + passes + ' passed, ' + failures + ' failed, ' + skipped + ' skipped');
process.exit(failures ? 1 : 0);
