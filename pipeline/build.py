"""CLI per produrre livelli ECMWF, allerte, vigilanza DPC e manifest Colonnello Meteo."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import sys
from time import monotonic

import numpy as np

try:
    from pipeline.allerte import geometry_fingerprint, json_bytes, reduce_alerts, simplify_zones
    from pipeline.encode import encode_masked_scalar, encode_precip_rate, encode_scalar, encode_wind
    from pipeline.grib2 import decode_message, iter_messages
    from pipeline.source_ecmwf import ECMWFSource, IndeterminateRun, RunNotAvailable, normalize_run, run_id
    from pipeline.source_dpc import DPCSource, VigilanzaSource
    from pipeline.source_dwd import DWDSource, build_precip_eu
    from pipeline.temporali_eu import STALE_AFTER_HOURS, build_temporali_eu
    from pipeline.vigilanza import reduce_vigilanza
except ModuleNotFoundError:  # esecuzione diretta dalla cartella pipeline
    from allerte import geometry_fingerprint, json_bytes, reduce_alerts, simplify_zones
    from encode import encode_masked_scalar, encode_precip_rate, encode_scalar, encode_wind
    from grib2 import decode_message, iter_messages
    from source_ecmwf import ECMWFSource, IndeterminateRun, RunNotAvailable, normalize_run, run_id
    from source_dpc import DPCSource, VigilanzaSource
    from source_dwd import DWDSource, build_precip_eu
    from temporali_eu import STALE_AFTER_HOURS, build_temporali_eu
    from vigilanza import reduce_vigilanza


LAYER_PARAMETERS = {
    "wind10m": ("10u", "10v"),
    "t2m": ("2t",),
    "precip": ("tp",),
    "precip_rate": ("tp",),
    "gust": ("10fg",),
    "msl": ("msl",),
    "tcc": ("tcc",),
    "rh2m": ("2t", "2d"),
    "cape": ("mucape",),
    "snow": ("sf",),
    "swh": ("swh",),
}
OPTIONAL_PARAMETERS = {"10fg"}
LAYER_UNITS = {
    "wind10m": "m/s", "t2m": "K", "precip": "m", "gust": "m/s",
    "msl": "Pa", "tcc": "0-1", "rh2m": "%", "cape": "J/kg",
    "snow": "m", "swh": "m", "precip_rate": "mm/h",
}


def relative_humidity(temperature: np.ndarray, dewpoint: np.ndarray) -> np.ndarray:
    """Magnus sull'acqua (17.625, 243.04 °C); ingressi in K, uscita in %."""
    if np.shape(temperature) != np.shape(dewpoint):
        raise ValueError("temperatura e rugiada devono avere la stessa griglia")
    t = np.asarray(temperature, dtype=np.float64) - 273.15
    td = np.asarray(dewpoint, dtype=np.float64) - 273.15
    return np.clip(100.0 * np.exp(17.625 * td / (243.04 + td) - 17.625 * t / (243.04 + t)), 0, 100)


def cloud_fraction(field) -> np.ndarray:
    """La tabella WMO esprime tcc in %, la tabella locale ECMWF in frazione."""
    if field.units not in ("%", "0-1"):
        raise ValueError(f"unita' tcc non supportata: {field.units}")
    return np.clip(field.values / (100.0 if field.units == "%" else 1.0), 0, 1)


def valid_steps(model: str, maximum: int = 360) -> list[int]:
    if model == "ifs":
        return list(range(0, min(maximum, 144) + 1, 3)) + (
            list(range(150, maximum + 1, 6)) if maximum > 144 else []
        )
    return list(range(0, maximum + 1, 6))


def parse_steps(specification: str, model: str) -> list[int]:
    result: set[int] = set()
    native = set(valid_steps(model))
    for part in specification.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start_text, end_text = part.split("-", 1)
            start, end = int(start_text), int(end_text)
            if start > end:
                raise ValueError(f"intervallo passi invertito: {part}")
            result.update(step for step in native if start <= step <= end)
        else:
            result.add(int(part))
    invalid = sorted(result - native)
    if invalid or not result:
        raise ValueError(f"passi non validi per {model}: {invalid or specification}")
    return sorted(result)


