"""
Backend — Examen Musculoesquelético
Módulo de autenticación propio (independiente de ICA y de Supabase Auth).

Variables de entorno esperadas (Render):
  SUPABASE_URL
  SUPABASE_SERVICE_KEY
  JWT_SECRET          (secreto propio de este proyecto, generado por ti)
"""

import os
import random
from datetime import datetime, timedelta, timezone
from typing import Optional, List

import bcrypt
import jwt
from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr
from supabase import create_client, Client

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
JWT_SECRET = os.environ["JWT_SECRET"]
JWT_ALGO = "HS256"

sb: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

# ---------------- PAQUETES (presets fijos de la encuesta) ----------------
PAQUETES = {
    "agil":     {"n": {"basica": 65, "intermedia": 10, "compleja": 5},  "minutos": 50},
    "estandar": {"n": {"basica": 30, "intermedia": 20, "compleja": 10}, "minutos": 70},
    "exigente": {"n": {"basica": 15, "intermedia": 15, "compleja": 10}, "minutos": 90},
}
PESO_BASE = {"basica": 1, "intermedia": 2, "compleja": 3}

app = FastAPI(title="Examen Musculoesquelético API")
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https://.*\.vercel\.app|http://localhost:\d+",
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------- HELPERS DE PASSWORD Y JWT ----------------
def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode(), password_hash.encode())

def crear_token(interrogador: dict) -> str:
    payload = {
        "sub": interrogador["id"],
        "email": interrogador["email"],
        "rol": interrogador["rol"],
        # sin expiración por ahora — se define más adelante
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGO)


# ---------------- DEPENDENCIAS DE AUTH ----------------
def get_current_interrogador(authorization: str = Header(...)) -> dict:
    token = authorization.replace("Bearer ", "")
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
    except jwt.PyJWTError:
        raise HTTPException(401, "Token inválido o expirado")
    return payload

def requiere_admin(interrogador: dict = Depends(get_current_interrogador)) -> dict:
    if interrogador["rol"] != "admin":
        raise HTTPException(403, "Solo el administrador puede hacer esto")
    return interrogador


# ---------------- MODELOS ----------------
class LoginIn(BaseModel):
    email: EmailStr
    password: str

class CrearInterrogadorIn(BaseModel):
    email: EmailStr
    password: str
    nombre: str
    rol: str = "interrogador"  # "admin" | "interrogador"

class PreguntaIn(BaseModel):
    region: str
    complejidad: str  # basica | intermedia | compleja
    pregunta: str
    opciones: List[str]
    correcta: int
    explicacion: Optional[str] = None

class MaterialIn(BaseModel):
    region: str
    tipo: str  # ppt | resumen
    titulo: str
    storage_path: str

class SesionIn(BaseModel):
    nombre: str
    fecha: str  # YYYY-MM-DD
    alumnos_ids: List[str]

class VotoIn(BaseModel):
    alumno_id: str
    paquete: str

class AsistenciaIn(BaseModel):
    alumno_id: str

class IniciarExamenIn(BaseModel):
    sesion_id: str
    alumno_id: str

class ResponderIn(BaseModel):
    pregunta_id: str
    opcion_elegida: int


# ---------------- ENDPOINTS ----------------
@app.post("/auth/login")
def login(body: LoginIn):
    res = sb.table("interrogadores").select("*").eq("email", body.email).eq("activo", True).execute()
    if not res.data:
        raise HTTPException(401, "Email o contraseña incorrectos")

    interrogador = res.data[0]
    if not verify_password(body.password, interrogador["password_hash"]):
        raise HTTPException(401, "Email o contraseña incorrectos")

    token = crear_token(interrogador)
    return {
        "token": token,
        "nombre": interrogador["nombre"],
        "rol": interrogador["rol"],
    }

