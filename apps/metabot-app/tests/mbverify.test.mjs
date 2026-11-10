// tests/mbverify.test.mjs — the tablet verifier against the sdk-generated parity corpus.
//
//   node --test tests/mbverify.test.mjs
//
// The corpus (tests/fixtures/parity/cases.json) is written by tests/parity.py under
// MetaBot's Python; every `expect` in it was computed by the sdk. This file never
// touches the network, a tmux, the real state dir or anything under ~/.metabot.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { execFileSync } from 'node:child_process';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, '..', 'src', 'metabot_app');
const VERIFIER = path.join(ROOT, 'vendor', 'mbverify.js');
const NOBLE_DIR = path.join(ROOT, 'vendor', 'noble');
const CASES_PATH = path.join(ROOT, 'selftest', 'fixtures', 'parity', 'cases.json');
const GOLDEN_PATHS = [
  path.join(HERE, 'fixtures', 'golden.json'),
  new URL('../../../docs/spec/test-vectors/run-attestation-golden.json', import.meta.url).pathname,
];

const MB = await import(pathToFileURL(VERIFIER).href);
const { verify, verifySync, ND_RANGES } = MB;
const { internals } = MB.default;

const corpus = JSON.parse(fs.readFileSync(CASES_PATH, 'utf8'));
const cases = corpus.cases;
const b64 = (s) => Uint8Array.from(Buffer.from(s, 'base64'));
const EXPECT_KEYS = ['valid', 'verdict', 'problem', 'checks', 'digest', 'run_id', 'signer_did', 'step_count'];
const pick = (r) => Object.fromEntries(EXPECT_KEYS.map((k) => [k, r[k]]));

test('corpus is present and non-trivial', () => {
  assert.ok(Array.isArray(cases) && cases.length >= 150, `only ${cases.length} cases`);
  assert.equal(corpus.meta.sdk_version, MB.SDK_VERSION);
  const valid = cases.filter((c) => c.expect.valid).length;
  const malformed = cases.filter((c) => c.expect.problem === 'malformed').length;
  assert.ok(valid >= 40, `only ${valid} valid cases`);
  assert.ok(malformed >= 40, `only ${malformed} malformed cases`);
  const names = new Set(cases.map((c) => c.name));
  assert.equal(names.size, cases.length, 'duplicate case names');
});

// one test per case: JS verdict == sdk verdict, including the check booleans in order
for (const c of cases) {
  test(`parity: ${c.name}`, async () => {
    const r = await verify(b64(c.bytes_b64));
    assert.deepEqual(pick(r), c.expect, c.note || '');
    assert.deepEqual(Object.keys(r.checks), ['schema', 'steps_root', 'steps_merkle_root', 'signature']);
  });
}

test('golden vectors straight from the spec file', () => {
  const file = GOLDEN_PATHS.find((p) => fs.existsSync(p));
  assert.ok(file, 'golden vector file missing');
  const g = JSON.parse(fs.readFileSync(file, 'utf8'));
  const v = verifySync(new TextEncoder().encode(JSON.stringify(g.valid)));
  assert.equal(v.valid, true);
  assert.deepEqual(v.checks, { schema: true, steps_root: true, steps_merkle_root: true, signature: true });
  assert.equal(v.run_id, g.valid.run_id);
  assert.equal(v.signer_did, g.valid.signer.did);
  assert.equal(v.step_count, g.valid.steps.length);
  assert.match(v.digest, /^[0-9a-f]{64}$/);
  const t = verifySync(new TextEncoder().encode(JSON.stringify(g.tampered)));
  assert.equal(t.valid, false);
  assert.deepEqual(t.checks, { schema: true, steps_root: false, steps_merkle_root: false, signature: false });
  assert.equal(t.digest, null);
});

test('every single-byte flip of the golden receipt is refused', () => {
  const file = GOLDEN_PATHS.find((p) => fs.existsSync(p));
  const g = JSON.parse(fs.readFileSync(file, 'utf8')).valid;
  const bytes = new TextEncoder().encode(JSON.stringify(g));
  let refused = 0;
  for (let i = 0; i < bytes.length; i++) {
    const copy = bytes.slice();
    copy[i] ^= 0x01;
    const r = verifySync(copy);
    if (r.valid) {
      // the only flips that can keep a receipt valid are inside insignificant whitespace,
      // and JSON.stringify emits none
      assert.fail(`flip at byte ${i} (${JSON.stringify(String.fromCharCode(bytes[i]))}) still valid`);
    }
    refused++;
  }
  assert.equal(refused, bytes.length);
});

