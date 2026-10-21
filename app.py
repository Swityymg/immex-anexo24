from __future__ import annotations

import hashlib
import os
import re
from datetime import date, datetime
from io import StringIO
from pathlib import Path
from typing import Any

import pandas as pd
import plotly.express as px
import streamlit as st

from ledger import DEFAULT_WORKBOOK, calculate_ledger, load_source_data
from persistence import (
    DB_PATH, add_audit_event, add_entry, add_exit, get_audit_events, get_captures,
    get_close_steps, get_setting, initialize_database, set_close_step, set_setting,
)

st.set_page_config(page_title="IMMEX | Control Anexo 24", page_icon="📦", layout="wide")
initialize_database()
PROFILE_DIR = DEFAULT_WORKBOOK.parent / "profiles"


@st.cache_data(show_spinner=False)
def load_data(path: str) -> dict[str, Any]:
    return load_source_data(path)


def money(value: float) -> str:
    return f"${value:,.2f} MXN"


def fmt_kg(value: float) -> str:
    return f"{value:,.2f} kg"


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


def dynamic_analysis(result: dict[str, Any], cutoff: date, tolerance: float) -> str:
    output = ["### Análisis de excepciones", ""]
    count = 0
    shortages = result["reconciliation"].loc[result["reconciliation"]["faltante_kg"] > 0]
    for _, row in shortages.sort_values("faltante_kg", ascending=False).iterrows():
        count += 1
        output.append(
            f"**Faltante de {row['material']} ({row['descripcion']}): {fmt_kg(row['faltante_kg'])}.** "
            f"Requerido {fmt_kg(row['descarga_requerida_kg'])}; disponible de entradas {fmt_kg(row['importado_kg'])}."
        )
        for exit_row in row["salidas_detalle"]:
            output.append(f"- Salida {exit_row['pedimento']} · {exit_row['tipo']} · {fmt_kg(exit_row['cantidad_kg'])} · {exit_row['observacion']}")
        output.append("Acción: validar acuse, evidencia física, lote y documento origen antes de ajustar.")
        output.append("")
    excess = result["merma"].loc[result["merma"]["merma_pct"] > tolerance]
    for _, row in excess.sort_values("merma_pct", ascending=False).iterrows():
        count += 1
        output.append(
            f"**Merma por arriba del umbral interno: {row['producto']} / {row['material']}, "
            f"{row['merma_pct']:.2%} frente a {tolerance:.2%}.** Merma contabilizada: {fmt_kg(row['merma_kg'])}."
        )
        output.append("Acción: revisar orden, turno, parámetros del proceso, báscula y destino del desperdicio.")
        output.append("")
    active = result["ledger"].loc[
        (result["ledger"]["saldo_kg"] > 0) &
        result["ledger"]["estatus"].isin({"VENCIDO", "ALERTA 30", "ALERTA 60", "ALERTA 90"})
    ].sort_values("dias_restantes")
    for _, row in active.iterrows():
        count += 1
        output.append(
            f"**Lote próximo a vencer: {row['pedimento']} / {row['material']} / fracción {row['fraccion']}.** "
            f"{fmt_kg(row['saldo_kg'])}; vence {row['vencimiento']:%d/%m/%Y}, "
            f"{row['dias_restantes']} días al corte {cutoff:%d/%m/%Y}. "
            "Acción: evaluar retorno/exportación, transferencia o cambio de régimen con asesoría aduanera."
        )
        output.append("")
    if count == 0:
        output.append("No se detectaron faltantes, mermas por arriba del umbral ni lotes en alertas.")
    output.extend([
        "### Plan de respuesta",
        "1. Conciliar cada diferencia con documentos fuente y su responsable operativo.",
        "2. Registrar correcciones solo contra evidencia aprobada, manteniendo el rastro de auditoría.",
        "3. Calendarizar retorno o regularización de los lotes próximos a vencer.",
        "",
        "Análisis para priorización; no es una opinión legal ni determina por sí mismo una infracción.",
    ])
    return "\n".join(output)


@st.fragment(run_every="1s")
def render_close_clock(started_at: str) -> None:
    elapsed = max(0, int((datetime.now().astimezone() - datetime.fromisoformat(started_at)).total_seconds()))
    st.metric("Tiempo del cierre", f"{elapsed // 3600:02d}:{elapsed // 60 % 60:02d}:{elapsed % 60:02d}")


st.title("Control de inventarios IMMEX")
st.caption("ANEXO 24 · PEPS estricto · Exposición y conciliación operativa")

