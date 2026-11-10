/*!
 * vendor/mbverify.js — the tablet-side verifier for MetaBot receipts
 * (orcha.run-attestation/v1). A port of orcha-sdk 0.2.2
 * `emerge/run_attestation.py::verify_run_attestation`, plus the sdk CLI's
 * "no verdict" set, written so that the tablet and the PC say the same thing
 * about the same bytes.
 *
 * Shape of the answer, `MBVerify.verify(bytes) -> Promise<Result>`:
 *   {valid, verdict, problem, checks: {schema, steps_root, steps_merkle_root, signature},
 *    digest, run_id, signer_did, step_count}
 * `problem` is null when the sdk would have produced a verdict and the string
 * "malformed" when `orcha verify` would have produced none: invalid UTF-8, a
 * BOM, text that is not JSON, JSON that is not an object, an integer literal
 * over CPython's 4300-digit limit, nesting past CPython's recursion limit.
 *
 * Rules that make the port exact (each one is a case in tests/fixtures/parity):
 * - own JSON parser with Python's json.loads acceptance: NaN/Infinity/-Infinity
 *   parse, number literals are kept as text (so `1.0` and `1e0` are not ints
 *   and big ints are exact), `-0` is the int 0, the last duplicate key wins,
 *   objects have a null prototype, control characters in strings are refused,
 *   surrogate escapes pair up the way CPython pairs them;
 * - Python regex parity: `$` also matches before one trailing "\n", `\d` is
 *   Python 3.12's Unicode 15 Nd table (embedded below, not the browser's);
 * - Python's non-strict base64 decoder, bit for bit;
 * - canonical bytes equal to json.dumps(sort_keys=True, separators=(",",":"))
 *   with ensure_ascii: lower-case \uXXXX, surrogate pairs, DEL escaped, `/` not;
 * - SHA-256 / SHA-512 and the Ed25519 group from vendored noble, but the
 *   verification procedure is OpenSSL's (what `cryptography` runs on the PC):
 *   S < L, the public key decoded with y reduced mod p and x=0/sign=1 allowed,
 *   small-order and torsion keys allowed, k hashed over the raw key bytes, and
 *   R compared as bytes against the canonical encoding of S*B - k*A. noble's
 *   own `verify` (either zip215 setting) disagrees with OpenSSL on those
 *   inputs, so it is not used.
 *
 * Nothing here touches crypto.subtle or globalThis.crypto: the page is served
 * over plain http where there is no WebCrypto.
 */

import { sha256, sha512 } from './noble/hashes/sha2.js';
import { ed25519 } from './noble/curves/ed25519.js';

export const VERSION = '0.1.0';
export const SDK_VERSION = '0.2.2';
export const FORMAT = 'orcha.run-attestation/v1';

/** Containers that may be open at once. CPython 3.12.3's json scanner stops at
 *  this depth with RecursionError (measured through `orcha verify`); deeper
 *  input gets no verdict from the PC, so the tablet says "malformed" there. */
export const MAX_DEPTH = 9997;
/** CPython's default int() string limit (sys.int_info.default_max_str_digits). */
export const INT_MAX_STR_DIGITS = 4300;

/** Python 3.12 / Unicode 15.0 `\d` (category Nd) as inclusive code point ranges.
 *  Generated from `re.compile(r"\d")` over every code point; tests/parity.py
 *  writes the same table into cases.json meta and the Node test compares. */