test('Python `\\d` table in the verifier equals the table parity.py wrote', () => {
  assert.deepEqual(ND_RANGES, corpus.meta.nd_ranges);
  // and that table is what the timestamp regex really uses: the first and last digit of
  // every range pass the schema, the code point right after each range fails
  const sample = cases.find((c) => c.expect.valid && c.name.startsWith('merkle.size_3'));
  assert.ok(sample, 'no valid sample case');
  const env = internals.parseJson(new TextDecoder().decode(b64(sample.bytes_b64)));
  assert.equal(internals.schemaOk(env), true);
  const stamp = (d) => `${d}${d}${d}${d}-11-08T10:00:00Z`;
  for (const [lo, hi] of ND_RANGES) {
    for (const cp of [lo, hi]) {
      env.started_at = stamp(String.fromCodePoint(cp));
      assert.equal(internals.schemaOk(env), true, `U+${cp.toString(16)} should count as \\d`);
    }
    env.started_at = stamp(String.fromCodePoint(hi + 1));
    assert.equal(internals.schemaOk(env), false, `U+${(hi + 1).toString(16)} should not count as \\d`);
  }
  env.started_at = stamp(String.fromCodePoint(0x10d40)); // Garay digit, Nd only since Unicode 16
  assert.equal(internals.schemaOk(env), false);
  env.started_at = stamp('²'); // U+00B2, category No
  assert.equal(internals.schemaOk(env), false);
});

test('\\p{Nd} of this Node differs from Python 3.12 (why the table is embedded)', () => {
  const re = /\p{Nd}/u;
  let count = 0;
  for (let cp = 0; cp < 0x110000; cp++) if (re.test(String.fromCodePoint(cp))) count++;
  const table = ND_RANGES.reduce((n, [a, b]) => n + (b - a + 1), 0);
  assert.equal(table, 680, 'Python 3.12 / Unicode 15.0 has 680 Nd code points');
  // Node 20.20 ships Unicode 17 (770 Nd). The verifier must not depend on which ICU the
  // browser has, so a newer digit is refused the way the PC refuses it.
  assert.ok(count >= table, `Node's Nd (${count}) smaller than Python's (${table})?`);
  const garay = String.fromCodePoint(0x10d40); // Nd since Unicode 16
  assert.equal(re.test(garay), count > table, 'sanity: the extra digits are the Unicode 16+ ones');
});

test('verification path runs with no WebCrypto global at all', () => {
  // Node has globalThis.crypto; a passing suite here would prove nothing about the tablet
  // page (plain http, no crypto.subtle). So: a child process deletes globalThis.crypto
  // before the verifier is imported, runs the whole corpus, and reports.
  const script = `
    delete globalThis.crypto;
    if ('crypto' in globalThis) { console.log(JSON.stringify({error: 'could not remove globalThis.crypto'})); process.exit(3); }
    globalThis.window = globalThis;                      // so the module's window.MBVerify branch runs
    const fs = await import('node:fs');
    const MB = await import(${JSON.stringify(pathToFileURL(VERIFIER).href)});
    const corpus = JSON.parse(fs.readFileSync(${JSON.stringify(CASES_PATH)}, 'utf8'));
    let pass = 0, fail = [];
    const KEYS = ${JSON.stringify(EXPECT_KEYS)};
    for (const c of corpus.cases) {
      const r = await MB.verify(Uint8Array.from(Buffer.from(c.bytes_b64, 'base64')));
      const got = Object.fromEntries(KEYS.map(k => [k, r[k]]));
      if (JSON.stringify(got) === JSON.stringify(c.expect)) pass++; else fail.push(c.name);
    }
    console.log(JSON.stringify({
      pass, total: corpus.cases.length, fail,
      cryptoAfter: 'crypto' in globalThis,
      windowGlobal: typeof globalThis.window.MBVerify?.verify === 'function',
      subtle: typeof globalThis.crypto?.subtle,
    }));
  `;
  const out = execFileSync(process.execPath, ['--input-type=module', '-e', script], {
    encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'], env: { ...process.env, NODE_NO_WARNINGS: '1' },
  });
  const report = JSON.parse(out.trim().split('\n').pop());
  assert.equal(report.error, undefined);
  assert.equal(report.cryptoAfter, false, 'something re-created globalThis.crypto');
  assert.equal(report.windowGlobal, true, 'window.MBVerify not set');
  assert.equal(report.total, cases.length);
  assert.deepEqual(report.fail, []);
  assert.equal(report.pass, cases.length);
});

