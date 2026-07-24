# Author Notes — Firmware Release Publisher

## Design overview

The publisher is a single Node.js ESM module (`environment/publisher/release-publisher.mjs`) that performs five steps sequentially:

1. **CSV import** into DuckDB via `read_csv_auto`
2. **SQL reconciliation**: `SELECT DISTINCT` collapses exact duplicates; a subquery on `WITHDRAWAL` rows collects cancelled `entry_id` values; the main query keeps only `BUILD` rows whose `entry_id` is not cancelled
3. **Key metadata fetch**: `GET /v1/signing-key/current` from the gateway to learn the active `key_id`
4. **Sign + submit loop**: for each publishable bundle (ordered by `bundle_id`): canonicalize descriptor, sign via `openssl cms -sign` with the current key, POST to gateway, capture receipt, persist in DuckDB
5. **Deterministic output**: two lines per bundle

## Key design choices

### Why `solution/publish.sh` copies a separate `release-publisher.mjs`

The grader needs to install a known-good reference before running the test suite. Rather than embedding the solution inline in a shell script, copying a standalone `.mjs` file keeps the reference solution independently testable (`npm run report`) and matches the same file the candidate delivers.

### Why DuckDB for receipt storage

The pipeline already depends on DuckDB for reconciliation. Using the same database for idempotency avoids adding another dependency (SQLite, a JSON file) and makes it easy to verify the DB state in the grader.

### Why `request_token = token-<bundle_id>`

Deterministic tokens let the gateway perform idempotent replay without the publisher storing client-side state on first run. The binding between bundle and token is implicit in the token name rather than requiring a separate lookup table.

### Why `record_type` is explicit rather than inferred

The manifest uses `record_type = 'WITHDRAWAL'` rows rather than negative `size_bytes` or a separate withdrawal manifest. This is explicit and unambiguous — no risk of misinterpreting a zero-byte build as a withdrawal.

## Traps

### 1. Wrong-key trap

The `keys/revoked/` directory contains a keypair that reproduces the production failure. A naive publisher that signs with the first keypair it finds will produce `UNTRUSTED_SIGNATURE`. The solution must explicitly use `keys/current/`.

### 2. Canonical byte agreement

The signed bytes and the POSTed `descriptor` field must be identical. `JSON.stringify()` in Node produces sorted-key output by default for flat objects, but the gateway's `canonicalEncode` function also sorts keys recursively. If the publisher uses a different serialization (e.g. extra spaces, different key order) the signature fails.

### 3. Withdrawal semantics

A `WITHDRAWAL` cancels a single `BUILD` by `entry_id`, not by `bundle_id`. Multiple withdrawals may target builds in the same bundle. A bundle whose every build is withdrawn (`BND-104`) must be omitted from output entirely.

### 4. Duplicate rows

Three exact-duplicate rows exist in the CSV (identical across all columns). `SELECT DISTINCT *` handles this, but a naive `GROUP BY` without dedup first would double-count the collapsed rows.

### 5. Idempotency

Without DuckDB persistence, re-running the publisher would create duplicate publications (the gateway would see new `request_token` values). The publisher checks `publications` table before signing/submitting, and reuses stored receipts.

### 6. Output ordering

`ORDER BY bundle_id` is required. The golden file expects `BND-101, BND-102, BND-103` in that order.

## Proof: grader scores

### Empty run (no solution) → reward 0

With no `publisher/release-publisher.mjs` present, `npm run report` fails with MODULE_NOT_FOUND. The grader receives no output and scores 0.

### With reference solution → reward 1

The reference publisher:
- Loads and reconciles the manifest → 3 publishable bundles (BND-104 excluded)
- Signs each descriptor with the current key → gateway returns `PUBLISHED`
- Persists receipts in `releases.duckdb`
- Prints deterministic output matching the golden file (RECEIPT masked)

All grader assertions pass → reward 1.

## Expected bundle output

```
BUNDLE BND-101 SIGNED KEY=fw-signing-2026-current
BUNDLE BND-101 PUBLISHED RECEIPT=pub-<random> TOKEN=token-BND-101 STATUS=PUBLISHED
BUNDLE BND-102 SIGNED KEY=fw-signing-2026-current
BUNDLE BND-102 PUBLISHED RECEIPT=pub-<random> TOKEN=token-BND-102 STATUS=PUBLISHED
BUNDLE BND-103 SIGNED KEY=fw-signing-2026-current
BUNDLE BND-103 PUBLISHED RECEIPT=pub-<random> TOKEN=token-BND-103 STATUS=PUBLISHED
```

BND-104 is omitted (all builds withdrawn).