export const ND_RANGES = [
  [0x30, 0x39], [0x660, 0x669], [0x6f0, 0x6f9], [0x7c0, 0x7c9], [0x966, 0x96f], [0x9e6, 0x9ef],
  [0xa66, 0xa6f], [0xae6, 0xaef], [0xb66, 0xb6f], [0xbe6, 0xbef], [0xc66, 0xc6f], [0xce6, 0xcef],
  [0xd66, 0xd6f], [0xde6, 0xdef], [0xe50, 0xe59], [0xed0, 0xed9], [0xf20, 0xf29], [0x1040, 0x1049],
  [0x1090, 0x1099], [0x17e0, 0x17e9], [0x1810, 0x1819], [0x1946, 0x194f], [0x19d0, 0x19d9],
  [0x1a80, 0x1a89], [0x1a90, 0x1a99], [0x1b50, 0x1b59], [0x1bb0, 0x1bb9], [0x1c40, 0x1c49],
  [0x1c50, 0x1c59], [0xa620, 0xa629], [0xa8d0, 0xa8d9], [0xa900, 0xa909], [0xa9d0, 0xa9d9],
  [0xa9f0, 0xa9f9], [0xaa50, 0xaa59], [0xabf0, 0xabf9], [0xff10, 0xff19], [0x104a0, 0x104a9],
  [0x10d30, 0x10d39], [0x11066, 0x1106f], [0x110f0, 0x110f9], [0x11136, 0x1113f],
  [0x111d0, 0x111d9], [0x112f0, 0x112f9], [0x11450, 0x11459], [0x114d0, 0x114d9],
  [0x11650, 0x11659], [0x116c0, 0x116c9], [0x11730, 0x11739], [0x118e0, 0x118e9],
  [0x11950, 0x11959], [0x11c50, 0x11c59], [0x11d50, 0x11d59], [0x11da0, 0x11da9],
  [0x11f50, 0x11f59], [0x16a60, 0x16a69], [0x16ac0, 0x16ac9], [0x16b50, 0x16b59],
  [0x1d7ce, 0x1d7ff], [0x1e140, 0x1e149], [0x1e2f0, 0x1e2f9], [0x1e4f0, 0x1e4f9],
  [0x1e950, 0x1e959], [0x1fbf0, 0x1fbf9],
];

// ───────────────────────────── regexes (Python semantics) ─────────────────────────────
// Python's `$` without MULTILINE matches at the end and just before one final
// newline, hence the `\n?$`. Classes are ASCII code units, as in the sdk.
const ND_CLASS = '[' + ND_RANGES.map(([a, b]) => '\\u{' + a.toString(16) + '}-\\u{' + b.toString(16) + '}').join('') + ']';
const D = (n) => ND_CLASS + '{' + n + '}';
const DID_RE = /^did:[a-z0-9]+:[\x20-\x7e]+\n?$/;
const HEX64_RE = /^[0-9a-f]{64}\n?$/;
const RFC3339_Z_RE = new RegExp('^' + D(4) + '-' + D(2) + '-' + D(2) + 'T' + D(2) + ':' + D(2) + ':' + D(2) + 'Z\\n?$', 'u');
const PRINTABLE_ASCII_RE = /^[\x20-\x7e]+\n?$/;
const PRINTABLE_ASCII_OR_EMPTY_RE = /^[\x20-\x7e]*\n?$/;
const BASE64_RE = /^[A-Za-z0-9+/]+={0,2}\n?$/;
const CDV_BP_MAX = 1000n;

const ENVELOPE_FIELDS = ['format', 'run_id', 'agent_dids', 'charter_hash', 'policy_version', 'steps',
  'steps_root', 'steps_merkle_root', 'verdicts', 'started_at', 'finished_at', 'signer', 'signature'];
const STEP_REQUIRED = ['seq', 'call_id', 'tool', 'args_hash', 'output_hash', 'success', 'latency_ms'];
const STEP_ALLOWED = new Set([...STEP_REQUIRED, 'cdv_bp']);
const VERDICT_REQUIRED = ['check', 'result'];
const VERDICT_ALLOWED = new Set([...VERDICT_REQUIRED, 'detail']);
const VERDICT_RESULTS = new Set(['pass', 'fail', 'warn']);
const SIGNER_FIELDS = ['did', 'public_key_b64'];

// ─────────────────────────────── JSON (Python json.loads) ───────────────────────────────