@app.post("/auth/interrogadores")
def crear_interrogador(body: CrearInterrogadorIn, admin: dict = Depends(requiere_admin)):
    """Solo el admin (Cristóbal) puede crear cuentas nuevas."""
    if body.rol not in ("admin", "interrogador"):
        raise HTTPException(400, "Rol inválido")

    existe = sb.table("interrogadores").select("id").eq("email", body.email).execute()
    if existe.data:
        raise HTTPException(409, "Ya existe una cuenta con ese email")

    res = sb.table("interrogadores").insert({
        "email": body.email,
        "password_hash": hash_password(body.password),
        "nombre": body.nombre,
        "rol": body.rol,
    }).execute()

    nuevo = res.data[0]
    return {"id": nuevo["id"], "email": nuevo["email"], "nombre": nuevo["nombre"], "rol": nuevo["rol"]}

@app.get("/auth/me")
def me(interrogador: dict = Depends(get_current_interrogador)):
    return interrogador


# ---------------- BANCO DE PREGUNTAS (cualquier interrogador logueado) ----------------
@app.post("/preguntas")
def crear_pregunta(p: PreguntaIn, interrogador: dict = Depends(get_current_interrogador)):
    row = p.model_dump()
    row["creado_por"] = interrogador["sub"]
    res = sb.table("banco_preguntas").insert(row).execute()
    return res.data[0]

@app.post("/preguntas/bulk")
def crear_preguntas_bulk(preguntas: List[PreguntaIn], interrogador: dict = Depends(get_current_interrogador)):
    rows = [{**p.model_dump(), "creado_por": interrogador["sub"]} for p in preguntas]
    res = sb.table("banco_preguntas").insert(rows).execute()
    return {"insertadas": len(res.data)}

@app.get("/preguntas")
def listar_preguntas(region: Optional[str] = None, interrogador: dict = Depends(get_current_interrogador)):
    q = sb.table("banco_preguntas").select("*").eq("activo", True)
    if region:
        q = q.eq("region", region)
    return q.execute().data

@app.delete("/preguntas/{pregunta_id}")
def borrar_pregunta(pregunta_id: str, interrogador: dict = Depends(get_current_interrogador)):
    sb.table("banco_preguntas").update({"activo": False}).eq("id", pregunta_id).execute()
    return {"ok": True}


# ---------------- MATERIALES (subir: interrogador logueado / listar: público) ----------------
@app.post("/materiales")
def registrar_material(m: MaterialIn, interrogador: dict = Depends(get_current_interrogador)):
    row = m.model_dump()
    row["subido_por"] = interrogador["sub"]
    res = sb.table("materiales").insert(row).execute()
    return res.data[0]

@app.get("/materiales")
def listar_materiales(region: Optional[str] = None):
    q = sb.table("materiales").select("*")
    if region:
        q = q.eq("region", region)
    rows = q.order("created_at", desc=True).execute().data
    for r in rows:
        r["url"] = sb.storage.from_("materiales").get_public_url(r["storage_path"])
    return rows


# ---------------- SESIONES / ASISTENCIA (control: solo admin) ----------------
@app.post("/sesiones")
def crear_sesion(s: SesionIn, admin: dict = Depends(requiere_admin)):
    res = sb.table("sesiones_examen").insert({
        "nombre": s.nombre, "fecha": s.fecha, "estado": "creada", "creado_por": admin["sub"]
    }).execute()
    sesion = res.data[0]
    rows = [{"sesion_id": sesion["id"], "alumno_id": aid} for aid in s.alumnos_ids]
    if rows:
        sb.table("sesion_alumnos").insert(rows).execute()
    return sesion

@app.post("/sesiones/{sesion_id}/abrir-asistencia")
def abrir_asistencia(sesion_id: str, admin: dict = Depends(requiere_admin)):
    sb.table("sesiones_examen").update({"estado": "asistencia"}).eq("id", sesion_id).execute()
    return {"ok": True}

@app.get("/sesiones/{sesion_id}/alumnos")
def listar_alumnos_sesion(sesion_id: str):
    """Lista de nombre/RUT precargados, para que el alumno toque su nombre. Público (pantalla de asistencia)."""
    res = sb.table("sesion_alumnos").select("alumno_id, alumnos(id, nombre, rut)").eq("sesion_id", sesion_id).execute()
    return [r["alumnos"] for r in res.data]

