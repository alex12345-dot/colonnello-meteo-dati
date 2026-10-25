"""Riduzione pura del bollettino di vigilanza meteorologica nazionale DPC."""

from __future__ import annotations

from datetime import datetime, timedelta
from html.parser import HTMLParser
import json
import logging
import math
from pathlib import Path
import re

try:
    from pipeline.source_dpc import VIGILANZA_URL, vigilanza_days
except ModuleNotFoundError:
    from source_dpc import VIGILANZA_URL, vigilanza_days


LOGGER = logging.getLogger(__name__)
PHENOMENA = json.loads(
    (Path(__file__).parent / "data" / "fenomeni_dpc.json").read_text(encoding="utf-8")
)["fenomeni"]


def _text(value: str) -> str:
    return " ".join(value.split())


class _Sections(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.sections = []
        self.heading = None
        self.bold_depth = 0
        self.title = []
        self.body = []
        self.hidden = 0

    def _flush(self):
        if self.heading is not None:
            self.sections.append({"titolo": self.heading, "testo": _text("".join(self.body)).lstrip(": ")})
        self.body = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1
        if self.hidden:
            return
        if tag in ("strong", "b"):
            if not self.bold_depth:
                self._flush()
                self.title = []
            self.bold_depth += 1
        elif tag in ("p", "div", "br", "li"):
            self.handle_data(" ")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)
            return
        if self.hidden:
            return
        if tag in ("strong", "b") and self.bold_depth:
            self.bold_depth -= 1
            if not self.bold_depth:
                self.heading = _text("".join(self.title)).rstrip(": ")
        elif tag in ("p", "div", "li"):
            self.handle_data(" ")

    def handle_data(self, data):
        if not self.hidden:
            (self.title if self.bold_depth else self.body).append(data)


def html_sections(html: str) -> list[dict]:
    if not isinstance(html, str) or not html.strip():
        raise ValueError("descrizione HTML vigilanza assente")
    parser = _Sections()
    parser.feed(html)
    parser.close()
    parser._flush()
    if not parser.sections or any(not section["titolo"] or not section["testo"] for section in parser.sections):
        raise ValueError("sezioni vigilanza vuote o senza titoli in grassetto")
    return parser.sections


def reduce_phenomena(topology: dict) -> list[dict]:
    if topology.get("type") != "Topology" or not isinstance(topology.get("objects"), dict) or not topology["objects"]:
        raise ValueError("atteso TopoJSON dei fenomeni vigilanza")
    phenomena = []

    def visit(geometry):
        if geometry.get("type") == "GeometryCollection":
            for child in geometry["geometries"]:
                visit(child)
            return
        if geometry.get("type") != "Point":
            raise ValueError("geometria vigilanza diversa da Point")
        properties = geometry["properties"]
        identifier = properties["id_fenomeno"]
        lat, lon = properties["lat"], properties["lon"]
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in (lat, lon)):
            raise ValueError("coordinate vigilanza non finite o non numeriche")
        if not -90 <= lat <= 90 or not -180 <= lon <= 180:
            raise ValueError("coordinate vigilanza fuori intervallo")
        if isinstance(identifier, bool) or not re.fullmatch(r"\d+", str(identifier)):
            raise ValueError("id_fenomeno vigilanza non valido")
        identifier = int(identifier)
        description = PHENOMENA.get(str(identifier))
        if description is None:
            LOGGER.warning("Fenomeno vigilanza sconosciuto: id=%s", identifier)
            description = {"categoria": "altro", "nome": f"Fenomeno {identifier}"}
        phenomena.append({
            "id": identifier, "categoria": description["categoria"], "nome": description["nome"],
            "lat": round(lat, 4), "lon": round(lon, 4),
        })

    for geometry in topology["objects"].values():
        visit(geometry)
    return phenomena


def reduce_vigilanza(stamp: str, index: dict, topologies: dict, style: dict | None = None) -> dict:
    # style resta accettato per compatibilita' con la tupla restituita dalla fonte.
    issued = datetime.strptime(stamp, "%Y%m%d").date()
    if index.get("date") != stamp or not isinstance(index.get("name"), str) or not index["name"].strip():
        raise ValueError("data o titolo indice vigilanza non validi")
    offsets = {"oggi": 0, "domani": 1, "dopodomani": 2}
    days = [{
        "giorno": day, "data": (issued + timedelta(days=offsets[day])).isoformat(),
        "sezioni": html_sections(entry.get("html_description")),
        "fenomeni": reduce_phenomena(topologies[day]),
    } for day, entry in vigilanza_days(index)]
    return {
        "fonte": "Dipartimento della Protezione Civile", "licenza": "CC BY 4.0",
        "url_fonte": VIGILANZA_URL, "data": issued.isoformat(),
        "titolo": _text(index["name"]), "giorni": days,
    }
