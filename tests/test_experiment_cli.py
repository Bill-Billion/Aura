from __future__ import annotations

import json

from backend.experiments.cli import main


def test_cli_resolve_and_summarize(tmp_path, capsys) -> None:
    exit_code = main(
        ["validate-pilot", "benchmarks/aurabench-dev/manifest.json"]
    )
    assert exit_code == 0
    validation = json.loads(capsys.readouterr().out)
    assert validation["pairs"] == 8
    assert validation["gate_status"] == "pending"

    exit_code = main(
        [
            "validate-pilot",
            "benchmarks/aurabench-dev/manifest.json",
            "--require-approved",
        ]
    )
    assert exit_code == 1
    assert "not approved" in capsys.readouterr().err

    output = tmp_path / "experiment"
    exit_code = main(
        [
            "resolve",
            "benchmarks/aurabench-dev/matrix.yaml",
            "--output",
            str(output),
        ]
    )
    assert exit_code == 0
    resolved = output / "resolved-matrix.json"
    assert resolved.is_file()
    payload = json.loads(capsys.readouterr().out)
    assert payload["cells"] == 96

    exit_code = main(["summarize", str(resolved), "--output", str(output)])
    assert exit_code == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["completed"] == 0
    assert summary["pending"] == 96


def test_cli_reports_invalid_input_without_traceback(tmp_path, capsys) -> None:
    exit_code = main(
        ["resolve", str(tmp_path / "missing.yaml"), "--output", str(tmp_path)]
    )
    assert exit_code == 1
    assert "error:" in capsys.readouterr().err
