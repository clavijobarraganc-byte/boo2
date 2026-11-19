"""API externa de solo lectura: catálogo clínico y disponibilidad de agenda.

Autenticación: cabecera `X-API-Key` comparada con la variable de entorno
`EXTERNAL_API_KEY`. Si la variable no está definida, la API responde 503 en
lugar de arrancar con una clave por defecto conocida.

No expone datos personales de pacientes: solo médicos, consultorios,
tratamientos y franjas horarias ocupadas o libres.
"""

import hmac
import os
from datetime import date, datetime, time, timedelta

import pymysql
from flask import Blueprint, jsonify, request

from db import connect_to_db, module_response

api_externa = Blueprint("api_externa", __name__, url_prefix="/api/externa")

# (columnas expuestas, columnas donde busca ?q=)
CATALOGS = {
    "medicos": (("id", "nombre", "especialidad", "telefono"), ("nombre", "especialidad")),
    "consultorios": (("id", "nombre", "ubicacion", "telefono", "estado"), ("nombre", "ubicacion")),
    "tratamientos": (("id", "nombre", "descripcion", "duracion", "costo"), ("nombre", "descripcion")),
}

OPENING_TIME = time(9, 0)
CLOSING_TIME = time(18, 0)
SLOT_STEP = timedelta(minutes=30)
MAX_RESULTS = 200


@api_externa.before_request
def require_api_key():
    expected = os.environ.get("EXTERNAL_API_KEY", "")
    if not expected:
        return jsonify(error="La API externa no está configurada: falta EXTERNAL_API_KEY."), 503
    supplied = request.headers.get("X-API-Key", "")
    if not supplied or not hmac.compare_digest(supplied, expected):
        return jsonify(error="API key ausente o inválida."), 401
    return None


@api_externa.get("")
@api_externa.get("/")
def describe():
    return jsonify(
        api="Clínica Camilo's · API externa",
        version="1.0",
        auth="Cabecera X-API-Key",
        solo_lectura=True,
        endpoints={
            "GET /api/externa/medicos": "Catálogo de médicos (sin correo electrónico)",
            "GET /api/externa/consultorios": "Consultorios y su estado",
            "GET /api/externa/tratamientos": "Tratamientos, duración y costo",
            "GET /api/externa/disponibilidad": "Franjas libres/ocupadas de una fecha",
        },
        parametros="Todas las rutas de catálogo aceptan ?q= para búsqueda de texto. "
                   "Disponibilidad acepta ?fecha=YYYY-MM-DD, ?medico= y ?consultorio=.",
    )


def _catalog(table, table_columns, search_columns):
    query = request.args.get("q", "").strip()[:80]
    columns = ", ".join(f"`{column}`" for column in table_columns)
    sql = f"SELECT {columns} FROM `{table}`"
    parameters = ()
    if query:
        sql += " WHERE " + " OR ".join(f"`{column}` LIKE %s" for column in search_columns)
        parameters = tuple(f"%{query}%" for _ in search_columns)
    sql += " ORDER BY `nombre` LIMIT %s"
    parameters += (MAX_RESULTS,)

    connection = None
    try:
        connection = connect_to_db()
        with connection.cursor() as cursor:
            cursor.execute(sql, parameters)
            rows = cursor.fetchall()
    except pymysql.MySQLError:
        return jsonify(error="Servicio temporalmente no disponible."), 503
    finally:
        if connection:
            connection.close()
    return jsonify(items=[module_response(row) for row in rows], total=len(rows))


@api_externa.get("/medicos")
def list_medicos():
    return _catalog("medicos", *CATALOGS["medicos"])


@api_externa.get("/consultorios")
def list_consultorios():
    return _catalog("consultorios", *CATALOGS["consultorios"])


@api_externa.get("/tratamientos")
def list_tratamientos():
    return _catalog("tratamientos", *CATALOGS["tratamientos"])


def _as_time(value):
    """PyMySQL devuelve las columnas TIME como timedelta."""
    if isinstance(value, timedelta):
        return (datetime.min + value).time()
    return value


@api_externa.get("/disponibilidad")
def disponibilidad():
    raw_date = request.args.get("fecha", "").strip()
    try:
        target = date.fromisoformat(raw_date) if raw_date else date.today()
    except ValueError:
        return jsonify(error="El parámetro 'fecha' debe tener el formato YYYY-MM-DD."), 400

    if target < date.today():
        return jsonify(error="La fecha indicada ya pasó."), 400

    medico = request.args.get("medico", "").strip()[:200]
    consultorio = request.args.get("consultorio", "").strip()[:100]

    sql = "SELECT `hora` FROM `citas` WHERE `fecha` = %s AND `estado` <> 'Cancelada'"
    parameters = [target]
    if medico:
        sql += " AND `medico` = %s"
        parameters.append(medico)
    if consultorio:
        sql += " AND `consultorio` = %s"
        parameters.append(consultorio)

    connection = None
    try:
        connection = connect_to_db()
        with connection.cursor() as cursor:
            cursor.execute(sql, tuple(parameters))
            occupied = {_as_time(row["hora"]) for row in cursor.fetchall()}
    except pymysql.MySQLError:
        return jsonify(error="Servicio temporalmente no disponible."), 503
    finally:
        if connection:
            connection.close()

    now = datetime.now()
    slots = []
    current = datetime.combine(target, OPENING_TIME)
    closing = datetime.combine(target, CLOSING_TIME)
    while current < closing:
        slot = current.time()
        current += SLOT_STEP
        if target == now.date() and slot <= now.time():
            continue
        slots.append({
            "hora": slot.strftime("%H:%M"),
            "estado": "ocupado" if slot in occupied else "libre",
        })

    return jsonify(
        fecha=target.isoformat(),
        medico=medico or None,
        consultorio=consultorio or None,
        libres=sum(1 for slot in slots if slot["estado"] == "libre"),
        franjas=slots,
    )
