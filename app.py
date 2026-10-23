from __future__ import annotations

import json
import os
import tomllib
from datetime import date, datetime
from io import StringIO
from pathlib import Path
from typing import Any

import pandas as pd
import plotly.express as px
import streamlit as st

from ledger import (
    DEFAULT_WORKBOOK, calculate_ledger, create_empty_template,
    load_csv_source_data, load_custom_xlsx_source_data, load_source_data,
)
from persistence import (
    DB_PATH, add_audit_event, add_entry, add_exit, get_audit_events, get_captures,
    get_close_steps, get_setting, initialize_database, set_close_step, set_setting,
)
from normative import gemini_models, load_anexo_text, load_status, local_normative_answer, remember_model, status_notification
from ui_extras import kpi_cards, render_chatbot, render_header, summary_card

st.set_page_config(page_title="IMMEX | Control Anexo 24", page_icon="📦", layout="wide")
initialize_database()


@st.cache_data(show_spinner=False)
def load_data(path: str) -> dict[str, Any]:
    return load_source_data(path)


def money(value: float) -> str:
    return f"${value:,.2f} MXN"


def fmt_kg(value: float) -> str:
    return f"{value:,.2f} kg"


def gemini_api_key() -> str | None:
    key, _, _ = gemini_credential_status()
    return key


