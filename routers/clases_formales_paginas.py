"""
routers/clases_formales_paginas.py
Constructor de sesion para Clases Formales: el interrogador arma su
clase como paginas secuenciales, cada una con un titulo y opcionalmente
una herramienta (ej. "semaforo", "espectro", o "ninguna" si es solo
contenido/exposicion).

Modulo de creacion, exclusivo del interrogador -no hay acceso publico
aca, a diferencia de clases_formales_preguntas.py-.

Orden de las paginas: gap-based partiendo en saltos de 10 (10, 20, 30...)
para paginas nuevas al final. Al insertar/mover una pagina entre dos
existentes, se usa el punto medio entre sus vecinas (puede resultar en
decimales) en vez de reindexar toda la secuencia. El interrogador nunca
ve estos numeros -en el frontend reordena por drag-and-drop, el
'orden' es un detalle interno-.

Config de cada pagina (JSONB, columna 'config'): guarda lo que varia
segun la herramienta -pregunta del semaforo, extremos del espectro,
texto libre, etc-. La estructura de config depende de tipo_herramienta
y no se valida a nivel de backend por ahora -queda abierta a que el
frontend defina la forma exacta de cada herramienta sin requerir
cambios aca-.

Tabla usada: paginas_clase (nombre y columnas a crear al final, junto
con el resto del esquema de Clases Formales).
  id              uuid
  sesion_id       uuid
  orden           float
  titulo          text
  tipo_herramienta text   ("ninguna" | "semaforo" | "espectro" | ...)
  config          jsonb
  created_at      timestamptz
"""

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel

from routers.auth import sb, get_current_interrogador

router = APIRouter(prefix="/clases-formales/paginas", tags=["clases-formales-paginas"])

SALTO_ORDEN = 10


# ---------------- MODELOS ----------------
class PaginaIn(BaseModel):
    sesion_id: str
    titulo: str
    tipo_herramienta: str = "ninguna"
    config: dict = {}


class PaginaEditarIn(BaseModel):
    titulo: str | None = None
    tipo_herramienta: str | None = None
    config: dict | None = None


class MoverIn(BaseModel):
    orden_anterior: float | None = None  # None si va al principio
    orden_siguiente: float | None = None  # None si va al final


# ---------------- ENDPOINTS ----------------
@router.post("")
def crear_pagina(body: PaginaIn, interrogador: dict = Depends(get_current_interrogador)):
    """Crea una pagina nueva al final de la secuencia de la sesion
    -orden = ultima pagina existente + SALTO_ORDEN, o SALTO_ORDEN si es
    la primera-."""
    ultima = (
        sb.table("paginas_clase")
        .select("orden")
        .eq("sesion_id", body.sesion_id)
        .order("orden", desc=True)
        .limit(1)
        .execute()
        .data
    )
    nuevo_orden = (ultima[0]["orden"] + SALTO_ORDEN) if ultima else SALTO_ORDEN

    res = sb.table("paginas_clase").insert({
        "sesion_id": body.sesion_id,
        "orden": nuevo_orden,
        "titulo": body.titulo.strip(),
        "tipo_herramienta": body.tipo_herramienta,
        "config": body.config,
    }).execute()

    return res.data[0]


@router.get("/{sesion_id}")
def listar_paginas(sesion_id: str, interrogador: dict = Depends(get_current_interrogador)):
    """Lista las paginas de la sesion en su orden de presentacion."""
    return (
        sb.table("paginas_clase")
        .select("*")
        .eq("sesion_id", sesion_id)
        .order("orden")
        .execute()
        .data
    )


@router.patch("/{pagina_id}")
def editar_pagina(
    pagina_id: str,
    body: PaginaEditarIn,
    interrogador: dict = Depends(get_current_interrogador),
):
    """Edita titulo, tipo de herramienta y/o config de una pagina
    existente. Solo actualiza los campos que vengan informados."""
    cambios = {k: v for k, v in body.model_dump().items() if v is not None}
    if not cambios:
        raise HTTPException(400, "No hay cambios para aplicar")

    res = sb.table("paginas_clase").update(cambios).eq("id", pagina_id).execute()
    if not res.data:
        raise HTTPException(404, "Pagina no encontrada")

    return res.data[0]


@router.patch("/{pagina_id}/mover")
def mover_pagina(
    pagina_id: str,
    body: MoverIn,
    interrogador: dict = Depends(get_current_interrogador),
):
    """Reordena una pagina calculando el punto medio entre sus nuevos
    vecinos -sin reindexar el resto de la secuencia-.

    - orden_anterior=None -> va al principio (mitad entre 0 y la siguiente)
    - orden_siguiente=None -> va al final (siguiente + SALTO_ORDEN)
    - ambos presentes -> punto medio entre ambos"""
    if body.orden_anterior is None and body.orden_siguiente is None:
        raise HTTPException(400, "Debe indicar al menos un vecino")

    if body.orden_anterior is None:
        nuevo_orden = body.orden_siguiente / 2
    elif body.orden_siguiente is None:
        nuevo_orden = body.orden_anterior + SALTO_ORDEN
    else:
        nuevo_orden = (body.orden_anterior + body.orden_siguiente) / 2

    res = sb.table("paginas_clase").update({"orden": nuevo_orden}).eq("id", pagina_id).execute()
    if not res.data:
        raise HTTPException(404, "Pagina no encontrada")

    return res.data[0]


@router.delete("/{pagina_id}")
def eliminar_pagina(pagina_id: str, interrogador: dict = Depends(get_current_interrogador)):
    res = sb.table("paginas_clase").delete().eq("id", pagina_id).execute()
    if not res.data:
        raise HTTPException(404, "Pagina no encontrada")

    return {"ok": True}
  
