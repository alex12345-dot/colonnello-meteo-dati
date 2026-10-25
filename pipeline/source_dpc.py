"""Scaricamento dei bollettini DPC: scoperta delle coppie e HTTP con retry limitati."""

from __future__ import annotations

from http.client import HTTPException
import json
import os
import re
import sys
import time
from typing import Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen


REPOSITORY = "pcm-dpc/DPC-Bollettini-Criticita-Idrogeologica-Idraulica"
REPOSITORY_URL = f"https://github.com/{REPOSITORY}"
API_URL = f"https://api.github.com/repos/{REPOSITORY}"
RAW_URL = f"https://raw.githubusercontent.com/{REPOSITORY}"
TRANSIENT_STATUS = {408, 429, 500, 502, 503, 504}
FILENAME = re.compile(r"^(\d{8}_\d{4})_(today|tomorrow)\.json$")


class DPCError(RuntimeError):
    """Fonte non disponibile o risposta non utilizzabile."""


def latest_complete_bulletin(paths: Iterable[str]) -> str:
    """Sceglie soltanto coppie con identico timestamp, indipendentemente dall'ordine."""
    pairs: dict[str, set[str]] = {}
    for path in paths:
        match = FILENAME.fullmatch(path.rsplit("/", 1)[-1])
        if match:
            stamp, day = match.groups()
            pairs.setdefault(stamp, set()).add(day)
    complete = [stamp for stamp, days in pairs.items() if days == {"today", "tomorrow"}]
    if not complete:
        raise DPCError("nessuna coppia DPC today/tomorrow completa osservata")
    return max(complete)


class DPCSource:
    def __init__(
        self,
        *,
        token: str | None = None,
        attempts: int = 3,
        timeout: float = 30.0,
        backoff: float = 1.0,
        opener: Callable = urlopen,
        sleeper: Callable = time.sleep,
    ) -> None:
        if attempts < 1 or timeout <= 0 or backoff < 0:
            raise ValueError("parametri HTTP DPC non validi")
        self.token = token if token is not None else os.environ.get("GITHUB_TOKEN", "")
        self.attempts = attempts
        self.timeout = timeout
        self.backoff = backoff
        self.opener = opener
        self.sleeper = sleeper

    def _json(self, url: str) -> dict:
        host = urlsplit(url).hostname
        headers = {"User-Agent": "colonnello-meteo-dati", "Accept": "application/json"}
        # La credenziale serve solo all'API, mai al download raw.
        if host == "api.github.com" and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        for attempt in range(1, self.attempts + 1):
            try:
                with self.opener(Request(url, headers=headers), timeout=self.timeout) as response:
                    status = response.status
                    print(f"DPC host={host} HTTP={status} tentativo={attempt}", file=sys.stderr)
                    if status != 200:
                        raise HTTPError(url, status, "stato inatteso", {}, None)
                    payload = response.read()
            except HTTPError as exc:
                observed = f"host={host} HTTP={exc.code}"
                retry = exc.code in TRANSIENT_STATUS
            except (URLError, OSError, HTTPException) as exc:
                observed = f"host={host} errore={type(exc).__name__}: {exc}"
                retry = True
            else:
                try:
                    value = json.loads(payload)
                except (ValueError, UnicodeError) as exc:
                    raise DPCError(f"host={host} HTTP={status}: JSON non valido") from exc
                if not isinstance(value, dict):
                    raise DPCError(f"host={host} HTTP={status}: atteso oggetto JSON")
                return value
            print(f"DPC {observed} tentativo={attempt}", file=sys.stderr)
            if not retry or attempt == self.attempts:
                raise DPCError(f"DPC {observed}; tentativi={attempt}")
            self.sleeper(min(self.backoff * 2 ** (attempt - 1), 15.0))
        raise AssertionError("tentativi DPC esauriti")

    def _tree(self, reference: str) -> dict:
        tree = self._json(f"{API_URL}/git/trees/{quote(reference, safe='')}")
        if tree.get("truncated") or not isinstance(tree.get("tree"), list):
            raise DPCError("lista git/trees DPC incompleta o non valida")
        return tree

    def latest_bulletin(self) -> tuple[str, dict, dict]:
        # Alberi NON ricorsivi: niente limite Contents di 1000 file, niente albero
        # ricorsivo troncato. I download usano il commit osservato, non un ramo mobile.
        repository = self._json(API_URL)
        branch = repository["default_branch"]
        commit = self._json(f"{API_URL}/commits/{quote(branch, safe='')}")["sha"]
        tree = self._tree(commit)
        for directory in ("files", "geojson"):
            entries = [e for e in tree["tree"] if e.get("path") == directory and e.get("type") == "tree"]
            if len(entries) != 1:
                raise DPCError(f"directory DPC non trovata: {directory}")
            tree = self._tree(entries[0]["sha"])
        stamp = latest_complete_bulletin(e["path"] for e in tree["tree"] if e.get("type") == "blob")
        base = f"{RAW_URL}/{quote(commit, safe='')}/files/geojson/{stamp}"
        return stamp, self._json(f"{base}_today.json"), self._json(f"{base}_tomorrow.json")
