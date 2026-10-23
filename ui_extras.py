"""Compact UI elements for Anexo 24 notifications, daily summary, and chatbot."""
from __future__ import annotations

from datetime import date
from html import escape
from pathlib import Path
from typing import Callable

import streamlit as st

CSS = """
<style>
.immex-kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,210px),1fr));gap:12px;margin:8px 0 16px}
.immex-kpi{min-width:0;background:#fff;border:1px solid #eee1e7;border-radius:12px;padding:16px 18px;box-shadow:0 3px 12px rgba(81,49,67,.05)}
.immex-header-title{font-size:1.75rem;font-weight:700;line-height:1.15;color:inherit;margin:0 0 5px;white-space:normal}
.immex-kpi-label{font-size:.82rem;color:#786a72;margin-bottom:7px}
.immex-kpi-value{font-size:1.02rem;font-weight:700;color:#3f3038;line-height:1.25;white-space:nowrap}
.immex-kpi-unit{display:inline-block;font-size:.72rem;color:#82747c;margin-left:7px;font-weight:500;vertical-align:middle}
.immex-kpi-risk,.immex-kpi-warning,.immex-kpi-neutral{border-color:#e8e2e5}
.immex-notice{border:1px solid #e8e2e5;border-radius:9px;padding:11px 13px;margin:0 0 9px;background:#fff;color:#493b43}
.immex-notice-title{font-weight:650;font-size:.91rem}.immex-notice-detail{font-size:.82rem;color:#6d5f67;margin-top:4px;line-height:1.4}
.immex-notice-action{font-size:.79rem;color:#80606d;margin-top:6px;line-height:1.4}
.immex-critical,.immex-warning,.immex-info,.immex-ok,.immex-muted{background:#fff;border-color:#e8e2e5}
.immex-group-label{font-size:.71rem;text-transform:uppercase;color:#8a7882;margin:9px 0 6px}
div[data-testid="stPopoverBody"]{min-width:min(390px,92vw);max-height:72vh;overflow-y:auto}
.st-key-cutebot{position:fixed!important;right:22px;bottom:20px;z-index:1000;width:64px!important;height:64px!important}
.st-key-cutebot [data-testid="stPopover"] button[data-testid="stPopoverButton"]{width:60px!important;height:60px!important;min-height:60px!important;padding:0!important;border-radius:50%!important;border:1px solid #e8c4d2!important;background:#fff5f8!important;color:#8f4967!important;box-shadow:0 5px 18px rgba(126,72,96,.2)!important;transition:transform .16s ease,box-shadow .16s ease}
.st-key-cutebot [data-testid="stPopover"] button[data-testid="stPopoverButton"]:hover{transform:translateY(-2px);box-shadow:0 8px 22px rgba(126,72,96,.27)!important}
.st-key-cutebot [data-testid="stPopover"] button[data-testid="stPopoverButton"] p{font-size:1.6rem!important;margin:0!important}
@media(max-width:760px){.immex-header-title{font-size:1.35rem}.immex-kpis{grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.immex-kpi{padding:12px}.immex-kpi-value{font-size:.92rem}}
</style>
"""

LEVEL_ORDER = {"critical": 0, "warning": 1, "info": 2, "ok": 3, "muted": 4}
LEVEL_LABELS = {
    "critical": "Urgentes",
    "warning": "Requieren atención",
    "info": "Informativas",
    "ok": "Estado normativo",
    "muted": "Pendientes de configurar",
}


def inject_styles() -> None:
    st.markdown(CSS, unsafe_allow_html=True)


def kpi_cards(items: list[tuple[str, str, str, str]]) -> None:
    inject_styles()
    cards = []
    for label, value, unit, tone in items:
        cards.append(
            f'<section class="immex-kpi immex-kpi-{escape(tone)}">'
            f'<div class="immex-kpi-label">{escape(label)}</div>'
            f'<div class="immex-kpi-value">{escape(value)}'
            f'<span class="immex-kpi-unit">{escape(unit)}</span></div></section>'
        )
    st.markdown('<div class="immex-kpis">' + "".join(cards) + "</div>", unsafe_allow_html=True)


def pending_count(items: list[dict]) -> int:
    return sum(item.get("level") in {"critical", "warning", "info", "muted"} for item in items)


