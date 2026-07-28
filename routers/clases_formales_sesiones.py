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

Avance de pagina: estrictamente secuencial, solo hacia adelante -una
clase se recorre completa de principio a fin, no queda a medias ni
admite saltos-. PATCH /avanzar mueve pagina_actual_orden a la siguiente
pagina existente (por orden ascendente), o fija la primera si todavia
no hay ninguna activa. El alumno nunca consulta esto -su pantalla es
fija (preguntas + semaforo)-, solo lo usan admin y proyeccion. La
lectura publica de "cual pagina esta activa ahora" vive en un archivo
aparte (routers/clases_formales_actual.py), sin auth, siguiendo el
mismo patron de separar interrogador/publico que
casos_vivo_profesor.py / casos_vivo_alumno.py.
"""

import random
import string

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel

from routers.auth import sb, get_current_interrogador

router = APIRouter(prefix="/clases-formales/sesiones", tags=["clases-formales-sesiones"])


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


@router.patch("/{sesion_id}/avanzar")
def avanzar_sesion(sesion_id: str, interrogador: dict = Depends(get_current_interrogador)):
    """Si la sesion todavia no tiene pagina activa (fase de QR/asistencia),
    fija la primera pagina del contenido -esto es lo que hace el boton
    'Iniciar clase'-. Si ya hay una pagina activa, mueve
    pagina_actual_orden a la siguiente en la secuencia -estrictamente
    hacia adelante, sin saltos ni retrocesos-. Si ya esta en la ultima
    pagina, no hace nada (devuelve la sesion tal cual)."""
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


@router.patch("/{sesion_id}/cerrar")
def cerrar_sesion(sesion_id: str, interrogador: dict = Depends(get_current_interrogador)):
    """Cierra la sesion -los alumnos ya no pueden entrar ni interactuar."""
    res = sb.table("sesiones_clase").update({"estado": "cerrada"}).eq("id", sesion_id).execute()
    if not res.data:
        raise HTTPException(404, "Sesion no encontrada")

    return res.data[0]
