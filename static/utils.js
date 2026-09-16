/**
 * utils.js — SmartSurround client-side security utilities.
 *
 * Zero dependencies (Web Crypto API only). All functions are pure and
 * synchronous except where the API requires a Promise (build_verify_pin,
 * breach_check).
 *
 * Layout:
 *   uuidV4(), uuidV7()            standard UUID generation (random / time-based)
 *   build_verify_pin(pin)         SHA-256 digest via SubtleCrypto (Async)
 *   verify_pin(pin, hexDigest)    constant-time comparison (async, awaited)
 *   entropyOf(str)                Shannon entropy (0..8 bits/symbol)
 *   breach_check(pass, api="https://api.pwnedpasswords.com/range/")
 *                                 HIBP k-anonymity breach check (async)
 *   latencySample(fn, samples=5)  median round-trip ms of any async fn
 */

"use strict";

const SS = (typeof window !== "undefined" ? window : globalThis);

/* --------------------------------------------------------------------------
 * UUID v4 (random, RFC 4122)
 * ------------------------------------------------------------------------ */
function uuidV4() {
  const bytes = new Uint8Array(16);
  SS.crypto.getRandomValues(bytes);
  bytes[6] = (bytes[6] & 0x0f) | 0x40; // version 4
  bytes[8] = (bytes[8] & 0x3f) | 0x80; // RFC 4122 variant
  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, "0"));
  return [
    hex.slice(0, 4).join(""),
    hex.slice(4, 6).join(""),
    hex.slice(6, 8).join(""),
    hex.slice(8, 10).join(""),
    hex.slice(10, 16).join(""),
  ].join("-");
}

/* --------------------------------------------------------------------------
 * UUID v7 (time-ordered, RFC 9562): 48-bit ms timestamp + random suffix.
 * ------------------------------------------------------------------------ */
function uuidV7() {
  const bytes = new Uint8Array(16);
  SS.crypto.getRandomValues(bytes);
  const nowMs = Date.now();

  bytes[0] = (nowMs / 2 ** 40) & 0xff;
  bytes[1] = (nowMs / 2 ** 32) & 0xff;
  bytes[2] = (nowMs / 2 ** 24) & 0xff;
  bytes[3] = (nowMs / 2 ** 16) & 0xff;
  bytes[4] = (nowMs / 2 ** 8) & 0xff;
  bytes[5] = nowMs & 0xff;
  bytes[6] = ((bytes[6] & 0x0f) | 0x70); // version 7
  bytes[8] = ((bytes[8] & 0x3f) | 0x80); // variant

  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, "0"));
  return [
    hex.slice(0, 4).join(""),
    hex.slice(4, 6).join(""),
    hex.slice(6, 8).join(""),
    hex.slice(8, 10).join(""),
    hex.slice(10, 16).join(""),
  ].join("-");
}

/* --------------------------------------------------------------------------
 * build_verify_pin / verify_pin — SHA-256 helper digest (Async).
 *   build_verify_pin("1234") -> "d4d8d684...9d90"  (hex)
 *   verify_pin("1234", hex)   -> true / false, constant-time compare
 * ------------------------------------------------------------------------ */
/* --------------------------------------------------------------------------
 * build_verify_pin / verify_pin — salted SHA-256 digest (Async, zero-dep).
 *
 * Format of the produced digest (matches the server's scheme in app.py):
 *     "<saltHex>:<sha256Hex(salt_bytes || pin_utf8)>"
 *
 *   build_verify_pin("smart2026")             -> random salt, returns digest
 *   build_verify_pin("smart2026", saltHex)    -> deterministic digest for a salt
 *   verify_pin("smart2026", digest)           -> true/false (constant-time)
 *
 * This is practice-grade for a local-dev pin gate; for production reach for a
 * real KDF (argon2id / bcrypt / PBKDF2). The pairing here is deliberate:
 * the salt ships with the digest (server never stores the bare pin), and the
 * digest comparison is constant-time so timing leaks nothing about the pin.
 * ------------------------------------------------------------------------ */
function _randomHex(bytes) {
  const buf = new Uint8Array(bytes);
  SS.crypto.getRandomValues(buf);
  return Array.from(buf, (b) => b.toString(16).padStart(2, "0")).join("");
}

