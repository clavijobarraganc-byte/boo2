"""Conexión MySQL y utilidades compartidas entre la app y las APIs."""

import os
from datetime import timedelta
from decimal import Decimal

import pymysql
import pymysql.cursors


DB_NAME = os.environ.get("DB_NAME", "hospedaje")


def connect_to_db():
    """Abre una conexión nueva; quien la usa es responsable de cerrarla."""
    return pymysql.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        user=os.environ.get("DB_USER", "root"),
        password=os.environ.get("DB_PASSWORD", ""),
        database=DB_NAME,
        cursorclass=pymysql.cursors.DictCursor,
        charset="utf8mb4",
        connect_timeout=5,
        ssl_disabled=True,
    )


def module_response(row):
    """Serialize MySQL types that JSON cannot encode (Decimal, time, date)."""
    normalized = {}
    for key, value in row.items():
        if isinstance(value, Decimal):
            value = float(value)
        elif isinstance(value, timedelta):
            seconds = int(value.total_seconds())
            value = f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"
        elif hasattr(value, "isoformat"):
            value = value.isoformat()
        normalized[key] = value
    return normalized
