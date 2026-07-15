"""
routers/examen.py
Rendición del examen: genera las preguntas al azar por cuota de
complejidad según el paquete elegido en la encuesta, recibe las
respuestas (el timer arranca con la primera), y calcula el puntaje
y la nota final al cerrar.
"""

import random
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from routers.auth import sb

router = APIRouter(prefix="/examen", tags=["examen"])

# ---------------- PAQUETES (presets fijos de la encuesta) ----------------
PAQUETES = {
    "agil":     {"n": {"basica": 65, "intermedia": 10, "compleja": 5},  "minutos": 50},
    "estandar": {"n": {"basica": 30, "intermedia": 20, "compleja": 10}, "minutos": 70},
    "exigente": {"n": {"basica": 15, "intermedia": 15, "compleja": 10}, "minutos": 90},
}
PESO_BASE = {"basica": 1, "intermedia": 2, "compleja": 3}


# ---------------- MODELOS ----------------
class IniciarExamenIn(BaseModel):
    sesion_id: str
    alumno_id: str

class ResponderIn(BaseModel):
    pregunta_id: str
    opcion_elegida: int


# ---------------- HELPERS ----------------
def _seleccionar_preguntas(paquete: str):
    cuotas = PAQUETES[paquete]["n"]
    seleccion = {}
    for complejidad, cantidad in cuotas.items():
        disponibles = sb.table("banco_preguntas").select("id").eq("activo", True).eq("complejidad", complejidad).execute().data
        ids = [r["id"] for r in disponibles]
        if len(ids) < cantidad:
            raise HTTPException(409, f"No hay suficientes preguntas '{complejidad}' en el banco ({len(ids)}/{cantidad})")
        seleccion[complejidad] = random.sample(ids, cantidad)
    return seleccion

def _calcular_puntos(seleccion: dict):
    total_peso = sum(PESO_BASE[c] * len(ids) for c, ids in seleccion.items())
    escala = 100 / total_peso
    puntos = {}
    for complejidad, ids in seleccion.items():
        valor = round(PESO_BASE[complejidad] * escala, 4)
        for qid in ids:
            puntos[qid] = valor
    return puntos


# ---------------- ENDPOINTS (públicos, los usa el alumno) ----------------
@router.post("/iniciar")
def iniciar_examen(body: IniciarExamenIn):
    sesion = sb.table("sesiones_examen").select("*").eq("id", body.sesion_id).single().execute().data
    if not sesion or sesion["estado"] != "en_curso":
        raise HTTPException(409, "La sesión no está en curso todavía")

    existente = sb.table("examen_instancia").select("*").eq("sesion_id", body.sesion_id).eq("alumno_id", body.alumno_id).execute().data
    if existente:
        instancia = existente[0]
    else:
        paquete = sesion["paquete_elegido"]
        seleccion = _seleccionar_preguntas(paquete)
        puntos = _calcular_puntos(seleccion)
        pregunta_ids = [qid for ids in seleccion.values() for qid in ids]
        random.shuffle(pregunta_ids)
        res = sb.table("examen_instancia").insert({
            "sesion_id": body.sesion_id, "alumno_id": body.alumno_id, "paquete": paquete,
            "pregunta_ids": pregunta_ids, "puntos_por_pregunta": puntos,
        }).execute()
        instancia = res.data[0]

    preguntas = sb.table("banco_preguntas").select("id, region, pregunta, opciones").in_("id", instancia["pregunta_ids"]).execute().data
    orden = {qid: i for i, qid in enumerate(instancia["pregunta_ids"])}
    preguntas.sort(key=lambda q: orden[q["id"]])

    return {
        "instancia_id": instancia["id"],
        "paquete": instancia["paquete"],
        "minutos_totales": PAQUETES[instancia["paquete"]]["minutos"],
        "iniciado_at": instancia["iniciado_at"],
        "preguntas": preguntas,
    }

@router.post("/{instancia_id}/responder")
def responder(instancia_id: str, body: ResponderIn):
    instancia = sb.table("examen_instancia").select("*").eq("id", instancia_id).single().execute().data
    if not instancia:
        raise HTTPException(404, "Examen no encontrado")
    if instancia["finalizado_at"]:
        raise HTTPException(409, "El examen ya fue finalizado")

    if not instancia["iniciado_at"]:
        sb.table("examen_instancia").update({"iniciado_at": datetime.now(timezone.utc).isoformat()}).eq("id", instancia_id).execute()

    pregunta = sb.table("banco_preguntas").select("correcta").eq("id", body.pregunta_id).single().execute().data
    correcta = pregunta["correcta"] == body.opcion_elegida

    sb.table("respuestas").upsert({
        "examen_instancia_id": instancia_id, "pregunta_id": body.pregunta_id,
        "opcion_elegida": body.opcion_elegida, "correcta": correcta,
    }).execute()
    return {"correcta": correcta}

@router.post("/{instancia_id}/finalizar")
def finalizar_examen(instancia_id: str):
    instancia = sb.table("examen_instancia").select("*").eq("id", instancia_id).single().execute().data
    if not instancia:
        raise HTTPException(404, "Examen no encontrado")

    respuestas = sb.table("respuestas").select("pregunta_id, correcta").eq("examen_instancia_id", instancia_id).execute().data
    puntos_map = instancia["puntos_por_pregunta"]
    puntaje = sum(puntos_map[r["pregunta_id"]] for r in respuestas if r["correcta"])
    porcentaje = puntaje / 100

    if porcentaje <= 0.6:
        nota = 1 + (porcentaje / 0.6) * 3
    else:
        nota = 4 + ((porcentaje - 0.6) / 0.4) * 3

    sb.table("examen_instancia").update({
        "finalizado_at": datetime.now(timezone.utc).isoformat(),
        "puntaje_total": round(puntaje, 2),
        "porcentaje": round(porcentaje * 100, 1),
        "nota": round(nota, 1),
    }).eq("id", instancia_id).execute()

    return {"puntaje_total": round(puntaje, 2), "porcentaje": round(porcentaje * 100, 1), "nota": round(nota, 1)}
      
