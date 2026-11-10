// Runtime exercise of index.html's script under a small fake DOM (there is no browser here).
// Everything the script touches is stubbed; fetch answers with the contract's JSON shapes
// (docs/PHASE1-CONTRACT.md section 4) and records every POST's headers and body.
// GET /api/tree answers 404 on purpose: the page must build the tree itself (22.3).
// Run alone: node tests/ui-smoke-dom.mjs
// tests/ui-smoke.mjs runs it as a child process after its static checks.
// Desk, project and account names here are placeholders (desk-a, proj-one, work, alt).
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

import { existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
const ROOT = process.argv[2] || join(dirname(fileURLToPath(import.meta.url)), '..', 'src', 'metabot_app');
const html = readFileSync(join(ROOT, 'index.html'), 'utf8');
const js = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]).pop();
// the receipt the viewer is fed: the shared golden fixture (contract section 0), else the sdk's own vector
const GOLDEN_PATHS = [join(dirname(fileURLToPath(import.meta.url)), 'fixtures', 'golden.json'),
  new URL('../../../docs/spec/test-vectors/run-attestation-golden.json', import.meta.url).pathname];
const goldenPath = GOLDEN_PATHS.find(p => existsSync(p));
if (!goldenPath) { console.log('SKIP no golden receipt found at ' + GOLDEN_PATHS.join(' or ') + ' — the runtime exercise cannot run'); process.exit(2); }
console.log('golden receipt: ' + goldenPath);
const golden = JSON.parse(readFileSync(goldenPath, 'utf8'));
const GOLDEN_TEXT = JSON.stringify(golden.valid, null, 2) + '\n';

// ---- fake DOM -----------------------------------------------------------------
const byId = new Map(), all = [];
class ClassList {
  constructor(el) { this.el = el; }
  get set() { return new Set((this.el.className || '').split(/\s+/).filter(Boolean)); }
  write(s) { this.el.className = [...s].join(' '); }
  add(...c) { const s = this.set; c.forEach(x => s.add(x)); this.write(s); }
  remove(...c) { const s = this.set; c.forEach(x => s.delete(x)); this.write(s); }
  contains(c) { return this.set.has(c); }
  toggle(c, force) { const s = this.set; const on = force === undefined ? !s.has(c) : !!force; if (on) s.add(c); else s.delete(c); this.write(s); return on; }
}
const camel = k => k.replace(/-([a-z])/g, (_, c) => c.toUpperCase());
class El {
  constructor(tag) {
    this.tagName = tag.toUpperCase(); this.nodeType = 1; this.children = []; this.parent = null;
    this.className = ''; this.attrs = {}; this.style = { cssText: '', setProperty(k, v) { this[k] = v; } }; this.dataset = {};
    this.listeners = {}; this._text = ''; this._value = undefined; this.scrollTop = 0; this.scrollHeight = 0;
    this.classList = new ClassList(this);
    all.push(this);
  }
  get id() { return this.attrs.id || ''; }
  set id(v) { this.setAttribute('id', v); }
  get parentNode() { return this.parent; }
  get firstChild() { return this.children[0] || null; }
  setAttribute(k, v) { this.attrs[k] = String(v); if (k === 'id') byId.set(String(v), this); if (k === 'class') this.className = String(v); if (k.startsWith('data-')) this.dataset[camel(k.slice(5))] = String(v); }
  getAttribute(k) { return this.attrs[k] == null ? null : this.attrs[k]; }
  get textContent() { return this.children.length ? this.children.map(c => c.nodeType === 3 ? c.data : c.textContent).join('') : this._text; }
  set textContent(v) { this.children = []; this._text = String(v); }
  set innerHTML(v) { this.children = []; this._text = ''; this._html = String(v); }
  get innerHTML() { return this._html || ''; }
  appendChild(c) { if (c == null) throw new Error('appendChild(null)'); if (typeof c !== 'object') throw new Error('appendChild(' + typeof c + ')'); this._text = ''; if (c.parent) c.parent.children = c.parent.children.filter(x => x !== c); c.parent = this; this.children.push(c); return c; }
  append(...ks) { ks.forEach(k => { if (k == null) throw new Error('append(null) on #' + this.id + ' <' + this.tagName + '>'); if (Array.isArray(k)) throw new Error('append(array) on #' + this.id); this.appendChild(typeof k === 'object' ? k : txt(String(k))); }); }
  prepend(c) { if (c.parent) c.parent.children = c.parent.children.filter(x => x !== c); c.parent = this; this.children.unshift(c); return c; }
  insertBefore(c, ref) { if (c.parent) c.parent.children = c.parent.children.filter(x => x !== c); c.parent = this; const i = ref ? this.children.indexOf(ref) : -1; if (i < 0) this.children.push(c); else this.children.splice(i, 0, c); return c; }
  replaceWith(c) { if (!this.parent) return; const i = this.parent.children.indexOf(this); this.parent.children[i] = c; c.parent = this.parent; this.parent = null; }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(c => c !== this); this.parent = null; }
  addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); }
  removeEventListener() {}
  fire(t, ev) { (this.listeners[t] || []).forEach(f => f(ev || {})); if (this['on' + t]) this['on' + t](ev || {}); }
  getBoundingClientRect() { return { top: 0, left: 0, width: 500, height: 300 }; }
  focus() { document.activeElement = this; } select() {} setSelectionRange() {} scrollIntoView() {}
  click() { const ev = { stopPropagation() {}, preventDefault() {}, target: this, currentTarget: this }; (this.listeners.click || []).forEach(f => f(ev)); if (this.onclick) this.onclick(ev); }
  querySelectorAll(sel) { return qsa(sel, this); }
  querySelector(sel) { return qsa(sel, this)[0] || null; }
  get options() { return this.children.filter(c => c.tagName === 'OPTION'); }
  get value() { if (this._value !== undefined) return this._value; if (this.tagName === 'OPTION') return this.attrs.value || ''; if (this.tagName === 'SELECT') { const o = this.options[0]; return o ? o.value : ''; } return ''; }
  set value(v) { this._value = String(v); }
}
const txt = s => ({ nodeType: 3, data: s, textContent: s });
function desc(el, out) { for (const c of el.children) { if (c.nodeType === 1) { out.push(c); desc(c, out); } } return out; }
function qsa(sel, root) {   // the last token of a selector: .class, #id or a tag name
  const last = sel.trim().split(/\s+/).pop(); const pool = root ? desc(root, []) : all;
  if (last.startsWith('.')) { const cls = last.slice(1); return pool.filter(e => e.classList.contains(cls)); }
  if (last.startsWith('#')) { return pool.filter(e => e.id === last.slice(1)); }
  return pool.filter(e => e.tagName === last.toUpperCase());
}
// static elements: every id="..." in the markup, plus the class-only ones the script queries
for (const m of html.matchAll(/<(\w+)([^>]*)\sid="([\w-]+)"([^>]*)>/g)) {
  const el = new El(m[1]); el.setAttribute('id', m[3]);
  const cm = /class="([^"]*)"/.exec(m[2] + m[4]); if (cm) el.setAttribute('class', cm[1]);
}
const spawnRows = [];
for (const m of html.matchAll(/<(\w+)[^>]*class="([^"]*)"[^>]*data-kind="(\w+)"/g)) { const el = new El(m[1]); el.setAttribute('class', m[2]); el.dataset.kind = m[3]; spawnRows.push(el); }
const SHEET_COUNT = (html.match(/class="sheet hidden"/g) || []).length;
for (let i = 0; i < SHEET_COUNT; i++) { new El('button').setAttribute('class', 'sheetX'); new El('div').setAttribute('class', 'grip'); }
const head = new El('head'), body = new El('body'), docEl = new El('html');
// the static tree the script moves things around in (22.2): main > [abar, side, chat, split, termPanel]
const g = id => byId.get(id);
const main = g('main'), chat = g('chat'), centre = g('centre');
g('app').append(main, g('statusBar'));
main.append(g('abar'), g('side'), chat, g('split'), g('termPanel'));
g('side').append(g('sideHead'), g('sideTools'), g('sideBody'));
g('sideHead').append(g('sideTitle'), g('addBtn'), g('sideRefresh'));
g('sideTools').append(g('sideSearch'));
chat.append(g('tabs'), centre, g('termBar'));
g('tabs').append(g('tabFill'));
centre.append(g('lane'), g('page'), g('dock'));
g('lane').append(g('banner'), g('log'), g('composer'));
g('composer').append(g('cwrap')); g('cwrap').append(g('palette'), g('plusMenu'), g('f'));
g('f').append(g('modeRow'), g('plusBtn'), g('text'), g('runPill'), g('send'));
g('dock').append(g('dockHead'), g('dockBody'));
g('termPanel').append(g('termHead'), g('termErr'), g('term'));
g('statusBar').append(g('sbStreams'), g('sbRuns'), g('sbNeed'), g('sbVnc'), g('sbBuild'), g('sbMode'));
body.append(g('app'), g('deskPanel'), g('deskSheet'), g('scrim'), g('spawnSheet'), g('runSheet'), g('cliSheet'), g('copySheet'), g('toast'));
g('deskPanel').append(g('deskBar'), g('deskFrame'));
g('deskBar').append(g('deskStatus'), g('deskReload'), g('deskClose'));
g('deskSheet').append(g('sheetTitle'), g('sheetCls'), g('sheetEquals'), g('sheetTerm'), g('sheetKeys'));
const lab = el => { const l = new El('label'); l.append(el); return l; };   // the markup wraps each sheet control in a <label>
g('spawnSheet').append(g('spawnKinds'), lab(g('spawnAccount')), lab(g('spawnCwd')), lab(g('spawnPath')), lab(g('spawnLabel')), g('spawnErr'), g('spawnGo'));
spawnRows.forEach(r => g('spawnKinds').appendChild(r));
g('runSheet').append(g('runChips')); g('runChips').append(lab(g('chipEngine')), lab(g('chipAccount')), lab(g('chipProject')), lab(g('chipMode')), lab(g('chipCost')), ...[g('chipSuggest') && lab(g('chipSuggest')), g('runWhy')].filter(Boolean), g('runEquals'));
const DOC_LISTENERS = {};
const document = {
  head, body, documentElement: docEl, hidden: false, activeElement: null,
  getElementById: id => byId.get(id) || null,
  createElement: tag => {
    const el = new El(tag);
    if (tag === 'script') { Object.defineProperty(el, 'src', { set(v) { el._src = v; setTimeout(() => { if (/mbverify/.test(v)) el.onerror && el.onerror(); else el.onload && el.onload(); }, 0); }, get() { return el._src; } }); }
    return el;
  },
  createElementNS: (ns, tag) => { const el = new El(tag); el.ns = ns; return el; },
  createTextNode: txt,
  querySelectorAll: qsa,
  querySelector: sel => qsa(sel)[0] || null,
  addEventListener(t, f) { (DOC_LISTENERS[t] = DOC_LISTENERS[t] || []).push(f); },
  execCommand() { return true; },
};
const docFire = (t, ev) => (DOC_LISTENERS[t] || []).forEach(f => f(ev));
const LOG = { toasts: [], posts: [], gets: [], errors: [], ws: [], vib: [] };
class FakeWS {
  constructor(u) { this.url = u; this.readyState = 0; this.sent = []; LOG.ws.push(this); setTimeout(() => { if (this.readyState === 0) { this.readyState = 1; this.onopen && this.onopen(); } }, 0); }
  send(d) { this.sent.push(d); } close() { this.readyState = 3; this.onclose && this.onclose({ code: 1000 }); }
}
// xterm whose size never changes: the fit addon is a no-op, so onResize never fires (the
// regression case for the resize-on-connect hotfix)
let DISPOSED = 0;
class FakeTerm {
  constructor(opts) { this.options = opts || {}; this.cols = 80; this.rows = 24; this.buffer = { active: { baseY: 0, viewportY: 0, getLine: () => null } }; this.written = []; }
  open() {} loadAddon() {} onSelectionChange() {} onData() {} onResize(f) { this._onResize = f; } write(d) { this.written.push(d); } focus() {} getSelection() { return ''; } dispose() { DISPOSED++; }
}
// ---- fetch: the contract's shapes ---------------------------------------------
const DESKS = [
  { name: 'desk-a', armed: true, armed_at: 1730000000, n: 1, pane: true, reach: 'inbox', cli: 'claude', meta: {}, project: 'proj-one', cwd: '/work/u/proj-one', needs_you: false },
  { name: 'claude-2', armed: false, armed_at: 0, n: 0, pane: true, reach: 'pane', cli: 'claude', meta: { kind: 'claude', account: 'work' }, project: 'proj-one', cwd: '/work/u/proj-one', needs_you: true },
  { name: 'run-3', armed: false, armed_at: 0, n: 0, pane: true, reach: 'idle', cli: 'shell', meta: { kind: 'run', engine: 'claude' }, project: 'other', cwd: '/tmp/x', needs_you: false },
  { name: 'stray', armed: false, armed_at: 0, n: 0, pane: false, reach: 'none', cli: '', meta: {}, project: 'other', cwd: '', needs_you: false },
];
const MODULES = [
  { id: 'runs', label: 'Runs', icon: '◎', order: 10, kind: 'page', page: 'runs', phase: 1 },
  { id: 'accounts', label: 'Accounts', icon: '⚿', order: 20, kind: 'page', page: 'accounts', phase: 1 },
  { id: 'plugins', label: 'Extend', icon: '⬢', order: 30, kind: 'catalog', phase: 1, modes: ['user', 'dev'],
    sections: [{ title: 'Installed', items: [{ catalog: 'plugin.list', label: 'List' }, { catalog: 'plugin.new', label: 'New', params_ui: { name: { default: 'demo' } } }] }] },
  { id: 'office', label: 'Office', icon: '▦', order: 60, kind: 'catalog', phase: 1,
    sections: [{ title: 'Desks', items: [{ catalog: 'plugin.list', label: 'List' }, { catalog: 'plugin.new', label: 'New', params_ui: { name: { default: 'demo' } } }] }] },
  { id: 'workflows', label: 'Workflows', icon: '⇶', order: 40, kind: 'catalog', phase: 1, sections: [{ title: 'Workflows', items: [{ catalog: 'workflow.list', label: 'List' }] }] },
  { id: 'connections', label: 'Connections', icon: '⌁', order: 50, kind: 'catalog', phase: 1, sections: [{ title: 'Connections', items: [{ catalog: 'plugin.list', label: 'List' }] }] },
  { id: 'sdk', label: 'SDK', icon: '∑', order: 70, kind: 'page', page: 'sdk', phase: 1 },
  { id: 'desktop', label: 'Desktop', icon: '▣', order: 80, kind: 'page', page: 'desktop', phase: 1 },
  { id: 'tools', label: 'Tools', icon: '⚒', order: 90, kind: 'page', page: 'tools', phase: 1 },
  { id: 'a2a', label: 'A2A', icon: '⇄', order: 95, kind: 'card', phase: 2, card: { text: 'later', links: [] } },
  // section 18: a module that contributes palette commands; /handoff here must NOT override the built-in
  { id: 'pingmod', label: 'Ping', icon: '◌', order: 96, kind: 'card', phase: 1, card: { text: 'pings', links: [] }, modes: ['user', 'dev'],
    commands: [
      { slash: '/ping', label: 'Ping', hint: 'ping a desk', args: [{ name: 'desk', type: 'desk' }, { name: 'text', type: 'text' }], run: { send: { kind: 'message' } } },
      { slash: '/plug', label: 'Plug', hint: 'scaffold a plugin', args: [{ name: 'name', type: 'text' }], run: { catalog: 'plugin.new' } },
      { slash: '/handoff', label: 'Evil', hint: 'tries to override a built-in', args: [], run: { send: { kind: 'message' } } },
      { slash: 'bad slash', label: 'Bad', hint: 'malformed', args: [], run: {} },
    ] },
];
const CATALOG = { entries: [
  { id: 'plugin.list', label: 'List plugins', module: 'plugins', kind: 'read', argv: ['{metabot}', 'plugin', 'list'], params: [] },
  { id: 'plugin.new', label: 'New plugin', module: 'plugins', kind: 'desk', argv: ['{metabot}', 'plugin', 'new', '{name}'], params: [{ name: 'name', type: 'id', label: 'Plugin name', required: true }] },
  { id: 'engines.list', label: 'Engines', module: 'office', kind: 'read', argv: ['{metabot}', 'engines'], params: [] },
  { id: 'sdk.verify', label: 'sdk verify', module: 'sdk', kind: 'read', argv: ['{orcha}', 'verify', '--json', '{run}'], params: [{ name: 'run', type: 'receipt', label: 'Receipt', required: true }] },
  { id: 'workflow.list', label: 'List workflows', module: 'workflows', kind: 'read', argv: ['{metabot}', 'workflow', 'list'], params: [] },
  { id: 'workflow.run', label: 'Run workflow', module: 'workflows', kind: 'desk', argv: ['{metabot}', 'workflow', 'run', '{ref}'], params: [{ name: 'ref', type: 'id', label: 'Workflow', required: true }] },
  // section 25.3 B7
  { id: 'propose', label: 'Propose', module: 'workflows', kind: 'read', argv: ['{metabot}', 'propose', '--json', '--folder', '{folder}', '{task}'], params: [{ name: 'folder', type: 'path', label: 'Folder', required: true }, { name: 'task', type: 'text', label: 'Task', required: true }] },
  { id: 'suggest', label: 'Suggest', module: 'runs', kind: 'read', argv: ['{metabot}', 'suggest', '{task}'], params: [{ name: 'task', type: 'text', label: 'Task', required: true }] },
  { id: 'ladder', label: 'Ladder', module: 'workflows', kind: 'read', argv: ['{metabot}', 'ladder'], params: [] },
  { id: 'engines.manifest', label: 'Engines manifest', module: 'plugins', kind: 'read', argv: ['{metabot}', 'engines', '--manifest'], params: [] },
  { id: 'doctor.models', label: 'Local models', module: 'plugins', kind: 'desk', argv: ['{metabot}', 'doctor', '--models'], params: [] },
  { id: 'plugin.new-bridge', label: 'New bridge', module: 'plugins', kind: 'desk', argv: ['{metabot}', 'plugin', 'new', '--bridge', '{name}'], params: [{ name: 'name', type: 'slug', label: 'Bridge name', required: true }] },
  { id: 'plugin.import-skills', label: 'Import skills', module: 'plugins', kind: 'desk', argv: ['{metabot}', 'plugin', 'import-skills', '--name', '{name}', '{folder}'], params: [{ name: 'name', type: 'slug', label: 'Plugin name', required: true }, { name: 'folder', type: 'path', label: 'Skills folder', required: true }] },
  { id: 'plugin.marketplace.add', label: 'Add marketplace', module: 'plugins', kind: 'desk', argv: ['{metabot}', 'plugin', 'marketplace', 'add', '{name}', '{folder}'], params: [{ name: 'name', type: 'slug', label: 'Marketplace', required: true }, { name: 'folder', type: 'path', label: 'Folder', required: true }] },
  { id: 'plugin.marketplace.remove', label: 'Remove marketplace', module: 'plugins', kind: 'desk', argv: ['{metabot}', 'plugin', 'marketplace', 'remove', '{name}'], params: [{ name: 'name', type: 'slug', label: 'Marketplace', required: true }] },
  { id: 'plugin.install', label: 'Install plugin', module: 'plugins', kind: 'desk', argv: ['{metabot}', 'plugin', 'install', '{ref}'], params: [{ name: 'ref', type: 'text', label: 'NAME@MARKETPLACE', required: true }] },
  { id: 'plugin.enable', label: 'Enable plugin', module: 'plugins', kind: 'desk', argv: ['{metabot}', 'plugin', 'enable', '{name}'], params: [{ name: 'name', type: 'slug', label: 'Plugin', required: true }] },
  { id: 'plugin.disable', label: 'Disable plugin', module: 'plugins', kind: 'desk', argv: ['{metabot}', 'plugin', 'disable', '{name}'], params: [{ name: 'name', type: 'slug', label: 'Plugin', required: true }] },
  { id: 'plugin.remove', label: 'Remove plugin', module: 'plugins', kind: 'desk', argv: ['{metabot}', 'plugin', 'remove', '{name}'], params: [{ name: 'name', type: 'slug', label: 'Plugin', required: true }] },
  { id: 'plugin.enable-bridges', label: 'Enable bridges', module: 'plugins', kind: 'desk', argv: ['{metabot}', 'plugin', 'enable', '--allow-bridges', '{name}'], params: [{ name: 'name', type: 'slug', label: 'Plugin', required: true }] },
  { id: 'plugin.enable-servers', label: 'Add servers', module: 'plugins', kind: 'desk', argv: ['{metabot}', 'plugin', 'enable', '--add-servers', '{name}'], params: [{ name: 'name', type: 'slug', label: 'Plugin', required: true }] },
  { id: 'plugin.enable-hooks', label: 'Allow hooks (developer)', module: 'plugins', kind: 'desk', argv: ['{metabot}', 'plugin', 'enable', '--allow-hooks', '{name}'], params: [{ name: 'name', type: 'slug', label: 'Plugin', required: true }] },
  { id: 'workflow.new-ladder', label: 'New ladder workflow', module: 'workflows', kind: 'desk', argv: ['{metabot}', 'workflow', 'new', '--ladder', '--cwd', '{folder}', '{name}', '{prompt}'], params: [{ name: 'name', type: 'id', label: 'Name', required: true }, { name: 'folder', type: 'path', label: 'Folder', required: true }, { name: 'prompt', type: 'text', label: 'Prompt', required: true }] },
  { id: 'workflow.new-office', label: 'New office workflow', module: 'workflows', kind: 'desk', argv: ['{metabot}', 'workflow', 'new', '--office', '{folder}', '--cwd', '{folder}', '{name}', '{prompt}'], params: [{ name: 'name', type: 'id', label: 'Name', required: true }, { name: 'folder', type: 'path', label: 'Folder', required: true }, { name: 'prompt', type: 'text', label: 'Prompt', required: true }] },
  { id: 'workflow.approve', label: 'Approve workflow', module: 'workflows', kind: 'desk', argv: ['{metabot}', 'workflow', 'approve', '{ref}'], params: [{ name: 'ref', type: 'id', label: 'Workflow', required: true }] },
  { id: 'workflow.schedule', label: 'Schedule workflow', module: 'workflows', kind: 'desk', argv: ['{metabot}', 'workflow', 'schedule', '{ref}'], params: [{ name: 'ref', type: 'id', label: 'Workflow', required: true }] },
  { id: 'serve', label: 'Serve', module: 'workflows', kind: 'desk', argv: ['{metabot}', 'serve'], params: [] },
]};
// GET /api/extend (25.3 B6): one plugin pinned with a sha256: prefix, one bare; a bridge engine that is local; a broken user module
const XT = {
  plugins: [
    { name: 'notes', ref: 'notes@notes', marketplace: 'notes', tree: 'sha256:a1b2c3d4e5f6' + '0'.repeat(52), version: '0.1.0', enabled: true,
      skills: ['notes'], commands: [], agents: [], hooks: { PreToolUse: 1 }, hooks_allowed: false, servers: ['notes-mcp'], servers_added: false, bridges: [], bridges_on: false, error: null },
    { name: 'echo', ref: 'echo@echo', marketplace: 'echo', tree: 'f0e1d2c3b4a5' + '9'.repeat(52), version: '0.2.0', enabled: false,
      skills: [], commands: ['hello'], agents: [], hooks: {}, hooks_allowed: false, servers: [], servers_added: false, bridges: ['echo'], bridges_on: false, error: null },
  ],
  marketplaces: [
    { name: 'notes', folder: '/work/u/proj-one/notes', plugins: [{ name: 'notes', description: 'the notes skill' }], error: null },
    { name: 'team', folder: '/work/u/proj-one/team', plugins: [{ name: 'review-kit', description: "<b>review</b> & 'check'" }], error: null },
  ],
  engines: [
    { name: 'claude', kind: 'builtin', local: false, models: [], plugin: null },
    { name: 'cursor', kind: 'builtin', local: false, models: [], plugin: null },
    { name: 'openai', kind: 'builtin', local: true, models: ['qwen2.5-coder:3b'], plugin: null },
    { name: 'echo', kind: 'bridge', local: true, models: ['echo-1'], plugin: 'echo@echo' },
  ],
  errors: [],
  modules: { builtin: ['runs', 'accounts', 'plugins'], user: [
    { id: 'pingmod', file: '/s/modules/pingmod.json', ok: true, error: null },
    { id: 'broken', file: '/s/modules/broken.json', ok: false, error: 'unknown catalog id x.y' }], folder: '/s/modules' },
  serve: { running: false, desk: '' },
};
let XT_DOWN = false;   // GET /api/extend answers 404 (an older server)
const WF_ROWS = [];   // workflow list rows a test adds: what MetaBot stored after a desk was answered
const PROPOSAL = (task, folder) => {   // metabot propose --json, by the task's words
  const shape = /ladder|build/.test(task) ? 'ladder' : /office/.test(task) ? 'office' : 'ask';
  return { shape, name: shape === 'ask' ? 'review-the-parser' : shape === 'ladder' ? 'build-the-ladder' : 'office-split',
    rules: shape === 'ask' ? ['a review is one ask, on the review role', 'a review reads and changes nothing, so the step is read_only'] : ['build work runs as a ladder'],
    runs_on: shape === 'ask' ? ['ask: claude, role review, no login'] : ['ladder: rung 1 free echo'],
    missing: shape === 'ladder' ? ['max_cost in step ladder: it may run on a paid rung'] : [],
    // MetaBot's own step shapes (propose.py _ask / _ladder / _office, then allow_write, allow and max_cost on every step)
    draft: { steps: [/noengine/.test(task) ? { name: 'ask', kind: 'ask', prompt: task }
      : Object.assign({ name: shape, kind: shape, prompt: task }, shape === 'ask' ? { role: 'review', engine: 'claude', read_only: true } : shape === 'ladder' ? { checks: [] } : { dir: folder, office_sha256: 'cd'.repeat(32) },
        { allow_write: false, allow: [], max_cost: null })] },
    sha256: '9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08', next: ['metabot workflow new … ' + folder] };
};
const KEY_OK = ['enter', 'esc', 'up', 'down', 'tab', '1', '2', '3', 'y', 'n', 'q'];
const MODS_MADE = new Set();
const RUNS = { runs: [
  { run_id: 'live-1', state: 'live', desk: 'run-3', needs_you: false, project: 'other', started: '2026-08-07T10:00:00Z', engine: 'claude', cwd: '/tmp/x', live_id: 'l1' },
  { run_id: 'run-9c2e-example', state: 'done', receipt_state: 'valid', desk: null, project: 'other', row: { engine: 'claude', started_at: '2026-08-06T00:58:21Z' } },
], total: 2, error: null };
const ACCT = { claude: [{ name: 'work', home: '/s/accounts/claude/work', max_at_once: 3, ready: true, source: 'metabot' }],
  cursor: [{ name: 'alt', folder: '/s/accounts/cursor/alt', proven: false, ready: false }], defaults: { claude: { ready: true }, cursor: { ready: false } } };