@app.post("/sesiones/{sesion_id}/asistencia")
def marcar_asistencia(sesion_id: str, body: AsistenciaIn):
    sb.table("asistencia").upsert({"sesion_id": sesion_id, "alumno_id": body.alumno_id}).execute()
    return {"ok": True}

@app.get("/sesiones/{sesion_id}/asistencia")
def ver_asistencia(sesion_id: str):
    res = sb.table("asistencia").select("alumno_id, marcado_at, alumnos(nombre, rut)").eq("sesion_id", sesion_id).execute()
    return res.data


# ---------------- ENCUESTA EN VIVO ----------------
@app.post("/sesiones/{sesion_id}/abrir-encuesta")
def abrir_encuesta(sesion_id: str, admin: dict = Depends(requiere_admin)):
    sb.table("sesiones_examen").update({"estado": "encuesta"}).eq("id", sesion_id).execute()
    return {"ok": True}

@app.post("/sesiones/{sesion_id}/votar")
def votar(sesion_id: str, body: VotoIn):
    if body.paquete not in PAQUETES:
        raise HTTPException(400, "Paquete inválido")
    sb.table("encuesta_votos").upsert({
        "sesion_id": sesion_id, "alumno_id": body.alumno_id, "paquete": body.paquete
    }).execute()
    return {"ok": True}

@app.get("/sesiones/{sesion_id}/encuesta")
def ver_encuesta(sesion_id: str):
    res = sb.table("encuesta_votos").select("paquete").eq("sesion_id", sesion_id).execute()
    conteo = {"agil": 0, "estandar": 0, "exigente": 0}
    for r in res.data:
        conteo[r["paquete"]] += 1
    return conteo

@app.post("/sesiones/{sesion_id}/cerrar-encuesta")
def cerrar_encuesta(sesion_id: str, admin: dict = Depends(requiere_admin)):
    conteo = ver_encuesta(sesion_id)
    ganador = max(conteo, key=conteo.get)
    sb.table("sesiones_examen").update({
        "estado": "en_curso", "paquete_elegido": ganador
    }).eq("id", sesion_id).execute()
    return {"paquete_elegido": ganador, "conteo": conteo}


# ---------------- GENERACIÓN Y RENDICIÓN DEL EXAMEN (público, lo usa el alumno) ----------------
def _seleccionar_preguntas(paquete: str):
    cuotas = PAQUETES[paquete]["n"]
    seleccion = {}
    for complejidad, cantidad in cuotas.items():
        disponibles = sb.table("banco_preguntas").select("id").eq("activo", True).eq("complejidad", complejidad).execute().data
        ids = [r["id"] for r in disponibles]
        if len(ids) < cantidad:
            raise HTTPException(409, f"No hay suficientes preguntas '{complejidad}' en el banco ({len(ids)}/{cantidad})")
        seleccion[complejidad] = random.sample(ids, cantidad)
    return seleccion

def _calcular_puntos(seleccion: dict):
    total_peso = sum(PESO_BASE[c] * len(ids) for c, ids in seleccion.items())
    escala = 100 / total_peso
    puntos = {}
    for complejidad, ids in seleccion.items():
        valor = round(PESO_BASE[complejidad] * escala, 4)
        for qid in ids:
            puntos[qid] = valor
    return puntos