with st.sidebar:
    st.header("Perfil y sesión")
    role = st.radio("Vista", ["Operación", "Gerencia", "Auditor (solo lectura)"])
    readonly = role == "Auditor (solo lectura)"
    operator = st.text_input("Usuario / responsable", "Auditor" if readonly else "Operador IMMEX", disabled=readonly)
    reason = st.text_input("Motivo", "Consulta de auditoría" if readonly else "")
    if not readonly and not reason.strip():
        st.caption("Indica el motivo para habilitar cambios y altas auditables.")
    if not readonly:
        uploaded_profile = st.file_uploader("Agregar perfil Excel", type=["xlsx"])
        if uploaded_profile is not None:
            digest = hashlib.sha256(uploaded_profile.getvalue()).hexdigest()
            if st.session_state.get("profile_digest") != digest:
                PROFILE_DIR.mkdir(parents=True, exist_ok=True)
                name = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(uploaded_profile.name).stem)
                path = PROFILE_DIR / f"{name}-{datetime.now():%Y%m%d%H%M%S}.xlsx"
                path.write_bytes(uploaded_profile.getvalue())
                st.session_state.profile_digest = digest
                st.rerun()
    profile_paths = [DEFAULT_WORKBOOK, *sorted((PROFILE_DIR.glob("*.xlsx") if PROFILE_DIR.exists() else []))]
    labels = {path.name: path for path in profile_paths}
    profile_name = st.selectbox("Perfil / RFC", list(labels))
    profile_path = labels[profile_name]

try:
    source = load_data(str(profile_path))
except Exception as error:
    st.error(f"No se pudo leer el perfil: {error}")
    st.stop()

params = source["params"]
company = str(params.get("Razón social", profile_path.stem))
rfc = str(params.get("RFC", profile_path.stem))
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
cols = st.columns(4)
cols[0].metric("Exposición total", money(metrics["saldo_total_mxn"]))
cols[1].metric("En ventanas de alerta", money(metrics["riesgo_alertas_mxn"]))
cols[2].metric("Pedimentos por atender", str(metrics["pedimentos_alerta"]))
cols[3].metric("Faltante por conciliar", fmt_kg(metrics["faltante_total_kg"]))
if first_due is not None:
    urgency = "vencido" if first_due["dias_restantes"] < 0 else "vence en"
    st.warning(f"Primer vencimiento: {first_due['pedimento']} · {first_due['material']} · {urgency} {abs(int(first_due['dias_restantes']))} días ({first_due['vencimiento']:%d/%m/%Y}).")

if readonly:
    tabs = st.tabs(["Exposición", "Ledger", "Reporte por fracción", "Bitácora"])
else:
    tabs = st.tabs([
        "Inicio Gerencial", "Ledger y Simulador PEPS", "Captura operativa", "Excepciones y Mermas",
        "Asistente IA", "Cierre mensual", "Reportes Anexo 24", "Bitácora",
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
        st.subheader("Asistente de auditoría")
        st.caption("Configura ANTHROPIC_API_KEY como variable de entorno o secreto de Streamlit; la clave no se solicita en pantalla.")
        if st.button("Analizar todas las excepciones", type="primary"):
            analysis = dynamic_analysis(result, cutoff, tolerance)
            api_key = os.getenv("ANTHROPIC_API_KEY")
            try:
                api_key = api_key or st.secrets.get("ANTHROPIC_API_KEY")
            except Exception:
                pass
            if api_key:
                try:
                    import anthropic

                    model = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")
                    client = anthropic.Anthropic(api_key=api_key)
                    with st.spinner("Analizando las excepciones de este perfil..."):
                        response = client.messages.create(
                            model=model, max_tokens=2400,
                            messages=[{
                                "role": "user",
                                "content": (
                                    "Eres un asistente operativo IMMEX. Explica cada excepción entregada, distingue hechos de hipótesis, "
                                    "no inventes disposiciones legales ni afirmes infracciones, y plantea acciones con evidencia y responsable. "
                                    "Considera todo el perfil y todas las partidas.\n\n" + analysis
                                ),
                            }],
                        )
                    answer = "\n".join(block.text for block in response.content if getattr(block, "type", "") == "text")
                    st.markdown(answer or analysis)
                    add_audit_event(rfc, operator, "Asistente IA", "Análisis completo de excepciones", profile_name,
                                    None, {"modelo": model})
                except Exception as error:
                    st.error(f"La llamada a Anthropic falló ({type(error).__name__}): {error}")
                    st.markdown("**Análisis local completo, no respuesta generada por IA:**")
                    st.markdown(analysis)
            else:
                st.info("No hay credencial configurada. Se muestra el análisis determinista local de todas las excepciones.")
                st.markdown(analysis)

    with tabs[5]:
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

    with tabs[6]:
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

    with tabs[7]:
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

st.divider()
st.caption(
    f"{company} · RFC {rfc} · Corte {cutoff:%d/%m/%Y} · Tipo de cambio {exchange_rate:,.4f} MXN/USD · "
    f"Tolerancia interna {tolerance:.2%} · Fuente {profile_name} · Persistencia SQLite"
)