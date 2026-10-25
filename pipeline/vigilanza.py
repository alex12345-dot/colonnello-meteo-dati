"""Riduzione pura del bollettino di vigilanza meteorologica nazionale DPC."""

from __future__ import annotations

from datetime import datetime, timedelta
from html.parser import HTMLParser
import math
import re

try:
    from pipeline.source_dpc import VIGILANZA_URL, vigilanza_days
except ModuleNotFoundError:
    from source_dpc import VIGILANZA_URL, vigilanza_days


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


def phenomenon_legend(style: dict | None) -> dict[str, str]:
    """Lo stile DPC puo' contenere solo icone: queste non sono nomi di fenomeni."""
    result = {}
    if style is None:
        return result
    for group in style.get("style_properties", []):
        if group.get("propertyKey") != "id_fenomeno":
            continue
        for prop in group.get("properties", []):
            if prop.get("property") not in ("label", "name", "title", "description", "nome", "descrizione"):
                continue
            for entry in prop.get("values", []):
                label = entry.get("value")
                if "key" in entry and isinstance(label, str) and _text(label):
                    result[str(entry["key"])] = _text(label)
    return result


def reduce_phenomena(topology: dict, legend: dict[str, str]) -> list[dict]:
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
        key = str(identifier)
        phenomena.append({"tipo": legend.get(key, f"fenomeno {key}"), "lat": round(lat, 4), "lon": round(lon, 4)})

    for geometry in topology["objects"].values():
        visit(geometry)
    return phenomena


def reduce_vigilanza(stamp: str, index: dict, topologies: dict, style: dict | None = None) -> dict:
    issued = datetime.strptime(stamp, "%Y%m%d").date()
    if index.get("date") != stamp or not isinstance(index.get("name"), str) or not index["name"].strip():
        raise ValueError("data o titolo indice vigilanza non validi")
    legend = phenomenon_legend(style)
    offsets = {"oggi": 0, "domani": 1, "dopodomani": 2}
    days = [{
        "giorno": day, "data": (issued + timedelta(days=offsets[day])).isoformat(),
        "sezioni": html_sections(entry.get("html_description")),
        "fenomeni": reduce_phenomena(topologies[day], legend),
    } for day, entry in vigilanza_days(index)]
    return {
        "fonte": "Dipartimento della Protezione Civile", "licenza": "CC BY 4.0",
        "url_fonte": VIGILANZA_URL, "data": issued.isoformat(),
        "titolo": _text(index["name"]), "giorni": days,
    }
