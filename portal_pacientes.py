"""Portal de pacientes: registro, sesión, solicitud y cancelación de citas.

Las cuentas viven en `cuentas_paciente` y se enlazan con `huesped`. Las citas
del portal se guardan en `citas` con `huesped_id`, sin tocar el flujo del panel
administrativo (que sigue trabajando con el nombre del paciente).
"""

import hmac
import re
import secrets
from datetime import date, datetime, time, timedelta
from functools import wraps

import pymysql
from flask import (
    Blueprint,
    current_app,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

from db import connect_to_db, module_response

portal = Blueprint("portal", __name__, url_prefix="/portal")

EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
OPENING = time(9, 0)
CLOSING = time(18, 0)
STEP = timedelta(minutes=30)
CANCELABLES = ("Pendiente", "Confirmada")
DIAS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
MESES = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
         "agosto", "septiembre", "octubre", "noviembre", "diciembre")

CREATE_CUENTAS = """
    CREATE TABLE IF NOT EXISTS `cuentas_paciente` (
        `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
        `huesped_id` BIGINT UNSIGNED NOT NULL,
        `correo` VARCHAR(254) NOT NULL,
        `password_hash` VARCHAR(255) NOT NULL,
        `activo` TINYINT(1) NOT NULL DEFAULT 1,
        `creado_en` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY `uq_cuenta_correo` (`correo`),
        UNIQUE KEY `uq_cuenta_huesped` (`huesped_id`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""


def _slot_labels():
    labels = []
    cursor = datetime.combine(date(2000, 1, 1), OPENING)
    limit = datetime.combine(date(2000, 1, 1), CLOSING)
    while cursor < limit:
        labels.append(cursor.strftime("%H:%M"))
        cursor += STEP
    return tuple(labels)


SLOTS = _slot_labels()
_schema_ready = False


# --------------------------------------------------------------------------- #
# Infraestructura
# --------------------------------------------------------------------------- #
def _ensure_schema(connection):
    """Crea la tabla de cuentas y la columna `citas.huesped_id` si faltan."""
    global _schema_ready
    if _schema_ready:
        return
    with connection.cursor() as cursor:
        cursor.execute(CREATE_CUENTAS)
        cursor.execute("SHOW COLUMNS FROM `citas` LIKE 'huesped_id'")
        if not cursor.fetchone():
            cursor.execute(
                "ALTER TABLE `citas` ADD COLUMN `huesped_id` BIGINT UNSIGNED NULL AFTER `id`"
            )
        cursor.execute("SHOW INDEX FROM `citas` WHERE `Key_name` = 'idx_citas_huesped'")
        if not cursor.fetchone():
            cursor.execute("ALTER TABLE `citas` ADD INDEX `idx_citas_huesped` (`huesped_id`)")
    connection.commit()
    _schema_ready = True


def _csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


def _csrf_valid():
    supplied = request.form.get("csrf_token", "")
    expected = session.get("csrf_token", "")
    return bool(supplied) and bool(expected) and hmac.compare_digest(supplied, expected)


def _flash(kind, message):
    session["portal_flash"] = [kind, message]


def _take_flash():
    return session.pop("portal_flash", None)


def paciente_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("paciente_id"):
            if request.path.endswith("/horarios"):
                return jsonify(error="Tu sesión ha caducado. Inicia sesión de nuevo."), 401
            return redirect(url_for("portal.login"))
        return view(*args, **kwargs)

    return wrapped


def _hhmm(value):
    if isinstance(value, timedelta):
        total = int(value.total_seconds())
        return f"{total // 3600:02d}:{total % 3600 // 60:02d}"
    if isinstance(value, time):
        return value.strftime("%H:%M")
    return str(value)[:5]


def _fecha_legible(iso):
    try:
        parsed = date.fromisoformat(iso)
    except (TypeError, ValueError):
        return iso or ""
    return f"{DIAS[parsed.weekday()]} {parsed.day:02d} {MESES[parsed.month - 1]} {parsed.year}"


def _serialize_cita(row):
    data = module_response(row)
    data["hora"] = _hhmm(row["hora"])
    data["fecha_legible"] = _fecha_legible(data.get("fecha"))
    return data


# --------------------------------------------------------------------------- #
# Consultas compartidas
# --------------------------------------------------------------------------- #
def _paciente(connection, huesped_id):
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT `id`, `nombre`, `apellido`, `correo` FROM `huesped` WHERE `id` = %s",
            (huesped_id,),
        )
        return cursor.fetchone()


def _attach_legacy_citas(connection, paciente):
    """Enlaza citas antiguas guardadas por nombre, solo si el nombre es único."""
    nombre_completo = f"{paciente['nombre']} {paciente['apellido']}"
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE `citas` SET `huesped_id` = %s
             WHERE `huesped_id` IS NULL AND `paciente` = %s
               AND (SELECT COUNT(*) FROM `huesped`
                     WHERE CONCAT(`nombre`, ' ', `apellido`) = %s) = 1
            """,
            (paciente["id"], nombre_completo, nombre_completo),
        )
        updated = cursor.rowcount
    if updated:
        connection.commit()
    return updated


