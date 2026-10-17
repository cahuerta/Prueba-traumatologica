"""
services/importar_docx.py
Lector de exámenes en Word (.docx) para cargar preguntas al banco.

Lo usa routers/preguntas.py (carga desde la página de administración).

Reglas de lectura:
  - Una pregunta = enunciado + 5 alternativas.
  - La respuesta correcta es la alternativa escrita en NEGRITA.
  - Las imágenes (radiografías) del enunciado quedan asociadas a su pregunta.
  - Todas las preguntas se proponen con complejidad "basica"; la región se
    asigna por palabras clave (se corrige después al editar).
  - Las preguntas que dependen de otra ("la pregunta anterior", "en este
    caso") quedan marcadas como "por revisar", con una nota.
"""

import hashlib
import io
import re

import docx
from docx.oxml.ns import qn

REGIONES_VALIDAS = {
    "hombro", "codo", "mano_muneca", "columna", "cadera_pelvis", "rodilla",
    "tobillo_pie", "ortogeriatria", "imagenologia", "ciencias_basicas",
}

# Región por palabras clave (se revisa en orden; la primera que calza gana).
# Es solo un punto de partida: la región se corrige después en la pantalla
# de edición del banco.
REGLAS_REGION = [
    ("columna", ["adams", "escoliosis", "columna", "lumbar", "vertebral", "risser"]),
    ("hombro", ["manguito", "hombro", "clavícula", "acromioclavicular", "húmero proximal", "humero proximal"]),
    ("codo", ["codo", "olécranon", "olecranon", "cúpula radial", "cupula radial"]),
    ("mano_muneca", ["muñeca", "muneca", "escafoides", "trapecio", "estiloides"]),
    ("cadera_pelvis", ["cadera", "pelvis", "pertroc", "pubiana"]),
    ("rodilla", ["rodilla", "menisco", "lca", "lcp", "colateral"]),
    ("tobillo_pie", ["tobillo", "pilón", "pilon", "mortaja", "subtalar"]),
    ("imagenologia", ["técnicas de imagen", "tecnicas de imagen"]),
    ("ortogeriatria", ["fragilidad", "adultos mayores"]),
    ("ciencias_basicas", [
        "cartílago", "cartilago", "colágeno", "colageno", "consolidación", "consolidacion",
        "pseudoartrosis", "compartimental", "tumores", "expuesta", "energía", "energia",
        "unión", "union",
    ]),
]

# Ajustes de región hechos a mano para el examen "Prueba de Especialidades II
# Traumatología 2025-1" (clave = primeros 12 caracteres del SHA-256 del
# archivo, valor = {número de pregunta: región}). Con otro archivo no se
# aplican, y las regiones quedan solo por palabras clave.
AJUSTES_REGION = {
    "5bf3b5e96a1e": {
        13: "hombro", 18: "mano_muneca", 24: "cadera_pelvis", 25: "cadera_pelvis",
        26: "cadera_pelvis", 29: "tobillo_pie", 30: "tobillo_pie", 31: "tobillo_pie",
    },
}

# Marcas de que la pregunta depende de otra (pierde sentido si se mezcla).
PATRON_DEPENDIENTE = re.compile(
    r"pregunta anterior|pregunta\s+\d+|continuando con|en este caso|en base a la pregunta",
    re.IGNORECASE,
)
PATRON_PREGUNTA_CORTA = re.compile(r"^\W*cu[aá]l es (la|el) (conducta|manejo|diagn)", re.IGNORECASE)
PATRON_LETRA = re.compile(r"^\s*[a-eA-E]\s*[\)\.]\s+")


# ---------------- LECTURA DEL WORD ----------------
def _lineas_con_negrita(p):
    """Devuelve [(texto, proporcion_en_negrita)] por cada línea del párrafo
    (un párrafo puede traer saltos de línea internos)."""
    lineas = [[0, 0, ""]]  # [letras_total, letras_negrita, texto]
    for r in p.runs:
        partes = r.text.split("\n")
        for i, parte in enumerate(partes):
            if i > 0:
                lineas.append([0, 0, ""])
            lineas[-1][2] += parte
            n = sum(1 for ch in parte if ch.isalnum())
            lineas[-1][0] += n
            if r.bold:
                lineas[-1][1] += n
    salida = []
    for total, neg, texto in lineas:
        texto = texto.replace("\xa0", " ").strip()
        if texto:
            salida.append((texto, (neg / total) if total else 0.0))
    return salida


def _imagenes_del_parrafo(p, doc):
    """[(bytes, content_type, extension)] de las imágenes de un párrafo."""
    salida = []
    for blip in p._p.iter(qn("a:blip")):
        rid = blip.get(qn("r:embed"))
        if not rid:
            continue
        parte = doc.part.related_parts[rid]
        ct = parte.content_type
        ext = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif"}.get(ct, "png")
        salida.append((parte.blob, ct, ext))
    return salida


def _numeracion(p):
    pPr = p._p.pPr
    if pPr is None or pPr.numPr is None:
        return None
    numpr = pPr.numPr
    nid = numpr.numId.val if numpr.numId is not None else None
    il = numpr.ilvl.val if numpr.ilvl is not None else 0
    return (nid, il)


