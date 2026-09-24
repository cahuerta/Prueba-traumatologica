"""
services/cache_clases_formales.py
Cache en memoria PURO para las herramientas en vivo de Clases Formales
(semaforo + asistencia). A diferencia de votos_local.py y
preguntas_local.py, esto NUNCA se persiste -ni a disco ni a Supabase-:
es una lectura del momento para que el interrogador decida si sigue
avanzando o repite algo, sin valor historico real (decision explicita:
no vale la pena guardarlo).

El semaforo es CONTINUO por sesion completa -no por pagina-: el alumno
responde una sola vez "sigo?" y esa respuesta se mantiene viva durante
toda la clase, sin reiniciarse al cambiar de pagina. Aislado por
sesion_id: cada sesion en vivo tiene su propio espacio en memoria.

La asistencia es la excepcion: igual que en Casos Clinicos, cada ingreso
(nombre + RUT o matricula, validado contra el conjunto activo) se
persiste en Supabase, y esta memoria es solo una copia rapida para el
polling del mando -se recarga desde Supabase tras un reinicio-.

Como el semaforo, esto vive solo en RAM del proceso -mismo supuesto que
cache_vivo.py: valido unicamente porque el servicio corre con UN SOLO
proceso (WEB_CONCURRENCY=1, confirmado en Render). Si se escala a mas de
un worker, hay que mover esto a un cache compartido (ej. Redis).
"""

# ---------------- SEMAFORO ("sigo?" si/no, continuo por sesion) ----------------
_semaforo: dict = {}   # sesion_id -> {alumno_id: bool}


def responder_semaforo(sesion_id: str, alumno_id: str, sigo: bool):
    """El alumno responde (o cambia) su estado. Se sobreescribe si ya
    habia respondido antes -el semaforo refleja el estado actual, no
    el primer clic-. Vive durante toda la sesion, no se reinicia nunca
    mientras la clase esta en curso."""
    if sesion_id not in _semaforo:
        _semaforo[sesion_id] = {}
    _semaforo[sesion_id][alumno_id] = sigo


def obtener_resultado_semaforo(sesion_id: str) -> dict:
    """{"total": int, "porcentaje_sigo": float, "color": "verde"|"amarillo"|"rojo"}
    Sin identidad de alumno. Umbrales (sobre % que respondio 'si'):
      >= 60%  -> verde
      40-59%  -> amarillo
      < 40%   -> rojo
    Si aun no hay respuestas, se asume verde por defecto (arranca en
    verde hasta que la sala empiece a votar)."""
    respuestas = _semaforo.get(sesion_id, {})
    total = len(respuestas)

    if total == 0:
        return {"total": 0, "porcentaje_sigo": 100.0, "color": "verde"}

    total_si = sum(1 for sigo in respuestas.values() if sigo)
    porcentaje = (total_si / total) * 100

    if porcentaje >= 60:
        color = "verde"
    elif porcentaje >= 40:
        color = "amarillo"
    else:
        color = "rojo"

    return {"total": total, "porcentaje_sigo": round(porcentaje, 1), "color": color}


# ---------------- ASISTENCIA (quien ingreso, con nombre, por sesion) ----------------
# Igual que Casos Clinicos: cada ingreso se PERSISTE en Supabase
# (asistencia_clase_alumnos, ver clases_formales_ingreso.py) y ademas se
# guarda aca en memoria para responder rapido el polling del mando. Si el
# servidor se reinicia, la memoria arranca vacia y se recarga una vez
# desde Supabase (ver clases_formales_sesiones.asistencia_sesion), asi que
# el conteo y los nombres ya no se pierden con un reinicio de Render.
_asistencia: dict = {}   # sesion_id -> {alumno_id: {"nombre", "rut", "marcado_at"}}
_cargada: set = set()    # sesiones ya sincronizadas con Supabase en este proceso


def marcar_presente(sesion_id: str, alumno_id: str, nombre: str, rut: str, marcado_at: str):
    """Se llama una vez por ingreso. Si el alumno entra de nuevo, no se
    duplica (clave alumno_id): se actualiza su nombre y se conserva la
    hora del primer ingreso."""
    presentes = _asistencia.setdefault(sesion_id, {})
    previo = presentes.get(alumno_id)
    presentes[alumno_id] = {
        "nombre": nombre,
        "rut": rut,
        "marcado_at": previo["marcado_at"] if previo else marcado_at,
    }


def esta_cargada(sesion_id: str) -> bool:
    return sesion_id in _cargada


def cargar_desde_supabase(sesion_id: str, filas: list):
    """Recarga la asistencia de una sesion desde Supabase (tras un
    reinicio). 'filas' = [{alumno_id, marcado_at, alumnos: {nombre, rut}}].
    Lo que ya estaba en memoria (ingresos posteriores) se conserva."""
    presentes = _asistencia.setdefault(sesion_id, {})
    for f in filas:
        alumno = f.get("alumnos") or {}
        presentes.setdefault(f["alumno_id"], {
            "nombre": alumno.get("nombre") or "",
            "rut": alumno.get("rut") or "",
            "marcado_at": f.get("marcado_at"),
        })
    _cargada.add(sesion_id)


def obtener_presentes(sesion_id: str) -> list:
    """[{alumno_id, nombre, rut, marcado_at}] en orden de llegada."""
    presentes = _asistencia.get(sesion_id, {})
    lista = [{"alumno_id": aid, **datos} for aid, datos in presentes.items()]
    lista.sort(key=lambda p: p.get("marcado_at") or "")
    return lista


def obtener_total_presentes(sesion_id: str) -> int:
    return len(_asistencia.get(sesion_id, {}))