def _citas_de(connection, huesped_id):
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT `id`, `fecha`, `hora`, `medico`, `consultorio`, `tratamiento`,
                   `motivo`, `estado`
              FROM `citas`
             WHERE `huesped_id` = %s
             ORDER BY `fecha` DESC, `hora` DESC
             LIMIT 200
            """,
            (huesped_id,),
        )
        return [_serialize_cita(row) for row in cursor.fetchall()]


def _catalogo(connection, tabla):
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT `nombre` FROM `{tabla}` ORDER BY `nombre`")
        return [row["nombre"] for row in cursor.fetchall()]


def _consultorio_libre(connection):
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT `nombre` FROM `consultorios` WHERE `estado` = 'Disponible' ORDER BY `id` LIMIT 1"
        )
        row = cursor.fetchone()
    return row["nombre"] if row else "Por asignar"


def _horas_ocupadas(connection, target, medico=None):
    sql = """
        SELECT `hora` FROM `citas`
         WHERE `fecha` = %s AND `estado` <> 'Cancelada'
    """
    parameters = [target]
    if medico:
        sql += " AND `medico` = %s"
        parameters.append(medico)
    with connection.cursor() as cursor:
        cursor.execute(sql, parameters)
        return {_hhmm(row["hora"]) for row in cursor.fetchall()}


def _slots_libres(connection, target, medico=None):
    if target < date.today():
        return []
    ocupadas = _horas_ocupadas(connection, target, medico)
    ahora = datetime.now().strftime("%H:%M")
    libres = []
    for slot in SLOTS:
        if target == date.today() and slot <= ahora:
            continue
        if slot in ocupadas:
            continue
        libres.append(slot)
    return libres


# --------------------------------------------------------------------------- #
# Autenticación del paciente
# --------------------------------------------------------------------------- #
@portal.get("/login")
def login_form():
    if session.get("paciente_id"):
        return redirect(url_for("portal.panel"))
    return render_template("portal_login.html", error=None, flash=_take_flash(), correo="")


@portal.post("/login")
def login():
    if not _csrf_valid():
        return render_template(
            "portal_login.html",
            error="La sesión del formulario caducó. Inténtalo de nuevo.",
            flash=None,
            correo="",
        )
    correo = request.form.get("correo", "").strip().lower()
    password = request.form.get("password", "")
    if not correo or not password:
        return render_template(
            "portal_login.html", error="Introduce tu correo y contraseña.", flash=None, correo=correo
        )

    connection = None
    try:
        connection = connect_to_db()
        _ensure_schema(connection)
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT c.`id`, c.`huesped_id`, c.`password_hash`, c.`activo`,
                       h.`nombre`, h.`apellido`
                  FROM `cuentas_paciente` c
                  JOIN `huesped` h ON h.`id` = c.`huesped_id`
                 WHERE c.`correo` = %s
                 LIMIT 1
                """,
                (correo,),
            )
            cuenta = cursor.fetchone()
    except pymysql.MySQLError as error:
        current_app.logger.exception("Error al iniciar sesión del paciente: %s", error)
        return render_template(
            "portal_login.html",
            error="No pudimos validar tu acceso. Inténtalo más tarde.",
            flash=None,
            correo=correo,
        )
    finally:
        if connection:
            connection.close()

    if not cuenta or not cuenta["activo"] or not check_password_hash(cuenta["password_hash"], password):
        return render_template(
            "portal_login.html", error="Correo o contraseña incorrectos.", flash=None, correo=correo
        )

    session.clear()
    session["paciente_id"] = cuenta["huesped_id"]
    session["paciente_nombre"] = f"{cuenta['nombre']} {cuenta['apellido']}"
    session["paciente_correo"] = correo
    session.permanent = True
    _csrf_token()
    return redirect(url_for("portal.panel"))


