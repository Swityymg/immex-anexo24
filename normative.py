"""Estado persistido del chequeo diario DOF/RGCE y texto base del Anexo 24."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).parent / "data"
STATUS_PATH = DATA_DIR / "normative_status.json"
ANEXO_PATH = DATA_DIR / "anexo24_2026.txt"
BASELINE_LABEL = "Anexo 24 RGCE 2026 (DOF 15/01/2026)"
_MODELS = ["gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite", "gemini-2.5-flash"]
_working_model: str | None = None


def gemini_models() -> list[str]:
    import os

    configured = os.getenv("GEMINI_MODEL")
    ordered = ([configured] if configured else []) + ([_working_model] if _working_model else []) + _MODELS
    return list(dict.fromkeys(model for model in ordered if model))


def remember_model(model: str) -> None:
    global _working_model
    _working_model = model


def load_status() -> dict[str, Any] | None:
    try:
        return json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def load_anexo_text(max_chars: int = 90000) -> str:
    try:
        return ANEXO_PATH.read_text(encoding="utf-8")[:max_chars]
    except OSError:
        return ""


def _format_checked_at(value: str | None) -> str:
    try:
        return datetime.fromisoformat(value).strftime("%d/%m/%Y %H:%M")
    except (TypeError, ValueError):
        return "sin fecha"


def status_notification(status: dict[str, Any] | None, recent_days: int = 7) -> dict[str, str]:
    """Return a concise, cautious notification from the scheduled DOF scan."""
    if not status:
        return {
            "level": "muted",
            "title": "Chequeo normativo pendiente",
            "detail": "Aún no hay resultado del proceso diario.",
            "action": "En el repositorio de la app, habilita GitHub Actions y ejecuta una vez «Chequeo diario Anexo 24».",
        }

    try:
        checked = datetime.fromisoformat(status["checked_at"])
        cutoff = (checked - timedelta(days=recent_days)).date().isoformat()
    except (KeyError, TypeError, ValueError):
        cutoff = "0000-00-00"

    items = sorted(status.get("items", []), key=lambda item: item.get("date", ""), reverse=True)
    recent = [item for item in items if item.get("date", "") >= cutoff]
    changes = [item for item in recent if item.get("touches_anexo24")]
    if changes:
        top = changes[0]
        return {
            "level": "critical",
            "title": "Posible publicación relacionada con Anexo 24",
            "detail": f"{top.get('title', 'Publicación DOF')} · {top.get('date', '')}",
            "action": "Abrir la fuente oficial y comparar el texto antes de tomar acciones operativas.",
            "url": top.get("url", ""),
        }

    article_108 = [item for item in recent if item.get("mentions_article_108")]
    if article_108:
        top = article_108[0]
        return {
            "level": "info",
            "title": "Referencia reciente al artículo 108",
            "detail": f"{top.get('title', 'Publicación DOF')} · {top.get('date', '')}; relación potencial, no cambio confirmado al Anexo 24.",
            "action": "Revisar el texto oficial y determinar si tiene impacto en los materiales IMMEX del perfil.",
            "url": top.get("url", ""),
        }

    if status.get("days_ok", 0) == 0:
        return {
            "level": "warning",
            "title": "No se pudo verificar el DOF",
            "detail": "; ".join(status.get("errors", []))[:180] or "La fuente no respondió.",
            "action": "El resultado no confirma ausencia de cambios; consulta la publicación oficial.",
        }

    other_recent = [item for item in recent if item not in changes]
    if other_recent:
        top = other_recent[0]
        return {
            "level": "info",
            "title": "Publicación RGCE reciente para revisar",
            "detail": f"{top.get('title', 'Publicación DOF')} · {top.get('date', '')}; el barrido no detectó mención directa al Anexo 24.",
            "action": "Revisar el documento oficial; el filtro automatizado puede omitir cambios indirectos.",
            "url": top.get("url", ""),
        }

    return {
        "level": "ok",
        "title": "Sin cambios detectados en el barrido",
        "detail": f"{status.get('days_ok', 0)} días consultados · último chequeo {_format_checked_at(status.get('checked_at'))} · base: {BASELINE_LABEL}.",
        "action": "Esto no sustituye revisar la publicación oficial completa.",
    }


def local_normative_answer(status: dict[str, Any] | None) -> str:
    notice = status_notification(status)
    lines = [f"**{notice['title']}.** {notice['detail']}"]
    if notice.get("action"):
        lines.append(notice["action"])
    lines.append("El barrido es automático y limitado a títulos/textos publicados; no es una certificación legal.")
    return "\n\n".join(lines)
