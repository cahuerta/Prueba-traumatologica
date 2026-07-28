"""
routers/clases_formales_ingreso.py
Registro de asistencia de Clases Formales -publico, sin auth-, mismo
espiritu que la asistencia de casos clinicos (casos_vivo_alumno.py:
ingreso_alumno_vivo), pero en su propio archivo, siguiendo el patron
que ya tiene este modulo de un archivo chico por funcion (ver
clases_formales_semaforo.py, clases_formales_preguntas.py,
clases_formales_trivia.py, clases_formales_actual.py) en vez de
agrupar todo el lado alumno en un solo archivo grande.

No valida el RUT contra una tabla de alumnos -mismo criterio que ya
usan semaforo/preguntas/trivia en este modulo: el RUT se acepta tal
cual, sin conjunto activo de por medio-. Solo cuenta como presente en
memoria (services/cache_clases_formales.py), no se persiste a
Supabase -decision explicita, igual que el semaforo: es un dato de
sala en vivo, no un registro historico-.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from routers.auth import sb
from services import cache_clases_formales

router = APIRouter(prefix="/clases-formales/ingreso", tags=["clases-formales-ingreso"])


class IngresoIn(BaseModel):
    sesion_id: str
    rut: str


@router.post("")
def ingreso_clase(body: IngresoIn):
    """El alumno entra desde el link/QR de la clase, con su RUT. Marca
    presencia en la sesion -si entra varias veces con el mismo RUT,
    sigue contando una sola vez (ver cache_clases_formales.marcar_presente)."""
    sesion = sb.table("sesiones_clase").select("id").eq("id", body.sesion_id).execute().data
    if not sesion:
        raise HTTPException(404, "Sesion no encontrada")

    cache_clases_formales.marcar_presente(body.sesion_id, body.rut.strip())

    return {"ok": True}
  