let META_INBOX = {name: 'test inbox', arm_hint: "this desk's Claude holds the test inbox", arm_command: '/test-arm {desk}'};   // B1: a configured inbox adapter
let META_DEFAULT_FOLDER = '/work/u/notes';   // 25.3 B4: a configured default folder outside every project
let PC_MODE = 'valid';   // valid | invalid | unreadable | down
let META_BUILD = 'b1';
const ALIASES = { 'claude-2': 'Second' };
const SPAWNED = {};   // label -> count: the second identical label is 'taken' (409)
const resp = (status, body, raw) => ({ ok: status < 300, status, json: async () => body, text: async () => raw ?? JSON.stringify(body), arrayBuffer: async () => new TextEncoder().encode(raw ?? JSON.stringify(body)).buffer });
globalThis.fetch = async (url, opts) => {
  const u = new URL(url, 'http://h');
  if (u.searchParams.get('t') !== 'tok') LOG.errors.push('no token on ' + url);
  if (opts && opts.method === 'POST') {
    LOG.posts.push({ path: u.pathname, headers: opts.headers, body: JSON.parse(opts.body) });
    const b = JSON.parse(opts.body);
    switch (u.pathname) {
      case '/api/cli/read':
        if (b.id === 'propose') return /garbage/.test(b.params.task) ? resp(200, { ok: true, id: b.id, argv: ['/x/metabot', 'propose'], exit: 2, ms: 3, stdout: '', stderr: 'metabot: not a task I can read' })
          : resp(200, { ok: true, id: b.id, argv: ['/x/metabot', 'propose', '--json'], exit: 0, ms: 9, stderr: '',
            stdout: JSON.stringify(/odd/.test(b.params.task) ? Object.assign(PROPOSAL(b.params.task, b.params.folder), { draft: { steps: [null, 'a line', { name: 'ask', kind: 'ask', engine: 'claude' }] } })
              : PROPOSAL(b.params.task, b.params.folder)) + '\n' });
        if (b.id === 'suggest') return /private/.test(b.params.task)
          ? resp(200, { ok: true, id: b.id, argv: ['/x/metabot', 'suggest'], exit: 1, ms: 4, stdout: 'role    private\nengine  -\nmodel   -\nrule    private: the task says "private"\nnote    no engine for the private role here, nor for the build role\n', stderr: '' })
          : resp(200, { ok: true, id: b.id, argv: ['/x/metabot', 'suggest'], exit: 0, ms: 4, stdout: 'role    review\nengine  cursor\nmodel   (its own)\nrule    review: the task says "review"\n', stderr: '' });
        return resp(200, { ok: true, id: b.id, argv: ['/x/metabot', 'engines'], exit: 0, ms: 12,
        stdout: b.id === 'engines.list' ? '* claude  2.1\n  cursor  ok\n  codex   not installed (codex not on PATH)\n  openai  http://127.0.0.1:1/v1 model m: down\n'
              : b.id === 'workflow.list' ? 'wf-one   approved   2 steps\nwf-two   draft      1 step\n' + WF_ROWS.map(r => r + '\n').join('') : 'plugin-a\n<b>&\'x\n', stderr: '' });
      case '/api/cli/desk':
        if (b.id === 'serve' && XT.serve.running) return resp(409, { ok: false, error: 'serve already runs' });
        if (b.params && typeof b.params.folder === 'string' && /^\/work\/u\/\.claude/.test(b.params.folder)) return resp(400, { ok: false, error: 'bad folder: bad path' });   // catalog.path_ok: never under ~/.claude*
        return resp(200, { ok: true, name: b.id.replace(/\./g, '-'), argv: ['/x/metabot'].concat(b.id.split('.')).concat(Object.values(b.params || {})) });
      case '/api/keys': {   // 25.3 B3 with the list form: {name, keys:[1–4 allowed keys]} or {name, text} at the code step
        const d = DESKS.find(x => x.name === b.name);
        if (!d) return resp(404, { ok: false, error: 'no such desk' });
        if ('text' in b) return d.signin && d.signin.step === 'code' ? resp(200, { ok: true }) : resp(409, { ok: false, error: 'not at the code step' });
        const ks = Array.isArray(b.keys) ? b.keys : [];
        if (!ks.length || ks.length > 4 || !ks.every(k => KEY_OK.includes(k))) return resp(400, { ok: false, error: 'bad keys' });
        return resp(200, { ok: true });
      }
      case '/api/modules/new':
        if (MODS_MADE.has(b.id)) return resp(409, { ok: false, error: 'module ' + b.id + ' exists' });
        MODS_MADE.add(b.id); return resp(200, { ok: true, file: '/s/modules/' + b.id + '.json', error: null });
      case '/api/run': return resp(200, { ok: true, name: 'run-4', argv: ['/x/metabot', 'ask', '-e', b.engine, '-'], prompt_file: 'abc.txt', account_used: !!b.account });
      case '/api/mb/verify':
        if (PC_MODE === 'down') throw new TypeError('fetch failed');
        if (PC_MODE === 'unreadable') return resp(200, { ok: true, id: b.id, pc: null, pc_exit: 2, metabot: { receipt_state: 'unreadable' }, index_sha_ok: null, sha256: 'ab'.repeat(32) });
        return resp(200, { ok: true, id: b.id, pc: { valid: PC_MODE === 'valid', verdict: PC_MODE === 'valid' ? 'valid' : 'bad signature', run_id: b.id, signer_did: 'did:orcha:system:validator', step_count: 2,
          checks: { schema: true, steps_root: true, steps_merkle_root: true, signature: PC_MODE === 'valid' }, coverage: {} }, pc_exit: PC_MODE === 'valid' ? 0 : 1, metabot: { receipt_state: 'valid' }, index_sha_ok: true, sha256: 'ab'.repeat(32) });
      case '/api/sdk/verify': {
        const raw = b.raw_b64 ? Buffer.from(b.raw_b64, 'base64').toString('utf8') : b.raw;
        return raw === 'garbage' ? resp(200, { ok: true, pc: null, pc_exit: 2, stderr: 'not json' }) : resp(200, { ok: true, pc: { valid: true, verdict: 'valid', checks: { schema: true } }, pc_exit: 0, stderr: '' });
      }
      case '/api/spawn': {
        if (b.signin) {   // 25.3 B1: a sign-in desk on a named account, one at a time per account
          if (!b.account) return resp(400, { ok: false, error: 'sign-in needs an account' });
          if (DESKS.some(d => d.signin && d.signin.engine === b.kind && d.signin.account === b.account)) return resp(409, { ok: false, error: 'already signing in' });
          const name = 'signin-' + b.kind + '-' + b.account;
          DESKS.push({ name, armed: false, armed_at: 0, n: 0, pane: true, reach: 'pane', cli: b.kind, meta: { kind: b.kind, account: b.account, signin: true }, project: 'other', cwd: '/s/signin', needs_you: false,
            signin: { engine: b.kind, account: b.account, step: b.kind === 'cursor' ? 'link' : 'style', url: b.kind === 'cursor' ? 'https://cursor.com/loginDeepControl?challenge=abc&mode=login' : null } });
          return resp(200, { ok: true, name });
        }
        if ((b.kind === 'claude' || b.kind === 'cursor') && !b.cwd && !META_DEFAULT_FOLDER) return resp(400, { ok: false, error: 'choose a project folder' });   // 25.3 B4
        if (b.label) { SPAWNED[b.label] = (SPAWNED[b.label] || 0) + 1; if (SPAWNED[b.label] > 1) return resp(409, { ok: false, error: 'taken' }); }
        return resp(200, { ok: true, name: b.label || b.kind + '-9' });
      }
      case '/api/accounts': return resp(200, { ok: true, engine: b.engine, name: b.name, folder: '/s/accounts/' + b.engine + '/' + b.name });
      case '/api/kill': { const i = DESKS.findIndex(d => d.name === b.name && d.signin); if (i >= 0) DESKS.splice(i, 1); return resp(200, { ok: true }); }
      case '/api/send': if (b.target === 'old-chat') { const d = DESKS.find(x => x.name === 'old-chat'); return d && d.reach === 'pane' ? resp(200, { ok: true, via: 'pane', reach: 'pane' }) : resp(409, { ok: false, error: 'not armed', reach: 'none', via: '' }); }
        return b.text === 'fail-me' ? resp(409, { ok: false, error: 'not armed', reach: 'none', via: '' }) : resp(200, { ok: true, via: b.target === 'desk-a' ? 'inbox' : 'pane', reach: b.target === 'desk-a' ? 'inbox' : 'pane' });
      case '/api/alias': if (b.name === 'refuse-me') return resp(409, { ok: false, error: 'no such desk' }); if (b.alias) ALIASES[b.name] = b.alias; else delete ALIASES[b.name]; return resp(200, { ok: true });
      case '/api/pane': if (b.action === 'resume') { const d = DESKS.find(x => x.name === b.target); if (!d || !d.resume) return resp(409, { ok: false, error: 'no saved Claude session under this name' }); Object.assign(d, { reach: 'pane', resume: false, pane: true }); return resp(200, { ok: true, error: '', name: d.name }); }
        return resp(200, { ok: true });
      case '/api/modules/reload': return resp(200, { ok: true });
      default: return resp(404, { ok: false, error: 'not found' });
    }
  }
  LOG.gets.push(u.pathname + u.search);
  switch (true) {
    case u.pathname === '/api/meta': return resp(200, { app: 'MetaBot', version: '0.1.0', build: META_BUILD, metabot: { bin: '/x/metabot', version: '0.3.0', ok: true }, orcha: { bin: '/x/orcha', version: '0.2.2', ok: true }, display: { vnc: false, unit: 'fc-gui' }, default_desk: 'desk-a', default_folder: META_DEFAULT_FOLDER, limits: { max_desks: 24, max_runs: 6 }, modules: 7, catalog: 4, inbox: META_INBOX });
    case u.pathname === '/api/tree': return resp(404, { ok: false, error: 'not found' });
    case u.pathname === '/api/desks': return resp(200, { desks: u.searchParams.get('scope') === 'metachat' ? DESKS.filter(d => d.meta.kind) : DESKS, streams: { 'in@lan': true, 'in@net': false } });
    case u.pathname === '/api/aliases': return resp(200, { aliases: ALIASES });
    case u.pathname === '/api/history': return resp(200, { messages: [{ via: 'in', time: 1730000001, title: 'inbox -> desk-a', message: "hi 'there' <b>", target: 'desk-a' }] });
    case u.pathname === '/api/projects': return resp(200, { projects: [{ name: 'proj-one', paths: ['/work/u/proj-one'], desks: ['desk-a'], runs: 1 }, { name: 'other', paths: [], desks: ['run-3'], runs: 1 }] });
    case u.pathname === '/api/accounts': return resp(200, ACCT);
    case u.pathname === '/api/extend': return XT_DOWN ? resp(404, { ok: false, error: 'not found' }) : resp(200, XT);
    case u.pathname === '/api/modules': return resp(200, { modules: MODULES, errors: [{ file: 'user/broken.json', error: 'unknown catalog id x.y' }] });
    case u.pathname === '/api/catalog': return resp(200, CATALOG);
    case u.pathname === '/api/mb/runs': return resp(200, RUNS);
    case u.pathname === '/api/mb/run/run-9c2e-example': return resp(200, { ok: true, run: { run_id: 'run-9c2e-example', meta: { engine: 'claude', model: 'm', account: 'work', cost_musd: 12345, cost_source: 'engine', tokens: { in: 1, out: 2 }, prompt: '<script>p</script>', final: "answer 'x'" }, verdicts: [], caveat: '' }, receipt_state: 'valid', desk: null });
    case u.pathname === '/api/mb/receipt/run-9c2e-example': return resp(200, null, GOLDEN_TEXT);
    case u.pathname.startsWith('/api/mb/receipt/'): return resp(404, { ok: false, error: 'not found' });
    default: return resp(404, { ok: false, error: 'not found' });
  }
};
// ---- globals the script expects -----------------------------------------------
let PROMPT_ANSWER = null;
const BOOT_DEV = process.env.MC_UI_BOOT === 'dev';   // second run from ui-smoke.mjs: a device that chose Developer earlier
const ES_LIST = [];
class FakeES { constructor(u) { this.url = u; this.readyState = 0; this.listeners = {}; ES_LIST.push(this); } addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); } close() { this.readyState = 2; } }
Object.assign(globalThis, {
  window: globalThis, document, localStorage: BOOT_DEV ? { fcMode: 'dev' } : {}, navigator: { vibrate(v) { LOG.vib.push(v); } },
  location: { search: '?t=tok', protocol: 'http:', host: '127.0.0.1:1', reload() { LOG.reloaded = true; } },
  innerWidth: 1400, innerHeight: 876, addEventListener() {}, removeEventListener() {},
  requestAnimationFrame: f => setTimeout(f, 0),
  getComputedStyle: () => ({ getPropertyValue: () => '' }),
  EventSource: FakeES,
  WebSocket: FakeWS, Terminal: FakeTerm, FitAddon: { FitAddon: class { fit() {} } },
  ResizeObserver: class { observe() {} },
  Event: class { constructor(t) { this.type = t; } },
  confirm: () => true, prompt: () => PROMPT_ANSWER, alert() {},
});
const tick = (ms = 5) => new Promise(r => setTimeout(r, ms));
const until = async (cond, ms = 2000) => { const t0 = Date.now(); while (!cond() && Date.now() - t0 < ms) await tick(5); return cond(); };
const STATS = { p: 0, f: 0 };
const ok = (c, w) => { if (c) { STATS.p++; console.log('ok   ' + w); } else { STATS.f++; console.log('FAIL ' + w); } };
const isResize = s => typeof s === 'string' && s[0] === '\x00' && JSON.parse(s.slice(1)).type === 'resize';

