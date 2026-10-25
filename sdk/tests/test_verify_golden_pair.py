"""orcha verify must reject the golden pair file, not treat it as an envelope."""

from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path

from emerge import cli


def test_verify_golden_pair_explains_the_shape(tmp_path: Path, capsys) -> None:
    pair = {
        "description": "fixture",
        "source": "test",
        "valid": {"schema": "orcha.run-attestation/v1"},
        "tampered": {"schema": "orcha.run-attestation/v1"},
    }
    path = tmp_path / "run-attestation-golden.json"
    path.write_text(json.dumps(pair), encoding="utf-8")
    rc = cli.cmd_verify(Namespace(envelope=str(path), resolve_did=False, json=False))
    err = capsys.readouterr().err
    assert rc == 2
    assert "golden" in err.lower()
    assert "jq .valid" in err


def test_verify_real_envelope_is_not_a_pair() -> None:
    assert (
        cli._is_golden_pair({"schema": "orcha.run-attestation/v1", "run_id": "x"})
        is False
    )
    assert cli._is_golden_pair({"valid": {}, "tampered": {}}) is True
