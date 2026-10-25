from pathlib import Path
from copy import deepcopy
import json

import pipeline.build as build_module
from pipeline.build import build, parse_steps
from pipeline.grib2 import iter_messages
from pipeline.allerte import geometry_fingerprint
from pipeline.source_dpc import DPCError


FIXTURE = Path(__file__).parent / "fixtures" / "ifs-12h-surface.grib2"


class OfflineSource:
    def __init__(self):
        messages = [bytes(message) for message in iter_messages(FIXTURE.read_bytes())]
        self.payloads = dict(zip(("tp", "10u", "10v", "2t"), messages, strict=True))

    def download_parameters(self, model, run, step, parameters, optional=()):
        return {name: self.payloads[name] for name in parameters}, []


def test_native_model_steps():
    assert parse_steps("0-12", "ifs") == [0, 3, 6, 9, 12]
    assert parse_steps("0-12", "aifs-single") == [0, 6, 12]


def test_offline_build_writes_layers_and_manifest(tmp_path):
    manifest = build(
        "ifs",
        "2026091300",
        "12",
        "wind10m,t2m,precip",
        tmp_path,
        source=OfflineSource(),
    )
    assert manifest["steps"][0]["valid_time"] == "2026-09-13T12:00:00Z"
    assert manifest["license"] == "CC-BY-4.0"
    assert set(manifest["steps"][0]["layers"]) == {"wind10m", "t2m", "precip"}
    assert (tmp_path / "manifest.json").is_file()
    assert (tmp_path / "wind10m" / "012.png").is_file()


def test_build_reports_each_completed_step_only_on_stderr(tmp_path, capsys, monkeypatch):
    instants = iter((10.0, 11.25))
    monkeypatch.setattr(build_module, "monotonic", lambda: next(instants), raising=False)

    build(
        "ifs",
        "2026091300",
        "12",
        "wind10m,t2m,precip",
        tmp_path,
        source=OfflineSource(),
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "passo 12 completato in 1.25 s\n"



def test_manifest_dichiara_origine_della_griglia(tmp_path):
    """La griglia ECMWF parte a 180 gradi est: senza origine dichiarata il client
    disegna il mondo ruotato di meta' senza che nulla segnali l'errore."""
    manifest = build(
        "ifs", "2026091300", "12", "wind10m,t2m,precip", tmp_path, source=OfflineSource()
    )
    grid = manifest["grid"]
    assert grid["width"] == 1440 and grid["height"] == 721
    assert grid["lon0"] == -180.0, "origine longitudine mancante o sbagliata"
    assert grid["lat0"] == 90.0
    assert grid["dlon"] == 0.25 and grid["dlat"] == 0.25
    assert "scanning_mode" in grid


def test_build_publishes_dpc_and_keeps_unchanged_geometry(tmp_path, dpc_pair, monkeypatch):
    manifest = build("ifs", "2026091300", "12", "t2m", tmp_path, source=OfflineSource())
    entry = manifest["allerte"]
    assert entry == {
        "url": "allerte.json", "zone_url": "zone-allerta.json", "bollettino": "20261025_1326",
        "emesso": "2026-10-25T13:26:00+01:00", "impronta_zone": geometry_fingerprint(dpc_pair[0]),
        "stato": "aggiornato",
    }
    assert json.loads((tmp_path / entry["url"]).read_text(encoding="utf-8"))["bollettino"] == entry["bollettino"]
    zone_file = tmp_path / entry["zone_url"]
    previous = zone_file.read_bytes(), zone_file.stat().st_mtime_ns

    def must_not_regenerate(*args, **kwargs):
        raise AssertionError("geometrie identiche non devono essere rigenerate")

    monkeypatch.setattr(build_module, "simplify_zones", must_not_regenerate)
    repeated = build("ifs", "2026091300", "12", "t2m", tmp_path, source=OfflineSource())
    assert repeated["allerte"]["stato"] == "aggiornato"
    assert (zone_file.read_bytes(), zone_file.stat().st_mtime_ns) == previous


def test_changed_geometry_is_regenerated(tmp_path, dpc_pair):
    first = build("ifs", "2026091300", "12", "t2m", tmp_path, source=OfflineSource())
    changed = deepcopy(dpc_pair[0])
    # Traslazione completa: cambia davvero il file geometrico, senza aprire gli anelli.
    for point in changed["features"][0]["geometry"]["coordinates"][0]:
        point[0] += 0.1

    class ChangedDPC:
        def latest_bulletin(self):
            return "20261026_1326", changed, dpc_pair[1]

    previous = (tmp_path / "zone-allerta.json").read_bytes()
    second = build("ifs", "2026091300", "12", "t2m", tmp_path,
                   source=OfflineSource(), dpc_source=ChangedDPC())
    assert second["allerte"]["stato"] == "aggiornato"
    assert second["allerte"]["impronta_zone"] != first["allerte"]["impronta_zone"]
    assert (tmp_path / "zone-allerta.json").read_bytes() != previous


class FailedDPC:
    def latest_bulletin(self):
        raise DPCError("host=api.github.com HTTP=503; tentativi=3")


def test_failed_dpc_keeps_last_good_files_and_ecmwf_continues(tmp_path, capsys):
    first = build("ifs", "2026091300", "12", "t2m", tmp_path, source=OfflineSource())
    old_files = {name: (tmp_path / name).read_bytes() for name in ("allerte.json", "zone-allerta.json")}
    second = build("ifs", "2026091306", "12", "t2m", tmp_path,
                   source=OfflineSource(), dpc_source=FailedDPC())
    assert second["run"] == "2026091306"
    assert second["steps"][0]["layers"]["t2m"]
    assert second["allerte"] == {**first["allerte"], "stato": "non aggiornato"}
    assert all((tmp_path / name).read_bytes() == content for name, content in old_files.items())
    assert second["warnings"][-1]["source"] == "DPC"
    assert "::warning::DPC non aggiornato: DPCError: host=api.github.com HTTP=503" in capsys.readouterr().err
    assert json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8")) == second


def test_first_dpc_failure_has_warning_without_fabricated_alerts(tmp_path):
    manifest = build("ifs", "2026091300", "12", "t2m", tmp_path,
                     source=OfflineSource(), dpc_source=FailedDPC())
    assert "allerte" not in manifest
    assert not (tmp_path / "allerte.json").exists()
    assert manifest["warnings"][-1]["source"] == "DPC"


def test_unknown_level_preserves_previous_files(tmp_path, dpc_pair):
    first = build("ifs", "2026091300", "12", "t2m", tmp_path, source=OfflineSource())
    previous = (tmp_path / "allerte.json").read_bytes()
    dpc_pair[0]["features"][0]["properties"]["Per rischio idraulico"] = "INDEFINITO"

    class InvalidDPC:
        def latest_bulletin(self):
            return "20261026_1326", *dpc_pair

    result = build("ifs", "2026091300", "12", "t2m", tmp_path,
                   source=OfflineSource(), dpc_source=InvalidDPC())
    assert result["allerte"] == {**first["allerte"], "stato": "non aggiornato"}
    assert (tmp_path / "allerte.json").read_bytes() == previous
