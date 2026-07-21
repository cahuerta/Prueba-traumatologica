"""
routers/casos_vivo.py
Presentacion dinamica en vivo - alternativa al PPT (no lo reemplaza, coexiste).

- Casos clinicos: viñeta + foto/video opcional (bucket privado "casos"),
  con sus preguntas del banco_preguntas ya existente, en orden fijo (1-5).
- Presentaciones: varios casos, en el orden que el interrogador decida.
  Se arman con anticipacion y se reutilizan (equivalente a guardar el PPT).
- Sesion en vivo: corre sobre una presentacion. El alumno entra con
  nombre + RUT (reusa tabla "alumnos"), vota por pregunta. El profesor
  ve el detalle nombre->opcion para pedir fundamento oral; la pantalla
  proyectada solo muestra el agregado. Al revelar, se busca en los
  materiales (docx) de la region del caso y Claude redacta un resumen
  corto y fundamentado citando la fuente (services/fundamento_vivo.py).
  Avance de pregunta/caso es siempre en orden fijo (no se puede saltar).
"""

import random
import string
import time
from typing import Optional

from fastapi import APIRouter, Depends, Form, UploadFile, File, HTTPException
from pydantic import BaseModel

from routers.auth import sb, get_current_interrogador
from services.fundamento_vivo import buscar_fundamento

router = APIRouter(prefix="/casos-vivo", tags=["casos-vivo"])


class CasoIn(BaseModel):
    region: str
    titulo: str
    vineta_clinica: str

class PreguntaCasoIn(BaseModel):
    pregunta_id: str
    orden: int

class PresentacionIn(BaseModel):
    titulo: str
    region: Optional[str] = None

class PresentacionCasoIn(BaseModel):
    caso_id: str
    orden: int

class IniciarSesionIn(BaseModel):
    presentacion_id: str

class IngresoAlumnoIn(BaseModel):
    nombre: str
    rut: str

class VotarIn(BaseModel):
    sesion_id: str
    alumno_id: str
    pregunta_id: str
    opcion: int

class AccionIn(BaseModel):
    accion: str


@router.post("/casos")
def crear_caso(c: CasoIn, interrogador: dict = Depends(get_current_interrogador)):
    row = c.model_dump()
    row["creado_por"] = interrogador["sub"]
    res = sb.table("casos_clinicos").insert(row).execute()
    return res.data[0]

@router.post("/casos/media")
async def subir_media_caso(
    tipo: str = Form(...),
    archivo: UploadFile = File(...),
    interrogador: dict = Depends(get_current_interrogador),
):
    if tipo not in ("foto", "video"):
        raise HTTPException(400, "Tipo invalido, debe ser 'foto' o 'video'")

    contenido = await archivo.read()
    storage_path = f"{int(time.time())}-{archivo.filename}"

    sb.storage.from_("casos").upload(
        storage_path, contenido, {"content-type": archivo.content_type}
    )

    return {"media_url": storage_path, "media_tipo": tipo}

@router.post("/casos/{caso_id}/media")
def asociar_media_caso(caso_id: str, media_url: str, media_tipo: str, interrogador: dict = Depends(get_current_interrogador)):
    if media_tipo not in ("foto", "video"):
        raise HTTPException(400, "Tipo invalido")
    res = sb.table("casos_clinicos").update({
        "media_url": media_url, "media_tipo": media_tipo
    }).eq("id", caso_id).execute()
    if not res.data:
        raise HTTPException(404, "Caso no encontrado")
    return res.data[0]

@router.get("/casos/{caso_id}/media")
def obtener_media_caso(caso_id: str, interrogador: dict = Depends(get_current_interrogador)):
    c = sb.table("casos_clinicos").select("media_url").eq("id", caso_id).single().execute().data
    if not c or not c["media_url"]:
        raise HTTPException(404, "Este caso no tiene foto/video")
    firmada = sb.storage.from_("casos").create_signed_url(c["media_url"], 300)
    url = firmada.get("signedURL") or firmada.get("signed_url")
    return {"url": url}

@router.get("/casos")
def listar_casos(region: Optional[str] = None, interrogador: dict = Depends(get_current_interrogador)):
    q = sb.table("casos_clinicos").select("*")
    if region:
        q = q.eq("region", region)
    return q.order("created_at", desc=True).execute().data