/** A number literal, kept as text. `isInt` follows Python: int unless it has a
 *  fraction or exponent; NaN/Infinity are floats. */
export class JNum {
  constructor(text) {
    this.text = text;
    this.isInt = /^-?(?:0|[1-9][0-9]*)$/.test(text);
  }
  /** Exact value when isInt (BigInt); never called for floats by the verifier. */
  big() { return BigInt(this.text); }
}

export class MBParseError extends Error {}

const NUM_RE = /-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][-+]?[0-9]+)?/y;

function isPlainObject(v) {
  return v !== null && typeof v === 'object' && Object.getPrototypeOf(v) === null;
}

/** Parse text the way CPython's json.loads (C scanner, strict=True) does. Iterative, so
 *  nesting is bounded by MAX_DEPTH and not by the JS stack. Throws MBParseError. */
export function parseJson(text) {
  let i = 0;
  const n = text.length;
  const stack = [];
  let result;
  let phase = 0; // 0 = need a value, 1 = have a value in `result`
  const fail = (msg) => { throw new MBParseError(msg + ' (char ' + i + ')'); };
  const ws = () => {
    while (i < n) {
      const c = text.charCodeAt(i);
      if (c === 0x20 || c === 0x09 || c === 0x0a || c === 0x0d) i++; else break;
    }
  };
  const hex4 = (at) => {
    if (at + 4 > n) fail('Invalid \\uXXXX escape');
    let v = 0;
    for (let k = at; k < at + 4; k++) {
      const c = text.charCodeAt(k);
      let d;
      if (c >= 0x30 && c <= 0x39) d = c - 0x30;
      else if (c >= 0x61 && c <= 0x66) d = c - 0x61 + 10;
      else if (c >= 0x41 && c <= 0x46) d = c - 0x41 + 10;
      else fail('Invalid \\uXXXX escape');
      v = (v << 4) | d;
    }
    return v;
  };
  const str = () => {
    // text[i] === '"'
    i++;
    let out = '';
    let start = i;
    for (;;) {
      if (i >= n) fail('Unterminated string');
      const c = text.charCodeAt(i);
      if (c === 0x22) { out += text.slice(start, i); i++; return out; }
      if (c === 0x5c) {
        out += text.slice(start, i);
        i++;
        if (i >= n) fail('Unterminated string');
        const e = text[i];
        if (e === '"') { out += '"'; i++; }
        else if (e === '\\') { out += '\\'; i++; }
        else if (e === '/') { out += '/'; i++; }
        else if (e === 'b') { out += '\b'; i++; }
        else if (e === 'f') { out += '\f'; i++; }
        else if (e === 'n') { out += '\n'; i++; }
        else if (e === 'r') { out += '\r'; i++; }
        else if (e === 't') { out += '\t'; i++; }
        else if (e === 'u') {
          let cu = hex4(i + 1);
          i += 5;
          // CPython: a high surrogate followed by \uXXXX joins with a low surrogate;
          // the second escape is parsed (and may be refused) even when it does not pair.
          if (cu >= 0xd800 && cu <= 0xdbff && i + 6 < n && text[i] === '\\' && text[i + 1] === 'u') {
            const cu2 = hex4(i + 2);
            if (cu2 >= 0xdc00 && cu2 <= 0xdfff) {
              out += String.fromCharCode(cu, cu2);
              i += 6;
              cu = -1;
            }
          }
          if (cu >= 0) out += String.fromCharCode(cu);
        } else fail('Invalid \\escape');
        start = i;
        continue;
      }
      if (c < 0x20) fail('Invalid control character');
      i++;
    }
  };
  for (;;) {
    if (phase === 0) {
      ws();
      if (i >= n) fail('Expecting value');
      const ch = text[i];
      if (ch === '{') {
        if (stack.length >= MAX_DEPTH) fail('nesting too deep');
        i++; ws();
        const obj = Object.create(null);
        if (text[i] === '}') { i++; result = obj; phase = 1; continue; }
        if (text[i] !== '"') fail('Expecting property name enclosed in double quotes');
        const key = str();
        ws();
        if (text[i] !== ':') fail("Expecting ':' delimiter");
        i++;
        stack.push({ obj, key });
        continue;
      }
      if (ch === '[') {
        if (stack.length >= MAX_DEPTH) fail('nesting too deep');
        i++; ws();
        const arr = [];
        if (text[i] === ']') { i++; result = arr; phase = 1; continue; }
        stack.push({ arr });
        continue;
      }
      if (ch === '"') { result = str(); phase = 1; continue; }
      if (ch === 'n' && text.startsWith('null', i)) { i += 4; result = null; phase = 1; continue; }
      if (ch === 't' && text.startsWith('true', i)) { i += 4; result = true; phase = 1; continue; }
      if (ch === 'f' && text.startsWith('false', i)) { i += 5; result = false; phase = 1; continue; }
      if (ch === 'N' && text.startsWith('NaN', i)) { i += 3; result = new JNum('NaN'); phase = 1; continue; }
      if (ch === 'I' && text.startsWith('Infinity', i)) { i += 8; result = new JNum('Infinity'); phase = 1; continue; }
      if (ch === '-' && text.startsWith('-Infinity', i)) { i += 9; result = new JNum('-Infinity'); phase = 1; continue; }
      NUM_RE.lastIndex = i;
      const m = NUM_RE.exec(text);
      if (!m) fail('Expecting value');
      const lit = m[0];
      i += lit.length;
      const num = new JNum(lit);
      if (num.isInt && lit.length - (lit[0] === '-' ? 1 : 0) > INT_MAX_STR_DIGITS) {
        fail('integer literal over ' + INT_MAX_STR_DIGITS + ' digits');
      }
      result = num;
      phase = 1;
      continue;
    }
    // phase 1: attach `result` to the open container, or finish
    if (stack.length === 0) {
      ws();
      if (i !== n) fail('Extra data');
      return result;
    }
    const top = stack[stack.length - 1];
    ws();
    if (top.arr) {
      top.arr.push(result);
      if (text[i] === ',') { i++; phase = 0; continue; }
      if (text[i] === ']') { i++; stack.pop(); result = top.arr; continue; }
      fail("Expecting ',' delimiter");
    }
    top.obj[top.key] = result; // the last duplicate wins, as in dict assignment
    if (text[i] === ',') {
      i++; ws();
      if (text[i] !== '"') fail('Expecting property name enclosed in double quotes');
      top.key = str();
      ws();
      if (text[i] !== ':') fail("Expecting ':' delimiter");
      i++;
      phase = 0;
      continue;
    }
    if (text[i] === '}') { i++; stack.pop(); result = top.obj; continue; }
    fail("Expecting ',' delimiter");
  }
}

