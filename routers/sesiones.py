"""
routers/sesiones.py
Ciclo de vida de una sesión de examen ("clase"): alumnos habilitados
(creados/vinculados desde nombre+RUT, pegados desde Excel), asistencia
por nombre+RUT (sin lista clicable, se corrobora contra los habilitados),
encuesta en vivo, y la tabla de resultados con el detalle de cada
pregunta respondida.
"""

from typing import List

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel

from routers.auth import sb, get_current_interrogador, requiere_admin

router = APIRouter(prefix="/sesiones", tags=["sesiones"])

PAQUETES = ("agil", "estandar", "exigente")


# ---------------- MODELOS ----------------
class AlumnoIn(BaseModel):
    nombre: str
    rut: str

class SesionIn(BaseModel):
    nombre: str
    fecha: str  # YYYY-MM-DD
    alumnos: List[AlumnoIn]  # pegados desde Excel: nombre + RUT por fila

class VotoIn(BaseModel):
    alumno_id: str
    paquete: str

class AsistenciaIn(BaseModel):
    nombre: str
    rut: str


# ---------------- CREACIÓN Y LISTADO DE SESIONES (admin) ----------------
@router.post("")
def crear_sesion(s: SesionIn, admin: dict = Depends(requiere_admin)):
    res = sb.table("sesiones_examen").insert({
        "nombre": s.nombre, "fecha": s.fecha, "estado": "creada", "creado_por": admin["sub"]
    }).execute()
    sesion = res.data[0]

    alumno_ids = []
    for a in s.alumnos:
        rut = a.rut.strip()
        nombre = a.nombre.strip()
        if not rut:
            continue
        existente = sb.table("alumnos").select("id").eq("rut", rut).execute().data
        if existente:
            alumno_id = existente[0]["id"]
            if nombre:
                sb.table("alumnos").update({"nombre": nombre}).eq("id", alumno_id).execute()
        else:
            nuevo = sb.table("alumnos").insert({"rut": rut, "nombre": nombre}).execute().data[0]
            alumno_id = nuevo["id"]
        alumno_ids.append(alumno_id)

    rows = [{"sesion_id": sesion["id"], "alumno_id": aid} for aid in alumno_ids]
    if rows:
        sb.table("sesion_alumnos").insert(rows).execute()

    return sesion

@router.get("")
def listar_sesiones(interrogador: dict = Depends(get_current_interrogador)):
    return sb.table("sesiones_examen").select("*").order("fecha", desc=True).execute().data

@router.get("/{sesion_id}")
def ver_sesion(sesion_id: str, interrogador: dict = Depends(get_current_interrogador)):
    res = sb.table("sesiones_examen").select("*").eq("id", sesion_id).single().execute().data
    if not res:
        raise HTTPException(404, "Sesión no encontrada")
    return res


# ---------------- ASISTENCIA (ingreso por nombre + RUT, sin lista clicable) ----------------
@router.post("/{sesion_id}/abrir-asistencia")
def abrir_asistencia(sesion_id: str, admin: dict = Depends(requiere_admin)):
    sb.table("sesiones_examen").update({"estado": "asistencia"}).eq("id", sesion_id).execute()
    return {"ok": True}

@router.post("/{sesion_id}/asistencia")
def marcar_asistencia(sesion_id: str, body: AsistenciaIn):
    """El alumno escribe su nombre y RUT. Se corrobora contra los habilitados de esta sesión."""
    alumno = sb.table("alumnos").select("id").eq("rut", body.rut.strip()).execute().data
    if not alumno:
        raise HTTPException(403, "RUT no habilitado para esta sesión")
    alumno_id = alumno[0]["id"]

    habilitado = sb.table("sesion_alumnos").select("alumno_id").eq("sesion_id", sesion_id).eq("alumno_id", alumno_id).execute().data
    if not habilitado:
        raise HTTPException(403, "Este alumno no está habilitado para esta sesión")

    sb.table("alumnos").update({"nombre": body.nombre.strip()}).eq("id", alumno_id).execute()
    sb.table("asistencia").upsert({"sesion_id": sesion_id, "alumno_id": alumno_id}).execute()

    return {"ok": True, "alumno_id": alumno_id}

