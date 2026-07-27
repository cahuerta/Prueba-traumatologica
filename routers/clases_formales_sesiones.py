"""
routers/clases_formales_sesiones.py
Ciclo de creacion de una sesion de Clases Formales -tabla separada de
sesiones_vivo (casos clinicos), por la misma decision de "duplicar en
vez de compartir" que se aplico a paginas_clase, preguntas_anonimas,
etc-. Cero riesgo de tocar lo que ya funciona en produccion.

Esta es la fila que el supraselector (a definir, tambien backend) usara
para decidir hacia donde mandar una sesion activa: si el codigo_acceso
existe en sesiones_vivo -> caso clinico. Si existe en sesiones_clase ->
Clases Formales.

Tabla usada: sesiones_clase (a crear al final, junto con el resto del
esquema de Clases Formales).
  id                  uuid
  nombre              text
  codigo_acceso       text (unico, corto, lo usan alumnos para entrar via QR/link)
  estado              text  ("preparacion" | "activa" | "cerrada")
  pagina_actual_orden float (null hasta que se activa; posicion en la
                       secuencia de paginas_clase.orden que admin/proyeccion
                       estan mostrando en este momento)
  created_at          timestamptz

Avance de pagina: estrictamente secuencial, solo hacia adelante -una
clase se recorre completa de principio a fin, no queda a medias ni
admite saltos-. PATCH /avanzar mueve pagina_actual_orden a la siguiente
pagina existente (por orden ascendente). El alumno nunca consulta esto
-su pantalla es fija (preguntas + semaforo)-, solo lo usan admin y
proyeccion. La lectura publica de "cual pagina esta activa ahora" vive
en un archivo aparte (routers/clases_formales_actual.py), sin auth,
siguiendo el mismo patron de separar interrogador/publico que
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
    nombre: str


# ---------------- HELPER ----------------
def _generar_codigo_acceso() -> str:
    """Codigo corto tipo el que ya usan las sesiones de casos clinicos
    -letras mayusculas y numeros, facil de mostrar en QR y de teclear
    manualmente si hace falta-.

    Con 6 caracteres (26 letras + 10 digitos) hay ~2.176 millones de
    combinaciones posibles -la probabilidad de choque en un uso normal
    es baja, pero no cero-, asi que se verifica contra la tabla antes
    de aceptarlo y se reintenta si ya existe (ver crear_sesion)."""
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
def crear_sesion(body: SesionIn, interrogador: dict = Depends(get_current_interrogador)):
    """Crea una sesion nueva de Clases Formales, en estado 'preparacion'
    -el interrogador arma sus paginas antes de activarla-."""
    codigo = _generar_codigo_unico()

    res = sb.table("sesiones_clase").insert({
        "nombre": body.nombre.strip(),
        "codigo_acceso": codigo,
        "estado": "preparacion",
    }).execute()

    return res.data[0]


@router.get("")
def listar_sesiones(interrogador: dict = Depends(get_current_interrogador)):
    """Lista todas las sesiones de Clases Formales, mas recientes primero."""
    return (
        sb.table("sesiones_clase")
        .select("*")
        .order("created_at", desc=True)
        .execute()
        .data
    )


@router.patch("/{sesion_id}/activar")
def activar_sesion(sesion_id: str, interrogador: dict = Depends(get_current_interrogador)):
    """Pasa la sesion a estado 'activa' -recien ahi los alumnos pueden
    entrar con el codigo de acceso y empezar a interactuar-. Ademas fija
    pagina_actual_orden en la primera pagina de la secuencia -la clase
    siempre arranca desde el principio-."""
    primera = (
        sb.table("paginas_clase")
        .select("orden")
        .eq("sesion_id", sesion_id)
        .order("orden")
        .limit(1)
        .execute()
        .data
    )
    cambios = {"estado": "activa"}
    if primera:
        cambios["pagina_actual_orden"] = primera[0]["orden"]

    res = sb.table("sesiones_clase").update(cambios).eq("id", sesion_id).execute()
    if not res.data:
        raise HTTPException(404, "Sesion no encontrada")

    return res.data[0]


@router.patch("/{sesion_id}/avanzar")
def avanzar_sesion(sesion_id: str, interrogador: dict = Depends(get_current_interrogador)):
    """Mueve pagina_actual_orden a la siguiente pagina en la secuencia
    -estrictamente hacia adelante, sin saltos ni retrocesos-. Si ya esta
    en la ultima pagina, no hace nada (devuelve la sesion tal cual)."""
    sesion = sb.table("sesiones_clase").select("*").eq("id", sesion_id).execute().data
    if not sesion:
        raise HTTPException(404, "Sesion no encontrada")
    sesion = sesion[0]

    siguiente = (
        sb.table("paginas_clase")
        .select("orden")
        .eq("sesion_id", sesion_id)
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
  
