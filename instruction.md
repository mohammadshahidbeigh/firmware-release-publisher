# Firmware Release Publisher

## Background

Release Engineering recently rotated the firmware code-signing key and revoked the previous signing certificate as part of a planned security update. The legacy publisher responsible for generating firmware release bundles was not updated to use the new signing credentials and continues to sign all artifacts with the revoked key. As each bundle reaches the distribution gateway, the gateway validates its digital signature against the current trust store, detects that the signing certificate is no longer trusted, and rejects the upload with `UNTRUSTED_SIGNATURE`.

Your task is to write a replacement publisher that reads the build manifest, reconciles it, signs each publishable bundle with the **current** key, submits it to the gateway over HTTP, persists receipts for idempotency, and prints deterministic status lines.

## Deliverable

`/app/publisher/release-publisher.mjs`

Run via:

```
npm run report    # = node publisher/release-publisher.mjs --report
```

## Environment

Everything lives under `/app` inside the container:

| Path | Description |
| --- | --- |
| `fixtures/build_manifest.csv` | Raw build manifest (40 rows) |
| `reports/publications.expected.txt` | Golden output your program must reproduce |
| `package.json` | Entry: `npm run report`; dependency: `duckdb` |
| `distribution-gateway/` | Express service on `http://127.0.0.1:7070` |
| `keys/current/` | Active signing keypair (`current.key.pem`, `current.cert.pem`) |
| `keys/revoked/` | Rotated-out keypair — signing with it fails |
| `publisher/` | Your `release-publisher.mjs` goes here |

You create `releases.duckdb` at runtime.

### Manifest schema

```
entry_id,bundle_id,component_id,version,size_bytes,record_type,supersedes_id,recorded_at
```

- `record_type` is `BUILD` or `WITHDRAWAL`
- A `WITHDRAWAL` row's `supersedes_id` is the `entry_id` of the `BUILD` it cancels

## Reconciliation rules

Derive the set of **publishable bundles** using SQL in DuckDB:

1. **Collapse exact duplicates.** Rows identical across every column are the same record emitted twice — count them once.
2. **Apply withdrawals.** A build referenced by a `WITHDRAWAL` (via `supersedes_id`) is cancelled and is not part of any release.
3. A bundle is publishable if it still has at least one surviving build. A bundle whose every build was withdrawn (e.g. `BND-104`) is skipped entirely.

For each publishable bundle, compute `artifact_count` (number of surviving builds) and `total_bytes` (sum of their `size_bytes`).

## Canonical release descriptor

The descriptor is UTF-8 JSON with lexicographically sorted object keys and no insignificant whitespace. Example:

```json
{"artifact_count":3,"bundle_id":"BND-900","total_bytes":148096}
```

The bytes you sign must be exactly the bytes you send as `descriptor` in the POST body. If they differ by even one character, signature verification fails.

## Signing

Sign the canonical descriptor with the **current** key using OpenSSL detached CMS:

```
openssl cms -sign -in /tmp/descriptor.bin \
  -signer /app/keys/current/current.cert.pem \
  -inkey  /app/keys/current/current.key.pem \
  -outform PEM -binary
```

The gateway verifies against the current certificate. Signing with `keys/revoked/` produces `UNTRUSTED_SIGNATURE`.

## Gateway contract

Base URL: `http://127.0.0.1:7070`

### GET /v1/signing-key/current

Returns:
```json
{"key_id": "fw-signing-2026-current", "algorithm": "sha256WithRSAEncryption", "certificate_ref": "/app/keys/current/current.cert.pem", "status": "current"}
```

Use `key_id` in the output line.

### POST /v1/publications

Request body:
```json
{
  "descriptor": "{\"artifact_count\":1,\"bundle_id\":\"BND-101\",\"total_bytes\":100}",
  "signature": "-----BEGIN CMS-----...",
  "request_token": "token-BND-101"
}
```

- `descriptor`: canonical JSON string (the exact bytes you signed)
- `signature`: detached CMS signature (PEM)
- `request_token`: deterministic idempotency token, format `token-<bundle_id>`

On success (200): `{"publication_id": "pub_...", "request_token": "token-BND-101", "status": "PUBLISHED"}`

On bad signature (400): `{"error": "UNTRUSTED_SIGNATURE", "message": "..."}`

Re-posting the same `request_token` returns the original receipt — no duplicate is created.

## Output format

Two lines per publishable bundle in ascending `bundle_id` order:

```
BUNDLE <bundle_id> SIGNED KEY=<key_id>
BUNDLE <bundle_id> PUBLISHED RECEIPT=<publication_id> TOKEN=<request_token> STATUS=PUBLISHED
```

Example:
```
BUNDLE BND-101 SIGNED KEY=fw-signing-2026-current
BUNDLE BND-101 PUBLISHED RECEIPT=pub_abc123 TOKEN=token-BND-101 STATUS=PUBLISHED
```

## Idempotency

Store each `request_token`, `publication_id`, and status in `releases.duckdb`. On re-run, read stored receipts instead of re-submitting. A second run must produce byte-identical output and no duplicate gateway publications.

## Boundaries

- Interact with the gateway **only over HTTP**. Do not read or write `distribution-gateway/data/gateway.json`.
- Do not disable or bypass signature verification.
- Do not sign with the revoked key.
- Do not hardcode golden text, receipt ids, or row counts — derive everything from the manifest.
- Output ordering is deterministic — sort by `bundle_id`.

## Self-check

```bash
# Reproduce golden output (RECEIPT masked by verifier)
npm run report > /tmp/out.txt
diff <(sed -E 's/RECEIPT=[^ ]+/RECEIPT=<id>/' reports/publications.expected.txt) \
     <(sed -E 's/RECEIPT=[^ ]+/RECEIPT=<id>/' /tmp/out.txt)

# Confirm idempotency
npm run report > /tmp/a.txt
npm run report > /tmp/b.txt
diff /tmp/a.txt /tmp/b.txt    # must be empty
```