"""Accesso HTTP anonimo agli open data ECMWF con richieste byte-range."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from http.client import HTTPException
import json
import math
import sys
import time
from typing import Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


BASE_URL = "https://ecmwf-forecasts.s3.eu-central-1.amazonaws.com"
MIRROR_URLS = (
    BASE_URL,
    "https://data.ecmwf.int/forecasts",
)
# Stati HTTP che vale la pena ritentare: throttling e guasti momentanei. Un 403 o un 400
# non cambia riprovando, e sei attese sarebbero solo tempo perso.
TRANSIENT_STATUS = frozenset({408, 429, 500, 502, 503, 504})
# Tetto a ogni singola attesa: ne' un Retry-After ostile ne' il backoff possono bloccare
# il job per l'intero timeout del workflow (rilievo Codex 15/09/2026).
MAX_WAIT_SECONDS = 60.0
# Il "503 Slow Down" del bucket ECMWF NON e' una punizione al nostro ritmo: il 17/09/2026,
# da una linea domestica e con una richiesta al secondo, 11 sonde su 20 sono tornate 503 e
# quella subito successiva 206. E' throttling globale sull'oggetto appena pubblicato, e si
# comporta come una moneta: cio' che conta e' il NUMERO di tentativi, non la loro distanza.
# Con sei tentativi la probabilita' di perdere una singola richiesta era 0,55^6 = 2,8 %, che
# su ~110 richieste per giro fa fallire il giro nel 96 % dei casi (tre run perse il 16-17/09).
# Quindi: molti tentativi, attese brevi all'inizio e mai oltre MAX_BACKOFF_SECONDS. Le attese
# lunghe restano solo per un Retry-After esplicito, che il server manda quando e' davvero giu'.
MAX_BACKOFF_SECONDS = 15.0
# Budget complessivo di una singola richiesta, inclusi timeout e attese. Deve superare i
# 165,5 s di backoff dei 16 tentativi rapidi su 503, ma impedire che 16 timeout da 30 s
# monopolizzino il workflow per oltre dieci minuti.
MAX_REQUEST_SECONDS = 240.0
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


def forecast_path(model: str, run: str | datetime, step: int, extension: str) -> str:
    try:
        model_path = MODEL_PATHS[model]
    except KeyError as exc:
        raise ValueError(f"modello non supportato: {model}") from exc
    stamp = normalize_run(run)
    date = stamp.strftime("%Y%m%d")
    cycle = stamp.strftime("%H")
    filename = f"{date}{cycle}0000-{step}h-oper-fc.{extension}"
    return f"/{date}/{cycle}z/{model_path}/0p25/oper/{filename}"


def forecast_url(model: str, run: str | datetime, step: int, extension: str) -> str:
    return f"{BASE_URL}{forecast_path(model, run, step, extension)}"


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
        attempts: int = 16,
        backoff: float = 0.5,
        timeout: float = 30.0,
        max_elapsed: float = MAX_REQUEST_SECONDS,
        opener: Callable = urlopen,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.attempts = attempts
        self.backoff = backoff
        self.timeout = timeout
        self.max_elapsed = max_elapsed
        self.opener = opener
        self.sleeper = sleeper
        self.clock = clock
        self._index_cache: dict[str, list[dict]] = {}
        self._connection_failures: dict[str, int] = {}
        self._excluded_hosts: set[str] = set()

    def _record_connection_failure(self, host: str) -> None:
        count = self._connection_failures.get(host, 0) + 1
        self._connection_failures[host] = count
        if count < 2 or host in self._excluded_hosts:
            return
        self._excluded_hosts.add(host)
        print(
            f"ECMWF host={host} escluso errori_connessione={count}",
            file=sys.stderr,
        )
        if len(self._excluded_hosts) == len(MIRROR_URLS):
            self._connection_failures.clear()
            self._excluded_hosts.clear()

    def _request(self, path: str, headers: dict[str, str] | None = None) -> bytes:
        last_error: BaseException | None = None
        started = self.clock()
        attempts_made = 0
        mirror_index = 0
        missing_hosts: set[str] = set()
        failed_here: set[str] = set()
        retried_excluded: set[str] = set()
        retry_attempts = 0
        while retry_attempts < self.attempts:
            remaining = self.max_elapsed - (self.clock() - started)
            if remaining <= 0:
                break
            # Un host escluso e' solo una scorciatoia per non ripagare i suoi timeout alla
            # PRIMA scelta: non e' una risposta. Appena tutti gli host attivi hanno fallito
            # qui — 404, strozzatura o guasto — gli esclusi rientrano fra i candidati: uno
            # puo' essere guarito, e senza di loro "non lo so" diventerebbe "la corsa non
            # esiste" e latest_run scivolerebbe in silenzio su una corsa vecchia
            # (rilievi [P1] e [P2] del 18/09/2026).
            attivi = [
                host
                for host in MIRROR_URLS
                if host not in self._excluded_hosts and host not in missing_hosts
            ]
            mirrors = [host for host in attivi if host not in failed_here]
            if not mirrors:
                # Gli attivi hanno gia' fallito su questo oggetto: si concede UNA prova per
                # richiesta a un host escluso, che nel frattempo puo' essere guarito. Una
                # sola, altrimenti si ripaga il suo timeout a ogni giro (rilievo [P2] della
                # seconda review, contro quello della quarta: questo e' il compromesso).
                mirrors = [
                    host
                    for host in MIRROR_URLS
                    if host not in missing_hosts
                    and host in self._excluded_hosts
                    and host not in retried_excluded
                    and host not in failed_here
                ] or attivi
            if not mirrors:
                # Nessun host ancora utile: gli attivi hanno gia' fallito e l'escluso ha
                # avuto la sua prova. Insistere sarebbe grattare lo stesso muro per 240 s.
                if all(candidate in missing_hosts for candidate in MIRROR_URLS):
                    raise RunNotAvailable(path) from last_error
                break
            host = mirrors[mirror_index % len(mirrors)]
            if host in self._excluded_hosts:
                retried_excluded.add(host)
            url = f"{host}{path}"
            request = Request(url, headers=headers or {})
            wait = min(self.backoff * (2**retry_attempts), MAX_BACKOFF_SECONDS)
            attempts_made += 1
            try:
                with self.opener(request, timeout=min(self.timeout, remaining)) as response:
                    status = getattr(response, "status", None)
                    if headers and "Range" in headers and status == 200:
                        raise RangeNotHonored(f"server senza byte-range: {url}")
                    payload = response.read()
                    # I fallimenti che escludono un host vanno contati CONSECUTIVI: un giro
                    # scarica oltre cento oggetti e due intoppi isolati a distanza di minuti
                    # non sono un mirror guasto (rilievo [P2] del 18/09/2026).
                    # L'esclusione e' un sospetto, non una condanna: un host che risponde ha
                    # dimostrato di essere tornato e rientra in rotazione.
                    self._connection_failures.pop(host, None)
                    self._excluded_hosts.discard(host)
                    return payload
            except HTTPError as exc:
                # Una risposta HTTP, anche 404 o 503, prova che l'host e' raggiungibile:
                # interrompe la sequenza di fallimenti di connessione (rilievo [P2] del
                # 18/09/2026), che contano solo consecutivi, e lo rimette in rotazione —
                # era escluso perche' irraggiungibile, e non lo e' piu'.
                self._connection_failures.pop(host, None)
                self._excluded_hosts.discard(host)
                failed_here.add(host)
                if exc.code == 404:
                    last_error = exc
                    missing_hosts.add(host)
                    print(
                        f"ECMWF host={host} stato={exc.code} {exc.reason} "
                        f"tentativo={attempts_made} attesa=0s",
                        file=sys.stderr,
                    )
                    # La corsa e' assente solo se lo dicono TUTTI i mirror: un host escluso
                    # non ha detto niente.
                    if all(candidate in missing_hosts for candidate in MIRROR_URLS):
                        raise RunNotAvailable(path) from exc
                    mirror_index += 1
                    continue
                if exc.code not in TRANSIENT_STATUS:
                    print(
                        f"ECMWF host={host} stato={exc.code} {exc.reason} "
                        f"tentativo={attempts_made} attesa=0s",
                        file=sys.stderr,
                    )
                    raise NetworkError(f"HTTP {exc.code} non ritentabile: {url}") from exc
                last_error = exc
                # Il 503 arriva anche alla prima richiesta di una sessione (misurato: 55 % di
                # 503 a una richiesta al secondo), quindi si insiste subito; se il server
                # manda un Retry-After e' lui a dire quanto aspettare e quello vince.
                retry_after = _retry_after_seconds(exc)
                if retry_after is not None:
                    wait = max(retry_after, wait)
                observed = f"stato={exc.code} {exc.reason}"
            except RangeNotHonored as exc:
                print(
                    f"ECMWF host={host} eccezione={type(exc).__name__}: {exc} "
                    f"tentativo={attempts_made} attesa=0s",
                    file=sys.stderr,
                )
                raise
            except (URLError, TimeoutError, OSError, HTTPException) as exc:
                # HTTPException copre IncompleteRead: connessione caduta a meta' payload.
                last_error = exc
                observed = f"eccezione={type(exc).__name__}: {exc}"
                failed_here.add(host)
                self._record_connection_failure(host)
            retry_attempts += 1
            remaining = self.max_elapsed - (self.clock() - started)
            chosen_wait = (
                min(wait, remaining)
                if retry_attempts < self.attempts and remaining > 0
                else 0.0
            )
            print(
                f"ECMWF host={host} {observed} tentativo={attempts_made} "
                f"attesa={chosen_wait:g}s",
                file=sys.stderr,
            )
            mirror_index += 1
            if chosen_wait > 0:
                self.sleeper(chosen_wait)
        raise NetworkError(
            f"richiesta fallita dopo {attempts_made} tentativi: {path}"
        ) from last_error

    def get_index(self, model: str, run: str | datetime, step: int) -> list[dict]:
        path = forecast_path(model, run, step, "index")
        if path not in self._index_cache:
            self._index_cache[path] = parse_index(self._request(path))
        return self._index_cache[path]

    def download_entry(self, model: str, run: str | datetime, step: int, entry: dict) -> bytes:
        path = forecast_path(model, run, step, "grib2")
        payload = self._request(path, range_header(entry))
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
