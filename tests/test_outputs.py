"""Verifier tests for the firmware release publisher task.

Each test maps to a functional_criteria[] entry. The verifier:
1. Starts the distribution gateway in the background
2. Runs `npm run report` (the candidate's publisher)
3. Asserts stdout matches the golden output (RECEIPT masked)
4. Inspects DuckDB state for persisted receipts
5. Drives the gateway directly to confirm signature verification works
"""

from __future__ import annotations

import csv
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

# --- Paths ---
APP = Path(os.environ.get("APP_DIR", "/app"))
GATEWAY_DIR = APP / "distribution-gateway"
GATEWAY_BASE = "http://127.0.0.1:7070"
DB_PATH = APP / "releases.duckdb"
MANIFEST_PATH = APP / "fixtures" / "build_manifest.csv"
GOLDEN_PATH = APP / "reports" / "publications.expected.txt"
KEYS_DIR = APP / "keys"

CURRENT_CERT = KEYS_DIR / "current" / "current.cert.pem"
CURRENT_KEY = KEYS_DIR / "current" / "current.key.pem"
REVOKED_CERT = KEYS_DIR / "revoked" / "revoked.cert.pem"
REVOKED_KEY = KEYS_DIR / "revoked" / "revoked.key.pem"

# --- Helpers ---


