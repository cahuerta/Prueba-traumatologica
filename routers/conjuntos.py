"""
routers/conjuntos.py
Gestión de conjuntos (paquetes de alumnos): una generación por año, o un
conjunto de prueba con pocos alumnos para testear el sistema antes de
usar la generación oficial. Solo uno puede estar activo a la vez -eso lo
controla el endpoint de activar, desactivando los demás en la misma
operación-. Todo el sistema (ingreso de alumnos, materiales, sesiones,
casos vivo) opera siempre contra el conjunto activo.

Crear y activar son exclusivos del admin: solo él decide qué generación
o conjunto de alumnos está en uso. Los interrogadores trabajan con el
conjunto que el admin haya dejado activo, sin poder cambiarlo.
"""

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel

from routers.auth import sb, get_current_interrogador, requiere_admin

router = APIRouter(prefix="/conjuntos", tags=["conjuntos"])

TIPOS_VALIDOS = ("oficial", "test")


# ---------------- MODELOS ----------------
class ConjuntoIn(BaseModel):
    nombre: str
    tipo: str = "oficial"  # "oficial" | "test"


# ---------------- ENDPOINTS ----------------
@router.get("")
def listar_conjuntos(interrogador: dict = Depends(get_current_interrogador)):
    return sb.table("conjuntos").select("*").order("created_at", desc=True).execute().data


@router.post("")
def crear_conjunto(body: ConjuntoIn, admin: dict = Depends(requiere_admin)):
    """Solo el admin crea conjuntos nuevos (generación de un año, o un
    conjunto de prueba)."""
    if body.tipo not in TIPOS_VALIDOS:
        raise HTTPException(400, "Tipo inválido")

    res = sb.table("conjuntos").insert({
        "nombre": body.nombre.strip(),
        "tipo": body.tipo,
    }).execute()

    return res.data[0]


@router.post("/{conjunto_id}/activar")
def activar_conjunto(conjunto_id: str, admin: dict = Depends(requiere_admin)):
    """Solo el admin activa un conjunto. Desactiva todos los demás en la
    misma operación, para garantizar que exista un único activo."""
    existe = sb.table("conjuntos").select("id").eq("id", conjunto_id).execute().data
    if not existe:
        raise HTTPException(404, "Conjunto no encontrado")

    sb.table("conjuntos").update({"activo": False}).eq("activo", True).execute()
    sb.table("conjuntos").update({"activo": True}).eq("id", conjunto_id).execute()

    return {"ok": True, "conjunto_id": conjunto_id}
  