@portal.get("/registro")
def registro_form():
    if session.get("paciente_id"):
        return redirect(url_for("portal.panel"))
    return render_template("portal_registro.html", error=None, flash=_take_flash(), values={})


@portal.post("/registro")
def registro():
    if session.get("paciente_id"):
        return redirect(url_for("portal.panel"))

    values = {
        "nombre": request.form.get("nombre", "").strip(),
        "apellido": request.form.get("apellido", "").strip(),
        "correo": request.form.get("correo", "").strip().lower(),
    }
    password = request.form.get("password", "")
    confirmation = request.form.get("password_confirm", "")

    def fail(message):
        return render_template("portal_registro.html", error=message, flash=None, values=values)

    if not _csrf_valid():
        return fail("La sesión del formulario caducó. Inténtalo de nuevo.")
    if not values["nombre"] or not values["apellido"] or not values["correo"]:
        return fail("Completa tu nombre, apellido y correo.")
    if len(values["nombre"]) > 100 or len(values["apellido"]) > 100:
        return fail("El nombre y el apellido no pueden superar 100 caracteres.")
    if len(values["correo"]) > 254 or not EMAIL_PATTERN.fullmatch(values["correo"]):
        return fail("Introduce un correo electrónico válido.")
    if len(password) < 8:
        return fail("La contraseña debe tener al menos 8 caracteres.")
    if len(password) > 128:
        return fail("La contraseña es demasiado larga.")
    if password != confirmation:
        return fail("Las contraseñas no coinciden.")

    connection = None
    try:
        connection = connect_to_db()
        _ensure_schema(connection)
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM `cuentas_paciente` WHERE `correo` = %s LIMIT 1", (values["correo"],))
            if cursor.fetchone():
                return fail("Ese correo ya tiene una cuenta. Inicia sesión.")
            cursor.execute("SELECT `id` FROM `huesped` WHERE `correo` = %s LIMIT 1", (values["correo"],))
            existente = cursor.fetchone()
            if existente:
                huesped_id = existente["id"]
            else:
                cursor.execute(
                    "INSERT INTO `huesped` (`nombre`, `apellido`, `correo`) VALUES (%s, %s, %s)",
                    (values["nombre"], values["apellido"], values["correo"]),
                )
                huesped_id = cursor.lastrowid
            cursor.execute(
                "INSERT INTO `cuentas_paciente` (`huesped_id`, `correo`, `password_hash`) VALUES (%s, %s, %s)",
                (huesped_id, values["correo"], generate_password_hash(password)),
            )
        connection.commit()
    except pymysql.err.IntegrityError as error:
        if connection:
            connection.rollback()
        current_app.logger.warning("Registro duplicado en el portal: %s", error)
        return fail("Ese correo ya está registrado. Inicia sesión o usa otro correo.")
    except pymysql.MySQLError as error:
        if connection:
            connection.rollback()
        current_app.logger.exception("Error al registrar paciente: %s", error)
        return render_template(
            "portal_registro.html",
            error="No pudimos crear tu cuenta. Comprueba la base de datos e inténtalo de nuevo.",
            flash=None,
            values=values,
        )
    finally:
        if connection:
            connection.close()

    session.clear()
    session["paciente_id"] = huesped_id
    session["paciente_nombre"] = f"{values['nombre']} {values['apellido']}"
    session["paciente_correo"] = values["correo"]
    session.permanent = True
    _csrf_token()
    _flash("success", "Cuenta creada. Ya puedes solicitar tu primera cita.")
    return redirect(url_for("portal.panel"))


