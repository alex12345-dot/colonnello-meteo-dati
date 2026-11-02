"""Pioggia oraria ICON-EU DWD: HTTP limitato, bz2 e GRIB2 senza ecCodes."""

from __future__ import annotations

import bz2
from datetime import datetime, timedelta, timezone
from http.client import HTTPException
from pathlib import Path
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import numpy as np

try:
    from pipeline.encode import encode_precip_rate
    from pipeline.grib2 import decode_message, iter_messages
    from pipeline.source_ecmwf import normalize_run, run_id, _retry_after_seconds
except ModuleNotFoundError:
    from encode import encode_precip_rate
    from grib2 import decode_message, iter_messages
    from source_ecmwf import normalize_run, run_id, _retry_after_seconds


BASE_URL = "https://opendata.dwd.de/weather/nwp/icon-eu/grib"
TRANSIENT_STATUS = frozenset({408, 429, 500, 502, 503, 504})


class DWDError(RuntimeError):
    pass


class RunNotAvailable(DWDError):
    pass


def forecast_url(run: str | datetime, step: int) -> str:
    stamp = normalize_run(run)
    if stamp.hour not in (0, 6, 12, 18) or stamp.minute or stamp.second:
        raise ValueError("corsa ICON-EU non valida")
    if not 0 <= step <= 72:
        raise ValueError("passo ICON-EU fuori 0..72")
    return (
        f"{BASE_URL}/{stamp:%H}/tot_prec/"
        f"icon-eu_europe_regular-lat-lon_single-level_{run_id(stamp)}_{step:03d}_TOT_PREC.grib2.bz2"
    )


