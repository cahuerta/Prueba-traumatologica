"""
scripts/importar_docx.py
Importación ÚNICA de un examen en Word (.docx) al banco de preguntas.

Qué hace:
  - Lee las preguntas (enunciado + 5 alternativas) del documento.
  - La respuesta correcta es la alternativa escrita en NEGRITA.
  - Las imágenes (radiografías) se suben al bucket privado "preguntas" y
    quedan asociadas a su pregunta (media_tipo = "foto").
  - Todas las preguntas entran con complejidad "basica"; la región se
    asigna por palabras clave (se corrige después al editar).
  - Las preguntas que dependen de otra ("la pregunta anterior", "en este
    caso") entran marcadas como "por revisar" con una nota.
  - Deja un registro en la tabla cargas_preguntas (archivo, quién, fecha,
    cuántas entraron y cuántas se rechazaron) y cada pregunta guarda su
    carga_id, para poder desactivar el lote completo.

Uso (desde la carpeta del backend):
  python scripts/importar_docx.py archivo.docx
      -> SOLO MUESTRA lo detectado, no guarda nada.
  python scripts/importar_docx.py archivo.docx --guardar --rut 12345678-9
      -> guarda. --rut es el RUT del interrogador que sube el archivo.
  Agregar --forzar para volver a cargar un archivo idéntico ya cargado.

Variables de entorno necesarias (solo para --guardar):
  SUPABASE_URL, SUPABASE_SERVICE_KEY
"""

import argparse
import hashlib
import os
import re
import sys
import time

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


def extraer_preguntas(ruta):
    doc = docx.Document(ruta)
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
    with open(ruta, "rb") as f:
        huella = hashlib.sha256(f.read()).hexdigest()
    for numero, region in AJUSTES_REGION.get(huella[:12], {}).items():
        for q in preguntas:
            if q["numero"] == numero:
                q["region"] = region
    for q in preguntas:
        assert q["region"] in REGIONES_VALIDAS, q["region"]
    return preguntas, anomalias


# ---------------- PRESENTACIÓN ----------------
def mostrar(preguntas, anomalias):
    letras = "ABCDE"
    print(f"\nPreguntas detectadas: {len(preguntas)}   Anomalías: {len(anomalias)}\n")
    for q in preguntas:
        corr = letras[q["correcta"]] if q["correcta"] is not None else "?"
        marca = " [POR REVISAR]" if q["revisar"] else ""
        img = f" [{len(q['imagenes'])} img]" if q["imagenes"] else ""
        print(f"#{q['numero']:>2} {q['region']:<16} correcta={corr}{img}{marca}")
        print(f"     {q['pregunta'][:110]}")
        if q["nota_revision"]:
            print(f"     > {q['nota_revision']}")
    for a in anomalias:
        print(f"ANOMALÍA #{a['numero']}: {a['motivo']} — {a['texto']}")
    print()


# ---------------- GUARDADO ----------------
def guardar(ruta, preguntas, anomalias, rut, forzar):
    from supabase import create_client

    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])

    interrogador = sb.table("interrogadores").select("id, nombre").eq("rut", rut).execute().data
    if not interrogador:
        sys.exit(f"No existe un interrogador con RUT {rut}.")
    interrogador = interrogador[0]

    with open(ruta, "rb") as f:
        huella = hashlib.sha256(f.read()).hexdigest()

    previa = sb.table("cargas_preguntas").select("id, created_at").eq("archivo_hash", huella).execute().data
    if previa and not forzar:
        sys.exit(f"Este archivo ya fue cargado el {previa[0].get('created_at')}. Usa --forzar para repetirlo.")

    # Una pregunta sin correcta detectada no puede guardarse (correcta es
    # obligatoria en la tabla): se registra como rechazada.
    guardables = [q for q in preguntas if q["correcta"] is not None]
    rechazadas = [
        {"numero": q["numero"], "motivo": "sin alternativa correcta detectada", "texto": q["pregunta"][:120]}
        for q in preguntas if q["correcta"] is None
    ] + anomalias

    carga = sb.table("cargas_preguntas").insert({
        "archivo": os.path.basename(ruta),
        "archivo_hash": huella,
        "subido_por": interrogador["id"],
        "total_detectadas": len(preguntas) + len(anomalias),
        "insertadas": 0,
        "rechazadas": len(rechazadas),
        "detalle_rechazadas": rechazadas,
    }).execute().data[0]
    carga_id = carga["id"]

    insertadas = 0
    for q in guardables:
        media_url = media_tipo = None
        if q["imagenes"]:
            data, ct, ext = q["imagenes"][0]
            media_url = f"{int(time.time())}-import-{carga_id[:8]}-p{q['numero']}.{ext}"
            sb.storage.from_("preguntas").upload(media_url, data, {"content-type": ct})
            media_tipo = "foto"
        sb.table("banco_preguntas").insert({
            "region": q["region"],
            "complejidad": "basica",
            "pregunta": q["pregunta"],
            "opciones": q["opciones"],
            "correcta": q["correcta"],
            "media_url": media_url,
            "media_tipo": media_tipo,
            "creado_por": interrogador["id"],
            "carga_id": carga_id,
            "numero_origen": q["numero"],
            "revisar": q["revisar"],
            "nota_revision": q["nota_revision"],
        }).execute()
        insertadas += 1

    sb.table("cargas_preguntas").update({"insertadas": insertadas}).eq("id", carga_id).execute()
    print(f"Listo. Carga {carga_id}: {insertadas} preguntas insertadas, {len(rechazadas)} rechazadas.")
    for r in rechazadas:
        print(f"  rechazada #{r['numero']}: {r['motivo']}")


def main():
    ap = argparse.ArgumentParser(description="Importa un examen en Word al banco de preguntas.")
    ap.add_argument("archivo")
    ap.add_argument("--guardar", action="store_true", help="guarda en la base (sin esto solo muestra)")
    ap.add_argument("--rut", help="RUT del interrogador que sube el archivo (obligatorio con --guardar)")
    ap.add_argument("--forzar", action="store_true", help="repite la carga de un archivo ya cargado")
    args = ap.parse_args()

    preguntas, anomalias = extraer_preguntas(args.archivo)
    mostrar(preguntas, anomalias)

    if args.guardar:
        if not args.rut:
            sys.exit("Falta --rut con el RUT del interrogador que sube el archivo.")
        guardar(args.archivo, preguntas, anomalias, args.rut, args.forzar)
    else:
        print("(Vista previa: no se guardó nada. Agrega --guardar --rut <RUT> para guardar.)")


if __name__ == "__main__":
    main()
