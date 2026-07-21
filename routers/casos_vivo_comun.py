"""
routers/casos_vivo_comun.py
Modelos Pydantic y funciones auxiliares compartidas entre
casos_vivo_alumno.py y casos_vivo_profesor.py. No expone ningun
endpoint propio - solo helpers importados por los otros dos routers.
"""

from typing import List, Optional

from fastapi import HTTPException
from pydantic import BaseModel

from routers.auth import sb


# ---------------- MODELOS ----------------
class CasoIn(BaseModel):
    region: str
    titulo: str
    vineta_clinica: str

class PreguntaCasoIn(BaseModel):
    pregunta_id: str
    orden: int  # 1-5, fijo

class FundamentoIn(BaseModel):
    explicacion: str
    fuentes: Optional[List[str]] = None

class PresentacionIn(BaseModel):
    titulo: str
    region: Optional[str] = None

class PresentacionCasoIn(BaseModel):
    caso_id: str
    orden: int

class IniciarSesionIn(BaseModel):
    presentacion_id: str

class IngresoAlumnoIn(BaseModel):
    nombre: str
    rut: str

class VotarIn(BaseModel):
    sesion_id: str
    alumno_id: str
    pregunta_id: str
    opcion: int

class AccionIn(BaseModel):
    accion: str  # "abrir_votacion" | "cerrar_votacion" | "revelar" | "siguiente"


# ---------------- HELPERS DE SESION EN VIVO ----------------

def obtener_sesion(sesion_id: str) -> dict:
    sesion = sb.table("sesiones_vivo").select("*").eq("id", sesion_id).single().execute().data
    if not sesion:
        raise HTTPException(404, "Sesion no encontrada")
    return sesion

def caso_actual(sesion: dict) -> Optional[dict]:
    """Devuelve el caso clinico completo (titulo, vineta, media) en el orden actual de la sesion."""
    puente = sb.table("presentacion_casos").select(
        "casos_clinicos(id, region, titulo, vineta_clinica, media_url, media_tipo)"
    ).eq("presentacion_id", sesion["presentacion_id"]).eq("orden", sesion["caso_actual_orden"]).execute().data
    return puente[0]["casos_clinicos"] if puente else None

def pregunta_actual(sesion: dict) -> Optional[dict]:
    """Resuelve la pregunta activa navegando presentacion->caso(orden)->pregunta(orden).
    Incluye el fundamento ya revisado y guardado (explicacion_generada/fuentes_generadas)
    y el caso clinico completo (para mostrar titulo + vineta + media del caso)."""
    caso = caso_actual(sesion)
    if not caso:
        return None

    pregunta_puente = sb.table("caso_preguntas").select(
        "pregunta_id, explicacion_generada, fuentes_generadas, "
        "banco_preguntas(id, pregunta, opciones, correcta, explicacion, media_url, media_tipo)"
    ).eq("caso_id", caso["id"]).eq("orden", sesion["pregunta_actual_orden"]).execute().data
    if not pregunta_puente:
        return None

    return {"caso": caso, **pregunta_puente[0]}

def total_preguntas_caso(caso_id: str) -> int:
    filas = sb.table("caso_preguntas").select("id").eq("caso_id", caso_id).execute().data
    return len(filas)

def total_casos_presentacion(presentacion_id: str) -> int:
    filas = sb.table("presentacion_casos").select("id").eq("presentacion_id", presentacion_id).execute().data
    return len(filas)

def url_firmada_media(bucket: str, storage_path: Optional[str], segundos: int = 300) -> Optional[str]:
    """Genera una URL firmada temporal para un archivo en un bucket privado.
    Devuelve None si no hay storage_path (pregunta/caso sin foto/video)."""
    if not storage_path:
        return None
    firmada = sb.storage.from_(bucket).create_signed_url(storage_path, segundos)
    return firmada.get("signedURL") or firmada.get("signed_url")
                      
