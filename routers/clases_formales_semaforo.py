"""
routers/clases_formales_semaforo.py
Herramienta "semaforo" de Clases Formales: el alumno responde una
pregunta binaria ("sigo?" si/no), el interrogador ve el resultado
agregado en vivo (color verde/amarillo/rojo segun % de "si").

Continuo por SESION completa -no por pagina-: el alumno responde una
sola vez y su respuesta se mantiene vigente durante toda la clase, sin
reiniciarse al cambiar de pagina.

Sin persistencia -ni disco ni Supabase-: es una lectura del momento,
vive solo en memoria (services/cache_clases_formales.py).

El alumno NO tiene login propio -mismo patron que preguntas y votos-:
valida su RUT contra el conjunto activo en cada request.
"""

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel

from routers.auth import sb, get_current_interrogador
from routers.conjuntos_comun import obtener_conjunto_activo_id
from services.cache_clases_formales import responder_semaforo, obtener_resultado_semaforo

router = APIRouter(prefix="/clases-formales/semaforo", tags=["clases-formales-semaforo"])


# ---------------- MODELOS ----------------
class ResponderIn(BaseModel):
    rut: str
    sigo: bool


# ---------------- HELPER: validar alumno contra el conjunto activo ----------------
def _validar_alumno(rut: str) -> str:
    """Devuelve el alumno_id si el RUT pertenece al conjunto activo.
    Lanza 403 si no -mismo criterio que en preguntas anonimas y en
    ingreso_alumno_vivo-."""
    conjunto_id = obtener_conjunto_activo_id()
    alumno = (
        sb.table("alumnos")
        .select("id")
        .eq("rut", rut.strip())
        .eq("conjunto_id", conjunto_id)
        .execute()
        .data
    )
    if not alumno:
        raise HTTPException(403, "RUT no reconocido en el conjunto activo")
    return alumno[0]["id"]


# ---------------- ENDPOINT PUBLICO (alumno) ----------------
@router.post("/{sesion_id}/responder")
def responder(sesion_id: str, body: ResponderIn):
    """El alumno responde (o cambia) su estado para la sesion completa."""
    alumno_id = _validar_alumno(body.rut)
    responder_semaforo(sesion_id, alumno_id, body.sigo)

    return {"ok": True}


# ---------------- ENDPOINT DEL INTERROGADOR ----------------
@router.get("/{sesion_id}/resultado")
def resultado(sesion_id: str, interrogador: dict = Depends(get_current_interrogador)):
    """Resultado agregado en vivo para la proyeccion/panel del
    interrogador: total de respuestas, % que dijo 'si', y el color
    correspondiente. Sin identidad de alumno."""
    return obtener_resultado_semaforo(sesion_id)
    
