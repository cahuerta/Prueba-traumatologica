"""
routers/conjuntos_comun.py
Helper compartido para obtener el conjunto (generación de alumnos, o
conjunto de prueba) actualmente activo. Lo usan los routers que necesitan
saber contra qué grupo de alumnos validar (materiales, casos_vivo_alumno,
sesiones, etc.).

Solo un conjunto puede estar activo a la vez -esa regla la aplica el
router de conjuntos al activar uno (desactivando los demás), no una
restricción a nivel de base de datos-.
"""

from fastapi import HTTPException

from routers.auth import sb


def obtener_conjunto_activo_id() -> str:
    """Devuelve el id del conjunto actualmente activo.
    Lanza 409 si no hay ninguno activo -evita que el resto del sistema
    opere en un estado ambiguo sin conjunto definido."""
    res = sb.table("conjuntos").select("id").eq("activo", True).execute().data
    if not res:
        raise HTTPException(409, "No hay un conjunto activo. Actívalo en Configuración.")
    return res[0]["id"]
  