def gemini_credential_status() -> tuple[str | None, str, str | None]:
    key = os.getenv("GEMINI_API_KEY")
    if key:
        return key, "variable de entorno", None
    secrets_file = Path(__file__).parent / ".streamlit" / "secrets.toml"
    if secrets_file.exists():
        try:
            tomllib.loads(secrets_file.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as error:
            line = getattr(error, "lineno", None)
            column = getattr(error, "colno", None)
            location = f"línea {line}, columna {column}" if line and column else str(error).split("(")[-1].rstrip(")")
            return None, "", f".streamlit/secrets.toml no es TOML válido ({location}). Usa GEMINI_API_KEY = \"tu_clave\"."
    try:
        value = st.secrets.get("GEMINI_API_KEY")
        if value:
            return str(value), "st.secrets", None
        return None, "", None
    except Exception as error:
        if type(error).__name__ in {"StreamlitSecretNotFoundError", "FileNotFoundError"}:
            return None, "", None
        return None, "", f"No se pudo leer .streamlit/secrets.toml ({type(error).__name__}); verifica que sea TOML válido y que la clave esté entre comillas dobles."


def ask_gemini(prompt: str, system_instruction: str | None = None) -> str:
    key = gemini_api_key()
    if not key:
        raise RuntimeError("GEMINI_API_KEY no está configurada.")
    from google import genai

    client = genai.Client(api_key=key)
    config = None
    if system_instruction:
        from google.genai import types

        config = types.GenerateContentConfig(system_instruction=system_instruction)
    last_error: Exception | None = None
    for model in gemini_models():
        try:
            response = client.models.generate_content(model=model, contents=prompt, config=config)
            text = getattr(response, "text", None)
            if not text:
                raise RuntimeError(f"Gemini {model} devolvió una respuesta vacía.")
            remember_model(model)
            return text
        except Exception as error:
            last_error = error
    raise RuntimeError(f"Ningún modelo Gemini configurado respondió: {last_error}")


def read_csv_rows(upload: Any, kind: str) -> list[dict[str, Any]]:
    frame = pd.read_csv(upload, sep=None, engine="python", encoding="utf-8-sig")
    frame.columns = [str(column).strip().lower() for column in frame.columns]
    if kind == "Entradas":
        required = {"pedimento", "clave", "fecha_entrada", "material", "cantidad_kg", "valor_unit_usd"}
        missing = required - set(frame.columns)
        if missing:
            raise ValueError(f"Faltan columnas: {', '.join(sorted(missing))}")
        return [{
            "pedimento": str(row.pedimento).strip(), "clave": str(row.clave).strip(),
            "fecha_entrada": pd.to_datetime(row.fecha_entrada).date(), "material": str(row.material).strip(),
            "cantidad_kg": float(row.cantidad_kg), "valor_unit_usd": float(row.valor_unit_usd),
        } for row in frame.itertuples(index=False)]
    required = {"pedimento", "clave", "fecha", "material", "tipo", "cantidad_kg"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"Faltan columnas: {', '.join(sorted(missing))}")
    return [{
        "pedimento": str(row.pedimento).strip(), "clave": str(row.clave).strip(),
        "fecha": pd.to_datetime(row.fecha).date(), "material": str(row.material).strip(),
        "tipo": str(row.tipo).strip(), "cantidad_kg": float(row.cantidad_kg),
        "observacion": str(getattr(row, "observacion", "") or ""),
    } for row in frame.itertuples(index=False)]


def csv_report(title: str, company: str, rfc: str, frame: pd.DataFrame) -> bytes:
    output = StringIO()
    pd.DataFrame([["Reporte Anexo 24", title], ["Razón social", company], ["RFC", rfc]]).to_csv(
        output, index=False, header=False
    )
    frame.to_csv(output, index=False, lineterminator="\n")
    return output.getvalue().encode("utf-8-sig")


def balance_by_fraction(ledger: pd.DataFrame) -> pd.DataFrame:
    active = ledger.loc[ledger["saldo_kg"] > 0]
    if active.empty:
        return pd.DataFrame(columns=["fraccion", "materiales", "pedimentos", "saldo_kg", "riesgo_mxn", "primer_vencimiento"])
    return active.groupby("fraccion", dropna=False, as_index=False).agg(
        materiales=("material", "nunique"), pedimentos=("pedimento", "nunique"),
        saldo_kg=("saldo_kg", "sum"), riesgo_mxn=("riesgo_mxn", "sum"),
        primer_vencimiento=("vencimiento", "min"),
    ).sort_values("primer_vencimiento")


def local_copilot_answer(question: str, result: dict[str, Any], cutoff: date, tolerance: float,
                        production: pd.DataFrame, company: str, rfc: str) -> str:
    metrics = result["metrics"]
    normalized_question = question.casefold()
    parts = [f"**{company} · RFC {rfc} · corte {cutoff:%d/%m/%Y}**"]
    if any(word in normalized_question for word in ("merma", "desperdicio", "scrap")):
        high_capture = production.groupby("producto")["merma_pct"].max().to_dict() if not production.empty else {}
        if high_capture:
            max_product, max_rate = max(high_capture.items(), key=lambda item: item[1])
            parts.append(
                f"La merma capturada máxima es {float(max_rate):.2%} para {max_product}; "
                f"la tolerancia interna vigente es {tolerance:.2%}. Las tasas consolidadas por material se calculan "
                "ponderando kilos teóricos y pueden ser distintas de la tasa máxima de un mes."
            )
        above = result["merma"].loc[result["merma"]["merma_pct"] > tolerance]
        if not above.empty:
            parts.append("Revisiones consolidadas: " + "; ".join(
                f"{row.producto}/{row.material} {row.merma_pct:.2%}" for row in above.itertuples()
            ) + ".")
    elif any(word in normalized_question for word in ("faltante", "descuadre", "mp-", "conciliar")):
        deficits = result["reconciliation"].loc[result["reconciliation"]["faltante_kg"] > 0]
        if deficits.empty:
            parts.append("No hay faltantes calculados en este perfil.")
        else:
            for row in deficits.itertuples():
                parts.append(
                    f"{row.material}: faltan {fmt_kg(row.faltante_kg)}; requerida "
                    f"{fmt_kg(row.descarga_requerida_kg)} y entradas {fmt_kg(row.importado_kg)}."
                )
                for exit_row in row.salidas_detalle:
                    parts.append(f"La salida asociada registrada es {exit_row['pedimento']} ({exit_row['tipo']}) por {fmt_kg(exit_row['cantidad_kg'])}.")
    elif any(word in normalized_question for word in ("vence", "vencimiento", "riesgo", "alerta", "pedimento")):
        alerts = result["ledger"].loc[
            result["ledger"]["saldo_kg"].gt(0) &
            result["ledger"]["estatus"].isin({"VENCIDO", "ALERTA 30", "ALERTA 60", "ALERTA 90"})
        ].sort_values("dias_restantes")
        parts.append(
            f"Riesgo en ventanas: {money(metrics['riesgo_alertas_mxn'])} en "
            f"{metrics['pedimentos_alerta']} pedimentos; exposición total {money(metrics['saldo_total_mxn'])}."
        )
        for row in alerts.itertuples():
            parts.append(
                f"{row.pedimento} · {row.material} · {row.dias_restantes} días · "
                f"{money(row.riesgo_mxn)} · vence {row.vencimiento:%d/%m/%Y}."
            )
        if not alerts.empty:
            first = alerts.iloc[0]
            if first["dias_restantes"] <= 30:
                parts.append(
                    f"Prioridad: programar retorno/exportación o evaluar cambio de régimen para "
                    f"{first['pedimento']}; validar documentos y fecha de vencimiento con el responsable aduanero."
                )
    else:
        parts.append(
            f"Exposición {money(metrics['saldo_total_mxn'])}; riesgo dentro de alertas "
            f"{money(metrics['riesgo_alertas_mxn'])}; pedimentos en alerta {metrics['pedimentos_alerta']}; "
            f"faltante {fmt_kg(metrics['faltante_total_kg'])}."
        )
    parts.append("Acción recomendada: cotejar los cálculos con documentos fuente y registrar cualquier ajuste con motivo y evidencia.")
    return "\n\n".join(parts)


@st.fragment(run_every="1s")
def _local_impact_narrative(delta_exposure: float, delta_risk: float, hyp: dict, base: dict) -> str:
    lines = ["**Análisis del escenario hipotético (cálculo local)**\n"]
    if abs(delta_exposure) < 1:
        lines.append("La exposición total no cambia con este escenario.")
    elif delta_exposure < 0:
        lines.append(f"La exposición total **disminuiría {money(abs(delta_exposure))}**, pasando de {money(base['saldo_total_mxn'])} a {money(hyp['saldo_total_mxn'])}.")
    else:
        lines.append(f"La exposición total **aumentaría {money(delta_exposure)}**, pasando de {money(base['saldo_total_mxn'])} a {money(hyp['saldo_total_mxn'])}.")

    if abs(delta_risk) < 1:
        lines.append("El riesgo en ventanas de alerta no cambia.")
    elif delta_risk < 0:
        lines.append(f"El riesgo en alertas **bajaría {money(abs(delta_risk))}** (de {money(base['riesgo_alertas_mxn'])} a {money(hyp['riesgo_alertas_mxn'])}).")
    else:
        lines.append(f"El riesgo en alertas **subiría {money(delta_risk)}** (de {money(base['riesgo_alertas_mxn'])} a {money(hyp['riesgo_alertas_mxn'])}).")

    delta_ped = hyp["pedimentos_alerta"] - base["pedimentos_alerta"]
    if delta_ped < 0:
        lines.append(f"El número de pedimentos en alerta se reduciría en {abs(delta_ped)} ({base['pedimentos_alerta']} → {hyp['pedimentos_alerta']}).")
    elif delta_ped > 0:
        lines.append(f"El número de pedimentos en alerta aumentaría en {delta_ped} ({base['pedimentos_alerta']} → {hyp['pedimentos_alerta']}).")

    delta_falt = hyp["faltante_total_kg"] - base["faltante_total_kg"]
    if abs(delta_falt) > 0.01:
        direction = "aumentaría" if delta_falt > 0 else "disminuiría"
        lines.append(f"El faltante total {direction} en {fmt_kg(abs(delta_falt))} ({fmt_kg(base['faltante_total_kg'])} → {fmt_kg(hyp['faltante_total_kg'])}).")

    lines.append("\n_Acción recomendada: valida los supuestos del escenario con documentos fuente antes de tomar decisiones._")
    return "\n\n".join(lines)


def render_close_clock(started_at: str) -> None:
    elapsed = max(0, int((datetime.now().astimezone() - datetime.fromisoformat(started_at)).total_seconds()))
    st.metric("Tiempo del cierre", f"{elapsed // 3600:02d}:{elapsed // 60 % 60:02d}:{elapsed % 60:02d}")


with st.sidebar:
    st.header("Perfil y sesión")
    role = st.radio("Vista", ["Operación", "Gerencia", "Auditor (solo lectura)"])
    readonly = role == "Auditor (solo lectura)"
    operator = st.text_input("Usuario / responsable", "Auditor" if readonly else "Operador IMMEX", disabled=readonly)
    reason = st.text_input("Motivo", "Consulta de auditoría" if readonly else "")
    configured_key, key_source, key_error = gemini_credential_status()
    if key_error:
        st.error("IA IMMEX · secrets.toml inválido. La clave debe tener formato `GEMINI_API_KEY = \"...\"`.")
    elif configured_key:
        st.caption(f"IA IMMEX · Gemini conectado desde {key_source}")
    else:
        st.caption("IA IMMEX · modo local; configura GEMINI_API_KEY")
    if not readonly and not reason.strip():
        st.caption("Indica el motivo para habilitar cambios y altas auditables.")
    data_origin = st.radio(
        "Origen de datos",
        ["Archivo oficial de prueba", "Cargar archivo propio (.xlsx o .csv)"],
        disabled=readonly,
    )
    own_upload = None
    if data_origin == "Cargar archivo propio (.xlsx o .csv)" and not readonly:
        own_upload = st.file_uploader("Archivo propio", type=["xlsx", "csv"])
    st.download_button(
        "Descargar plantilla modelo vacía",
        data=create_empty_template(),
        file_name="Plantilla_Anexo24_vacia.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        disabled=readonly,
    )

profile_name = DEFAULT_WORKBOOK.name
try:
    if own_upload is not None:
        if own_upload.name.lower().endswith(".csv"):
            source = load_csv_source_data(own_upload)
        else:
            source = load_custom_xlsx_source_data(own_upload)
        profile_name = own_upload.name
    else:
        source = load_data(str(DEFAULT_WORKBOOK))
except Exception as error:
    st.error(f"No se pudo leer el origen seleccionado: {error}")
    st.stop()
profile_name = source.get("profile_name", profile_name)

params = source["params"]
company = str(params.get("Razón social", Path(profile_name).stem))
rfc = str(params.get("RFC", Path(profile_name).stem))
cutoff_default = params.get("Fecha de corte") or date.today()
if isinstance(cutoff_default, str):
    cutoff_default = pd.to_datetime(cutoff_default).date()
default_fx = float(params.get("Tipo de cambio MXN por USD", 18.5))
default_tolerance = float(params.get("Tolerancia de merma (alerta interna)", 0.03))
entries_db, exits_db = get_captures(rfc)
if entries_db:
    source["entries"] = pd.concat([source["entries"], pd.DataFrame(entries_db)], ignore_index=True, sort=False)
if exits_db:
    source["exits"] = pd.concat([source["exits"], pd.DataFrame(exits_db)], ignore_index=True, sort=False)
if entries_db or exits_db:
    source["baseline_oracles"] = False
base_exits = source["exits"].copy()
scenario = st.session_state.get("peps_simulation")
if scenario and st.session_state.get("peps_applied", False):
    source["exits"] = pd.concat([base_exits, pd.DataFrame([{
        "pedimento": "SIMULACION", "clave": "SIM", "fecha": scenario.get("fecha", cutoff_default),
        "material": scenario["material"], "tipo": "Escenario temporal",
        "cantidad_kg": scenario["quantity"], "observacion": scenario["reason"],
    }])], ignore_index=True)
    source["baseline_oracles"] = False

saved_cutoff = get_setting(rfc, "fecha_corte", cutoff_default.isoformat())
saved_fx = float(get_setting(rfc, "tipo_cambio", default_fx))
saved_tolerance = float(get_setting(rfc, "tolerancia_merma", default_tolerance))
saved_cutoff = pd.to_datetime(saved_cutoff).date() if isinstance(saved_cutoff, str) else saved_cutoff
with st.sidebar:
    st.divider()
    cutoff = st.date_input("Fecha de corte", saved_cutoff, disabled=readonly or not reason.strip())
    exchange_rate = st.number_input("Tipo de cambio MXN/USD", min_value=0.0001, value=saved_fx, step=0.1, format="%.4f", disabled=readonly or not reason.strip())
    tolerance = st.slider("Tolerancia interna de merma %", 0.0, 15.0, saved_tolerance * 100, 0.1, disabled=readonly or not reason.strip()) / 100
if not readonly:
    audit_user = operator.strip() or "Operador IMMEX"
    if reason.strip():
        set_setting(rfc, "fecha_corte", cutoff.isoformat(), audit_user, reason.strip())
        set_setting(rfc, "tipo_cambio", float(exchange_rate), audit_user, reason.strip())
        set_setting(rfc, "tolerancia_merma", float(tolerance), audit_user, reason.strip())

result = calculate_ledger(source, cutoff, exchange_rate, tolerance)
ledger = result["ledger"]
metrics = result["metrics"]
active_lots = ledger.loc[ledger["saldo_kg"] > 0].sort_values("dias_restantes")
first_due = active_lots.iloc[0] if not active_lots.empty else None
norm_status = load_status()

notification_items: list[dict[str, str]] = []
urgent_lots = active_lots.loc[active_lots["dias_restantes"] <= 30]
for _, lot in urgent_lots.iterrows():
    notification_items.append({
        "level": "critical",
        "title": f"Vencimiento · {lot['pedimento']} · {lot['material']}",
        "detail": f"{int(lot['dias_restantes'])} días · {money(lot['riesgo_mxn'])} en riesgo",
        "action": f"Prepara un borrador de plan de retorno o regularización para {lot['pedimento']} ({lot['material']}); solicita evidencia y no afirmes una rectificación sin revisar documentos.",
        "button": "Preparar borrador de retorno",
    })
for _, shortage in result["reconciliation"].loc[result["reconciliation"]["faltante_kg"] > 1e-9].iterrows():
    notification_items.append({
        "level": "warning",
        "title": f"Faltante · {shortage['material']}",
        "detail": f"{fmt_kg(shortage['faltante_kg'])} por conciliar",
        "action": f"Prepara una lista de verificación documental para el faltante de {shortage['material']} por {fmt_kg(shortage['faltante_kg'])}, incluyendo entradas, consumos y salidas asociadas.",
        "button": "Preparar conciliación",
    })
if not source["production"].empty:
    deviation_by_product = source["production"].groupby("producto")["merma_pct"].max()
    for product, rate in deviation_by_product.items():
        if float(rate) > tolerance:
            production_row = source["production"].loc[
                (source["production"]["producto"] == product) &
                (source["production"]["merma_pct"] == rate)
            ].iloc[0]
            notification_items.append({
                "level": "info",
                "title": f"Desviación operativa · {product}",
                "detail": f"Merma capturada {float(rate):.2%} · tolerancia interna {tolerance:.2%} · {production_row['periodo']}",
                "action": f"Sugiere una revisión de causa raíz para merma {product} de {float(rate):.2%} en {production_row['periodo']}; solicita lote, turno, calidad y evidencia de báscula.",
                "button": "Preparar revisión de proceso",
            })

norm_notification = status_notification(norm_status)
notification_items.append(norm_notification)


def app_context() -> dict[str, Any]:
    production_rates = source["production"].groupby("producto")["merma_pct"].max().to_dict() if not source["production"].empty else {}
    alert_statuses = {"VENCIDO", "ALERTA 30", "ALERTA 60", "ALERTA 90"}
    return {
        "perfil": profile_name, "razon_social": company, "rfc": rfc, "fecha_corte": cutoff.isoformat(),
        "riesgo_total_mxn": round(metrics["saldo_total_mxn"], 2),
        "riesgo_en_alertas_mxn": round(metrics["riesgo_alertas_mxn"], 2),
        "pedimentos_en_alerta": ledger.loc[ledger["estatus"].isin(alert_statuses),
            ["pedimento", "material", "saldo_kg", "riesgo_mxn", "dias_restantes", "estatus"]].to_dict("records"),
        "faltantes": result["reconciliation"].loc[result["reconciliation"]["faltante_kg"] > 0,
            ["material", "descripcion", "faltante_kg", "salidas_detalle"]].to_dict("records"),
        "merma_maxima_capturada_por_producto": production_rates,
        "merma_consolidada": result["merma"].to_dict("records"),
        "tolerancia_interna": tolerance,
        "vigilante_anexo24": norm_status,
    }


def answer_question(question: str) -> str:
    system_prompt = (
        "Eres IA IMMEX, copiloto del Control Anexo 24. Responde en español, de forma amable y breve. "
        "Distingue hechos calculados de hipótesis, no inventes normas ni des asesoría legal definitiva. "
        "Para preguntas normativas usa el estado persistido del monitor y el texto oficial Anexo 24 RGCE 2026 "
        "fechado 15/01/2026 que sigue; no afirmes que es una versión más reciente que esa fecha.\n\n"
        "ESTADO DEL MONITOR:\n" + json.dumps(norm_status, ensure_ascii=False, default=str) +
        "\n\nCONTEXTO DEL LEDGER:\n" + json.dumps(app_context(), ensure_ascii=False, default=str) +
        "\n\nTEXTO DE REFERENCIA DEL ANEXO 24:\n" + load_anexo_text()
    )
    if gemini_api_key():
        try:
            return ask_gemini(question, system_instruction=system_prompt)
        except Exception as error:
            fallback = local_normative_answer(norm_status) if any(term in question.casefold() for term in ("dof", "rgce", "anexo", "cambio", "actualiz")) else local_copilot_answer(question, result, cutoff, tolerance, source["production"], company, rfc)
            return f"Gemini no respondió ({type(error).__name__}). Respuesta local, no generada por IA.\n\n{fallback}"
    if any(term in question.casefold() for term in ("dof", "rgce", "anexo", "cambio", "actualiz", "norma")):
        return "**Modo local (sin Gemini).**\n\n" + local_normative_answer(norm_status)
    return "**Modo local (sin Gemini).**\n\n" + local_copilot_answer(question, result, cutoff, tolerance, source["production"], company, rfc)


def daily_summary() -> tuple[str, str]:
    cache_key = (rfc, cutoff.isoformat(), round(metrics["saldo_total_mxn"], 2),
                 round(metrics["riesgo_alertas_mxn"], 2), norm_status.get("checked_at") if norm_status else None,
                 bool(gemini_api_key()))
    cached = st.session_state.get("daily_summary")
    if cached and cached["key"] == cache_key:
        return cached["text"], cached["source"]
    lines = [
        f"- **Exposición:** {money(metrics['saldo_total_mxn'])}; **en ventanas de alerta:** {money(metrics['riesgo_alertas_mxn'])} en {metrics['pedimentos_alerta']} pedimento(s)."
    ]
    if first_due is not None:
        days = int(first_due["dias_restantes"])
        due = f"venció hace {abs(days)} días" if days < 0 else f"vence en {days} días ({first_due['vencimiento']:%d/%m/%Y})"
        lines.append(f"- **Lo más urgente:** {first_due['pedimento']} · {first_due['material']} {due}.")
    if metrics["faltante_total_kg"] > 0:
        lines.append(f"- **Por conciliar:** {fmt_kg(metrics['faltante_total_kg'])} de faltante agregado.")
    lines.append(f"- **Anexo 24:** {norm_notification['title']}. {norm_notification['detail']}")
    summary, source_label = "\n".join(lines), "cálculo local"
    if gemini_api_key():
        try:
            summary = ask_gemini(
                "Escribe un resumen ejecutivo del día en exactamente cuatro viñetas breves con prioridad y siguiente paso. "
                "No inventes hechos normativos; usa solo el contexto: " + json.dumps(app_context(), ensure_ascii=False, default=str),
                system_instruction=(
                    "Eres IA IMMEX, asistente ejecutivo. No des asesoría legal definitiva. Para contenido normativo "
                    "la única fuente documental cargada es el Anexo 24 RGCE 2026 con fecha 15/01/2026; no afirmes que "
                    "es la versión vigente más reciente. Texto de referencia:\n" + load_anexo_text()
                ),
            )
            source_label = "Gemini"
        except Exception:
            source_label = "cálculo local (Gemini no disponible)"
    st.session_state.daily_summary = {"key": cache_key, "text": summary, "source": source_label}
    return summary, source_label


header_slot = st.container()
render_header(header_slot, notification_items)
kpi_cards([
    ("Exposición total", f"${metrics['saldo_total_mxn']:,.2f}", "MXN", "risk"),
    ("En ventanas de alerta", f"${metrics['riesgo_alertas_mxn']:,.2f}", "MXN", "warning"),
    ("Pedimentos por atender", str(metrics["pedimentos_alerta"]), "", "neutral"),
    ("Faltante por conciliar", f"{metrics['faltante_total_kg']:,.2f}", "kg", "warning"),
])
show_daily_summary = st.toggle("☀️ Resumen del día", value=False)
if show_daily_summary:
    summary_text, summary_source = daily_summary()
    summary_card(summary_text, summary_source, cutoff)
render_chatbot(answer_question)

if readonly:
    tabs = st.tabs(["Exposición", "Ledger", "Reporte por fracción", "Bitácora"])
elif role == "Gerencia":
    tabs = st.tabs(["Dashboard BI", "Riesgo y vencimientos", "Reportes", "Bitácora"])
else:
    tabs = st.tabs([
        "Panel de operación", "Ledger y Simulador PEPS", "Captura operativa", "Excepciones y Mermas",
        "Cierre mensual", "Reportes Anexo 24", "Bitácora", "🔮 Simulador de Impacto", "🔍 Clasificador Arancelario",
    ])

if readonly:
    with tabs[0]:
        statuses = ["VENCIDO", "ALERTA 30", "ALERTA 60", "ALERTA 90", "OK", "Agotado"]
        exposure = ledger.groupby("estatus", as_index=False)["riesgo_mxn"].sum().set_index("estatus").reindex(statuses, fill_value=0).reset_index()
        figure = px.bar(exposure, x="estatus", y="riesgo_mxn", color="estatus", title="Exposición por estatus")
        st.plotly_chart(figure, use_container_width=True)
    with tabs[1]:
        st.dataframe(ledger, use_container_width=True, hide_index=True)
    with tabs[2]:
        st.dataframe(balance_by_fraction(ledger), use_container_width=True, hide_index=True)
    with tabs[3]:
        st.dataframe(pd.DataFrame(get_audit_events(rfc)), use_container_width=True, hide_index=True)
elif role == "Gerencia":
    with tabs[0]:
        st.subheader("Dashboard ejecutivo")
        left, right = st.columns([1.4, 1])
        with left:
            if not ledger.empty:
                exposure = balance_by_fraction(ledger)
                fraction_chart = px.bar(
                    exposure.sort_values("riesgo_mxn", ascending=True),
                    x="riesgo_mxn", y="fraccion", orientation="h",
                    labels={"riesgo_mxn": "Exposición (MXN)", "fraccion": "Fracción arancelaria"},
                    title="Exposición por fracción arancelaria",
                    color_discrete_sequence=["#8297a5"],
                )
                fraction_chart.update_layout(showlegend=False, height=340, xaxis_tickprefix="$", xaxis_tickformat=",.0f")
                st.plotly_chart(fraction_chart, use_container_width=True)
            else:
                st.info("No hay lotes para mostrar.")
        with right:
            status_order = ["VENCIDO", "ALERTA 30", "ALERTA 60", "ALERTA 90", "OK", "Agotado"]
            status_data = ledger.groupby("estatus", as_index=False)["riesgo_mxn"].sum()
            status_data = status_data.set_index("estatus").reindex(status_order, fill_value=0).reset_index()
            status_chart = px.pie(
                status_data, names="estatus", values="riesgo_mxn", hole=0.62,
                title="Composición de exposición",
                color_discrete_sequence=["#be6575", "#d89b61", "#d3ba72", "#80a69a", "#7f99a8", "#c9c2c5"],
            )
            status_chart.update_layout(height=340, showlegend=True, legend_title_text="Estatus")
            st.plotly_chart(status_chart, use_container_width=True)

        st.subheader("Tendencia de vencimientos")
        if not active_lots.empty:
            horizon = active_lots.loc[active_lots["dias_restantes"] <= 180].copy()
            if not horizon.empty:
                horizon["ventana"] = pd.cut(
                    horizon["dias_restantes"], bins=[-100000, 0, 30, 60, 90, 180],
                    labels=["Vencido", "0-30 días", "31-60 días", "61-90 días", "91-180 días"],
                )
                timeline = horizon.groupby("ventana", observed=False, as_index=False).agg(
                    lotes=("pedimento", "nunique"), riesgo_mxn=("riesgo_mxn", "sum")
                )
                st.dataframe(timeline, use_container_width=True, hide_index=True,
                             column_config={"riesgo_mxn": st.column_config.NumberColumn(format="$%.2f")})
            else:
                st.success("No hay vencimientos dentro de 180 días.")
        st.subheader("Indicadores operativos")
        scrap_columns = st.columns(3)
        scrap_columns[0].metric("Materiales con faltante", str(int((result["reconciliation"]["faltante_kg"] > 0).sum())))
        scrap_columns[1].metric("Desviaciones de merma", str(int((result["merma"]["merma_pct"] > tolerance).sum())))
        scrap_columns[2].metric("Valor total en inventario", money(metrics["saldo_total_mxn"]))

    with tabs[1]:
        st.subheader("Lotes para decisión gerencial")
        st.dataframe(
            active_lots[["pedimento", "material", "fraccion", "saldo_kg", "riesgo_mxn", "vencimiento", "dias_restantes", "estatus"]],
            use_container_width=True, hide_index=True,
            column_config={"riesgo_mxn": st.column_config.NumberColumn(format="$%.2f")},
        )
        st.subheader("Faltantes y merma")
        manager_exceptions, manager_scrap = st.columns(2)
        with manager_exceptions:
            st.caption("Faltantes por material")
            st.dataframe(
                result["reconciliation"].loc[result["reconciliation"]["faltante_kg"] > 0,
                    ["material", "descripcion", "faltante_kg"]],
                use_container_width=True, hide_index=True,
            )
        with manager_scrap:
            st.caption("Mermas consolidadas sobre tolerancia")
            st.dataframe(
                result["merma"].loc[result["merma"]["merma_pct"] > tolerance,
                    ["producto", "material", "merma_pct", "tolerancia_pct"]],
                use_container_width=True, hide_index=True,
                column_config={"merma_pct": st.column_config.NumberColumn(format="percent"),
                               "tolerancia_pct": st.column_config.NumberColumn(format="percent")},
            )

    with tabs[2]:
        st.subheader("Reportes ejecutivos")
        st.dataframe(balance_by_fraction(ledger), use_container_width=True, hide_index=True)
        st.download_button(
            "Descargar saldos por fracción",
            data=csv_report("Saldos por fracción", company, rfc, balance_by_fraction(ledger)),
            file_name=f"anexo24_{rfc}_saldos_por_fraccion_{cutoff.isoformat()}.csv",
            mime="text/csv",
        )

    with tabs[3]:
        events = pd.DataFrame(get_audit_events(rfc))
        st.subheader("Bitácora")
        st.dataframe(events, use_container_width=True, hide_index=True)
else:
    with tabs[0]:
        st.subheader("Prioridades de hoy")
        if first_due is not None:
            days = int(first_due["dias_restantes"])
            if days < 0:
                action = "Escalar hoy: confirmar existencia y evaluar retorno o cambio de régimen con el responsable aduanero."
            elif days <= 30:
                action = "Prioridad inmediata: programar retorno/exportación o cambio de régimen antes del vencimiento."
            elif days <= 60:
                action = "Reservar transporte y expediente de retorno; validar transferencia o cambio de régimen."
            elif days <= 90:
                action = "Calendarizar retorno/exportación y confirmar el saldo del lote."
            else:
                action = "Monitorear saldo y mantener evidencia de destino vinculada al pedimento."
            st.info(action)
        recommendations = active_lots.loc[active_lots["dias_restantes"] <= 90].copy()
        if not recommendations.empty:
            recommendations["accion_sugerida"] = recommendations["dias_restantes"].map(
                lambda days: "Regularizar de inmediato" if days < 0 else
                "Retornar/exportar o cambiar régimen" if days <= 30 else
                "Programar retorno y validar expediente" if days <= 60 else
                "Calendarizar retorno antes de 90 días"
            )
            st.dataframe(
                recommendations[["pedimento", "material", "fraccion", "saldo_kg", "riesgo_mxn", "vencimiento", "dias_restantes", "accion_sugerida"]],
                use_container_width=True, hide_index=True,
                column_config={"riesgo_mxn": st.column_config.NumberColumn("Riesgo MXN", format="$%.2f")},
            )
        else:
            st.success("No hay lotes en la ventana preventiva de 90 días.")
        st.subheader("Línea de vencimientos")
        timeline = active_lots.loc[active_lots["dias_restantes"] <= 180]
        if not timeline.empty:
            figure = px.scatter(
                timeline, x="dias_restantes", y="pedimento", size="riesgo_mxn", color="estatus",
                hover_data=["material", "saldo_kg", "vencimiento"],
                labels={"dias_restantes": "Días restantes", "pedimento": "Pedimento"},
            )
            for days, color in [(30, "#c23b22"), (60, "#df8b24"), (90, "#c6a72d")]:
                figure.add_vline(x=days, line_dash="dash", line_color=color, annotation_text=f"{days} días")
            st.plotly_chart(figure, use_container_width=True)
        else:
            st.info("Sin lotes activos dentro de 180 días.")

    with tabs[1]:
        st.subheader("Ledger por lote")
        st.dataframe(ledger, use_container_width=True, hide_index=True, height=420)
        st.subheader("Simulador PEPS reversible")
        st.caption("Vista previa temporal. Solo se vuelve persistente al aplicarla como movimiento de salida.")
        materials = source["materials"]["material"].dropna().astype(str).tolist()
        with st.form("peps_simulation"):
            sim_material = st.selectbox("Material", materials)
            sim_quantity = st.number_input("Descarga simulada (kg)", min_value=0.0, step=10.0)
            sim_reason = st.text_input("Motivo del escenario", "Simulación de consumo adicional")
            preview = st.form_submit_button("Vista previa PEPS")
        if preview:
            st.session_state.peps_simulation = {
                "material": sim_material, "quantity": sim_quantity, "reason": sim_reason, "fecha": cutoff,
            }
            st.session_state.peps_applied = False
        simulation = st.session_state.get("peps_simulation")
        if simulation and simulation["quantity"] > 0:
            simulated_exits = pd.concat([base_exits, pd.DataFrame([{
                "pedimento": "SIMULACION", "clave": "SIM", "fecha": cutoff,
                "material": simulation["material"], "tipo": "Escenario", "cantidad_kg": simulation["quantity"],
                "observacion": simulation["reason"],
            }])], ignore_index=True)
            simulated = calculate_ledger({**source, "exits": simulated_exits, "baseline_oracles": False}, cutoff, exchange_rate, tolerance)
            st.dataframe(
                simulated["ledger"].loc[simulated["ledger"]["material"] == simulation["material"]],
                use_container_width=True, hide_index=True,
            )
            apply_col, undo_col, commit_col = st.columns(3)
            if apply_col.button("Aplicar al escenario", type="primary"):
                add_audit_event(
                    rfc, operator, "Aplicar simulación PEPS", simulation["reason"],
                    simulation["material"], None,
                    {"cantidad_kg": simulation["quantity"], "aplicado": True},
                )
                st.session_state.peps_applied = True
                st.rerun()
            if undo_col.button("Revertir escenario"):
                add_audit_event(
                    rfc, operator, "Revertir simulación PEPS", simulation["reason"],
                    simulation["material"],
                    {"cantidad_kg": simulation["quantity"], "aplicado": True},
                    {"cantidad_kg": simulation["quantity"], "aplicado": False},
                )
                st.session_state.peps_applied = False
                st.session_state.pop("peps_simulation", None)
                st.rerun()
            if commit_col.button("Registrar como salida real", disabled=not reason.strip()):
                add_exit({
                    "rfc": rfc, "pedimento": "SIMULACION", "clave": "SIM", "fecha": cutoff,
                    "material": simulation["material"], "tipo": "Salida registrada desde simulación",
                    "cantidad_kg": simulation["quantity"], "observacion": simulation["reason"],
                    "usuario": operator, "motivo": reason.strip(),
                })
                st.session_state.pop("peps_simulation", None)
                st.session_state.peps_applied = False
                st.rerun()

    with tabs[2]:
        st.subheader("Registrar entradas y salidas")
        materials = source["materials"]["material"].dropna().astype(str).tolist()
        entry_tab, exit_tab, import_tab = st.tabs(["Entrada / pedimento", "Salida", "Carga CSV"])
        with entry_tab:
            with st.form("register_entry", clear_on_submit=True):
                entry_pedimento = st.text_input("Número de pedimento")
                entry_key = st.text_input("Clave de operación", "IN")
                entry_date = st.date_input("Fecha de entrada", cutoff, key="entry_date")
                entry_material = st.selectbox("Material", materials, key="entry_material")
                entry_quantity = st.number_input("Cantidad importada (kg)", min_value=0.001, step=10.0)
                entry_unit_value = st.number_input("Valor unitario (USD/kg)", min_value=0.0, step=0.01, format="%.4f")
                save_entry = st.form_submit_button("Registrar pedimento", disabled=not reason.strip())
            if save_entry:
                if not entry_pedimento.strip():
                    st.error("El pedimento es obligatorio.")
                else:
                    add_entry({
                        "rfc": rfc, "pedimento": entry_pedimento.strip(), "clave": entry_key.strip(),
                        "fecha_entrada": entry_date, "material": entry_material, "cantidad_kg": entry_quantity,
                        "valor_unit_usd": entry_unit_value, "usuario": operator.strip() or "Operador IMMEX",
                        "motivo": reason.strip(),
                    })
                    st.success("Entrada guardada en SQLite y en bitácora.")
                    st.rerun()
        with exit_tab:
            with st.form("register_exit", clear_on_submit=True):
                exit_pedimento = st.text_input("Pedimento de salida / retorno")
                exit_key = st.text_input("Clave de operación", "RT")
                exit_date = st.date_input("Fecha de salida", cutoff, key="exit_date")
                exit_material = st.selectbox("Material", materials, key="exit_material")
                exit_type = st.selectbox("Tipo", ["Retorno", "Destrucción", "Cambio de régimen", "Transferencia", "Otro"])
                exit_quantity = st.number_input("Cantidad de salida (kg)", min_value=0.001, step=10.0, key="exit_quantity")
                exit_document = st.text_input("Documento o referencia")
                save_exit = st.form_submit_button("Registrar salida", disabled=not reason.strip())
            if save_exit:
                if not exit_pedimento.strip():
                    st.error("El pedimento de salida es obligatorio.")
                else:
                    add_exit({
                        "rfc": rfc, "pedimento": exit_pedimento.strip(), "clave": exit_key.strip(),
                        "fecha": exit_date, "material": exit_material, "tipo": exit_type,
                        "cantidad_kg": exit_quantity, "observacion": exit_document.strip(),
                        "usuario": operator.strip() or "Operador IMMEX", "motivo": reason.strip(),
                    })
                    st.success("Salida guardada en SQLite y en bitácora.")
                    st.rerun()
        with import_tab:
            record_kind = st.selectbox("Tipo de registros", ["Entradas", "Salidas"], key="csv_kind")
            csv_upload = st.file_uploader("Archivo CSV", type=["csv"], key="capture_csv")
            if csv_upload is not None and st.button("Importar movimientos", disabled=not reason.strip()):
                try:
                    rows = read_csv_rows(csv_upload, record_kind)
                    if any(row["cantidad_kg"] <= 0 for row in rows):
                        raise ValueError("Todas las cantidades deben ser positivas.")
                    for row in rows:
                        record = {**row, "rfc": rfc, "usuario": operator, "motivo": reason.strip()}
                        if record_kind == "Entradas":
                            add_entry(record)
                        else:
                            add_exit(record)
                    st.success(f"{len(rows)} movimientos guardados y auditados.")
                    st.rerun()
                except Exception as error:
                    st.error(f"No se importó el archivo: {error}")

    with tabs[3]:
        st.subheader("Excepciones y mermas")
        deficits = result["reconciliation"].loc[result["reconciliation"]["faltante_kg"] > 1e-9]
        if deficits.empty:
            st.success("No hay faltantes por conciliar.")
        else:
            for _, row in deficits.sort_values("faltante_kg", ascending=False).iterrows():
                with st.container(border=True):
                    st.markdown(f"**{row['material']} · {row['descripcion']}**")
                    st.write(
                        f"Faltan {fmt_kg(row['faltante_kg'])}; descarga requerida "
                        f"{fmt_kg(row['descarga_requerida_kg'])}, entradas "
                        f"{fmt_kg(row['importado_kg'])}."
                    )
                    for item in row["salidas_detalle"]:
                        st.caption(f"Salida {item['pedimento']} · {item['tipo']} · {fmt_kg(item['cantidad_kg'])} · {item['observacion']}")
        st.subheader("Merma sobre el umbral interno")
        over = result["merma"].loc[result["merma"]["merma_pct"] > tolerance].sort_values("merma_pct", ascending=False)
        if over.empty:
            st.success("No hay mermas por arriba de la tolerancia configurada.")
        else:
            st.dataframe(over, use_container_width=True, hide_index=True)
        st.subheader("Lotes con vencimiento próximo")
        alerts = active_lots.loc[active_lots["estatus"].isin({"VENCIDO", "ALERTA 30", "ALERTA 60", "ALERTA 90"})]
        st.dataframe(alerts, use_container_width=True, hide_index=True)

    with tabs[4]:
        st.subheader("Cierre mensual")
        period = st.text_input("Periodo", value=cutoff.strftime("%Y-%m"), key="close_month")
        timer_key = f"close_timer_{period}"
        started_at = get_setting(rfc, timer_key)
        clock_col, clock_button_col = st.columns([1, 2])
        if clock_button_col.button("Iniciar / reanudar reloj", disabled=readonly or not reason.strip()):
            if not started_at:
                set_setting(rfc, timer_key, datetime.now().astimezone().isoformat(timespec="seconds"),
                            operator, reason.strip())
            st.rerun()
        if started_at:
            render_close_clock(started_at)
        else:
            clock_col.metric("Tiempo del cierre", "--:--:--")
        close_labels = [
            "Cotejar entradas contra pedimentos y facturas",
            "Conciliar retornos, destrucciones, transferencias y cambios de régimen",
            "Revisar producción, BOM, mermas y faltantes PEPS",
            "Validar saldos por fracción y ventanas de vencimiento",
            "Exportar reportes, revisar bitácora y aprobar el cierre",
        ]
        completed = get_close_steps(rfc, period)
        done = sum(completed.get(index, False) for index in range(1, 6))
        st.progress(done / 5, text=f"{done} de 5 pasos completados")
        for index, label in enumerate(close_labels, start=1):
            checked = st.checkbox(
                label, value=completed.get(index, False), key=f"close_{rfc}_{period}_{index}",
                disabled=readonly or not reason.strip(),
            )
            if checked != completed.get(index, False):
                set_close_step(rfc, period, index, checked, operator, reason.strip())
                st.rerun()

    with tabs[5]:
        st.subheader("Reportes del Apartado A")
        report_name = st.selectbox("Reporte", ["Entradas", "Salidas", "Saldos por fracción", "Materiales Utilizados"])
        if report_name == "Entradas":
            report = source["entries"].copy()
        elif report_name == "Salidas":
            report = source["exits"].copy()
        elif report_name == "Saldos por fracción":
            report = balance_by_fraction(ledger)
        else:
            report = result["consumption"].copy()
        st.markdown(f"**{company}**  \nRFC: `{rfc}`")
        st.dataframe(report, use_container_width=True, hide_index=True)
        st.download_button(
            "Descargar CSV", data=csv_report(report_name, company, rfc, report),
            file_name=f"anexo24_{rfc}_{report_name.lower().replace(' ', '_')}_{cutoff.isoformat()}.csv",
            mime="text/csv", icon=":material/download:",
        )

    with tabs[6]:
        st.subheader("Bitácora automática")
        events = pd.DataFrame(get_audit_events(rfc))
        if events.empty:
            st.info("Las altas y cambios de parámetros aparecerán aquí automáticamente.")
        else:
            events = events.rename(columns={
                "created_at": "Fecha y hora", "usuario": "Usuario", "accion": "Acción",
                "motivo": "Motivo", "referencia": "Referencia",
                "valor_anterior": "Valor anterior", "valor_nuevo": "Valor nuevo",
            })
            st.dataframe(events, use_container_width=True, hide_index=True)

    with tabs[7]:
        st.subheader("Simulador de Impacto")
        st.caption("Define un escenario hipotético y visualiza su efecto sobre la exposición, vencimientos y faltantes. Los cambios no se guardan.")

        sim_materials = source["materials"]["material"].dropna().astype(str).tolist()
        if "impact_exits" not in st.session_state:
            st.session_state.impact_exits = []
        if "impact_entries" not in st.session_state:
            st.session_state.impact_entries = []

        st.markdown("#### Parámetros generales")
        hyp_col1, hyp_col2 = st.columns(2)
        with hyp_col1:
            hyp_fx = st.number_input("Tipo de cambio hipotético (MXN/USD)", value=exchange_rate, step=0.5, format="%.4f", key="hyp_fx")
        with hyp_col2:
            hyp_cutoff = st.date_input("Fecha de corte hipotética", value=cutoff, key="hyp_cutoff")

        st.markdown("#### Salidas hipotéticas (retornos, exportaciones, cambios de régimen)")
        with st.form("add_hyp_exit", clear_on_submit=True):
            hx_col1, hx_col2, hx_col3 = st.columns([2, 1, 2])
            hx_material = hx_col1.selectbox("Material", sim_materials, key="hx_mat")
            hx_qty = hx_col2.number_input("Cantidad (kg)", min_value=0.001, step=10.0, key="hx_qty")
            hx_tipo = hx_col3.selectbox("Tipo", ["Retorno", "Exportación", "Cambio de régimen", "Destrucción", "Transferencia"], key="hx_tipo")
            if st.form_submit_button("➕ Agregar salida") and hx_qty > 0:
                st.session_state.impact_exits.append({"material": hx_material, "cantidad_kg": hx_qty, "tipo": hx_tipo})
                st.rerun()
        if st.session_state.impact_exits:
            st.dataframe(pd.DataFrame(st.session_state.impact_exits), use_container_width=True, hide_index=True)
            if st.button("🗑 Limpiar salidas", key="clear_hyp_exits"):
                st.session_state.impact_exits = []
                st.rerun()

        st.markdown("#### Entradas hipotéticas (nuevas importaciones)")
        with st.form("add_hyp_entry", clear_on_submit=True):
            he_col1, he_col2, he_col3 = st.columns([2, 1, 1])
            he_material = he_col1.selectbox("Material", sim_materials, key="he_mat")
            he_qty = he_col2.number_input("Cantidad (kg)", min_value=0.001, step=10.0, key="he_qty")
            he_val = he_col3.number_input("Valor unit. (USD/kg)", min_value=0.0, step=0.01, format="%.4f", key="he_val")
            if st.form_submit_button("➕ Agregar entrada") and he_qty > 0:
                st.session_state.impact_entries.append({"material": he_material, "cantidad_kg": he_qty, "valor_unit_usd": he_val})
                st.rerun()
        if st.session_state.impact_entries:
            st.dataframe(pd.DataFrame(st.session_state.impact_entries), use_container_width=True, hide_index=True)
            if st.button("🗑 Limpiar entradas", key="clear_hyp_entries"):
                st.session_state.impact_entries = []
                st.rerun()

        run_impact = st.button("⚡ Calcular impacto del escenario", type="primary")
        if run_impact:
            hyp_exits = base_exits.copy()
            for i, ex in enumerate(st.session_state.impact_exits):
                hyp_exits = pd.concat([hyp_exits, pd.DataFrame([{
                    "pedimento": f"HIPOT-S{i+1:02d}", "clave": "HIP", "fecha": hyp_cutoff,
                    "material": ex["material"], "tipo": ex["tipo"],
                    "cantidad_kg": ex["cantidad_kg"], "observacion": "Escenario hipotético",
                }])], ignore_index=True)
            hyp_entries = source["entries"].copy()
            for i, en in enumerate(st.session_state.impact_entries):
                hyp_entries = pd.concat([hyp_entries, pd.DataFrame([{
                    "pedimento": f"HIPOT-E{i+1:02d}", "clave": "HIP", "fecha_entrada": hyp_cutoff,
                    "material": en["material"], "cantidad_kg": en["cantidad_kg"],
                    "valor_unit_usd": en["valor_unit_usd"],
                }])], ignore_index=True)
            hyp_source = {**source, "entries": hyp_entries, "exits": hyp_exits, "baseline_oracles": False}
            hyp_result = calculate_ledger(hyp_source, hyp_cutoff, hyp_fx, tolerance)
            st.session_state.impact_result = hyp_result
            st.session_state.impact_params = {"fx": hyp_fx, "cutoff": hyp_cutoff}
            st.session_state.pop("impact_narrative", None)

        if "impact_result" in st.session_state:
            hyp_result = st.session_state.impact_result
            hyp_metrics = hyp_result["metrics"]
            delta_exposure = hyp_metrics["saldo_total_mxn"] - metrics["saldo_total_mxn"]
            delta_risk = hyp_metrics["riesgo_alertas_mxn"] - metrics["riesgo_alertas_mxn"]

            st.markdown("---")
            st.markdown("#### Comparación: Actual vs. Escenario")
            comp_cols = st.columns(4)
            for col, label, base_val, hyp_val, fmt_fn in zip(
                comp_cols,
                ["Exposición total", "Riesgo en alertas", "Pedimentos en alerta", "Faltante"],
                [metrics["saldo_total_mxn"], metrics["riesgo_alertas_mxn"], metrics["pedimentos_alerta"], metrics["faltante_total_kg"]],
                [hyp_metrics["saldo_total_mxn"], hyp_metrics["riesgo_alertas_mxn"], hyp_metrics["pedimentos_alerta"], hyp_metrics["faltante_total_kg"]],
                [money, money, str, fmt_kg],
            ):
                delta = hyp_val - base_val
                col.metric(label, fmt_fn(hyp_val), delta=f"{'+' if delta >= 0 else ''}{fmt_fn(delta)}", delta_color="inverse")

            comparison_df = pd.DataFrame({
                "Métrica": ["Exposición total (MXN)", "Riesgo en alertas (MXN)", "Pedimentos en alerta", "Faltante (kg)"],
                "Actual": [metrics["saldo_total_mxn"], metrics["riesgo_alertas_mxn"], float(metrics["pedimentos_alerta"]), metrics["faltante_total_kg"]],
                "Escenario": [hyp_metrics["saldo_total_mxn"], hyp_metrics["riesgo_alertas_mxn"], float(hyp_metrics["pedimentos_alerta"]), hyp_metrics["faltante_total_kg"]],
            })
            fig_impact = px.bar(
                comparison_df.melt(id_vars="Métrica", value_vars=["Actual", "Escenario"]),
                x="Métrica", y="value", color="variable", barmode="group",
                color_discrete_map={"Actual": "#8297a5", "Escenario": "#d89b61"},
                labels={"value": "Valor", "variable": ""},
                title="Impacto del escenario sobre las métricas clave",
            )
            fig_impact.update_layout(height=340)
            st.plotly_chart(fig_impact, use_container_width=True)

            st.markdown("#### Lotes con cambio de estatus")
            hyp_ledger = hyp_result["ledger"]
            merged_ledger = ledger[["pedimento", "material", "estatus", "saldo_kg", "riesgo_mxn"]].merge(
                hyp_ledger[["pedimento", "material", "estatus", "saldo_kg", "riesgo_mxn"]],
                on=["pedimento", "material"], suffixes=("_actual", "_escenario"), how="outer",
            )
            changed_lots = merged_ledger[merged_ledger["estatus_actual"] != merged_ledger["estatus_escenario"]].copy()
            if not changed_lots.empty:
                st.dataframe(changed_lots, use_container_width=True, hide_index=True)
            else:
                st.success("Ningún lote cambia de estatus con este escenario.")

            st.markdown("#### Análisis narrativo")
            if "impact_narrative" not in st.session_state:
                if gemini_api_key():
                    try:
                        hyp_context = {
                            "tipo_cambio_hipotetico": st.session_state.impact_params["fx"],
                            "corte_hipotetico": str(st.session_state.impact_params["cutoff"]),
                            "salidas_hipoteticas": st.session_state.impact_exits,
                            "entradas_hipoteticas": st.session_state.impact_entries,
                            "metricas_hipoteticas": {
                                "saldo_total_mxn": round(hyp_metrics["saldo_total_mxn"], 2),
                                "riesgo_alertas_mxn": round(hyp_metrics["riesgo_alertas_mxn"], 2),
                                "pedimentos_alerta": hyp_metrics["pedimentos_alerta"],
                                "faltante_total_kg": round(hyp_metrics["faltante_total_kg"], 2),
                            },
                        }
                        prompt = (
                            "Analiza este escenario hipotético de inventario IMMEX Anexo 24. "
                            "Situación actual: " + json.dumps(app_context(), ensure_ascii=False, default=str) +
                            "\nEscenario hipotético: " + json.dumps(hyp_context, ensure_ascii=False) +
                            "\n\nExplica en 3-4 párrafos breves: 1) qué cambiaría en el inventario, "
                            "2) el impacto financiero principal, 3) riesgos u oportunidades que abre este escenario, "
                            "4) acción recomendada. No inventes normas ni valores que no estén en el contexto."
                        )
                        narrative = ask_gemini(prompt, system_instruction=(
                            "Eres IA IMMEX, copiloto del Control Anexo 24. Responde en español, de forma amable y breve. "
                            "Distingue hechos calculados de hipótesis. No des asesoría legal definitiva."
                        ))
                    except Exception:
                        narrative = _local_impact_narrative(delta_exposure, delta_risk, hyp_metrics, metrics)
                else:
                    narrative = _local_impact_narrative(delta_exposure, delta_risk, hyp_metrics, metrics)
                st.session_state.impact_narrative = narrative
            st.markdown(st.session_state.impact_narrative)

    with tabs[8]:
        st.subheader("Clasificador Arancelario")
        st.caption(
            "Describe el producto o material y el asistente sugiere la fracción arancelaria TIGIE. "
            "No sustituye una opinión aduanera formal ni tiene efectos legales."
        )

        if "tariff_history" not in st.session_state:
            st.session_state.tariff_history = []

        existing_fractions = (
            source["materials"][["material", "descripcion", "fraccion"]]
            .dropna(subset=["fraccion"])
            .query("fraccion != ''")
            .to_dict("records")
        )

        with st.form("tariff_classifier", clear_on_submit=False):
            tc_description = st.text_area(
                "Descripción del producto / material",
                placeholder="Ej: Placa de acero inoxidable 304 laminada en frío, espesor 2 mm, para uso en manufactura de equipos médicos",
                height=100,
            )
            tc_col1, tc_col2 = st.columns(2)
            tc_origin = tc_col1.text_input("País de origen (opcional)", placeholder="México, China, EUA…")
            tc_use = tc_col2.text_input("Uso o destino (opcional)", placeholder="Componente para ensamble, materia prima…")
            tc_submit = st.form_submit_button("🔍 Clasificar", type="primary")

        if tc_submit and tc_description.strip():
            with st.spinner("Analizando clasificación arancelaria…"):
                materials_context = "\n".join(
                    f"- {row['material']}: {row['descripcion']} → fracción {row['fraccion']}"
                    for row in existing_fractions
                ) or "Sin materiales clasificados en el perfil actual."

                if gemini_api_key():
                    try:
                        tariff_prompt = (
                            f"Clasifica arancelariamente el siguiente producto según la TIGIE (Tarifa de los Impuestos "
                            f"Generales de Exportación e Importación de México, vigente 2024-2025) y el Anexo 24 RGCE 2026.\n\n"
                            f"PRODUCTO: {tc_description.strip()}\n"
                            + (f"PAÍS DE ORIGEN: {tc_origin.strip()}\n" if tc_origin.strip() else "")
                            + (f"USO / DESTINO: {tc_use.strip()}\n" if tc_use.strip() else "")
                            + f"\nMATERIALES YA CLASIFICADOS EN ESTE PERFIL IMMEX (para referencia):\n{materials_context}\n\n"
                            "Responde en este formato exacto:\n"
                            "**Fracción sugerida:** [10 dígitos, ej: 7208.51.01]\n"
                            "**Descripción TIGIE:** [descripción oficial de la partida/subpartida]\n"
                            "**Arancel general (Ad Valorem):** [porcentaje o 'Exento']\n"
                            "**TLCs con tasa preferencial:** [lista de tratados o 'Verificar en VUCEM']\n"
                            "**Plazo legal IMMEX:** [meses según Anexo 24, típicamente 18 o 36]\n"
                            "**Notas de clasificación:** [reglas de clasificación aplicables, notas de sección/capítulo relevantes]\n"
                            "**Nivel de confianza:** [Alto / Medio / Bajo] — [razón breve]\n\n"
                            "Si la clasificación es ambigua, menciona las fracciones alternativas y la razón. "
                            "No afirmes certeza legal; distingue claramente la sugerencia del dictamen formal."
                        )
                        tariff_result = ask_gemini(tariff_prompt, system_instruction=(
                            "Eres un experto en clasificación arancelaria mexicana (TIGIE, RGCE, Reglas de Clasificación). "
                            "Responde en español. Sé preciso con los dígitos de la fracción. "
                            "Distingue siempre entre sugerencia técnica y opinión aduanera formal."
                        ))
                        result_source = "Gemini"
                    except Exception as err:
                        tariff_result = None
                        result_source = f"error: {err}"
                else:
                    tariff_result = None
                    result_source = "local"

                if tariff_result is None:
                    desc_lower = tc_description.lower()
                    matches = [
                        row for row in existing_fractions
                        if any(word in desc_lower for word in row["descripcion"].lower().split() if len(word) > 3)
                    ]
                    if matches:
                        tariff_result = (
                            "**Modo local (sin Gemini) — coincidencias en el perfil actual:**\n\n"
                            + "\n".join(
                                f"- **{row['material']}** ({row['descripcion']}): fracción `{row['fraccion']}`"
                                for row in matches[:5]
                            )
                            + "\n\n_Activa Gemini para una clasificación completa basada en la TIGIE._"
                        )
                    else:
                        tariff_result = (
                            "**Modo local (sin Gemini).**\n\n"
                            "No se encontraron materiales similares en el perfil. "
                            "Configura `GEMINI_API_KEY` para obtener una clasificación TIGIE detallada."
                        )

                entry = {
                    "descripcion": tc_description.strip(),
                    "origen": tc_origin.strip() or "—",
                    "uso": tc_use.strip() or "—",
                    "resultado": tariff_result,
                    "fuente": result_source,
                }
                st.session_state.tariff_history.insert(0, entry)

        if st.session_state.tariff_history:
            for i, item in enumerate(st.session_state.tariff_history):
                with st.container(border=True):
                    header_col, badge_col = st.columns([4, 1])
                    header_col.markdown(f"**{item['descripcion'][:80]}{'…' if len(item['descripcion']) > 80 else ''}**")
                    badge_col.caption(f"vía {item['fuente']}")
                    if item["origen"] != "—" or item["uso"] != "—":
                        st.caption(f"Origen: {item['origen']} · Uso: {item['uso']}")
                    st.markdown(item["resultado"])
                    if i == 0 and gemini_api_key() and existing_fractions:
                        if st.button("📋 Copiar fracción al catálogo de materiales", key=f"copy_tariff_{i}"):
                            st.info("Registra la fracción manualmente en el catálogo desde Captura operativa → Entrada / pedimento.")

            if st.button("🗑 Limpiar historial de consultas", key="clear_tariff"):
                st.session_state.tariff_history = []
                st.rerun()

        if not st.session_state.tariff_history:
            st.info("Ingresa una descripción de producto para iniciar la clasificación.")
            if existing_fractions:
                with st.expander("Ver fracciones ya registradas en este perfil"):
                    st.dataframe(
                        pd.DataFrame(existing_fractions).rename(columns={
                            "material": "Clave", "descripcion": "Descripción", "fraccion": "Fracción arancelaria"
                        }),
                        use_container_width=True, hide_index=True,
                    )

st.divider()
st.caption(
    f"{company} · RFC {rfc} · Corte {cutoff:%d/%m/%Y} · Tipo de cambio {exchange_rate:,.4f} MXN/USD · "
    f"Tolerancia interna {tolerance:.2%} · Fuente {profile_name} · Persistencia SQLite"
)