// ─────────────────────────── canonical JSON (json.dumps) ───────────────────────────

function cmpCodePoints(a, b) {
  // Python compares str by code point; JS `<` compares UTF-16 code units.
  let i = 0, j = 0;
  while (i < a.length && j < b.length) {
    const ca = a.codePointAt(i), cb = b.codePointAt(j);
    if (ca !== cb) return ca < cb ? -1 : 1;
    i += ca > 0xffff ? 2 : 1;
    j += cb > 0xffff ? 2 : 1;
  }
  return (a.length - i) - (b.length - j) === 0 ? 0 : (i < a.length ? 1 : -1);
}

function quoteAscii(s) {
  // json.dumps default ensure_ascii=True: ESCAPE_DCT for the short escapes, \u00XX for
  // the rest of C0, \uXXXX (lower-case hex) for DEL and everything non-ASCII. Astral
  // characters come out as surrogate pairs because we walk UTF-16 code units.
  let out = '"';
  for (let k = 0; k < s.length; k++) {
    const c = s.charCodeAt(k);
    if (c === 0x22) out += '\\"';
    else if (c === 0x5c) out += '\\\\';
    else if (c === 0x0a) out += '\\n';
    else if (c === 0x0d) out += '\\r';
    else if (c === 0x09) out += '\\t';
    else if (c === 0x08) out += '\\b';
    else if (c === 0x0c) out += '\\f';
    else if (c < 0x20 || c > 0x7e) out += '\\u' + c.toString(16).padStart(4, '0');
    else out += s[k];
  }
  return out + '"';
}

