"""Esegue offline la decisione di pubblicazione effettiva del workflow."""

import ast
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
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


@pytest.mark.parametrize("ls_status", [0, 2, 128])
def test_workflow_requires_known_online_state_if_fetch_fails(tmp_path, ls_status):
    bash = shutil.which("bash.exe" if os.name == "nt" else "bash")
    if bash is None:
        pytest.skip("bash richiesto per eseguire il blocco del workflow")
    workflow = WORKFLOW.read_text(encoding="utf-8")
    restore = workflow.split("id: stato", 1)[1].split("run: |\n", 1)[1].split("\n      - name:", 1)[0]
    script = textwrap.dedent(restore)
    git = 'git() { if [ "$1" = fetch ]; then return 1; fi; return "$LS_STATUS"; }\n'
    env = {**os.environ, "LS_STATUS": str(ls_status), "RUNNER_TEMP": ".", "GITHUB_OUTPUT": "outputs"}
    result = subprocess.run([bash, "-eo", "pipefail", "-c", git + script], cwd=tmp_path,
                            env=env, capture_output=True, text=True, encoding="utf-8")
    if ls_status == 2:
        assert result.returncode == 0, result.stderr
        assert (tmp_path / "outputs").read_text() == "precedente=none\n"
    else:
        assert result.returncode == 1, result.stderr
        assert "impossibile verificare la corsa online" in result.stdout
        assert not (tmp_path / "outputs").exists()


@pytest.mark.parametrize("changed,skip", [(False, True), (True, False)])
def test_publish_new_layers_even_when_ecmwf_run_and_dpc_are_unchanged(tmp_path, changed, skip):
    old_layers = ["wind10m", "t2m", "precip"]
    new_layers = old_layers + ["gust", "msl", "tcc", "rh2m", "cape", "snow", "swh", "precip_rate"]
    (tmp_path / "out").mkdir()
    (tmp_path / "dpc-precedente").mkdir()
    previous = {"run": "2026102606", "layers": old_layers}
    current = {**previous, "layers": new_layers if changed else old_layers}
    (tmp_path / "dpc-precedente/manifest.json").write_text(json.dumps(previous), encoding="utf-8")
    (tmp_path / "out/manifest.json").write_text(json.dumps(current), encoding="utf-8")
    env_file = tmp_path / "env.txt"
    result = subprocess.run(
        [sys.executable, "-c", python_blocks()[0]], cwd=tmp_path,
        env={**os.environ, "RUNNER_TEMP": str(tmp_path), "GITHUB_ENV": str(env_file)},
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert ("SALTA=1" in env_file.read_text(encoding="utf-8")) == skip
    layers_argument = re.search(r"--layers (\S+)", WORKFLOW.read_text(encoding="utf-8")).group(1)
    assert layers_argument.split(",") == new_layers


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
        env={**os.environ, "NUOVA": manifest["run"], "ALLERTE_SHA": alerts_hash, "VIGILANZA_SHA": vigilanza_hash,
             "PRECIP_EU_SHA": hashlib.sha256(b"null").hexdigest(), "TEMPORALI_EU_SHA": hashlib.sha256(b"null").hexdigest()},
        capture_output=True, text=True,
    )
    assert result.returncode == (3 if stale else 0), result.stderr
    if stale:
        assert "voce vigilanza diversa" in result.stdout


@pytest.mark.parametrize("old_run,new_run,skip", [
    ("2026110200", "2026110200", True),
    ("2026110200", "2026110206", False),
    (None, "2026110200", False),
    ("2026110200", None, False),
    (None, None, True),
])
def test_publish_icon_eu_changes_with_same_ecmwf_and_dpc(tmp_path, old_run, new_run, skip):
    (tmp_path / "out").mkdir()
    (tmp_path / "dpc-precedente").mkdir()
    previous = {"run": "2026110200", "allerte": {}, "vigilanza": {}}
    current = {**previous}
    if old_run is not None:
        previous["precip_eu"] = {"run": old_run}
    if new_run is not None:
        current["precip_eu"] = {"run": new_run}
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
    expected = hashlib.sha256(json.dumps(current.get("precip_eu"), sort_keys=True).encode()).hexdigest()
    assert f"PRECIP_EU_SHA={expected}" in values