def render_header(slot, notifications: list[dict]) -> None:
    inject_styles()
    total = pending_count(notifications)
    with slot:
        left, right = st.columns([5, 1.1], vertical_alignment="center")
        with left:
            st.markdown('<div class="immex-header"><div class="immex-header-title">Control de inventarios IMMEX</div></div>', unsafe_allow_html=True)
            st.caption("ANEXO 24 · PEPS estricto · Exposición y conciliación operativa")
        with right.popover(f"🔔 {total}", use_container_width=True):
            st.markdown(f"### Notificaciones pendientes · {total}")
            if not notifications:
                st.success("No tienes notificaciones pendientes.")
                return
            last_level = None
            for item in sorted(notifications, key=lambda value: LEVEL_ORDER.get(value.get("level", "info"), 9)):
                level = item.get("level", "info")
                if level != last_level:
                    st.markdown(f'<div class="immex-group-label">{LEVEL_LABELS.get(level, "Otras")}</div>', unsafe_allow_html=True)
                    last_level = level
                css_level = {"critical": "critical", "warning": "warning", "info": "info", "ok": "ok", "muted": "muted"}.get(level, "info")
                action = f'<div class="immex-notice-action">Siguiente paso: {escape(item["action"])}</div>' if item.get("action") else ""
                body = (
                    f'<div class="immex-notice immex-{css_level}">'
                    f'<div class="immex-notice-title">{escape(item.get("title", "Aviso"))}</div>'
                    f'<div class="immex-notice-detail">{escape(item.get("detail", ""))}</div>{action}</div>'
                )
                st.markdown(body, unsafe_allow_html=True)
                if item.get("url"):
                    st.link_button("Abrir fuente oficial", item["url"], use_container_width=True)
            if any(item.get("title") == "Chequeo normativo pendiente" for item in notifications):
                st.caption("Para inicializar la alerta diaria: publica el repositorio en GitHub, habilita Actions y ejecuta «Chequeo diario Anexo 24» una vez.")


def summary_card(text: str, source: str, when: date) -> None:
    inject_styles()
    with st.container(border=True):
        st.markdown(f"#### ☀️ Resumen del día · {when:%d/%m/%Y}")
        st.markdown(text)
        st.caption(f"Preparado con {source}")
        pdf_path = Path(__file__).parent / "data" / "Anexo_24_RGCE_2026.pdf"
        if pdf_path.exists():
            st.download_button(
                "Consultar Anexo 24 cargado · DOF 15/01/2026",
                data=pdf_path.read_bytes(),
                file_name=pdf_path.name,
                mime="application/pdf",
                key="download_anexo24_reference",
                icon=":material/menu_book:",
            )


def render_chatbot(answer_fn: Callable[[str], str]) -> None:
    inject_styles()
    history = st.session_state.setdefault("cutebot_history", [{
        "role": "assistant",
        "content": "¡Hola! Soy IA IMMEX 🤖 Puedo ayudarte con vencimientos, exposición, mermas y cambios del Anexo 24.",
    }])
    with st.container(key="cutebot"):
        with st.popover("🤖", help="Abrir chat IA IMMEX"):
            st.markdown("### IA IMMEX 🤖")
            st.caption("Copiloto IMMEX · respuestas breves y basadas en el perfil abierto")
            chat_body = st.container(height=290, border=True)
            with chat_body:
                for message in history[-8:]:
                    with st.chat_message(message["role"], avatar="🤖" if message["role"] == "assistant" else "🙂"):
                        st.markdown(message["content"])
            with st.form("cutebot_form", clear_on_submit=True, border=False):
                question = st.text_input("Pregunta", label_visibility="collapsed", placeholder="¿Qué vence primero?")
                sent = st.form_submit_button("Enviar", icon=":material/send:", use_container_width=True)
            if sent and question.strip():
                history.append({"role": "user", "content": question.strip()})
                with st.spinner("IA IMMEX está revisando el resumen…"):
                    try:
                        answer = answer_fn(question.strip())
                    except Exception as error:
                        answer = f"No pude completar la respuesta ({type(error).__name__}). Revisa el resumen del día y las notificaciones."
                history.append({"role": "assistant", "content": answer})
                st.rerun()