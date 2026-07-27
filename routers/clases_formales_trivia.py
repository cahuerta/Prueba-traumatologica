"""
routers/clases_formales_trivia.py
Herramienta "trivia" de Clases Formales: el interrogador arma la
pregunta y alternativas de antemano dentro de la pagina (paginas_clase.config
= {pregunta, alternativas: [...], correcta: indice}), el alumno responde
en vivo solo con una LETRA (A-E) desde un selector fijo -nunca ve la
pregunta ni las alternativas, su pantalla no depende de la pagina
actual-. El interrogador ve el conteo en vivo y decide cuando revelar.

Efimero -igual que el semaforo, nunca se persiste a disco ni a
Supabase-. Aislado por PAGINA (pagina_id) -a diferencia del semaforo,
que es continuo por sesion completa-: cada pregunta de trivia es
especifica de su pagina, no tiene sentido que se mezcle con la trivia
de otra. Por estar aislada por pagina, no necesita reinicio explicito
del admin -cada pagina nueva ya empieza vacia sola, imposible que se
mezcle con la trivia de otra pagina.

Sin identidad de alumno en los resultados -mismo criterio que preguntas
y semaforo-. El alumno NO tiene login propio: valida su RUT contra el
conjunto activo en cada request.
"""

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel

from routers.auth import sb, get_current_interrogador
from routers.conjuntos_comun import obtener_conjunto_activo_id
from services.cache_trivia_clases_formales import (
    responder_trivia,
    obtener_resultado_trivia,
    revelar_trivia,
    obtener_mi_resultado,
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
@router.get("/{pagina_id}/resultado")
def resultado(pagina_id: str, interrogador: dict = Depends(get_current_interrogador)):
    """Conteo en vivo por letra, total de respuestas, y si ya se
    revelo. Sin identidad de alumno."""
    return obtener_resultado_trivia(pagina_id)


@router.patch("/{pagina_id}/revelar")
def revelar(pagina_id: str, interrogador: dict = Depends(get_current_interrogador)):
    """Marca la trivia de esta pagina como revelada -desde aca la
    proyeccion puede mostrar la letra correcta junto al conteo, y el
    alumno puede consultar si acerto."""
    revelar_trivia(pagina_id)
    return {"ok": True}


# ---------------- ENDPOINTS PUBLICOS (alumno) ----------------
@router.post("/{pagina_id}/responder")
def responder(pagina_id: str, body: ResponderIn):
    """El alumno responde con su letra, desde el selector fijo -sin
    saber a que pregunta corresponde."""
    letra = body.letra.strip().upper()
    if letra not in LETRAS_VALIDAS:
        raise HTTPException(400, "Letra invalida, debe ser A-E")

    alumno_id = _validar_alumno(body.rut)
    responder_trivia(pagina_id, alumno_id, letra)

    return {"ok": True}


@router.get("/{pagina_id}/mi-respuesta")
def mi_respuesta(pagina_id: str, rut: str):
    """La letra que el propio alumno respondio -para que sepa que
    contesto una vez que se revela. No compara contra la correcta, eso
    lo hace el frontend con el config de la pagina."""
    alumno_id = _validar_alumno(rut)
    return {"letra": obtener_mi_resultado(pagina_id, alumno_id)}
    
