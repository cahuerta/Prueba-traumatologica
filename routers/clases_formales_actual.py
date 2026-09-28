"""
routers/clases_formales_actual.py
Lectura publica (sin auth) de "cual pagina esta activa ahora mismo" en
una sesion de Clases Formales en vivo.

La consultan cada 2 s la pantalla de proyeccion (para renderizar el
contenido), el control remoto del interrogador (para saber que esta
mostrando la proyeccion) y el telefono de cada alumno (solo para saber
si la pagina activa es una trivia y mostrarle las alternativas).

CACHE: con 90 alumnos esto son ~45 lecturas por segundo. Antes cada una
hacia 2 consultas a Supabase, lo que disparaba el bug de concurrencia
HTTP/2 bajo carga: fallaban lecturas y la proyeccion caia al QR por un
instante. Ahora se sirve desde memoria (services/cache_clases_formales.py,
mismo patron que cache_vivo.py de Casos Clinicos): la sesion y las
paginas se cargan UNA vez y se actualizan solo cuando algo cambia de
verdad (avanzar, retroceder, cerrar, editar paginas).

Aca viven tambien los cargadores compartidos (sesion por codigo, sesion
por id, paginas de un contenido) que usan clases_formales_sesiones.py y
clases_formales_ingreso.py.

Separado de clases_formales_sesiones.py -que es 100% interrogador con
auth- siguiendo el mismo patron de casos_vivo_profesor.py (control) vs
casos_vivo_alumno.py (publico) que ya existe en el proyecto.
"""

from fastapi import APIRouter, HTTPException

from routers.auth import sb
from services import cache_clases_formales as cache

router = APIRouter(prefix="/clases-formales/actual", tags=["clases-formales-actual"])


# ---------------- CARGADORES (memoria primero, Supabase una sola vez) ----------------
def sesion_por_codigo(codigo: str):
    """Fila de sesiones_clase para ese codigo, o None si no existe.
    Un codigo inexistente no se guarda: se vuelve a consultar."""
    def cargar():
        filas = sb.table("sesiones_clase").select("*").eq("codigo_acceso", codigo).execute().data
        if not filas:
            return None
        cache.guardar_sesion(filas[0])
        return cache.obtener_sesion(filas[0]["id"])

    return cache.cargar_una_vez(lambda: cache.obtener_sesion_por_codigo(codigo), cargar)


def sesion_por_id(sesion_id: str):
    """Fila de sesiones_clase por id, o None si no existe."""
    def cargar():
        filas = sb.table("sesiones_clase").select("*").eq("id", sesion_id).execute().data
        if not filas:
            return None
        cache.guardar_sesion(filas[0])
        return cache.obtener_sesion(sesion_id)

    return cache.cargar_una_vez(lambda: cache.obtener_sesion(sesion_id), cargar)


def paginas_de(clase_formal_id: str) -> list:
    """Paginas del contenido ordenadas por 'orden' (lista vacia si no tiene)."""
    def cargar():
        filas = (
            sb.table("paginas_clase")
            .select("id, clase_formal_id, orden, titulo, tipo_herramienta, config")
            .eq("clase_formal_id", clase_formal_id)
            .order("orden")
            .execute()
            .data
        )
        cache.guardar_paginas(clase_formal_id, filas)
        return cache.obtener_paginas(clase_formal_id)

    return cache.cargar_una_vez(lambda: cache.obtener_paginas(clase_formal_id), cargar)


# ---------------- ENDPOINT ----------------
@router.get("/{codigo}")
def pagina_actual(codigo: str):
    """Resuelve por codigo_acceso y devuelve la pagina activa completa
    (id, titulo, tipo_herramienta, config). 404 si el codigo no existe o
    si la sesion aun no tiene ninguna pagina activa (no se ha activado, o
    no tiene paginas creadas). Mismos mensajes que antes: el frontend
    distingue estos 404 (mostrar el QR) de un fallo transitorio."""
    sesion = sesion_por_codigo(codigo)
    if not sesion:
        raise HTTPException(404, "Codigo de sesion invalido")

    orden = sesion.get("pagina_actual_orden")
    if orden is None:
        raise HTTPException(404, "La sesion aun no tiene una pagina activa")

    for pagina in paginas_de(sesion["clase_formal_id"]):
        if pagina["orden"] == orden:
            return {
                "id": pagina["id"],
                "titulo": pagina["titulo"],
                "tipo_herramienta": pagina["tipo_herramienta"],
                "config": pagina["config"],
            }

    raise HTTPException(404, "Pagina activa no encontrada")
