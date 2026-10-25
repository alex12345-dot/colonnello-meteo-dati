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


@pytest.mark.parametrize("previous_vigilanza,current_vigilanza,skip", [
    ({"data": "2026-10-25", "stato": "aggiornato"}, {"data": "2026-10-25", "stato": "aggiornato"}, True),
    ({"data": "2026-10-25", "stato": "aggiornato"}, {"data": "2026-10-26", "stato": "aggiornato"}, False),
    ({"data": "2026-10-25", "stato": "aggiornato"}, {"data": "2026-10-25", "stato": "non aggiornato"}, False),
    ({"data": "2026-10-25", "stato": "non aggiornato"}, {"data": "2026-10-25", "stato": "aggiornato"}, False),
    (None, {"data": "2026-10-25", "stato": "aggiornato"}, False),
    ({"data": "2026-10-25", "stato": "aggiornato"}, None, False),
    (None, None, True),
])
def test_publish_vigilanza_changes_with_same_run_and_alerts(tmp_path, previous_vigilanza, current_vigilanza, skip):
    (tmp_path / "out").mkdir()
    (tmp_path / "dpc-precedente").mkdir()
    previous = {"run": "2026102506", "allerte": {"bollettino": "20261025_1326", "stato": "aggiornato"}}
    current = {**previous}
    if previous_vigilanza is not None:
        previous["vigilanza"] = previous_vigilanza
    if current_vigilanza is not None:
        current["vigilanza"] = current_vigilanza
    (tmp_path / "dpc-precedente/manifest.json").write_text(json.dumps(previous), encoding="utf-8")
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
    expected = hashlib.sha256(json.dumps(current_vigilanza, sort_keys=True).encode()).hexdigest()
    assert f"VIGILANZA_SHA={expected}" in values


def test_restore_includes_all_dpc_products_before_each_build_attempt():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    products = re.search(r"for nome in (.*?); do\s+if git cat-file -e", workflow).group(1).split()
    assert {"allerte.json", "zone-allerta.json", "vigilanza.json"} <= set(products)
    assert 'git show "FETCH_HEAD:$nome" > "$RUNNER_TEMP/dpc-precedente/$nome"' in workflow
    build_function = workflow.split("costruisci() {", 1)[1].split("\n          }", 1)[0]
    assert build_function.index('cp "$RUNNER_TEMP/dpc-precedente/"*.json out/') < build_function.index("python pipeline/build.py")


@pytest.mark.parametrize("stale", [False, True])
def test_pages_check_rejects_stale_vigilanza_with_current_run_and_alerts(tmp_path, stale):
    manifest = {"run": "2026102506", "allerte": {"bollettino": "20261025_1326"},
                "vigilanza": {"data": "2026-10-25", "stato": "aggiornato"},
                "steps": [0], "grid": {"lon0": -180.0}}
    alerts_hash = hashlib.sha256(json.dumps(manifest["allerte"], sort_keys=True).encode()).hexdigest()
    vigilanza_hash = hashlib.sha256(json.dumps(manifest["vigilanza"], sort_keys=True).encode()).hexdigest()
    if stale:
        manifest["vigilanza"]["data"] = "2026-10-24"
    (tmp_path / "letto.json").write_text(json.dumps(manifest), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-c", python_blocks()[1]], cwd=tmp_path,
        env={**os.environ, "NUOVA": manifest["run"], "ALLERTE_SHA": alerts_hash, "VIGILANZA_SHA": vigilanza_hash},
        capture_output=True, text=True,
    )
    assert result.returncode == (3 if stale else 0), result.stderr
    if stale:
        assert "voce vigilanza diversa" in result.stdout
