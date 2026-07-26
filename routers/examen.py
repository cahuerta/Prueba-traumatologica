"""
routers/examen.py
Rendición del examen: genera las preguntas al azar por cuota de
complejidad según el paquete elegido en la encuesta, mezcla el orden
de las alternativas por alumno (evita copia por letra), recibe las
respuestas (el timer arranca con la primera), y calcula el puntaje
y la nota final al cerrar.

Ademas del banco de preguntas individuales, cada examen agrega una
seccion final de CASOS_CLINICOS_CANTIDAD casos clinicos completos (los
mismos usados en Presentacion en vivo): el bloque de preguntas de cada
caso se mantiene INTACTO y en su orden original (1-5) para dar contexto
-nunca se mezcla el orden entre preguntas de un mismo caso-, solo se
mezclan las alternativas de cada pregunta individual, igual que las del
banco. Cada pregunta de caso vale lo mismo que una pregunta "basica":
se re-normalizan todas juntas a 100 (no son puntos bono aparte).
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
PESO_BASE = {"basica": 1, "intermedia": 2, "compleja": 3, "caso_clinico": 1}

CASOS_CLINICOS_CANTIDAD = 4


# ---------------- MODELOS ----------------
class IniciarExamenIn(BaseModel):
    sesion_id: str
    alumno_id: str

class ResponderIn(BaseModel):
    pregunta_id: str
    opcion_elegida: int  # posición mostrada al alumno (ya mezclada), no el índice original


def _conjunto_activo_es_test() -> bool:
    """Si el conjunto activo es de tipo 'test', el examen se arma con
    cuotas flexibles (lo que haya disponible) en vez de exigir el banco
    completo -sirve para probar el flujo sin esperar tener las 200
    preguntas y los 4 casos clinicos completos."""
    fila = sb.table("conjuntos").select("tipo").eq("activo", True).execute().data
    return bool(fila) and fila[0]["tipo"] == "test"


# ---------------- HELPERS: preguntas individuales (banco) ----------------
def _seleccionar_preguntas(paquete: str, flexible: bool = False):
    cuotas = PAQUETES[paquete]["n"]
    seleccion = {}
    for complejidad, cantidad in cuotas.items():
        disponibles = sb.table("banco_preguntas").select("id").eq("activo", True).eq("complejidad", complejidad).execute().data
        ids = [r["id"] for r in disponibles]
        if flexible:
            cantidad = min(cantidad, len(ids))
        elif len(ids) < cantidad:
            raise HTTPException(409, f"No hay suficientes preguntas '{complejidad}' en el banco ({len(ids)}/{cantidad})")
        seleccion[complejidad] = random.sample(ids, cantidad) if cantidad else []
    return seleccion

def _calcular_puntos(seleccion: dict):
    """Recibe un dict {clave_de_peso: [ids]} -incluye tanto complejidades
    del banco como 'caso_clinico'- y re-normaliza TODO junto a 100."""
    total_peso = sum(PESO_BASE[c] * len(ids) for c, ids in seleccion.items())
    if total_peso == 0:
        return {}
    escala = 100 / total_peso
    puntos = {}
    for clave, ids in seleccion.items():
        valor = round(PESO_BASE[clave] * escala, 4)
        for qid in ids:
            puntos[qid] = valor
    return puntos

def _generar_orden_opciones(pregunta_ids: list, n_opciones: int = 5):
    """Por cada pregunta, un orden mezclado distinto. orden[pregunta_id][pos_mostrada] = índice_original."""
    orden = {}
    for qid in pregunta_ids:
        indices = list(range(n_opciones))
        random.shuffle(indices)
        orden[qid] = indices
    return orden


# ---------------- HELPERS: seccion de casos clinicos ----------------
def _seleccionar_casos_clinicos(cantidad: int = CASOS_CLINICOS_CANTIDAD, flexible: bool = False):
    """Elige 'cantidad' casos clinicos al azar (de los que ya tienen
    preguntas guardadas) para la seccion final del examen. Cada bloque
    se devuelve con sus preguntas YA EN SU ORDEN ORIGINAL (1-5): el
    contexto del caso se mantiene intacto, nunca se mezcla el orden de
    las preguntas dentro de un mismo caso."""
    filas = sb.table("caso_preguntas").select("caso_id").execute().data
    caso_ids_disponibles = list({f["caso_id"] for f in filas})

    if flexible:
        cantidad = min(cantidad, len(caso_ids_disponibles))
    elif len(caso_ids_disponibles) < cantidad:
        raise HTTPException(409, f"No hay suficientes casos clínicos con preguntas guardadas ({len(caso_ids_disponibles)}/{cantidad})")

    if cantidad == 0:
        return []

    elegidos = random.sample(caso_ids_disponibles, cantidad)

    bloques = []
    for caso_id in elegidos:
        preguntas = sb.table("caso_preguntas").select(
            "id, orden, pregunta, opciones, correcta, media_url, media_tipo"
        ).eq("caso_id", caso_id).order("orden").execute().data
        if preguntas:
            bloques.append(preguntas)
    return bloques

def _info_pregunta(pregunta_id: str):
    """La pregunta puede venir del banco o de un caso clinico -viven en
    tablas distintas, y cada una anota su respuesta en una tabla de
    respuestas separada (respuestas / respuestas_caso), cada cual con
    su propia foreign key estricta-. Devuelve (origen, indice_correcto)."""
    fila = sb.table("banco_preguntas").select("correcta").eq("id", pregunta_id).execute().data
    if fila:
        return "banco", fila[0]["correcta"]
    fila = sb.table("caso_preguntas").select("correcta").eq("id", pregunta_id).execute().data
    if fila:
        return "caso", fila[0]["correcta"]
    raise HTTPException(404, "Pregunta no encontrada")


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
        modo_flexible = _conjunto_activo_es_test()

        seleccion = _seleccionar_preguntas(paquete, flexible=modo_flexible)
        pregunta_ids = [qid for ids in seleccion.values() for qid in ids]
        random.shuffle(pregunta_ids)

        # Seccion final: bloques de casos clinicos completos, agregados
        # DESPUES de las preguntas individuales. Cada bloque va intacto
        # y en orden; el orden ENTRE bloques (cual caso va primero) si
        # queda al azar, segun la seleccion de _seleccionar_casos_clinicos.
        bloques_casos = _seleccionar_casos_clinicos(flexible=modo_flexible)
        caso_pregunta_ids = [p["id"] for bloque in bloques_casos for p in bloque]
        pregunta_ids += caso_pregunta_ids

        seleccion_total = {**seleccion, "caso_clinico": caso_pregunta_ids}
        puntos = _calcular_puntos(seleccion_total)

        orden_opciones = _generar_orden_opciones(pregunta_ids)

        res = sb.table("examen_instancia").insert({
            "sesion_id": body.sesion_id, "alumno_id": body.alumno_id, "paquete": paquete,
            "pregunta_ids": pregunta_ids, "puntos_por_pregunta": puntos, "orden_opciones": orden_opciones,
        }).execute()
        instancia = res.data[0]

    todos_ids = instancia["pregunta_ids"]

    filas_banco = sb.table("banco_preguntas").select(
        "id, region, pregunta, opciones, media_url, media_tipo"
    ).in_("id", todos_ids).execute().data

    filas_caso = sb.table("caso_preguntas").select(
        "id, pregunta, opciones, media_url, media_tipo, caso_id"
    ).in_("id", todos_ids).execute().data

    # Info del caso clinico (titulo/vineta/imagen) para que el alumno vea
    # el contexto antes de la primera pregunta de cada bloque -igual que
    # en Presentacion en vivo-. Se adjunta a CADA pregunta del bloque
    # (no solo la primera); el frontend decide cuando mostrar la vineta
    # comparando el caso de la pregunta actual con la anterior.
    caso_ids_usados = list({f["caso_id"] for f in filas_caso})
    casos_info = {}
    if caso_ids_usados:
        filas_casos_info = sb.table("casos_clinicos").select(
            "id, titulo, vineta_clinica, media_url, media_tipo"
        ).in_("id", caso_ids_usados).execute().data
        casos_info = {c["id"]: c for c in filas_casos_info}

    for f in filas_caso:
        f["region"] = None
        f["caso"] = casos_info.get(f["caso_id"])
        f.pop("caso_id", None)

    preguntas = filas_banco + filas_caso

    orden_lista = {qid: i for i, qid in enumerate(todos_ids)}
    preguntas.sort(key=lambda q: orden_lista[q["id"]])

    orden_opciones = instancia["orden_opciones"] or {}
    minutos_totales = PAQUETES[instancia["paquete"]]["minutos"]
    for q in preguntas:
        mapeo = orden_opciones.get(q["id"])
        if mapeo:
            q["opciones"] = [q["opciones"][i] for i in mapeo]
        if q.get("media_url"):
            firmada = sb.storage.from_("preguntas").create_signed_url(
                q["media_url"], minutos_totales * 60 + 300
            )
            q["media_url"] = firmada.get("signedURL") or firmada.get("signed_url")
        if q.get("caso") and q["caso"].get("media_url"):
            firmada_caso = sb.storage.from_("casos").create_signed_url(
                q["caso"]["media_url"], minutos_totales * 60 + 300
            )
            q["caso"]["media_url"] = firmada_caso.get("signedURL") or firmada_caso.get("signed_url")

    return {
        "instancia_id": instancia["id"],
        "paquete": instancia["paquete"],
        "minutos_totales": minutos_totales,
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

    origen, correcta_idx = _info_pregunta(body.pregunta_id)

    # La opción elegida viene en la posición MOSTRADA (mezclada); se traduce al índice original.
    mapeo = (instancia["orden_opciones"] or {}).get(body.pregunta_id)
    opcion_original = mapeo[body.opcion_elegida] if mapeo else body.opcion_elegida

    correcta = correcta_idx == opcion_original

    tabla = "respuestas" if origen == "banco" else "respuestas_caso"
    sb.table(tabla).upsert({
        "examen_instancia_id": instancia_id, "pregunta_id": body.pregunta_id,
        "opcion_elegida": opcion_original, "correcta": correcta,
    }).execute()
    return {"correcta": correcta}

@router.post("/{instancia_id}/finalizar")
def finalizar_examen(instancia_id: str):
    instancia = sb.table("examen_instancia").select("*").eq("id", instancia_id).single().execute().data
    if not instancia:
        raise HTTPException(404, "Examen no encontrado")

    respuestas_banco = sb.table("respuestas").select("pregunta_id, correcta").eq("examen_instancia_id", instancia_id).execute().data
    respuestas_caso = sb.table("respuestas_caso").select("pregunta_id, correcta").eq("examen_instancia_id", instancia_id).execute().data
    respuestas = respuestas_banco + respuestas_caso
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


# ---------------- DETECCIÓN DE SALIDA (cambio de app/pestaña durante el examen) ----------------
MAX_SALIDAS = 3

@router.post("/{instancia_id}/registrar-salida")
def registrar_salida(instancia_id: str):
    instancia = sb.table("examen_instancia").select("*").eq("id", instancia_id).single().execute().data
    if not instancia:
        raise HTTPException(404, "Examen no encontrado")
    if instancia["finalizado_at"]:
        raise HTTPException(409, "El examen ya fue finalizado")

    sb.table("intentos_salida").insert({"examen_instancia_id": instancia_id}).execute()
    nuevo_conteo = instancia["salidas_detectadas"] + 1
    sb.table("examen_instancia").update({"salidas_detectadas": nuevo_conteo}).eq("id", instancia_id).execute()

    if nuevo_conteo >= MAX_SALIDAS:
        resultado = finalizar_examen(instancia_id)
        return {"salidas": nuevo_conteo, "max_salidas": MAX_SALIDAS, "finalizado": True, "resultado": resultado}

    return {"salidas": nuevo_conteo, "max_salidas": MAX_SALIDAS, "finalizado": False}
    
