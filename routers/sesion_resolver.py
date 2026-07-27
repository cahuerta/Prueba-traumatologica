"""
routers/sesion_resolver.py
Supraselector: dado un codigo_acceso, resuelve a que tipo de sesion
pertenece -"caso_clinico" (tabla sesiones_vivo) o "clases_formales"
(tabla sesiones_clase)- sin que quien pregunta necesite saberlo de
antemano.

Publico, sin auth: lo usan los tres lados que hoy no saben de antemano
el tipo de sesion:
  - El alumno, al leer el QR (que interfaz mostrarle)
  - El interrogador, en su control remoto (que panel de control abrir)
  - La pantalla de proyeccion (que vista renderizar)

Solo hace dos queries livianas (uno por tabla, ninguna con JOIN pesado)
y esto ocurre una vez por apertura de sesion -no es un endpoint de
polling de alta frecuencia-, asi que no necesita cache propio.
"""

from fastapi import APIRouter, HTTPException

from routers.auth import sb

router = APIRouter(prefix="/sesion-activa", tags=["sesion-resolver"])


@router.get("/{codigo}")
def resolver_sesion(codigo: str):
    """Devuelve {"tipo": "caso_clinico" | "clases_formales", "sesion_id": ...}
    para que el caller sepa hacia donde seguir. 404 si el codigo no
    existe en ninguna de las dos tablas."""
    caso_clinico = (
        sb.table("sesiones_vivo")
        .select("id")
        .eq("codigo_acceso", codigo)
        .execute()
        .data
    )
    if caso_clinico:
        return {"tipo": "caso_clinico", "sesion_id": caso_clinico[0]["id"]}

    clases_formales = (
        sb.table("sesiones_clase")
        .select("id")
        .eq("codigo_acceso", codigo)
        .execute()
        .data
    )
    if clases_formales:
        return {"tipo": "clases_formales", "sesion_id": clases_formales[0]["id"]}

    raise HTTPException(404, "Codigo de sesion invalido")
  