// ---- run the page script, then the scenario ------------------------------------
const bootDev = `
(async () => {
  const T0 = Date.now();
  while (!window.DESKS && Date.now() - T0 < 2000) await tick();
  await until(() => MODULES.length > 1); await tick(40);
  const kids = el => el.children.filter(c => c.nodeType === 1);
  ok(UMODE === 'dev' && !document.body.classList.contains('user'), 'fcMode=dev persisted: boots in developer mode');
  ok(!$('termPanel').classList.contains('hidden'), 'developer boot on a wide screen opens the terminal panel');
  await until(() => TERMS['desk-a'] && TERMS['desk-a'].ws && TERMS['desk-a'].ws.sent.length >= 1);
  const rec = TERMS['desk-a'];
  ok(rec && rec.ws && rec.ws.sent.length >= 1 && isResize(rec.ws.sent[0]), 'the socket sent a resize frame before any data: ' + JSON.stringify(rec && rec.ws && rec.ws.sent[0]));
  const abIds = kids($('abar')).map(b => b.dataset.id).filter(Boolean);
  ok(abIds.includes('sdk') && abIds.includes('tools') && abIds.includes('plugins'), 'developer boot shows the developer-only modules in the activity bar: ' + abIds.join(','));
  ok(!TABS.some(t => t.kind === 'page' && t.id === 'setup'), 'no set-up tab in developer mode');
  ok($('termBar').classList.contains('hidden'), 'the thin terminal bar is hidden while the panel is open');
  hideTerm(); ok(!$('termBar').classList.contains('hidden'), 'closing the panel shows the thin terminal bar (developer, a desk open)');
  ok(!$('termErr').textContent, 'no JS error reached the strip' + ($('termErr').textContent ? ': ' + $('termErr').textContent : ''));
  console.log('\\n' + STATS.p + ' passed, ' + STATS.f + ' failed (developer boot)');
  process.exit(STATS.f ? 1 : 0);
})().catch(e => { console.log('FAIL developer boot threw: ' + (e && e.stack || e)); process.exit(1); });
`;
const scenario = `
(async () => {
  const T0 = Date.now();
  while (!window.DESKS && Date.now() - T0 < 2000) await tick();
  await until(() => MODULES.length > 1); await tick(30);
  const kids = el => el.children.filter(c => c.nodeType === 1);
  const rows = () => kids($('palette')).filter(c => c.classList.contains('prow'));
  const heads = () => kids($('palette')).filter(c => c.classList.contains('phd')).map(c => c.textContent);
  const chipsOf = b => b.querySelectorAll('.chip');
  const bodyOf = b => b.querySelectorAll('.body')[0].innerHTML;   // bubble bodies go in through md(): innerHTML under this fake
  const sbText = () => ['sbStreams', 'sbRuns', 'sbNeed', 'sbMode', 'sbVnc', 'sbBuild'].map(i => $(i).textContent).join(' | ');
  const msgs = () => kids($('log')).filter(c => c.classList.contains('msg'));
  const abIds = () => kids($('abar')).map(b => b.dataset.id).filter(Boolean);
  const tabEls = () => kids($('tabs')).filter(c => c.classList.contains('tab'));
  const treeRows = () => $('sideBody').querySelectorAll('.tn');
  const leaf = n => treeRows().find(r => r.dataset.kind === 'desk' && r.dataset.name === n);
  const node = k => treeRows().find(r => r.dataset.kind === 'node' && r.dataset.key === k);
  const press = row => $('sideBody').fire('pointerdown', {target: row, clientY: 120});   // the listeners are delegated to #sideBody (finding ui-state-6)
  const lift = row => $('sideBody').fire('pointerup', {target: row});
  const tap = row => { press(row); lift(row); };

  // ---- section 16 + 23: a fresh device boots in user mode, named MetaBot -----------------
  ok(UMODE === 'user' && document.body.classList.contains('user'), 'fresh device: fcMode defaults to user and body.user is set');
  ok($('termPanel').classList.contains('hidden') && $('termBar').classList.contains('hidden'), 'user mode: no terminal panel, no terminal bar');
  ok($('wsName').textContent === 'desk-a', 'default desk from /api/meta: ' + $('wsName').textContent);
  ok(META.app === 'MetaBot' && !/MetaChat/.test($('wsName').textContent), 'the app is MetaBot where shown');
  ok(ACTIVE && ACTIVE.kind === 'chat' && ACTIVE.id === 'desk-a' && tabEls().length === 1 && tabEls()[0].classList.contains('on'), 'boot opens one chat tab for the default desk');
  ok(JSON.parse(localStorage.fcTabs)[0].id === 'desk-a', 'fcTabs persisted: ' + localStorage.fcTabs);
  ok(aliases['claude-2'] === 'Second' && disp('claude-2') === 'Second', 'GET /api/aliases replaced the local copy (server copy wins)');
  ok(!TABS.some(t => t.id === 'setup'), 'desks exist: no set-up tab was forced open');
  // activity bar (22.5): Chats first, modules by mode, Settings last; the SDK and Tools are developer-only
  ok(abIds()[0] === 'chats' && abIds().at(-1) === 'settings', 'activity bar: Chats first, Settings pinned last: ' + abIds().join(','));
  ok(!abIds().includes('sdk') && !abIds().includes('tools') && !abIds().includes('office') && abIds().includes('runs') && abIds().includes('accounts') && abIds().includes('workflows') && abIds().includes('desktop'), 'user mode activity bar hides developer modules: ' + abIds().join(','));
  ok(abIds().includes('plugins') && kids($('abar')).find(b => b.dataset.id === 'plugins').textContent === 'Extend', 'U3: Extend (module plugins) shows in user mode: ' + abIds().join(','));
  ok(abIds().includes('pingmod'), 'a manifest with modes [user, dev] shows in user mode');
  ok(kids($('abar')).every(b => b.tagName !== 'BUTTON' || b.id === 'abMode' || b.querySelectorAll('svg').length === 1), 'every activity item carries one inline SVG icon');
  const chatsBtn = kids($('abar')).find(b => b.dataset.id === 'chats');
  ok(chatsBtn.querySelectorAll('.cnt')[0] && chatsBtn.querySelectorAll('.cnt')[0].textContent === '1', 'Chats shows the needs-you count');
  ok(!/0\\.3\\.0|0\\.2\\.2/.test(sbText()), 'no version string in the status line: ' + sbText());
  ok($('sbNeed').textContent === 'needs you 1' && $('sbRuns').textContent === 'runs 1 live', 'status line: ' + $('sbNeed').textContent + ' · ' + $('sbRuns').textContent);
  ok($('sbStreams').textContent.includes('lan') && $('sbStreams').textContent.includes('net'), 'stream health in the status line: ' + $('sbStreams').textContent);
  ok($('sbVnc').textContent === ':99' && $('sbBuild').textContent === 'build b1', 'status line shows :99 and the build: ' + $('sbVnc').textContent + ' / ' + $('sbBuild').textContent);

  // ---- 22.3: the project tree, built on the client because GET /api/tree is 404 -----------
  ok(LOG.gets.some(x => x.startsWith('/api/tree')) && TREE_404 === true, '/api/tree was tried and its 404 remembered');
  ok(LOG.gets.filter(x => x.startsWith('/api/tree')).length === 1, 'the 404 is not retried on every refresh: ' + LOG.gets.filter(x => x.startsWith('/api/tree')).length);
  ok(!!node('waiting') && node('waiting').textContent.includes('Waiting on you') && node('waiting').textContent.includes('1'), 'tree: Waiting on you with a count');
  ok(!!node('p:proj-one') && !!node('f:proj-one:/work/u/proj-one'), 'tree: project → folder nodes');
  ok(!!leaf('desk-a') && !!leaf('claude-2') && leaf('claude-2').textContent.includes('Second'), 'tree: desks under their folder, aliases shown');
  ok(leaf('claude-2').querySelectorAll('.tst')[0].textContent === 'needs you' && leaf('desk-a').querySelectorAll('.tst').length === 0, 'tree: a state word for the exceptions (needs you), nothing on a live desk');
  ok(leaf('stray') && leaf('stray').querySelectorAll('.tst')[0].textContent === 'not armed' && !!node('ended') && !node('floor'), 'tree: a stray session is under Ended with the one allowed word');
  ok(treeRows().some(r => r.dataset.kind === 'run' && r.dataset.name === 'live-1'), 'tree: a live run sits under its folder');
  ok(!treeRows().some(r => /no CLI running|not armed · no desk here/.test(r.textContent)), 'tree: no REACH_TXT sentences on rows (21.6)');
  // collapse a node, persist, re-render
  tap(node('p:proj-one')); await tick(5);
  ok(JSON.parse(localStorage.fcTree)['p:proj-one'] === false && !leaf('desk-a'), 'tapping a node folds it and fcTree remembers');
  tap(node('p:proj-one')); await tick(5);
  ok(!!leaf('desk-a'), 'tapping again unfolds it');
  // a tap across a re-render: the long-press timer lives outside the rows (finding ui-state-6)
  press(leaf('claude-2')); renderTree(); lift(leaf('claude-2')); await tick(600);
  ok(WS === 'desk-a' && !TABS.some(t => t.id === 'claude-2'), 'a tap interrupted by a re-render is dropped, no menu or rename fires');
  // tap a desk: a tab opens
  tap(leaf('claude-2')); await tick(10);
  ok(WS === 'claude-2' && tabEls().length === 2 && tabEls()[1].classList.contains('on') && tabEls()[1].textContent.includes('Second'), 'tapping a desk opens its tab and makes it active');
  ok(!$('deskSheet').classList.contains('hidden'), 'user mode: a desk that needs you opens its prompt sheet on tap');
  closeSheets();
  // long-press = the node menu
  press(leaf('desk-a')); await tick(620);
  ok(!$('menu').classList.contains('hidden') && $('menu').textContent.includes('Rename') && $('menu').textContent.includes('Pin'), 'long-press on a desk opens its menu: ' + $('menu').textContent);
  hideMenus();
  // search filters desks and runs
  $('sideSearch').value = 'live'; $('sideSearch').oninput({target: $('sideSearch')}); await tick(5);
  ok(treeRows().some(r => r.dataset.name === 'live-1') && !leaf('stray'), 'side search narrows the tree to matches');
  $('sideSearch').value = ''; $('sideSearch').oninput({target: $('sideSearch')}); await tick(5);
  // tabs: max 8, close, Ctrl+Tab
  for (const n of ['t1', 't2', 't3', 't4', 't5', 't6', 't7']) ensureTab('chat', n);
  ok(TABS.length === 8, 'the tab bar holds at most 8 tabs: ' + TABS.length);
  renderTabs(); tabEls()[0].querySelectorAll('.tx')[0].onclick({stopPropagation() {}}); await tick(5);
  ok(TABS.length === 7 && tabEls().length === 7, 'the × closes a tab');
  TABS.splice(0, TABS.length, {kind: 'chat', id: 'desk-a'}, {kind: 'chat', id: 'claude-2'}); saveTabs(); activateTab('chat', 'desk-a'); await tick(5);
  docFire('keydown', {ctrlKey: true, key: 'Tab', preventDefault() {}}); await tick(5);
  ok(WS === 'claude-2', 'Ctrl+Tab cycles to the next tab: ' + WS);
  docFire('keydown', {ctrlKey: true, key: 'Tab', preventDefault() {}}); await tick(5);
  ok(WS === 'desk-a', 'and wraps around');

  // ---- 25.4 U6: Waiting on you answers by one tap through /api/keys; a typed yes, sudo and secrets go to the terminal ----
  const wkeys = () => node('waiting').parentNode.querySelectorAll('.wkeys');
  const wkText = i => wkeys()[i] ? wkeys()[i].querySelectorAll('.wk').map(b => b.textContent).join(',') : 'no keys row';
  ok(wkeys().length === 1 && wkText(0) === 'Answer in the terminal', 'U6: a Claude ask with no class (a client-built tree) goes to the terminal, never a one-tap Enter: ' + wkText(0));
  TREE = {waiting: [{desk: 'claude-2', run: null, cls: 'write', since: 0}], projects: [], other: [], ended: []}; renderTree();
  ok(wkeys().length === 1 && wkText(0) === 'Enter,Esc', 'U6: a Claude desk waiting on a select menu the server classed offers Enter and Esc: ' + wkText(0));
  ok(leaf('claude-2').querySelectorAll('.wk').length === 0, 'U6: the keys sit under the row, never inside it (a key tap is not a row tap)');
  const nK = LOG.posts.filter(q => q.path === '/api/keys').length;
  if (wkeys().length) { await wkeys()[0].querySelectorAll('.wk')[0].onclick({}); await tick(10); }
  const kq = LOG.posts.filter(q => q.path === '/api/keys').at(-1);
  ok(LOG.posts.filter(q => q.path === '/api/keys').length === nK + 1 && JSON.stringify(kq.body) === JSON.stringify({name: 'claude-2', keys: ['enter']}), 'U6: Enter posts {name, keys:["enter"]} (25.3 B3 list form): ' + JSON.stringify(kq && kq.body));
  ok(WS === 'desk-a' && $('deskSheet').classList.contains('hidden'), 'U6: a key tap answers in place: no desk switch, no sheet');
  // the server's waiting rows carry a class: a MetaBot write ask takes y or n then Enter; a destructive ask takes no key
  TREE = {waiting: [{desk: 'run-3', run: 'live-1', cls: 'write', since: 0}, {desk: 'claude-2', run: null, cls: 'destructive', since: 0}, {desk: 'stray', run: null, cls: 'read', since: 0}], projects: [], other: [], ended: []};
  renderTree();
  ok(wkText(0) === 'y,n' && wkText(1) === 'Answer in the terminal' && wkText(2) === 'Answer in the terminal', 'U6: write on a MetaBot run desk = y / n; destructive = the terminal; a desk of unknown kind = the terminal: ' + [0, 1, 2].map(wkText).join(' | '));
  if (wkeys().length === 3) { await wkeys()[0].querySelectorAll('.wk')[0].onclick({}); await tick(10); }
  ok(JSON.stringify((LOG.posts.filter(q => q.path === '/api/keys').at(-1) || {}).body) === JSON.stringify({name: 'run-3', keys: ['y', 'enter']}), 'U6: y on a line prompt posts keys ["y","enter"]');
  const nK2 = LOG.posts.filter(q => q.path === '/api/keys').length;
  if (wkeys().length === 3) { await wkeys()[1].querySelectorAll('.wk')[0].onclick({}); await tick(10); }
  ok(LOG.posts.filter(q => q.path === '/api/keys').length === nK2 && WS === 'claude-2' && !$('deskSheet').classList.contains('hidden'), 'U6: a destructive ask opens its desk (user mode: the prompt sheet) and posts no key');
  closeSheets(); setWS('desk-a'); await tick(5);
  // what an ask takes: the server word, the class, the screen when this page holds the terminal, the desk kind
  const scr = line => ({ term: { rows: 24, buffer: { active: { baseY: 0, getLine: vy => vy === 23 ? { translateToString: () => line } : null } } } });
  const ak = (w, d) => { try { return askKind(w, d); } catch (e) { return 'threw ' + e.message; } };
  TERMS['zz-ask'] = scr('[sudo] password for u:');
  const akSudo = ak({desk: 'zz-ask', cls: 'write'}, {name: 'zz-ask', cli: 'shell', meta: {kind: 'cli'}});
  TERMS['zz-ask'] = scr("  allow? type 'yes' to allow, anything else declines:");
  const akYes = ak({desk: 'zz-ask', cls: 'write'}, {name: 'zz-ask', cli: 'shell', meta: {kind: 'run'}});
  TERMS['zz-ask'] = scr("  allow? type 'y' to allow, anything else declines:");
  const akY = ak({desk: 'zz-ask', cls: 'write'}, {name: 'zz-ask', cli: 'shell', meta: {kind: 'run'}});
  TERMS['zz-ask'] = scr('install it? [y/N]');
  const akYN = ak({desk: 'zz-ask', cls: 'read'}, {name: 'zz-ask', cli: 'shell', meta: {kind: 'cli'}});
  delete TERMS['zz-ask'];
  ok(akSudo === 'secret', 'U6: a sudo password on the screen is a secret, answered in the terminal: ' + akSudo);
  ok(akYes === 'yes', "U6: MetaBot's type 'yes' is never a one-tap key, whatever the class says: " + akYes);
  ok(akY === 'line' && akYN === 'line', "U6: allow? type 'y' and [y/N] are line answers: " + akY + ' ' + akYN);
  ok(ak({desk: 'x', cls: 'destructive'}, {name: 'x', cli: 'claude', meta: {kind: 'claude'}}) === 'yes', 'U6: a destructive class wins over the desk kind');
  ok(ak({desk: 'x', cls: 'destructive', ask: 'select'}, {name: 'x', cli: 'claude', meta: {kind: 'claude'}}) === 'yes', 'U6: a destructive class wins over a server word too');
  ok(ak({desk: 'x', cls: 'write'}, {name: 'x', cli: 'shell', meta: {}}) === '', 'U6: a shell desk with no screen to read goes to the terminal (sudo lives there)');
  ok(ak({desk: 'x', cls: ''}, {name: 'x', cli: 'claude', meta: {kind: 'claude'}}) === '' && ak({desk: 'x', cls: 'write'}, {name: 'x', cli: 'claude', meta: {kind: 'claude'}}) === 'select', 'U6: a Claude ask takes Enter / Esc once its class is known, the terminal before');
  ok(ak({desk: 'x', cls: 'read', ask: 'select'}, {name: 'x', cli: 'shell', meta: {}}) === 'select', 'U6: a server word naming the ask wins');
  ok(ak({desk: 'x', cls: 'read', ask: 'secret'}, {name: 'x', cli: 'claude', meta: {kind: 'claude'}}) === 'secret', 'U6: the server can name a secret');
  ok(ak({desk: 'x', cls: 'read'}, {name: 'x', cli: 'claude', meta: {kind: 'claude'}, signin: {engine: 'claude', account: 'work', step: 'method', url: null}}) === 'signin', 'U6: a sign-in desk is answered on its card');
  // the PC names the ask: ask (signin | secret | yes | line | select) and ask_line on waiting rows and desk rows; the word picks the keys, the line is text
  ok(ak({desk: 'x', cls: 'read', ask: 'line'}, {name: 'x', cli: 'shell', meta: {}}) === 'line' && ak({desk: 'x', cls: 'read', ask: 'signin'}, {name: 'x', cli: 'claude', meta: {kind: 'claude'}}) === 'signin'
    && ak({desk: 'x', cls: 'write', ask: 'yes'}, {name: 'x', cli: 'shell', meta: {kind: 'run'}}) === 'yes', 'U6: the word wins over the desk kind: line on a shell desk, signin, yes');
  ok(ak({desk: 'x', cls: 'read'}, {name: 'x', cli: 'shell', meta: {}, ask: 'select', cls: 'read'}) === 'select', 'U6: a desk row word counts when the waiting row has none');
  ok(ak({desk: 'x', cls: 'destructive', ask: 'line'}, {name: 'x', cli: 'shell', meta: {kind: 'run'}}) === 'yes', 'U6: a destructive class still goes to the terminal, whatever the word (21.7 item 2)');
  TERMS['zz-ask'] = scr('[sudo] password for u:');
  const akEsc = ak({desk: 'zz-ask', cls: 'write', ask: 'line'}, {name: 'zz-ask', cli: 'shell', meta: {kind: 'run'}});
  delete TERMS['zz-ask'];
  ok(akEsc === 'secret', 'U6: a secret on the screen this page holds goes to the terminal over a line word (the screen can take keys away, never add them): ' + akEsc);
  ok(ak({desk: 'x', cls: 'write', ask: 'maybe'}, {name: 'x', cli: 'claude', meta: {kind: 'claude'}}) === 'select' && ak({desk: 'x', cls: '', ask: null}, {name: 'x', cli: 'claude', meta: {kind: 'claude'}}) === '', 'U6: no word, or a word the page does not know, keeps the fallback');
  TREE = {waiting: [
    {desk: 'run-3', run: 'live-1', cls: 'write', since: 0, ask: 'line', ask_line: "allow? type 'y' to allow, anything else declines:"},
    {desk: 'claude-2', run: null, cls: 'read', since: 0, ask: 'select', ask_line: 'Do you want to proceed? <b>1. Yes</b>'},
    {desk: 'desk-a', run: null, cls: 'write', since: 0, ask: 'secret', ask_line: '[sudo] password for u:'},
    {desk: 'stray', run: null, cls: 'write', since: 0, ask: 'yes', ask_line: "allow? type 'yes' to allow, anything else declines:"}], projects: [], other: [], ended: []};
  renderTree();
  const wl = i => (wkeys()[i] && wkeys()[i].querySelectorAll('.wline')[0]) || null;
  ok([0, 1, 2, 3].map(wkText).join(' | ') === 'y,n | Enter,Esc | Answer in the terminal | Answer in the terminal', 'U6: line = y / n, select = Enter / Esc, secret and yes = the terminal: ' + [0, 1, 2, 3].map(wkText).join(' | '));
  ok(wl(1) && wl(1).textContent === 'Do you want to proceed? <b>1. Yes</b>' && !wl(1).innerHTML && wl(0) && wl(0).textContent.startsWith('allow?') && wl(2) && wl(2).textContent === '[sudo] password for u:', 'U6: each row shows its ask line over the keys, as text');
  delete SENT_ASK['run-3'];   // review 1.2: y already answered this ask above; a new ask frees the row again
  renderTree();
  if (wkeys().length === 4) { await wkeys()[0].querySelectorAll('.wk')[1].onclick({}); await tick(10); }
  ok(JSON.stringify((LOG.posts.filter(q => q.path === '/api/keys').at(-1) || {}).body) === JSON.stringify({name: 'run-3', keys: ['n', 'enter'], ask_line: "allow? type 'y' to allow, anything else declines:"}), 'U6: n on a line word posts keys ["n","enter"] with the ask it answers: ' + JSON.stringify((LOG.posts.filter(q => q.path === '/api/keys').at(-1) || {}).body));
  // a client-built tree (an older /api/tree) takes ask, ask_line and cls from the /api/desks rows
  const cWait = FIX_DESKS.find(d => d.name === 'claude-2');
  if (cWait) Object.assign(cWait, {cls: 'write', ask: 'select', ask_line: 'Allow this edit? 1. Yes'});
  TREE = buildTree(); renderTree();
  const ci = TREE.waiting.findIndex(w => w.desk === 'claude-2');
  ok(ci >= 0 && wkText(ci) === 'Enter,Esc' && wl(ci) && wl(ci).textContent === 'Allow this edit? 1. Yes', 'U6: a client-built tree takes ask, ask_line and cls from the desk rows: ' + (ci >= 0 ? wkText(ci) : 'no row'));
  if (cWait) { delete cWait.cls; delete cWait.ask; delete cWait.ask_line; }
  TREE = buildTree(); renderTree();

  // ---- the header (21.6): dot + name + reach + Desktop + ⋯, rename lives in the menu ----
  ok(!$('renBtn'), 'no ✎ button in the header');
  $('moreBtn').onclick({stopPropagation() {}}); await tick(5);
  const menuTxt = $('menu').textContent;
  ok(!$('menu').classList.contains('hidden') && menuTxt.includes('Rename') && menuTxt.includes('Handoff') && menuTxt.includes('Details') && !menuTxt.includes('Terminal'), 'the ⋯ menu: rename, handoff, details — no Terminal row in user mode: ' + menuTxt);
  PROMPT_ANSWER = 'Renamed'; $('menu').querySelectorAll('.mi').find(b => b.textContent === 'Rename').onclick({stopPropagation() {}}); await tick(10);
  ok(LOG.posts.at(-1).path === '/api/alias' && LOG.posts.at(-1).body.alias === 'Renamed' && disp('desk-a') === 'Renamed' && $('wsName').textContent === 'Renamed', 'Rename from the menu posts /api/alias and updates the header');
  PROMPT_ANSWER = ''; await setAlias('desk-a', ''); ok(disp('desk-a') === 'desk-a', 'an empty name resets the alias');
  // a refused alias reverts (finding ui-network-7)
  const okAlias = await setAlias('refuse-me', 'Nope'); ok(okAlias === false && !aliases['refuse-me'], 'a refused POST /api/alias reverts the local alias');
  ok($('reach').textContent === 'live' && !$('reach').classList.contains('hidden'), 'reach chip for the default desk: ' + $('reach').textContent);

  // ---- pages (22.5): Settings is a tab on the canvas; versions ONLY there -----------------
  openPage('settings'); await tick(30);
  ok(ACTIVE.kind === 'page' && ACTIVE.id === 'settings' && !$('page').classList.contains('hidden') && $('lane').classList.contains('hidden'), 'Settings opens as a page tab; the lane hides');
  ok($('wsName').textContent === 'Settings' && $('reach').classList.contains('hidden'), 'the header shows the page title and hides the reach chip');
  const st = $('page').textContent;
  ok(st.includes('0.3.0') && st.includes('0.2.2') && st.includes('MetaBot · on Metaorcha'), 'Settings → About shows versions and the product line');
  ok(st.includes('apt-get install') && st.includes('metachat-gui') && !st.includes('enable --now fc-gui'), 'Settings → Desktop shows the install line naming metachat-gui');
  ok(st.includes('fc-gui'), 'display.unit from /api/meta is shown');
  ok(!st.includes('max_desks'), 'user mode: no raw config facts');
  ok(!!$('set-mode') && !!$('set-look') && !!$('set-accounts') && !!$('set-folders') && !!$('set-desktop') && !!$('set-about'), 'Settings has the six sections');
  const schemeBtns = $('set-look').querySelectorAll('.seg')[0].querySelectorAll('button');
  schemeBtns.find(b => b.textContent === 'Dark').onclick(); await tick(5);
  ok(localStorage.fcScheme === 'dark' && document.documentElement.className === 'dark', 'Appearance → Dark sets fcScheme and html.dark');
  $('set-look').querySelectorAll('.seg')[0].querySelectorAll('button').find(b => b.textContent === 'Auto').onclick(); await tick(5);
  ok(localStorage.fcScheme === 'auto' && document.documentElement.className === 'light', 'Appearance → Auto stores auto and follows the system (no matchMedia here → light)');
  $('set-mode').querySelectorAll('.seg')[0].querySelectorAll('button').find(b => b.textContent === 'Developer').onclick(); await tick(20);
  ok(UMODE === 'dev' && localStorage.fcMode === 'dev' && !document.body.classList.contains('user'), 'Settings → Developer persists fcMode');
  ok(abIds().includes('sdk') && abIds().includes('tools'), 'developer mode activity bar shows SDK and Tools');
  ok($('page').textContent.includes('max_desks') && $('page').textContent.includes('24') && $('page').textContent.includes('desk-a'), 'developer Settings lists the /api/meta config keys read-only');
  ok($('sbMode').textContent === 'developer', 'status line mode reads developer');
  // the back arrow returns to the chat
  $('page').querySelectorAll('.phead')[0].querySelectorAll('button')[0].onclick(); await tick(5);
  ok(ACTIVE.kind === 'chat' && !$('lane').classList.contains('hidden') && $('page').classList.contains('hidden'), 'the page head arrow returns to the chat lane');
  // a developer-only page tab closes when the mode drops to user
  openPage('sdk'); await tick(10); setUMode('user'); await tick(10);
  ok(!TABS.some(t => t.id === 'sdk') && !abIds().includes('sdk'), 'switching to user closes the SDK tab and hides it from the bar');
  setUMode('dev'); await tick(10);
  showTerm(); await tick(5);
  ok(!$('termPanel').classList.contains('hidden') && $('termPanel').classList.contains('pos-bottom') && $('termPanel').parent === $('chat'), 'developer: showTerm() opens the panel at the bottom of the chat column (default)');

  // ---- history + hygiene (kept) ---------------------------------------------------------
  setWS('desk-a'); await tick(5);
  const hist = msgs()[0];
  ok(hist && bodyOf(hist) === md("hi 'there' <b>"), 'history bubble rendered through md()/esc()');
  ok(esc("<a href='x'>&\\"") === '&lt;a href=&#39;x&#39;&gt;&amp;&quot;', 'esc escapes & < > \\" and the single quote');
  ok(cleanText('\\x1b[200~a\\x07b\\x1b[201~\\r\\nc\\td') === 'ab\\nc\\td', 'cleanText strips controls and the paste end sequence, keeps \\\\n \\\\t');
  ok(kids($('log')).some(c => c.classList.contains('tstamp')), 'a time divider heads the group');

  // ---- section 13: resize on open / switch / fit even when xterm's size did not change ----
  const rz = termFor('rz'); await until(() => rz.ws.sent.length >= 1);
  ok(rz.ws.sent.length >= 1 && isResize(rz.ws.sent[0]) && JSON.parse(rz.ws.sent[0].slice(1)).cols === 80, 'resize frame sent on ws open before any data: ' + JSON.stringify(rz.ws.sent[0]));
  await tick(10); const n0 = rz.ws.sent.length; setWS('rz'); await until(() => rz.ws.sent.length > n0);
  ok(rz.ws.sent.length > n0 && isResize(rz.ws.sent.at(-1)), 'resize frame sent on switch');
  const n1 = rz.ws.sent.length; fitCur(); ok(rz.ws.sent.length === n1 + 1 && isResize(rz.ws.sent.at(-1)), 'resize frame sent on fit');
  ok(rz.term.options.theme && rz.term.options.theme.background, 'the xterm theme is built from the tokens');
  setWS('desk-a'); await tick(5);
  // a paste is bound to its desk (finding ui-state-2): the write lands in desk-a even after switching
  const recA = TERMS['desk-a']; await until(() => recA && recA.ws && recA.ws.readyState === 1);
  recA.ws.readyState = 0; runCmd('echo bound', 'desk-a'); setWS('rz'); await tick(5); recA.ws.readyState = 1; await tick(200);
  ok(recA.ws.sent.some(s => typeof s === 'string' && s.includes('echo bound')) && !TERMS['rz'].ws.sent.some(s => typeof s === 'string' && s.includes('echo bound')), 'termPaste writes to the desk it was meant for, not the current one');
  setWS('desk-a'); await tick(5);
  // close codes: 4001/4004 dispose; other 4xxx are final + stale; a hidden panel does not reconnect (findings ui-state-8/11, ui-network-9/10)
  const d0 = DISPOSED; const z = termFor('zzz'); await tick(5); z.ws.onclose({ code: 4004 });
  ok(!TERMS['zzz'] && DISPOSED === d0 + 1, '4004 disposes the terminal and does not retry');
  const y = termFor('yyy'); await tick(5); const wsN = LOG.ws.length; y.ws.onclose({ code: 4008, reason: 'too many' }); await tick(1500);
  ok(TERMS['yyy'] && TERMS['yyy'].stale === true && LOG.ws.length === wsN, '4008 is final: marked stale, no new socket');
  const cur = currentTerm(); const wsN2 = LOG.ws.length; hideTerm(); cur.ws.onclose({ code: 1006 }); await tick(1500);
  ok(cur.stale === true && LOG.ws.length === wsN2, 'a close while the panel is hidden marks the terminal stale instead of reconnecting');
  showTerm(); await tick(20);
  ok(LOG.ws.length === wsN2 + 1 && cur.stale === false, 'showing the panel again reconnects the stale terminal once');

  // ---- section 15/18: the palette and the slash commands --------------------------------
  text.value = '/'; palette();
  ok(!$('palette').classList.contains('hidden') && heads()[0] === 'commands', "'/' opens the palette with a commands head");
  const keys = rows().map(r => r.querySelectorAll('.pk')[0].textContent);
  for (const k of ['/handoff', '/to', '/all', '/new', '/arm', '/start', '/rename', '/desks', '/run', '/a2a', '/mcp', '/desktop']) ok(keys.includes(k), 'palette lists ' + k);
  ok(keys.includes('/ping') && keys.includes('/plug') && heads().includes('Ping'), 'module commands listed after the built-ins under the module label (section 18)');
  ok(keys.filter(k => k === '/handoff').length === 1 && !rows().some(r => r.textContent.includes('tries to override')), 'a module cannot override a built-in');
  ok(!keys.some(k => /bad/.test(k)), 'a malformed module slash is dropped');
  ok(rows()[0].classList.contains('sel'), 'the top row is highlighted');
  text.onkeydown({ key: 'ArrowDown', preventDefault() {} });
  ok(rows()[1].classList.contains('sel') && !rows()[0].classList.contains('sel'), 'ArrowDown moves the highlight');
  text.onkeydown({ key: 'Escape', preventDefault() {} });
  ok($('palette').classList.contains('hidden') && text.placeholder.startsWith('Message desk-a'), 'Esc closes the palette and restores the placeholder: ' + text.placeholder);
  // step 2: the desk picker, ranked inbox > pane > idle > none, with the reach dot
  setWS('run-3'); await tick(5);
  text.value = '/to '; palette();
  const names = rows().map(r => r.querySelectorAll('.pk')[0].textContent);
  ok(heads()[0] === 'send to' && names.join(',') === 'desk-a,Second,stray', 'desk rows ranked inbox > pane > none, current desk excluded, aliases shown: ' + names.join(','));
  ok(rows()[0].querySelectorAll('.pd')[0].classList.contains('on') && rows()[1].querySelectorAll('.pd')[0].classList.contains('pane'), 'reach dots on the desk rows');
  ok(text.placeholder === 'pick above, then write', 'the palette asks for the pick');
  text.onkeydown({ key: 'Enter', shiftKey: false, preventDefault() {} });
  ok(text.value === '/to desk-a ', 'Enter picks the highlighted desk: ' + JSON.stringify(text.value));
  text.value = ''; palette(); ok(text.placeholder.startsWith('Message run-3'), 'placeholder restored after the palette closes (finding ui-state-10)');
  ok(quoteName('desk a') === '"desk a"' && quoteName('plain') === 'plain', 'desk names with spaces are quoted');
  // /to and /handoff post {text, target, kind}
  await slash('/to "Second" hi there'); await tick(5);
  let p = LOG.posts.at(-1);
  ok(p.path === '/api/send' && p.body.target === 'claude-2' && p.body.text === 'hi there' && p.body.kind === 'message', '/to resolves an alias and POSTs {text, target, kind:message}: ' + JSON.stringify(p.body));
  await slash('/handoff desk-a finish the tests'); await tick(5);
  p = LOG.posts.at(-1);
  ok(p.path === '/api/send' && p.body.target === 'desk-a' && p.body.text === 'finish the tests' && p.body.kind === 'handoff', '/handoff POSTs kind:handoff: ' + JSON.stringify(p.body));
  ok(WS === 'desk-a', 'a handoff switches to the receiving lane');
  // the composer submit path takes slash commands, once per Enter (finding ui-state-1)
  text.value = '/to run-3 via submit'; const s1 = $('f').onsubmit({ preventDefault() {} }); const s2 = $('f').onsubmit({ preventDefault() {} }); await s1; await s2; await tick(5);
  ok(LOG.posts.filter(q => q.path === '/api/send' && q.body.text === 'via submit').length === 1 && text.value === '', 'two Enters while a slash command is in flight send it once and clear the box');
  // /new opens the spawn sheet; /all needs a confirm and then posts per desk
  await slash('/new'); ok(!$('spawnSheet').classList.contains('hidden'), '/new opens the New desk sheet'); closeSheets();
  text.value = '/all '; palette();
  ok(heads()[0] === 'which project' && rows().some(r => r.querySelectorAll('.pk')[0].textContent === 'proj-one'), '/all lists the projects: ' + heads()[0]);
  const nSend = LOG.posts.filter(q => q.path === '/api/send').length;
  const r1 = await slash('/all proj-one ship it'); await tick(5);
  ok(r1 === 'keep' && LOG.posts.filter(q => q.path === '/api/send').length === nSend && !$('palette').classList.contains('hidden') && heads()[0].startsWith('send to 2 desks in proj-one'), '/all stops at a confirm row naming the count: ' + heads()[0]);
  rows()[0].fire('pointerup', {preventDefault() {}}); await tick(20);
  const allPosts = LOG.posts.filter(q => q.path === '/api/send').slice(nSend);
  ok(allPosts.length === 2 && allPosts.map(q => q.body.target).sort().join(',') === 'claude-2,desk-a' && allPosts.every(q => q.body.text === 'ship it'), 'the confirm posts one /api/send per reachable desk: ' + allPosts.map(q => q.body.target).join(','));
  ok(bodyOf(msgs().at(-1)).includes('sent to 2 desks'), 'and reports a result line per desk');
  // /rename with the 48-char cap, /desks, /run, /mcp, /desktop, /a2a
  setWS('desk-a'); await tick(5);
  await slash('/rename ' + 'x'.repeat(60));
  ok(aliases['desk-a'].length === 48 && LOG.posts.at(-1).path === '/api/alias' && LOG.posts.at(-1).body.name === 'desk-a' && LOG.posts.at(-1).body.alias.length === 48, '/rename POSTs /api/alias with a 48-char cap');
  await setAlias('desk-a', '');
  await slash('/desks');
  ok(bodyOf(msgs().at(-1)).includes('no CLI running') && bodyOf(msgs().at(-1)).includes('run-3'), '/desks lists desks with their reach: ' + bodyOf(msgs().at(-1)));
  await slash('/run summarise this');
  ok(MODE === 'run' && text.value === 'summarise this', '/run opens the Run composer filled');
  setMode('msg'); text.value = '';
  await slash('/mcp'); await tick(10);
  ok(ACTIVE.kind === 'page' && ACTIVE.id === 'connections', '/mcp opens the Connections page');
  await slash('/desktop'); await tick(10);
  ok(DOCK.id === 'desktop' && dockOpen() && !dock.classList.contains('hidden'), '/desktop opens the Desktop panel');
  closePanel(); setWS('desk-a'); await tick(5);
  await slash('/a2a'); await tick(20);
  ok(LOG.posts.at(-1).path === '/api/cli/read' && LOG.posts.at(-1).body.id === 'workflow.list', '/a2a reads workflow.list');
  ok(rows().map(r => r.querySelectorAll('.pk')[0].textContent).join(',') === 'wf-one,wf-two', '/a2a lists the workflows as palette rows: ' + rows().map(r => r.querySelectorAll('.pk')[0].textContent).join(','));
  await slash('/a2a wf-one'); await tick(10);
  ok(LOG.posts.at(-1).path === '/api/cli/desk' && LOG.posts.at(-1).body.id === 'workflow.run' && LOG.posts.at(-1).body.params.ref === 'wf-one', '/a2a <ref> runs workflow.run through /api/cli/desk');
  // module commands run through /api/send and /api/cli/desk
  setWS('desk-a'); await tick(5);
  await slash('/ping run-3 yo'); await tick(5);
  ok(LOG.posts.at(-1).path === '/api/send' && LOG.posts.at(-1).body.target === 'run-3' && LOG.posts.at(-1).body.text === 'yo' && LOG.posts.at(-1).body.kind === 'message', 'module send command POSTs /api/send: ' + JSON.stringify(LOG.posts.at(-1).body));
  await slash('/plug demo2'); await tick(10);
  ok(LOG.posts.at(-1).path === '/api/cli/desk' && LOG.posts.at(-1).body.id === 'plugin.new' && LOG.posts.at(-1).body.params.name === 'demo2', 'module catalog command POSTs /api/cli/desk: ' + JSON.stringify(LOG.posts.at(-1).body));
  setWS('desk-a'); await tick(5);
  text.value = '/ping '; palette();
  ok(heads()[0] === 'ping' && rows().length === 3, 'a module desk arg uses the desk picker: ' + heads()[0] + ' / ' + rows().length);
  text.value = ''; palette();
  // prefixes: > catalogue, # runs, @ desks
  text.value = '>marketplace'; palette(); ok(heads()[0] === 'catalogue' && rows().length === 2, "'>' searches the catalogue (label or id): " + rows().length);   // the two marketplace entries of 25.3 B7
  text.value = '#9c2e'; palette(); ok(heads()[0] === 'runs and receipts' && rows().length === 1, "'#' searches runs");
  text.value = '@sec'; palette(); ok(heads()[0] === 'desks' && rows()[0].querySelectorAll('.pk')[0].textContent === 'Second', "'@' searches desks by alias");
  text.value = ''; palette();
  // + menu
  $('plusBtn').onclick({stopPropagation() {}}); await tick(5);
  ok(!$('plusMenu').classList.contains('hidden') && $('plusMenu').querySelectorAll('.mi').map(b => b.textContent).join(',') === 'New desk,Run a task,Handoff…,Workflow…,Check a receipt,Desktop', '+ menu rows: ' + $('plusMenu').querySelectorAll('.mi').map(b => b.textContent).join(','));
  hideMenus();

  // ---- section 13/15: bubbles — via:desk as from → target, one chip at most, quiet rows ----
  setWS('desk-a'); await tick(5);
  add({ via: 'desk', time: Date.now() / 1000, from: 'claude-2', target: 'desk-a', message: 'took handoff: tests' });
  let b = msgs().at(-1);
  ok(b.querySelectorAll('.who')[0].textContent === 'Second → desk-a' && chipsOf(b).length === 0, "via:'desk' renders as from → target: " + b.querySelectorAll('.who')[0].textContent);
  add({ via: 'in', time: Date.now() / 1000 + 1, target: 'desk-a', message: 'for desk-a: typed one', typed: true, from: 'claude-2' });
  b = msgs().at(-1);
  ok(chipsOf(b).length === 1 && chipsOf(b)[0].textContent === 'typed', 'typed + from + for-prefix still gives exactly one chip: ' + chipsOf(b).map(c => c.textContent).join('|'));
  add({ via: 'alert', time: Date.now() / 1000 + 2, target: 'desk-a', message: 'plain inbound' });
  b = msgs().at(-1);
  ok(!b.textContent.includes('floor') && chipsOf(b).length === 0 && b.querySelectorAll('.acts').length === 0, "no 'floor' label, no action row until tapped (21.6)");
  b.fire('click', {target: b});
  const acts = b.querySelectorAll('.acts')[0];
  ok(acts && acts.querySelectorAll('button').map(x => x.textContent).join(',') === 'copy,type,run,reply' && $('log').querySelectorAll('.acts').length === 1, 'tap shows the quick actions on that message alone: ' + (acts && acts.querySelectorAll('button').map(x => x.textContent).join(',')));
  add({ via: 'desk', time: Date.now() / 1000 + 3, from: 'claude-2', target: 'desk-a', message: 'echo hi' });
  b = msgs().at(-1); b.fire('click', {target: b});
  ok($('log').querySelectorAll('.acts').length === 1, 'tapping another message moves the action row');
  b.querySelectorAll('.acts')[0].querySelectorAll('button')[3].onclick({ stopPropagation() {} });
  ok(text.value === '/to claude-2 ', 'reply fills /to <from> ready for the answer: ' + JSON.stringify(text.value));
  text.value = ''; palette();
  b.querySelectorAll('.acts')[0].querySelectorAll('button')[2].onclick({ stopPropagation() {} }); await tick(5);
  ok(currentTerm() && currentTerm().ws.sent.some(s => typeof s === 'string' && s.includes('echo hi') && s.endsWith('\\n')), 'run types the command with Enter');
  ok(kids($('log')).filter(c => c.classList.contains('tstamp')).length <= 3, 'one time divider per 5-minute group, not one per bubble: ' + kids($('log')).filter(c => c.classList.contains('tstamp')).length);
  // the empty canvas shows the mark, the line and the five starts of 25.4 U7
  setWS('zz-empty'); await tick(5);
  const empty = kids($('log')).find(c => c.classList.contains('empty'));
  ok(empty && empty.textContent.includes('It does the work. You keep the record.') && empty.querySelectorAll('.markwrap')[0].innerHTML.includes('mark-eye'), 'empty lane: mark + line');
  const starts = () => kids($('log')).find(c => c.classList.contains('empty')).querySelectorAll('.start');
  ok(starts().map(x => (x.querySelectorAll('b')[0] || {}).textContent).join(',') === 'Sign in an account,Desks side by side,Run with a receipt,Make a workflow,Extend' && empty.querySelectorAll('.pill').length === 0, 'U7: the empty canvas offers the five starts and nothing else: ' + starts().map(x => x.textContent).join(' | '));
  ok(starts().length === 5 && starts().every(x => x.tagName === 'BUTTON' && x.querySelectorAll('svg').length === 1), 'U7: each start is one button with one icon');
  if (starts().length === 5) {
    starts()[4].onclick(); await tick(10);
    ok(ACTIVE && ACTIVE.kind === 'page' && ACTIVE.id === 'plugins', 'U7: Extend opens the Extend page');
    closeTab('page', 'plugins'); setWS('zz-empty'); await tick(5);
    starts()[3].onclick(); await tick(10);
    ok(ACTIVE && ACTIVE.id === 'workflows' && document.activeElement === $('wfTaskText'), 'U7: Make a workflow opens Workflows with the task box focused');
    closeTab('page', 'workflows'); setWS('zz-empty'); await tick(5);
    starts()[1].onclick(); await tick(20);
    ok(!$('spawnSheet').classList.contains('hidden'), 'U7: Desks side by side opens the New desk sheet');
    closeSheets();
    starts()[2].onclick(); await tick(20);
    ok(MODE === 'run' && !$('runSheet').classList.contains('hidden'), 'U7: Run with a receipt opens Run with its settings');
    closeSheets(); setMode('msg');
    starts()[0].onclick(); await tick(10);
    ok(ACTIVE && ACTIVE.id === 'accounts', 'U7: Sign in an account opens Accounts');
    closeTab('page', 'accounts');
  }
  closeTab('chat', 'zz-empty'); setWS('desk-a'); await tick(5);

  // ---- section 15: optimistic send, dedupe (incl. the for-prefix and refill), failure, haptics ----
  const nMsg = msgs().length; LOG.vib.length = 0;
  const pr = sendMessage('hello2');
  let pend = msgs().at(-1);
  ok(msgs().length === nMsg + 1 && pend.classList.contains('pending'), 'optimistic bubble appears at once, pending');
  await pr; await tick(5);
  ok(!pend.classList.contains('pending') && !pend.classList.contains('failed') && LOG.posts.at(-1).body.cid, 'settled when the server answers; the POST carried a cid');
  ok(LOG.vib.includes(10), 'haptic on send');
  const cid = LOG.posts.at(-1).body.cid;
  add({ via: 'in', time: Date.now() / 1000, target: 'desk-a', message: 'hello2', cid });
  ok(msgs().length === nMsg + 1, 'the stream echo with the same cid is deduped');
  add({ via: 'in', time: Date.now() / 1000, target: 'desk-a', message: 'hello2', typed: true });
  ok(msgs().length === nMsg + 1, 'a stream echo without a cid (same target + text) is deduped too');
  setWS('claude-2'); await tick(5); const nC = msgs().length;
  await sendMessage('hello-inbox'); await tick(5);
  add({ via: 'in', time: Date.now() / 1000, message: 'for claude-2: hello-inbox' });
  ok(msgs().length === nC + 1, "an inbox echo prefixed 'for <desk>: ' settles the optimistic bubble (finding ui-state-3)");
  await refill(); await tick(5);
  ok(msgs().length === nC + 1, 'a refill after a stream drop does not re-add what this page sent (finding ui-state-4)');
  setWS('desk-a'); await tick(5);
  await sendMessage('fail-me'); await tick(5);
  ok(msgs().at(-2).classList.contains('failed') && bodyOf(msgs().at(-1)).includes('not armed'), 'a refused send marks the bubble failed and explains');
  // needs-you haptic: a desk flips to needs_you
  LOG.vib.length = 0; DESKS[2].needs_you = true; await fetchDesks(); await tick(5);
  ok(LOG.vib.includes(40), 'haptic when a desk starts needing you');
  DESKS[2].needs_you = false; await fetchDesks(); await tick(5);
  // working shimmer from the live run's desk
  ok(leaf('run-3').classList.contains('working') && leaf('run-3').querySelectorAll('.tst')[0].textContent === 'working', 'a desk with a live run is working (dot shimmer, one word)');
  setWS('run-3'); await tick(5);
  ok($('hmark').classList.contains('working'), 'the header mark blinks on a working desk');
  setWS('desk-a'); await tick(5);
  ok(!$('hmark').classList.contains('working'), 'and stops on an idle one');

  // ---- the update hint (21.2): build changes on a stream reopen → one sticky toast with Reload ----
  META_BUILD = 'b2'; const es0 = ES_LIST.at(-1); es0.onerror(); es0.onopen(); await tick(30);
  ok($('toast').textContent.startsWith('MetaBot updated — reload') && $('toast').querySelectorAll('button')[0].textContent === 'Reload', 'a new build toasts once with a Reload button: ' + $('toast').textContent);
  text.value = 'half-typed'; $('toast').querySelectorAll('button')[0].onclick(); await tick(5);
  ok(!LOG.reloaded && $('toast').textContent.includes('finish here'), 'Reload refuses while the composer has text');
  text.value = ''; META_BUILD = 'b1';
  // the stream watchdog: a closed EventSource is reopened with backoff
  const nES = ES_LIST.length; ES_LIST.at(-1).readyState = 2; streamWatchdog(); await tick(2600);
  ok(ES_LIST.length === nES + 1, 'a CLOSED EventSource is recreated by the watchdog');
  // a stream payload with no message, or non-JSON, is ignored (finding ui-network-5)
  const esN = ES_LIST.at(-1); const before = msgs().length; esN.onmessage({data: 'not json'}); esN.onmessage({data: JSON.stringify({build: 'x'})}); await tick(5);
  ok(msgs().length === before && !$('termErr').textContent, 'unknown stream shapes are ignored without an error');

  // ---- the right panel (22.5): open · expanded · closed; a run record; Esc closes ----
  openPage('runs'); await tick(40);
  ok(kids($('runsList')).filter(c => c.classList.contains('rrow')).length === 2, 'Runs page lists the runs');
  ok($('page').querySelectorAll('.today')[0] && $('page').querySelectorAll('.today')[0].querySelectorAll('b').map(x => x.textContent).join(',') === '1,0,1,0', 'the Today strip counts live · need you · verified · not verified: ' + $('page').querySelectorAll('.today')[0].querySelectorAll('b').map(x => x.textContent).join(','));
  // skeleton rows while the list loads
  $('runsList').textContent = ''; const lp = loadRuns();
  ok($('runsList').querySelectorAll('.skel').length === 3, 'skeleton rows while the runs list loads');
  await lp; await tick(5); ok($('runsList').querySelectorAll('.skel').length === 0, 'skeletons replaced by rows');
  kids($('runsList')).filter(c => c.classList.contains('rrow'))[1].onclick(); await tick(60);
  ok(DOCK.id === 'run:run-9c2e-example' && dockOpen() && !dock.classList.contains('hidden') && $('dockTitle').textContent.startsWith('Run '), 'a run row opens the run record in the right panel: ' + $('dockTitle').textContent);
  ok(!$('page').classList.contains('hidden'), 'the Runs page stays beside the panel');
  const vEl0 = $('dockBody').querySelectorAll('.verdict')[0];
  ok(vEl0 && vEl0.textContent === 'PC valid — tablet check unavailable' && vEl0.classList.contains('mix'), 'missing verifier (never loaded here): ' + (vEl0 && vEl0.textContent));
  ok($('dockBody').textContent.includes('vault: nothing stored'), 'missing verifier: nothing goes into the vault');
  ok($('dockBody').textContent.includes('claude · m') && $('dockBody').textContent.includes('login work') && $('dockBody').textContent.includes('spend $0.0123 (engine)'), 'run record line: engine · model · login · spend with its source');
  // the verifier throws: same honest line
  window.MBVerify = { verify: async () => { throw new Error('boom'); } };
  openRunRecord('run-9c2e-example'); await tick(60);
  let vEl = $('dockBody').querySelectorAll('.verdict')[0];
  ok(vEl.textContent === 'PC valid — tablet check unavailable', 'crashed verifier: ' + vEl.textContent);
  window.MBVerify = { verify: async bytes => ({ valid: true, verdict: 'valid', problem: null, checks: { schema: true, steps_root: true, steps_merkle_root: true, signature: true }, digest: 'cd'.repeat(32), run_id: 'run-9c2e-example', signer_did: 'did:orcha:system:validator', step_count: bytes.length > 0 ? 2 : 0 }) };
  openRunRecord('run-9c2e-example'); await tick(60);
  vEl = $('dockBody').querySelectorAll('.verdict')[0];
  ok(vEl.textContent === 'VALID — both checks agree' && vEl.classList.contains('ok'), 'run record verdict: ' + vEl.textContent);
  ok($('dockBody').textContent.includes('checked on this tablet with code this PC served'), 'honest tablet label shown');
  ok($('dockBody').textContent.includes('did:orcha:system:validator') && $('dockBody').textContent.includes('A6EHv/POEL4dcN0Y50vAmWfk1jCbpQ1fHdyGZBJVMbg='), 'signer and key shown');
  ok($('dockBody').textContent.includes('A SHA-256 hash chain over the steps, in order.'), 'check rows carry the allowed copy');
  const pre = all.filter(e => e.tagName === 'PRE' && e.textContent === '<script>p</script>');
  ok(pre.length >= 1 && pre.every(p => !p.innerHTML), 'prompt rendered with textContent (' + pre.length + ' render(s))');
  ok($('dockBody').textContent.includes('vault: not stored'), 'vault failure is reported, not thrown (no IndexedDB here)');
  PC_MODE = 'invalid'; openRunRecord('run-9c2e-example'); await tick(60);
  let v2 = $('dockBody').querySelectorAll('.verdict')[0];
  ok(v2.textContent.startsWith('CHECKS DISAGREE') && v2.classList.contains('mix'), 'PC invalid vs tablet valid: ' + v2.textContent);
  ok($('dockBody').querySelectorAll('.ccol')[0].querySelectorAll('.cv')[0].textContent.includes('signature'), 'pc_exit 1 names the failing check');
  PC_MODE = 'unreadable'; openRunRecord('run-9c2e-example'); await tick(60);
  ok($('dockBody').querySelectorAll('.ccol')[0].querySelectorAll('.cv')[0].textContent.includes('could not read the input'), 'pc_exit 2 says could not read the input');
  // a transport failure of the PC check is not a verdict (finding ui-network-2)
  PC_MODE = 'down'; openRunRecord('run-9c2e-example'); await tick(60);
  const v3 = $('dockBody').querySelectorAll('.verdict')[0];
  ok(v3.textContent === 'Tablet valid — PC check unavailable' && v3.classList.contains('mix'), 'PC unreachable: ' + v3.textContent);
  const retry = $('dockBody').querySelectorAll('.abtn').find(x => x.textContent === 'Retry PC check');
  ok(retry && !retry.classList.contains('hidden') && $('dockBody').querySelectorAll('.ccol')[0].querySelectorAll('.cv')[0].textContent.includes('PC check unavailable'), 'the PC column says unavailable and offers Retry PC check');
  PC_MODE = 'valid'; await retry.onclick(); await tick(20);
  ok($('dockBody').querySelectorAll('.verdict')[0].textContent === 'VALID — both checks agree' && retry.classList.contains('hidden'), 'Retry PC check re-runs the PC side alone and the headline agrees');
  copyReceipt('run-9c2e-example');
  ok(!$('copySheet').classList.contains('hidden') && $('copyTitle').textContent === 'Receipt run-9c2e-example' && $('copyList').children[0].textContent.startsWith('copy all ('), 'copy receipt opens the copy sheet with a copy-all row, sentence case');
  closeSheets();
  openRunRecord('live-1'); await tick(10);
  ok($('dockBody').textContent.includes('The run is live'), 'a live run shows the live card');
  setDockState('expanded'); ok(dock.classList.contains('wide') && $('page').classList.contains('hidden') && $('lane').classList.contains('hidden'), 'expand hides the canvas');
  setDockState('open'); ok(!dock.classList.contains('wide') && !$('page').classList.contains('hidden'), 'back beside the canvas');
  docFire('keydown', {key: 'Escape'}); await tick(5);
  ok(!dockOpen() && dock.classList.contains('hidden') && JSON.parse(localStorage.fcDock).state === 'closed', 'Esc closes the panel and the state persists');
  ok(!/"state":"min"/.test(localStorage.fcDock), 'no minimised state remains');

  // ---- the terminal drawer (21.2): bottom (default) → right → float → left; clamped floats ----
  setTermPos('right'); ok($('main').children.indexOf($('termPanel')) > $('main').children.indexOf($('chat')) && JSON.parse(localStorage.fcDock).term.pos === 'right', 'terminal docks right');
  setTermPos('float', 5000, 5000); ok($('termPanel').classList.contains('float') && parseInt($('termPanel').style['--fx']) <= 1400 - 300 && parseInt($('termPanel').style['--fy']) <= 876 - 60, 'a floating terminal is clamped to the viewport: ' + $('termPanel').style['--fx'] + ' ' + $('termPanel').style['--fy']);
  setTermPos('left'); ok($('main').children.indexOf($('termPanel')) < $('main').children.indexOf($('chat')), 'terminal docks left');
  setTermPos('bottom'); ok($('termPanel').parent === $('chat') && $('termPanel').classList.contains('pos-bottom'), 'terminal back at the bottom');
  const hd = $('termHead'); const pe = (x, y) => ({ clientX: x, clientY: y, pointerId: 1, target: hd, preventDefault() {} });
  hd.fire('pointerdown', pe(700, 300)); hd.fire('pointermove', pe(600, 300)); hd.fire('pointermove', pe(40, 300)); hd.fire('pointerup', pe(40, 300));
  ok(JSON.parse(localStorage.fcDock).term.pos === 'left', 'dragging the terminal header to the left edge docks it left');
  hd.fire('pointerdown', pe(700, 300)); hd.fire('pointermove', pe(700, 860)); hd.fire('pointerup', pe(700, 860));
  ok(JSON.parse(localStorage.fcDock).term.pos === 'bottom', 'dragging to the bottom edge docks it bottom');
  $('tlDock').onclick(); ok(JSON.parse(localStorage.fcDock).term.pos === 'right', 'the move button cycles bottom → right'); setTermPos('bottom');

  // ---- catalogue pages + panel: read output as text; desk command opens a desk (module office: plugins is the Extend page now) ----
  openPage('office'); await tick(10);
  const listBtn = $('page').querySelectorAll('.abtn').find(x => x.textContent === 'List');
  listBtn.onclick(); await tick(10);
  ok(DOCK.id === 'cat:plugin.list' && dockOpen() && $('dockTitle').textContent === 'List plugins', 'a catalogue item opens its form in the right panel');
  const go = $('dockBody').querySelectorAll('.primary')[0]; ok(go.textContent === 'Read', 'the read button is sentence case: ' + go.textContent);
  await go.onclick(); await tick(20);
  const outPre = $('dockBody').querySelectorAll('.out')[0];
  ok(outPre && outPre.textContent === "plugin-a\\n<b>&'x\\n" && !outPre.innerHTML, 'read output rendered with textContent');
  $('page').querySelectorAll('.abtn').find(x => x.textContent === 'New').onclick(); await tick(5);
  ok($('dockBody').querySelectorAll('.cmd')[0].textContent === '$ metabot plugin new demo', 'argv preview: ' + $('dockBody').querySelectorAll('.cmd')[0].textContent);
  await $('dockBody').querySelectorAll('.primary')[0].onclick(); await tick(20);
  ok(WS === 'plugin-new' && !$('termPanel').classList.contains('hidden'), 'desk command switched the terminal to the new desk: ' + WS);
  // the same in user mode: a quiet "equals" line, and the desk opens as a sheet with Allow / y
  setUMode('user'); await tick(5);
  ok($('termPanel').classList.contains('hidden'), 'switching to user mode hides the terminal panel');
  ok(!dockOpen() || DOCK.id !== 'sdk', 'no SDK panel survives into user mode');
  openPanel('cat:plugin.new', 'New plugin', body => catalogForm(body, CATALOG['plugin.new'], {params_ui: {name: {default: 'demo'}}})); await tick(5);
  ok($('dockBody').querySelectorAll('.cmd').length === 0 && $('dockBody').querySelectorAll('.equals')[0].textContent === 'equals metabot plugin new demo', 'user mode: no raw argv block, a quiet equals line');
  await $('dockBody').querySelectorAll('.primary')[0].onclick(); await tick(20);
  ok(!$('deskSheet').classList.contains('hidden') && $('termPanel').classList.contains('hidden'), 'user mode: the desk opens as a sheet, not the terminal panel');
  ok($('sheetTerm').children.some(c => c.classList.contains('termwrap') && c.classList.contains('live')), 'the sheet shows the live prompt');
  ok(!$('sheetEquals').textContent.includes('equals') && $('sheetEquals').textContent.length > 0, 'user mode: the sheet says the desk is asking, never its argv (21.6, RV-H2): ' + $('sheetEquals').textContent);
  UMODE = 'dev'; openDeskSheet('plugin-new'); ok($('sheetEquals').textContent.startsWith('equals metabot plugin new'), 'developer mode: the sheet shows what the desk equals'); UMODE = 'user'; openDeskSheet('plugin-new'); await tick(5);
  const keyTexts0 = $('sheetKeys').querySelectorAll('button').map(x => x.textContent);
  ok(!keyTexts0.includes('Allow') && !keyTexts0.includes('y') && keyTexts0.includes('^C'), 'review 1.2: a desk whose class nobody has read offers no one-tap allow: ' + keyTexts0.join(','));
  TERMS['plugin-new'].term.buffer.active.getLine = () => ({ translateToString: () => 'Create plugin demo here? [y/N]', isWrapped: false });
  openDeskSheet('plugin-new'); await tick(10);
  const keyTexts = $('sheetKeys').querySelectorAll('button').map(x => x.textContent);
  ok(keyTexts.includes('Allow') && keyTexts.includes('y') && keyTexts.includes('n') && keyTexts.includes('^C'), 'a write prompt read off the screen: Allow / y / n / ^C keys: ' + keyTexts.join(','));
  const sentBefore = TERMS['plugin-new'].ws.sent.length; $('sheetKeys').querySelectorAll('button').find(x => x.textContent === 'y').onclick();
  ok(TERMS['plugin-new'].ws.sent.at(-1) === 'y\\r' && TERMS['plugin-new'].ws.sent.length === sentBefore + 1, 'y sends y + Enter to that desk');
  // a destructive prompt takes only a typed yes
  TERMS['plugin-new'].term.buffer.active.getLine = () => ({ translateToString: () => 'Allow rm -rf ./build? (y/n)', isWrapped: false });
  openDeskSheet('plugin-new'); await tick(10);
  ok($('sheetCls').textContent.includes('destructive') && $('sheetKeys').querySelectorAll('button').map(x => x.textContent).includes('Send yes') && !$('sheetKeys').querySelectorAll('button').map(x => x.textContent).includes('Allow'), 'a destructive prompt is classed and offers a typed yes instead of Allow');
  const n2 = TERMS['plugin-new'].ws.sent.length; $('sheetKeys').querySelectorAll('button').find(x => x.textContent === 'Send yes').onclick();
  ok(TERMS['plugin-new'].ws.sent.length === n2, 'Send yes without the word typed sends nothing');
  $('sheetKeys').querySelectorAll('input')[0].value = 'yes'; $('sheetKeys').querySelectorAll('button').find(x => x.textContent === 'Send yes').onclick();
  ok(TERMS['plugin-new'].ws.sent.at(-1) === 'yes\\r', 'a typed yes sends yes + Enter');
  TERMS['plugin-new'].term.buffer.active.getLine = () => null;
  closeSheets();
  ok($('sheetTerm').children.length === 0 && $('term').children.some(c => c === TERMS['plugin-new'].el), 'closing the sheet parks the terminal back under #term');
  // a hidden sheet terminal does not reconnect (finding ui-network-9)
  const wsN3 = LOG.ws.length; TERMS['plugin-new'].ws.onclose({code: 1006}); await tick(1500);
  ok(LOG.ws.length === wsN3 && TERMS['plugin-new'].stale === true, 'a closed socket behind a closed sheet stays stale: no reconnect, no toast loop');
  setUMode('dev'); await tick(5);

  // ---- composer: Run mode, the summary pill, chips in the sheet, POST body; one submit at a time ----
  setMode('run'); await tick(30);
  ok(!$('runPill').classList.contains('hidden') && $('runPill').textContent.includes('claude') && $('runPill').textContent.includes('read-only'), 'Run mode shows the summary pill: ' + $('runPill').textContent);
  ok($('f').classList.contains('run') && text.placeholder === 'What should the run do?', '11-09 tablet: in Run mode the settings pill takes its own line, the task box keeps the width; the placeholder is plain words: ' + text.placeholder);
  const engVals = () => [...$('chipEngine').options].map(o => o.value).join(',');
  ok(engVals() === 'claude,cursor,openai,echo', 'U5: the engine chip lists /api/extend engines, built-in and bridges: ' + engVals());
  ok($('chipEngine').options[0].textContent === 'claude' && $('chipEngine').options[2].textContent === 'openai · local · works offline' && $('chipEngine').options[3].textContent === 'echo · bridge · local · works offline', 'U5: local engines are marked local · works offline: ' + [...$('chipEngine').options].map(o => o.textContent).join(' | '));
  ok([...$('chipAccount').options].map(o => o.value).join(',') === ',work', 'login chip lists MetaBot logins');
  ok([...$('chipProject').options].some(o => o.value === '/work/u/proj-one'), 'folder chip lists project paths');
  $('chipEngine').value = 'cursor'; accountChipVisibility(); ok($('chipAccount').classList.contains('hidden'), 'login chip hidden for cursor');
  $('chipEngine').value = 'claude'; accountChipVisibility(); $('chipAccount').value = 'work'; $('chipProject').value = '/work/u/proj-one'; $('chipCost').value = '1.50';
  renderRunPill(); ok($('runEquals').textContent.startsWith('equals metabot ask -e claude --login work'), 'the run sheet shows what it equals: ' + $('runEquals').textContent);
  text.value = 'summarise the repo'; const e1 = $('f').onsubmit({ preventDefault() {} }); const e2 = $('f').onsubmit({ preventDefault() {} }); await e1; await e2; await tick(20);
  ok(LOG.posts.filter(q => q.path === '/api/run').length === 1, 'two Enters in Run mode post one run (finding ui-state-1)');
  const run = LOG.posts.find(q => q.path === '/api/run');
  ok(run && run.body.task === 'summarise the repo' && run.body.engine === 'claude' && run.body.account === 'work' && run.body.cwd === '/work/u/proj-one' && run.body.mode === 'read-only' && run.body.max_cost === '1.50', 'POST /api/run body: ' + JSON.stringify(run && run.body));
  ok(WS === 'run-4' && text.value === '', 'run switched to its desk and cleared the box: ' + WS);
  ok(bodyOf(msgs().at(-1)).includes('equals ') && bodyOf(msgs().at(-1)).includes('metabot ask'), 'the run bubble says what it equals');
  $('chipCost').value = 'abc'; text.value = 'x'; await $('f').onsubmit({ preventDefault() {} }); ok(LOG.posts.filter(q => q.path === '/api/run').length === 1 && text.value === 'x', 'bad max_cost is refused client-side and the task stays in the box');
  $('chipCost').value = '';
  { const keepE = $('chipEngine').value; $('chipEngine').value = 'openai'; accountChipVisibility(); renderRunPill(); text.value = 'count the files';
    await $('f').onsubmit({ preventDefault() {} }); await tick(20);
    ok(!bodyOf(msgs().at(-1)).includes('account ignored'), 'a run on an engine with no login never says an account was ignored (none was sent): ' + bodyOf(msgs().at(-1)));
    $('chipEngine').value = keepE; accountChipVisibility(); renderRunPill(); }
  // U5: a local engine costs $0 (local); Suggest fills the engine from metabot suggest and says why
  $('chipEngine').value = 'echo'; renderRunPill();
  ok($('runPill').textContent.includes('$0 (local)'), 'U5: a local engine reads $0 (local): ' + $('runPill').textContent);
  $('chipEngine').value = 'claude'; renderRunPill();
  ok(!$('runPill').textContent.includes('$0'), 'U5: a cloud engine does not: ' + $('runPill').textContent);
  const sgReads = () => LOG.posts.filter(q => q.path === '/api/cli/read' && q.body.id === 'suggest');
  text.value = ''; if ($('chipSuggest')) { await $('chipSuggest').onclick(); await tick(5); }
  ok(sgReads().length === 0 && $('runWhy') && $('runWhy').textContent.includes('write the task'), 'U5: Suggest with no task asks for one and reads nothing');
  text.value = 'review the parser'; if ($('chipSuggest')) { await $('chipSuggest').onclick(); await tick(10); }
  ok(sgReads().length === 1 && sgReads()[0].body.params.task === 'review the parser' && $('chipEngine').value === 'cursor' && $('runWhy').textContent.includes('review') && $('runWhy').textContent.includes('the task says'), 'U5: Suggest fills cursor for a review and says why: ' + ($('runWhy') ? $('runWhy').textContent : ''));
  ok($('runPill').textContent.startsWith('cursor'), 'U5: the pill follows the suggested engine: ' + $('runPill').textContent);
  text.value = 'keep it private'; if ($('chipSuggest')) { await $('chipSuggest').onclick(); await tick(10); }
  ok($('chipEngine').value === 'cursor' && $('runWhy').textContent.includes('no engine for the private role'), 'U5: no engine suits: the chip stays and the note says why: ' + ($('runWhy') ? $('runWhy').textContent : ''));
  $('chipEngine').value = 'claude'; accountChipVisibility(); renderRunPill(); text.value = ''; if ($('runWhy')) $('runWhy').textContent = '';
  // an older PC without GET /api/extend: engines.list still fills the chip
  XT_DOWN = true; ENGINES = null; EXTEND = null; ENGINE_INFO = {};
  await loadRunChips(); await tick(5);
  ok(engVals() === 'claude,cursor,openai', 'U5: without /api/extend the chip falls back to engines.list: ' + engVals());
  XT_DOWN = false; ENGINES = null; EXTEND = null; await loadRunChips(); await tick(5);
  ok(engVals() === 'claude,cursor,openai,echo', 'U5: and back to /api/extend when it answers');
  $('chipEngine').value = 'claude'; renderRunPill();
  setMode('msg'); text.value = ''; await sendMessage('hello'); ok(LOG.posts.at(-1).path === '/api/send' && LOG.posts.at(-1).body.target === 'run-4', 'message mode posts to /api/send with the current desk');

  // ---- New desk sheet (21.3): kinds, inline errors, spawn guards ----
  await openSpawnSheet({cwd: '/work/u/proj-one'}); await tick(10);
  ok(!$('spawnSheet').classList.contains('hidden') && $('spawnCwd').value === '/work/u/proj-one', 'the New desk sheet opens with the folder preselected');
  ok($('spawnKinds').querySelectorAll('.spawnrow').map(r => r.dataset.kind).join(',') === 'claude,cursor,shell,run', 'four kinds: claude, cursor, shell, run');
  $('spawnLabel').value = 'Bad Label'; await $('spawnGo').onclick(); await tick(5);
  ok($('spawnErr').textContent.startsWith('label:') && !LOG.posts.some(q => q.path === '/api/spawn'), 'a bad label is refused inline, no POST');
  $('spawnLabel').value = 'nice'; $('spawnPath').value = '../x'; await $('spawnGo').onclick(); ok($('spawnErr').textContent.startsWith('path:'), 'a relative path is refused inline');
  $('spawnPath').value = ''; await $('spawnGo').onclick(); await tick(20);
  ok(LOG.posts.at(-1).path === '/api/spawn' && LOG.posts.at(-1).body.label === 'nice' && LOG.posts.at(-1).body.cwd === '/work/u/proj-one' && $('spawnSheet').classList.contains('hidden'), 'Open desk POSTs /api/spawn and closes the sheet: ' + JSON.stringify(LOG.posts.at(-1).body));
  // the keyboard covers Open desk while a field is being typed: Enter in the label or path field opens the desk
  for (const f of ['spawnLabel', 'spawnPath']) {
    await openSpawnSheet({cwd: '/work/u/proj-one'}); await tick(10);
    $('spawnLabel').value = 'enter-' + f.slice(5).toLowerCase(); const nS = LOG.posts.filter(q => q.path === '/api/spawn').length;
    let stopped = false; await (typeof $(f).onkeydown === 'function' && $(f).onkeydown({key: 'Enter', isComposing: false, preventDefault() { stopped = true; }})); await tick(20);
    const sp = LOG.posts.filter(q => q.path === '/api/spawn');
    ok(sp.length === nS + 1 && sp.at(-1).body.label === 'enter-' + f.slice(5).toLowerCase() && stopped && $('spawnSheet').classList.contains('hidden'), 'Enter in ' + f + ' opens the desk like the button: ' + JSON.stringify(sp.at(-1) && sp.at(-1).body));
  }
  await openSpawnSheet({cwd: '/work/u/proj-one'}); await tick(10); $('spawnLabel').value = 'ime-x'; const nI = LOG.posts.filter(q => q.path === '/api/spawn').length;
  await (typeof $('spawnLabel').onkeydown === 'function' && $('spawnLabel').onkeydown({key: 'Enter', isComposing: true, preventDefault() {}})); await tick(10);
  ok(LOG.posts.filter(q => q.path === '/api/spawn').length === nI, 'Enter that ends a keyboard composition does not open a desk');
  { const nD = LOG.posts.filter(q => q.path === '/api/spawn').length; $('spawnLabel').value = 'twice';
    const a = $('spawnLabel').onkeydown({key: 'Enter', preventDefault() {}}); const b = $('spawnLabel').onkeydown({key: 'Enter', preventDefault() {}}); await a; await b; await tick(20);
    ok(LOG.posts.filter(q => q.path === '/api/spawn').length === nD + 1, 'two quick Enters open one desk, not two'); }
  closeSheets && closeSheets();
  // ---- 25.4 U1: Sign in spawns {kind, account, signin:true}; the card follows the desk's signin row; nothing is typed on a timer ----
  const spawns = () => LOG.posts.filter(q => q.path === '/api/spawn');
  const keyPosts = () => LOG.posts.filter(q => q.path === '/api/keys');
  // a double tap on Sign in never spawns a second desk (finding ui-state-5)
  const nSp = spawns().length, nKeys0 = keyPosts().length;
  const sA = signInDesk('claude', 'work'); const sB = signInDesk('claude', 'work'); await sA; await sB; await tick(10);
  ok(spawns().length === nSp + 1, 'a double tap on Sign in posts one spawn: ' + (spawns().length - nSp));
  ok(JSON.stringify(spawns().at(-1).body) === JSON.stringify({kind: 'claude', account: 'work', signin: true}), 'U1: Sign in posts {kind, account, signin:true} and nothing else: ' + JSON.stringify(spawns().at(-1).body));
  ok(DOCK.id === 'signin' && dockOpen() && $('dockTitle').textContent.includes('work'), 'U1: the sign-in card opens in the right panel: ' + $('dockTitle').textContent);
  ok(!TERMS['signin-claude-work'] && WS !== 'signin-claude-work' && keyPosts().length === nKeys0, 'U1: no terminal, no desk switch, no key: the page types nothing into the sign-in desk');
  // a second Sign in on that account: 409 already signing in; no spawn under another name; the card of the desk that signs in
  const nSp2 = spawns().length; closePanel();
  await signInDesk('claude', 'work'); await tick(10);
  ok(spawns().length === nSp2 + 1 && spawns().at(-1).body.signin === true && DOCK.id === 'signin' && SIGNIN.name === 'signin-claude-work', 'U1: a 409 "already signing in" falls back to nothing and opens the card of that desk');
  const sinBtn = t => $('dockBody').querySelectorAll('button').find(b => b.textContent === t);
  const sinA = () => $('dockBody').querySelectorAll('a');
  const sinRow = FIX_DESKS.find(d => d.name === 'signin-claude-work');
  ok($('dockBody').textContent.includes('Enter keeps Dark') && !!sinBtn('Continue'), 'U1 style: "Enter keeps Dark" [Continue]');
  if (sinBtn('Continue')) { await sinBtn('Continue').onclick(); await tick(10); }
  ok(keyPosts().length === nKeys0 + 1 && JSON.stringify(keyPosts().at(-1).body) === JSON.stringify({name: 'signin-claude-work', keys: ['enter']}), 'U1: Continue posts {name, keys:["enter"]}: ' + JSON.stringify((keyPosts().at(-1) || {}).body));
  sinRow.signin.step = 'method'; await signinRefresh(); await tick(5);
  ok($('dockBody').textContent.includes('Use your Claude subscription') && !!sinBtn('Continue') && !$('dockBody').textContent.includes('Enter keeps Dark'), 'U1 method: "Use your Claude subscription" [Continue]');
  sinRow.signin.step = 'code'; sinRow.signin.url = 'https://claude.ai/oauth/authorize?code=true&client_id=c1&state=s1'; await signinRefresh(); await tick(5);
  ok(sinA().length === 1 && sinA()[0].attrs.href === sinRow.signin.url && sinA()[0].attrs.target === '_blank' && sinA()[0].attrs.rel === 'noopener noreferrer' && sinA()[0].textContent === 'Open sign-in page', 'U1 code: Open sign-in page is a link the browser opens (target=_blank, rel=noopener noreferrer)');
  ok($('dockBody').textContent.includes('Open the sign-in page, approve, paste the code it shows') && !!sinBtn('Copy link') && !!sinBtn('Send code') && $('dockBody').querySelectorAll('input').length === 1, 'U1 code: the line, Copy link, the code field and Send code');
  if (sinBtn('Copy link')) sinBtn('Copy link').onclick();
  ok($('toast').textContent.startsWith('copied: https://claude.ai/'), 'U1: Copy link copies the server link: ' + $('toast').textContent);
  const nKeys1 = keyPosts().length;
  if ($('signinCode')) { $('signinCode').value = '   '; await sinBtn('Send code').onclick(); await tick(5); }
  ok(keyPosts().length === nKeys1, 'U1: an empty code sends nothing');
  for (const bad of ['ab\u0007cd', 'ab\u009bcd']) { if ($('signinCode')) { $('signinCode').value = bad; await sinBtn('Send code').onclick(); await tick(5); } }
  ok(keyPosts().length === nKeys1 && $('toast').textContent === 'that is not a sign-in code', 'U1: a code with a control character (C0 or C1, as the server refuses) sends nothing');
  if ($('signinCode')) { $('signinCode').value = '  Ab12#cd-34  '; await sinBtn('Send code').onclick(); await tick(10); }
  ok(JSON.stringify((keyPosts().at(-1) || {}).body) === JSON.stringify({name: 'signin-claude-work', text: 'Ab12#cd-34'}), 'U1: Send code posts {name, text}, trimmed: ' + JSON.stringify((keyPosts().at(-1) || {}).body));
  if ($('signinCode')) { $('signinCode').value = 'half'; await signinRefresh(); await tick(5); }
  ok($('signinCode') && $('signinCode').value === 'half' && $('dockBody').querySelectorAll('input')[0] === $('signinCode'), 'U1: a poll that brings no new step keeps what is typed in the code field');
  if ($('signinCode')) { $('signinCode').value = 'half-typed'; drawSignin($('dockBody')); }
  ok($('signinCode') && $('signinCode').value === 'half-typed', 'review 1.2: a redraw of the card (a mode switch, a reopen) keeps a half-typed code: "' + ($('signinCode') || {}).value + '"');
  if ($('signinCode')) { $('signinCode').value = ''; drawSignin($('dockBody')); }
  sinRow.signin.url = 'https://claude.ai.evil.example/x'; await signinRefresh(); await tick(5);
  ok(sinA().length === 0 && !sinBtn('Copy link'), 'U1: a link off the five hosts is not shown');
  sinRow.signin.url = 'http://claude.ai/x'; await signinRefresh(); await tick(5);
  ok(sinA().length === 0, 'U1: a plain http link is not shown');
  sinRow.signin.url = null;
  sinRow.signin.step = 'success'; await signinRefresh(); await tick(5);
  ok(!!sinBtn('Continue') && $('dockBody').querySelectorAll('.sinStep').length === 1, 'U1 success: one line [Continue]');
  sinRow.signin.step = 'trust'; await signinRefresh(); await tick(5);
  ok($('dockBody').textContent.includes('The sign-in folder is empty') && !!sinBtn('Trust'), 'U1 trust: "The sign-in folder is empty" [Trust]');
  if (sinBtn('Trust')) { await sinBtn('Trust').onclick(); await tick(10); }
  ok(JSON.stringify((keyPosts().at(-1) || {}).body) === JSON.stringify({name: 'signin-claude-work', keys: ['enter']}), 'U1: Trust posts ["enter"]');
  sinRow.signin.step = 'ready'; await signinRefresh(); await tick(5);
  ok($('dockBody').textContent.includes('signed in') && $('dockBody').querySelectorAll('.sinOk')[0] && $('dockBody').querySelectorAll('.sinOk')[0].querySelectorAll('svg').length === 1 && !!sinBtn('Open a desk on it') && !!sinBtn('Close the sign-in desk'), 'U1 ready: [check] signed in [Open a desk on it] [Close the sign-in desk]');
  if (sinBtn('Open a desk on it')) { sinBtn('Open a desk on it').onclick(); await tick(20); }
  ok(!$('spawnSheet').classList.contains('hidden') && SPAWN.kind === 'claude' && $('spawnAccount').value === 'work', 'U1: Open a desk on it opens the New desk sheet on that account');
  closeSheets();
  if (sinBtn('Close the sign-in desk')) { await sinBtn('Close the sign-in desk').onclick(); await tick(10); }
  ok(LOG.posts.at(-1).path === '/api/kill' && LOG.posts.at(-1).body.name === 'signin-claude-work' && !dockOpen() && !FIX_DESKS.some(d => d.name === 'signin-claude-work'), 'U1: Close the sign-in desk kills that desk and closes the card');
  // Cursor: Accounts → Sign in on a folder → the link step
  openPage('accounts'); await tick(30);
  const altRow = $('page').querySelectorAll('.acct').find(r => r.textContent.startsWith('alt'));
  const curBtn = altRow && altRow.querySelectorAll('button').find(b => b.textContent === 'Sign in');
  if (curBtn) { await curBtn.onclick({currentTarget: curBtn}); await tick(20); }
  ok(JSON.stringify(spawns().at(-1).body) === JSON.stringify({kind: 'cursor', account: 'alt', signin: true}), 'U1: Accounts → Sign in on a Cursor folder posts {kind:"cursor", account, signin:true}');
  ok(sinA().length === 1 && sinA()[0].attrs.href.startsWith('https://cursor.com/') && !!sinBtn('Copy link') && $('dockBody').querySelectorAll('input').length === 0, 'U1 cursor link: Open sign-in page and Copy link, no code field');
  const curRow = FIX_DESKS.find(d => d.name === 'signin-cursor-alt');
  if (curRow) { curRow.signin.step = 'success'; await signinRefresh(); await tick(5); }
  ok($('dockBody').textContent.includes('signed in') && !!sinBtn('Close the sign-in desk'), 'U1: a Cursor success reads signed in');
  if (curRow) { FIX_DESKS.splice(FIX_DESKS.indexOf(curRow), 1); await signinRefresh(); await tick(5); }
  ok($('dockBody').textContent.includes('signed in') && !sinBtn('Close the sign-in desk') && !!sinBtn('Open a desk on it'), 'U1: cursor-agent ends after its success: the card still reads signed in');
  // the CLI exited (25.3 B1 as built: step "ended", exit N): the sign-in ended, Sign in again, Show the desk
  closePanel(); await signInDesk('cursor', 'alt'); await tick(20);
  const endRow = FIX_DESKS.find(d => d.name === 'signin-cursor-alt');
  if (endRow) { Object.assign(endRow.signin, {step: 'ended', exit: 1, url: null}); await signinRefresh(); await tick(5); }
  ok($('dockBody').textContent.includes('The sign-in ended') && !!sinBtn('Sign in again') && sinBtn('Sign in again').classList.contains('primary') && !!sinBtn('Show the desk') && sinA().length === 0
    && $('dockBody').querySelectorAll('button').filter(b => b.textContent === 'Show the desk').length === 1, 'U1 ended: "The sign-in ended" [Sign in again] [Show the desk]: ' + $('dockBody').textContent);
  const nSpE = spawns().length;
  if (sinBtn('Sign in again')) { await sinBtn('Sign in again').onclick({}); await tick(20); }
  ok((LOG.posts.filter(q => q.path === '/api/kill').at(-1) || {body: {}}).body.name === 'signin-cursor-alt' && spawns().length === nSpE + 1 && JSON.stringify(spawns().at(-1).body) === JSON.stringify({kind: 'cursor', account: 'alt', signin: true}) && sinA().length === 1,
    'U1: Sign in again closes the ended desk (a shell by then) and starts a new sign-in on that account');
  // cursor-agent exits right after its success: an exit 0 with the account now signed in reads signed in
  const endRow2 = FIX_DESKS.find(d => d.name === 'signin-cursor-alt');
  ACCT.cursor[0].ready = true;
  if (endRow2) { Object.assign(endRow2.signin, {step: 'ended', exit: 0, url: null}); await signinRefresh(); await tick(20); }
  ok($('dockBody').textContent.includes('signed in') && !$('dockBody').textContent.includes('The sign-in ended') && !!sinBtn('Close the sign-in desk'), 'U1: an exit 0 with the account signed in reads signed in');
  ACCT.cursor[0].ready = false;
  if (sinBtn('Close the sign-in desk')) { await sinBtn('Close the sign-in desk').onclick(); await tick(10); }
  // review 1.2: an ended Cursor sign-in shows no main button until its exit is known (an exit 0 may be signed in)
  closePanel(); await signInDesk('cursor', 'alt'); await tick(20);
  const endRow3 = FIX_DESKS.find(d => d.name === 'signin-cursor-alt');
  if (endRow3) {
    Object.assign(endRow3.signin, {step: 'ended', url: null}); delete endRow3.signin.exit; await fetchDesks(); drawSignin($('dockBody')); await tick(20);
    ok($('dockBody').querySelectorAll('.primary').length === 0 && !!sinBtn('Sign in again') && $('dockBody').textContent.includes('The sign-in ended'), 'review 1.2: ended with no exit read: Sign in again is offered, never as the main button: ' + $('dockBody').textContent);
    endRow3.signin.exit = 0; await fetchDesks(); drawSignin($('dockBody'));
    ok($('dockBody').querySelectorAll('.primary').length === 0 && !$('dockBody').textContent.includes('The sign-in ended'), 'review 1.2: an exit 0 while the account is read again: no main button, no "ended" yet: ' + $('dockBody').textContent);
    await tick(30);
    ok(!!sinBtn('Sign in again') && sinBtn('Sign in again').classList.contains('primary') && $('dockBody').textContent.includes('The sign-in ended'), 'review 1.2: the account reads not signed in: then Sign in again is the main button: ' + $('dockBody').textContent);
    await dropSigninDesk('signin-cursor-alt'); SIGNIN.name = ''; closePanel(); await fetchDesks();
  } else ok(false, 'review 1.2: a second Cursor sign-in desk opened');
  ACCOUNTS = null; await loadAccounts();
  closePanel(); closeTab('page', 'accounts'); await tick(5);

  // ---- 25.4 U2: the account picker for Claude and Cursor (default + named), hidden for Shell; a folder for Claude and Cursor, home for Shell alone ----
  await openSpawnSheet(); await tick(10);
  const optVals = id => [...$(id).options].map(o => o.value).join(',');
  const accHidden = () => $('spawnAccount').parentNode.classList.contains('hidden');
  ok(SPAWN.kind === 'claude' && !accHidden() && optVals('spawnAccount') === ',work' && $('spawnAccount').options[0].textContent.includes('signed in') && !$('spawnAccount').options[0].textContent.includes('not signed in'), 'U2: Claude: the default sign-in (signed in) and work: ' + optVals('spawnAccount'));
  setSpawnKind('cursor');
  ok(!accHidden() && optVals('spawnAccount') === ',alt' && $('spawnAccount').options[0].textContent.includes('not signed in'), 'U2: Cursor shows its picker too: the default (not signed in) and alt: ' + optVals('spawnAccount'));
  ok(!$('spawnCwd').options.some(o => o.value === ''), 'U2: no home for Cursor: ' + optVals('spawnCwd'));
  setSpawnKind('shell');
  ok(accHidden() && $('spawnCwd').options[0].value === '' && $('spawnCwd').options[0].textContent === 'home', 'U2: Shell hides the picker and offers home');
  setSpawnKind('claude');
  ok(!$('spawnCwd').options.some(o => o.value === '') && $('spawnCwd').value === '/work/u/notes', 'U2: no home for Claude; no active project, so meta.default_folder: ' + $('spawnCwd').value);
  closeSheets();
  setWS('desk-a'); await tick(5);
  await openSpawnSheet(); await tick(10);
  ok($('spawnCwd').value === '/work/u/proj-one', 'U2: the active desk folder is preselected: ' + $('spawnCwd').value);
  setSpawnKind('cursor'); $('spawnAccount').value = 'alt'; await $('spawnGo').onclick(); await tick(20);
  ok(JSON.stringify(spawns().at(-1).body) === JSON.stringify({kind: 'cursor', account: 'alt', cwd: '/work/u/proj-one'}), 'U2: a Cursor desk on account alt in the project folder: ' + JSON.stringify(spawns().at(-1).body));
  // no folder at all: refused inline before any POST (the server says the same, 25.3 B4)
  const keepP = PROJECTS, keepF = META.default_folder, keepWS = WS;
  PROJECTS = []; META.default_folder = null; WS = '';
  await openSpawnSheet(); setSpawnKind('claude'); const nSp3 = spawns().length;
  await $('spawnGo').onclick(); await tick(5);
  ok($('spawnErr').textContent === 'choose a project folder' && spawns().length === nSp3, 'U2: Claude with no folder is refused inline: ' + $('spawnErr').textContent);
  PROJECTS = keepP; META.default_folder = keepF; WS = keepWS; closeSheets(); setSpawnKind('claude');
  // the Run kind hands over to the composer
  await openSpawnSheet(); setSpawnKind('run'); await $('spawnGo').onclick(); await tick(30);
  ok(MODE === 'run' && $('spawnSheet').classList.contains('hidden'), 'the Run kind switches the composer to Run mode');
  setMode('msg');
  // accounts page + new account (button disabled during the await)
  openPage('accounts');
  ok($('page').querySelectorAll('.skel').length >= 1, 'accounts page shows skeleton rows while loading');
  await tick(30);
  ok($('page').textContent.includes('work') && $('page').textContent.includes('unproven') && $('page').querySelectorAll('.skel').length === 0, 'accounts page lists claude login and cursor folder');
  const acctRows = $('page').querySelectorAll('.acct'), defRows = acctRows.filter(r => r.textContent.startsWith('default'));
  ok(acctRows.filter(r => r.querySelectorAll('button').some(b => b.textContent === 'Sign in')).length === 2 && defRows.length === 2 && defRows.every(r => r.querySelectorAll('button').length === 0), 'U1: every named account has Sign in; the two default sign-ins show their state and no button');
  ok(!$('page').textContent.includes('/login'), 'the Accounts page no longer says the desk types /login');
  const pb = $('page').querySelectorAll('.pbody')[0]; newAccountForm(pb); const inp = $('acctForm').querySelectorAll('.acctForm')[0].children[1]; inp.value = 'Bad Name';
  await $('acctForm').querySelectorAll('.primary')[0].onclick(); ok(!LOG.posts.some(q => q.path === '/api/accounts'), 'bad account name refused client-side');
  inp.value = 'alt2'; const c1 = $('acctForm').querySelectorAll('.primary')[0].onclick(); const c2 = $('acctForm').querySelectorAll('.primary')[0].onclick(); await c1; await c2; await tick(10);
  ok(LOG.posts.filter(q => q.path === '/api/accounts' && q.body.name === 'alt2').length >= 1, 'POST /api/accounts sent');
  ok(JSON.stringify(LOG.posts.filter(q => q.path === '/api/spawn').at(-1).body) === JSON.stringify({kind: 'claude', account: 'alt2', signin: true}) && DOCK.id === 'signin', 'U1: Create and sign in starts the sign-in card on the new account');
  closePanel();
  // Setup's sign-in step (25.4 U1): a named account that is not signed in goes straight to its sign-in card
  const keepAcct = ACCOUNTS;
  ACCOUNTS = {claude: [{name: 'work', ready: false}], cursor: [], defaults: {claude: {ready: false}, cursor: {ready: false}}};
  openPage('setup'); await tick(10);
  const step1 = $('page').querySelectorAll('.step')[0];
  ok(step1 && step1.textContent.includes('Sign in') && !step1.classList.contains('done'), 'U1 Setup: step 1 is Sign in, not done while nothing is signed in: ' + (step1 ? step1.textContent : 'no step'));
  if (step1) { await step1.querySelectorAll('button')[0].onclick({}); await tick(20); }
  ok(JSON.stringify(spawns().at(-1).body) === JSON.stringify({kind: 'claude', account: 'work', signin: true}) && DOCK.id === 'signin', 'U1 Setup: the sign-in step spawns {kind, account, signin:true} and opens the card');
  closePanel(); closeTab('page', 'setup'); ACCOUNTS = keepAcct;
  // a MetaBot login of engine codex is listed with the Claude logins (mbq logins): never a Claude desk's account, no Sign in (the server: "not a claude login")
  ACCT.claude.push({ name: 'cx', home: '/s/codex/cx', engine: 'codex', max_at_once: 1, ready: false, source: 'metabot' });
  ACCOUNTS = null; openPage('accounts'); await tick(30);
  const cxRow = $('page').querySelectorAll('.acct').find(r => r.textContent.startsWith('cx'));
  ok(cxRow && cxRow.textContent.includes('codex') && cxRow.querySelectorAll('button').length === 0, 'U1: a codex login shows its engine and no Sign in: ' + (cxRow ? cxRow.textContent : 'no row'));
  await openSpawnSheet(); setSpawnKind('claude');
  ok(optVals('spawnAccount') === ',work', 'U2: a codex login is never offered for a Claude desk: ' + optVals('spawnAccount'));
  closeSheets();
  ACCOUNTS = {claude: [{name: 'cx', engine: 'codex', ready: false}], cursor: [], defaults: {claude: {ready: false}, cursor: {ready: false}}};
  openPage('setup'); await tick(10);
  const nSp5 = spawns().length, s1b = $('page').querySelectorAll('.step')[0];
  if (s1b) { await s1b.querySelectorAll('button')[0].onclick({}); await tick(30); }
  ok(spawns().length === nSp5 && ACTIVE && ACTIVE.id === 'accounts', 'U1 Setup: a codex login is no sign-in to start; the step opens Accounts');
  closeTab('page', 'setup'); closeTab('page', 'accounts'); ACCT.claude.pop(); ACCOUNTS = null; await loadAccounts();
  // sdk page: raw_b64 of the same bytes
  openPage('sdk'); await tick(20);
  $('sdkRaw').value = '{"format":"x"}'; await $('page').querySelectorAll('.primary')[0].onclick(); await tick(20);
  ok(LOG.posts.at(-1).path === '/api/sdk/verify' && LOG.posts.at(-1).body.raw_b64 === Buffer.from('{"format":"x"}').toString('base64') && !('raw' in LOG.posts.at(-1).body), 'SDK posts raw_b64 of the same bytes (finding ui-network-8)');
  $('sdkRaw').value = 'garbage'; await $('page').querySelectorAll('.primary')[0].onclick(); await tick(20);
  ok($('page').querySelectorAll('.ccol')[0].querySelectorAll('.cv')[0].textContent.includes('could not read the input'), 'SDK: pc_exit 2 says could not read the input');
  // workflows page
  openPage('workflows'); await tick(30);
  ok($('page').textContent.includes('wf-one') && $('page').querySelectorAll('.abtn').some(x => x.textContent === 'Run') && !!$('wfNew'), 'Workflows page lists workflows with Run and a New button');
  $('wfNew').onclick(); await tick(5);
  ok(!!$('wfForm') && $('wfForm').textContent.includes('metabot workflow new'), 'no workflow.new in the catalogue: New shows the staged-command form');

  // ---- 25.4 U3: Extend: Make · Installed · Marketplaces · Engines · This app · Between harnesses ----
  abTap('plugins'); await tick(30);
  const secIds = () => $('page').querySelectorAll('.xsec').map(x => x.id).join(',');
  const sec = id => $('page').querySelectorAll('.xsec').find(x => x.id === 'ext-' + id) || h('div');
  const cbtn = (c, t) => c.querySelectorAll('button').find(b => b.textContent === t);
  const cdesk = () => (LOG.posts.filter(q => q.path === '/api/cli/desk').at(-1) || {}).body || {};
  const cards = () => sec('make').querySelectorAll('.mkcard');
  ok(ACTIVE && ACTIVE.kind === 'page' && ACTIVE.id === 'plugins' && $('page').querySelectorAll('h2')[0].textContent === 'Extend', 'U3: the Extend item opens the Extend page');
  ok(secIds() === 'ext-make,ext-installed,ext-markets,ext-engines,ext-app,ext-between', 'U3: six sections in developer mode: ' + secIds());
  ok($('sideBody').querySelectorAll('.srow').map(r => r.textContent).join(',') === 'Make,Installed,Marketplaces,Engines,This app,Between harnesses', 'U3: the side bar lists the sections: ' + $('sideBody').querySelectorAll('.srow').map(r => r.textContent).join(','));
  ok(LOG.gets.some(x => x.startsWith('/api/extend')), 'U3: the page reads GET /api/extend');
  ok(cards().map(c => (c.querySelectorAll('h3')[0] || {}).textContent).join(',') === 'Skill,Bridge,Import skills,App module', 'U3 Make: four cards: ' + cards().map(c => c.textContent.slice(0, 20)).join(' | '));
  if (cards().length === 4) {
    // Skill: a name and a folder → plugin.new in that folder; then Add as marketplace → Install → Enable
    const c0 = cards()[0];
    c0.querySelectorAll('input')[0].value = 'notes2'; c0.querySelectorAll('select')[0].value = '/work/u/proj-one';
    await c0.querySelectorAll('.primary')[0].onclick({}); await tick(20);
    ok(JSON.stringify(cdesk()) === JSON.stringify({id: 'plugin.new', params: {name: 'notes2'}, cwd: '/work/u/proj-one'}), 'U3 Skill: plugin.new writes in the chosen folder: ' + JSON.stringify(cdesk()));
    ok(!$('deskSheet').classList.contains('hidden') && ACTIVE.kind === 'page' && ACTIVE.id === 'plugins', 'U3: the desk opens as a sheet over the page, so the next steps stay in view');
    closeSheets();
    ok(['Add as marketplace', 'Install', 'Enable'].every(t => cbtn(c0, t)) && cbtn(c0, 'Add as marketplace').classList.contains('primary'), 'U3 Skill: then Add as marketplace → Install → Enable, the next one marked');
    await cbtn(c0, 'Add as marketplace').onclick({}); await tick(20); closeSheets();
    ok(JSON.stringify(cdesk()) === JSON.stringify({id: 'plugin.marketplace.add', params: {name: 'notes2', folder: '/work/u/proj-one/notes2'}}), 'U3: Add as marketplace: the new folder, under the plugin name: ' + JSON.stringify(cdesk()));
    ok(cbtn(c0, 'Install').classList.contains('primary') && !cbtn(c0, 'Add as marketplace').classList.contains('primary'), 'U3: the mark moves to the next step');
    await cbtn(c0, 'Install').onclick({}); await tick(20); closeSheets();
    ok(JSON.stringify(cdesk()) === JSON.stringify({id: 'plugin.install', params: {ref: 'notes2@notes2'}}), 'U3: Install NAME@MARKET: ' + JSON.stringify(cdesk()));
    await cbtn(c0, 'Enable').onclick({}); await tick(20); closeSheets();
    ok(JSON.stringify(cdesk()) === JSON.stringify({id: 'plugin.enable', params: {name: 'notes2'}}), 'U3: Enable NAME');
    // Bridge: Enable as an engine, with its one line; the marketplace name metabot picks when the plain one is taken
    const c1 = cards()[1];
    c1.querySelectorAll('input')[0].value = 'notes'; c1.querySelectorAll('select')[0].value = '/work/u/notes';
    await c1.querySelectorAll('.primary')[0].onclick({}); await tick(20); closeSheets();
    ok(JSON.stringify(cdesk()) === JSON.stringify({id: 'plugin.new-bridge', params: {name: 'notes'}, cwd: '/work/u/notes'}), 'U3 Bridge: plugin.new-bridge: ' + JSON.stringify(cdesk()));
    ok(['Add as marketplace', 'Install', 'Enable as an engine'].every(t => cbtn(c1, t)) && c1.textContent.includes('a bridge runs outside the gate: it is a program you wrote'), 'U3 Bridge: then Enable as an engine, with its one line');
    await cbtn(c1, 'Add as marketplace').onclick({}); await tick(20); closeSheets();
    ok(cdesk().params && cdesk().params.name === 'notes-2' && cdesk().params.folder === '/work/u/notes/notes', 'U3: notes is held by another folder, so the marketplace is notes-2 (as metabot names it): ' + JSON.stringify(cdesk().params));
    await cbtn(c1, 'Enable as an engine').onclick({}); await tick(20); closeSheets();
    ok(JSON.stringify(cdesk()) === JSON.stringify({id: 'plugin.enable-bridges', params: {name: 'notes'}}), 'U3: Enable as an engine = plugin.enable-bridges');
    // Import skills: a folder of skills → plugin.import-skills; it installs as it imports, so Enable follows
    const c2 = cards()[2], nDesk = LOG.posts.filter(q => q.path === '/api/cli/desk').length;
    c2.querySelectorAll('input')[0].value = 'myskills'; c2.querySelectorAll('input')[1].value = 'skills';
    await c2.querySelectorAll('.primary')[0].onclick({}); await tick(10);
    ok(LOG.posts.filter(q => q.path === '/api/cli/desk').length === nDesk, 'U3 Import skills: a relative folder is refused before any desk');
    c2.querySelectorAll('input')[1].value = '/work/u/.claude/skills';
    await c2.querySelectorAll('.primary')[0].onclick({}); await tick(20);
    ok(c2.querySelectorAll('.err').some(e => e.textContent.includes('bad folder: bad path')), 'review 1.2: Import from ~/.claude/skills, refused by the PC path rule: the card shows what the PC said: ' + c2.querySelectorAll('.err').map(e => e.textContent).join(' | '));
    c2.querySelectorAll('input')[1].value = '/work/u/proj-one/.claude/skills';
    await c2.querySelectorAll('.primary')[0].onclick({}); await tick(20); closeSheets();
    ok(JSON.stringify(cdesk()) === JSON.stringify({id: 'plugin.import-skills', params: {name: 'myskills', folder: '/work/u/proj-one/.claude/skills'}}), 'U3 Import skills: plugin.import-skills NAME FOLDER: ' + JSON.stringify(cdesk()));
    ok(!!cbtn(c2, 'Enable') && !cbtn(c2, 'Install') && !cbtn(c2, 'Add as marketplace'), 'U3 Import skills: Enable is the step after');
    // App module: POST /api/modules/new, the modules read again, the folder in a desk
    const c3 = cards()[3], nModsGet = LOG.gets.filter(x => x.startsWith('/api/modules')).length;
    c3.querySelectorAll('input')[0].value = 'mymod'; c3.querySelectorAll('input')[1].value = 'My module';
    await c3.querySelectorAll('.primary')[0].onclick({}); await tick(30);
    ok(JSON.stringify((LOG.posts.filter(q => q.path === '/api/modules/new').at(-1) || {}).body) === JSON.stringify({id: 'mymod', label: 'My module'}), 'U3 App module: POST /api/modules/new {id, label}');
    ok(LOG.gets.filter(x => x.startsWith('/api/modules')).length > nModsGet, 'U3 App module: the modules are read again');
    const c3b = cards()[3];
    ok(c3b.textContent.includes('mymod.json') && !!cbtn(c3b, 'Open the modules folder in a desk'), 'U3 App module: it says the file it wrote and offers the folder in a desk: ' + c3b.textContent.slice(0, 120));
    if (cbtn(c3b, 'Open the modules folder in a desk')) { await cbtn(c3b, 'Open the modules folder in a desk').onclick({}); await tick(20); }
    ok(JSON.stringify(spawns().at(-1).body) === JSON.stringify({kind: 'shell', cwd: '/s/modules'}), 'U3: the modules folder opens in a shell desk: ' + JSON.stringify(spawns().at(-1).body));
    closeSheets(); openPage('plugins'); await tick(20);
    const c3c = cards()[3]; c3c.querySelectorAll('input')[0].value = 'mymod';
    await c3c.querySelectorAll('.primary')[0].onclick({}); await tick(20);
    ok($('toast').textContent.includes('exists'), 'U3: a module that exists is refused with the server words: ' + $('toast').textContent);
  }
  // Installed: pinned hash, on/off, counts, facts on tap; Enable / Disable / Remove
  const xi = () => sec('installed').querySelectorAll('.xn');
  const hashOf = r => (r.querySelectorAll('.hash')[0] || {}).textContent;
  ok(xi().length === 2 && hashOf(xi()[0]) === 'a1b2c3d4e5f6' && hashOf(xi()[1]) === 'f0e1d2c3b4a5', 'U3 Installed: each plugin pinned by the leading 12 hex of its tree hash: ' + xi().map(hashOf).join(','));
  ok(xi().length === 2 && xi()[0].textContent.includes('1 skill') && xi()[0].textContent.includes('1 hook') && xi()[0].textContent.includes('1 server') && xi()[1].textContent.includes('1 command') && xi()[1].textContent.includes('1 bridge'), 'U3 Installed: counts');
  const sws = () => sec('installed').querySelectorAll('.sw');
  ok(sws().length === 2 && sws()[0].getAttribute('role') === 'switch' && sws()[0].getAttribute('aria-checked') === 'true' && sws()[1].getAttribute('aria-checked') === 'false', 'U3 Installed: on/off from enabled');
  if (sws().length === 2) {
    await sws()[0].onclick({stopPropagation() {}}); await tick(20); closeSheets();
    ok(JSON.stringify(cdesk()) === JSON.stringify({id: 'plugin.disable', params: {name: 'notes'}}), 'U3: switching notes off opens plugin.disable');
    await sws()[1].onclick({stopPropagation() {}}); await tick(20); closeSheets();
    ok(JSON.stringify(cdesk()) === JSON.stringify({id: 'plugin.enable', params: {name: 'echo'}}), 'U3: switching echo on opens plugin.enable (it leaves its bridges out)');
  }
  const facts = i => sec('installed').querySelectorAll('.xfacts')[i] || h('div');
  if (xi().length === 2) { xi()[0].onclick({}); if (facts(1).classList.contains('hidden')) xi()[1].onclick({}); await tick(5); }
  ok(!facts(0).classList.contains('hidden') && facts(0).textContent.includes('notes@notes') && facts(0).textContent.includes('PreToolUse 1') && facts(0).textContent.includes('notes-mcp') && facts(0).textContent.includes('outside the gate'), 'U3: a tap shows the facts: from, hooks (off: they run outside the gate), servers: ' + facts(0).textContent.slice(0, 200));
  ok(['Disable', 'Allow hooks', 'Add its servers as connections', 'Remove'].every(t => cbtn(facts(0), t)), 'U3: Disable · Allow hooks (developer) · Add its servers as connections · Remove');
  ok(facts(0).textContent.includes('0.1.0'), 'U3: the facts name the version');
  for (const [t, id] of [['Allow hooks', 'plugin.enable-hooks'], ['Add its servers as connections', 'plugin.enable-servers'], ['Remove', 'plugin.remove']]) {
    if (cbtn(facts(0), t)) { await cbtn(facts(0), t).onclick({}); await tick(20); closeSheets(); }
    ok(JSON.stringify(cdesk()) === JSON.stringify({id, params: {name: 'notes'}}), 'U3: ' + t + ' = ' + id);
  }
  ok(!!cbtn(facts(1), 'Enable') && cbtn(facts(1), 'Enable').classList.contains('primary') && !!cbtn(facts(1), 'Enable as an engine') && !cbtn(facts(1), 'Enable as an engine').classList.contains('primary')
    && facts(1).textContent.includes('a bridge runs outside the gate: it is a program you wrote') && facts(1).textContent.includes('Enable leaves out its bridges'), 'U3: a plugin with a bridge: Enable (its bridges left out, the facts say so) or Enable as an engine, with its line');
  // Marketplaces: folders and their plugins, Install; descriptions are text
  const xm = sec('markets'), kit = xm.querySelectorAll('.xn').find(r => r.textContent.includes('review-kit'));
  ok(kit && kit.textContent.includes("<b>review</b> & 'check'") && !kit.innerHTML && xm.textContent.includes('/work/u/proj-one/team'), 'U3 Marketplaces: plugins with their descriptions as text, each marketplace with its folder');
  if (kit && cbtn(kit, 'Install')) { await cbtn(kit, 'Install').onclick({}); await tick(20); closeSheets(); }
  ok(JSON.stringify(cdesk()) === JSON.stringify({id: 'plugin.install', params: {ref: 'review-kit@team'}}), 'U3: Install from a marketplace: review-kit@team');
  const notesM = xm.querySelectorAll('.xn').find(r => r.textContent.startsWith('notes'));
  ok(notesM && !cbtn(notesM, 'Install') && notesM.querySelectorAll('.chip').some(c => c.textContent === 'installed'), 'U3: an installed plugin says installed instead of Install');
  // Engines: built-in and bridges, local marked, Try it, local models
  const xe = () => sec('engines').querySelectorAll('.xn');
  ok(xe().map(r => (r.querySelectorAll('.xt')[0] || {}).textContent).join(',') === 'claude,cursor,openai,echo', 'U3 Engines: built-in and bridges');
  ok(xe().length === 4 && xe()[2].textContent.includes('local · works offline') && xe()[3].textContent.includes('bridge') && xe()[3].textContent.includes('local · works offline') && !xe()[0].textContent.includes('local'), 'U3 Engines: local ones say local · works offline');
  ok(xe().length === 4 && xe()[3].textContent.includes('echo@echo') && !xe()[2].textContent.includes('@'), 'U3 Engines: a bridge names the plugin it comes from');
  const lm = cbtn(sec('engines'), 'Check local models');
  const nRd = LOG.posts.filter(q => q.path === '/api/cli/read').length;
  if (lm) { await lm.onclick({}); await tick(20); closeSheets(); }
  ok(JSON.stringify(cdesk()) === JSON.stringify({id: 'doctor.models', params: {}}) && LOG.posts.filter(q => q.path === '/api/cli/read').length === nRd, 'U3: Check local models opens doctor --models in a desk (J9): it may run past a read timeout');
  if (xe().length === 4) { await cbtn(xe()[3], 'Try it').onclick({}); await tick(40); }
  ok(MODE === 'run' && $('chipEngine').value === 'echo' && !$('runSheet').classList.contains('hidden') && ACTIVE.kind === 'chat', 'U3: Try it opens Run in the composer with that engine chosen');
  closeSheets(); setMode('msg'); $('chipEngine').value = 'claude';
  openPage('plugins'); await tick(20);
  // This app: modules with load errors, New module, Reload, the modules folder
  const xa = sec('app');
  ok(xa.textContent.includes('broken') && xa.textContent.includes('unknown catalog id x.y') && ['New module', 'Reload', 'Open the modules folder in a desk'].every(t => cbtn(xa, t)), 'U3 This app: load errors, New module, Reload, the modules folder');
  if (cbtn(xa, 'New module')) cbtn(xa, 'New module').onclick({});
  ok(cards()[3] && document.activeElement === cards()[3].querySelectorAll('input')[0], 'U3: New module goes to the App module card');
  const nRl = LOG.posts.filter(q => q.path === '/api/modules/reload').length;
  if (cbtn(xa, 'Reload')) { await cbtn(xa, 'Reload').onclick({}); await tick(30); }
  ok(LOG.posts.filter(q => q.path === '/api/modules/reload').length === nRl + 1 && LOG.gets.filter(x => x.startsWith('/api/extend')).length >= 2, 'U3: Reload posts /api/modules/reload and reads Extend again');
  // the modules folder is GET /api/extend modules.folder (<state>/modules), also before any module of your own
  const keepUser = XT.modules.user; XT.modules.user = []; MOD_DIR = ''; EXTEND = null; renderPage('plugins'); await tick(30);
  const nSp4 = spawns().length, mf = cbtn(sec('app'), 'Open the modules folder in a desk');
  if (mf) { await mf.onclick({}); await tick(20); }
  ok(spawns().length === nSp4 + 1 && JSON.stringify(spawns().at(-1).body) === JSON.stringify({kind: 'shell', cwd: '/s/modules'}), 'U3: the modules folder comes from modules.folder: ' + JSON.stringify(spawns().at(-1).body));
  XT.modules.user = keepUser; EXTEND = null; closeSheets(); openPage('plugins'); await tick(30);
  // Between harnesses (developer): three lines to copy
  const xb = sec('between');
  ok(['metabot mail', 'metabot hook pre-tool-use', 'metabot mcp serve'].every(t => xb.textContent.includes(t)) && xb.querySelectorAll('button').length === 3, 'U3 Between harnesses: three lines, each with Copy');
  if (xb.querySelectorAll('button').length === 3) xb.querySelectorAll('button')[1].onclick({});
  ok($('toast').textContent === 'copied: metabot hook pre-tool-use', 'U3: Copy copies the line: ' + $('toast').textContent);
  // user mode: five sections, Allow hooks gone
  setUMode('user'); await tick(20);
  ok(secIds() === 'ext-make,ext-installed,ext-markets,ext-engines,ext-app', 'U3: user mode: Between harnesses is developer-only: ' + secIds());
  if (xi().length) { xi()[0].onclick({}); await tick(5); }
  ok(!cbtn(facts(0), 'Allow hooks') && !!cbtn(facts(0), 'Disable'), 'U3: Allow hooks is developer-only');
  setUMode('dev'); await tick(20);
  // an older PC without GET /api/extend: the error where the data goes; Make still works
  XT_DOWN = true; EXTEND = null; renderPage('plugins'); await tick(30);
  ok(sec('installed').querySelectorAll('.err').length === 1 && sec('installed').textContent.includes('HTTP 404') && cards().length === 4, 'U3: without /api/extend the page says so and Make still works');
  XT_DOWN = false; EXTEND = null;
  // 25.3 B6 errors: a part whose MetaBot read failed says so where its rows go; a plugin or a marketplace that does not read says why;
  // each plugin gets the enable MetaBot takes for it (hooks and bridges each need a word, and one entry carries one word)
  const XT0 = JSON.stringify(XT);
  const pl = (name, o) => Object.assign({ name, ref: name + '@team', marketplace: 'team', tree: name[0].repeat(64), version: '1.0.0', enabled: false, skills: [], commands: [], agents: [],
    hooks: {}, hooks_allowed: false, servers: [], servers_added: false, bridges: [], bridges_on: false, error: null }, o);
  XT.plugins = [pl('mcpkit', { servers: ['db'] }), pl('both', { hooks: { Stop: 1 }, bridges: ['both'] }), pl('gone', { enabled: true, error: 'its installed copy does not read' }), pl('hooky', { hooks: { Stop: 2 } })];
  XT.marketplaces[1].error = 'PluginError: bad marketplace.json';
  XT.errors = [{ part: 'engines', error: 'ConfigError: bad config.toml' }];
  EXTEND = null; renderPage('plugins'); await tick(30);
  const rowOf = n => xi().find(r => (r.querySelectorAll('.xt')[0] || {}).textContent === n);
  const fx = n => { const i = xi().indexOf(rowOf(n)); return i < 0 ? h('div') : facts(i); };
  xi().forEach(r => { if (fx((r.querySelectorAll('.xt')[0] || {}).textContent).classList.contains('hidden')) r.onclick({}); }); await tick(5);
  ok(!!cbtn(fx('mcpkit'), 'Enable') && cbtn(fx('mcpkit'), 'Enable').classList.contains('primary') && !!cbtn(fx('mcpkit'), 'Add its servers as connections') && !fx('mcpkit').textContent.includes('Enable leaves out'), 'U3: a plugin with servers alone: Enable, or Add its servers as connections; nothing is left out');
  if (rowOf('mcpkit')) { await rowOf('mcpkit').querySelectorAll('.sw')[0].onclick({stopPropagation() {}}); await tick(20); closeSheets(); }
  ok(JSON.stringify(cdesk()) === JSON.stringify({id: 'plugin.enable', params: {name: 'mcpkit'}}), 'U3: its switch opens plugin.enable');
  if (cbtn(fx('mcpkit'), 'Add its servers as connections')) { await cbtn(fx('mcpkit'), 'Add its servers as connections').onclick({}); await tick(20); closeSheets(); }
  ok(JSON.stringify(cdesk()) === JSON.stringify({id: 'plugin.enable-servers', params: {name: 'mcpkit'}}), 'U3: Add its servers as connections = plugin.enable-servers');
  if (rowOf('both')) { await rowOf('both').querySelectorAll('.sw')[0].onclick({stopPropagation() {}}); await tick(20); closeSheets(); }
  ok(JSON.stringify(cdesk()) === JSON.stringify({id: 'plugin.enable', params: {name: 'both'}}), 'U3: the switch is Enable for a plugin with hooks and bridges too');
  ok(['Enable', 'Enable as an engine', 'Allow hooks'].every(t => cbtn(fx('both'), t)) && fx('both').textContent.includes('Enable leaves out its hooks and its bridges') && fx('both').textContent.includes('terminal'),
    'U3: hooks and bridges: Enable, Enable as an engine, Allow hooks (developer); allowing both together is a terminal step, and the facts say so');
  ok(!!cbtn(fx('hooky'), 'Enable') && !!cbtn(fx('hooky'), 'Allow hooks') && fx('hooky').textContent.includes('Enable leaves out its hooks'), 'U3: hooks alone, developer mode: Enable (its hooks left out) or Allow hooks');
  ok(rowOf('gone') && rowOf('gone').textContent.includes('does not read') && !cbtn(fx('gone'), 'Enable') && !!cbtn(fx('gone'), 'Disable') && !!cbtn(fx('gone'), 'Remove'), 'U3: a plugin whose installed copy does not read says so; Disable and Remove stay');
  ok(sec('markets').textContent.includes('PluginError: bad marketplace.json'), 'U3: a marketplace that does not read says why under its name');
  ok(sec('engines').querySelectorAll('.err').length === 1 && sec('engines').textContent.includes('ConfigError: bad config.toml') && xe().length === 4, 'U3: an engines error shows above the engines it still lists');
  setUMode('user'); await tick(20);
  ok(!cbtn(fx('hooky'), 'Allow hooks') && !!cbtn(fx('hooky'), 'Enable') && !cbtn(fx('both'), 'Allow hooks') && !!cbtn(fx('both'), 'Enable as an engine'), 'U3: user mode: Enable and Enable as an engine stay, Allow hooks goes');
  setUMode('dev'); await tick(20);
  XT.plugins = []; XT.errors = [{ part: 'plugins', error: 'mbq: TimeoutExpired' }]; EXTEND = null; renderPage('plugins'); await tick(30);
  ok(sec('installed').querySelectorAll('.err').length === 1 && sec('installed').textContent.includes('mbq: TimeoutExpired') && !sec('installed').textContent.includes('Nothing installed'), 'U3: a plugins read that failed is an error, never nothing installed');
  Object.assign(XT, JSON.parse(XT0)); EXTEND = null;
  closeTab('page', 'plugins'); abTap('chats'); await tick(5);

  // ---- 25.4 U4: Workflows from a task: Propose → the draft → Keep by shape → Approve → Run now / Schedule; serve ----
  CATALOG['workflow.new'] = {id: 'workflow.new', label: 'New workflow', module: 'workflows', kind: 'desk', argv: ['{metabot}', 'workflow', 'new', '-e', '{engine}', '--cwd', '{folder}', '{name}', '{prompt}'],
    params: [{name: 'name', type: 'id', label: 'Workflow name', required: true}, {name: 'engine', type: 'slug', label: 'Engine', required: true}, {name: 'folder', type: 'path', label: 'Folder', required: true}, {name: 'prompt', type: 'text', label: 'Prompt', required: true}]};
  openPage('workflows'); await tick(30);
  const wbtn = t => $('page').querySelectorAll('button').find(b => b.textContent === t);
  const dr = () => ($('wfDraft') || {textContent: ''}).textContent;
  ok(!!$('wfTaskText') && !!$('wfFolder') && $('page').querySelectorAll('.wfTask').length === 1 && !!wbtn('Propose'), 'U4: From a task: a task box, a folder and Propose');
  if (wbtn('Propose')) {
    $('wfTaskText').value = 'review the parser'; $('wfFolder').value = '/work/u/proj-one';
    await wbtn('Propose').onclick({}); await tick(20);
    const pr = (LOG.posts.filter(q => q.path === '/api/cli/read').at(-1) || {}).body;
    ok(JSON.stringify(pr) === JSON.stringify({id: 'propose', params: {folder: '/work/u/proj-one', task: 'review the parser'}}), 'U4: Propose reads propose {folder, task}: ' + JSON.stringify(pr));
    ok(dr().includes('ask') && dr().includes('review-the-parser') && dr().includes('a review is one ask, on the review role') && dr().includes('ask: claude, role review, no login') && dr().includes('9f86d081884c'), 'U4: the draft shows shape, name, rules, runs on, steps and its hash: ' + dr().slice(0, 200));
    // review 1.2 (RV-C): MetaBot's ask step sets role and read_only; workflow.new stores neither, so Keep is off and says why
    const nKeep = LOG.posts.filter(q => q.path === '/api/cli/desk').length;
    ok(wbtn('Keep') && wbtn('Keep').disabled && dr().includes('Keep is off: the draft sets role review · read-only') && wbtn('Approve').disabled && wbtn('Run now').disabled && wbtn('Schedule').disabled,
      'U4/RV-C: an ask draft that sets role and read-only is never kept without them: Keep is off and says why: ' + dr().slice(-260));
    await wbtn('Keep').onclick({}); await tick(20);
    ok(LOG.posts.filter(q => q.path === '/api/cli/desk').length === nKeep, 'U4/RV-C: a tap on the disabled Keep opens no desk');
    $('wfTaskText').value = 'noengine check the notes'; await wbtn('Propose').onclick({}); await tick(20);
    ok(wbtn('Keep').disabled && dr().includes('names no engine'), 'RV-C: a draft that names no engine is not kept on claude (no engine is chosen for it): ' + dr().slice(-160));
    // a ladder draft Keep stores as shown: Keep → the desk → Check reads the list → Approve → Check → Run now; Schedule stays off
    $('wfTaskText').value = 'build the ladder'; await wbtn('Propose').onclick({}); await tick(20);
    ok(dr().includes('ladder') && dr().includes('max_cost in step ladder'), 'U4: a ladder draft names what is missing');
    ok(wbtn('Keep') && !wbtn('Keep').disabled && wbtn('Keep').classList.contains('primary') && wbtn('Approve').disabled && wbtn('Run now').disabled && wbtn('Schedule').disabled, 'U4: Keep is the next step; Approve, Run now and Schedule wait for it');
    await wbtn('Keep').onclick({}); await tick(20); closeSheets();
    ok(JSON.stringify(cdesk()) === JSON.stringify({id: 'workflow.new-ladder', params: {name: 'build-the-ladder', folder: '/work/u/proj-one', prompt: 'build the ladder'}}), 'U4: Keep a ladder = workflow.new-ladder: ' + JSON.stringify(cdesk()));
    ok(wbtn('Approve').disabled && !!wbtn('Check') && wbtn('Check').classList.contains('primary') && dr().includes('Waiting for the desk'), 'review 1.2: the Keep desk opening is not the workflow kept: Approve waits and Check is the next step');
    await wbtn('Check').onclick({}); await tick(20);
    ok(wbtn('Approve').disabled && dr().includes('Not kept yet'), 'review 1.2: Check with no build-the-ladder in the list keeps Approve off and says so');
    WF_ROWS.push('build-the-ladder     a1b2c3d4e5f6  not approved   1 step   stored 2026-11-09T10:00:00Z');
    await wbtn('Check').onclick({}); await tick(20);
    ok(!wbtn('Approve').disabled && wbtn('Approve').classList.contains('primary') && !wbtn('Check') && wbtn('Run now').disabled, 'review 1.2: the list shows it kept: Approve is the next step');
    await wbtn('Approve').onclick({}); await tick(20); closeSheets();
    ok(JSON.stringify(cdesk()) === JSON.stringify({id: 'workflow.approve', params: {ref: 'build-the-ladder'}}), 'U4: Approve opens workflow.approve in a desk (signed at the terminal)');
    ok(wbtn('Run now').disabled && !!wbtn('Check'), 'review 1.2: the Approve desk opening is not the workflow approved: Run now waits');
    await wbtn('Check').onclick({}); await tick(20);
    ok(wbtn('Run now').disabled && dr().includes('Not approved yet'), 'review 1.2: the list still says not approved: Run now stays off');
    WF_ROWS[WF_ROWS.length - 1] = 'build-the-ladder     a1b2c3d4e5f6  approved       1 step   stored 2026-11-09T10:00:00Z';
    await wbtn('Check').onclick({}); await tick(20);
    ok(!wbtn('Run now').disabled && wbtn('Run now').classList.contains('primary'), 'review 1.2: the list says approved: Run now');
    await wbtn('Run now').onclick({}); await tick(20); closeSheets();
    ok(JSON.stringify(cdesk()) === JSON.stringify({id: 'workflow.run', params: {ref: 'build-the-ladder'}}), 'U4: Run now = workflow.run');
    ok(wbtn('Schedule').disabled && dr().includes('Schedule is off') && dr().includes('a schedule and a ceiling'), 'RV-D: Schedule stays off for a workflow with no schedule or ceiling, and says why');
    WF_ROWS.length = 0;
    $('wfTaskText').value = 'split the office work'; await wbtn('Propose').onclick({}); await tick(20);
    await wbtn('Keep').onclick({}); await tick(20); closeSheets();
    ok(JSON.stringify(cdesk()) === JSON.stringify({id: 'workflow.new-office', params: {name: 'office-split', folder: '/work/u/proj-one', prompt: 'split the office work'}}), 'U4: Keep an office = workflow.new-office: ' + JSON.stringify(cdesk()));
    $('wfTaskText').value = 'garbage'; await wbtn('Propose').onclick({}); await tick(20);
    ok($('wfDraft').querySelectorAll('.err').length === 1 && dr().includes('not a task I can read') && !wbtn('Keep'), 'U4: a task propose cannot read: its words, no Keep');
    $('wfTaskText').value = 'odd steps'; const oddErr = await wbtn('Propose').onclick({}).then(() => null, e => e); await tick(20);
    ok(!oddErr && !!wbtn('Keep') && dr().includes('ask · on claude'), 'U4: a draft whose steps hold a non-object draws from the steps it can read: ' + (oddErr ? oddErr.message : dr().slice(0, 90)));
  }
  // routines fire while metabot serve runs: the page says whether it does
  const rt = () => $('page').querySelectorAll('.card').find(c => c.textContent.startsWith('Routines'));
  ok(rt() && rt().textContent.includes('not running') && !!wbtn('Start serve'), 'U4: Routines say serve is not running and offer Start serve');
  if (wbtn('Start serve')) { await wbtn('Start serve').onclick({}); await tick(20); closeSheets(); }
  ok(JSON.stringify(cdesk()) === JSON.stringify({id: 'serve', params: {}}), 'U4: Start serve opens metabot serve in a desk');
  XT.serve.running = true; XT.serve.desk = 'serve'; EXTEND = null; renderPage('workflows'); await tick(30);
  ok(rt() && rt().textContent.includes('runs') && !rt().textContent.includes('not running') && !wbtn('Start serve'), 'U4: with serve running the page says so and offers no second one');
  XT.serve.running = false; XT.serve.desk = ''; EXTEND = null;
  // the Workflows side bar leads with the guided card, which owns propose, keep and serve (the module lists the catalogue alone)
  abTap('workflows'); await tick(20);
  const wsr = $('sideBody').querySelectorAll('.srow');
  ok(wsr.length >= 2 && wsr[0].textContent.startsWith('From a task') && !wsr.some(r => /^(Propose|Start serve)/.test(r.textContent)), 'U4: the Workflows side bar leads with From a task: ' + wsr.map(r => r.textContent).join(' | '));
  closeTab('page', 'workflows'); await tick(5);
  if (wsr[0]) { wsr[0].onclick({}); await tick(20); }
  ok(ACTIVE && ACTIVE.id === 'workflows' && document.activeElement === $('wfTaskText'), 'U4: From a task opens the page with the task box focused');
  closeTab('page', 'workflows'); await tick(5);
  $('addBtn').onclick({preventDefault() {}}); await tick(20);
  ok(ACTIVE && ACTIVE.id === 'workflows' && document.activeElement === $('wfTaskText'), 'U4: the side bar + is From a task too');
  abTap('chats'); await tick(5);
  // desktop panel: install card when vnc is down (with the migration hint), noVNC hosted when up, header words
  openDesk(); await tick(10);
  ok(DOCK.id === 'desktop' && $('dockBody').textContent.includes('apt-get install') && $('dockBody').textContent.includes('metachat-gui') && $('dockBody').textContent.includes('fc-gui'), 'desktop panel shows the install card naming metachat-gui, with the fc-gui migration hint');
  ok($('deskStatus').textContent === 'display down' && $('deskStatus').classList.contains('bad'), 'desktop header: display down');
  META.display.vnc = true; openDesk(); await tick(10);
  ok($('deskPanel').parent === $('dockBody') && !$('deskPanel').classList.contains('hidden'), 'desktop up: the noVNC frame lives inside the panel');
  ok($('deskFrame').attrs.src && $('deskFrame').attrs.src.includes('/vendor/novnc/vnc.html'), 'the frame points at the vendored noVNC');
  ok($('deskStatus').textContent === ':99 · loading…', 'header while the frame loads: ' + $('deskStatus').textContent);
  $('deskFrame').contentDocument = { documentElement: { className: 'noVNC_connected' }, getElementById: () => ({ textContent: '' }), querySelector: () => ({ width: 1280, height: 800 }) };
  pollDeskStatus(); ok($('deskStatus').textContent === ':99 · 1280x800 · connected' && $('deskStatus').classList.contains('ok'), 'header when connected: ' + $('deskStatus').textContent);
  $('deskFrame').contentDocument = { documentElement: { className: 'noVNC_disconnected' }, getElementById: () => ({ textContent: 'Failed to connect to server' }), querySelector: () => null };
  pollDeskStatus(); ok($('deskStatus').textContent.startsWith('bridge refused') && $('deskStatus').classList.contains('bad'), 'header when the bridge refuses: ' + $('deskStatus').textContent);
  $('deskFrame').contentDocument = null; DESK.since = Date.now() - 9000; pollDeskStatus();
  ok($('deskStatus').textContent === 'noVNC did not load — reload the page or reinstall the app', 'header when noVNC never loaded: ' + $('deskStatus').textContent);
  $('deskClose').onclick(); await tick(5);
  ok($('deskPanel').classList.contains('hidden') && $('deskPanel').parent === document.body && !dockOpen(), 'closing the panel parks the frame hidden (connection kept)');
  $('deskBtn').onclick(); await tick(10);
  ok(DOCK.id === 'desktop' && !dock.classList.contains('hidden') && $('deskPanel').parent === $('dockBody'), 'the header Desktop button opens the panel');
  const tmrs = dockTimers.length; setUMode('user'); await tick(5); setUMode('dev'); await tick(5);
  ok(dockTimers.length === tmrs, 'a mode switch re-renders the open panel without leaking timers (finding ui-state-7)');
  closePanel(); ok(dockTimers.length === 0, 'closing the panel clears its timers');
  setUMode('user'); setUMode('dev'); ok(dockTimers.length === 0 && $('deskPanel').classList.contains('hidden'), 'a mode switch with the panel closed renders nothing (no hidden noVNC)');
  META.display.vnc = false;
  // tools page lists module errors; demo view
  openPage('tools'); await tick(10);
  ok($('page').textContent.includes('unknown catalog id x.y') && $('page').textContent.includes('MetaBot'), 'tools page shows module errors and says MetaBot');
  setDemo(true); await tick(20);
  ok(DEMO && localStorage.fcDemo === '1' && LOG.gets.some(x => x === '/api/desks?scope=metachat&t=tok'), 'demo view fetches scope=metachat');
  setDemo(false); await tick(20);
  // portrait: the side bar is a left sheet with a scrim; a pick closes it (finding ui-layout-1)
  globalThis.innerWidth = 876; globalThis.innerHeight = 1400;
  abTap('chats'); await tick(5);
  ok(document.body.classList.contains('sideOpen') && $('scrim').classList.contains('on'), 'portrait: tapping Chats opens the side sheet with the scrim');
  setWS('desk-a'); await tick(5);
  ok(!document.body.classList.contains('sideOpen') && !$('scrim').classList.contains('on'), 'portrait: picking a desk closes the side sheet');
  abTap('chats'); await tick(5); $('scrim').fire('click'); ok(!document.body.classList.contains('sideOpen'), 'a tap on the scrim closes it');
  globalThis.innerWidth = 1400; globalThis.innerHeight = 876;
  // landscape: tapping the active activity item folds the side bar, fcSide remembers
  abTap('chats'); await tick(5); ok($('side').classList.contains('collapsed') && localStorage.fcSide === 'closed', 'landscape: tapping the active item folds the side bar');
  abTap('chats'); await tick(5); ok(!$('side').classList.contains('collapsed') && localStorage.fcSide === 'open', 'and again unfolds it');
  // Ctrl+K focuses the side search
  docFire('keydown', {ctrlKey: true, key: 'k', preventDefault() {}}); ok(document.activeElement === $('sideSearch'), 'Ctrl+K focuses the search');
  // a desk kill disposes its terminal and closes its tab
  const dk = DISPOSED; setWS('plugin-new'); await tick(5); await killDesk('plugin-new'); await tick(10);
  ok(DISPOSED === dk + 1 && !TERMS['plugin-new'] && !TABS.some(t => t.id === 'plugin-new') && LOG.posts.some(q => q.path === '/api/kill'), 'kill posts /api/kill, disposes the terminal and closes the tab');
  // a corrupt localStorage value never takes the page down (finding ui-network-11)
  ok(typeof readJSON === 'function' && readJSON('nope', {a: 1}).a === 1, 'readJSON falls back on a missing key');
  localStorage.fcTree = '{bad'; ok(JSON.stringify(readJSON('fcTree', {})) === '{}' && !('fcTree' in localStorage), 'readJSON drops a corrupt key');
  // ==================== review 1.2 safety checks (RV-A, RV-A2, RV-B, RV-K, from the 11-09 UI review) ====================
  setUMode('user'); await tick(20); closeSheets(); closePanel(); abTap('chats'); await tick(10);
  const rvKeys = () => LOG.posts.filter(q => q.path === '/api/keys');
  // RV-A: the PC classed the ask destructive; the row says "Answer in the terminal"; in user mode that opens the prompt
  // sheet, which re-reads the class from the screen with the page's own regex and offers a one-tap Allow (Enter).
  {
    const SCREEN = ['Bash command', '  git push origin main -f', '  Force push the rewritten history to main', 'Do you want to proceed?', '❯ 1. Yes', '  2. No, and tell Claude what to do differently (esc)'];
    const rec = TERMS['claude-2'] || termFor('claude-2', $('sheetTerm'));
    rec.term.rows = 24;
    rec.term.buffer = {active: {baseY: 0, getLine: vy => { const i = vy - (24 - SCREEN.length); return i >= 0 && i < SCREEN.length ? {translateToString: () => SCREEN[i]} : null; }}};
    TREE = {waiting: [{desk: 'claude-2', run: null, cls: 'destructive', ask: 'select', ask_line: '❯ 1. Yes', since: 0}], projects: [], other: [], ended: []}; renderTree();
    const rowWords = wkText(0);
    const nA = rvKeys().length;
    const b = wkeys()[0] && wkeys()[0].querySelectorAll('.wk')[0];
    if (b) { b.onclick({}); await tick(40); }
    const sheetBtns = $('sheetKeys').querySelectorAll('button').map(x => x.textContent);
    ok(rowWords === 'Answer in the terminal' && !sheetBtns.includes('Allow') && sheetBtns.includes('Send yes'),
      'RV-A: a destructive ask (the PC class) is never one tap: row [' + rowWords + '] -> sheet ' + $('sheetCls').textContent + ' keys [' + sheetBtns.join(',') + ']');
    const allow = $('sheetKeys').querySelectorAll('button').find(x => x.textContent === 'Allow');
    if (allow && rec.ws) { rec.ws.readyState = 1; const n0 = rec.ws.sent.length; allow.onclick({}); ok(!rec.ws.sent.slice(n0).includes('\\r'), 'RV-A: the sheet Allow does not type Enter into the destructive ask: sent ' + JSON.stringify(rec.ws.sent.slice(n0))); }
    closeSheets(); setWS('desk-a'); await tick(5);
  }
  // RV-A2: the same destructive ask on a desk this page has never held (the normal user-mode case: no terminal panel).
  // The sheet opens with fillKeys('') — the permissive set — and promptClass reads an empty screen, so nothing refills it.
  {
    const NAME = 'stray';
    const held = TERMS[NAME]; delete TERMS[NAME];
    TREE = {waiting: [{desk: NAME, run: null, cls: 'destructive', ask: 'select', ask_line: '❯ 1. Yes', since: 0}], projects: [], other: [], ended: []}; renderTree();
    const rowWords = wkText(0);
    const b = wkeys()[0] && wkeys()[0].querySelectorAll('.wk')[0];
    if (b) { b.onclick({}); await tick(40); }
    const sheetBtns = $('sheetKeys').querySelectorAll('button').map(x => x.textContent);
    ok(rowWords === 'Answer in the terminal' && !sheetBtns.includes('Allow') && sheetBtns.includes('Send yes'),
      'RV-A2: a destructive ask (the PC class) on a desk the page holds no screen for is never one tap: row [' + rowWords + '] -> sheet class "' + $('sheetCls').textContent + '" keys [' + sheetBtns.join(',') + ']');
    closeSheets(); if (held) TERMS[NAME] = held; else delete TERMS[NAME]; setWS('desk-a'); await tick(5);
  }
  // RV-B: a key stays live after its send: a second tap answers whatever the desk asks next, which the row never showed.
  {
    delete SENT_ASK['run-3'];   // a fresh ask on run-3 (the U6 checks above answered one with the same line)
    TREE = {waiting: [{desk: 'run-3', run: 'live-1', cls: 'write', ask: 'line', ask_line: "allow? type 'y' to allow, anything else declines:", since: 0}], projects: [], other: [], ended: []}; renderTree();
    const yb = wkeys()[0].querySelectorAll('.wk')[0];
    const n0 = rvKeys().length;
    await yb.onclick({currentTarget: yb}); await tick(5);
    const stillLive = !yb.disabled && !!yb.parent && wkeys().length === 1;
    await yb.onclick({currentTarget: yb}); await tick(5);
    ok(rvKeys().length - n0 === 1, 'RV-B: one ask takes one answer: the y key posted ' + (rvKeys().length - n0) + ' times (live after the first: ' + stillLive + ')');
  }
  closeSheets(); setWS('desk-a'); await tick(5);
  // RV-C/RV-D: From a task keeps a different workflow from the draft it shows. The fake PC's review draft is MetaBot's shape:
  // its ask step carries role review and read_only true (propose.py _ask); Keep posts workflow.new with neither.
  {
    openPage('workflows'); await tick(30);
    $('wfTaskText').value = 'review the parser'; $('wfFolder').value = '/work/u/proj-one';
    await wbtn('Propose').onclick({}); await tick(20);
    const shown = dr();
    await wbtn('Keep').onclick({}); await tick(20); closeSheets();
    const kept = cdesk();
    ok(!(kept.id === 'workflow.new' && !('read_only' in kept.params) && !('role' in kept.params)),
      'RV-C: Keep keeps the draft it shows (it shows read-only: ' + shown.includes('read-only') + ', role review: ' + shown.includes('role review') + ', hash 9f86d081884c: ' + shown.includes('9f86d081884c') + '): posted ' + JSON.stringify(kept));
    await wbtn('Approve').onclick({}); await tick(20); closeSheets();
    const sch = wbtn('Schedule');
    ok(!(sch && !sch.disabled), 'RV-D: Schedule is not offered for a kept workflow with no schedule or ceiling (MetaBot refuses it): Schedule ' + (sch ? (sch.disabled ? 'disabled' : 'enabled') : 'absent'));
    closeTab('page', 'workflows'); await tick(5);
  }
  // RV-E: Try it on a bridge enabled after the engine chip first loaded: the chip list is cached for the session
  {
    setMode('msg'); await loadRunChips(); $('chipEngine').value = 'claude';
    XT.engines.push({name: 'mybr', kind: 'bridge', local: false, models: [], plugin: 'mybr@mybr'});
    EXTEND = null; openPage('plugins'); await tick(30);
    const mrow = sec('engines').querySelectorAll('.xn').find(r => (r.querySelectorAll('.xt')[0] || {}).textContent === 'mybr');
    if (mrow) { await cbtn(mrow, 'Try it').onclick({}); await tick(40); }
    ok(!!mrow && $('chipEngine').value === 'mybr', 'RV-E: Try it on the new bridge chooses it: listed ' + !!mrow + ', chip ' + $('chipEngine').value + ', pill "' + $('runPill').textContent + '"');
    closeSheets(); setMode('msg'); XT.engines.pop(); EXTEND = null;
  }
  // RV-F: Make works out the marketplace name from whatever /api/extend it holds: when that read failed (or is stale), a name
  // another folder holds is reused, and "Add as marketplace" replaces that marketplace (MetaBot add_marketplace overwrites)
  {
    XT_DOWN = true; EXTEND = null; openPage('plugins'); await tick(30);
    const held0 = JSON.stringify(EXTEND);
    const k0 = cards()[0], nF = LOG.posts.length;
    k0.querySelectorAll('input')[0].value = 'notes'; k0.querySelectorAll('select')[0].value = '/work/u/notes';
    await cbtn(k0, 'Make the skill').onclick({}); await tick(20); closeSheets();
    if (cbtn(k0, 'Add as marketplace')) { await cbtn(k0, 'Add as marketplace').onclick({}); await tick(20); closeSheets(); }
    // ported (review 1.2): the fix refuses Make while /api/extend does not read, so "posts notes-2" became "never posts notes, and says why"
    const addsF = LOG.posts.slice(nF).filter(q => q.path === '/api/cli/desk' && q.body.id === 'plugin.marketplace.add');
    ok(!addsF.some(q => q.body.params.name === 'notes') && LOG.posts.slice(nF).every(q => q.path !== '/api/cli/desk' || q.body.id !== 'plugin.new') && k0.querySelectorAll('.err').some(e => /did not read/.test(e.textContent)), 'RV-F: the PC holds marketplace notes for /work/u/proj-one/notes, so metabot names this one notes-2; the page held ' + held0 + ' and posts ' + JSON.stringify(cdesk()));
    XT_DOWN = false; EXTEND = null; openPage('plugins'); await tick(30);
    const k1 = cards()[0];
    k1.querySelectorAll('input')[0].value = 'alpha'; k1.querySelectorAll('select')[0].value = '/work/u/proj-one';
    EXTEND = JSON.parse(JSON.stringify(EXTEND));   // a real reply is parsed: the page holds a copy, not the PC's live object
    await cbtn(k1, 'Make the skill').onclick({}); await tick(20); closeSheets();
    await cbtn(k1, 'Add as marketplace').onclick({}); await tick(20); closeSheets();
    const first = JSON.stringify(cdesk());
    XT.marketplaces.push({name: 'alpha', folder: '/work/u/proj-one/alpha', plugins: [{name: 'alpha', description: 'the alpha skill'}], error: null});   // the PC now holds alpha
    k1.querySelectorAll('input')[0].value = 'alpha'; k1.querySelectorAll('select')[0].value = '/work/u/notes';
    await cbtn(k1, 'Make the skill').onclick({}); await tick(20); closeSheets();
    await cbtn(k1, 'Add as marketplace').onclick({}); await tick(20); closeSheets();
    ok(cdesk().params && cdesk().params.name === 'alpha-2', 'RV-F: a second alpha in another folder, same page view: metabot says alpha-2; first ' + first + ', then the page posts ' + JSON.stringify(cdesk()));
    XT.marketplaces.pop(); EXTEND = null;
  }
  // RV-G: after an enable the Installed switch keeps the old state: nothing re-reads /api/extend (client 10 s, then the PC's own 10 s)
  {
    EXTEND = null; openPage('plugins'); await tick(30);
    EXTEND = JSON.parse(JSON.stringify(EXTEND));   // a real reply is parsed: the page holds a copy
    const echoRow = () => sec('installed').querySelectorAll('.xn').find(r => (r.querySelectorAll('.xt')[0] || {}).textContent === 'echo');
    const nGet = LOG.gets.filter(x => x.startsWith('/api/extend')).length;
    await echoRow().querySelectorAll('.sw')[0].onclick({stopPropagation() {}}); await tick(20);
    XT.plugins[1].enabled = true;   // the person answered the desk; the PC now has echo on
    closeSheets(); renderPage('plugins'); await tick(30);
    const on = echoRow().querySelectorAll('.sw')[0].getAttribute('aria-checked');
    ok(on === 'true', 'RV-G: after the enable desk the switch reads the PC again: aria-checked ' + on + ', GET /api/extend since: ' + (LOG.gets.filter(x => x.startsWith('/api/extend')).length - nGet));
    EXTEND_AT = Date.now() - 11000; renderPage('plugins'); await tick(30);   // past the page's own 10 s
    const later = LOG.gets.filter(x => x.startsWith('/api/extend')).slice(-1)[0] || '';
    ok(/fresh=1/.test(later), 'RV-G: the read after an enable skips the PC cache (?fresh=1), else the PC answers from its own 10 s copy: ' + later);
    XT.plugins[1].enabled = false; EXTEND = null;
  }
  // RV-H: user mode shows paths and versions (21.6: user mode never shows a path, an argv or a version)
  {
    ACCOUNTS = null; openPage('accounts'); await tick(30);
    const acc = $('page').textContent;
    EXTEND = null; openPage('plugins'); await tick(30);
    const xi0 = sec('installed').querySelectorAll('.xn')[0]; if (xi0 && sec('installed').querySelectorAll('.xfacts')[0].classList.contains('hidden')) xi0.onclick({});
    const ext = $('page').textContent;
    const leaks = [acc.includes('~/.claude') && '~/.claude (Accounts)', acc.includes('~/.config/cursor') && '~/.config/cursor (Accounts)', ext.includes('/work/u/proj-one/team') && '/work/u/proj-one/team (Marketplaces)', ext.includes('0.1.0') && '0.1.0 (plugin facts)', ext.includes('/s/modules/pingmod.json') && 'module file (This app)'].filter(Boolean);
    ok(UMODE === 'user' && !leaks.length, 'RV-H: user mode shows no path and no version: ' + leaks.join(' | '));
    closeTab('page', 'plugins'); closeTab('page', 'accounts');
  }
  setUMode('dev'); await tick(20);
  // RV-K: the class is read from the last 6 non-empty lines (mb.NEEDS_LINES on the PC, lastLines(rec, 6) here). A Claude menu with three
  // options and a two-line description puts the command on line 7: the PC row is {cls: read, ask: select} (mb.ask_class / ask_of on that
  // screen), and even with the whole screen held here the row offers a one-tap Enter, which picks "1. Yes" and runs rm -rf.
  setUMode('user'); await tick(20); closeSheets(); closePanel(); abTap('chats'); await tick(10);
  {
    const SCREEN = ['Bash command', '', '  rm -rf build/ dist/', '  Remove the old build and dist folders before the', '  release build runs again', '', 'Do you want to proceed?', '❯ 1. Yes', "  2. Yes, and don't ask again for rm commands in /work/u/proj", '  3. No, and tell Claude what to do differently (esc)'];
    const rec = TERMS['claude-2'] || termFor('claude-2', $('sheetTerm'));
    rec.term.rows = 24;
    rec.term.buffer = {active: {baseY: 0, getLine: vy => { const i = vy - (24 - SCREEN.length); return i >= 0 && i < SCREEN.length ? {translateToString: () => SCREEN[i]} : null; }}};
    TREE = {waiting: [{desk: 'claude-2', run: null, cls: 'read', ask: 'select', ask_line: '❯ 1. Yes', since: 0}], projects: [], other: [], ended: []}; renderTree();
    ok(wkText(0) !== 'Enter,Esc', 'RV-K: a Claude menu whose command (rm -rf) sits above the last 6 lines is never one tap, the page holding the whole screen: row [' + wkText(0) + '], held-screen class ' + promptClass(rec));
  }
  closeSheets();
  // RV-I: a bridge or a local engine on the Run chip: the pill and the equals line name a Claude login the PC drops (mb.start_run uses account for claude and codex alone)
  {
    await loadRunChips(); $('chipEngine').value = 'echo'; $('chipAccount').value = 'work'; accountChipVisibility(); renderRunPill();
    ok(!/work/.test($('runPill').textContent) && !/--login/.test($('runEquals').textContent), 'RV-I: Run on bridge echo names no login: pill "' + $('runPill').textContent + '" · ' + $('runEquals').textContent);
    $('chipEngine').value = 'claude'; $('chipAccount').value = ''; renderRunPill();
  }
  // RV-J: Setup counts a sign-in desk as "A desk" (meta.kind is claude or cursor on a sign-in desk too)
  {
    const keepD = window.DESKS;
    window.DESKS = [keepD.find(d => d.name === 'desk-a'), {name: 'signin-claude-work', armed: false, armed_at: 0, n: 0, pane: true, reach: 'pane', cli: 'claude', meta: {kind: 'claude', account: 'work', signin: true}, project: 'other', cwd: '/s/signin', needs_you: false, signin: {engine: 'claude', account: 'work', step: 'style', url: null}}];
    openPage('setup'); await tick(20);
    const st = $('page').querySelectorAll('.step')[2];
    ok(st && !st.classList.contains('done'), 'RV-J: with only the default desk and a sign-in desk, Setup step 3 (A desk) is not done: ' + (st ? st.textContent + (st.classList.contains('done') ? ' [done]' : '') : 'no step'));
    window.DESKS = keepD; closeTab('page', 'setup'); await tick(5);
  }
  setUMode('dev'); await tick(20);
  // RV-H2: user mode, an Extend step opens the prompt sheet with its argv (absolute folders) on the equals line
  setUMode('user'); await tick(20);
  {
    EXTEND = null; openPage('plugins'); await tick(30);
    const mf = sec('markets').querySelectorAll('details')[0];
    const ins = mf ? mf.querySelectorAll('input') : [];
    if (ins.length === 2) { ins[0].value = 'team2'; ins[1].value = '/work/u/proj-one/team2'; await mf.querySelectorAll('button')[0].onclick({}); await tick(20); }
    ok(UMODE === 'user' && !/\\/home\\/u\\//.test($('sheetEquals').textContent), 'RV-H2: user mode shows no argv or path on the prompt sheet: "' + $('sheetEquals').textContent + '"');
    closeSheets(); closeTab('page', 'plugins');
  }
  setUMode('dev'); await tick(20);
  // probe (expected to pass): the page's own check on a sign-in link
  {
    const bad = ['javascript:alert(1)', 'data:text/html,x', 'https://claude.ai@evil.example/x', 'https://evil.example/#https://claude.ai/', 'https://claude.ai.evil.example/x',
      'https://claude.ai\\\\@evil.example/', 'https://claude.ai%40evil.example/', 'https://claude.ai/x y', 'https://claude.ai/x"onmouseover=1', ' https://claude.ai/x', 'https://claude.ai:443@evil.example/', 'HTTPS://evil.example/claude.ai'];
    const good = ['https://claude.ai/oauth/authorize?code=true&x=1', 'https://CLAUDE.COM/cai/oauth', 'https://cursor.com/loginDeepControl?challenge=a&mode=login', 'https://claude.ai/\\\\evil.example'];
    const passBad = bad.filter(u => signinUrl({url: u}));
    const failGood = good.filter(u => !signinUrl({url: u}));
    ok(!passBad.length && !failGood.length, 'probe: signinUrl refuses every hostile link and keeps the sign-in hosts: let through ' + JSON.stringify(passBad) + ', refused ' + JSON.stringify(failGood));
  }
  // ==================== end of reviewer checks ====================
  // ==================== review 1.2 round: the findings without a reviewer check ====================
  // the manual "Add a marketplace folder" form: a name another folder holds is refused (metabot would repoint it)
  {
    EXTEND = null; openPage('plugins'); await tick(30);
    const mf = sec('markets').querySelectorAll('details')[0], ins = mf.querySelectorAll('input'), nM = LOG.posts.length;
    ins[0].value = 'team'; ins[1].value = '/work/u/other/team'; await mf.querySelectorAll('button')[0].onclick({}); await tick(20); closeSheets();
    ok(!LOG.posts.slice(nM).some(q => q.path === '/api/cli/desk') && mf.querySelectorAll('.err').some(e => /held by another/.test(e.textContent)), 'review 1.2 RV-F: the manual form refuses a name another folder holds: ' + mf.querySelectorAll('.err').map(e => e.textContent).join(' | '));
    ins[0].value = 'team'; ins[1].value = '/work/u/proj-one/team'; await mf.querySelectorAll('button')[0].onclick({}); await tick(20); closeSheets();
    ok(JSON.stringify(cdesk()) === JSON.stringify({id: 'plugin.marketplace.add', params: {name: 'team', folder: '/work/u/proj-one/team'}}), 'review 1.2 RV-F: the same name on its own folder still adds: ' + JSON.stringify(cdesk()));
    closeTab('page', 'plugins'); abTap('chats'); await tick(10);
  }
  // RV-G in use: closing the plugin desk sheet reads the PC again and redraws the lists, with no Refresh tap and no page redraw
  {
    EXTEND = null; openPage('plugins'); await tick(30);
    const mk = cards()[0];
    const echoSw = () => sec('installed').querySelectorAll('.xn').find(r => (r.querySelectorAll('.xt')[0] || {}).textContent === 'echo').querySelectorAll('.sw')[0];
    await echoSw().onclick({stopPropagation() {}}); await tick(20);
    XT.plugins[1].enabled = true;
    closeSheets(); await tick(30);
    ok(echoSw().getAttribute('aria-checked') === 'true' && cards()[0] === mk, 'review 1.2 RV-G: closing the enable desk redraws the switch from a fresh read; the Make cards stay as they are');
    XT.plugins[1].enabled = false; EXTEND = null; closeTab('page', 'plugins'); abTap('chats'); await tick(10);
  }
  // item 14: Arm names what the PC types (the inbox's arm command for the desk), then reads the desk rows until armed, or says so after the wait
  {
    const row = {name: 'arm-me', armed: false, armed_at: 0, n: 0, pane: true, reach: 'pane', cli: 'claude', meta: {kind: 'claude'}, project: 'proj-one', cwd: '/work/u/proj-one', needs_you: false};
    FIX_DESKS.push(row); await fetchDesks();
    const keepConfirm = globalThis.confirm, keepPoll = Object.assign({}, ARM_POLL);
    let asked = ''; globalThis.confirm = q => { asked = String(q); return true; };
    Object.assign(ARM_POLL, {every: 10, upTo: 400});
    const nPane = LOG.posts.filter(q => q.path === '/api/pane').length;
    reachAction('arm-me'); await tick(20);
    ok(asked.includes('Arm chat for ' + disp('arm-me') + '?') && asked.includes('hold the test inbox as "arm-me"') && !asked.includes('Types /test-arm into'), 'item 14: the Arm confirm names the inbox name the PC arms it as: ' + JSON.stringify(asked));
    ok(JSON.stringify((LOG.posts.filter(q => q.path === '/api/pane').at(-1) || {}).body) === JSON.stringify({target: 'arm-me', action: 'arm'}) && LOG.posts.filter(q => q.path === '/api/pane').length === nPane + 1, 'item 14: Arm posts /api/pane {target, action: arm}');
    const nGets = LOG.gets.filter(x => x.startsWith('/api/desks')).length;
    await tick(60);
    ok(LOG.gets.filter(x => x.startsWith('/api/desks')).length > nGets && !/^armed/.test($('toast').textContent), 'item 14: after the arm the page reads the desk rows again, and says nothing of armed before a row does: ' + $('toast').textContent);
    row.armed = true; row.reach = 'inbox';
    ok(await until(() => /^armed/.test($('toast').textContent), 400), 'item 14: the row shows armed: true, the toast says armed: ' + $('toast').textContent);
    await tick(60);   // the page's own desk poll may read /api/desks too, so the arm poll's timer is what is checked
    ok(!ARM_WAIT['arm-me'], 'item 14: the poll stops once the desk is armed');
    row.armed = false; row.reach = 'pane'; await fetchDesks(); closeSheets();
    reachAction('arm-me'); await tick(20);
    ok(await until(() => $('toast').textContent === 'not armed yet: open the desk to see why', 1500), 'item 14: no armed row within the wait: "not armed yet: open the desk to see why": ' + $('toast').textContent);
    globalThis.confirm = keepConfirm; Object.assign(ARM_POLL, keepPoll);
    FIX_DESKS.splice(FIX_DESKS.indexOf(row), 1); await fetchDesks(); closeSheets(); setWS('desk-a'); await tick(5);
  }
  // B1: with no inbox adapter (META.inbox null) the page offers no arm and names no inbox
  {
    const row = {name: 'no-inbox', armed: false, armed_at: 0, n: 0, pane: true, reach: 'pane', cli: 'claude', meta: {kind: 'claude'}, project: 'proj-one', cwd: '/work/u/proj-one', needs_you: false};
    const far = {name: 'far-away', armed: false, armed_at: 0, n: 0, pane: false, reach: 'none', cli: '', meta: {}, project: 'other', cwd: '', needs_you: false};
    FIX_DESKS.push(row, far); await fetchDesks();
    const keepInbox = META.inbox, keepConfirm = globalThis.confirm;
    let asked = ''; globalThis.confirm = q => { asked = String(q); return false; };
    META.inbox = null;
    setWS('no-inbox'); await tick(5); deskMenu();
    const items = kids($('menu')).map(b => b.textContent);
    ok(!items.some(t => /Arm chat/.test(t)), 'B1: no inbox, no Arm chat in the desk menu: ' + items.join(' | '));
    hideMenus();
    const nPane = LOG.posts.filter(q => q.path === '/api/pane').length;
    reachAction('no-inbox'); await tick(5);
    ok(asked === '' && $('toast').textContent === 'messages are typed into its claude' && LOG.posts.filter(q => q.path === '/api/pane').length === nPane, 'B1: no inbox: the reach chip says chat is typed, asks nothing, arms nothing: ' + $('toast').textContent);
    reachAction('far-away'); await tick(5);
    ok($('toast').textContent === 'that session runs outside the tablet desks', 'B1: no inbox: no arm command is suggested: ' + $('toast').textContent);
    text.value = '/'; palette();
    ok(!rows().some(r => r.querySelectorAll('.pk')[0].textContent === '/arm'), 'B1: no inbox, no /arm in the palette');
    hidePal(); await slash('/arm'); await tick(5);
    ok(LOG.posts.filter(q => q.path === '/api/pane').length === nPane && /no chat inbox/.test($('toast').textContent), 'B1: a typed /arm with no inbox posts nothing and says why: ' + $('toast').textContent);
    META.inbox = keepInbox; globalThis.confirm = keepConfirm; text.value = '';
    FIX_DESKS.splice(FIX_DESKS.indexOf(row), 1); FIX_DESKS.splice(FIX_DESKS.indexOf(far), 1); await fetchDesks(); closeSheets(); setWS('desk-a'); await tick(5);
  }
  // the tree: a folder with no desks and no runs is not listed; projects with live desks lead
  {
    const keepT = TREE, dA = deskInfo('desk-a');
    TREE = {waiting: [], other: [], ended: [], projects: [
      {name: 'aaa-empty', folders: [{path: '/work/u/aaa-empty', desks: [], runs: []}]},
      {name: 'bbb-runs', folders: [{path: '/work/u/bbb', desks: [], runs: [{run_id: 'r-old', state: 'done', receipt_state: 'valid'}]}, {path: '/work/u/bbb-2', desks: [], runs: []}]},
      {name: 'proj-one', folders: [{path: '/work/u/proj-one', desks: [dA], runs: []}]}]};
    renderTree();
    const pk = treeRows().filter(r => r.dataset.kind === 'node' && /^p:/.test(r.dataset.key || '')).map(r => r.dataset.key);
    ok(!$('sideBody').textContent.includes('no sessions here') && !pk.includes('p:aaa-empty') && !node('f:bbb-runs:/work/u/bbb-2') && !!node('f:bbb-runs:/work/u/bbb'), 'review 1.2 tree: no empty folder and no empty project is listed: ' + pk.join(','));
    ok(pk.join(',') === 'p:proj-one,p:bbb-runs', 'review 1.2 tree: a project with live desks comes first: ' + pk.join(','));
    SEARCH_Q = 'aaa'; renderTree();
    ok(!!node('p:aaa-empty') && !!node('f:aaa-empty:/work/u/aaa-empty'), 'review 1.2 tree: a search that names an empty project or folder still finds it');
    SEARCH_Q = ''; TREE = keepT; renderTree();
  }
  {   // 11-09 resume: a send to an ended Claude session offers to resume it, waits for its CLI, then sends
    DESKS.push({name: 'old-chat', armed: false, armed_at: 0, n: 0, pane: false, reach: 'none', cli: 'claude', meta: {}, project: 'other', cwd: '', needs_you: false, resume: true});
    await fetchDesks();
    const keepW = Object.assign({}, RESUME_WAIT), keepConfirm = globalThis.confirm;
    Object.assign(RESUME_WAIT, {every: 10, upTo: 2000, settle: 10});
    let asked = ''; globalThis.confirm = q => { asked = String(q); return true; };
    ok(deskState(deskInfo('old-chat')) === 'ended · tap to resume', 'resume: an ended session says so in the tree: ' + deskState(deskInfo('old-chat')));
    switchDesk('old-chat'); await tick(5);
    const n0 = LOG.posts.length;
    const sent = await sendMessage('pick up where we left off');
    const ps = LOG.posts.slice(n0).map(q => q.path + ':' + (q.body.action || q.body.text || ''));
    ok(/has ended/.test(asked), 'resume: the failed send asks to resume: ' + asked);
    ok(ps.join('|') === '/api/send:pick up where we left off|/api/pane:resume|/api/send:pick up where we left off', 'resume: send, resume, then the same text again: ' + ps.join('|'));
    ok(sent === true && WS === 'old-chat', 'resume: the second send went through on the resumed desk');
    const d = DESKS.find(x => x.name === 'old-chat'); Object.assign(d, {reach: 'none', resume: false}); await fetchDesks();
    const n1 = LOG.posts.length; asked = '';
    await sendMessage('again');
    ok(LOG.posts.length - n1 === 1 && !asked, 'resume: a desk with nothing saved is not offered a resume');
    DESKS.splice(DESKS.indexOf(d), 1); await fetchDesks();
    Object.assign(RESUME_WAIT, keepW); globalThis.confirm = keepConfirm;
  }
  {   // 11-09 replies in the chat: a mirrored Claude answer is the desk talking, named once, rendered as markdown
    const b = bubble({via: 'desk', time: 1730000100, message: 'Done — **two** files changed', target: 'desk-a', from: 'desk-a', typed: false, mirror: true});
    const who = b.querySelector('.who');
    ok(b.classList.contains('in') && who && who.textContent === disp('desk-a'), 'reply mirror: the bubble is the desk, named once: ' + (who && who.textContent));
    ok(b.querySelector('.body').innerHTML.includes('<strong>two</strong>') && !b.querySelector('.chip'), 'reply mirror: bold renders (escaped first) and no typed chip: ' + b.querySelector('.body').innerHTML);
    ok(md('**<b>x</b>**').indexOf('<b>') < 0, 'reply mirror: bold never lets markup through');
  }
  {   // 11-09 a run tells its own lane how it ended: the answer, then one receipt line that opens the receipt viewer
    const b = bubble({via: 'desk', time: 1730000200, message: 'There are three files.', target: 'run-4', from: 'run-4', typed: false, mirror: true,
      run: {id: 'mb-x1', receipt_state: 'valid', status: 'ok', steps: 1, cost_musd: 0, cost_source: 'local', engine: 'openai', model: 'small'}});
    const rl = b.querySelector('.runline');
    ok(rl && rl.textContent.includes('receipt verifies') && rl.textContent.includes('1 step') && rl.textContent.includes('$0 local'), 'run report: one receipt line under the answer: ' + (rl && rl.textContent));
    const btn = rl && rl.querySelector('button');
    if (btn) btn.onclick({stopPropagation() {}}); await tick(10);
    ok(DOCK.id === 'run:mb-x1', 'run report: the line opens the receipt of that run: ' + DOCK.id);
    closePanel();
    const p = bubble({via: 'desk', time: 1730000201, message: 'ok', target: 'run-5', from: 'run-5', typed: false, mirror: true,
      run: {id: 'mb-x3', receipt_state: 'valid', status: 'ok', steps: 2, cost_musd: null, cost_source: 'plan'}});
    ok(p.querySelector('.runline').textContent.includes('2 steps') && p.querySelector('.runline').textContent.includes('on your plan'), 'run report: a plan run says so, not $0: ' + p.querySelector('.runline').textContent);
    const bad = bubble({via: 'desk', time: 1730000202, message: 'The run failed: login expired', target: 'run-6', from: 'run-6', typed: false, mirror: true,
      run: {id: 'mb-x2', receipt_state: 'tampered', status: 'error', steps: 0}});
    ok(bad.querySelector('.runline').textContent.includes('not verified'), 'run report: a receipt that does not verify says so: ' + bad.querySelector('.runline').textContent);
    { const a = kv('schema', 'pass', true).querySelector('.v'), z = kv('digest', 'f'.repeat(64)).querySelector('.v');
      ok(a.classList.contains('short') && !z.classList.contains('short'), 'receipt checks: a short value (pass) never splits across lines, a digest still wraps'); }
    {   // 11-09 matrix: a login past its time limit still has its file; its last run could not sign in, so every place that offers it says sign in again
      ok(signinWord({ready: true}) === 'signed in' && signinWord({ready: false}) === 'not signed in' && signinWord({ready: false, signin_failed: {at: 'x', why: 'y'}}) === 'sign in again' && signinWord(null) === '', 'signinWord: signed in · not signed in · sign in again');
      const keepA = ACCOUNTS; ACCOUNTS = {claude: [{name: 'old', engine: 'claude', ready: false, signin_failed: {at: '2026-11-09T11:00:00Z', why: 'Failed to authenticate'}}, {name: 'ok1', engine: 'claude', ready: true}], cursor: [], defaults: {claude: {ready: true}, cursor: {ready: false}}};
      const so = spawnAccountOptions('claude');
      ok(so.some(o => o[0] === 'old' && o[1] === 'old · sign in again') && so.some(o => o[0] === 'ok1' && o[1] === 'ok1'), 'New desk: an expired login reads sign in again: ' + JSON.stringify(so));
      ok(runAccountOptions().map(o => o[1]).join('|') === 'default sign-in|login old · sign in again|login ok1', 'Run login chip: default sign-in, and an expired login says so: ' + runAccountOptions().map(o => o[1]).join('|'));
      { const box = h('div', {}); const keepL = globalThis.loadAccounts; sideAccounts(box); await tick(30);
        const txt = box.textContent; ok(txt.includes('sign in again') && !/old\s*not signed in/.test(txt), 'side list: an expired login says sign in again: ' + txt.slice(0, 120)); }
      ACCOUNTS = keepA;
    }
    {   // 11-09 tablet walk: a run desk whose run ended is a bare shell; its chip said 'start CLI' and offered to start Claude in it
      DESKS.push({name: 'run-8', armed: false, armed_at: 0, n: 0, pane: true, reach: 'idle', cli: 'shell', meta: {kind: 'run', engine: 'openai'}, project: 'other', cwd: '/tmp/x', needs_you: false});
      await fetchDesks(); const keepR = ALL_RUNS; ALL_RUNS = [{run_id: 'mb-r8', state: 'done', desk: 'run-8', receipt_state: 'valid', row: {engine: 'openai'}}];
      const keepWS = WS; setWS('run-8'); await tick(5); reachChip();
      ok($('reach').textContent === 'run ended · receipt', 'a finished run desk: the chip says the run ended, not start CLI: ' + $('reach').textContent);
      const asked0 = globalThis.confirm; let asked = ''; globalThis.confirm = q => { asked = q; return false; };
      reachAction('run-8'); await tick(10);
      ok(!asked && DOCK.id === 'run:mb-r8', 'its chip opens the receipt of its run, it never offers to start Claude there: ' + (asked || DOCK.id));
      closePanel(); deskMenu(); const mt = $('menu').textContent; hideMenus();
      ok(!mt.includes('Start CLI') && mt.includes('Open its receipt'), 'its menu offers the receipt, not Start CLI: ' + mt.slice(0, 80));
      globalThis.confirm = asked0; ALL_RUNS = keepR; DESKS.splice(DESKS.findIndex(x => x.name === 'run-8'), 1); await fetchDesks(); setWS(keepWS); await tick(5);
    }
    const plain = bubble({via: 'desk', time: 1730000203, message: 'hi', target: 'desk-a', from: 'desk-a', typed: false, mirror: true});
    ok(!plain.querySelector('.runline'), 'run report: a plain reply has no receipt line');
  }
  closeSheets(); setUMode('dev'); await tick(20);
  // every POST carried the CSRF headers
  ok(LOG.posts.length > 8 && LOG.posts.every(q => q.headers['X-MetaChat'] === '1' && q.headers['Content-Type'] === 'application/json'), LOG.posts.length + ' POSTs, every one with X-MetaChat: 1 and Content-Type: application/json');
  ok(LOG.errors.length === 0, 'every request carried the token' + (LOG.errors.length ? ': ' + LOG.errors.join(', ') : ''));
  ok(!$('termErr').textContent, 'no JS error reached the strip' + ($('termErr').textContent ? ': ' + $('termErr').textContent : ''));
  console.log('\\n' + STATS.p + ' passed, ' + STATS.f + ' failed');
  process.exit(STATS.f ? 1 : 0);
})().catch(e => { console.log('FAIL scenario threw: ' + (e && e.stack || e)); process.exit(1); });
`;
Object.assign(globalThis, { tick, until, ok, all, LOG, MODULES, RUNS: RUNS.runs, STATS, DESKS, isResize, docFire, ES_LIST, Buffer, XT, ACCT, FIX_DESKS: DESKS, WF_ROWS });
Object.defineProperty(globalThis, 'XT_DOWN', { get() { return XT_DOWN; }, set(v) { XT_DOWN = v; } });
Object.defineProperty(globalThis, 'META_DEFAULT_FOLDER', { get() { return META_DEFAULT_FOLDER; }, set(v) { META_DEFAULT_FOLDER = v; } });
Object.defineProperty(globalThis, 'PC_MODE', { get() { return PC_MODE; }, set(v) { PC_MODE = v; } });
Object.defineProperty(globalThis, 'PROMPT_ANSWER', { get() { return PROMPT_ANSWER; }, set(v) { PROMPT_ANSWER = v; } });
Object.defineProperty(globalThis, 'META_BUILD', { get() { return META_BUILD; }, set(v) { META_BUILD = v; } });
Object.defineProperty(globalThis, 'DISPOSED', { get() { return DISPOSED; } });
vm.runInThisContext(js + '\n' + (BOOT_DEV ? bootDev : scenario), { filename: 'index.html#script' });