def start_gateway():
    """Start the gateway as a background process and wait for readiness."""
    env = os.environ.copy()
    env["CURRENT_CERT_PATH"] = str(CURRENT_CERT)
    proc = subprocess.Popen(
        ["node", "server.js"],
        cwd=str(GATEWAY_DIR),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # Wait for readiness (up to 10 s)
    import urllib.request
    import urllib.error

    for _ in range(50):
        try:
            resp = urllib.request.urlopen(f"{GATEWAY_BASE}/healthz", timeout=2)
            if resp.status == 200:
                return proc
        except (urllib.error.URLError, ConnectionError, OSError):
            time.sleep(0.2)
    proc.kill()
    raise RuntimeError("Gateway did not become ready within 10 s")


def stop_gateway(proc):
    """Stop the gateway process."""
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def run_publisher():
    """Run npm run report and return (stdout, returncode)."""
    result = subprocess.run(
        ["npm", "run", "report"],
        cwd=str(APP),
        capture_output=True,
        text=True,
    )
    return result.stdout, result.returncode


def parse_manifest(path):
    """Parse CSV manifest into a list of dicts."""
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def canonical_descriptor(artifact_count, bundle_id, total_bytes):
    """Build canonical JSON descriptor (sorted keys, no whitespace)."""
    import json
    return json.dumps(
        {"artifact_count": artifact_count, "bundle_id": bundle_id, "total_bytes": total_bytes},
        separators=(",", ":"),
    )


def sign_descriptor(descriptor, cert_path, key_path):
    """Sign a descriptor with openssl cms and return PEM signature."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        desc_file = Path(tmp) / "descriptor.bin"
        desc_file.write_text(descriptor, encoding="utf-8")
        result = subprocess.run(
            [
                "openssl", "cms", "-sign",
                "-in", str(desc_file),
                "-signer", str(cert_path),
                "-inkey", str(key_path),
                "-outform", "PEM",
                "-binary",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"openssl cms -sign failed: {result.stderr}")
        return result.stdout


def assert_receipt_matches(line):
    """Assert a PUBLISHED line matches the expected format and return receipt."""
    m = re.match(
        r"^BUNDLE (\S+) PUBLISHED RECEIPT=(\S+) TOKEN=(\S+) STATUS=PUBLISHED$",
        line.strip(),
    )
    assert m, f"PUBLISHED line does not match expected format: {line!r}"
    return m.group(1), m.group(2), m.group(3)


# --- Tests ---


class TestPublisher:
    """Tests that require a running gateway and a working publisher."""

    @classmethod
    def setup_class(cls):
        cls._gw = start_gateway()
        # Remove any stale DB
        if DB_PATH.exists():
            DB_PATH.unlink()
        if DB_PATH.with_suffix(".duckdb.wal").exists():
            DB_PATH.with_suffix(".duckdb.wal").unlink()

    @classmethod
    def teardown_class(cls):
        stop_gateway(cls._gw)

    def test_publisher_runs_successfully(self):
        """functional_criteria[id=report_output_matches]: the publisher runs
        without errors and exits 0."""
        stdout, rc = run_publisher()
        assert rc == 0, f"npm run report exited {rc}:\n{stdout}"

    def test_output_matches_golden(self):
        """functional_criteria[id=report_output_matches]: stdout matches the
        golden output file (with RECEIPT masked)."""
        stdout, _ = run_publisher()

        golden = GOLDEN_PATH.read_text(encoding="utf-8")

        # Mask RECEIPT values before comparing
        stdout_masked = re.sub(r"RECEIPT=\S+", "RECEIPT=<id>", stdout)
        golden_masked = re.sub(r"RECEIPT=\S+", "RECEIPT=<id>", golden)

        assert stdout_masked == golden_masked, (
            f"Output does not match golden.\n"
            f"--- got (masked) ---\n{stdout_masked}\n"
            f"--- expected (masked) ---\n{golden_masked}\n"
        )

    def test_bundles_101_102_103_published(self):
        """functional_criteria[id=withdrawals_and_duplicates_reconciled]:
        BND-101, BND-102, BND-103 appear in output; BND-104 is absent."""
        stdout, _ = run_publisher()
        lines = stdout.strip().split("\n")

        published_bundles = set()
        for line in lines:
            if "PUBLISHED" in line:
                bundle_id, _, _ = assert_receipt_matches(line)
                published_bundles.add(bundle_id)

        assert "BND-101" in published_bundles, "BND-101 not published"
        assert "BND-102" in published_bundles, "BND-102 not published"
        assert "BND-103" in published_bundles, "BND-103 not published"
        assert "BND-104" not in published_bundles, "BND-104 should be absent"

    def test_output_order_deterministic(self):
        """functional_criteria[id=report_output_matches]: bundles appear in
        ascending bundle_id order."""
        stdout, _ = run_publisher()
        lines = stdout.strip().split("\n")
        signed_lines = [l for l in lines if "SIGNED" in l]

        bundle_ids = []
        for line in signed_lines:
            m = re.match(r"^BUNDLE (\S+) SIGNED", line.strip())
            assert m, f"Bad SIGNED line: {line}"
            bundle_ids.append(m.group(1))

        assert bundle_ids == sorted(bundle_ids), (
            f"Bundles not in ascending order: {bundle_ids}"
        )

    def test_published_with_current_key(self):
        """functional_criteria[id=bundles_signed_with_current_key_accepted]:
        all published bundles have STATUS=PUBLISHED (not UNTRUSTED_SIGNATURE)."""
        stdout, _ = run_publisher()
        lines = stdout.strip().split("\n")
        for line in lines:
            if "PUBLISHED" in line:
                assert "STATUS=PUBLISHED" in line, f"Unexpected status: {line}"

    def test_key_id_in_output(self):
        """functional_criteria[id=bundles_signed_with_current_key_accepted]:
        the key_id from the gateway appears in SIGNED lines."""
        stdout, _ = run_publisher()
        lines = stdout.strip().split("\n")
        for line in lines:
            if "SIGNED" in line:
                assert "KEY=fw-signing-2026-current" in line, (
                    f"Wrong key in SIGNED line: {line}"
                )

    def test_receipts_persisted_in_duckdb(self):
        """functional_criteria[id=receipts_and_tokens_persisted_in_duckdb]:
        releases.duckdb contains the gateway receipts."""
        assert DB_PATH.exists(), "releases.duckdb does not exist"

        import duckdb
        con = duckdb.connect(str(DB_PATH))
        rows = con.execute(
            "SELECT bundle_id, request_token, publication_id, status FROM publications ORDER BY bundle_id"
        ).fetchall()
        con.close()

        assert len(rows) == 3, f"Expected 3 publication rows, got {len(rows)}"
        bundle_ids = [r[0] for r in rows]
        assert bundle_ids == ["BND-101", "BND-102", "BND-103"], (
            f"Unexpected bundle_ids: {bundle_ids}"
        )
        for row in rows:
            assert row[3] == "PUBLISHED", f"Status not PUBLISHED for {row[0]}"

    def test_idempotent_rerun(self):
        """functional_criteria[id=idempotent_rerun_no_duplicate_publications]:
        re-running produces identical output and no duplicate gateway rows."""
        stdout1, _ = run_publisher()
        stdout2, _ = run_publisher()

        assert stdout1 == stdout2, "Re-run output differs from first run"

        import duckdb
        con = duckdb.connect(str(DB_PATH))
        count = con.execute("SELECT COUNT(*) FROM publications").fetchone()[0]
        con.close()

        assert count == 3, (
            f"Expected 3 publication rows after re-run, got {count} — "
            "duplicates were created"
        )

    def test_revoked_key_signature_rejected(self):
        """functional_criteria[id=revoked_key_signature_rejected]: a descriptor
        signed with the revoked key is rejected as UNTRUSTED_SIGNATURE."""
        import urllib.request
        import json

        descriptor = canonical_descriptor(1, "BND-TEST", 100)
        sig = sign_descriptor(descriptor, REVOKED_CERT, REVOKED_KEY)
        body = json.dumps({
            "descriptor": descriptor,
            "signature": sig,
            "request_token": f"token-revoked-{uuid.uuid4().hex[:8]}",
        }).encode("utf-8")

        req = urllib.request.Request(
            f"{GATEWAY_BASE}/v1/publications",
            data=body,
            headers={"content-type": "application/json"},
            method="POST",
        )
        resp = urllib.request.urlopen(req)
        assert resp.status == 200, f"Expected 200, got {resp.status}"

        data = json.loads(resp.read().decode("utf-8"))
        assert data.get("error") == "UNTRUSTED_SIGNATURE", (
            f"Expected UNTRUSTED_SIGNATURE, got: {data}"
        )