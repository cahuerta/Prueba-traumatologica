"""
routers/casos_vivo_comun.py
Modelos Pydantic y funciones auxiliares compartidas entre
casos_vivo_alumno.py y casos_vivo_profesor.py. No expone ningun
endpoint propio - solo helpers importados por los otros dos routers.

Los helpers de sesion/caso/pregunta pasan primero por la cache en
memoria (routers/cache_vivo.py) antes de tocar Supabase -con 90 alumnos
en polling cada pocos segundos, evita que cada lectura le pegue a
Supabase, que es donde vive el bug de concurrencia HTTP/2 bajo carga-.
"""

from typing import List, Optional

from fastapi import HTTPException
from pydantic import BaseModel

from routers.auth import sb
from routers import cache_vivo


# ---------------- MODELOS ----------------
class CasoIn(BaseModel):
    region: str
    titulo: str
    vineta_clinica: str

class GenerarAlternativasCasoIn(BaseModel):
    """El interrogador escribe la pregunta + la respuesta correcta.
    Claude propone solo las 4 alternativas falsas (no guarda nada)."""
    pregunta: str
    respuesta_correcta: str

class PreguntaCasoIn(BaseModel):
    """Guardado final de una pregunta del caso, ya revisada por el interrogador."""
    orden: int  # 1-5, fijo
    pregunta: str
    opciones: List[str]  # exactamente 5
    correcta: int         # indice 0-4
    media_url: Optional[str] = None
    media_tipo: Optional[str] = None  # "foto" | "video"

class PreguntaCasoUpdateIn(BaseModel):
    """Editar una pregunta del caso ya guardada (no cambia el orden)."""
    pregunta: str
    opciones: List[str]
    correcta: int
    media_url: Optional[str] = None
    media_tipo: Optional[str] = None

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
    pregunta_id: str  # id de caso_preguntas
    opcion: int

class AccionIn(BaseModel):
    accion: str  # "abrir_votacion" | "cerrar_votacion" | "revelar" | "siguiente"


# ---------------- HELPERS DE SESION EN VIVO (con cache) ----------------

def obtener_sesion(sesion_id: str) -> dict:
    cacheada = cache_vivo.obtener_sesion_cache(sesion_id)
    if cacheada is not None:
        return cacheada

    sesion = sb.table("sesiones_vivo").select("*").eq("id", sesion_id).single().execute().data
    if not sesion:
        raise HTTPException(404, "Sesion no encontrada")

    cache_vivo.guardar_sesion_cache(sesion_id, sesion)
    return sesion

def obtener_sesion_por_codigo(codigo: str) -> dict:
    """Igual que obtener_sesion, pero para cuando el alumno solo conoce
    el codigo_acceso (no el sesion_id todavia)."""
    sesion_id = cache_vivo.resolver_sesion_id_por_codigo(codigo)
    if sesion_id:
        cacheada = cache_vivo.obtener_sesion_cache(sesion_id)
        if cacheada is not None:
            return cacheada

    sesion = sb.table("sesiones_vivo").select("*").eq("codigo_acceso", codigo).single().execute().data
    if not sesion:
        raise HTTPException(404, "Codigo de sesion invalido")

    cache_vivo.guardar_sesion_cache(sesion["id"], sesion)
    return sesion

def caso_actual(sesion: dict) -> Optional[dict]:
    """Devuelve el caso clinico completo (titulo, vineta, media) en el orden actual de la sesion."""
    presentacion_id = sesion["presentacion_id"]
    orden = sesion["caso_actual_orden"]

    encontrado, caso_cacheado = cache_vivo.obtener_posicion_caso_cache(presentacion_id, orden)
    if encontrado:
        return caso_cacheado

    puente = sb.table("presentacion_casos").select(
        "casos_clinicos(id, region, titulo, vineta_clinica, media_url, media_tipo)"
    ).eq("presentacion_id", presentacion_id).eq("orden", orden).execute().data
    caso = puente[0]["casos_clinicos"] if puente else None

    cache_vivo.guardar_posicion_caso_cache(presentacion_id, orden, caso)
    return caso

def pregunta_actual(sesion: dict) -> Optional[dict]:
    """Resuelve la pregunta activa navegando presentacion->caso(orden)->pregunta(orden).
    caso_preguntas ya NO depende de banco_preguntas: la pregunta, opciones,
    correcta y su media viven directo en la fila. Devuelve un dict PLANO con
    los campos de caso_preguntas + la clave "caso" con el caso clinico completo."""
    caso = caso_actual(sesion)
    if not caso:
        return None

    orden = sesion["pregunta_actual_orden"]
    cacheada = cache_vivo.obtener_pregunta_cache(caso["id"], orden)
    if cacheada is not None:
        return {"caso": caso, **cacheada}

    filas = sb.table("caso_preguntas").select(
        "id, pregunta, opciones, correcta, media_url, media_tipo, "
        "explicacion_generada, fuentes_generadas"
    ).eq("caso_id", caso["id"]).eq("orden", orden).execute().data
    if not filas:
        return None

    cache_vivo.guardar_pregunta_cache(caso["id"], orden, filas[0])
    return {"caso": caso, **filas[0]}

def total_preguntas_caso(caso_id: str) -> int:
    cacheado = cache_vivo.obtener_total_preguntas_cache(caso_id)
    if cacheado is not None:
        return cacheado

    filas = sb.table("caso_preguntas").select("id").eq("caso_id", caso_id).execute().data
    total = len(filas)
    cache_vivo.guardar_total_preguntas_cache(caso_id, total)
    return total

def total_casos_presentacion(presentacion_id: str) -> int:
    cacheado = cache_vivo.obtener_total_casos_cache(presentacion_id)
    if cacheado is not None:
        return cacheado

    filas = sb.table("presentacion_casos").select("id").eq("presentacion_id", presentacion_id).execute().data
    total = len(filas)
    cache_vivo.guardar_total_casos_cache(presentacion_id, total)
    return total

def url_firmada_media(bucket: str, storage_path: Optional[str], segundos: int = 300) -> Optional[str]:
    """Genera una URL firmada temporal para un archivo en un bucket privado.
    Devuelve None si no hay storage_path (pregunta/caso sin foto/video)."""
    if not storage_path:
        return None
    firmada = sb.storage.from_(bucket).create_signed_url(storage_path, segundos)
    return firmada.get("signedURL") or firmada.get("signed_url")
    
