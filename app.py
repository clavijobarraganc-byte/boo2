"""Patient/guest register backed by the existing hospedaje.huesped table."""

import hmac
import os
import re
import secrets
from datetime import date, time, timedelta
from functools import wraps
import pymysql
from flask import Blueprint, Flask, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from api_externa import api_externa
from db import DB_NAME, connect_to_db, module_response
from portal_pacientes import portal


# Templates stay beside app.py to preserve the supplied two-file project layout.
app = Flask(__name__, template_folder=".")
app.config.update(
    SECRET_KEY=os.environ.get("FLASK_SECRET_KEY", "local-only-change-this-secret"),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "false").lower() == "true",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
)

app.register_blueprint(api_externa)
app.register_blueprint(portal)

# Las dos APIs internas viven en app.py porque comparten los helpers de dominio.
# La externa está aparte por ser una superficie de consumo distinta.
api_pacientes = Blueprint("api_pacientes", __name__, url_prefix="/api/interna/pacientes")
api_modulos = Blueprint("api_modulos", __name__, url_prefix="/api/interna/modulos")

LOGIN_USERNAME = os.environ.get("APP_USERNAME", "admin")
LOGIN_PASSWORD_HASH = generate_password_hash(os.environ.get("APP_PASSWORD", "admin123"))
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
REQUIRED_COLUMNS = {"nombre", "apellido", "correo"}
MODULE_FIELDS = {
    "citas": [
        ("fecha", "Fecha", "date", 10, True),
        ("hora", "Hora", "time", 5, True),
        ("paciente", "Paciente", "text", 200, True),
        ("medico", "Médico", "text", 200, True),
        ("consultorio", "Consultorio", "text", 100, True),
        ("tratamiento", "Tratamiento", "text", 200, False),
        ("motivo", "Motivo", "textarea", 500, False),
        ("estado", "Estado", "status", 20, True),
    ],
    "consultorios": [
        ("nombre", "Nombre", "text", 100, True),
        ("ubicacion", "Ubicación", "text", 200, True),
        ("telefono", "Teléfono", "text", 30, False),
        ("estado", "Estado", "room_status", 20, True),
    ],
    "medicos": [
        ("nombre", "Nombre completo", "text", 200, True),
        ("especialidad", "Especialidad", "text", 120, True),
        ("correo", "Correo electrónico", "email", 254, False),
        ("telefono", "Teléfono", "text", 30, False),
    ],
    "tratamientos": [
        ("nombre", "Nombre", "text", 150, True),
        ("descripcion", "Descripción", "textarea", 500, False),
        ("duracion", "Duración", "text", 60, False),
        ("costo", "Costo", "money", 20, False),
    ],
}
MODULE_OPTIONS = {
    "estado": {"Pendiente", "Confirmada", "Atendida", "Cancelada"},
    "room_status": {"Disponible", "Ocupado", "Mantenimiento"},
}
MODULE_TABLES = {
    "citas": """
        CREATE TABLE IF NOT EXISTS `citas` (
            `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
            `huesped_id` BIGINT UNSIGNED NULL,
            `fecha` DATE NOT NULL,
            `hora` TIME NOT NULL,
            `paciente` VARCHAR(200) NOT NULL,
            `medico` VARCHAR(200) NOT NULL,
            `consultorio` VARCHAR(100) NOT NULL,
            `tratamiento` VARCHAR(200) NOT NULL DEFAULT '',
            `motivo` VARCHAR(500) NOT NULL DEFAULT '',
            `estado` ENUM('Pendiente', 'Confirmada', 'Atendida', 'Cancelada') NOT NULL,
            `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            INDEX `idx_citas_fecha_hora` (`fecha`, `hora`),
            INDEX `idx_citas_huesped` (`huesped_id`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    "consultorios": """
        CREATE TABLE IF NOT EXISTS `consultorios` (
            `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
            `nombre` VARCHAR(100) NOT NULL,
            `ubicacion` VARCHAR(200) NOT NULL,
            `telefono` VARCHAR(30) NOT NULL DEFAULT '',
            `estado` ENUM('Disponible', 'Ocupado', 'Mantenimiento') NOT NULL,
            `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    "medicos": """
        CREATE TABLE IF NOT EXISTS `medicos` (
            `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
            `nombre` VARCHAR(200) NOT NULL,
            `especialidad` VARCHAR(120) NOT NULL,
            `correo` VARCHAR(254) NOT NULL DEFAULT '',
            `telefono` VARCHAR(30) NOT NULL DEFAULT '',
            `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    "tratamientos": """
        CREATE TABLE IF NOT EXISTS `tratamientos` (
            `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
            `nombre` VARCHAR(150) NOT NULL,
            `descripcion` VARCHAR(500) NOT NULL DEFAULT '',
            `duracion` VARCHAR(60) NOT NULL DEFAULT '',
            `costo` DECIMAL(10, 2) NULL,
            `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
}


class DatabaseUnavailable(Exception):
    """Raised when the expected table/schema is unavailable."""


def table_metadata(connection):
    """Discover the existing primary key instead of assuming its column name."""
    with connection.cursor() as cursor:
        cursor.execute("SHOW COLUMNS FROM `huesped`")
        columns = {row["Field"] for row in cursor.fetchall()}
        cursor.execute("SHOW KEYS FROM `huesped` WHERE `Key_name` = 'PRIMARY'")
        primary_keys = cursor.fetchall()

    if not REQUIRED_COLUMNS.issubset(columns):
        raise DatabaseUnavailable("La tabla huesped no contiene nombre, apellido y correo.")
    if len(primary_keys) != 1:
        raise DatabaseUnavailable("La tabla huesped necesita una clave primaria simple para editar y eliminar.")

    primary_key = primary_keys[0]["Column_name"]
    # The identifier came from MySQL metadata; quoting it is safe and values remain parametrized.
    return primary_key, columns


def csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


@app.context_processor
def inject_csrf_token():
    return {"csrf_token": csrf_token, "db_name": DB_NAME}


def is_api_request():
    return request.path.startswith("/api/") or request.is_json


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("authenticated"):
            if is_api_request():
                return jsonify(error="Tu sesión ha caducado. Inicia sesión de nuevo."), 401
            return redirect(url_for("login", next=request.full_path if request.query_string else request.path))
        return view(*args, **kwargs)

    return wrapped


def csrf_protected(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        supplied_token = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token")
        if not supplied_token or not hmac.compare_digest(supplied_token, session.get("csrf_token", "")):
            if is_api_request():
                return jsonify(error="La sesión del formulario caducó. Actualiza la página e inténtalo de nuevo."), 400
            return render_template("login.html", error="La sesión del formulario caducó. Inténtalo de nuevo."), 400
        return view(*args, **kwargs)

    return wrapped


def read_guest_fields(payload):
    if not hasattr(payload, "get"):
        return None, "Los datos enviados no tienen un formato válido."
    fields = {}
    for field in ("nombre", "apellido", "correo"):
        value = payload.get(field, "")
        if not isinstance(value, str):
            value = ""
        fields[field] = value.strip()

    if not fields["nombre"]:
        return None, "El nombre es obligatorio."
    if not fields["apellido"]:
        return None, "El apellido es obligatorio."
    if not fields["correo"]:
        return None, "El correo es obligatorio."
    if len(fields["nombre"]) > 100 or len(fields["apellido"]) > 100:
        return None, "El nombre y el apellido no pueden superar 100 caracteres."
    if len(fields["correo"]) > 254 or not EMAIL_PATTERN.fullmatch(fields["correo"]):
        return None, "Introduce un correo electrónico válido."
    return fields, None


def guest_response(row, primary_key):
    return {
        "id": row[primary_key],
        "nombre": row["nombre"],
        "apellido": row["apellido"],
        "correo": row["correo"],
    }


def database_error(error):
    app.logger.exception("Error al consultar hospedaje.huesped: %s", error)
    return jsonify(error="No se pudo completar la operación con la base de datos. Comprueba MySQL y la tabla hospedaje.huesped."), 503


def connect_module_db():
    connection = connect_to_db()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET NAMES utf8mb4")
            for statement in MODULE_TABLES.values():
                cursor.execute(statement)
        connection.commit()
    except pymysql.MySQLError:
        connection.close()
        raise
    return connection


def read_module_fields(module, payload):
    if not hasattr(payload, "get"):
        return None, "Los datos enviados no tienen un formato válido."

    values = {}
    for name, label, kind, max_length, required in MODULE_FIELDS[module]:
        value = payload.get(name, "")
        value = value.strip() if isinstance(value, str) else ""
        if required and not value:
            return None, f"El campo {label.lower()} es obligatorio."
        if len(value) > max_length:
            return None, f"El campo {label.lower()} supera el límite permitido."
        if kind == "email" and value and not EMAIL_PATTERN.fullmatch(value):
            return None, "Introduce un correo electrónico válido."
        if kind == "date" and value:
            try:
                date.fromisoformat(value)
            except ValueError:
                return None, "Selecciona una fecha válida."
        if kind == "time" and value:
            try:
                time.fromisoformat(value)
            except ValueError:
                return None, "Selecciona una hora válida."
        if kind in MODULE_OPTIONS and value not in MODULE_OPTIONS[kind]:
            return None, f"Selecciona una opción válida para {label.lower()}."
        if kind == "money" and value and not re.fullmatch(r"\d{1,8}(?:\.\d{1,2})?", value):
            return None, "El costo debe ser un importe positivo con hasta dos decimales."
        values[name] = None if kind == "money" and not value else value
    return values, None


@app.after_request
def add_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("authenticated"):
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        if not hmac.compare_digest(request.form.get("csrf_token", ""), session.get("csrf_token", "")):
            error = "La sesión del formulario caducó. Actualiza la página e inténtalo de nuevo."
        else:
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            valid_username = hmac.compare_digest(username, LOGIN_USERNAME)
            valid_password = check_password_hash(LOGIN_PASSWORD_HASH, password)
            if valid_username and valid_password:
                session.clear()
                session["authenticated"] = True
                session["username"] = username
                session.permanent = True
                csrf_token()
                next_url = request.args.get("next", "")
                if next_url.startswith("/") and not next_url.startswith("//"):
                    return redirect(next_url)
                return redirect(url_for("index"))
            error = "Usuario o contraseña incorrectos."
    return render_template("login.html", error=error)


@app.post("/logout")
@login_required
@csrf_protected
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    return render_template("index.html", username=session.get("username", "admin"))


@app.get("/<module>/")
@app.get("/<module>/index.html")
@login_required
def module_index(module):
    if module not in MODULE_FIELDS and module != "pacientes":
        return jsonify(error="El módulo solicitado no existe."), 404
    destination = f"{url_for('index')}#{module}"
    return render_template(f"{module}/index.html", destination=destination)


@app.route("/agregar", methods=["GET", "POST"])
@login_required
def ingreso():
    """Legacy route retained; accepts the original HTML form and JSON clients."""
    if request.method == "GET":
        return redirect(url_for("index"))
    if request.is_json:
        if not hmac.compare_digest(request.headers.get("X-CSRF-Token", ""), session.get("csrf_token", "")):
            return jsonify(error="Token de seguridad inválido. Actualiza la página."), 400
        payload = request.get_json(silent=True) or {}
    else:
        if not hmac.compare_digest(request.form.get("csrf_token", ""), session.get("csrf_token", "")):
            return redirect(url_for("index", error="La sesión del formulario caducó."))
        payload = request.form
    fields, error = read_guest_fields(payload)
    if error:
        if request.is_json:
            return jsonify(error=error), 400
        return redirect(url_for("index", error=error))
    return create_guest(fields, redirect_on_success=not request.is_json)


def create_guest(fields, redirect_on_success=False):
    connection = None
    try:
        connection = connect_to_db()
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM `huesped` WHERE `correo` = %s LIMIT 1", (fields["correo"],))
            if cursor.fetchone():
                return jsonify(error="El correo ya existe. Usa una dirección diferente."), 409
            cursor.execute(
                "INSERT INTO `huesped` (`nombre`, `apellido`, `correo`) VALUES (%s, %s, %s)",
                (fields["nombre"], fields["apellido"], fields["correo"]),
            )
            guest_id = cursor.lastrowid
        connection.commit()
        if redirect_on_success:
            return redirect(url_for("index", success="Huésped agregado correctamente."))
        return jsonify(message="Huésped agregado correctamente.", id=guest_id), 201
    except pymysql.err.IntegrityError as error:
        if connection:
            connection.rollback()
        if error.args and error.args[0] == 1062:
            return jsonify(error="El correo ya existe. Usa una dirección diferente."), 409
        return jsonify(error="No se pudo guardar el huésped por un conflicto de datos."), 409
    except (pymysql.MySQLError, DatabaseUnavailable) as error:
        return database_error(error)
    finally:
        if connection:
            connection.close()


@app.get("/api/interna")
@login_required
def internal_api_description():
    return jsonify(
        internas=[
            {
                "nombre": "Pacientes",
                "prefijo": "/api/interna/pacientes",
                "auth": "Sesión del panel; X-CSRF-Token en POST, PUT y DELETE",
                "endpoints": {
                    "GET /api/interna/pacientes?q=": "Lista con total y coincidencias",
                    "POST /api/interna/pacientes": "Crea un paciente",
                    "PUT /api/interna/pacientes/<id>": "Actualiza un paciente",
                    "DELETE /api/interna/pacientes/<id>": "Elimina un paciente",
                },
            },
            {
                "nombre": "Módulos clínicos",
                "prefijo": "/api/interna/modulos",
                "auth": "Sesión del panel; X-CSRF-Token en POST, PUT y DELETE",
                "modulos": sorted(MODULE_FIELDS),
                "endpoints": {
                    "GET /api/interna/modulos/<módulo>?q=": "Lista de registros",
                    "POST /api/interna/modulos/<módulo>": "Crea un registro",
                    "PUT /api/interna/modulos/<módulo>/<id>": "Actualiza un registro",
                    "DELETE /api/interna/modulos/<módulo>/<id>": "Elimina un registro",
                },
            },
        ],
        compatibilidad=["/api/huespedes", "/api/modulos/<módulo>"],
        externa="/api/externa (X-API-Key, solo lectura)",
    )


@api_pacientes.get("")
@app.get("/api/interna/pacientes")
@app.get("/api/huespedes")
@login_required
def list_guests():
    query = request.args.get("q", "").strip()[:100]
    connection = None
    try:
        connection = connect_to_db()
        primary_key, _ = table_metadata(connection)
        selected_id = f"`{primary_key}`"
        sql = f"SELECT {selected_id}, `nombre`, `apellido`, `correo` FROM `huesped`"
        parameters = ()
        if query:
            pattern = f"%{query}%"
            sql += f" WHERE CAST({selected_id} AS CHAR) LIKE %s OR `nombre` LIKE %s OR `apellido` LIKE %s OR `correo` LIKE %s"
            parameters = (pattern, pattern, pattern, pattern)
        sql += f" ORDER BY {selected_id} DESC"
        with connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) AS total FROM `huesped`")
            total_records = cursor.fetchone()["total"]
            cursor.execute(sql, parameters)
            rows = cursor.fetchall()
        guests = [guest_response(row, primary_key) for row in rows]
        return jsonify(items=guests, total=total_records, filtered=len(guests))
    except (pymysql.MySQLError, DatabaseUnavailable) as error:
        return database_error(error)
    finally:
        if connection:
            connection.close()


@api_pacientes.post("")
@app.post("/api/interna/pacientes")
@app.post("/api/huespedes")
@login_required
@csrf_protected
def add_guest():
    fields, error = read_guest_fields(request.get_json(silent=True) or {})
    if error:
        return jsonify(error=error), 400
    return create_guest(fields)


@api_pacientes.route("/<guest_id>", methods=["PUT", "DELETE"])
@app.route("/api/interna/pacientes/<guest_id>", methods=["PUT", "DELETE"])
@app.route("/api/huespedes/<guest_id>", methods=["PUT", "DELETE"])
@login_required
@csrf_protected
def update_or_delete_guest(guest_id):
    connection = None
    try:
        connection = connect_to_db()
        primary_key, _ = table_metadata(connection)
        if request.method == "PUT":
            fields, error = read_guest_fields(request.get_json(silent=True) or {})
            if error:
                return jsonify(error=error), 400
            with connection.cursor() as cursor:
                cursor.execute(
                    f"SELECT 1 FROM `huesped` WHERE `correo` = %s AND `{primary_key}` <> %s LIMIT 1",
                    (fields["correo"], guest_id),
                )
                if cursor.fetchone():
                    return jsonify(error="El correo ya existe. Usa una dirección diferente."), 409
                cursor.execute(
                    f"UPDATE `huesped` SET `nombre` = %s, `apellido` = %s, `correo` = %s WHERE `{primary_key}` = %s",
                    (fields["nombre"], fields["apellido"], fields["correo"], guest_id),
                )
                if cursor.rowcount == 0:
                    cursor.execute(f"SELECT 1 FROM `huesped` WHERE `{primary_key}` = %s", (guest_id,))
                    if not cursor.fetchone():
                        return jsonify(error="No se encontró el huésped solicitado."), 404
            connection.commit()
            return jsonify(message="Datos del huésped actualizados correctamente.")

        with connection.cursor() as cursor:
            cursor.execute(f"DELETE FROM `huesped` WHERE `{primary_key}` = %s", (guest_id,))
            if cursor.rowcount == 0:
                return jsonify(error="No se encontró el huésped solicitado."), 404
        connection.commit()
        return jsonify(message="Huésped eliminado correctamente.")
    except pymysql.err.IntegrityError as error:
        if connection:
            connection.rollback()
        if error.args and error.args[0] == 1062:
            return jsonify(error="El correo ya existe. Usa una dirección diferente."), 409
        return jsonify(error="No se pudo guardar el huésped por un conflicto de datos."), 409
    except (pymysql.MySQLError, DatabaseUnavailable) as error:
        if connection:
            connection.rollback()
        return database_error(error)
    finally:
        if connection:
            connection.close()


@api_modulos.route("/<module>", methods=["GET", "POST"])
@app.route("/api/interna/modulos/<module>", methods=["GET", "POST"])
@app.route("/api/modulos/<module>", methods=["GET", "POST"])
@login_required
def list_or_create_module_records(module):
    if module not in MODULE_FIELDS:
        return jsonify(error="El módulo solicitado no existe."), 404
    if request.method == "POST":
        supplied_token = request.headers.get("X-CSRF-Token", "")
        if not supplied_token or not hmac.compare_digest(supplied_token, session.get("csrf_token", "")):
            return jsonify(error="La sesión del formulario caducó. Actualiza la página e inténtalo de nuevo."), 400
        values, error = read_module_fields(module, request.get_json(silent=True) or {})
        if error:
            return jsonify(error=error), 400
        connection = None
        try:
            connection = connect_module_db()
            fields = list(values)
            columns = ", ".join(f"`{field}`" for field in fields)
            placeholders = ", ".join(["%s"] * len(fields))
            with connection.cursor() as cursor:
                cursor.execute(
                    f"INSERT INTO `{module}` ({columns}) VALUES ({placeholders})",
                    tuple(values.values()),
                )
                new_id = cursor.lastrowid
            connection.commit()
            return jsonify(message="Registro creado correctamente.", id=new_id), 201
        except pymysql.MySQLError as error:
            app.logger.exception("Error al crear registro en %s: %s", module, error)
            return jsonify(error="No se pudo guardar el registro."), 503
        finally:
            if connection:
                connection.close()

    query = request.args.get("q", "").strip()[:100]
    connection = None
    try:
        connection = connect_module_db()
        fields = [field[0] for field in MODULE_FIELDS[module]]
        sql = f"SELECT * FROM `{module}`"
        parameters = ()
        if query:
            filters = " OR ".join(f"`{field}` LIKE %s" for field in fields)
            sql += f" WHERE {filters}"
            parameters = tuple(f"%{query}%" for _ in fields)
        sql += " ORDER BY `id` DESC"
        with connection.cursor() as cursor:
            cursor.execute(sql, parameters)
            rows = cursor.fetchall()
        return jsonify(items=[module_response(row) for row in rows], total=len(rows))
    except pymysql.MySQLError as error:
        app.logger.exception("Error al consultar el módulo %s: %s", module, error)
        return jsonify(error="No se pudo acceder a los datos del módulo."), 503
    finally:
        if connection:
            connection.close()


@api_modulos.route("/<module>/<int:record_id>", methods=["PUT", "DELETE"])
@app.route("/api/interna/modulos/<module>/<int:record_id>", methods=["PUT", "DELETE"])
@app.route("/api/modulos/<module>/<int:record_id>", methods=["PUT", "DELETE"])
@login_required
@csrf_protected
def update_or_delete_module_record(module, record_id):
    if module not in MODULE_FIELDS:
        return jsonify(error="El módulo solicitado no existe."), 404
    connection = None
    try:
        connection = connect_module_db()
        if request.method == "DELETE":
            with connection.cursor() as cursor:
                cursor.execute(f"DELETE FROM `{module}` WHERE `id` = %s", (record_id,))
                deleted = cursor.rowcount
            connection.commit()
            if deleted == 0:
                return jsonify(error="No se encontró el registro solicitado."), 404
            return jsonify(message="Registro eliminado correctamente.")

        values, error = read_module_fields(module, request.get_json(silent=True) or {})
        if error:
            return jsonify(error=error), 400
        assignments = ", ".join(f"`{field}` = %s" for field in values)
        exists = True
        with connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE `{module}` SET {assignments} WHERE `id` = %s",
                (*values.values(), record_id),
            )
            updated = cursor.rowcount
            if updated == 0:
                cursor.execute(f"SELECT 1 FROM `{module}` WHERE `id` = %s", (record_id,))
                exists = cursor.fetchone() is not None
        connection.commit()
        if not exists:
            return jsonify(error="No se encontró el registro solicitado."), 404
        return jsonify(message="Registro actualizado correctamente.")
    except pymysql.MySQLError as error:
        if connection:
            connection.rollback()
        app.logger.exception("Error al actualizar el módulo %s: %s", module, error)
        return jsonify(error="No se pudo completar la operación."), 503
    finally:
        if connection:
            connection.close()


# Se registran al final: Flask exige que el blueprint esté completo antes de
# registrarlo y sus rutas se decoran más abajo.
app.register_blueprint(api_pacientes)
app.register_blueprint(api_modulos)


if __name__ == "__main__":
    # Debug is deliberately disabled. Use a production WSGI server before exposing this app.
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5000")), debug=False)