@pytest.mark.parametrize("stale", [False, True])
def test_pages_check_rejects_old_icon_eu_with_current_ecmwf_and_dpc(tmp_path, stale):
    manifest = {"run": "2026110200", "allerte": {}, "vigilanza": {},
                "precip_eu": {"run": "2026110206"}, "steps": [0], "grid": {"lon0": -180.0}}
    def digest(key):
        return hashlib.sha256(json.dumps(manifest.get(key), sort_keys=True).encode()).hexdigest()
    env = {**os.environ, "NUOVA": manifest["run"], "ALLERTE_SHA": digest("allerte"),
           "VIGILANZA_SHA": digest("vigilanza"), "PRECIP_EU_SHA": digest("precip_eu"),
           "TEMPORALI_EU_SHA": digest("temporali_eu")}
    if stale:
        manifest["precip_eu"]["run"] = "2026110200"
    (tmp_path / "letto.json").write_text(json.dumps(manifest), encoding="utf-8")
    result = subprocess.run([sys.executable, "-c", python_blocks()[1]], cwd=tmp_path,
                            env=env, capture_output=True, text=True)
    assert result.returncode == (3 if stale else 0), result.stderr
    if stale:
        assert "voce precip_eu diversa" in result.stdout


@pytest.mark.parametrize("next_run,skip", [("2026112000", True), ("2026112006", False)])
def test_workflow_publishes_only_new_temporali_with_other_sources_unchanged(tmp_path, next_run, skip):
    (tmp_path / "out").mkdir()
    (tmp_path / "dpc-precedente").mkdir()
    previous = {"run": "2026111912", "temporali_eu": {"run": "2026112000"}}
    current = {**previous, "temporali_eu": {"run": next_run}}
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
    expected = hashlib.sha256(json.dumps(current["temporali_eu"], sort_keys=True).encode()).hexdigest()
    assert f"TEMPORALI_EU_SHA={expected}" in values
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert '--temporali-eu --previous-output "$RUNNER_TEMP/dpc-precedente"' in workflow
    assert "precip_rate temporali_eu)" in workflow


@pytest.mark.parametrize("stale", [False, True])
def test_pages_check_rejects_stale_temporali_with_current_other_sources(tmp_path, stale):
    manifest = {"run": "2026111912", "steps": [0], "grid": {"lon0": -180.0},
                "temporali_eu": {"run": "2026112006"}}
    expected = hashlib.sha256(json.dumps(manifest["temporali_eu"], sort_keys=True).encode()).hexdigest()
    null = hashlib.sha256(b"null").hexdigest()
    env = {**os.environ, "NUOVA": manifest["run"], "ALLERTE_SHA": null, "VIGILANZA_SHA": null,
           "PRECIP_EU_SHA": null, "TEMPORALI_EU_SHA": expected}
    if stale:
        manifest["temporali_eu"]["run"] = "2026112000"
    (tmp_path / "letto.json").write_text(json.dumps(manifest), encoding="utf-8")
    result = subprocess.run([sys.executable, "-c", python_blocks()[1]], cwd=tmp_path,
                            env=env, capture_output=True, text=True)
    assert result.returncode == (3 if stale else 0), result.stderr
    if stale:
        assert "voce temporali_eu diversa" in result.stdout


def test_workflow_publishes_stale_status_change_for_same_weather_cycle(tmp_path):
    out, old = tmp_path / "out", tmp_path / "dpc-precedente"
    out.mkdir()
    old.mkdir()
    entry = {"run": "2026112000", "stale": False}
    previous = {"run": "2026111912", "temporali_eu": entry}
    current = {**previous, "temporali_eu": {**entry, "stale": True}}
    (old / "manifest.json").write_text(json.dumps(previous), encoding="utf-8")
    (out / "manifest.json").write_text(json.dumps(current), encoding="utf-8")
    env_file = tmp_path / "env.txt"
    result = subprocess.run([sys.executable, "-c", python_blocks()[0]], cwd=tmp_path,
                            env={**os.environ, "RUNNER_TEMP": str(tmp_path), "GITHUB_ENV": str(env_file)},
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "SALTA=1" not in env_file.read_text(encoding="utf-8")
