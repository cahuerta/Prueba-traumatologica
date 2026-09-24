"""
routers/clases_formales_ingreso.py
Ingreso del alumno a una sesion de Clases Formales -publico, sin auth-.
Funciona IGUAL que el ingreso de Casos Clinicos (casos_vivo_alumno.py:
ingreso_alumno_vivo): el alumno entra con nombre + RUT o numero de
matricula, se valida contra el conjunto de alumnos activo, se actualiza
su nombre en la tabla alumnos y se registra su asistencia en Supabase.

Se mantiene en su propio archivo, siguiendo el patron de este modulo de
un archivo chico por funcion (ver clases_formales_semaforo.py,
clases_formales_preguntas.py, clases_formales_trivia.py,
clases_formales_actual.py).

Tabla usada: asistencia_clase_alumnos (crear con
migracion_asistencia_clase_alumnos.sql), espejo de asistencia_vivo:
  sesion_id   uuid  (FK a sesiones_clase.id)
  alumno_id   uuid  (FK a alumnos.id)
  marcado_at  timestamptz
  PK (sesion_id, alumno_id)

Ademas se copia en memoria (services/cache_clases_formales.py) para que
el polling del mando no consulte Supabase cada 2 segundos.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from routers.auth import sb
from routers.conjuntos_comun import obtener_conjunto_activo_id
from services import cache_clases_formales

router = APIRouter(prefix="/clases-formales/ingreso", tags=["clases-formales-ingreso"])


class IngresoIn(BaseModel):
    sesion_id: str
    nombre: str
    rut: str  # RUT o numero de matricula, texto libre (igual que Casos Clinicos)


@router.post("")
def ingreso_clase(body: IngresoIn):
    """El alumno entra desde el link/QR de la clase con nombre + RUT o
    matricula. Si entra varias veces, sigue contando una sola vez
    (upsert por sesion_id + alumno_id)."""
    nombre = body.nombre.strip()
    rut = body.rut.strip()
    if not nombre:
        raise HTTPException(400, "Ingresa tu nombre")
    if not rut:
        raise HTTPException(400, "Ingresa tu RUT o número de matrícula")

    sesion = sb.table("sesiones_clase").select("id").eq("id", body.sesion_id).execute().data
    if not sesion:
        raise HTTPException(404, "Sesion no encontrada")

    conjunto_id = obtener_conjunto_activo_id()
    alumno = sb.table("alumnos").select("id").eq("rut", rut).eq("conjunto_id", conjunto_id).execute().data
    if not alumno:
        raise HTTPException(403, "RUT no reconocido en el conjunto activo")
    alumno_id = alumno[0]["id"]

    sb.table("alumnos").update({"nombre": nombre}).eq("id", alumno_id).execute()

    marcado_at = datetime.now(timezone.utc).isoformat()
    sb.table("asistencia_clase_alumnos").upsert(
        {"sesion_id": body.sesion_id, "alumno_id": alumno_id},
        on_conflict="sesion_id,alumno_id",
        ignore_duplicates=True,
    ).execute()

    cache_clases_formales.marcar_presente(body.sesion_id, alumno_id, nombre, rut, marcado_at)

    return {"ok": True, "alumno_id": alumno_id}