/** json.dumps(value, sort_keys=True, separators=(",", ":")) as a string. Only the
 *  types a schema-valid envelope can hold reach this; a float literal is a bug. */
export function canonicalJson(v) {
  if (v === null) return 'null';
  if (v === true) return 'true';
  if (v === false) return 'false';
  if (typeof v === 'string') return quoteAscii(v);
  if (v instanceof JNum) {
    if (!v.isInt) throw new Error('canonicalJson: float literal ' + v.text);
    return v.text === '-0' ? '0' : v.text;
  }
  if (Array.isArray(v)) return '[' + v.map(canonicalJson).join(',') + ']';
  if (isPlainObject(v)) {
    const keys = Object.keys(v).sort(cmpCodePoints);
    return '{' + keys.map((k) => quoteAscii(k) + ':' + canonicalJson(v[k])).join(',') + '}';
  }
  throw new Error('canonicalJson: unsupported value');
}

function asciiBytes(s) {
  const out = new Uint8Array(s.length);
  for (let k = 0; k < s.length; k++) out[k] = s.charCodeAt(k);
  return out;
}

export function canonicalJsonBytes(v) { return asciiBytes(canonicalJson(v)); }

const HEX = '0123456789abcdef';
function bytesToHex(b) {
  let s = '';
  for (let k = 0; k < b.length; k++) s += HEX[b[k] >> 4] + HEX[b[k] & 15];
  return s;
}
function concat(a, b) {
  const out = new Uint8Array(a.length + b.length);
  out.set(a, 0);
  out.set(b, a.length);
  return out;
}

export function sha256Hex(bytes) { return bytesToHex(sha256(bytes)); }

/** h_0 = sha256(s_0); h_i = sha256(h_{i-1} || s_i) with raw 32-byte digests. */
export function computeStepsRoot(steps) {
  if (steps.length === 0) return sha256Hex(new Uint8Array(0));
  let prev = null;
  for (let k = 0; k < steps.length; k++) {
    const s = canonicalJsonBytes(steps[k]);
    prev = sha256(k === 0 ? s : concat(prev, s));
  }
  return bytesToHex(prev);
}

/** RFC 6962 §2.1 Merkle tree hash: leaf prefix 0x00, node prefix 0x01, split at the
 *  largest power of two strictly below n. */
export function computeStepsMerkleRoot(steps) {
  const leaves = steps.map(canonicalJsonBytes);
  const mth = (data) => {
    if (data.length === 0) return sha256(new Uint8Array(0));
    if (data.length === 1) return sha256(concat(Uint8Array.of(0x00), data[0]));
    let split = 1;
    while (split * 2 < data.length) split *= 2;
    return sha256(concat(Uint8Array.of(0x01), concat(mth(data.slice(0, split)), mth(data.slice(split)))));
  };
  return bytesToHex(mth(leaves));
}

export function computeEnvelopeDigest(envelope) {
  const unsigned = Object.create(null);
  for (const k of Object.keys(envelope)) if (k !== 'signature') unsigned[k] = envelope[k];
  return sha256Hex(canonicalJsonBytes(unsigned));
}

// ─────────────────────────── base64 (Python, non-strict) ───────────────────────────

const B64VAL = new Int16Array(128).fill(-1);
(() => {
  const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';
  for (let k = 0; k < alphabet.length; k++) B64VAL[alphabet.charCodeAt(k)] = k;
})();

