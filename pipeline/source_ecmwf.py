"""Accesso HTTP anonimo agli open data ECMWF con richieste byte-range."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from http.client import HTTPException
import json
import math
import time
from typing import Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


BASE_URL = "https://ecmwf-forecasts.s3.eu-central-1.amazonaws.com"
# Stati HTTP che vale la pena ritentare: throttling e guasti momentanei. Un 403 o un 400
# non cambia riprovando, e sei attese sarebbero solo tempo perso.
TRANSIENT_STATUS = frozenset({408, 429, 500, 502, 503, 504})
# Tetto a ogni singola attesa: ne' un Retry-After ostile ne' il backoff possono bloccare
# il job per l'intero timeout del workflow (rilievo Codex 15/09/2026).
MAX_WAIT_SECONDS = 60.0
MODEL_PATHS = {"ifs": "ifs", "aifs": "aifs-single", "aifs-single": "aifs-single"}


class ECMWFError(RuntimeError):
    pass


class RunNotAvailable(ECMWFError):
    """La run richiesta non è ancora pubblicata (HTTP 404)."""


class NetworkError(ECMWFError):
    """Errore di rete persistente, distinto da una run assente."""


class RangeNotHonored(NetworkError):
    """Il server ha ignorato l'header Range: si evita il download completo."""


class ParameterNotAvailable(ECMWFError):
    pass


def normalize_run(run: str | datetime) -> datetime:
    if isinstance(run, datetime):
        value = run
    else:
        text = run.rstrip("zZ")
        value = datetime.strptime(text, "%Y%m%d%H")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def run_id(run: str | datetime) -> str:
    return normalize_run(run).strftime("%Y%m%d%H")


