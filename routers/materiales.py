"""
routers/materiales.py
PPT y resúmenes que suben los interrogadores, organizados por región,
para que los alumnos los descarguen después.
"""

from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from routers.auth import sb, get_current_interrogador

router = APIRouter(prefix="/materiales", tags=["materiales"])


# ---------------- MODELOS ----------------
class MaterialIn(BaseModel):
    region: str
    tipo: str  # "ppt" | "resumen"
    titulo: str
    storage_path: str  # path ya subido al bucket 'materiales' de Supabase Storage


# ---------------- ENDPOINTS ----------------
@router.post("")
def registrar_material(m: MaterialIn, interrogador: dict = Depends(get_current_interrogador)):
    """El archivo ya debe estar subido al bucket 'materiales' desde el frontend;
    acá solo se registra su metadata."""
    row = m.model_dump()
    row["subido_por"] = interrogador["sub"]
    res = sb.table("materiales").insert(row).execute()
    return res.data[0]

@router.get("")
def listar_materiales(region: Optional[str] = None):
    """Público — los alumnos lo usan para ver qué hay disponible para descargar."""
    q = sb.table("materiales").select("*")
    if region:
        q = q.eq("region", region)
    rows = q.order("created_at", desc=True).execute().data
    for r in rows:
        r["url"] = sb.storage.from_("materiales").get_public_url(r["storage_path"])
    return rows
  