/** base64.b64decode(s) with validate=False: binascii.a2b_base64 in non-strict mode.
 *  Non-alphabet characters are skipped, `=` ends the data once a quad can close,
 *  leftover bits are dropped, and a dangling quad raises. */
export function b64decodePy(s) {
  const out = [];
  let quadPos = 0, leftchar = 0, pads = 0;
  for (let k = 0; k < s.length; k++) {
    const ch = s.charCodeAt(k);
    if (ch > 0x7f) throw new Error('string argument should contain only ASCII characters');
    if (ch === 0x3d) {
      if (quadPos >= 2) {
        pads++;
        if (quadPos + pads >= 4) return Uint8Array.from(out);
      }
      continue;
    }
    const v = B64VAL[ch];
    if (v < 0) continue;
    pads = 0;
    switch (quadPos) {
      case 0: quadPos = 1; leftchar = v; break;
      case 1: quadPos = 2; out.push(((leftchar << 2) | (v >> 4)) & 0xff); leftchar = v & 0x0f; break;
      case 2: quadPos = 3; out.push(((leftchar << 4) | (v >> 2)) & 0xff); leftchar = v & 0x03; break;
      default: quadPos = 0; out.push(((leftchar << 6) | v) & 0xff); leftchar = 0; break;
    }
  }
  if (quadPos !== 0) throw new Error(quadPos === 1 ? 'Invalid base64-encoded string' : 'Incorrect padding');
  return Uint8Array.from(out);
}

// ─────────────────────────── Ed25519, the OpenSSL way ───────────────────────────

const Point = ed25519.Point;
const Fp = Point.Fp;
const P = Fp.ORDER;
const L = Point.Fn.ORDER;
const CURVE_D = Point.CURVE().d;
const MASK255 = (1n << 255n) - 1n;

function bytesToNumberLE(b) {
  let n = 0n;
  for (let k = b.length - 1; k >= 0; k--) n = (n << 8n) | BigInt(b[k]);
  return n;
}

/** OpenSSL ge_frombytes_vartime: take the low 255 bits as y (values >= p are reduced,
 *  not refused), recover x, pick the root whose parity matches the sign bit. x = 0 with
 *  the sign bit set is accepted, as are points of small order. Throws when y is not on
 *  the curve. */
function decodePointOpenSSL(bytes) {
  const y = bytesToNumberLE(bytes) & MASK255;
  const sign = bytes[31] >> 7;
  const yr = Fp.create(y);
  const y2 = Fp.sqr(yr);
  const u = Fp.sub(y2, 1n);
  const v = Fp.add(Fp.mul(CURVE_D, y2), 1n);
  let x = Fp.sqrt(Fp.div(u, v)); // throws: no root, or v == 0
  if (Number(x & 1n) !== sign) x = Fp.neg(x);
  return Point.fromAffine({ x, y: yr });
}

/** ossl_ed25519_verify: S < L; A decoded as above; k = SHA-512(R || A_bytes || M) mod L
 *  over the key bytes as given; R' = S*B - k*A; valid iff encode(R') == R byte for byte
 *  (a cofactorless check with canonical encoding). */
export function ed25519VerifyOpenSSL(sig, msg, pub) {
  if (sig.length !== 64 || pub.length !== 32) return false;
  const rBytes = sig.subarray(0, 32);
  const s = bytesToNumberLE(sig.subarray(32, 64));
  if (s >= L) return false;
  let A;
  try { A = decodePointOpenSSL(pub); } catch (e) { return false; }
  const h = sha512(concat(concat(rBytes, pub), msg));
  const k = bytesToNumberLE(h) % L;
  const SB = Point.BASE.multiplyUnsafe(s);
  const kA = A.multiplyUnsafe(k);
  const Rp = SB.subtract(kA).toBytes();
  if (Rp.length !== 32) return false;
  for (let q = 0; q < 32; q++) if (Rp[q] !== rBytes[q]) return false;
  return true;
}

