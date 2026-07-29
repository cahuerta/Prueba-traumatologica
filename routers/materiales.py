"""
routers/materiales.py
PPT y resúmenes que suben los interrogadores, organizados por región.
El alumno entra por un QR fijo (nombre + RUT, validado contra el
conjunto de alumnos actualmente activo), y cada visita/descarga queda
registrada para el análisis.
"""

import time
from typing import Optional

from fastapi import APIRouter, Depends, Form, UploadFile, File, HTTPException
from pydantic import BaseModel

from routers.auth import sb, get_current_interrogador
from routers.conjuntos_comun import obtener_conjunto_activo_id

router = APIRouter(prefix="/materiales", tags=["materiales"])


# ---------------- MODELOS ----------------
class IngresoIn(BaseModel):
    nombre: str
    rut: str

class DescargaIn(BaseModel):
    alumno_id: str


# ---------------- SUBIR / LISTAR (interrogador / público) ----------------
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
        r["url"] = None  # el link real solo se entrega al registrar la descarga
    return rows


# ---------------- INGRESO DEL ALUMNO (QR fijo, nombre + RUT) ----------------
@router.post("/ingreso")
def ingreso_materiales(body: IngresoIn):
    """Valida contra el conjunto de alumnos actualmente activo.
    Si el RUT no existe en ese conjunto, no entra."""
    conjunto_id = obtener_conjunto_activo_id()

    alumno = sb.table("alumnos").select("id").eq("rut", body.rut.strip()).eq("conjunto_id", conjunto_id).execute().data
    if not alumno:
        raise HTTPException(403, "RUT no reconocido en el conjunto activo")
    alumno_id = alumno[0]["id"]

    sb.table("alumnos").update({"nombre": body.nombre.strip()}).eq("id", alumno_id).execute()
    sb.table("visitas_materiales").insert({"alumno_id": alumno_id}).execute()

    return {"ok": True, "alumno_id": alumno_id}


# ---------------- DESCARGA (registra y entrega el link real) ----------------
@router.post("/{material_id}/descargar")
def descargar_material(material_id: str, body: DescargaIn):
    material = sb.table("materiales").select("storage_path").eq("id", material_id).execute().data
    if not material:
        raise HTTPException(404, "Material no encontrado")

    sb.table("descargas_materiales").insert({
        "alumno_id": body.alumno_id, "material_id": material_id
    }).execute()

    url = sb.storage.from_("materiales").get_public_url(material[0]["storage_path"])
    return {"url": url}


# ---------------- DESCARGA ADMIN (revisar lo subido, sin registrar como descarga de alumno) ----------------
@router.get("/{material_id}/descargar-admin")
def descargar_material_admin(material_id: str, interrogador: dict = Depends(get_current_interrogador)):
    """El interrogador necesita poder bajar lo que subió para revisarlo o
    corregirlo afuera. No inserta en descargas_materiales -eso es
    exclusivo del flujo de alumnos, para no ensuciar las estadisticas de
    /materiales/analisis con aperturas del propio interrogador."""
    material = sb.table("materiales").select("storage_path").eq("id", material_id).execute().data
    if not material:
        raise HTTPException(404, "Material no encontrado")

    url = sb.storage.from_("materiales").get_public_url(material[0]["storage_path"])
    return {"url": url}


# ---------------- BORRAR (registro + archivo del storage) ----------------
@router.delete("/{material_id}")
def borrar_material(material_id: str, interrogador: dict = Depends(get_current_interrogador)):
    material = sb.table("materiales").select("storage_path").eq("id", material_id).execute().data
    if not material:
        raise HTTPException(404, "Material no encontrado")

    sb.storage.from_("materiales").remove([material[0]["storage_path"]])
    sb.table("materiales").delete().eq("id", material_id).execute()

    return {"ok": True}


# ---------------- ANÁLISIS (para el admin/interrogador) ----------------
@router.get("/analisis")
def analisis_materiales(interrogador: dict = Depends(get_current_interrogador)):
    conjunto_id = obtener_conjunto_activo_id()

    por_material = sb.table("analisis_descargas_material").select("*").execute().data
    por_alumno = sb.table("analisis_actividad_alumno").select("*").eq("conjunto_id", conjunto_id).order("total_visitas", desc=True).execute().data
    materiales_info = sb.table("materiales").select("id, region, tipo, titulo").execute().data

    materiales_map = {m["id"]: m for m in materiales_info}
    for fila in por_material:
        info = materiales_map.get(fila["material_id"], {})
        fila["region"] = info.get("region")
        fila["tipo"] = info.get("tipo")
        fila["titulo"] = info.get("titulo")

    return {"por_material": por_material, "por_alumno": por_alumno}
    