@portal.post("/logout")
@paciente_required
def logout():
    if not _csrf_valid():
        return redirect(url_for("portal.panel"))
    session.clear()
    return redirect(url_for("portal.login"))


# --------------------------------------------------------------------------- #
# Panel del paciente
# --------------------------------------------------------------------------- #
@portal.get("/")
@paciente_required
def panel():
    connection = None
    paciente = None
    citas = []
    medicos = []
    tratamientos = []
    database_issue = None
    try:
        connection = connect_to_db()
        _ensure_schema(connection)
        paciente = _paciente(connection, session["paciente_id"])
        if not paciente:
            session.clear()
            return redirect(url_for("portal.login"))
        _attach_legacy_citas(connection, paciente)
        citas = _citas_de(connection, paciente["id"])
        medicos = _catalogo(connection, "medicos")
        tratamientos = _catalogo(connection, "tratamientos")
    except pymysql.MySQLError as error:
        current_app.logger.exception("Error al cargar el portal del paciente: %s", error)
        database_issue = "No pudimos consultar tus citas en este momento. Inténtalo de nuevo en unos minutos."
        paciente = paciente or {
            "id": session["paciente_id"],
            "nombre": session.get("paciente_nombre", "Paciente"),
            "apellido": "",
            "correo": session.get("paciente_correo", ""),
        }
    finally:
        if connection:
            connection.close()

    today = date.today().isoformat()
    for cita in citas:
        cita["cancelable"] = cita["estado"] in CANCELABLES and cita["fecha"] >= today
    proxima = next(
        (cita for cita in sorted(citas, key=lambda item: (item["fecha"], item["hora"]))
         if cita["fecha"] >= today and cita["estado"] in CANCELABLES),
        None,
    )
    resumen = {
        "proximas": sum(1 for cita in citas if cita["fecha"] >= today and cita["estado"] in CANCELABLES),
        "confirmadas": sum(1 for cita in citas if cita["estado"] == "Confirmada"),
        "canceladas": sum(1 for cita in citas if cita["estado"] == "Cancelada"),
        "atendidas": sum(1 for cita in citas if cita["estado"] == "Atendida"),
    }

    return render_template(
        "portal.html",
        paciente=paciente,
        citas=citas,
        proxima=proxima,
        resumen=resumen,
        medicos=medicos,
        tratamientos=tratamientos,
        slots=SLOTS,
        hoy=today,
        max_fecha=(date.today() + timedelta(days=120)).isoformat(),
        flash=_take_flash(),
        database_issue=database_issue,
    )


# --------------------------------------------------------------------------- #
# Perfil del paciente
# --------------------------------------------------------------------------- #
def _paciente_sesion():
    """Respaldo con los datos de sesión cuando la consulta a la base falla."""
    partes = session.get("paciente_nombre", "Paciente").split(" ", 1)
    return {
        "id": session.get("paciente_id"),
        "nombre": partes[0],
        "apellido": partes[1] if len(partes) > 1 else "",
        "correo": session.get("paciente_correo", ""),
    }


def _perfil_page(paciente, error=None, values=None, seccion="datos", flash=None):
    return render_template(
        "portal_perfil.html",
        paciente=paciente,
        values=values or {
            "nombre": paciente["nombre"],
            "apellido": paciente["apellido"],
            "correo": paciente["correo"],
        },
        error=error,
        flash=flash,
        seccion=seccion,
    )


@portal.get("/perfil")
@paciente_required
def perfil():
    connection = None
    try:
        connection = connect_to_db()
        _ensure_schema(connection)
        paciente = _paciente(connection, session["paciente_id"])
    except pymysql.MySQLError as error:
        current_app.logger.exception("Error al leer el perfil del paciente: %s", error)
        return _perfil_page(
            _paciente_sesion(),
            error="No pudimos leer tus datos en este momento. Inténtalo de nuevo en unos minutos.",
            flash=_take_flash(),
        )
    finally:
        if connection:
            connection.close()

    if not paciente:
        session.clear()
        return redirect(url_for("portal.login"))

    session["paciente_nombre"] = f"{paciente['nombre']} {paciente['apellido']}"
    session["paciente_correo"] = paciente["correo"]
    return _perfil_page(paciente, flash=_take_flash())


