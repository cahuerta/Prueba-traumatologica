"""
routers/clases_formales_trivia.py
Herramienta "trivia" de Clases Formales: el interrogador arma la
pregunta y alternativas de antemano dentro de la pagina (paginas_clase.config
= {pregunta, alternativas: [...], correcta: indice}), el alumno responde
en vivo solo con una LETRA (A-E) desde un selector fijo -nunca ve la
pregunta ni las alternativas, su pantalla no depende de la pagina
actual-. El interrogador ve el conteo en vivo y decide cuando revelar.

Efimero -igual que el semaforo, nunca se persiste a disco ni a
Supabase-. A diferencia del semaforo (continuo, nunca se limpia), la
trivia necesita reinicio EXPLICITO del admin antes de cada pregunta
nueva, porque el alumno no sabe en que pagina va la clase y su voto
siempre llega al mismo lugar (la sesion) sin importar que trivia este
activa.

Sin identidad de alumno en los resultados -mismo criterio que preguntas
y semaforo-. El alumno NO tiene login propio: valida su RUT contra el
conjunto activo en cada request.
"""

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel

from routers.auth import sb, get_current_interrogador
from routers.conjuntos_comun import obtener_conjunto_activo_id
from services.cache_trivia_clases_formales import (
    iniciar_trivia,
    responder_trivia,
    obtener_resultado_trivia,
    revelar_trivia,
)

router = APIRouter(prefix="/clases-formales/trivia", tags=["clases-formales-trivia"])

LETRAS_VALIDAS = {"A", "B", "C", "D", "E"}


# ---------------- MODELOS ----------------
class ResponderIn(BaseModel):
    rut: str
    letra: str


# ---------------- HELPER: validar alumno contra el conjunto activo ----------------
def _validar_alumno(rut: str) -> str:
    """Devuelve el alumno_id si el RUT pertenece al conjunto activo.
    Lanza 403 si no -mismo criterio que en preguntas anonimas y en
    semaforo-."""
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


# ---------------- ENDPOINTS DEL INTERROGADOR ----------------
@router.patch("/{sesion_id}/iniciar")
def iniciar(sesion_id: str, interrogador: dict = Depends(get_current_interrogador)):
    """Reinicia el conteo -a usar justo antes de pedirle a la sala que
    vote la pregunta de la pagina actual. Limpia cualquier voto que
    haya quedado de una trivia anterior en la misma sesion."""
    iniciar_trivia(sesion_id)
    return {"ok": True}


@router.get("/{sesion_id}/resultado")
def resultado(sesion_id: str, interrogador: dict = Depends(get_current_interrogador)):
    """Conteo en vivo por letra, total de respuestas, y si ya se
    revelo. Sin identidad de alumno."""
    return obtener_resultado_trivia(sesion_id)


@router.patch("/{sesion_id}/revelar")
def revelar(sesion_id: str, interrogador: dict = Depends(get_current_interrogador)):
    """Marca la trivia actual como revelada -desde aca la proyeccion
    puede mostrar la letra correcta junto al conteo."""
    revelar_trivia(sesion_id)
    return {"ok": True}


# ---------------- ENDPOINT PUBLICO (alumno) ----------------
@router.post("/{sesion_id}/responder")
def responder(sesion_id: str, body: ResponderIn):
    """El alumno responde con su letra, desde el selector fijo -sin
    saber a que pregunta corresponde. Si vota antes de que el admin
    inicie la trivia, el voto igual se registra (queda vigente para la
    proxima vez que se inicie)."""
    letra = body.letra.strip().upper()
    if letra not in LETRAS_VALIDAS:
        raise HTTPException(400, "Letra invalida, debe ser A-E")

    alumno_id = _validar_alumno(body.rut)
    responder_trivia(sesion_id, alumno_id, letra)

    return {"ok": True}
