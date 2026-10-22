"""trust_store_check: fingerprint matching, bundle discovery and the uv floor (offline)."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "trust_store_check", Path(__file__).resolve().parents[1] / "scripts" / "trust_store_check.py"
)
tsc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tsc)

BAD, GOOD = b"compromised root der", b"healthy root der"


def pem(der: bytes) -> str:
    b64 = base64.b64encode(der).decode()
    body = "\n".join(b64[i:i + 64] for i in range(0, len(b64), 64))
    return f"=== /CN=x\n-----BEGIN CERTIFICATE-----\n{body}\n-----END CERTIFICATE-----\n"


def test_fingerprints_ignore_labels_and_wrapping():
    assert tsc.fingerprints(pem(BAD) + pem(GOOD)) == {
        hashlib.sha256(BAD).hexdigest(), hashlib.sha256(GOOD).hexdigest()
    }


def test_real_fingerprints_are_sha256_hex():
    assert all(len(f) == 64 and int(f, 16) >= 0 for f in tsc.COMPROMISED_ROOTS)


def _run(tmp_path, monkeypatch, bundle_text, uv):
    (tmp_path / "bundle.pem").write_text(bundle_text)
    monkeypatch.setattr(tsc, "BUNDLES", [str(tmp_path / "bundle.pem")])
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.setattr(tsc, "COMPROMISED_ROOTS", {hashlib.sha256(BAD).hexdigest(): "Bad Root"})
    monkeypatch.setattr(tsc, "uv_version", lambda: uv)
    return tsc.main()


def test_compromised_bundle_fails(tmp_path, monkeypatch, capsys):
    assert _run(tmp_path, monkeypatch, pem(GOOD) + pem(BAD), (0, 13, 5)) == 1
    assert "Bad Root" in capsys.readouterr().out


def test_clean_bundle_and_fixed_uv_pass(tmp_path, monkeypatch):
    assert _run(tmp_path, monkeypatch, pem(GOOD), (0, 13, 4)) == 0
    assert _run(tmp_path, monkeypatch, pem(GOOD), None) == 0  # no uv installed


def test_old_uv_fails(tmp_path, monkeypatch, capsys):
    assert _run(tmp_path, monkeypatch, pem(GOOD), (0, 12, 23)) == 1
    assert "brew upgrade uv" in capsys.readouterr().out
