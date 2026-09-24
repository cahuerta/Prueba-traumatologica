"""
services/cache_trivia_clases_formales.py
Cache en memoria PURO para la herramienta "trivia" de Clases Formales.
Igual que el semaforo: efimero, nunca se persiste a disco ni a
Supabase -sin valor historico, es solo para que el interrogador vea el
resultado en vivo y decida cuando revelar-.

Aislado por pagina_id -a diferencia del semaforo, que es continuo por
sesion completa-: cada pregunta de trivia es especifica de su pagina,
no tiene sentido que se mezcle con la trivia de otra pagina.

El alumno responde con una letra (A-E) sin ver la pregunta ni las
alternativas -su pantalla tiene un selector de letras fijo, siempre
visible-. La pregunta/alternativas/correcta viven en paginas_clase.config,
no aca.

Como el semaforo, vive solo en RAM del proceso -valido unicamente
porque el servicio corre con UN SOLO proceso (WEB_CONCURRENCY=1,
confirmado en Render).
"""

_trivia: dict = {}  # pagina_id -> {"respuestas": {alumno_id: letra}, "revelada": bool}


def _estado(pagina_id: str) -> dict:
    if pagina_id not in _trivia:
        _trivia[pagina_id] = {"respuestas": {}, "revelada": False}
    return _trivia[pagina_id]


def responder_trivia(pagina_id: str, alumno_id: str, letra: str):
    """El alumno responde (o cambia) su letra. Se sobreescribe si ya
    habia respondido antes -mismo criterio que el semaforo-."""
    _estado(pagina_id)["respuestas"][alumno_id] = letra


def obtener_resultado_trivia(pagina_id: str) -> dict:
    """{"total": int, "conteos": {"A": n, "B": n, ...}, "revelada": bool}
    Sin identidad de alumno. Solo cuenta letras que efectivamente
    llegaron -no rellena con ceros letras que la pregunta no tenga-."""
    estado = _estado(pagina_id)
    conteos: dict = {}
    for letra in estado["respuestas"].values():
        conteos[letra] = conteos.get(letra, 0) + 1

    return {
        "total": len(estado["respuestas"]),
        "conteos": conteos,
        "revelada": estado["revelada"],
    }


def revelar_trivia(pagina_id: str):
    """El interrogador marca la trivia como revelada -desde ahi el
    alumno puede consultar si acerto."""
    _estado(pagina_id)["revelada"] = True


def obtener_mi_resultado(pagina_id: str, alumno_id: str) -> dict | None:
    """La letra que el alumno respondio, o None si no ha respondido
    todavia. Usado solo si se revela, para que el alumno sepa que
    contesto (no se usa para comparar contra la correcta -eso lo hace
    el frontend, que ya conoce la correcta via el config de la pagina
    activa que consulta en silencio)."""
    estado = _estado(pagina_id)
    return estado["respuestas"].get(alumno_id)


def obtener_respuestas_trivia(pagina_id: str) -> dict:
    """{alumno_id: letra} de la trivia de esta pagina -copia, para no
    exponer el dict interno-. Solo lo usa el panel del interrogador
    (detalle nombre -> letra, igual que Casos Clinicos), nunca la
    proyeccion ni el alumno."""
    return dict(_estado(pagina_id)["respuestas"])
