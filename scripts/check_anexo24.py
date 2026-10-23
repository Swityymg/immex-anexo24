#!/usr/bin/env python3
"""Chequeo diario del DOF: busca publicaciones de RGCE que puedan afectar el Anexo 24."""
from __future__ import annotations

import html
import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "normative_status.json"
TZ = ZoneInfo("America/Mexico_City")
BASE = "https://www.dof.gob.mx/"
HEADERS = {"User-Agent": "Mozilla/5.0 (IMMEX-Anexo24-Monitor)"}
TITLE_TERMS = (
    "reglas generales de comercio exterior", "anexo 24", "control de inventarios",
    "ley aduanera", "artículo 108", "articulo 108",
)
RULE_RE = re.compile(r"\b(?:4\.3\.1|4\.8\.3|7\.1\.1|7\.1\.4|7\.5\.1)\b")
ARTICLE_108_RE = re.compile(r"\b(?:art[ií]culo|art\.)\s*108\b", re.I)
LINK_RE = re.compile(r'<a[^>]+href="([^"]*nota_detalle[^"]*)"[^>]*>(.*?)</a>', re.I | re.S)
TAG_RE = re.compile(r"<[^>]+>")


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(TAG_RE.sub(" ", text))).strip()


def fetch(url: str) -> str:
    response = requests.get(url, headers=HEADERS, timeout=25)
    response.raise_for_status()
    response.encoding = response.apparent_encoding or response.encoding
    return response.text


def scan_day(day) -> list[dict]:
    index = fetch(f"{BASE}index.php?year={day:%Y}&month={day:%m}&day={day:%d}")
    links = LINK_RE.findall(index)
    if not links and day.weekday() < 6:
        raise RuntimeError(f"índice {day:%d/%m/%Y} sin enlaces (¿cambió el formato?)")
    found, seen = [], set()
    for href, raw_title in links:
        title = clean(raw_title)
        url = urljoin(BASE, html.unescape(href))
        if url in seen or not any(term in title.lower() for term in TITLE_TERMS):
            continue
        seen.add(url)
        body = clean(fetch(url)).lower()
        terms = (["Anexo 24"] if "anexo 24" in body else []) + sorted(set(RULE_RE.findall(body)))
        mentions_article_108 = bool(ARTICLE_108_RE.search(body))
        if mentions_article_108:
            terms.append("Ley Aduanera, artículo 108")
        found.append({
            "date": day.isoformat(), "title": title, "url": url,
            "touches_anexo24": bool("Anexo 24" in terms or RULE_RE.search(body)),
            "mentions_article_108": mentions_article_108,
            "terms": terms,
        })
    return found


def main() -> None:
    days = max(1, int(os.getenv("DAYS") or 3))
    today = datetime.now(TZ).date()
    previous = {}
    if OUT.exists():
        try:
            previous = json.loads(OUT.read_text(encoding="utf-8"))
        except ValueError:
            pass
    items = {i["url"]: i for i in previous.get("items", [])}
    errors, days_ok = [], 0
    for offset in range(days):
        day = today - timedelta(days=offset)
        try:
            for item in scan_day(day):
                items[item["url"]] = item
            days_ok += 1
        except Exception as error:  # noqa: BLE001
            errors.append(f"{day:%d/%m}: {type(error).__name__}: {error}"[:200])
    ordered = sorted(items.values(), key=lambda i: i["date"], reverse=True)[:60]
    status = {"checked_at": datetime.now(TZ).isoformat(timespec="seconds"),
              "days_scanned": days, "days_ok": days_ok, "errors": errors, "items": ordered}
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Días OK: {days_ok}/{days} · publicaciones: {len(ordered)} · errores: {len(errors)}")


if __name__ == "__main__":
    main()