@router.get("/casos/{caso_id}")
def obtener_caso(caso_id: str, interrogador: dict = Depends(get_current_interrogador)):
    caso = sb.table("casos_clinicos").select("*").eq("id", caso_id).single().execute().data
    if not caso:
        raise HTTPException(404, "Caso no encontrado")

    puente = sb.table("caso_preguntas").select(
        "id, orden, pregunta_id, banco_preguntas(id, pregunta, opciones, correcta, explicacion, media_url, media_tipo, complejidad)"
    ).eq("caso_id", caso_id).order("orden").execute().data

    caso["preguntas"] = puente
    return caso

@router.post("/casos/{caso_id}/preguntas")
def agregar_pregunta_caso(caso_id: str, p: PreguntaCasoIn, interrogador: dict = Depends(get_current_interrogador)):
    if not (1 <= p.orden <= 5):
        raise HTTPException(400, "El orden debe estar entre 1 y 5")

    existe_orden = sb.table("caso_preguntas").select("id").eq("caso_id", caso_id).eq("orden", p.orden).execute().data
    if existe_orden:
        raise HTTPException(409, f"Ya existe una pregunta en el orden {p.orden} para este caso")

    res = sb.table("caso_preguntas").insert({
        "caso_id": caso_id, "pregunta_id": p.pregunta_id, "orden": p.orden
    }).execute()
    return res.data[0]

@router.delete("/casos/{caso_id}/preguntas/{caso_pregunta_id}")
def quitar_pregunta_caso(caso_id: str, caso_pregunta_id: str, interrogador: dict = Depends(get_current_interrogador)):
    sb.table("caso_preguntas").delete().eq("id", caso_pregunta_id).eq("caso_id", caso_id).execute()
    return {"ok": True}


@router.post("/presentaciones")
def crear_presentacion(p: PresentacionIn, interrogador: dict = Depends(get_current_interrogador)):
    row = p.model_dump()
    row["creado_por"] = interrogador["sub"]
    res = sb.table("presentaciones").insert(row).execute()
    return res.data[0]

@router.get("/presentaciones")
def listar_presentaciones(interrogador: dict = Depends(get_current_interrogador)):
    return sb.table("presentaciones").select("*").order("created_at", desc=True).execute().data

@router.get("/presentaciones/{presentacion_id}")
def obtener_presentacion(presentacion_id: str, interrogador: dict = Depends(get_current_interrogador)):
    pres = sb.table("presentaciones").select("*").eq("id", presentacion_id).single().execute().data
    if not pres:
        raise HTTPException(404, "Presentacion no encontrada")

    casos = sb.table("presentacion_casos").select(
        "id, orden, casos_clinicos(id, titulo, region, vineta_clinica, media_url, media_tipo)"
    ).eq("presentacion_id", presentacion_id).order("orden").execute().data

    for c in casos:
        caso_id = c["casos_clinicos"]["id"]
        preguntas = sb.table("caso_preguntas").select(
            "orden, pregunta_id, banco_preguntas(pregunta, opciones)"
        ).eq("caso_id", caso_id).order("orden").execute().data
        c["preguntas"] = preguntas

    pres["casos"] = casos
    return pres

@router.post("/presentaciones/{presentacion_id}/casos")
def agregar_caso_presentacion(presentacion_id: str, pc: PresentacionCasoIn, interrogador: dict = Depends(get_current_interrogador)):
    existe_orden = sb.table("presentacion_casos").select("id").eq("presentacion_id", presentacion_id).eq("orden", pc.orden).execute().data
    if existe_orden:
        raise HTTPException(409, f"Ya existe un caso en el orden {pc.orden} de esta presentacion")

    res = sb.table("presentacion_casos").insert({
        "presentacion_id": presentacion_id, "caso_id": pc.caso_id, "orden": pc.orden
    }).execute()
    return res.data[0]

@router.delete("/presentaciones/{presentacion_id}/casos/{presentacion_caso_id}")
def quitar_caso_presentacion(presentacion_id: str, presentacion_caso_id: str, interrogador: dict = Depends(get_current_interrogador)):
    sb.table("presentacion_casos").delete().eq("id", presentacion_caso_id).eq("presentacion_id", presentacion_id).execute()
    return {"ok": True}


def _generar_codigo(largo: int = 6) -> str:
    alfabeto = string.ascii_uppercase + string.digits
    return "".join(random.choices(alfabeto, k=largo))