def _elementos(doc):
    """Aplana el documento en una lista de elementos en orden:
    {"tipo": "texto"|"imagen", ...}. Cada línea de texto trae si es
    parte del enunciado ("enun") o una alternativa ("opc")."""
    # La numeración de las preguntas es la lista principal de nivel 0:
    # se detecta como la numeración nivel 0 más usada entre los párrafos
    # que NO vienen en grupos de 5 -en la práctica, la de numId más repetido
    # con ilvl 0 y párrafos largos-. Aquí se fija por frecuencia de uso.
    conteo = {}
    for p in doc.paragraphs:
        num = _numeracion(p)
        if num and num[1] == 0 and p.text.strip():
            conteo[num[0]] = conteo.get(num[0], 0) + 1
    num_enunciados = max(conteo, key=conteo.get) if conteo else None

    elems = []
    for p in doc.paragraphs:
        for img in _imagenes_del_parrafo(p, doc):
            elems.append({"tipo": "imagen", "img": img})
        lineas = _lineas_con_negrita(p)
        if not lineas:
            continue
        num = _numeracion(p)
        for k, (texto, neg) in enumerate(lineas):
            es_letra = bool(PATRON_LETRA.match(texto))
            if es_letra:
                rol = "opc"
            elif num == (num_enunciados, 0) or num is None:
                rol = "enun"
            else:
                rol = "opc"
            elems.append({"tipo": "texto", "texto": texto, "neg": neg, "rol": rol})
    return elems


def _limpiar_alternativa(texto):
    return PATRON_LETRA.sub("", texto).strip()


def _region(texto):
    bajo = texto.lower()
    for region, claves in REGLAS_REGION:
        for c in claves:
            if re.search(r"\b" + re.escape(c), bajo):
                return region
    return "ciencias_basicas"


def huella_archivo(contenido: bytes) -> str:
    """SHA-256 del archivo: sirve para avisar si ya fue cargado."""
    return hashlib.sha256(contenido).hexdigest()


def extraer_preguntas(contenido: bytes):
    """Lee el .docx (bytes) y devuelve (preguntas, anomalias).
    Cada pregunta: {numero, pregunta, opciones[5], correcta (0-4 o None),
    imagenes[(bytes, content_type, ext)], revisar, nota_revision, region}."""
    doc = docx.Document(io.BytesIO(contenido))
    elems = _elementos(doc)

    # El título del examen y todo lo anterior al primer enunciado se ignora.
    inicio = next((i for i, e in enumerate(elems) if e["tipo"] == "texto" and e["rol"] == "enun" and e["neg"] < 0.5), 0)
    elems = elems[inicio:]

    preguntas, anomalias = [], []
    enun, imgs, opc = [], [], []

    def cerrar():
        nonlocal enun, imgs, opc
        if not opc:
            return
        n = len(preguntas) + len(anomalias) + 1
        if len(opc) != 5 or not enun:
            anomalias.append({
                "numero": n,
                "motivo": f"{len(opc)} alternativas detectadas (se esperaban 5)" if len(opc) != 5 else "sin enunciado",
                "texto": " / ".join(enun)[:120],
            })
        else:
            negritas = [i for i, (_, neg) in enumerate(opc) if neg >= 0.5]
            texto_enun = " ".join(enun).strip()
            item = {
                "numero": n,
                "pregunta": texto_enun,
                "opciones": [_limpiar_alternativa(t) for t, _ in opc],
                "correcta": negritas[0] if len(negritas) == 1 else None,
                "imagenes": list(imgs),
                "revisar": False,
                "nota_revision": None,
                "region": _region(texto_enun),
            }
            if len(negritas) != 1:
                item["revisar"] = True
                item["nota_revision"] = (
                    "No se detectó una única alternativa en negrita: marca la correcta."
                    if not negritas else "Había más de una alternativa en negrita: confirma la correcta."
                )
            preguntas.append(item)
        enun, imgs, opc = [], [], []

    # Recorre los elementos: el enunciado es lo que viene antes de las 5
    # alternativas. Al aparecer un nuevo enunciado tras alternativas, cierra.
    for e in elems:
        if e["tipo"] == "imagen":
            if opc:  # imagen después de alternativas = pertenece a la siguiente
                cerrar()
            imgs.append(e["img"])
        elif e["rol"] == "enun":
            if opc:
                cerrar()
            enun.append(e["texto"])
        else:
            opc.append((e["texto"], e["neg"]))
    cerrar()

    # Preguntas que dependen del contexto de otra.
    for i, q in enumerate(preguntas):
        previa = preguntas[i - 1] if i > 0 else None
        por_texto = bool(PATRON_DEPENDIENTE.search(q["pregunta"]))
        por_forma = bool(
            previa and previa["imagenes"] and len(q["pregunta"]) < 70
            and PATRON_PREGUNTA_CORTA.match(q["pregunta"])
        )
        if not (por_texto or por_forma):
            continue
        q["revisar"] = True
        nota = "Se refiere al caso/pregunta anterior; al mezclar el orden pierde sentido."
        if previa and previa["imagenes"] and not q["imagenes"]:
            q["imagenes"] = list(previa["imagenes"])
            nota += " Se copió la imagen de la pregunta anterior; agrega el contexto al enunciado si hace falta."
        q["nota_revision"] = (q["nota_revision"] + " " if q["nota_revision"] else "") + nota
    huella = huella_archivo(contenido)
    for numero, region in AJUSTES_REGION.get(huella[:12], {}).items():
        for q in preguntas:
            if q["numero"] == numero:
                q["region"] = region
    for q in preguntas:
        assert q["region"] in REGIONES_VALIDAS, q["region"]
    return preguntas, anomalias
  
