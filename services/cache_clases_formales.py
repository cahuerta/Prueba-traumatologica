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

La asistencia sigue el mismo criterio: se cuenta presente al alumno
apenas ingresa con su RUT (ver endpoint de ingreso), y ese conteo se
mantiene vivo durante toda la sesion -no depende de pagina ni de
polling repetido-. Aislada por sesion_id igual que el semaforo.

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


# ---------------- ASISTENCIA (quien ha ingresado con su RUT, por sesion) ----------------
_asistencia: dict = {}   # sesion_id -> set(rut)


def marcar_presente(sesion_id: str, rut: str):
    """Se llama una vez por ingreso -si el alumno entra de nuevo con el
    mismo RUT, el set no duplica, sigue contando una sola vez-. Vive
    durante toda la sesion, igual que el semaforo."""
    if sesion_id not in _asistencia:
        _asistencia[sesion_id] = set()
    _asistencia[sesion_id].add(rut)


def obtener_total_presentes(sesion_id: str) -> int:
    return len(_asistencia.get(sesion_id, set()))
    
