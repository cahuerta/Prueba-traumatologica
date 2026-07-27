"""
routers/clases_formales_contenido.py
CRUD del CONTENIDO de una clase formal -analogo a "presentacion" en
casos clinicos-: el nombre y el contenedor de paginas que se arma una
vez y se reutiliza cada vez que se dicta la clase.

Separado de la sesion en vivo (clases_formales_sesiones.py): el
contenido no tiene codigo_acceso ni estado ni pagina_actual_orden -eso
es exclusivo de una sesion iniciada a partir de este contenido-. Un
mismo contenido puede iniciarse en varias sesiones distintas (distintos
cursos, distintas fechas), cada una con su propio codigo y su propio
avance de pagina.

Tabla usada: clases_formales (a crear al final, junto con el resto del
esquema).
  id          uuid
  nombre      text
  created_at  timestamptz
"""

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel

from routers.auth import sb, get_current_interrogador

router = APIRouter(prefix="/clases-formales/contenido", tags=["clases-formales-contenido"])


# ---------------- MODELOS ----------------
class ContenidoIn(BaseModel):
    nombre: str


class ContenidoEditarIn(BaseModel):
    nombre: str


# ---------------- ENDPOINTS ----------------
@router.post("")
def crear_contenido(body: ContenidoIn, interrogador: dict = Depends(get_current_interrogador)):
    """Crea un contenido nuevo -vacio, sin paginas todavia-. El
    interrogador sigue directo al constructor para armar las paginas."""
    res = sb.table("clases_formales").insert({"nombre": body.nombre.strip()}).execute()
    return res.data[0]


@router.get("")
def listar_contenidos(interrogador: dict = Depends(get_current_interrogador)):
    """Lista todo el contenido armado, mas reciente primero -se usa
    tanto para editar/seguir armando como para elegir cual iniciar."""
    return (
        sb.table("clases_formales")
        .select("*")
        .order("created_at", desc=True)
        .execute()
        .data
    )


@router.patch("/{clase_formal_id}")
def editar_contenido(
    clase_formal_id: str,
    body: ContenidoEditarIn,
    interrogador: dict = Depends(get_current_interrogador),
):
    """Renombrar el contenido -las paginas se editan en
    clases_formales_paginas.py, este endpoint solo toca el nombre."""
    res = (
        sb.table("clases_formales")
        .update({"nombre": body.nombre.strip()})
        .eq("id", clase_formal_id)
        .execute()
    )
    if not res.data:
        raise HTTPException(404, "Contenido no encontrado")

    return res.data[0]


@router.delete("/{clase_formal_id}")
def eliminar_contenido(clase_formal_id: str, interrogador: dict = Depends(get_current_interrogador)):
    """Elimina el contenido y sus paginas -no toca sesiones en vivo ya
    cerradas, que quedan historicas con su propio codigo_acceso."""
    res = sb.table("clases_formales").delete().eq("id", clase_formal_id).execute()
    if not res.data:
        raise HTTPException(404, "Contenido no encontrado")

    return {"ok": True}
