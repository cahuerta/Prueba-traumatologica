"""
routers/clases_formales_sesiones.py
Ciclo de creacion de una sesion de Clases Formales -tabla separada de
sesiones_vivo (casos clinicos), por la misma decision de "duplicar en
vez de compartir" que se aplico a paginas_clase, preguntas_anonimas,
etc-. Cero riesgo de tocar lo que ya funciona en produccion.

Esta es la fila que el supraselector (a definir, tambien backend) usara
para decidir hacia donde mandar una sesion activa: si el codigo_acceso
existe en sesiones_vivo -> caso clinico. Si existe en sesiones_clase ->
Clases Formales.

Tabla usada: sesiones_clase (a crear al final, junto con el resto del
esquema de Clases Formales).
  id              uuid
  nombre          text
  codigo_acceso   text (unico, corto, lo usan alumnos para entrar via QR/link)
  estado          text  ("preparacion" | "activa" | "cerrada")
  created_at      timestamptz
"""

import random
import string

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel

from routers.auth import sb, get_current_interrogador

router = APIRouter(prefix="/clases-formales/sesiones", tags=["clases-formales-sesiones"])


# ---------------- MODELOS ----------------
class SesionIn(BaseModel):
    nombre: str


# ---------------- HELPER ----------------
def _generar_codigo_acceso() -> str:
    """Codigo corto tipo el que ya usan las sesiones de casos clinicos
    -letras mayusculas y numeros, facil de mostrar en QR y de teclear
    manualmente si hace falta-."""
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=6))


# ---------------- ENDPOINTS ----------------
@router.post("")
def crear_sesion(body: SesionIn, interrogador: dict = Depends(get_current_interrogador)):
    """Crea una sesion nueva de Clases Formales, en estado 'preparacion'
    -el interrogador arma sus paginas antes de activarla-."""
    codigo = _generar_codigo_acceso()

    res = sb.table("sesiones_clase").insert({
        "nombre": body.nombre.strip(),
        "codigo_acceso": codigo,
        "estado": "preparacion",
    }).execute()

    return res.data[0]


@router.get("")
def listar_sesiones(interrogador: dict = Depends(get_current_interrogador)):
    """Lista todas las sesiones de Clases Formales, mas recientes primero."""
    return (
        sb.table("sesiones_clase")
        .select("*")
        .order("created_at", desc=True)
        .execute()
        .data
    )


@router.patch("/{sesion_id}/activar")
def activar_sesion(sesion_id: str, interrogador: dict = Depends(get_current_interrogador)):
    """Pasa la sesion a estado 'activa' -recien ahi los alumnos pueden
    entrar con el codigo de acceso y empezar a interactuar."""
    res = sb.table("sesiones_clase").update({"estado": "activa"}).eq("id", sesion_id).execute()
    if not res.data:
        raise HTTPException(404, "Sesion no encontrada")

    return res.data[0]


@router.patch("/{sesion_id}/cerrar")
def cerrar_sesion(sesion_id: str, interrogador: dict = Depends(get_current_interrogador)):
    """Cierra la sesion -los alumnos ya no pueden entrar ni interactuar."""
    res = sb.table("sesiones_clase").update({"estado": "cerrada"}).eq("id", sesion_id).execute()
    if not res.data:
        raise HTTPException(404, "Sesion no encontrada")

    return res.data[0]
  
