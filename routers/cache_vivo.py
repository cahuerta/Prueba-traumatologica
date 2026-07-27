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

Se invalida (se refresca) unicamente en momentos reales:
  1. El admin ejecuta una accion (abrir_votacion, cerrar_votacion,
     revelar, siguiente) -> invalidar_sesion() / actualizar_sesion_cache().
  2. Un alumno vota -> invalidar_resultados().
  3. Alguien marca asistencia (ingreso a la sesion) -> invalidar_asistencia().
  4. Se crea/edita/quita una pregunta de un caso -> invalidar_pregunta()
     (tambien invalida el TOTAL de preguntas de ese caso).
  5. Se agrega/quita un caso de una presentacion -> invalidar_posiciones_presentacion()
     (tambien invalida el TOTAL de casos de esa presentacion).
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
    especifica orden, invalida todas las preguntas de ese caso.
    Tambien invalida el TOTAL de preguntas de ese caso -cambio junto,
    porque agregar/quitar una pregunta es justo lo que cambia ese numero."""
    if orden is None:
        for clave in list(_preguntas):
            if clave[0] == caso_id:
                _preguntas.pop(clave, None)
    else:
        _preguntas.pop((caso_id, orden), None)
    _total_preguntas_caso.pop(caso_id, None)


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
    """Se llama al agregar/quitar un caso de una presentacion. Tambien
    invalida el TOTAL de casos de esa presentacion -mismo motivo que
    invalidar_pregunta con el total de preguntas-."""
    for clave in list(_posicion_a_caso):
        if clave[0] == presentacion_id:
            _posicion_a_caso.pop(clave, None)
    _total_casos_presentacion.pop(presentacion_id, None)


# ---------------- TOTALES (cantidad de preguntas por caso / casos por presentacion) ----------------
# Se consultan en CADA poll de CADA alumno mientras estado=='cerrada' (para
# calcular 'finalizada'), asi que sin cache le pegan a Supabase sin
# necesidad -son numeros que casi nunca cambian mientras la clase esta en
# curso, solo al editar contenido desde el panel de preparacion-.
_total_preguntas_caso: dict = {}      # caso_id -> int
_total_casos_presentacion: dict = {}  # presentacion_id -> int

def obtener_total_preguntas_cache(caso_id: str):
    return _total_preguntas_caso.get(caso_id)

def guardar_total_preguntas_cache(caso_id: str, total: int):
    _total_preguntas_caso[caso_id] = total

def obtener_total_casos_cache(presentacion_id: str):
    return _total_casos_presentacion.get(presentacion_id)

def guardar_total_casos_cache(presentacion_id: str, total: int):
    _total_casos_presentacion[presentacion_id] = total


# ---------------- PAGINA DEL RESUMEN FINAL ----------------
# El admin la avanza con un boton (endpoint dedicado); Proyeccion y Admin
# la leen en cada poll del panel para saber que pagina del resumen mostrar.
# Vive solo en memoria -es un dato de UI efimero, no necesita sobrevivir
# a un reinicio del servicio ni guardarse en Supabase-.
_pagina_resumen: dict = {}   # sesion_id -> int

def obtener_pagina_resumen(sesion_id: str) -> int:
    return _pagina_resumen.get(sesion_id, 0)

def avanzar_pagina_resumen(sesion_id: str) -> int:
    nueva = obtener_pagina_resumen(sesion_id) + 1
    _pagina_resumen[sesion_id] = nueva
    return nueva

def reiniciar_pagina_resumen(sesion_id: str):
    _pagina_resumen.pop(sesion_id, None)
  