@portal.post("/perfil")
@paciente_required
def actualizar_perfil():
    values = {
        "nombre": request.form.get("nombre", "").strip(),
        "apellido": request.form.get("apellido", "").strip(),
        "correo": request.form.get("correo", "").strip().lower(),
    }
    nombre_nuevo = f"{values['nombre']} {values['apellido']}".strip()
    connection = None
    paciente = None
    try:
        connection = connect_to_db()
        _ensure_schema(connection)
        paciente = _paciente(connection, session["paciente_id"])
        if not paciente:
            session.clear()
            return redirect(url_for("portal.login"))

        def fail(message):
            return _perfil_page(paciente, error=message, values=values)

        if not _csrf_valid():
            return fail("La sesión del formulario caducó. Inténtalo de nuevo.")
        if not values["nombre"] or not values["apellido"] or not values["correo"]:
            return fail("El nombre, el apellido y el correo son obligatorios.")
        if len(values["nombre"]) > 100 or len(values["apellido"]) > 100:
            return fail("El nombre y el apellido no pueden superar 100 caracteres.")
        if len(values["correo"]) > 254 or not EMAIL_PATTERN.fullmatch(values["correo"]):
            return fail("Introduce un correo electrónico válido.")

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT `huesped_id` FROM `cuentas_paciente` WHERE `correo` = %s LIMIT 1",
                (values["correo"],),
            )
            propietario = cursor.fetchone()
            cursor.execute(
                "SELECT `id` FROM `huesped` WHERE `correo` = %s AND `id` <> %s LIMIT 1",
                (values["correo"], paciente["id"]),
            )
            otro_paciente = cursor.fetchone()
        if (propietario and propietario["huesped_id"] != paciente["id"]) or otro_paciente:
            return fail("Ese correo ya está registrado por otro paciente. Usa uno distinto.")

        nombre_anterior = f"{paciente['nombre']} {paciente['apellido']}"
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE `huesped` SET `nombre` = %s, `apellido` = %s, `correo` = %s WHERE `id` = %s",
                (values["nombre"], values["apellido"], values["correo"], paciente["id"]),
            )
            cursor.execute(
                "UPDATE `cuentas_paciente` SET `correo` = %s WHERE `huesped_id` = %s",
                (values["correo"], paciente["id"]),
            )
            # Las citas guardan una copia del nombre: la mantenemos al día.
            cursor.execute(
                "UPDATE `citas` SET `paciente` = %s WHERE `huesped_id` = %s AND `paciente` = %s",
                (nombre_nuevo, paciente["id"], nombre_anterior),
            )
        connection.commit()
    except pymysql.err.IntegrityError as error:
        if connection:
            connection.rollback()
        current_app.logger.warning("Correo duplicado al actualizar el perfil: %s", error)
        return _perfil_page(
            paciente or _paciente_sesion(),
            error="Ese correo ya está registrado por otro paciente. Usa uno distinto.",
            values=values,
        )
    except pymysql.MySQLError as error:
        if connection:
            connection.rollback()
        current_app.logger.exception("Error al actualizar el perfil del paciente: %s", error)
        return _perfil_page(
            paciente or _paciente_sesion(),
            error="No pudimos guardar tus cambios. Inténtalo de nuevo en unos minutos.",
            values=values,
        )
    finally:
        if connection:
            connection.close()

    session["paciente_nombre"] = nombre_nuevo
    session["paciente_correo"] = values["correo"]
    _flash("success", "Tus datos personales se actualizaron correctamente.")
    return redirect(url_for("portal.perfil"))