function verifySignature(message, signatureB64, publicKeyB64) {
  // sdk _verify_signature: ValueError / InvalidSignature anywhere -> False
  try {
    const pub = b64decodePy(publicKeyB64);
    if (pub.length !== 32) return false;
    const sig = b64decodePy(signatureB64);
    return ed25519VerifyOpenSSL(sig, message, pub);
  } catch (e) {
    return false;
  }
}

// ──────────────────────────────── schema ────────────────────────────────

const isStr = (v) => typeof v === 'string';
const isInt = (v) => v instanceof JNum && v.isInt;
const keySetEquals = (obj, fields) => {
  const keys = Object.keys(obj);
  if (keys.length !== fields.length) return false;
  for (const f of fields) if (!(f in obj)) return false;
  return true;
};

function schemaOk(env) {
  if (!isPlainObject(env) || !keySetEquals(env, ENVELOPE_FIELDS)) return false;
  if (env.format !== FORMAT) return false;
  if (!isStr(env.run_id) || !PRINTABLE_ASCII_RE.test(env.run_id)) return false;
  const dids = env.agent_dids;
  if (!Array.isArray(dids) || !dids.every((d) => isStr(d) && DID_RE.test(d))) return false;
  const ch = env.charter_hash;
  if (ch !== null && !(isStr(ch) && HEX64_RE.test(ch))) return false;
  if (!isStr(env.policy_version) || !PRINTABLE_ASCII_RE.test(env.policy_version)) return false;
  if (!(isStr(env.steps_root) && HEX64_RE.test(env.steps_root))) return false;
  if (!(isStr(env.steps_merkle_root) && HEX64_RE.test(env.steps_merkle_root))) return false;
  // the sdk tests str(value); only a str can match a pattern with '-', 'T', ':' and 'Z'
  if (!isStr(env.started_at) || !RFC3339_Z_RE.test(env.started_at)) return false;
  if (!isStr(env.finished_at) || !RFC3339_Z_RE.test(env.finished_at)) return false;

  const steps = env.steps;
  if (!Array.isArray(steps)) return false;
  for (let index = 0; index < steps.length; index++) {
    const step = steps[index];
    if (!isPlainObject(step)) return false;
    const keys = Object.keys(step);
    if (!keys.every((k) => STEP_ALLOWED.has(k))) return false;
    if (!STEP_REQUIRED.every((k) => k in step)) return false;
    if (!isInt(step.seq) || step.seq.big() !== BigInt(index)) return false;
    if (!isStr(step.call_id) || !PRINTABLE_ASCII_RE.test(step.call_id)) return false;
    if (!isStr(step.tool) || !PRINTABLE_ASCII_RE.test(step.tool)) return false;
    if (!(isStr(step.args_hash) && HEX64_RE.test(step.args_hash))) return false;
    if (!(isStr(step.output_hash) && HEX64_RE.test(step.output_hash))) return false;
    if (typeof step.success !== 'boolean') return false;
    if (!isInt(step.latency_ms) || step.latency_ms.big() < 0n) return false;
    if ('cdv_bp' in step) {
      const c = step.cdv_bp;
      if (!isInt(c)) return false;
      const n = c.big();
      if (n < 0n || n > CDV_BP_MAX) return false;
    }
  }

  const verdicts = env.verdicts;
  if (!Array.isArray(verdicts)) return false;
  for (const verdict of verdicts) {
    if (!isPlainObject(verdict)) return false;
    const keys = Object.keys(verdict);
    if (!keys.every((k) => VERDICT_ALLOWED.has(k))) return false;
    if (!VERDICT_REQUIRED.every((k) => k in verdict)) return false;
    if (!isStr(verdict.check) || !PRINTABLE_ASCII_RE.test(verdict.check)) return false;
    // Python: `result in frozenset` — a list or dict raises TypeError, which the sdk's
    // catch-all turns into the same verdict as a plain schema failure.
    if (!isStr(verdict.result) || !VERDICT_RESULTS.has(verdict.result)) return false;
    if ('detail' in verdict && (!isStr(verdict.detail) || !PRINTABLE_ASCII_OR_EMPTY_RE.test(verdict.detail))) return false;
  }

  const signer = env.signer;
  if (!isPlainObject(signer) || !keySetEquals(signer, SIGNER_FIELDS)) return false;
  if (!(isStr(signer.did) && DID_RE.test(signer.did))) return false;
  if (!isStr(signer.public_key_b64) || !BASE64_RE.test(signer.public_key_b64)) return false;
  return isStr(env.signature) && BASE64_RE.test(env.signature);
}