@app.post("/examen/iniciar")
def iniciar_examen(body: IniciarExamenIn):
    sesion = sb.table("sesiones_examen").select("*").eq("id", body.sesion_id).single().execute().data
    if not sesion or sesion["estado"] != "en_curso":
        raise HTTPException(409, "La sesión no está en curso todavía")

    existente = sb.table("examen_instancia").select("*").eq("sesion_id", body.sesion_id).eq("alumno_id", body.alumno_id).execute().data
    if existente:
        instancia = existente[0]
    else:
        paquete = sesion["paquete_elegido"]
        seleccion = _seleccionar_preguntas(paquete)
        puntos = _calcular_puntos(seleccion)
        pregunta_ids = [qid for ids in seleccion.values() for qid in ids]
        random.shuffle(pregunta_ids)
        res = sb.table("examen_instancia").insert({
            "sesion_id": body.sesion_id, "alumno_id": body.alumno_id, "paquete": paquete,
            "pregunta_ids": pregunta_ids, "puntos_por_pregunta": puntos,
        }).execute()
        instancia = res.data[0]

    preguntas = sb.table("banco_preguntas").select("id, region, pregunta, opciones").in_("id", instancia["pregunta_ids"]).execute().data
    orden = {qid: i for i, qid in enumerate(instancia["pregunta_ids"])}
    preguntas.sort(key=lambda q: orden[q["id"]])

    return {
        "instancia_id": instancia["id"],
        "paquete": instancia["paquete"],
        "minutos_totales": PAQUETES[instancia["paquete"]]["minutos"],
        "iniciado_at": instancia["iniciado_at"],
        "preguntas": preguntas,
    }

@app.post("/examen/{instancia_id}/responder")
def responder(instancia_id: str, body: ResponderIn):
    instancia = sb.table("examen_instancia").select("*").eq("id", instancia_id).single().execute().data
    if not instancia:
        raise HTTPException(404, "Examen no encontrado")
    if instancia["finalizado_at"]:
        raise HTTPException(409, "El examen ya fue finalizado")

    if not instancia["iniciado_at"]:
        sb.table("examen_instancia").update({"iniciado_at": datetime.now(timezone.utc).isoformat()}).eq("id", instancia_id).execute()

    pregunta = sb.table("banco_preguntas").select("correcta").eq("id", body.pregunta_id).single().execute().data
    correcta = pregunta["correcta"] == body.opcion_elegida

    sb.table("respuestas").upsert({
        "examen_instancia_id": instancia_id, "pregunta_id": body.pregunta_id,
        "opcion_elegida": body.opcion_elegida, "correcta": correcta,
    }).execute()
    return {"correcta": correcta}

@app.post("/examen/{instancia_id}/finalizar")
def finalizar_examen(instancia_id: str):
    instancia = sb.table("examen_instancia").select("*").eq("id", instancia_id).single().execute().data
    if not instancia:
        raise HTTPException(404, "Examen no encontrado")

    respuestas = sb.table("respuestas").select("pregunta_id, correcta").eq("examen_instancia_id", instancia_id).execute().data
    puntos_map = instancia["puntos_por_pregunta"]
    puntaje = sum(puntos_map[r["pregunta_id"]] for r in respuestas if r["correcta"])
    porcentaje = puntaje / 100

    if porcentaje <= 0.6:
        nota = 1 + (porcentaje / 0.6) * 3
    else:
        nota = 4 + ((porcentaje - 0.6) / 0.4) * 3

    sb.table("examen_instancia").update({
        "finalizado_at": datetime.now(timezone.utc).isoformat(),
        "puntaje_total": round(puntaje, 2),
        "porcentaje": round(porcentaje * 100, 1),
        "nota": round(nota, 1),
    }).eq("id", instancia_id).execute()

    return {"puntaje_total": round(puntaje, 2), "porcentaje": round(porcentaje * 100, 1), "nota": round(nota, 1)}


# ---------------- ANÁLISIS DOCENTE (cualquier interrogador logueado) ----------------
@app.get("/sesiones/{sesion_id}/analisis")
def analisis_sesion(sesion_id: str, interrogador: dict = Depends(get_current_interrogador)):
    resumen = sb.table("analisis_sesion").select("*").eq("sesion_id", sesion_id).execute().data
    preguntas = sb.table("analisis_preguntas").select("*").eq("sesion_id", sesion_id).order("pct_acierto").execute().data
    complejidad = sb.table("analisis_complejidad").select("*").eq("sesion_id", sesion_id).execute().data
    return {
        "resumen": resumen[0] if resumen else None,
        "preguntas_mas_falladas": preguntas[:10],
        "por_complejidad": complejidad,
            }
  
