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

Volver a entrar (recarga, gesto "atras", se cerro el navegador): /iniciar
devuelve la MISMA instancia, con las respuestas ya guardadas traducidas a
la posicion que ve el alumno, el tiempo que le queda y las salidas ya
registradas, para que el alumno siga donde estaba. Si el examen ya fue
finalizado, devuelve solo el resultado.

Tiempo y cierre de la sesion: el examen parte para TODOS al cerrar la
encuesta (sesiones_examen.iniciada_at) y termina a la misma hora para todos
(termina_at = iniciada_at + minutos del paquete). Al cerrar la encuesta se
programa UN unico cierre para termina_at (programar_cierre_sesion): a esa
hora se finalizan los examenes que sigan abiertos, con lo respondido, y la
sesion pasa a 'finalizada'. Si todos los alumnos que ingresaron terminan
antes, la sesion se cierra al finalizar el ultimo. Si el servicio se
reinicia, al arrancar se vuelven a programar los cierres pendientes.

Conjunto: la sesion guarda el conjunto activo con que se creo
(sesiones_examen.conjunto_id); el examen usa ESE conjunto, aunque despues
se active otro.
"""

import random
import threading
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
    item: str = "clinico"  # "clinico" | "administrativo" -enviado por el frontend en el momento mismo de iniciar, no se guarda en ninguna tabla-

class ResponderIn(BaseModel):
    pregunta_id: str
    opcion_elegida: int  # posición mostrada al alumno (ya mezclada), no el índice original


def _conjunto_es_test(conjunto_id: str) -> bool:
    """Si el conjunto DE LA SESION es de tipo 'test', las cuotas del examen
    son flexibles (toma lo que haya disponible) en vez de exigir el
    banco completo -sirve para probar el flujo sin esperar tener las
    200 preguntas y los 4 casos clinicos completos. Esto es INDEPENDIENTE
    de que el contenido sea clinico o administrativo -eso lo decide
    'item', no el conjunto-. Se usa el conjunto guardado en la sesion,
    no el activo ahora: cambiar el conjunto activo no afecta una sesion
    ya creada."""
    fila = sb.table("conjuntos").select("tipo").eq("id", conjunto_id).execute().data
    return bool(fila) and fila[0]["tipo"] == "test"


def _parse_fecha(valor):
    return datetime.fromisoformat(str(valor).replace("Z", "+00:00"))


def _resultado_guardado(instancia: dict) -> dict:
    return {
        "puntaje_total": float(instancia["puntaje_total"] or 0),
        "porcentaje": float(instancia["porcentaje"] or 0),
        "nota": float(instancia["nota"] or 0),
    }


# ---------------- HELPERS: preguntas individuales (banco) ----------------
def _seleccionar_preguntas(paquete: str, item: str = "clinico", modo_test: bool = False):
    """'item' decide el CONTENIDO (region 'administrativo' o clinico
    -todo lo demas-), independiente de 'modo_test' que solo decide si
    la CUOTA es flexible o estricta. Los 4 cruces posibles: test+clinico,
    test+administrativo, oficial+clinico, oficial+administrativo."""
    cuotas = PAQUETES[paquete]["n"]
    seleccion = {}
    for complejidad, cantidad in cuotas.items():
        q = sb.table("banco_preguntas").select("id").eq("activo", True).eq("complejidad", complejidad)
        q = q.eq("region", "administrativo") if item == "administrativo" else q.neq("region", "administrativo")
        disponibles = q.execute().data
        ids = [r["id"] for r in disponibles]
        if modo_test:
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
def _seleccionar_casos_clinicos(cantidad: int = CASOS_CLINICOS_CANTIDAD, item: str = "clinico", modo_test: bool = False):
    """Elige 'cantidad' casos clinicos al azar (de los que ya tienen
    preguntas guardadas) para la seccion final del examen. Cada bloque
    se devuelve con sus preguntas YA EN SU ORDEN ORIGINAL (1-5): el
    contexto del caso se mantiene intacto, nunca se mezcla el orden de
    las preguntas dentro de un mismo caso.

    'item' decide el CONTENIDO (region 'administrativo' o clinico),
    independiente de 'modo_test' que solo decide si la CANTIDAD es
    flexible o estricta."""
    filas = sb.table("caso_preguntas").select("caso_id").execute().data
    caso_ids_con_preguntas = list({f["caso_id"] for f in filas})

    if caso_ids_con_preguntas:
        q = sb.table("casos_clinicos").select("id").in_("id", caso_ids_con_preguntas)
        q = q.eq("region", "administrativo") if item == "administrativo" else q.neq("region", "administrativo")
        filas_clinicos = q.execute().data
        caso_ids_disponibles = [f["id"] for f in filas_clinicos]
    else:
        caso_ids_disponibles = []

    if modo_test:
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
    if not sesion:
        raise HTTPException(404, "Sesión no encontrada")

    existente = sb.table("examen_instancia").select("*").eq("sesion_id", body.sesion_id).eq("alumno_id", body.alumno_id).execute().data
    # Examen ya cerrado (por el alumno o por el cierre de la sesion): solo su resultado.
    if existente and existente[0]["finalizado_at"]:
        return {"finalizado": True, "resultado": _resultado_guardado(existente[0])}

    if sesion["estado"] == "finalizada":
        raise HTTPException(409, "La sesión de examen ya terminó")
    if sesion["estado"] != "en_curso":
        raise HTTPException(409, "La sesión no está en curso todavía")
    if not sesion.get("conjunto_id") or not sesion.get("termina_at"):
        raise HTTPException(409, "Esta sesión se creó antes de que la sesión guardara su conjunto y su horario. Crea una sesión nueva.")

    if existente:
        instancia = existente[0]
    else:
        paquete = sesion["paquete_elegido"]
        modo_test = _conjunto_es_test(sesion["conjunto_id"])

        seleccion = _seleccionar_preguntas(paquete, item=body.item, modo_test=modo_test)
        pregunta_ids = [qid for ids in seleccion.values() for qid in ids]
        random.shuffle(pregunta_ids)

        # Seccion final: bloques de casos clinicos completos, agregados
        # DESPUES de las preguntas individuales. Cada bloque va intacto
        # y en orden; el orden ENTRE bloques (cual caso va primero) si
        # queda al azar, segun la seleccion de _seleccionar_casos_clinicos.
        bloques_casos = _seleccionar_casos_clinicos(item=body.item, modo_test=modo_test)
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

    # Foto de cada caso clinico: se firma UNA sola vez por caso, aqui,
    # antes de recorrer las preguntas. Las 5 preguntas de un caso comparten
    # el MISMO registro de casos_info, asi que si se firmara dentro del
    # loop de preguntas, la primera reemplazaria el path por la URL firmada
    # y la segunda intentaria firmar esa URL como si fuera un archivo
    # (-> StorageApiError 404 "Object not found"). Firmado aqui, todas las
    # preguntas del caso reciben la misma URL firmada.
    for caso in casos_info.values():
        if caso.get("media_url"):
            firmada_caso = sb.storage.from_("casos").create_signed_url(
                caso["media_url"], minutos_totales * 60 + 300
            )
            caso["media_url"] = firmada_caso.get("signedURL") or firmada_caso.get("signed_url")

    for q in preguntas:
        mapeo = orden_opciones.get(q["id"])
        if mapeo:
            q["opciones"] = [q["opciones"][i] for i in mapeo]
        if q.get("media_url"):
            firmada = sb.storage.from_("preguntas").create_signed_url(
                q["media_url"], minutos_totales * 60 + 300
            )
            q["media_url"] = firmada.get("signedURL") or firmada.get("signed_url")

    return {
        "instancia_id": instancia["id"],
        "paquete": instancia["paquete"],
        "minutos_totales": minutos_totales,
        "iniciado_at": instancia["iniciado_at"],
        "segundos_restantes": _segundos_restantes(sesion["termina_at"]),
        "respuestas": _respuestas_guardadas(instancia["id"], orden_opciones),
        "salidas_detectadas": instancia.get("salidas_detectadas") or 0,
        "preguntas": preguntas,
    }


def _segundos_restantes(termina_at):
    """Tiempo que le queda al alumno: el mismo para todos, hasta la hora de
    termino de la sesion (cierre de la encuesta + minutos del paquete)."""
    restante = (_parse_fecha(termina_at) - datetime.now(timezone.utc)).total_seconds()
    return max(0, int(restante))


def _respuestas_guardadas(instancia_id: str, orden_opciones: dict):
    """{pregunta_id: posicion_mostrada} de lo que el alumno ya respondio.
    En la base se guarda el indice ORIGINAL de la alternativa; aqui se
    traduce de vuelta a la posicion que el alumno ve (orden mezclado)."""
    filas = sb.table("respuestas").select("pregunta_id, opcion_elegida").eq("examen_instancia_id", instancia_id).execute().data
    filas += sb.table("respuestas_caso").select("pregunta_id, opcion_elegida").eq("examen_instancia_id", instancia_id).execute().data
    respuestas = {}
    for r in filas:
        mapeo = orden_opciones.get(r["pregunta_id"])
        respuestas[r["pregunta_id"]] = mapeo.index(r["opcion_elegida"]) if mapeo else r["opcion_elegida"]
    return respuestas

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
    # on_conflict: una respuesta se identifica por (examen, pregunta). Sin
    # esto el upsert intenta INSERTAR una fila nueva al cambiar la respuesta
    # y choca con el unique (examen_instancia_id, pregunta_id).
    sb.table(tabla).upsert({
        "examen_instancia_id": instancia_id, "pregunta_id": body.pregunta_id,
        "opcion_elegida": opcion_original, "correcta": correcta,
    }, on_conflict="examen_instancia_id,pregunta_id").execute()
    return {"correcta": correcta}

@router.post("/{instancia_id}/finalizar")
def finalizar_examen(instancia_id: str):
    instancia = sb.table("examen_instancia").select("*").eq("id", instancia_id).single().execute().data
    if not instancia:
        raise HTTPException(404, "Examen no encontrado")
    # Ya finalizado (p. ej. lo cerro el termino de la sesion y luego el
    # reloj del alumno llego a cero): se devuelve lo guardado, sin recalcular.
    if instancia["finalizado_at"]:
        return _resultado_guardado(instancia)

    resultado = _calcular_y_guardar_nota(instancia)
    _cerrar_sesion_si_terminaron_todos(instancia["sesion_id"])
    return resultado


def _calcular_y_guardar_nota(instancia: dict) -> dict:
    instancia_id = instancia["id"]
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


# ---------------- CIERRE DE LA SESION ----------------
def _cerrar_sesion_si_terminaron_todos(sesion_id: str):
    """Si todos los alumnos que ingresaron a la sesion ya finalizaron su
    examen, la sesion se cierra en ese momento (sin esperar termina_at)."""
    presentes = {a["alumno_id"] for a in sb.table("asistencia").select("alumno_id").eq("sesion_id", sesion_id).execute().data}
    if not presentes:
        return
    finalizados = {
        i["alumno_id"] for i in sb.table("examen_instancia").select("alumno_id, finalizado_at").eq("sesion_id", sesion_id).execute().data
        if i["finalizado_at"]
    }
    if presentes <= finalizados:
        cerrar_sesion(sesion_id)


def cerrar_sesion(sesion_id: str):
    """Finaliza los examenes que sigan abiertos (con lo respondido) y pasa
    la sesion a 'finalizada'. Si ya estaba finalizada, no hace nada."""
    sesion = sb.table("sesiones_examen").select("estado").eq("id", sesion_id).execute().data
    if not sesion or sesion[0]["estado"] == "finalizada":
        return
    abiertas = sb.table("examen_instancia").select("*").eq("sesion_id", sesion_id).is_("finalizado_at", "null").execute().data
    for instancia in abiertas:
        _calcular_y_guardar_nota(instancia)
    sb.table("sesiones_examen").update({
        "estado": "finalizada", "finalizada_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", sesion_id).execute()
    print(f"[examen] Sesión {sesion_id} finalizada ({len(abiertas)} exámenes cerrados al término).")


_cierres_programados = {}  # sesion_id -> threading.Timer
_lock_cierres = threading.Lock()


def _ejecutar_cierre(sesion_id: str):
    with _lock_cierres:
        _cierres_programados.pop(sesion_id, None)
    try:
        cerrar_sesion(sesion_id)
    except Exception as e:
        print(f"[examen] ERROR cerrando la sesión {sesion_id}: {type(e).__name__}: {e}")


def programar_cierre_sesion(sesion_id: str, termina_at):
    """Programa UN unico cierre para la hora de termino de la sesion."""
    segundos = (_parse_fecha(termina_at) - datetime.now(timezone.utc)).total_seconds()
    with _lock_cierres:
        anterior = _cierres_programados.pop(sesion_id, None)
        if anterior:
            anterior.cancel()
        timer = threading.Timer(max(0, segundos), _ejecutar_cierre, args=(sesion_id,))
        timer.daemon = True
        _cierres_programados[sesion_id] = timer
        timer.start()


def reprogramar_cierres_pendientes():
    """Al arrancar el servicio: vuelve a programar el cierre de cada sesion
    en curso (si su hora ya paso, se cierra de inmediato)."""
    filas = sb.table("sesiones_examen").select("id, termina_at").eq("estado", "en_curso").execute().data
    for f in filas:
        if f.get("termina_at"):
            programar_cierre_sesion(f["id"], f["termina_at"])


def minutos_del_paquete(paquete: str) -> int:
    return PAQUETES[paquete]["minutos"]


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
