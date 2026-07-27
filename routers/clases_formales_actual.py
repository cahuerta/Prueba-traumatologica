"""
routers/clases_formales_actual.py
Lectura publica (sin auth) de "cual pagina esta activa ahora mismo" en
una sesion de Clases Formales en vivo.

Solo la consumen admin (control remoto, para saber que esta mostrando
la proyeccion) y la pantalla de proyeccion (para renderizar el
contenido). El alumno NUNCA consulta esto -su pantalla es fija:
preguntas anonimas + semaforo, sin depender de en que pagina va la
clase-.

Separado de clases_formales_sesiones.py -que es 100% interrogador con
auth- siguiendo el mismo patron de casos_vivo_profesor.py (control) vs
casos_vivo_alumno.py (publico) que ya existe en el proyecto.
"""

from fastapi import APIRouter, HTTPException

from routers.auth import sb

router = APIRouter(prefix="/clases-formales/actual", tags=["clases-formales-actual"])


@router.get("/{codigo}")
def pagina_actual(codigo: str):
    """Resuelve por codigo_acceso y devuelve la pagina activa completa
    (titulo, tipo_herramienta, config). 404 si el codigo no existe o si
    la sesion aun no tiene ninguna pagina activa (no se ha activado, o
    no tiene paginas creadas)."""
    sesion = (
        sb.table("sesiones_clase")
        .select("id, pagina_actual_orden")
        .eq("codigo_acceso", codigo)
        .execute()
        .data
    )
    if not sesion:
        raise HTTPException(404, "Codigo de sesion invalido")
    sesion = sesion[0]

    if sesion["pagina_actual_orden"] is None:
        raise HTTPException(404, "La sesion aun no tiene una pagina activa")

    pagina = (
        sb.table("paginas_clase")
        .select("id, titulo, tipo_herramienta, config")
        .eq("sesion_id", sesion["id"])
        .eq("orden", sesion["pagina_actual_orden"])
        .execute()
        .data
    )
    if not pagina:
        raise HTTPException(404, "Pagina activa no encontrada")

    return pagina[0]
  
