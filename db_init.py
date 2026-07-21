"""
db_init.py
Se ejecuta una vez al arrancar el backend (desde main.py).
Se conecta directo a Postgres con DATABASE_URL y corre schema.sql completo.
Como todo el schema usa "create table if not exists" / "create or replace view",
es seguro correrlo cada vez que el servicio arranca: si ya existe, no hace nada.

Además, crea (de forma idempotente) el bucket de Storage "casos" — privado,
mismo patrón que el bucket "preguntas" (URL firmada temporal al usarlo).
"""

import os
import psycopg2
from supabase import create_client

DATABASE_URL = os.environ["DATABASE_URL"]
SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]

BUCKETS_PRIVADOS = ["casos"]


def inicializar_schema():
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        sql = f.read()

    conn = psycopg2.connect(DATABASE_URL)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(sql)
        print("[db_init] schema.sql aplicado correctamente.")
    except Exception as e:
        print(f"[db_init] ERROR aplicando schema.sql: {e}")
    finally:
        conn.close()

    _inicializar_buckets()


def _inicializar_buckets():
    sb = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

    for nombre in BUCKETS_PRIVADOS:
        try:
            sb.storage.create_bucket(nombre, options={"public": False})
            print(f"[db_init] Bucket '{nombre}' creado (privado).")
        except Exception as e:
            mensaje = str(e).lower()
            if "already exists" in mensaje or "duplicate" in mensaje:
                print(f"[db_init] Bucket '{nombre}' ya existía, sin cambios.")
            else:
                print(f"[db_init] ERROR creando bucket '{nombre}': {e}")
                
