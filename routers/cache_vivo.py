"""
routers/cache_vivo.py
Cache en memoria para el estado de sesiones en vivo (casos clinicos).
No expira por tiempo: mientras nada cambie de verdad, todas las lecturas
-potencialmente decenas por segundo con 90 alumnos en polling- se sirven
desde memoria, sin tocar Supabase (que es donde vive el bug de
concurrencia HTTP/2 bajo carga alta).

Solo es seguro porque el servicio corre con UN SOLO proceso
(WEB_CONCURRENCY=1, confirmado en Render) -si en algun momento se
escala a mas de un worker, este diseño deja de ser valido y habria que
mover esto a un cache compartido (ej. Redis)-.

Se invalida (se refresca) unicamente en 3 momentos reales:
  1. El admin ejecuta una accion (abrir_votacion, cerrar_votacion,
     revelar, siguiente) -> invalidar_sesion() / actualizar_sesion_cache().
  2. Un alumno vota -> invalidar_resultados().
  3. Alguien marca asistencia (ingreso a la sesion) -> invalidar_asistencia().
"""

# ---------------- SESION (estado, caso/pregunta actual) ----------------
_sesiones: dict = {}          # sesion_id -> fila de sesiones_vivo
_codigo_a_sesion: dict = {}   # codigo_acceso -> sesion_id (no cambia nunca en la vida de la sesion)


def obtener_sesion_cache(sesion_id: str):
    return _sesiones.get(sesion_id)

def guardar_sesion_cache(sesion_id: str, fila: dict):
    _sesiones[sesion_id] = fila
    codigo = fila.get("codigo_acceso")
    if codigo:
        _codigo_a_sesion[codigo] = sesion_id

def resolver_sesion_id_por_codigo(codigo: str):
    return _codigo_a_sesion.get(codigo)

def actualizar_sesion_cache(sesion_id: str, cambios: dict):
    """Actualiza directo en memoria (en vez de solo invalidar) para que
    el proximo GET no tenga ni que ir a Supabase a buscarla de nuevo."""
    if sesion_id in _sesiones:
        _sesiones[sesion_id].update(cambios)

def invalidar_sesion(sesion_id: str):
    _sesiones.pop(sesion_id, None)


# ---------------- RESULTADOS (conteo de votos de la pregunta actual) ----------------
_resultados: dict = {}   # sesion_id -> {"total": int, "conteo": {opcion: cantidad}}

def obtener_resultados_cache(sesion_id: str):
    return _resultados.get(sesion_id)

def guardar_resultados_cache(sesion_id: str, data: dict):
    _resultados[sesion_id] = data

def invalidar_resultados(sesion_id: str):
    _resultados.pop(sesion_id, None)


# ---------------- ASISTENCIA ----------------
_asistencia: dict = {}   # sesion_id -> {"presentes": [...], "total_habilitados": int, "total_presentes": int}

def obtener_asistencia_cache(sesion_id: str):
    return _asistencia.get(sesion_id)

def guardar_asistencia_cache(sesion_id: str, data: dict):
    _asistencia[sesion_id] = data

def invalidar_asistencia(sesion_id: str):
    _asistencia.pop(sesion_id, None)


# ---------------- PREGUNTA DEL CASO (texto, opciones, media - casi estatico) ----------------
_preguntas: dict = {}   # (caso_id, orden) -> fila de caso_preguntas

def obtener_pregunta_cache(caso_id: str, orden: int):
    return _preguntas.get((caso_id, orden))

def guardar_pregunta_cache(caso_id: str, orden: int, fila: dict):
    _preguntas[(caso_id, orden)] = fila

def invalidar_pregunta(caso_id: str, orden: int = None):
    """Se llama al crear/editar/quitar una pregunta del caso. Si no se
    especifica orden, invalida todas las preguntas de ese caso."""
    if orden is None:
        for clave in list(_preguntas):
            if clave[0] == caso_id:
                _preguntas.pop(clave, None)
    else:
        _preguntas.pop((caso_id, orden), None)


# ---------------- CASO CLINICO (titulo, vineta, media - estatico) ----------------
_casos: dict = {}   # caso_id -> fila de casos_clinicos

def obtener_caso_cache(caso_id: str):
    return _casos.get(caso_id)

def guardar_caso_cache(caso_id: str, fila: dict):
    _casos[caso_id] = fila

def invalidar_caso(caso_id: str):
    _casos.pop(caso_id, None)


# ---------------- POSICION -> CASO (que caso corresponde a cada orden de la presentacion) ----------------
_posicion_a_caso: dict = {}   # (presentacion_id, orden) -> fila de casos_clinicos (o None)

def obtener_posicion_caso_cache(presentacion_id: str, orden: int):
    """Devuelve una tupla (encontrado: bool, valor) para distinguir
    'no esta en cache' de 'esta en cache y es None' (posicion sin caso)."""
    clave = (presentacion_id, orden)
    if clave in _posicion_a_caso:
        return True, _posicion_a_caso[clave]
    return False, None

def guardar_posicion_caso_cache(presentacion_id: str, orden: int, caso):
    _posicion_a_caso[(presentacion_id, orden)] = caso

def invalidar_posiciones_presentacion(presentacion_id: str):
    """Se llama al agregar/quitar un caso de una presentacion."""
    for clave in list(_posicion_a_caso):
        if clave[0] == presentacion_id:
            _posicion_a_caso.pop(clave, None)
  
