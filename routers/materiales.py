"""
routers/materiales.py
PPT y resúmenes que suben los interrogadores, organizados por región,
para que los alumnos los descarguen después.
El archivo llega directo al backend (multipart) y el backend lo sube
a Supabase Storage usando la service_key.
"""

import time
from typing import Optional

from fastapi import APIRouter, Depends, Form, UploadFile, File, HTTPException

from routers.auth import sb, get_current_interrogador

router = APIRouter(prefix="/materiales", tags=["materiales"])


# ---------------- ENDPOINTS ----------------
@router.post("")
async def subir_material(
    region: str = Form(...),
    tipo: str = Form(...),
    titulo: str = Form(...),
    archivo: UploadFile = File(...),
    interrogador: dict = Depends(get_current_interrogador),
):
    if tipo not in ("ppt", "resumen"):
        raise HTTPException(400, "Tipo inválido")

    contenido = await archivo.read()
    storage_path = f"{region}/{int(time.time())}-{archivo.filename}"

    sb.storage.from_("materiales").upload(
        storage_path, contenido, {"content-type": archivo.content_type}
    )

    res = sb.table("materiales").insert({
        "region": region,
        "tipo": tipo,
        "titulo": titulo,
        "storage_path": storage_path,
        "subido_por": interrogador["sub"],
    }).execute()

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
    