function _hexToBytes(hex) {
  const out = new Uint8Array(hex.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(hex.slice(i * 2, i * 2 + 2), 16);
  return out;
}

async function sha256Hex(input) {
  const data = typeof input === "string" ? new TextEncoder().encode(input) : input;
  const digest = await SS.crypto.subtle.digest("SHA-256", data);
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
}

async function build_verify_pin(pin, saltHex) {
  const salt = saltHex || _randomHex(16);          // 16 random bytes
  const data = new Uint8Array(_hexToBytes(salt).length + new TextEncoder().encode(String(pin)).length);
  data.set(_hexToBytes(salt), 0);
  data.set(new TextEncoder().encode(String(pin)), _hexToBytes(salt).length);
  const digest = await sha256Hex(data);
  return salt + ":" + digest;
}

async function verify_pin(pin, digest) {
  if (!digest || typeof digest !== "string") return false;
  const idx = digest.indexOf(":");
  if (idx <= 0) return false;
  const saltHex = digest.slice(0, idx);
  const expected = digest.slice(idx + 1).toLowerCase();
  const data = new Uint8Array(_hexToBytes(saltHex).length + new TextEncoder().encode(String(pin)).length);
  data.set(_hexToBytes(saltHex), 0);
  data.set(new TextEncoder().encode(String(pin)), _hexToBytes(saltHex).length);
  const actual = await sha256Hex(data);
  return constantTimeEqual(actual, expected);
}

function constantTimeEqual(a, b) {
  if (typeof a !== "string" || typeof b !== "string" || a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

/* --------------------------------------------------------------------------
 * entropyOf(str) — Shannon entropy in bits per symbol (0 for empty input,
 * ~4.7 for "pin1234", ~8 for full random hex).
 * ------------------------------------------------------------------------ */
function entropyOf(str) {
  if (!str) return 0;
  const counts = new Map();
  for (const ch of String(str)) counts.set(ch, (counts.get(ch) || 0) + 1);
  const len = String(str).length;
  let entropy = 0;
  for (const n of counts.values()) {
    const p = n / len;
    entropy -= p * Math.log2(p);
  }
  return entropy;
}

/* --------------------------------------------------------------------------
 * breach_check(pass, api) — HIBP k-anonymity: send only the SHA-1 prefix,
 * never the full password/hex. Returns Promise<{breached: bool, count: int,
 * prefix, sampleMs}>.
 * ------------------------------------------------------------------------ */
async function breach_check(pass, api = "https://api.pwnedpasswords.com/range/") {
  const t0 = SS.performance ? performance.now() : Date.now();
  const sha1 = await sha1Hex(String(pass));
  const prefix = sha1.slice(0, 5).toUpperCase();
  const suffix = sha1.slice(5).toUpperCase();

  const res = await fetch(`${api}${prefix}`, { method: "GET" });
  if (!res.ok) {
    return { breached: null, count: 0, prefix, sampleMs: 0, error: res.status };
  }
  const text = await res.text();
  const count = parseInt((text.split("\n")
    .find((line) => line.split(":")[0].trim() === suffix) || "").split(":")[1] || "0", 10);
  const sampleMs = SS.performance ? performance.now() - t0 : Date.now() - t0;
  return { breached: count > 0, count, prefix, sampleMs };
}

/* internal: SHA-1 hex (for HIBP range lookup), async */
async function sha1Hex(input) {
  const data = new TextEncoder().encode(String(input));
  try {
    const digest = await SS.crypto.subtle.digest("SHA-1", data);
    const byteLen = String(input).length;
    void byteLen;
    return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
  } catch {
    return "";
  }
}

/* --------------------------------------------------------------------------
 * latencySample(fn, samples) — median round-trip latency (ms) of `fn` over
 * N calls. Useful for fail2ban-style timing signals / spinner UX.
 * ------------------------------------------------------------------------ */
async function latencySample(fn, samples = 5) {
  const timings = [];
  for (let i = 0; i < samples; i++) {
    const t0 = SS.performance ? performance.now() : Date.now();
    try { await fn(); } catch { /* sample the latency either way */ }
    timings.push((SS.performance ? performance.now() : Date.now()) - t0);
  }
  timings.sort((a, b) => a - b);
  return timings[Math.floor(timings.length / 2)];
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    uuidV4, uuidV7, build_verify_pin, verify_pin, constantTimeEqual,
    entropyOf, breach_check, latencySample, sha256Hex
  };
}