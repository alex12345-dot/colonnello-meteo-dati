"""CLI per produrre livelli ECMWF, allerte, vigilanza DPC e manifest Colonnello Meteo."""

from __future__ import annotations

import argparse
from datetime import timedelta
import json
from pathlib import Path
import sys
from time import monotonic

import numpy as np

try:
    from pipeline.allerte import geometry_fingerprint, json_bytes, reduce_alerts, simplify_zones
    from pipeline.encode import encode_masked_scalar, encode_scalar, encode_wind
    from pipeline.grib2 import decode_message, iter_messages
    from pipeline.source_ecmwf import ECMWFSource, normalize_run, run_id
    from pipeline.source_dpc import DPCSource, VigilanzaSource
    from pipeline.vigilanza import reduce_vigilanza
except ModuleNotFoundError:  # esecuzione diretta dalla cartella pipeline
    from allerte import geometry_fingerprint, json_bytes, reduce_alerts, simplify_zones
    from encode import encode_masked_scalar, encode_scalar, encode_wind
    from grib2 import decode_message, iter_messages
    from source_ecmwf import ECMWFSource, normalize_run, run_id
    from source_dpc import DPCSource, VigilanzaSource
    from vigilanza import reduce_vigilanza


LAYER_PARAMETERS = {
    "wind10m": ("10u", "10v"),
    "t2m": ("2t",),
    "precip": ("tp",),
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
    "snow": "m", "swh": "m",
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
) -> dict:
    model = "aifs-single" if model == "aifs" else model
    steps = parse_steps(steps_spec, "ifs" if model == "ifs" else "aifs-single")
    layers = parse_layers(layers_spec)
    if "swh" in layers and model != "ifs":
        raise ValueError("swh richiede il modello ifs e lo stream wave")
    source = source or ECMWFSource()
    run_time = source.latest_run(model, step=steps[0]) if run == "latest" else normalize_run(run)
    destination = Path(output)
    destination.mkdir(parents=True, exist_ok=True)
    previous_alerts = None
    previous_vigilanza = None
    manifest_path = destination / "manifest.json"
    if manifest_path.is_file():
        try:
            previous = json.loads(manifest_path.read_text(encoding="utf-8"))
            previous_alerts = previous.get("allerte")
            previous_vigilanza = previous.get("vigilanza")
        except (ValueError, OSError):
            pass  # Un manifest illeggibile non e' un'ultima voce buona.

    parameters = list(
        dict.fromkeys(parameter for layer in layers for parameter in LAYER_PARAMETERS[layer])
    )
    optional = OPTIONAL_PARAMETERS.intersection(parameters)
    oper_parameters = [name for name in parameters if name != "swh"]
    manifest_steps = []
    grid_meta = None
    warnings = []
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

        layer_entries = {}
        for layer in layers:
            needed = LAYER_PARAMETERS[layer]
            if any(name not in fields for name in needed):
                continue
            relative = Path(layer) / f"{step:03d}.png"
            path = destination / relative
            if layer == "wind10m":
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

    manifest = {
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
        # DPC e' indipendente: anche errori di schema/geometria devono lasciare
        # pubblicabili i livelli ECMWF, dichiarando l'osservazione nel log e nel manifest.
        message = f"DPC non aggiornato: {type(exc).__name__}: {exc}"
        print(f"::warning::{message}", file=sys.stderr)
        warnings.append({"source": "DPC", "message": message})
        if previous_alerts:
            manifest["allerte"] = {**previous_alerts, "stato": "non aggiornato"}
    (destination / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=("ifs", "aifs", "aifs-single"), required=True)
    parser.add_argument("--run", required=True, help="latest oppure YYYYMMDDHH")
    parser.add_argument("--steps", required=True, help="es. 0-144 oppure 0,3,6")
    parser.add_argument("--layers", required=True, help=",".join(LAYER_PARAMETERS))
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    build(args.model, args.run, args.steps, args.layers, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
