"""
routers/clases_formales_sesiones.py
Sesion EN VIVO de Clases Formales -tabla separada de sesiones_vivo
(casos clinicos), por la misma decision de "duplicar en vez de
compartir" que se aplico a paginas_clase, preguntas_anonimas, etc-.
Cero riesgo de tocar lo que ya funciona en produccion.

Iniciar una sesion NO crea contenido: se elige un clase_formal_id ya
armado de antemano (routers/clases_formales_contenido.py, con sus
paginas ya construidas en clases_formales_paginas.py) -mismo patron
que casos clinicos, donde "iniciar presentacion" elige una presentacion
ya armada en vez de crear casos sobre la marcha-.

La sesion nace directamente ACTIVA -sin estado intermedio de
"preparacion"-: como el contenido ya existe de antemano, no hay nada
que esperar entre crear la sesion y que el alumno pueda entrar. Solo
dos estados posibles: "activa" | "cerrada".

pagina_actual_orden nace VACIO (null) -esa es la fase de "QR/asistencia"
que se muestra en proyeccion antes de arrancar el contenido, igual que
casos clinicos muestra el QR antes de "presentando"-. El primer PATCH
/avanzar que haga el interrogador es el que fija la primera pagina y
arranca la clase para real; los siguientes /avanzar mueven a la
pagina siguiente como siempre.

ASISTENCIA: el conteo EN VIVO se lee de la cache en memoria
(services/cache_clases_formales.py), sin tocar Supabase en cada poll
-mismo espiritu que cache_vivo.py, evitar pegarle a Supabase con
lecturas de alta frecuencia-. Ademas, UNA sola vez, a los 15 minutos
del inicio de la sesion, se guarda una foto del conteo en la tabla
asistencia_clase (persistencia real, sobrevive a un reinicio del
servidor) -no un insert por cada alumno que ingresa, un unico insert
por sesion-. Ese chequeo ocurre de forma perezosa: se dispara la
primera vez que GET /asistencia se llama despues de cumplidos los 15
minutos, no con un cron aparte.

Esta es la fila que el supraselector (routers/sesion_resolver.py) usa
para decidir hacia donde mandar un codigo activo: si existe en
sesiones_vivo -> caso clinico. Si existe en sesiones_clase -> Clases
Formales.

Tabla usada: sesiones_clase (ya creada en Supabase).
  id                  uuid
  clase_formal_id     uuid  (FK a clases_formales, el contenido elegido)
  nombre              text  (copiado del contenido al iniciar, por si el
                       contenido se edita/renombra despues -la sesion ya
                       dictada mantiene el nombre que tenia ese dia)
  codigo_acceso       text  (unico, corto, lo usan alumnos para entrar via QR/link)
  estado              text  ("activa" | "cerrada")
  pagina_actual_orden float (null hasta el primer avanzar; luego,
                       posicion en la secuencia de paginas_clase.orden
                       que admin/proyeccion estan mostrando ahora)
  created_at          timestamptz

Tabla usada: asistencia_clase (ya creada en Supabase).
  sesion_id        uuid  (PK, FK a sesiones_clase.id)
  total_presentes  int
  guardado_at      timestamptz

Avance de pagina: estrictamente secuencial, de a una pagina, sin
saltos. PATCH /avanzar mueve pagina_actual_orden a la siguiente
pagina existente (por orden ascendente), o fija la primera si todavia
no hay ninguna activa. PATCH /retroceder vuelve a la pagina anterior,
deteniendose en la primera (nunca vuelve a la fase de QR). El alumno nunca consulta esto -su pantalla es
fija (preguntas + semaforo)-, solo lo usan admin y proyeccion. La
lectura publica de "cual pagina esta activa ahora" vive en un archivo
aparte (routers/clases_formales_actual.py), sin auth, siguiendo el
mismo patron de separar interrogador/publico que
casos_vivo_profesor.py / casos_vivo_alumno.py.
"""

import random
import string
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel

from routers.auth import sb, get_current_interrogador
from routers.conjuntos_comun import obtener_conjunto_activo_id
from services import cache_clases_formales

router = APIRouter(prefix="/clases-formales/sesiones", tags=["clases-formales-sesiones"])

MINUTOS_FOTO_ASISTENCIA = 15