@portal.post("/perfil/password")
@paciente_required
def actualizar_password():
    actual = request.form.get("password_actual", "")
    nueva = request.form.get("password_nueva", "")
    confirmacion = request.form.get("password_confirm", "")
    connection = None
    paciente = None
    try:
        connection = connect_to_db()
        _ensure_schema(connection)
        paciente = _paciente(connection, session["paciente_id"])
        if not paciente:
            session.clear()
            return redirect(url_for("portal.login"))

        def fail(message):
            return _perfil_page(paciente, error=message, seccion="seguridad")

        if not _csrf_valid():
            return fail("La sesión del formulario caducó. Inténtalo de nuevo.")
        if not actual or not nueva or not confirmacion:
            return fail("Completa tu contraseña actual y escribe la nueva dos veces.")
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT `password_hash` FROM `cuentas_paciente` WHERE `huesped_id` = %s LIMIT 1",
                (paciente["id"],),
            )
            cuenta = cursor.fetchone()
        if not cuenta or not check_password_hash(cuenta["password_hash"], actual):
            return fail("Tu contraseña actual no es correcta.")
        if len(nueva) < 8:
            return fail("La nueva contraseña debe tener al menos 8 caracteres.")
        if len(nueva) > 128:
            return fail("La nueva contraseña es demasiado larga.")
        if nueva != confirmacion:
            return fail("Las contraseñas nuevas no coinciden.")

        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE `cuentas_paciente` SET `password_hash` = %s WHERE `huesped_id` = %s",
                (generate_password_hash(nueva), paciente["id"]),
            )
        connection.commit()
    except pymysql.MySQLError as error:
        if connection:
            connection.rollback()
        current_app.logger.exception("Error al cambiar la contraseña del paciente: %s", error)
        return _perfil_page(
            paciente or _paciente_sesion(),
            error="No pudimos cambiar tu contraseña. Inténtalo de nuevo en unos minutos.",
            seccion="seguridad",
        )
    finally:
        if connection:
            connection.close()

    _flash("success", "Tu contraseña se actualizó correctamente.")
    return redirect(url_for("portal.perfil"))


def _datos_form():
    return {
        "fecha": request.form.get("fecha", "").strip(),
        "hora": request.form.get("hora", "").strip(),
        "medico": request.form.get("medico", "").strip(),
        "tratamiento": request.form.get("tratamiento", "").strip(),
        "motivo": request.form.get("motivo", "").strip(),
    }