class DWDSource:
    def __init__(self, *, attempts=4, backoff=0.5, timeout=30.0,
                 max_elapsed=240.0, total_budget=900.0, opener=urlopen,
                 sleeper=time.sleep, clock=time.monotonic):
        self.attempts = attempts
        self.backoff = backoff
        self.timeout = timeout
        self.max_elapsed = max_elapsed
        self.total_budget = total_budget
        self.opener = opener
        self.sleeper = sleeper
        self.clock = clock
        self.deadline = None

    def check_budget(self):
        if self.deadline is None:
            self.deadline = self.clock() + self.total_budget
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            raise DWDError("budget totale ICON-EU esaurito")
        return remaining

    def _request(self, url, *, method="GET", probe=False):
        self.check_budget()
        request_deadline = min(self.deadline, self.clock() + self.max_elapsed)
        last_error = None
        for attempt in range(self.attempts):
            remaining = min(self.check_budget(), request_deadline - self.clock())
            if remaining <= 0:
                break
            wait = min(self.backoff * 2 ** attempt, 15.0)
            headers = {"Range": "bytes=0-0"} if probe and method == "GET" else {}
            try:
                with self.opener(Request(url, headers=headers, method=method),
                                 timeout=min(self.timeout, remaining)) as response:
                    if method == "HEAD":
                        payload = b""
                    elif probe:
                        payload = response.read(1)
                        if not payload:
                            raise DWDError("sonda ICON-EU vuota")
                    else:
                        # Controlla il budget anche durante un corpo ricevuto lentamente.
                        chunks = []
                        while True:
                            self.check_budget()
                            if self.clock() >= request_deadline:
                                raise DWDError("budget richiesta ICON-EU esaurito")
                            chunk = response.read(64 * 1024)
                            if not chunk:
                                break
                            chunks.append(chunk)
                        payload = b"".join(chunks)
                self.check_budget()
                return payload
            except HTTPError as exc:
                last_error = exc
                if exc.code == 404:
                    raise RunNotAvailable(url) from exc
                if method == "HEAD" and exc.code in (405, 501):
                    return self._request(url, probe=True)
                if exc.code not in TRANSIENT_STATUS:
                    raise DWDError(f"HTTP {exc.code} non ritentabile: {url}") from exc
                retry_after = _retry_after_seconds(exc)
                if retry_after is not None:
                    wait = max(wait, retry_after)
            except (URLError, TimeoutError, OSError, HTTPException) as exc:
                last_error = exc
            remaining = min(self.check_budget(), request_deadline - self.clock())
            wait = min(wait, max(0, remaining)) if attempt + 1 < self.attempts else 0
            print(f"ICON-EU {url} tentativo={attempt + 1} errore={last_error} attesa={wait:g}s",
                  file=sys.stderr)
            if wait > 0:
                self.sleeper(wait)
        raise DWDError(f"richiesta ICON-EU fallita: {url}") from last_error

    def latest_run(self, *, now: datetime | None = None) -> datetime:
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        current = current.replace(hour=current.hour // 6 * 6, minute=0, second=0, microsecond=0)
        # Corsa nominale corrente e fino a quattro corse precedenti.
        for index in range(5):
            run = current - timedelta(hours=6 * index)
            try:
                self._request(forecast_url(run, 72), method="HEAD", probe=True)
                return run
            except RunNotAvailable:
                continue
        raise RunNotAvailable("nessuna corsa ICON-EU completa entro quattro corse precedenti")

    def download_total(self, run, step):
        payload = bz2.decompress(self._request(forecast_url(run, step)))
        messages = list(iter_messages(payload))
        if len(messages) != 1:
            raise DWDError(f"atteso un GRIB ICON-EU, trovati {len(messages)}")
        field = decode_message(messages[0])
        # TOT_PREC in kg/m2 = mm, senza conversione x1000. Il file vero del DWD usa 0.1.52
        # (tasso con elaborazione statistica "accumulo", valori cumulati in kg/m2; verificato
        # sulla corsa 2026110200 passo 006: max 54 mm in 6 h); 0.1.8 e' la forma WMO classica.
        if (field.discipline, field.category, field.number) not in ((0, 1, 8), (0, 1, 52)):
            raise DWDError(f"parametro ICON-EU diverso da TOT_PREC: "
                           f"{field.discipline}.{field.category}.{field.number}")
        self.check_budget()
        return field


def north_south(field):
    grid = field.grid
    if grid.scanning_mode != 64:
        raise DWDError(f"scanning ICON-EU inatteso: {grid.scanning_mode}")
    values = np.asarray(field.values, dtype=np.float64)[::-1, :]
    if values.shape != (grid.nj, grid.ni) or not np.isfinite(values).all():
        raise DWDError("dominio ICON-EU incompleto")
    return values, {
        "width": grid.ni, "height": grid.nj, "lat0": grid.lat2,
        "lon0": ((grid.lon1 + 180.0) % 360.0) - 180.0,
        "dlat": grid.dj, "dlon": grid.di, "scanning_mode": 0,
    }


def build_precip_eu(destination: Path, source: DWDSource) -> dict:
    """Il workflow ricrea out: rigenera tutti i PNG prima di esporre il blocco."""
    run = normalize_run(source.latest_run())
    previous = None  # TOT_PREC[0] = 0 per definizione, nessun PNG al passo 0.
    grid_meta = None
    entries = []
    for step in range(1, 73):
        field = source.download_total(run, step)
        values, grid = north_south(field)
        if grid_meta is not None and grid != grid_meta:
            raise DWDError(f"griglia ICON-EU cambiata al passo {step}")
        grid_meta = grid
        rate = np.maximum(0.0, values - (previous if previous is not None else 0.0))
        relative = f"precip_eu/{step:03d}.png"
        metadata = encode_precip_rate(rate, destination / relative)
        entries.append({
            "valid_time": (run + timedelta(hours=step)).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "url": relative,
            **{key: metadata[key] for key in ("min", "max", "quantization_step")},
        })
        previous = values
        source.check_budget()
    return {
        "model": "icon-eu", "run": run_id(run),
        "attribution": "Deutscher Wetterdienst (DWD), ICON-EU", "license": "CC-BY-4.0",
        "grid": grid_meta, "encoding": "grayscale8", "transform": "sqrt",
        "units": "mm/h", "interval_hours": 1, "steps": entries,
    }
