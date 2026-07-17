"""
db_init.py
Se ejecuta una vez al arrancar el backend (desde main.py).
Se conecta directo a Postgres con DATABASE_URL y corre schema.sql completo.
Como todo el schema usa "create table if not exists" / "create or replace view",
es seguro correrlo cada vez que el servicio arranca: si ya existe, no hace nada.
"""

import os
import psycopg2

DATABASE_URL = os.environ["DATABASE_URL"]
SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")


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
      
