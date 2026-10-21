from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any


DB_PATH = Path(__file__).parent / "data" / "anexo24.sqlite3"


def _connect(path: str | Path = DB_PATH) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _json(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, default=lambda item: item.isoformat() if isinstance(item, (date, datetime)) else str(item))


def initialize_database(path: str | Path = DB_PATH) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with _connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rfc TEXT NOT NULL,
                pedimento TEXT NOT NULL,
                clave TEXT NOT NULL,
                fecha_entrada TEXT NOT NULL,
                material TEXT NOT NULL,
                cantidad_kg REAL NOT NULL CHECK (cantidad_kg > 0),
                valor_unit_usd REAL NOT NULL CHECK (valor_unit_usd >= 0),
                usuario TEXT NOT NULL,
                motivo TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS exits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rfc TEXT NOT NULL,
                pedimento TEXT NOT NULL,
                clave TEXT NOT NULL,
                fecha TEXT NOT NULL,
                material TEXT NOT NULL,
                tipo TEXT NOT NULL,
                cantidad_kg REAL NOT NULL CHECK (cantidad_kg > 0),
                observacion TEXT NOT NULL,
                usuario TEXT NOT NULL,
                motivo TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rfc TEXT NOT NULL,
                created_at TEXT NOT NULL,
                usuario TEXT NOT NULL,
                accion TEXT NOT NULL,
                motivo TEXT NOT NULL,
                referencia TEXT NOT NULL,
                valor_anterior TEXT,
                valor_nuevo TEXT
            );
            CREATE TABLE IF NOT EXISTS settings (
                rfc TEXT NOT NULL,
                clave TEXT NOT NULL,
                valor TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (rfc, clave)
            );
            CREATE TABLE IF NOT EXISTS close_steps (
                rfc TEXT NOT NULL,
                periodo TEXT NOT NULL,
                paso INTEGER NOT NULL,
                completado INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (rfc, periodo, paso)
            );
            """
        )


def _insert_audit(
    connection: sqlite3.Connection,
    rfc: str,
    usuario: str,
    accion: str,
    motivo: str,
    referencia: str,
    anterior: Any = None,
    nuevo: Any = None,
) -> None:
    connection.execute(
        """INSERT INTO audit_events
        (rfc, created_at, usuario, accion, motivo, referencia, valor_anterior, valor_nuevo)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (rfc, datetime.now().astimezone().isoformat(timespec="seconds"), usuario, accion,
         motivo, referencia, _json(anterior), _json(nuevo)),
    )


