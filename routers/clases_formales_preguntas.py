"""
routers/clases_formales_preguntas.py
Preguntas anonimas de una sesion de Clases Formales -capa base, siempre
abierta durante toda la sesion (no depende del constructor de paginas).

El alumno NO tiene login/token propio -mismo patron que casos_vivo_alumno.py-:
cada request publico valida el RUT directo contra el conjunto de alumnos
activo (obtener_conjunto_activo_id). El id del alumno se guarda para
anti-doble-upvote, pero nunca se expone en las respuestas.

El interrogador si usa JWT (get_current_interrogador) para listar y
marcar preguntas como respondidas.

Persistencia: buffer a disco (services/preguntas_local.py). El volcado a
Supabase NO depende de un evento de cierre -las preguntas estan abiertas
toda la sesion-: se aprovecha cada GET /preguntas del interrogador
(polling normal del panel) para volcar en batch lo pendiente antes de
responder.
"""

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel

from routers.auth import sb, get_current_interrogador
from routers.conjuntos_comun import obtener_conjunto_activo_id
from services.preguntas_local import (
    registrar_pregunta,
    registrar_upvote,
    marcar_respondida,
    listar_preguntas,
    volcar_pendientes_a_supabase,
)

router = APIRouter(prefix="/clases-formales/preguntas", tags=["clases-formales-preguntas"])


# ---------------- MODELOS ----------------
class PreguntarIn(BaseModel):
    sesion_id: str
    rut: str
    texto: str


class UpvoteIn(BaseModel):
    sesion_id: str
    rut: str


# ---------------- HELPER: validar alumno contra el conjunto activo ----------------
def _validar_alumno(rut: str) -> str:
    """Devuelve el alumno_id si el RUT pertenece al conjunto activo.
    Lanza 403 si no -mismo criterio que ingreso_alumno_vivo-."""
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


# ---------------- ENDPOINTS PUBLICOS (alumno) ----------------
@router.post("")
def preguntar(body: PreguntarIn):
    """El alumno manda una pregunta anonima. Se valida su RUT contra el
    conjunto activo pero su identidad nunca se devuelve ni se expone al
    interrogador."""
    if not body.texto.strip():
        raise HTTPException(400, "La pregunta no puede estar vacia")

    alumno_id = _validar_alumno(body.rut)
    pregunta_id = registrar_pregunta(body.sesion_id, alumno_id, body.texto.strip())

    return {"ok": True, "pregunta_id": pregunta_id}


@router.post("/{pregunta_id}/upvote")
def upvote(pregunta_id: str, body: UpvoteIn):
    """El alumno vota por una pregunta ya existente. Un upvote por
    alumno por pregunta -no se duplica-."""
    alumno_id = _validar_alumno(body.rut)
    ok = registrar_upvote(body.sesion_id, pregunta_id, alumno_id)

    if not ok:
        raise HTTPException(409, "Pregunta no encontrada o ya votada por este alumno")

    return {"ok": True}


# ---------------- ENDPOINTS DEL INTERROGADOR ----------------
@router.get("/{sesion_id}")
def listar(sesion_id: str, interrogador: dict = Depends(get_current_interrogador)):
    """Panel del interrogador. Devuelve las preguntas ordenadas por
    upvotes descendente, sin ningun campo de identidad de alumno.

    Aprovecha este mismo poll para volcar a Supabase en batch lo que
    aun no se ha persistido -no hay un evento de 'cierre' para las
    preguntas, estan abiertas toda la sesion-."""
    pendientes = volcar_pendientes_a_supabase(sesion_id)
    if pendientes:
        sb.table("preguntas_anonimas").insert(pendientes).execute()

    return listar_preguntas(sesion_id)


@router.patch("/{pregunta_id}/responder")
def responder(
    pregunta_id: str,
    sesion_id: str,
    interrogador: dict = Depends(get_current_interrogador),
):
    """El interrogador marca una pregunta como respondida."""
    ok = marcar_respondida(sesion_id, pregunta_id)
    if not ok:
        raise HTTPException(404, "Pregunta no encontrada")

    return {"ok": True}
  