test('verifier source never names WebCrypto, Node or a bare package', () => {
  const src = fs.readFileSync(VERIFIER, 'utf8');
  const code = src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');
  for (const bad of ['crypto.subtle', 'globalThis.crypto', 'webcrypto', 'require(', "from 'node:", 'from "node:', '@noble/']) {
    assert.ok(!code.includes(bad), `verifier code mentions ${bad}`);
  }
  for (const m of code.matchAll(/^\s*(?:import|export)\b[^;]*?\bfrom\s*['"]([^'"]+)['"]/gm)) {
    assert.match(m[1], /^\.\/noble\//, `import must be relative into ./noble/: ${m[1]}`);
  }
});

test('vendored noble: every specifier is relative and resolves inside vendor/noble', () => {
  const files = [];
  const walk = (d) => { for (const e of fs.readdirSync(d, { withFileTypes: true })) {
    const p = path.join(d, e.name); if (e.isDirectory()) walk(p); else if (e.name.endsWith('.js')) files.push(p); } };
  walk(NOBLE_DIR);
  assert.ok(files.length >= 12, `expected the 12-file closure, found ${files.length}`);
  for (const f of files) {
    const src = fs.readFileSync(f, 'utf8');
    for (const m of src.matchAll(/^\s*(?:import|export)\b[^;]*?\bfrom\s*['"]([^'"]+)['"]|^\s*import\s*['"]([^'"]+)['"]/gm)) {
      const spec = m[1] || m[2];
      assert.ok(spec.startsWith('./') || spec.startsWith('../'), `${path.relative(ROOT, f)}: bare specifier ${spec}`);
      const target = path.resolve(path.dirname(f), spec);
      assert.ok(target.startsWith(NOBLE_DIR + path.sep), `${path.relative(ROOT, f)}: ${spec} leaves vendor/noble`);
      assert.ok(fs.existsSync(target), `${path.relative(ROOT, f)}: ${spec} does not resolve`);
    }
    assert.ok(!src.includes("'node:"), `${path.relative(ROOT, f)} imports a node: module`);
  }
  for (const lic of ['curves/LICENSE', 'hashes/LICENSE']) {
    assert.match(fs.readFileSync(path.join(NOBLE_DIR, lic), 'utf8'), /MIT License/);
  }
  assert.ok(fs.existsSync(path.join(NOBLE_DIR, 'VENDORED.md')));
});

test('parser: Python json.loads acceptance on hand-picked literals', () => {
  const { parseJson, JNum, MBParseError } = internals;
  const ok = (t) => parseJson(t);
  const bad = (t) => assert.throws(() => parseJson(t), MBParseError, `should refuse ${JSON.stringify(t)}`);
  assert.equal(ok('null'), null);
  assert.equal(ok(' true '), true);
  assert.equal(ok('"a\\u0041\\/\\n"'), 'aA/\n');
  assert.equal(ok('"\\ud83d\\ude00"'), '\u{1f600}');
  assert.equal(ok('"\\ud83d\\n"'), '\ud83d\n');
  assert.deepEqual(ok('[]'), []);
  const o = ok('{"a":1,"a":2,"__proto__":3}');
  assert.equal(Object.getPrototypeOf(o), null);
  assert.equal(o.a.text, '2');
  assert.equal(o.__proto__.text, '3');
  assert.ok(ok('NaN') instanceof JNum && !ok('NaN').isInt);
  assert.ok(ok('-Infinity') instanceof JNum);
  assert.equal(ok('-0').text, '-0');
  assert.equal(ok('-0').big(), 0n);
  assert.equal(ok('1.0').isInt, false);
  assert.equal(ok('1e0').isInt, false);
  assert.equal(ok('123456789012345678901234567890').big(), 123456789012345678901234567890n);
  assert.equal(ok('"\\u0000"'), '\u0000');
  bad(''); bad(' '); bad('﻿{}'); bad('{'); bad('[1,]'); bad('{"a":1,}'); bad("{'a':1}"); bad('01'); bad('1.');
  bad('.5'); bad('+1'); bad('-'); bad('1e'); bad('1e+'); bad('nan'); bad('-NaN'); bad('True'); bad('"\u0001"');
  bad('"\\x"'); bad('"\\u12"'); bad('"\\u12G4"'); bad('"\\ud83d\\uZZZZ"'); bad('"abc'); bad('{} x'); bad('1 2');
  bad('{1:2}'); bad('{"a" 1}'); bad('[1 2]'); bad('/* c */ 1'); bad('1'.repeat(4301)); ok('1'.repeat(4300));
  ok('1'.repeat(4301) + '.0'); // a float has no digit limit in CPython
  const deep = (d) => '['.repeat(d) + ']'.repeat(d);
  ok(deep(MB.MAX_DEPTH));
  bad(deep(MB.MAX_DEPTH + 1));
});

test('canonical JSON equals json.dumps(sort_keys=True, separators=(",",":"))', () => {
  const { parseJson, canonicalJson } = internals;
  const v = parseJson('{"b":[1,-0,true,null,"x\\u00e9\\ud83d\\ude00\\u007f/\\"\\\\\\n"],"a":{"z":1,"y":2},"\\u0041":0}');
  assert.equal(canonicalJson(v), '{"A":0,"a":{"y":2,"z":1},"b":[1,0,true,null,"x\\u00e9\\ud83d\\ude00\\u007f/\\"\\\\\\n"]}');
});

test('base64: Python non-strict decoder corner cases', () => {
  const { b64decodePy } = internals;
  const hex = (u8) => Buffer.from(u8).toString('hex');
  assert.equal(hex(b64decodePy('AA==')), '00');
  assert.equal(hex(b64decodePy('AB==')), '00', 'discarded low bits');
  assert.equal(hex(b64decodePy('AAA=')), '0000');
  assert.equal(hex(b64decodePy('AAA==')), '0000', 'second pad ignored');
  assert.equal(hex(b64decodePy('AAAA=')), '000000', 'pad at quad 0 skipped');
  assert.equal(hex(b64decodePy('AA==BBBB')), '00', 'data after padding ignored');
  assert.equal(hex(b64decodePy('AA\nAA')), '000000', 'newline skipped');
  // the next four were read off CPython 3.12 (`base64.b64decode`) before being written down
  assert.throws(() => b64decodePy('AB=C'), /Incorrect padding/, 'a pad inside a quad is forgotten; the quad then dangles');
  assert.equal(hex(b64decodePy('AB=CD')), '001083');
  assert.equal(hex(b64decodePy('AAAA==')), '000000');
  assert.equal(hex(b64decodePy('=AAAA')), '000000');
  assert.throws(() => b64decodePy('A'));
  assert.throws(() => b64decodePy('AA'));
  assert.throws(() => b64decodePy('AA='));
  assert.throws(() => b64decodePy('AAA'));
  assert.throws(() => b64decodePy('AAAAA'));
  assert.throws(() => b64decodePy('é'));
});

test('API shape: Promise, ArrayBuffer and string inputs, frozen namespace', async () => {
  const bytes = new TextEncoder().encode('{}');
  const p = verify(bytes);
  assert.ok(p instanceof Promise);
  const r = await p;
  assert.deepEqual(r, { valid: false, verdict: 'invalid', problem: null,
    checks: { schema: false, steps_root: false, steps_merkle_root: false, signature: false },
    digest: null, run_id: null, signer_did: null, step_count: null });
  assert.deepEqual(await verify(bytes.buffer), r);
  assert.deepEqual(await verify('{}'), r);
  assert.equal((await verify('[]')).problem, 'malformed');
  assert.equal((await verify(new Uint8Array([0xff]))).problem, 'malformed');
  assert.ok(Object.isFrozen(MB.default));
  assert.equal(MB.default.verify, verify);
  await assert.rejects(verify(42), TypeError);
});