# ---------------- MODELOS ----------------
class SesionIn(BaseModel):
    clase_formal_id: str


# ---------------- HELPER ----------------
def _generar_codigo_acceso() -> str:
    """Codigo corto tipo el que ya usan las sesiones de casos clinicos
    -letras mayusculas y numeros, facil de mostrar en QR y de teclear
    manualmente si hace falta-.

    Con 6 caracteres (26 letras + 10 digitos) hay ~2.176 millones de
    combinaciones posibles -la probabilidad de choque en un uso normal
    es baja, pero no cero-, asi que se verifica contra la tabla antes
    de aceptarlo y se reintenta si ya existe (ver iniciar_sesion)."""
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=6))


def _generar_codigo_unico() -> str:
    """Reintenta hasta encontrar un codigo que no exista todavia en
    sesiones_clase. En la practica casi siempre acierta al primer
    intento -el reintento es solo la red de seguridad-."""
    for _ in range(10):
        codigo = _generar_codigo_acceso()
        existe = sb.table("sesiones_clase").select("id").eq("codigo_acceso", codigo).execute().data
        if not existe:
            return codigo
    raise HTTPException(500, "No se pudo generar un codigo de acceso unico, intente de nuevo")


def _minutos_transcurridos(created_at: str) -> float:
    """created_at viene de Supabase como string ISO -acepta el sufijo
    'Z' que datetime.fromisoformat no maneja directo en Python <3.11."""
    ts = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    ahora = datetime.now(timezone.utc)
    return (ahora - ts).total_seconds() / 60


# ---------------- ENDPOINTS ----------------
@router.post("")
def iniciar_sesion(body: SesionIn, interrogador: dict = Depends(get_current_interrogador)):
    """Inicia una sesion en vivo a partir de un contenido ya armado.
    Nace 'activa' de inmediato, pero SIN pagina fijada todavia -esa es
    la fase de QR/asistencia en proyeccion, hasta que el interrogador
    toque 'Iniciar clase' (el primer /avanzar)."""
    contenido = sb.table("clases_formales").select("nombre").eq("id", body.clase_formal_id).execute().data
    if not contenido:
        raise HTTPException(404, "Contenido no encontrado")

    primera = (
        sb.table("paginas_clase")
        .select("orden")
        .eq("clase_formal_id", body.clase_formal_id)
        .order("orden")
        .limit(1)
        .execute()
        .data
    )
    if not primera:
        raise HTTPException(400, "El contenido no tiene paginas todavia, agrega al menos una antes de iniciar")

    codigo = _generar_codigo_unico()

    res = sb.table("sesiones_clase").insert({
        "clase_formal_id": body.clase_formal_id,
        "nombre": contenido[0]["nombre"],
        "codigo_acceso": codigo,
        "estado": "activa",
        "pagina_actual_orden": None,
    }).execute()

    return res.data[0]


@router.get("")
def listar_sesiones(interrogador: dict = Depends(get_current_interrogador)):
    """Lista todas las sesiones en vivo (activas e historicas), mas
    recientes primero."""
    return (
        sb.table("sesiones_clase")
        .select("*")
        .order("created_at", desc=True)
        .execute()
        .data
    )


@router.get("/{sesion_id}/asistencia")
def asistencia_sesion(sesion_id: str, interrogador: dict = Depends(get_current_interrogador)):
    """Conteo EN VIVO desde la cache en memoria -no toca Supabase en cada
    llamada-. Ademas, si ya pasaron 15 minutos desde el inicio de la
    sesion y todavia no existe una foto guardada, la guarda ahora mismo
    (unico insert por sesion, disparado de forma perezosa en este mismo
    request)."""
    sesion = sb.table("sesiones_clase").select("id, created_at").eq("id", sesion_id).execute().data
    if not sesion:
        raise HTTPException(404, "Sesion no encontrada")
    sesion = sesion[0]

    presentes = cache_clases_formales.obtener_total_presentes(sesion_id)

    conjunto_id = obtener_conjunto_activo_id()
    total_habilitados = (
        sb.table("alumnos")
        .select("id", count="exact")
        .eq("conjunto_id", conjunto_id)
        .execute()
        .count
    )

    if _minutos_transcurridos(sesion["created_at"]) >= MINUTOS_FOTO_ASISTENCIA:
        ya_guardada = sb.table("asistencia_clase").select("sesion_id").eq("sesion_id", sesion_id).execute().data
        if not ya_guardada:
            sb.table("asistencia_clase").insert({
                "sesion_id": sesion_id,
                "total_presentes": presentes,
            }).execute()

    return {"presentes": presentes, "total_habilitados": total_habilitados or 0}