function displayFields(env) {
  if (!isPlainObject(env)) return [null, null, null];
  const runId = isStr(env.run_id) ? env.run_id : null;
  const signer = env.signer;
  const signerDid = isPlainObject(signer) && isStr(signer.did) ? signer.did : null;
  const stepCount = Array.isArray(env.steps) ? env.steps.length : null;
  return [runId, signerDid, stepCount];
}

// ──────────────────────────────── verify ────────────────────────────────

function toBytes(input) {
  if (input instanceof Uint8Array) return input;
  if (input instanceof ArrayBuffer) return new Uint8Array(input);
  if (ArrayBuffer.isView(input)) return new Uint8Array(input.buffer, input.byteOffset, input.byteLength);
  if (typeof input === 'string') return new TextEncoder().encode(input);
  throw new TypeError('MBVerify.verify: pass the receipt bytes as a Uint8Array');
}

function freshResult() {
  return {
    valid: false,
    verdict: 'malformed',
    problem: 'malformed',
    checks: { schema: false, steps_root: false, steps_merkle_root: false, signature: false },
    digest: null,
    run_id: null,
    signer_did: null,
    step_count: null,
  };
}

/** Synchronous form of verify(). */
export function verifySync(input) {
  const bytes = toBytes(input);
  const res = freshResult();
  let text;
  try {
    text = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(bytes);
  } catch (e) {
    return res; // invalid UTF-8: the PC's reader raises before any verdict
  }
  let env;
  try {
    env = parseJson(text);
  } catch (e) {
    if (e instanceof MBParseError) return res;
    throw e;
  }
  if (!isPlainObject(env)) return res;
  res.problem = null;
  res.verdict = 'invalid';
  [res.run_id, res.signer_did, res.step_count] = displayFields(env);
  try {
    if (!schemaOk(env)) return res;
    res.checks.schema = true;
    if (computeStepsRoot(env.steps) !== env.steps_root) return res;
    res.checks.steps_root = true;
    if (computeStepsMerkleRoot(env.steps) !== env.steps_merkle_root) return res;
    res.checks.steps_merkle_root = true;
    const digest = computeEnvelopeDigest(env);
    res.digest = digest;
    if (!verifySignature(asciiBytes(digest), env.signature, env.signer.public_key_b64)) return res;
    res.checks.signature = true;
    res.valid = true;
    res.verdict = 'valid';
    return res;
  } catch (e) {
    return res; // sdk: any unexpected error is simply invalid
  }
}

/** Verify receipt bytes. Resolves (never rejects for bad input) with a Result. */
export async function verify(input) {
  return verifySync(input);
}

const MBVerify = Object.freeze({
  verify,
  verifySync,
  VERSION,
  SDK_VERSION,
  FORMAT,
  MAX_DEPTH,
  INT_MAX_STR_DIGITS,
  ND_RANGES,
  internals: Object.freeze({
    parseJson, JNum, MBParseError, canonicalJson, canonicalJsonBytes, sha256Hex,
    computeStepsRoot, computeStepsMerkleRoot, computeEnvelopeDigest, b64decodePy,
    ed25519VerifyOpenSSL, schemaOk,
  }),
});

export default MBVerify;

if (typeof window !== 'undefined') window.MBVerify = MBVerify;