def _obtener_sesion(sesion_id: str) -> dict:
    sesion = sb.table("sesiones_vivo").select("*").eq("id", sesion_id).single().execute().data
    if not sesion:
        raise HTTPException(404, "Sesion no encontrada")
    return sesion

def _caso_actual(sesion: dict) -> Optional[dict]:
    puente = sb.table("presentacion_casos").select(
        "casos_clinicos(id, region, titulo)"
    ).eq("presentacion_id", sesion["presentacion_id"]).eq("orden", sesion["caso_actual_orden"]).execute().data
    return puente[0]["casos_clinicos"] if puente else None

def _pregunta_actual(sesion: dict) -> Optional[dict]:
    caso = _caso_actual(sesion)
    if not caso:
        return None

    pregunta_puente = sb.table("caso_preguntas").select(
        "pregunta_id, banco_preguntas(id, pregunta, opciones, correcta, explicacion, media_url, media_tipo)"
    ).eq("caso_id", caso["id"]).eq("orden", sesion["pregunta_actual_orden"]).execute().data
    if not pregunta_puente:
        return None

    return {"caso": caso, **pregunta_puente[0]}

def _total_preguntas_caso(caso_id: str) -> int:
    filas = sb.table("caso_preguntas").select("id").eq("caso_id", caso_id).execute().data
    return len(filas)

def _total_casos_presentacion(presentacion_id: str) -> int:
    filas = sb.table("presentacion_casos").select("id").eq("presentacion_id", presentacion_id).execute().data
    return len(filas)


@router.post("/vivo/iniciar")
def iniciar_sesion(body: IniciarSesionIn, interrogador: dict = Depends(get_current_interrogador)):
    codigo = _generar_codigo()
    while sb.table("sesiones_vivo").select("id").eq("codigo_acceso", codigo).execute().data:
        codigo = _generar_codigo()

    res = sb.table("sesiones_vivo").insert({
        "presentacion_id": body.presentacion_id,
        "codigo_acceso": codigo,
        "estado": "esperando",
        "caso_actual_orden": 1,
        "pregunta_actual_orden": 1,
        "creado_por": interrogador["sub"],
    }).execute()
    return res.data[0]

@router.post("/vivo/{codigo}/ingreso")
def ingreso_alumno_vivo(codigo: str, body: IngresoAlumnoIn):
    sesion = sb.table("sesiones_vivo").select("id").eq("codigo_acceso", codigo).execute().data
    if not sesion:
        raise HTTPException(404, "Codigo de sesion invalido")

    alumno = sb.table("alumnos").select("id").eq("rut", body.rut.strip()).execute().data
    if not alumno:
        raise HTTPException(403, "RUT no reconocido en el sistema")
    alumno_id = alumno[0]["id"]

    sb.table("alumnos").update({"nombre": body.nombre.strip()}).eq("id", alumno_id).execute()

    return {"sesion_id": sesion[0]["id"], "alumno_id": alumno_id}

@router.get("/vivo/{codigo}/actual")
def estado_actual_alumno(codigo: str):
    sesion = sb.table("sesiones_vivo").select("*").eq("codigo_acceso", codigo).single().execute().data
    if not sesion:
        raise HTTPException(404, "Codigo de sesion invalido")

    pregunta = _pregunta_actual(sesion)
    if not pregunta:
        return {"estado": sesion["estado"], "pregunta": None}

    bp = pregunta["banco_preguntas"]
    salida = {
        "estado": sesion["estado"],
        "sesion_id": sesion["id"],
        "pregunta_id": bp["id"],
        "pregunta": bp["pregunta"],
        "opciones": bp["opciones"],
        "media_url": bp["media_url"],
        "media_tipo": bp["media_tipo"],
        "caso_actual_orden": sesion["caso_actual_orden"],
        "pregunta_actual_orden": sesion["pregunta_actual_orden"],
    }
    if sesion["estado"] == "cerrada":
        salida["correcta"] = bp["correcta"]
        salida["explicacion"] = sesion.get("explicacion_vivo") or bp["explicacion"]
        salida["fuentes"] = sesion.get("fuentes_vivo") or []
    return salida