def candidate_runs(now: datetime | None = None, count: int = 12) -> list[datetime]:
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    current = current.replace(hour=(current.hour // 6) * 6, minute=0, second=0, microsecond=0)
    return [current - timedelta(hours=6 * index) for index in range(count)]


def forecast_url(model: str, run: str | datetime, step: int, extension: str) -> str:
    try:
        model_path = MODEL_PATHS[model]
    except KeyError as exc:
        raise ValueError(f"modello non supportato: {model}") from exc
    stamp = normalize_run(run)
    date = stamp.strftime("%Y%m%d")
    cycle = stamp.strftime("%H")
    filename = f"{date}{cycle}0000-{step}h-oper-fc.{extension}"
    return f"{BASE_URL}/{date}/{cycle}z/{model_path}/0p25/oper/{filename}"


def parse_index(text: str | bytes) -> list[dict]:
    if isinstance(text, bytes):
        text = text.decode("utf-8")
    entries = []
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
            entry["_offset"] = int(entry["_offset"])
            entry["_length"] = int(entry["_length"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"riga index {line_number} non valida") from exc
        entries.append(entry)
    return entries


def range_header(entry: dict) -> dict[str, str]:
    start = int(entry["_offset"])
    length = int(entry["_length"])
    if start < 0 or length <= 0:
        raise ValueError("offset/lunghezza index non validi")
    return {"Range": f"bytes={start}-{start + length - 1}"}


def find_parameters(
    entries: Iterable[dict],
    parameters: Iterable[str],
    *,
    optional: Iterable[str] = (),
) -> tuple[dict[str, dict], list[str]]:
    wanted = list(dict.fromkeys(parameters))
    optional_set = set(optional)
    found: dict[str, dict] = {}
    for entry in entries:
        name = entry.get("param")
        if name in wanted and entry.get("levtype", "sfc") == "sfc" and name not in found:
            found[name] = entry
    absent = [name for name in wanted if name not in found]
    required_absent = [name for name in absent if name not in optional_set]
    if required_absent:
        raise ParameterNotAvailable("parametri assenti dall'index: " + ", ".join(required_absent))
    return found, absent


def _retry_after_seconds(error: HTTPError, now: datetime | None = None) -> float | None:
    """Secondi indicati da Retry-After (numero o data HTTP), limitati a MAX_WAIT_SECONDS."""
    headers = getattr(error, "headers", None)
    value = headers.get("Retry-After") if headers is not None else None
    if value is None:
        return None
    seconds: float | None = None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            when = parsedate_to_datetime(str(value))
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            seconds = (when - (now or datetime.now(timezone.utc))).total_seconds()
        except (TypeError, ValueError, IndexError):
            return None
    if seconds is None or not math.isfinite(seconds):
        return None
    return min(max(0.0, seconds), MAX_WAIT_SECONDS)


class ECMWFSource:
    def __init__(
        self,
        *,
        attempts: int = 6,
        backoff: float = 5.0,
        timeout: float = 30.0,
        opener: Callable = urlopen,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.attempts = attempts
        self.backoff = backoff
        self.timeout = timeout
        self.opener = opener
        self.sleeper = sleeper
        self._index_cache: dict[str, list[dict]] = {}

    def _request(self, url: str, headers: dict[str, str] | None = None) -> bytes:
        request = Request(url, headers=headers or {})
        last_error: BaseException | None = None
        for attempt in range(self.attempts):
            wait = min(self.backoff * (2**attempt), MAX_WAIT_SECONDS)
            try:
                with self.opener(request, timeout=self.timeout) as response:
                    status = getattr(response, "status", None)
                    if headers and "Range" in headers and status == 200:
                        raise RangeNotHonored(f"server senza byte-range: {url}")
                    return response.read()
            except HTTPError as exc:
                if exc.code == 404:
                    raise RunNotAvailable(url) from exc
                if exc.code not in TRANSIENT_STATUS:
                    raise NetworkError(f"HTTP {exc.code} non ritentabile: {url}") from exc
                last_error = exc
                # S3 risponde "503 Slow Down" quando si e' troppo veloci (15/09/2026: tre
                # giri di fila persi con 3 tentativi e attese di 0,5 e 1 s). Il ritmo lo
                # detta il server: si rispetta Retry-After se c'e', altrimenti si rallenta.
                retry_after = _retry_after_seconds(exc)
                if retry_after is not None:
                    wait = max(retry_after, wait)
            except RangeNotHonored:
                raise
            except (URLError, TimeoutError, OSError, HTTPException) as exc:
                # HTTPException copre IncompleteRead: connessione caduta a meta' payload.
                last_error = exc
            if attempt + 1 < self.attempts:
                self.sleeper(wait)
        raise NetworkError(f"richiesta fallita dopo {self.attempts} tentativi: {url}") from last_error

    def get_index(self, model: str, run: str | datetime, step: int) -> list[dict]:
        url = forecast_url(model, run, step, "index")
        if url not in self._index_cache:
            self._index_cache[url] = parse_index(self._request(url))
        return self._index_cache[url]

    def download_entry(self, model: str, run: str | datetime, step: int, entry: dict) -> bytes:
        url = forecast_url(model, run, step, "grib2")
        payload = self._request(url, range_header(entry))
        expected = int(entry["_length"])
        if len(payload) != expected:
            raise NetworkError(f"byte-range incompleto: attesi {expected}, ricevuti {len(payload)}")
        return payload

    def download_parameters(
        self,
        model: str,
        run: str | datetime,
        step: int,
        parameters: Iterable[str],
        *,
        optional: Iterable[str] = (),
    ) -> tuple[dict[str, bytes], list[str]]:
        selected, absent = find_parameters(
            self.get_index(model, run, step), parameters, optional=optional
        )
        return (
            {
                name: self.download_entry(model, run, step, entry)
                for name, entry in selected.items()
            },
            absent,
        )

    def available_runs(
        self,
        model: str,
        *,
        step: int = 0,
        now: datetime | None = None,
        count: int = 12,
        stop_at_first: bool = False,
    ) -> list[datetime]:
        # Le candidate sono in ordine dalla piu' recente. Con `stop_at_first` ci si ferma alla
        # prima pubblicata: chiedere anche le altre undici e' solo traffico verso un bucket che
        # risponde "503 Slow Down", e il 16/09/2026 due giri sono morti su una corsa di due
        # giorni prima quando quella buona era gia' stata trovata.
        available = []
        for run in candidate_runs(now, count):
            try:
                self.get_index(model, run, step)
            except RunNotAvailable:
                continue
            except NetworkError:
                # Una corsa piu' vecchia irraggiungibile non toglie niente a quella gia' trovata.
                if available:
                    break
                raise
            available.append(run)
            if stop_at_first:
                break
        return available

    def latest_run(self, model: str, *, step: int = 0, now: datetime | None = None) -> datetime:
        runs = self.available_runs(model, step=step, now=now, stop_at_first=True)
        if not runs:
            raise RunNotAvailable(f"nessuna run pubblicata per {model}")
        return runs[0]


build_range_header = range_header