def add_entry(record: dict[str, Any], path: str | Path = DB_PATH) -> int:
    with _connect(path) as connection:
        cursor = connection.execute(
            """INSERT INTO entries
            (rfc, pedimento, clave, fecha_entrada, material, cantidad_kg, valor_unit_usd, usuario, motivo, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (record["rfc"], record["pedimento"], record["clave"], record["fecha_entrada"].isoformat(),
             record["material"], record["cantidad_kg"], record["valor_unit_usd"], record["usuario"],
             record["motivo"], datetime.now().astimezone().isoformat(timespec="seconds")),
        )
        row_id = int(cursor.lastrowid)
        _insert_audit(connection, record["rfc"], record["usuario"], "Alta de entrada", record["motivo"],
                      f"entrada:{row_id}", None, record)
        return row_id


def add_exit(record: dict[str, Any], path: str | Path = DB_PATH) -> int:
    with _connect(path) as connection:
        cursor = connection.execute(
            """INSERT INTO exits
            (rfc, pedimento, clave, fecha, material, tipo, cantidad_kg, observacion, usuario, motivo, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (record["rfc"], record["pedimento"], record["clave"], record["fecha"].isoformat(),
             record["material"], record["tipo"], record["cantidad_kg"], record["observacion"],
             record["usuario"], record["motivo"], datetime.now().astimezone().isoformat(timespec="seconds")),
        )
        row_id = int(cursor.lastrowid)
        _insert_audit(connection, record["rfc"], record["usuario"], "Alta de salida", record["motivo"],
                      f"salida:{row_id}", None, record)
        return row_id


def get_captures(rfc: str, path: str | Path = DB_PATH) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    with _connect(path) as connection:
        entries = [dict(row) for row in connection.execute("SELECT * FROM entries WHERE rfc = ? ORDER BY id", (rfc,))]
        exits = [dict(row) for row in connection.execute("SELECT * FROM exits WHERE rfc = ? ORDER BY id", (rfc,))]
    for row in entries:
        row["fecha_entrada"] = date.fromisoformat(row["fecha_entrada"])
        row["lote_id"] = f"db-entry-{row['id']}"
    for row in exits:
        row["fecha"] = date.fromisoformat(row["fecha"])
    return entries, exits


def get_setting(rfc: str, key: str, default: Any = None, path: str | Path = DB_PATH) -> Any:
    with _connect(path) as connection:
        row = connection.execute("SELECT valor FROM settings WHERE rfc = ? AND clave = ?", (rfc, key)).fetchone()
    return json.loads(row["valor"]) if row else default


def set_setting(
    rfc: str, key: str, value: Any, usuario: str, motivo: str,
    path: str | Path = DB_PATH,
) -> bool:
    with _connect(path) as connection:
        row = connection.execute("SELECT valor FROM settings WHERE rfc = ? AND clave = ?", (rfc, key)).fetchone()
        previous = json.loads(row["valor"]) if row else None
        if previous == value:
            return False
        connection.execute(
            "INSERT INTO settings (rfc, clave, valor, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(rfc, clave) DO UPDATE SET valor=excluded.valor, updated_at=excluded.updated_at",
            (rfc, key, _json(value), datetime.now().astimezone().isoformat(timespec="seconds")),
        )
        _insert_audit(connection, rfc, usuario, "Cambio de parámetro", motivo,
                      key, previous, value)
    return True


def add_audit_event(
    rfc: str, usuario: str, accion: str, motivo: str, referencia: str,
    anterior: Any = None, nuevo: Any = None, path: str | Path = DB_PATH,
) -> None:
    with _connect(path) as connection:
        _insert_audit(connection, rfc, usuario, accion, motivo, referencia, anterior, nuevo)


def get_audit_events(rfc: str, path: str | Path = DB_PATH) -> list[dict[str, Any]]:
    with _connect(path) as connection:
        rows = connection.execute(
            "SELECT * FROM audit_events WHERE rfc = ? ORDER BY id DESC", (rfc,)
        ).fetchall()
    return [dict(row) for row in rows]


def get_close_steps(rfc: str, periodo: str, path: str | Path = DB_PATH) -> dict[int, bool]:
    with _connect(path) as connection:
        rows = connection.execute(
            "SELECT paso, completado FROM close_steps WHERE rfc = ? AND periodo = ?",
            (rfc, periodo),
        ).fetchall()
    return {int(row["paso"]): bool(row["completado"]) for row in rows}


def set_close_step(
    rfc: str, periodo: str, paso: int, completado: bool, usuario: str, motivo: str,
    path: str | Path = DB_PATH,
) -> None:
    with _connect(path) as connection:
        row = connection.execute(
            "SELECT completado FROM close_steps WHERE rfc = ? AND periodo = ? AND paso = ?",
            (rfc, periodo, paso),
        ).fetchone()
        previous = bool(row["completado"]) if row else False
        if previous == completado:
            return
        connection.execute(
            "INSERT INTO close_steps (rfc, periodo, paso, completado) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(rfc, periodo, paso) DO UPDATE SET completado=excluded.completado",
            (rfc, periodo, paso, int(completado)),
        )
        _insert_audit(connection, rfc, usuario, "Avance de cierre mensual", motivo,
                      f"{periodo}:paso:{paso}", previous, completado)