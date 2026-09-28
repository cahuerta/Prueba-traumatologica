"""
routers/clases_formales_avatar.py
Avatar que responde en la proyeccion las preguntas anonimas de los alumnos
en Clases Formales.

Flujo:
  1. El interrogador toca "Preparar" en una pregunta (decide que la responde
     el avatar). La respuesta se prepara en segundo plano.
  2. Cuando esta lista, toca "Proyectar avatar": la pregunta queda marcada
     como respondida y la proyeccion la muestra y la dice a pantalla completa.

Estado guardado SOLO en disco (services/avatar_local.py), no en Supabase.
Las preguntas se leen de services/preguntas_local.py sin modificarlo.

  POST /clases-formales/avatar/{pregunta_id}/preparar?sesion_id=   (interrogador)
  GET  /clases-formales/avatar/estados/{sesion_id}                 (interrogador)
  POST /clases-formales/avatar/{pregunta_id}/proyectar?sesion_id=  (interrogador)
  GET  /clases-formales/avatar/actual/{sesion_id}                  (publico, proyeccion)
"""

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

from routers.auth import get_current_interrogador
from services import avatar_local
from services.avatar_clase import preparar_respuesta
from services.preguntas_local import listar_preguntas, marcar_respondida

router = APIRouter(prefix="/clases-formales/avatar", tags=["clases-formales-avatar"])


def _texto_pregunta(sesion_id: str, pregunta_id: str) -> str:
    pregunta = next((p for p in listar_preguntas(sesion_id) if p["id"] == pregunta_id), None)
    if not pregunta:
        raise HTTPException(404, "Pregunta no encontrada")
    return pregunta["texto"]


def _preparar_en_segundo_plano(sesion_id: str, pregunta_id: str, texto: str) -> None:
    try:
        r = preparar_respuesta(texto)
    except Exception as e:  # noqa: BLE001 — la tarea nunca debe quedar en "preparando"
        print(f"[avatar] error preparando {pregunta_id}: {e!r}")
        r = {"estado": "error", "respuesta": "", "fuente": "ninguna", "region": None}
    avatar_local.guardar_resultado(sesion_id, pregunta_id, r["estado"], r["respuesta"], r["fuente"], r["region"])


@router.post("/{pregunta_id}/preparar")
def preparar(
    pregunta_id: str,
    sesion_id: str,
    tareas: BackgroundTasks,
    interrogador: dict = Depends(get_current_interrogador),
):
    texto = _texto_pregunta(sesion_id, pregunta_id)
    if avatar_local.marcar_preparando(sesion_id, pregunta_id, texto):
        tareas.add_task(_preparar_en_segundo_plano, sesion_id, pregunta_id, texto)
    return {"ok": True, "estado": avatar_local.estados(sesion_id).get(pregunta_id)}


@router.get("/estados/{sesion_id}")
def listar_estados(sesion_id: str, interrogador: dict = Depends(get_current_interrogador)):
    """{pregunta_id: preparando | lista | no_pertinente | error}"""
    return avatar_local.estados(sesion_id)


@router.post("/{pregunta_id}/proyectar")
def proyectar(pregunta_id: str, sesion_id: str, interrogador: dict = Depends(get_current_interrogador)):
    activo = avatar_local.proyectar(sesion_id, pregunta_id)
    if not activo:
        raise HTTPException(409, "La respuesta del avatar aún no está lista")
    marcar_respondida(sesion_id, pregunta_id)
    return {"ok": True}


@router.get("/actual/{sesion_id}")
def actual(sesion_id: str):
    """Lectura publica y liviana (solo disco) para la proyeccion: lo que el avatar
    debe decir ahora, o null. La proyeccion lo dice una vez por cada 'id'."""
    return {"activo": avatar_local.activo(sesion_id)}
  
