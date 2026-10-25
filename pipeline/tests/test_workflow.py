"""Esegue offline la decisione di pubblicazione effettiva del workflow."""

import ast
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import textwrap

import pytest


WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/dati.yml"


def python_blocks():
    return [textwrap.dedent(block) for block in re.findall(
        r"python - <<'PY'\n(.*?)\n          PY", WORKFLOW.read_text(encoding="utf-8"), re.DOTALL,
    )]


def test_embedded_python_syntax():
    blocks = python_blocks()
    assert len(blocks) == 2
    for block in blocks:
        ast.parse(block)


@pytest.mark.parametrize("run,bulletin,state,skip", [
    ("2026102506", "20261025_1326", "aggiornato", True),
    ("2026102506", "20261025_1600", "aggiornato", False),
    ("2026102506", "20261025_1326", "non aggiornato", False),
    ("2026102512", "20261025_1326", "aggiornato", False),
])
def test_publish_new_alert_even_with_same_ecmwf_run(tmp_path, run, bulletin, state, skip):
    (tmp_path / "out").mkdir()
    old_folder = tmp_path / "dpc-precedente"
    old_folder.mkdir()
    previous = {"run": "2026102506", "allerte": {"bollettino": "20261025_1326", "stato": "aggiornato"}}
    current = {"run": run, "allerte": {"bollettino": bulletin, "stato": state}}
    (old_folder / "manifest.json").write_text(json.dumps(previous), encoding="utf-8")
    (tmp_path / "out/manifest.json").write_text(json.dumps(current), encoding="utf-8")
    env_file = tmp_path / "env.txt"
    result = subprocess.run(
        [sys.executable, "-c", python_blocks()[0]], cwd=tmp_path,
        env={**os.environ, "RUNNER_TEMP": str(tmp_path), "GITHUB_ENV": str(env_file)},
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    values = env_file.read_text(encoding="utf-8")
    assert ("SALTA=1" in values) == skip
    expected = hashlib.sha256(json.dumps(current["allerte"], sort_keys=True).encode()).hexdigest()
    assert f"ALLERTE_SHA={expected}" in values