def parse_layers(specification: str) -> list[str]:
    layers = list(dict.fromkeys(name.strip() for name in specification.split(",") if name.strip()))
    unknown = [name for name in layers if name not in LAYER_PARAMETERS]
    if unknown:
        raise ValueError("layer non supportati: " + ", ".join(unknown))
    return layers


def _field(payload: bytes):
    messages = list(iter_messages(payload))
    if len(messages) != 1:
        raise ValueError(f"atteso un messaggio GRIB, trovati {len(messages)}")
    return decode_message(messages[0])


def update_alerts(destination: Path, previous: dict | None, source: DPCSource | None) -> dict:
    """Valida tutto prima di sostituire i prodotti; conserva le geometrie invariate."""
    bulletin, today, tomorrow = (source or DPCSource()).latest_bulletin()
    alerts = reduce_alerts(today, tomorrow, bulletin)
    fingerprint = geometry_fingerprint(today)
    regenerate = (
        not previous or previous.get("impronta_zone") != fingerprint
        or not (destination / "zone-allerta.json").is_file()
    )
    zones_payload = json_bytes(simplify_zones(today)) if regenerate else None
    alerts_payload = json_bytes(alerts)
    # Gli errori di fonte/riduzione non possono sovrascrivere l'ultima coppia buona.
    if zones_payload is not None:
        (destination / "zone-allerta.json.tmp").write_bytes(zones_payload)
    (destination / "allerte.json.tmp").write_bytes(alerts_payload)
    if zones_payload is not None:
        (destination / "zone-allerta.json.tmp").replace(destination / "zone-allerta.json")
    (destination / "allerte.json.tmp").replace(destination / "allerte.json")
    return {
        "url": "allerte.json", "zone_url": "zone-allerta.json",
        # Chiavi lette dall'app (meteo-core.js: meta.file, meta.zone).
        "file": "allerte.json", "zone": "zone-allerta.json",
        "bollettino": bulletin, "emesso": alerts["emesso"],
        "impronta_zone": fingerprint, "stato": "aggiornato",
    }


def update_vigilanza(destination: Path, source: VigilanzaSource | None) -> dict:
    stamp, index, topologies, _style = (source or VigilanzaSource()).latest_bulletin()
    bulletin = reduce_vigilanza(stamp, index, topologies)
    payload = json_bytes(bulletin)
    temporary = destination / "vigilanza.json.tmp"
    temporary.write_bytes(payload)
    temporary.replace(destination / "vigilanza.json")
    return {"url": "vigilanza.json", "file": "vigilanza.json", "data": bulletin["data"], "stato": "aggiornato"}


