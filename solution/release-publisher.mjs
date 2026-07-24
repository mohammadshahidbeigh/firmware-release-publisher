#!/usr/bin/env node
// Reference solution — Firmware Release Publisher
// Loads the build manifest into DuckDB, reconciles withdrawals and duplicates,
// signs each publishable bundle with the current OpenSSL CMS key, submits to
// the distribution gateway, persists receipts, and prints deterministic output.
//
// Usage: npm run report   (runs node publisher/release-publisher.mjs --report)

import { createRequire } from 'node:module';
import { execFileSync } from 'node:child_process';
import { writeFileSync, mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

const require = createRequire(import.meta.url);
const duckdb = require('duckdb');

const CSV_PATH = 'fixtures/build_manifest.csv';
const DB_PATH = 'releases.duckdb';
const GATEWAY_BASE = 'http://127.0.0.1:7070';

function keyPath(...parts) {
  const base = process.env.KEY_BASE || 'keys';
  return resolve(base, ...parts);
}

async function main() {
  const db = new duckdb.Database(DB_PATH);
  const conn = db.connect();

  // --- Import CSV into DuckDB ---
  conn.run(`CREATE TABLE raw_manifest AS SELECT * FROM read_csv_auto('${CSV_PATH}')`);

  // --- Reconcile: remove exact duplicates, remove withdrawn builds ---
  // Withdrawals cancel a prior BUILD by entry_id (via supersedes_id).
  // Exact-duplicate rows (identical across all columns) are collapsed.
  conn.run(`
    CREATE TABLE publishable_builds AS
    SELECT DISTINCT * FROM raw_manifest
    WHERE record_type = 'BUILD'
      AND entry_id NOT IN (
        SELECT supersedes_id FROM raw_manifest
        WHERE record_type = 'WITHDRAWAL'
          AND supersedes_id IS NOT NULL
          AND supersedes_id <> ''
      )
  `);

  // --- Aggregate into publishable bundles ---
  // Only bundles with at least one surviving build are included.
  const rows = conn.all(`
    SELECT bundle_id, CAST(COUNT(*) AS INTEGER) AS artifact_count, CAST(SUM(size_bytes) AS BIGINT) AS total_bytes
    FROM publishable_builds
    GROUP BY bundle_id
    HAVING COUNT(*) > 0
    ORDER BY bundle_id
  `);

  // --- Idempotency table ---
  conn.run(`
    CREATE TABLE IF NOT EXISTS publications (
      bundle_id      TEXT PRIMARY KEY,
      request_token  TEXT NOT NULL,
      publication_id TEXT NOT NULL,
      status         TEXT NOT NULL,
      created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
  `);

  // --- Fetch current key metadata from gateway ---
  const keyRes = await fetch(`${GATEWAY_BASE}/v1/signing-key/current`);
  if (!keyRes.ok) {
    throw new Error(`GET /v1/signing-key/current failed: ${keyRes.status}`);
  }
  const keyData = await keyRes.json();
  const keyId = keyData.key_id;

  // --- Process each bundle ---
  for (const row of rows) {
    const bundleId = row.bundle_id;
    const requestToken = `token-${bundleId}`;

    // Check for existing publication (idempotent re-run)
    const existing = conn.all(
      `SELECT * FROM publications WHERE bundle_id = ?`,
      bundleId
    );

    if (existing.length > 0) {
      const pub = existing[0];
      console.log(`BUNDLE ${bundleId} SIGNED KEY=${keyId}`);
      console.log(`BUNDLE ${bundleId} PUBLISHED RECEIPT=${pub.publication_id} TOKEN=${pub.request_token} STATUS=${pub.status}`);
      continue;
    }

    // Build canonical JSON descriptor (sorted keys, no whitespace)
    const descriptor = JSON.stringify({
      artifact_count: row.artifact_count,
      bundle_id: bundleId,
      total_bytes: Number(row.total_bytes),
    });

    // Sign descriptor with current key via OpenSSL CMS
    const sigPem = signDescriptor(descriptor);

    console.log(`BUNDLE ${bundleId} SIGNED KEY=${keyId}`);

    // Submit signed descriptor to gateway
    const pubRes = await fetch(`${GATEWAY_BASE}/v1/publications`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        descriptor,
        signature: sigPem,
        request_token: requestToken,
      }),
    });

    const receipt = await pubRes.json();

    if (receipt.status !== 'PUBLISHED') {
      console.error(`ERROR: ${bundleId} rejected — ${JSON.stringify(receipt)}`);
      process.exit(1);
    }

    // Persist receipt for idempotency
    conn.run(
      `INSERT INTO publications (bundle_id, request_token, publication_id, status) VALUES (?, ?, ?, ?)`,
      bundleId,
      requestToken,
      receipt.publication_id,
      receipt.status
    );

    console.log(`BUNDLE ${bundleId} PUBLISHED RECEIPT=${receipt.publication_id} TOKEN=${requestToken} STATUS=${receipt.status}`);
  }

  db.close();
}

function signDescriptor(descriptor) {
  const scratch = mkdtempSync(join(tmpdir(), 'pub-'));
  const descFile = join(scratch, 'descriptor.bin');

  try {
    writeFileSync(descFile, descriptor, 'utf-8');

    const sig = execFileSync('openssl', [
      'cms', '-sign',
      '-in', descFile,
      '-signer', keyPath('current', 'current.cert.pem'),
      '-inkey', keyPath('current', 'current.key.pem'),
      '-outform', 'PEM',
      '-binary',
    ], { encoding: 'utf-8' });

    return sig;
  } finally {
    rmSync(scratch, { recursive: true, force: true });
  }
}

main().catch(err => {
  console.error('Publisher failed:', err.message);
  process.exit(1);
});