@portal.post("/citas")
@paciente_required
def solicitar_cita():
    if not _csrf_valid():
        _flash("error", "La sesión del formulario caducó. Inténtalo de nuevo.")
        return redirect(url_for("portal.panel"))

    data = _datos_form()
    try:
        fecha = date.fromisoformat(data["fecha"])
    except ValueError:
        _flash("error", "Selecciona una fecha válida para tu cita.")
        return redirect(url_for("portal.panel"))
    if fecha < date.today():
        _flash("error", "No puedes solicitar una cita en una fecha pasada.")
        return redirect(url_for("portal.panel"))
    if fecha > date.today() + timedelta(days=120):
        _flash("error", "Solo puedes solicitar citas con un máximo de 120 días de anticipación.")
        return redirect(url_for("portal.panel"))
    if data["hora"] not in SLOTS:
        _flash("error", "Selecciona un horario disponible (cada 30 minutos, de 09:00 a 17:30).")
        return redirect(url_for("portal.panel"))
    if not data["medico"]:
        _flash("error", "Selecciona el médico que quieres ver.")
        return redirect(url_for("portal.panel"))
    if len(data["medico"]) > 200 or len(data["tratamiento"]) > 200 or len(data["motivo"]) > 500:
        _flash("error", "Uno de los campos supera el largo permitido.")
        return redirect(url_for("portal.panel"))

    connection = None
    try:
        connection = connect_to_db()
        _ensure_schema(connection)
        paciente = _paciente(connection, session["paciente_id"])
        if not paciente:
            session.clear()
            return redirect(url_for("portal.login"))

        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM `medicos` WHERE `nombre` = %s LIMIT 1", (data["medico"],))
            medico_existe = bool(cursor.fetchone())
        if not medico_existe:
            _flash("error", "El médico seleccionado ya no está disponible en el catálogo.")
            return redirect(url_for("portal.panel"))
        if fecha == date.today() and data["hora"] <= datetime.now().strftime("%H:%M"):
            _flash("error", "Ese horario de hoy ya pasó. Elige uno posterior.")
            return redirect(url_for("portal.panel"))
        if data["hora"] in _horas_ocupadas(connection, fecha, data["medico"]):
            _flash("error", "Ese horario ya está ocupado con el médico seleccionado. Elige otro.")
            return redirect(url_for("portal.panel"))

        consultorio = _consultorio_libre(connection)
        nombre_completo = f"{paciente['nombre']} {paciente['apellido']}"
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO `citas`
                    (`huesped_id`, `fecha`, `hora`, `paciente`, `medico`, `consultorio`,
                     `tratamiento`, `motivo`, `estado`)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'Pendiente')
                """,
                (paciente["id"], fecha, data["hora"], nombre_completo, data["medico"],
                 consultorio, data["tratamiento"], data["motivo"]),
            )
        connection.commit()
        _flash("success", f"Solicitud registrada para el {_fecha_legible(fecha.isoformat())} a las {data['hora']}. Recepción la confirmará.")
    except pymysql.MySQLError as error:
        if connection:
            connection.rollback()
        current_app.logger.exception("Error al solicitar cita: %s", error)
        _flash("error", "No pudimos registrar tu solicitud. Inténtalo de nuevo en unos minutos.")
    finally:
        if connection:
            connection.close()
    return redirect(url_for("portal.panel"))


@portal.post("/citas/<int:cita_id>/cancelar")
@paciente_required
def cancelar_cita(cita_id):
    if not _csrf_valid():
        _flash("error", "La sesión del formulario caducó. Inténtalo de nuevo.")
        return redirect(url_for("portal.panel"))

    connection = None
    try:
        connection = connect_to_db()
        _ensure_schema(connection)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT `id`, `fecha`, `hora`, `estado` FROM `citas` WHERE `id` = %s AND `huesped_id` = %s",
                (cita_id, session["paciente_id"]),
            )
            cita = cursor.fetchone()
            if not cita:
                _flash("error", "No encontramos esa cita entre tus registros.")
                return redirect(url_for("portal.panel"))
            if cita["estado"] not in CANCELABLES:
                _flash("error", f"La cita está en estado «{cita['estado']}» y ya no se puede cancelar.")
                return redirect(url_for("portal.panel"))
            if str(cita["fecha"]) < date.today().isoformat():
                _flash("error", "No puedes cancelar una cita que ya pasó. Llama a recepción.")
                return redirect(url_for("portal.panel"))
            cursor.execute(
                "UPDATE `citas` SET `estado` = 'Cancelada' WHERE `id` = %s AND `huesped_id` = %s",
                (cita_id, session["paciente_id"]),
            )
        connection.commit()
        _flash("success", "Tu cita quedó cancelada. El horario vuelve a estar disponible.")
    except pymysql.MySQLError as error:
        if connection:
            connection.rollback()
        current_app.logger.exception("Error al cancelar cita: %s", error)
        _flash("error", "No pudimos cancelar la cita. Inténtalo de nuevo en unos minutos.")
    finally:
        if connection:
            connection.close()
    return redirect(url_for("portal.panel"))


@portal.get("/horarios")
@paciente_required
def horarios():
    fecha_raw = request.args.get("fecha", "").strip()
    medico = request.args.get("medico", "").strip()
    try:
        target = date.fromisoformat(fecha_raw)
    except ValueError:
        return jsonify(error="Fecha inválida."), 400
    if target < date.today():
        return jsonify(error="La fecha ya pasó."), 400

    connection = None
    try:
        connection = connect_to_db()
        _ensure_schema(connection)
        libres = _slots_libres(connection, target, medico or None)
    except pymysql.MySQLError as error:
        current_app.logger.exception("Error al calcular horarios: %s", error)
        return jsonify(error="No pudimos calcular la disponibilidad."), 503
    finally:
        if connection:
            connection.close()

    return jsonify(
        fecha=target.isoformat(),
        fecha_legible=_fecha_legible(target.isoformat()),
        medico=medico,
        libres=libres,
        total=len(libres),
    )