@router.get("/{sesion_id}/asistencia")
def ver_asistencia(sesion_id: str, interrogador: dict = Depends(get_current_interrogador)):
    """Consola docente: quién ha marcado asistencia hasta ahora, en vivo."""
    res = sb.table("asistencia").select("alumno_id, marcado_at, alumnos(nombre, rut)").eq("sesion_id", sesion_id).order("marcado_at").execute().data
    total = sb.table("sesion_alumnos").select("alumno_id", count="exact").eq("sesion_id", sesion_id).execute()
    return {"presentes": res, "total_habilitados": total.count, "total_presentes": len(res)}


# ---------------- ENCUESTA EN VIVO (Ágil / Estándar / Exigente) ----------------
@router.post("/{sesion_id}/abrir-encuesta")
def abrir_encuesta(sesion_id: str, admin: dict = Depends(requiere_admin)):
    sb.table("sesiones_examen").update({"estado": "encuesta"}).eq("id", sesion_id).execute()
    return {"ok": True}

@router.post("/{sesion_id}/votar")
def votar(sesion_id: str, body: VotoIn):
    if body.paquete not in PAQUETES:
        raise HTTPException(400, "Paquete inválido")
    sb.table("encuesta_votos").upsert({
        "sesion_id": sesion_id, "alumno_id": body.alumno_id, "paquete": body.paquete
    }).execute()
    return {"ok": True}

@router.get("/{sesion_id}/encuesta")
def ver_encuesta(sesion_id: str):
    res = sb.table("encuesta_votos").select("paquete").eq("sesion_id", sesion_id).execute()
    conteo = {"agil": 0, "estandar": 0, "exigente": 0}
    for r in res.data:
        conteo[r["paquete"]] += 1
    return conteo

@router.post("/{sesion_id}/cerrar-encuesta")
def cerrar_encuesta(sesion_id: str, admin: dict = Depends(requiere_admin)):
    conteo = ver_encuesta(sesion_id)
    ganador = max(conteo, key=conteo.get)
    sb.table("sesiones_examen").update({
        "estado": "en_curso", "paquete_elegido": ganador
    }).eq("id", sesion_id).execute()
    return {"paquete_elegido": ganador, "conteo": conteo}


# ---------------- TABLA DE RESULTADOS (detalle pregunta por pregunta) ----------------
@router.get("/{sesion_id}/resultados")
def resultados_sesion(sesion_id: str, interrogador: dict = Depends(get_current_interrogador)):
    """
    Por cada alumno que rindió: su nota/puntaje, y el detalle de cada
    pregunta que le tocó (enunciado, su respuesta, si fue correcta,
    la alternativa correcta y la explicación).
    """
    instancias = sb.table("examen_instancia").select(
        "id, alumno_id, paquete, puntaje_total, porcentaje, nota, iniciado_at, finalizado_at, alumnos(nombre, rut)"
    ).eq("sesion_id", sesion_id).execute().data

    resultados = []
    for inst in instancias:
        respuestas = sb.table("respuestas").select(
            "pregunta_id, opcion_elegida, correcta, banco_preguntas(pregunta, opciones, correcta, explicacion, region, complejidad)"
        ).eq("examen_instancia_id", inst["id"]).execute().data

        detalle = []
        for r in respuestas:
            bp = r["banco_preguntas"]
            detalle.append({
                "pregunta": bp["pregunta"],
                "region": bp["region"],
                "complejidad": bp["complejidad"],
                "opciones": bp["opciones"],
                "respuesta_alumno": bp["opciones"][r["opcion_elegida"]],
                "respuesta_correcta": bp["opciones"][bp["correcta"]],
                "correcta": r["correcta"],
                "explicacion": bp["explicacion"],
            })

        resultados.append({
            "alumno": inst["alumnos"],
            "paquete": inst["paquete"],
            "puntaje_total": inst["puntaje_total"],
            "porcentaje": inst["porcentaje"],
            "nota": inst["nota"],
            "finalizado": inst["finalizado_at"] is not None,
            "n_preguntas_respondidas": len(detalle),
            "n_correctas": sum(1 for d in detalle if d["correcta"]),
            "n_incorrectas": sum(1 for d in detalle if not d["correcta"]),
            "detalle_preguntas": detalle,
        })

    return resultados
    