def build(
    model: str,
    run: str,
    steps_spec: str,
    layers_spec: str,
    output: str | Path,
    *,
    source: ECMWFSource | None = None,
    dpc_source: DPCSource | None = None,
    vigilanza_source: VigilanzaSource | None = None,
    dwd_source: DWDSource | None = None,
    previous_output: str | Path | None = None,
    temporali_eu: bool = False,
) -> dict:
    model = "aifs-single" if model == "aifs" else model
    steps = parse_steps(steps_spec, "ifs" if model == "ifs" else "aifs-single")
    layers = parse_layers(layers_spec)
    if "swh" in layers and model != "ifs":
        raise ValueError("swh richiede il modello ifs e lo stream wave")
    source = source or ECMWFSource()
    destination = Path(output).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    assets = Path(previous_output).resolve() if previous_output is not None else destination
    previous = {}
    manifest_path = destination / "manifest.json"
    previous_manifest = assets / "manifest.json"
    if previous_manifest.is_file():
        try:
            data = json.loads(previous_manifest.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                previous = data
        except (ValueError, OSError):
            pass  # Un manifest illeggibile non e' un'ultima voce buona.
    previous_alerts = previous.get("allerte")
    previous_vigilanza = previous.get("vigilanza")
    manifest = select_ecmwf(previous, model, run, steps, layers, assets, destination, source)
    warnings = manifest["warnings"]
    if dwd_source is not None:
        try:
            manifest["precip_eu"] = build_precip_eu(destination, dwd_source)
        except Exception as exc:
            # Niente PNG orfani pubblicati senza la chiave che li descrive.
            shutil.rmtree(destination / "precip_eu", ignore_errors=True)
            message = f"ICON-EU non pubblicato: {type(exc).__name__}: {exc}"
            print(f"::warning::{message}", file=sys.stderr)
            warnings.append({"source": "ICON-EU", "message": message})
        if temporali_eu:
            update_temporali_eu(manifest, previous.get("temporali_eu"), dwd_source, assets, destination)
    try:
        manifest["vigilanza"] = update_vigilanza(destination, vigilanza_source)
    except Exception as exc:
        message = f"DPC vigilanza non aggiornato: {type(exc).__name__}: {exc}"
        print(f"::warning::{message}", file=sys.stderr)
        warnings.append({"source": "DPC vigilanza", "message": message})
        if previous_vigilanza:
            manifest["vigilanza"] = {**previous_vigilanza, "stato": "non aggiornato"}
    try:
        manifest["allerte"] = update_alerts(destination, previous_alerts, dpc_source)
    except Exception as exc:
        message = f"DPC non aggiornato: {type(exc).__name__}: {exc}"
        print(f"::warning::{message}", file=sys.stderr)
        warnings.append({"source": "DPC", "message": message})
        if previous_alerts:
            manifest["allerte"] = {**previous_alerts, "stato": "non aggiornato"}
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def copy_product(relative: str, assets: Path, destination: Path) -> None:
    if assets != destination:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(assets / relative, target)


def update_temporali_eu(manifest, previous, source, assets, destination):
    if isinstance(previous, dict):
        stamp = previous.get("run", "")
        relative = previous.get("url")
        has_previous = (isinstance(stamp, str) and len(stamp) == 10 and stamp.isascii()
                        and stamp.isdecimal() and relative == f"temporali_eu/{stamp}.png"
                        and (assets / relative).is_file())
    else:
        has_previous = False
    try:
        run = source.latest_weather_run()
        candidate = run_id(run)
        if has_previous and stamp == candidate:
            copy_product(relative, assets, destination)
            manifest["temporali_eu"] = previous
            print(f"ICON-EU temporali_eu corsa={stamp} gia' pubblicata", file=sys.stderr)
        elif has_previous and stamp > candidate:
            raise ValueError(f"trovata corsa {candidate}, precedente a quella pubblicata")
        else:
            manifest["temporali_eu"] = build_temporali_eu(destination, source, run=run)
    except Exception as exc:
        message = f"ICON-EU temporali_eu non aggiornato: {type(exc).__name__}: {exc}"
        if has_previous:
            copy_product(relative, assets, destination)
            manifest["temporali_eu"] = previous
            message += f". Conservata la corsa {stamp}"
        print(f"::warning::{message}", file=sys.stderr)
        manifest["warnings"].append({"source": "ICON-EU temporali_eu", "message": message})
    if entry := manifest.get("temporali_eu"):
        age = datetime.now(timezone.utc) - normalize_run(entry["run"])
        stale = age >= timedelta(hours=STALE_AFTER_HOURS)
        manifest["temporali_eu"] = {**entry, "stale": stale}
        if stale:
            hours = age.total_seconds() / 3600
            message = f"ICON-EU temporali_eu corsa={entry['run']} dato vecchio: {hours:.1f} h (soglia {STALE_AFTER_HOURS} h)"
            print(f"::warning::{message}", file=sys.stderr)
            manifest["warnings"].append({"source": "ICON-EU temporali_eu", "message": message})


def select_ecmwf(previous, model, run, steps, layers, assets, destination, source):
    stamp = previous.get("run")
    published = None
    if isinstance(stamp, str) and len(stamp) == 10 and stamp.isascii() and stamp.isdecimal():
        try:
            published = normalize_run(stamp)
        except ValueError:
            pass  # Una data non valida non impone un limite alla ricerca.
    try:
        if run == "latest":
            floor = {"not_before": published} if published is not None else {}
            candidate = source.latest_run(model, step=steps[0], **floor)
        else:
            candidate = normalize_run(run)
        if published is not None and candidate < published:
            raise RunNotAvailable(f"trovata corsa {run_id(candidate)}, precedente a quella pubblicata")
    except (RunNotAvailable, IndeterminateRun) as exc:
        if published is None:
            raise
        retained = reuse_ecmwf(previous, previous.get("model"), published, None,
                               previous.get("layers", []), assets, destination, allow_partial=True)
        if retained is None:
            raise
        message = f"ECMWF: {exc}. Conservata la corsa {stamp}"
        print(f"::warning::{message}", file=sys.stderr)
        retained["warnings"].append({"source": "ECMWF", "message": message})
        return retained
    if run == "latest":
        reused = reuse_ecmwf(previous, model, candidate, steps, layers, assets, destination)
        if reused is not None:
            return reused
    return build_ecmwf(model, candidate, steps, layers, destination, source)


def reuse_ecmwf(previous, model, run_time, steps, layers, assets, destination, *, allow_partial=False):
    stamp = run_id(run_time)
    if (previous.get("model") != model or previous.get("run") != stamp
            or previous.get("layers") != layers or not previous.get("grid")):
        return None
    try:
        requested = set(layers)
        published_steps = [entry["step"] for entry in previous["steps"]]
        if not requested.issubset(LAYER_PARAMETERS) or (steps is not None and published_steps != steps):
            return None
        files = []
        for index, entry in enumerate(previous["steps"]):
            present = set(entry["layers"])
            expected = requested - {"precip_rate"} if index == 0 else requested
            if not present.issubset(requested) or (not allow_partial and not expected.issubset(present)):
                return None
            for layer, metadata in entry["layers"].items():
                relative = f"{layer}/{entry['step']:03d}.png"
                if metadata["url"] != relative or not (assets / relative).is_file():
                    return None
                files.append(relative)
        reused = {key: previous[key] for key in ("model", "run", "grid", "layers", "steps", "license", "attribution")}
        reused["warnings"] = [item for item in previous["warnings"] if "step" in item]
    except (KeyError, TypeError, AttributeError):
        return None
    if not files:
        return None
    for relative in files:
        copy_product(relative, assets, destination)
    print(f"ECMWF corsa={stamp} gia' pubblicata, nessuna novita' ECMWF", file=sys.stderr)
    return reused


def build_ecmwf(model, run_time, steps, layers, destination, source):
    parameters = list(
        dict.fromkeys(parameter for layer in layers for parameter in LAYER_PARAMETERS[layer])
    )
    optional = OPTIONAL_PARAMETERS.intersection(parameters)
    oper_parameters = [name for name in parameters if name != "swh"]
    manifest_steps = []
    grid_meta = None
    warnings = []
    previous_tp = None
    previous_tp_step = None
    for step in steps:
        step_started = monotonic()
        payloads, absent = source.download_parameters(
            model, run_time, step, oper_parameters, optional=optional
        ) if oper_parameters else ({}, [])
        if "swh" in parameters:
            wave_payloads, wave_absent = source.download_parameters(
                model, run_time, step, ["swh"], stream="wave"
            )
            payloads.update(wave_payloads)
            absent.extend(wave_absent)
        fields = {name: _field(payload) for name, payload in payloads.items()}
        step_grid = next((field.grid for field in fields.values()), None)
        for name, field in fields.items():
            if field.parameter != name and not (name == "10fg" and field.parameter == "gust"):
                raise ValueError(f"byte-range {name} contiene il parametro {field.parameter}")
            if field.grid != step_grid:
                raise ValueError(f"griglia {name} diversa dagli altri parametri")
        if absent:
            warnings.append({"step": step, "missing_parameters": absent})

        if "precip_rate" in layers and step != 0 and previous_tp is None:
            warnings.append({
                "step": step, "missing_layers": ["precip_rate"],
                "message": "precip_rate assente: tp del passo precedente non disponibile",
            })

        layer_entries = {}
        for layer in layers:
            needed = LAYER_PARAMETERS[layer]
            if any(name not in fields for name in needed):
                continue
            relative = Path(layer) / f"{step:03d}.png"
            path = destination / relative
            if layer == "precip_rate":
                if step == 0 or previous_tp is None:
                    continue
                if fields["tp"].grid != previous_tp.grid:
                    raise ValueError("griglia tp diversa dal passo precedente")
                interval_hours = step - previous_tp_step
                rate = np.maximum(
                    0.0,
                    np.asarray(fields["tp"].values, dtype=np.float64)
                    - np.asarray(previous_tp.values, dtype=np.float64),
                ) * 1000.0 / interval_hours
                metadata = encode_precip_rate(rate, path)
                metadata["interval_hours"] = interval_hours
            elif layer == "wind10m":
                metadata = encode_wind(fields["10u"].values, fields["10v"].values, path)
            elif layer == "swh":
                metadata = encode_masked_scalar(fields["swh"].values, path)
            elif layer == "rh2m":
                metadata = encode_scalar(relative_humidity(fields["2t"].values, fields["2d"].values), path)
            elif layer == "tcc":
                metadata = encode_scalar(cloud_fraction(fields["tcc"]), path)
            else:
                metadata = encode_scalar(fields[needed[0]].values, path)
            if layer == "gust" and fields["10fg"].period_hours is not None:
                metadata["period_hours"] = fields["10fg"].period_hours
            layer_entries[layer] = {"url": relative.as_posix(), **metadata, "units": LAYER_UNITS[layer]}

        if "precip_rate" in layers:
            previous_tp = fields.get("tp")
            previous_tp_step = step if previous_tp is not None else None

        if grid_meta is None and fields:
            g = next(iter(fields.values())).grid
            grid_meta = {
                "width": g.ni, "height": g.nj,
                "lat0": g.lat1, "lon0": ((g.lon1 + 180.0) % 360.0) - 180.0,
                "dlat": g.dj, "dlon": g.di,
                "scanning_mode": g.scanning_mode,
            }

        valid_time = run_time + timedelta(hours=step)
        manifest_steps.append(
            {
                "step": step,
                "valid_time": valid_time.isoformat(timespec="seconds").replace("+00:00", "Z"),
                "layers": layer_entries,
            }
        )
        print(
            f"passo {step} completato in {monotonic() - step_started:.2f} s",
            file=sys.stderr,
        )

    return {
        "model": model,
        "run": run_id(run_time),
        "grid": grid_meta,
        "layers": layers,
        "steps": manifest_steps,
        "license": "CC-BY-4.0",
        "attribution": (
            "ECMWF Open Data © European Centre for Medium-Range Weather Forecasts "
            "(ECMWF), licensed under CC BY 4.0"
        ),
        "warnings": warnings,
    }
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=("ifs", "aifs", "aifs-single"), required=True)
    parser.add_argument("--run", required=True, help="latest oppure YYYYMMDDHH")
    parser.add_argument("--steps", required=True, help="es. 0-144 oppure 0,3,6")
    parser.add_argument("--layers", required=True, help=",".join(LAYER_PARAMETERS))
    parser.add_argument("--out", required=True)
    parser.add_argument("--previous-output", type=Path, help="cartella dei file gia' pubblicati")
    parser.add_argument("--temporali-eu", action="store_true", help="includi il campo ww ICON-EU")
    args = parser.parse_args(argv)
    options = {"dwd_source": DWDSource()}
    if args.previous_output is not None:
        options["previous_output"] = args.previous_output
    if args.temporali_eu:
        options["temporali_eu"] = True
    build(args.model, args.run, args.steps, args.layers, args.out, **options)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