@router.get("/vivo/{sesion_id}/resultados")
def resultados_agregados(sesion_id: str):
    sesion = _obtener_sesion(sesion_id)
    pregunta = _pregunta_actual(sesion)
    if not pregunta:
        return {"total": 0, "conteo": {}}

    pregunta_id = pregunta["banco_preguntas"]["id"]
    votos = sb.table("votos_vivo").select("opcion").eq(
        "sesion_id", sesion_id
    ).eq("pregunta_id", pregunta_id).execute().data

    conteo = {}
    for v in votos:
        conteo[v["opcion"]] = conteo.get(v["opcion"], 0) + 1

    return {"total": len(votos), "conteo": conteo}

@router.get("/vivo/{sesion_id}/detalle")
def detalle_votos(sesion_id: str, interrogador: dict = Depends(get_current_interrogador)):
    sesion = _obtener_sesion(sesion_id)
    pregunta = _pregunta_actual(sesion)
    if not pregunta:
        return []

    pregunta_id = pregunta["banco_preguntas"]["id"]
    votos = sb.table("votos_vivo").select(
        "opcion, created_at, alumnos(id, nombre, rut)"
    ).eq("sesion_id", sesion_id).eq("pregunta_id", pregunta_id).order("created_at").execute().data

    return votos

@router.post("/vivo/votar")
def votar(body: VotarIn):
    ya_voto = sb.table("votos_vivo").select("id").eq(
        "sesion_id", body.sesion_id
    ).eq("pregunta_id", body.pregunta_id).eq("alumno_id", body.alumno_id).execute().data
    if ya_voto:
        raise HTTPException(409, "Este alumno ya voto esta pregunta")

    sesion = _obtener_sesion(body.sesion_id)
    if sesion["estado"] != "votando":
        raise HTTPException(409, "La votacion no esta abierta en este momento")

    res = sb.table("votos_vivo").insert({
        "sesion_id": body.sesion_id,
        "pregunta_id": body.pregunta_id,
        "alumno_id": body.alumno_id,
        "opcion": body.opcion,
    }).execute()
    return res.data[0]

@router.post("/vivo/{sesion_id}/accion")
def avanzar_sesion(sesion_id: str, body: AccionIn, interrogador: dict = Depends(get_current_interrogador)):
    sesion = _obtener_sesion(sesion_id)

    if body.accion == "abrir_votacion":
        sb.table("sesiones_vivo").update({"estado": "votando"}).eq("id", sesion_id).execute()

    elif body.accion == "cerrar_votacion":
        sb.table("sesiones_vivo").update({"estado": "discusion"}).eq("id", sesion_id).execute()

    elif body.accion == "revelar":
        pregunta = _pregunta_actual(sesion)
        if not pregunta:
            raise HTTPException(409, "No hay pregunta activa para revelar")

        bp = pregunta["banco_preguntas"]
        caso = pregunta["caso"]

        fundamento = buscar_fundamento(
            region=caso["region"],
            pregunta=bp["pregunta"],
            opciones=bp["opciones"],
            correcta=bp["correcta"],
        )

        explicacion_final = fundamento["explicacion"] or bp["explicacion"] or ""

        sb.table("sesiones_vivo").update({
            "estado": "cerrada",
            "explicacion_vivo": explicacion_final,
            "fuentes_vivo": fundamento["fuentes"],
        }).eq("id", sesion_id).execute()

    elif body.accion == "siguiente":
        caso = _caso_actual(sesion)
        if not caso:
            raise HTTPException(409, "No hay caso activo")

        total_preguntas = _total_preguntas_caso(caso["id"])
        siguiente_pregunta = sesion["pregunta_actual_orden"] + 1

        if siguiente_pregunta <= total_preguntas:
            sb.table("sesiones_vivo").update({
                "pregunta_actual_orden": siguiente_pregunta,
                "estado": "esperando",
                "explicacion_vivo": None,
                "fuentes_vivo": None,
            }).eq("id", sesion_id).execute()
        else:
            total_casos = _total_casos_presentacion(sesion["presentacion_id"])
            siguiente_caso = sesion["caso_actual_orden"] + 1
            if siguiente_caso <= total_casos:
                sb.table("sesiones_vivo").update({
                    "caso_actual_orden": siguiente_caso,
                    "pregunta_actual_orden": 1,
                    "estado": "esperando",
                    "explicacion_vivo": None,
                    "fuentes_vivo": None,
                }).eq("id", sesion_id).execute()
            else:
                sb.table("sesiones_vivo").update({"estado": "cerrada"}).eq("id", sesion_id).execute()
                return {"ok": True, "finalizada": True}
    else:
        raise HTTPException(400, "Accion invalida")

    return _obtener_sesion(sesion_id)