@router.patch("/{sesion_id}/avanzar")
def avanzar_sesion(sesion_id: str, interrogador: dict = Depends(get_current_interrogador)):
    """Si la sesion todavia no tiene pagina activa (fase de QR/asistencia),
    fija la primera pagina del contenido -esto es lo que hace el boton
    'Iniciar clase'-. Si ya hay una pagina activa, mueve
    pagina_actual_orden a la siguiente en la secuencia -estrictamente
    hacia adelante -para volver esta retroceder_sesion-. Si ya esta en
    la ultima pagina, no hace nada (devuelve la sesion tal cual)."""
    sesion = sb.table("sesiones_clase").select("*").eq("id", sesion_id).execute().data
    if not sesion:
        raise HTTPException(404, "Sesion no encontrada")
    sesion = sesion[0]

    if sesion["pagina_actual_orden"] is None:
        # Todavia no ha arrancado -fija la primera pagina del contenido-
        siguiente = (
            sb.table("paginas_clase")
            .select("orden")
            .eq("clase_formal_id", sesion["clase_formal_id"])
            .order("orden")
            .limit(1)
            .execute()
            .data
        )
    else:
        siguiente = (
            sb.table("paginas_clase")
            .select("orden")
            .eq("clase_formal_id", sesion["clase_formal_id"])
            .gt("orden", sesion["pagina_actual_orden"])
            .order("orden")
            .limit(1)
            .execute()
            .data
        )

    if not siguiente:
        return sesion

    res = (
        sb.table("sesiones_clase")
        .update({"pagina_actual_orden": siguiente[0]["orden"]})
        .eq("id", sesion_id)
        .execute()
    )
    return res.data[0]


@router.patch("/{sesion_id}/retroceder")
def retroceder_sesion(sesion_id: str, interrogador: dict = Depends(get_current_interrogador)):
    """Espejo de avanzar_sesion: mueve pagina_actual_orden a la pagina
    ANTERIOR de la secuencia. Si ya esta en la primera pagina, o la clase
    aun no se inicia (sin pagina activa), no hace nada y devuelve la
    sesion tal cual -nunca vuelve a la fase de QR/asistencia: el QR ya
    esta siempre visible en la esquina de la proyeccion para los
    atrasados-.

    No toca el estado de ninguna herramienta: al volver a una trivia se
    ven los votos que ya tenia (y la correcta, si ya se habia revelado),
    porque su cache vive por pagina_id y nunca se borra aqui. Despues de
    retroceder, 'Siguiente' continua desde esta pagina, porque avanzar
    siempre parte de la pagina activa."""
    sesion = sb.table("sesiones_clase").select("*").eq("id", sesion_id).execute().data
    if not sesion:
        raise HTTPException(404, "Sesion no encontrada")
    sesion = sesion[0]

    if sesion["pagina_actual_orden"] is None:
        return sesion

    anterior = (
        sb.table("paginas_clase")
        .select("orden")
        .eq("clase_formal_id", sesion["clase_formal_id"])
        .lt("orden", sesion["pagina_actual_orden"])
        .order("orden", desc=True)
        .limit(1)
        .execute()
        .data
    )

    if not anterior:
        return sesion

    res = (
        sb.table("sesiones_clase")
        .update({"pagina_actual_orden": anterior[0]["orden"]})
        .eq("id", sesion_id)
        .execute()
    )
    return res.data[0]


@router.patch("/{sesion_id}/cerrar")
def cerrar_sesion(sesion_id: str, interrogador: dict = Depends(get_current_interrogador)):
    """Cierra la sesion -los alumnos ya no pueden entrar ni interactuar."""
    res = sb.table("sesiones_clase").update({"estado": "cerrada"}).eq("id", sesion_id).execute()
    if not res.data:
        raise HTTPException(404, "Sesion no encontrada")

    return res.data[0]
