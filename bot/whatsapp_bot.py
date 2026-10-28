"""Asistente de WhatsApp para dar de alta coches en la web (y en Instagram) del concesionario.

Los trabajadores escriben al número del negocio (WhatsApp Business Cloud API):
  fotos  ->  preguntas detalladas  ->  resumen  ->  Publicar  ->  (opcional) Instagram.

Varios trabajadores pueden dar de alta coches a la vez: cada uno tiene su propia sesión.
Todo lo que se escribe en ficheros compartidos pasa por un cerrojo.

Arrancar:  uvicorn whatsapp_bot:app --host 0.0.0.0 --port 8000
"""
import asyncio, hashlib, hmac, io, json, logging, os, re, shutil, subprocess, time, uuid
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from PIL import Image, ImageOps

if __package__:  # funciona con `uvicorn bot.whatsapp_bot:app` desde la raíz del proyecto
    from . import instagram as ig
else:             # ejecución como módulo suelto (incluidos los tests existentes)
    import instagram as ig

load_dotenv()
log = logging.getLogger("wa")
logging.basicConfig(level=logging.INFO)


# ---------------------------------------------------------------- configuración
def _workers(raw: str) -> dict:
    """WORKERS="34611111111:Manolo,34622222222:Manuel"  (el nombre es opcional)."""
    d = {}
    for parte in (raw or "").replace(" ", "").split(","):
        tel, _, nombre = parte.partition(":")
        tel = tel.lstrip("+")
        if tel:
            d[tel] = nombre or tel
    return d


TOKEN = os.getenv("WA_TOKEN", "")
PHONE_ID = os.getenv("WA_PHONE_ID", "")
VERIFY = os.getenv("WA_VERIFY_TOKEN", "")
SECRET = os.getenv("WA_APP_SECRET", "")
ADMIN_API_TOKEN = os.getenv("ADMIN_API_TOKEN", "")
WORKERS = _workers(os.getenv("WORKERS", ""))
PROJECT_DIR = Path(__file__).resolve().parent.parent
WEB = Path(os.getenv("WEB_DIR", str(PROJECT_DIR / "web"))).expanduser().resolve()
WEB_URL = os.getenv("WEB_URL", "").rstrip("/")
GIT_PUSH = os.getenv("GIT_PUSH", "false").lower() == "true"
API = os.getenv("WA_API_BASE", "https://graph.facebook.com/v26.0").rstrip("/")
AUTO_SEGUNDOS = float(os.getenv("AUTO_SEGUNDOS", "10"))     # silencio tras las fotos para pasar a las preguntas
SESION_HORAS = float(os.getenv("SESION_HORAS", "12"))       # una alta abandonada caduca (la ventana de WhatsApp es de 24 h)
MAX_FOTOS = 20
JSON_FILE, FOTOS = WEB / "vehiculos.json", WEB / "fotos"
DATA = Path(os.getenv("DATA_DIR", str(Path(__file__).resolve().parent / "data"))).expanduser().resolve()
SESS_FILE, PRIV_FILE, TMP, BACKUPS = DATA / "sesiones.json", DATA / "privado.json", DATA / "tmp", DATA / "backups"
for _d in (TMP, BACKUPS, FOTOS):
    _d.mkdir(parents=True, exist_ok=True)
NEGOCIO = {"nombre": os.getenv("NEGOCIO_NOMBRE", ""), "tel": os.getenv("NEGOCIO_TEL", ""),
           "ciudad": os.getenv("NEGOCIO_CIUDAD", ""), "garantia": os.getenv("GARANTIA_TXT", "")}

# Estado en memoria (se guarda en disco tras cada cambio y se recupera al arrancar)
SESS: dict[str, dict] = {}
LOCKS: dict[str, asyncio.Lock] = {}     # un cerrojo por trabajador: sus mensajes se procesan de uno en uno
LOCK = asyncio.Lock()                   # vehiculos.json + privado.json: un alta/cambio a la vez, sin pisarse
GIT_LOCK = asyncio.Lock()
TAREAS: set = set()
EN_VUELO: dict[str, set] = {}           # fotos que se están descargando ahora mismo, por trabajador
VISTOS: list[str] = []
RECHAZOS: dict[str, float] = {}
IG_PASOS = ("ig_oferta", "ig_confirmar", "ig_texto")


# ---------------------------------------------------------------- preguntas
def _num(txt):
    s = re.sub(r"[^\d]", "", str(txt).replace(".", "").replace(",", ""))
    return int(s) if s else None


def _plate(txt):
    return re.sub(r"[^A-Z0-9]", "", str(txt).upper())


def v_texto(maximo=120):
    def f(t):
        t = re.sub(r"[\x00-\x1f\x7f]", " ", t).strip()
        if not t or t == "-":
            return None, "Escríbeme la respuesta por texto."
        return t[:maximo], None
    return f


def v_numero(lo, hi, nombre="valor"):
    def f(t):
        v = _num(t)
        if v is None or not (lo <= v <= hi):
            return None, f"No me cuadra ese {nombre}. Escríbelo solo con números (entre {lo} y {hi})."
        return v, None
    return f


def v_matricula(t):
    p = _plate(t)
    if not (4 <= len(p) <= 10):
        return None, "No parece una matrícula. Escríbela como 1234KLM."
    return p, None


def v_itv(t):
    if t.strip() == "-":
        return "", None
    m = re.fullmatch(r"\s*(\d{1,2})\s*[/\-.]\s*(\d{2}|\d{4})\s*", t)
    if m:
        mes, anio = int(m.group(1)), int(m.group(2))
        anio = anio + 2000 if anio < 100 else anio
        if 1 <= mes <= 12 and 2000 <= anio <= 2060:
            return f"{mes:02d}/{anio}", None
    return None, "Escribe mes y año, por ejemplo 03/2027. O - si no la sabes."


def v_consumo(t):
    if t.strip() == "-":
        return "", None
    try:
        v = float(t.strip().replace(",", "."))
    except ValueError:
        return None, "Escríbelo como 5,4 (litros cada 100 km). O - si no lo sabes."
    if not 0.5 <= v <= 40:
        return None, "Ese consumo no parece real. Escríbelo como 5,4. O - si no lo sabes."
    return round(v, 1), None


def v_equip(t):
    if t.strip() == "-":
        return [], None
    items = [re.sub(r"[\x00-\x1f\x7f]", " ", x).strip()[:60] for x in re.split(r"[,\n;]", t)]
    return [x for x in items if x][:25], None


def v_obs(t):
    if t.strip() == "-":
        return "", None
    return re.sub(r"[\x00-\x1f\x7f]", " ", t).strip()[:600], None


COMB = ["Gasolina", "Diésel", "Híbrido", "Eléctrico", "GLP"]
CARROC = {"Utilitario": "hatch", "Compacto": "hatch", "Berlina": "sedan", "Familiar": "sedan",
          "SUV": "suv", "Monovolumen": "suv", "Furgoneta": "suv", "Descapotable": "sedan"}
ETIQ = ["0", "ECO", "C", "B", "Sin etiqueta"]
ANIO_MAX = time.localtime().tm_year + 1
# kind: text | opt (<=3 botones) | list (<=10 filas)
STEPS = [
    dict(k="matricula", q="🔢 Matrícula (solo para uso interno, no se publica):", kind="text", val=v_matricula),
    dict(k="marca", q="Marca (ej. Volkswagen):", kind="text", val=v_texto(40)),
    dict(k="modelo", q="Modelo (ej. Golf):", kind="text", val=v_texto(40)),
    dict(k="version", q="Versión / acabado (ej. 1.5 TSI Life 130 CV):", kind="text", val=v_texto(80)),
    dict(k="anio", q="Año de matriculación (ej. 2020):", kind="text", val=v_numero(1990, ANIO_MAX, "año")),
    dict(k="km", q="Kilómetros (ej. 58400):", kind="text", val=v_numero(0, 999999, "kilometraje")),
    dict(k="precio", q="💶 Precio de venta en € (ej. 18900):", kind="text", val=v_numero(500, 500000, "precio")),
    dict(k="combustible", q="Combustible:", kind="list", opts=COMB),
    dict(k="cambio", q="Cambio:", kind="opt", opts=["Manual", "Automático"]),
    dict(k="cv", q="Potencia en CV (ej. 130):", kind="text", val=v_numero(20, 1500, "valor de potencia")),
    dict(k="etiqueta", q="Etiqueta medioambiental DGT:", kind="list", opts=ETIQ),
    dict(k="carroceria", q="Carrocería:", kind="list", opts=list(CARROC)),
    dict(k="puertas", q="Número de puertas:", kind="opt", opts=["3", "5"]),
    dict(k="plazas", q="Número de plazas (ej. 5):", kind="text", val=v_numero(1, 9, "número de plazas")),
    dict(k="color", q="Color (ej. Gris Indium):", kind="text", val=v_texto(40)),
    dict(k="propietarios", q="Propietarios anteriores:", kind="opt", opts=["1", "2", "3 o más"]),
    dict(k="itv", q="ITV vigente hasta (ej. 03/2027). Escribe - si no la sabes:", kind="text", val=v_itv),
    dict(k="consumo", q="Consumo mixto en l/100 km (ej. 5,4). Escribe - si no lo sabes:", kind="text", val=v_consumo),
    dict(k="equipamiento", q="🧰 Equipamiento, separado por comas (ej. Navegador, Cámara trasera, Climatizador). Escribe - para saltar:", kind="text", val=v_equip),
    dict(k="obs", q="📝 Observaciones: estado, libro de revisiones, reparaciones, extras… Escribe - si no hay:", kind="text", val=v_obs),
]
PRIVADOS = {"matricula"}

# Palabras escritas que equivalen a un botón
SINONIMOS = {"nuevo": "nuevo", "cancelar": "cancel", "listo": "listo", "/listo": "listo", "continuar": "listo",
             "atras": "atras", "atrás": "atras", "ayuda": "ayuda", "?": "ayuda", "menu": "menu", "menú": "menu",
             "hola": "menu", "lista": "lista", "empezar de nuevo": "nuevo_si"}
POR_PASO = {"confirmar": {"publicar": "pub", "si": "pub", "sí": "pub"},
            "dup": {"es otro coche": "dup_si", "otro": "dup_si", "si": "dup_si", "sí": "dup_si"},
            "ig_oferta": {"si": "ig_si", "sí": "ig_si", "instagram": "ig_si", "no": "ig_no"},
            "ig_confirmar": {"publicar": "ig_pub", "si": "ig_pub", "sí": "ig_pub", "no": "ig_no", "cambiar texto": "ig_texto"},
            "nuevo?": {"si": "nuevo_si", "sí": "nuevo_si", "no": "nuevo_no"}}


# ---------------------------------------------------------------- envío a WhatsApp
async def _post(payload):
    if not TOKEN:
        log.info("[sin token] %s", payload)
        return
    for intento in range(3):
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.post(f"{API}/{PHONE_ID}/messages", headers={"Authorization": f"Bearer {TOKEN}"},
                                 json={"messaging_product": "whatsapp", **payload})
            if r.status_code < 400:
                return
            log.error("WhatsApp %s: %s", r.status_code, r.text)
            if r.status_code < 500 and r.status_code != 429:
                return
        except httpx.HTTPError as e:
            log.error("WhatsApp red: %s", e)
        await asyncio.sleep(1.5 * (intento + 1))


async def send_text(to, body):
    await _post({"to": to, "type": "text", "text": {"body": body[:4000]}})


async def send_buttons(to, body, btns):
    """btns: [(id, título)], máx. 3 y 20 caracteres por título (límites de WhatsApp)."""
    await _post({"to": to, "type": "interactive", "interactive": {"type": "button", "body": {"text": body[:1000]},
        "action": {"buttons": [{"type": "reply", "reply": {"id": i, "title": t[:20]}} for i, t in btns[:3]]}}})


async def send_list(to, body, rows):
    """rows: [(id, título)], máx. 10 filas y 24 caracteres por título."""
    await _post({"to": to, "type": "interactive", "interactive": {"type": "list", "body": {"text": body[:1000]},
        "action": {"button": "Elegir", "sections": [{"title": "Opciones", "rows": [{"id": i, "title": t[:24]} for i, t in rows[:10]]}]}}})


async def download_media(media_id) -> bytes:
    async with httpx.AsyncClient(timeout=40) as c:
        h = {"Authorization": f"Bearer {TOKEN}"}
        metadata_response = await c.get(f"{API}/{media_id}", headers=h)
        metadata_response.raise_for_status()
        meta = metadata_response.json()
        media_url = meta.get("url") if isinstance(meta, dict) else None
        if not isinstance(media_url, str) or not media_url.startswith("https://"):
            raise ValueError("Meta no devolvió una URL segura para la foto")
        r = await c.get(media_url, headers=h)
        r.raise_for_status()
        return r.content


# ---------------------------------------------------------------- datos y ficheros
def _load(p, d):
    try:
        return json.loads(p.read_text("utf-8")) if p.exists() else d
    except json.JSONDecodeError:
        log.error("Fichero dañado: %s", p)
        raise


def _save(p, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), "utf-8")
    tmp.replace(p)   # escritura atómica: nadie lee un fichero a medias


def n(x):
    return f"{int(x):,}".replace(",", ".")


def siguiente_ref(lista, priv=None):
    refs = [int(c["ref"]) for c in lista if str(c.get("ref", "")).isdigit()]
    refs += [int(r) for r in (priv or {}) if str(r).isdigit()]   # una referencia borrada no se reutiliza
    return f"{max(refs + [0]) + 1:04d}"


def hacer_copia():
    if JSON_FILE.exists():
        shutil.copy2(JSON_FILE, BACKUPS / f"vehiculos-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}.json")
        for viejo in sorted(BACKUPS.glob("vehiculos-*.json"))[:-30]:
            viejo.unlink(missing_ok=True)


def _git_sync(msg):
    def run(*a):
        return subprocess.run(a, cwd=WEB, capture_output=True, text=True, timeout=120)
    try:
        # web/ puede ser su propio repositorio o una carpeta dentro de uno mayor (monorepo): vale cualquiera de los dos
        dentro = run("git", "rev-parse", "--is-inside-work-tree")
        if dentro.returncode != 0 or dentro.stdout.strip() != "true":
            log.error("No se puede desplegar: %s no está dentro de un repositorio git", WEB)
            return False
        added = run("git", "add", "-A", "--", ".")      # solo lo que cuelga de web/
        if added.returncode != 0:
            log.error("git add falló: %s", added.stderr)
            return False
        commit = run("git", "commit", "-m", msg)
        if commit.returncode != 0 and "nothing to commit" not in (commit.stdout + commit.stderr).lower():
            log.error("git commit falló: %s", commit.stderr)
            return False
        r = run("git", "push")
        if r.returncode != 0:
            pull = run("git", "pull", "--rebase")
            if pull.returncode == 0:
                r = run("git", "push")
            else:
                log.error("git pull --rebase falló: %s", pull.stderr)
        if r.returncode != 0:
            log.error("git push falló: %s", r.stderr)
        return r.returncode == 0
    except Exception:
        log.exception("git")
        return False


async def git_sync(msg) -> bool:
    if not GIT_PUSH:
        return True
    async with GIT_LOCK:
        return await asyncio.to_thread(_git_sync, msg)


def descripcion(c) -> str:
    base = (f"{c['marca']} {c['modelo']} {c['version']} de {c['anio']} con {n(c['km'])} km, {c['combustible'].lower()} "
            f"y cambio {c['cambio'].lower()} ({c['cv']} CV). Color {c['color'].lower()}. Etiqueta DGT {c['etiqueta']}.")
    if c.get("obs"):
        base += " " + c["obs"].rstrip(".") + "."
    if not os.getenv("ANTHROPIC_API_KEY"):
        return base
    try:
        import anthropic
        r = anthropic.Anthropic(timeout=20).messages.create(model=os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5"), max_tokens=300,
            messages=[{"role": "user", "content": "Redacta en español de España la descripción de un anuncio de coche de ocasión "
                "para un concesionario: 2-3 frases, tono cercano y honesto, sin inventar datos que no estén aquí, sin emojis, "
                "sin precio ni matrícula. Datos: " + json.dumps({k: v for k, v in c.items() if k not in PRIVADOS | {"fotos_tmp"}}, ensure_ascii=False)}])
        return r.content[0].text.strip() or base
    except Exception as e:
        log.warning("IA no disponible: %s", e)
        return base


# ---------------------------------------------------------------- sesiones
def persist():
    _save(SESS_FILE, SESS)   # síncrono y sin await entre cambiar y guardar: no se intercalan dos escrituras


def user_lock(to) -> asyncio.Lock:
    return LOCKS.setdefault(to, asyncio.Lock())


def nueva_sesion(to):
    SESS[to] = {"paso": "fotos", "c": {"fotos_tmp": []}, "v": 0, "t": time.time(), "foto_t": 0}
    return SESS[to]


def borrar_fotos_tmp(s):
    for f in (s or {}).get("c", {}).get("fotos_tmp", []):
        Path(f["p"]).unlink(missing_ok=True)


def cerrar_sesion(to):
    borrar_fotos_tmp(SESS.pop(to, None))
    persist()


def tiene_contenido(s):
    return bool(s and s["paso"] not in IG_PASOS and (s["c"]["fotos_tmp"] or any(k in s["c"] for k in ("matricula", "marca"))))


def cargar_sesiones():
    SESS.clear()
    SESS.update(_load(SESS_FILE, {}))
    for s in SESS.values():       # compatibilidad con sesiones de la versión anterior
        fotos = s.get("c", {}).get("fotos_tmp", [])
        s["c"]["fotos_tmp"] = [f if isinstance(f, dict) else {"p": f, "t": 0, "n": i} for i, f in enumerate(fotos)]
        s.setdefault("v", 0); s.setdefault("t", time.time()); s.setdefault("foto_t", 0)


def caducar_sesiones():
    limite = time.time() - SESION_HORAS * 3600
    for to in [u for u, s in SESS.items() if s.get("t", 0) < limite]:
        log.info("Sesión caducada de %s", to)
        borrar_fotos_tmp(SESS.pop(to))
    persist()
    for f in TMP.glob("*.jpg"):   # restos huérfanos
        if f.stat().st_mtime < limite:
            f.unlink(missing_ok=True)


# ---------------------------------------------------------------- preguntas y mensajes
async def ask(to, i):
    s = STEPS[i]
    head = f"({i + 1}/{len(STEPS)}) "
    if s["kind"] == "opt":
        await send_buttons(to, head + s["q"], [(f"o:{o}", o) for o in s["opts"]])
    elif s["kind"] == "list":
        await send_list(to, head + s["q"], [(f"o:{o}", o) for o in s["opts"]])
    else:
        await send_text(to, head + s["q"])


AYUDA = ("Puedes mandarme directamente las fotos de un coche y yo te voy preguntando lo demás.\n\n"
         "Otros mensajes:\n• *lista*: ver coches\n• *reservado 0148* · *vendido 0148* · *disponible 0148*\n"
         "• *precio 0148 17900*\n• *instagram 0148*: publicar un coche en Instagram\n"
         "• Durante un alta: *atrás*, *cancelar*, *?* (repite la pregunta)")


def resumen_texto(c):
    eq = c.get("equipamiento") or []
    return (f"📋 *Resumen*\n{c['marca']} {c['modelo']} {c['version']}\n{c['anio']} · {n(c['km'])} km · {c['combustible']} · {c['cambio']} · {c['cv']} CV\n"
            f"Etiqueta {c['etiqueta']} · {c['color']} · {c['carroceria']} · {c['puertas']} puertas · {c['plazas']} plazas\n"
            f"Propietarios: {c['propietarios']} · ITV: {c.get('itv') or '—'} · Consumo: {c.get('consumo') or '—'}\n"
            f"💶 *{n(c['precio'])} €*\n📸 {len(c['fotos_tmp'])} fotos\n🧰 {', '.join(eq) if eq else 'sin equipamiento indicado'}\n\n_{c['descripcion']}_")


async def mostrar_resumen(to, c):
    await send_text(to, resumen_texto(c))
    await send_buttons(to, "¿Publicamos este coche en la web?", [("a:pub", "Publicar"), ("a:nuevo_si", "Empezar de nuevo"), ("a:cancel", "Cancelar")])


async def mostrar_preview_ig(to, s):
    await send_text(to, "📸 *Así quedaría en Instagram* (carrusel con las fotos del coche):\n\n" + s["ig"]["caption"])
    await send_buttons(to, "¿Lo publicamos?", [("a:ig_pub", "Publicar ahora"), ("a:ig_texto", "Cambiar texto"), ("a:ig_no", "No publicar")])


async def repetir(to, s):
    """Vuelve a mostrar lo que el bot está esperando (tras un error, un '?' o un reinicio)."""
    p = s["paso"]
    if p == "fotos":
        n_f = len(s["c"]["fotos_tmp"])
        if n_f:
            await send_buttons(to, f"Tengo {n_f} foto(s). Si ya has terminado pulsa Continuar; si no, sigue enviándolas.", [("a:listo", "Continuar")])
        else:
            await send_text(to, "Envíame las fotos del coche 📸 y luego te hago unas preguntas.")
    elif isinstance(p, int):
        await ask(to, p)
    elif p == "confirmar":
        await mostrar_resumen(to, s["c"])
    elif p == "dup":
        await send_buttons(to, s["dup_msg"], [("a:dup_si", "Es otro coche"), ("a:cancel", "Cancelar alta")])
    elif p == "nuevo?":
        await send_buttons(to, "Tienes un coche a medias. ¿Lo descarto y empiezo otro?", [("a:nuevo_si", "Sí, empezar de nuevo"), ("a:nuevo_no", "No, seguir con este")])
    elif p == "ig_oferta":
        await send_buttons(to, "¿Quieres subirlo también a Instagram para promocionarlo?", [("a:ig_si", "Subir a Instagram"), ("a:ig_no", "No, solo la web")])
    elif p == "ig_confirmar":
        await mostrar_preview_ig(to, s)
    elif p == "ig_texto":
        await send_text(to, "Escríbeme el texto completo del post (máximo 2.000 caracteres).")


async def menu(to):
    nombre = WORKERS.get(to, "")
    await send_buttons(to, f"Hola {nombre} 👋 Soy el asistente de altas del concesionario.\n\nPara subir un coche, mándame sus fotos o pulsa el botón.",
                       [("a:nuevo", "Subir coche"), ("a:lista", "Ver lista"), ("a:ayuda", "Ayuda")])


# ---------------------------------------------------------------- fotos
def _guardar_foto(raw: bytes, destino: Path):
    """Comprueba que es una imagen, la endereza y la reduce. Falla si no es una imagen válida."""
    destino.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(io.BytesIO(raw)) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        im.thumbnail((2000, 2000))
        im.save(destino, "JPEG", quality=88)


def es_foto(msg):
    """Una foto normal, o una imagen enviada 'como archivo' (WhatsApp lo permite para no perder calidad)."""
    t = msg["type"]
    return t == "image" or (t == "document" and str(msg.get("document", {}).get("mime_type", "")).startswith("image/"))


async def recibir_foto(to, msg):
    """Las fotos se descargan EN PARALELO y fuera del cerrojo; solo añadirlas a la sesión va dentro."""
    try:
        media = msg.get("image") or msg.get("document") or {}
        raw = await download_media(media["id"]) if TOKEN else b""
        destino = TMP / f"{to}-{uuid.uuid4().hex}.jpg"
        await asyncio.to_thread(_guardar_foto, raw, destino)
    except Exception:
        log.exception("foto no válida de %s", to)
        extra = " Si la mandas como archivo, prueba a enviarla como foto normal." if msg["type"] == "document" else ""
        return await send_text(to, "No he podido recibir una foto. Vuelve a enviarla, por favor." + extra)
    ts = int(msg.get("timestamp") or time.time())
    async with user_lock(to):
        s = SESS.get(to)
        if s and s["paso"] in IG_PASOS:       # ya había terminado ese coche: estas fotos son de otro
            cerrar_sesion(to); s = None
        nuevo = s is None
        if nuevo:
            s = nueva_sesion(to)
        c = s["c"]
        if len(c["fotos_tmp"]) >= MAX_FOTOS:
            destino.unlink(missing_ok=True)
            if not s.get("aviso_max"):
                s["aviso_max"] = True; persist()
                await send_text(to, f"Máximo {MAX_FOTOS} fotos por coche. Las demás no las añado.")
            return
        c["fotos_tmp"].append({"p": str(destino), "t": ts, "n": len(c["fotos_tmp"])})
        s["v"] += 1; s["t"] = time.time()
        reciente = time.time() - s["foto_t"] < 12
        s["foto_t"] = time.time()
        persist()
        if nuevo:
            await send_text(to, "📸 Voy recibiendo las fotos de este coche. Cuando dejes de enviar, te haré unas preguntas.")
        if s["paso"] == "fotos":
            programar_auto(to, s["v"])
        elif not reciente:
            await send_text(to, f"📸 Foto añadida a este coche (llevo {len(c['fotos_tmp'])}).")


def programar_auto(to, v):
    t = asyncio.get_running_loop().create_task(_auto(to, v))
    TAREAS.add(t)
    t.add_done_callback(TAREAS.discard)


async def _auto(to, v):
    """Si el trabajador deja de mandar fotos unos segundos, pasamos solos a las preguntas."""
    try:
        await asyncio.sleep(AUTO_SEGUNDOS)
        async with user_lock(to):
            s = SESS.get(to)
            if not s or s["paso"] != "fotos" or s["v"] != v or not s["c"]["fotos_tmp"]:
                return                     # llegó otra foto después, o ya no procede
            await continuar_a_preguntas(to, s)
    except Exception:
        log.exception("auto")


async def continuar_a_preguntas(to, s):
    s["paso"] = 0; s["t"] = time.time(); persist()
    await send_text(to, f"✅ {len(s['c']['fotos_tmp'])} foto(s) recibida(s). Ahora unas preguntas sobre el coche "
                        f"({len(STEPS)} en total). Si falta alguna foto, sigue enviándola: la añado igualmente.")
    await ask(to, 0)


# ---------------------------------------------------------------- duplicados
def buscar_duplicado(placa, to, sesiones=True):
    """sesiones=True: avisa también de compañeros que lo están dando de alta ahora (aviso temprano).
    Al publicar se usa sesiones=False: solo cuentan los coches ya publicados, para no bloquear al primero en llegar."""
    for u, s in (SESS.items() if sesiones else ()):
        if u != to and s["paso"] not in IG_PASOS and _plate(s["c"].get("matricula", "")) == placa:
            return f"{WORKERS.get(u, u)} está dando de alta ahora mismo un coche con esa matrícula."
    priv = _load(PRIV_FILE, {})
    coches = {c["ref"]: c for c in _load(JSON_FILE, [])}
    for ref, p in priv.items():
        c = coches.get(ref)
        if c and not c.get("vendido") and _plate(p.get("matricula", "")) == placa:
            return f"Ya hay publicado un coche con esa matrícula (ref. {ref}: {c['marca']} {c['modelo']})."
    return None


# ---------------------------------------------------------------- publicar
def _procesar_lote(rutas, ref):
    """Guarda las fotos finales (1600 px, JPEG) de un coche. Devuelve los nombres de las que son válidas."""
    nombres, k = [], 0
    for p in rutas:
        try:
            with Image.open(p) as im:
                im = ImageOps.exif_transpose(im).convert("RGB")
                im.thumbnail((1600, 1600))
                k += 1
                name = f"{ref}-{k}.jpg"
                im.save(FOTOS / name, "JPEG", quality=82, optimize=True)
                nombres.append(f"fotos/{name}")
        except Exception:
            log.warning("foto descartada: %s", p)
    return nombres


def entrada_web(c, ref, nombres):
    e = {k: v for k, v in c.items() if k not in PRIVADOS | {"fotos_tmp", "obs"}}
    e["propietarios"] = {"1": 1, "2": 2}.get(str(c["propietarios"]), 3)
    for k in ("puertas", "plazas"):
        e[k] = int(c[k])
    if c.get("consumo") in (None, "", "-"): e.pop("consumo", None)
    if not c.get("itv"): e.pop("itv", None)
    e["equipamiento"] = c.get("equipamiento") if isinstance(c.get("equipamiento"), list) else []
    e.update(ref=ref, fotos=nombres, tipo=CARROC.get(c["carroceria"], "hatch"), nuevo=True)
    return e


async def publicar(to, s):
    c = s["c"]
    fotos = sorted(c["fotos_tmp"], key=lambda f: (f["t"], f["n"]))   # el orden real de llegada
    creadas: list[Path] = []
    async with LOCK:
        lista, priv = _load(JSON_FILE, []), _load(PRIV_FILE, {})
        dup = buscar_duplicado(_plate(c["matricula"]), to, sesiones=False)
        if dup and not s.get("dup_ok"):
            cerrar_sesion(to)
            return await send_text(to, f"⛔ No lo he publicado: {dup}\nEscribe *nuevo* si de verdad es otro coche.")
        ref = siguiente_ref(lista, priv)
        escrito_priv = False
        try:
            nombres = await asyncio.to_thread(_procesar_lote, [f["p"] for f in fotos], ref)
            creadas = [FOTOS / x.split("/")[-1] for x in nombres]
            if not nombres:
                borrar_fotos_tmp(s); c["fotos_tmp"] = []; s["paso"] = "fotos"; persist()
                return await send_text(to, "Ninguna de las fotos era válida. Envíame las fotos otra vez, por favor 📸")
            entrada = entrada_web(c, ref, nombres)
            if ig.cfg_from_env().activo:
                try:
                    await asyncio.to_thread(ig.preparar_imagenes, WEB, entrada)   # si falla, se generan más tarde al publicar
                except Exception:
                    log.exception("imágenes de Instagram (se reintentará al publicar)")
            priv[ref] = {"matricula": c["matricula"], "alta_por": WORKERS.get(to, to), "tel": to,
                         "fecha": time.strftime("%Y-%m-%d %H:%M")}
            _save(PRIV_FILE, priv); escrito_priv = True
            lista.insert(0, entrada)
            hacer_copia()
            _save(JSON_FILE, lista)           # lo público se escribe el último: si algo falla antes, no queda un anuncio a medias
        except Exception:
            for f in creadas:
                f.unlink(missing_ok=True)
            for f in (WEB / "fotos" / "ig").glob(f"{ref}-*.jpg"):
                f.unlink(missing_ok=True)
            if escrito_priv:
                p = _load(PRIV_FILE, {}); p.pop(ref, None); _save(PRIV_FILE, p)
            raise
    borrar_fotos_tmp(s)
    ok = await git_sync(f"Alta coche {ref}: {c['marca']} {c['modelo']}")
    enlace = f"{WEB_URL}/#coche-{ref}" if WEB_URL else f"ref. {ref}"
    texto = f"🎉 *Publicado*: {c['marca']} {c['modelo']} (ref. {ref})\n{enlace}\n"
    texto += ("Estará visible en la web en 1-2 minutos." if GIT_PUSH else "Guardado en el servidor.") if ok else \
             "⚠️ El coche está guardado, pero no he podido subirlo a la web automáticamente. Avisa al administrador."
    await send_text(to, texto)
    if ig.cfg_from_env().activo and ok:
        s.clear()
        s.update({"paso": "ig_oferta", "c": {"fotos_tmp": []}, "v": 0, "t": time.time(), "foto_t": 0, "pub": entrada})
        persist()
        await repetir(to, s)
    else:
        cerrar_sesion(to)


# ---------------------------------------------------------------- instagram
def _caption(entrada):
    return ig.caption(entrada, NEGOCIO)


async def estado_ig(ref, nuevo=None):
    async with LOCK:
        priv = _load(PRIV_FILE, {})
        p = priv.setdefault(ref, {})
        if nuevo is not None:
            if nuevo == "limpiar":
                p.pop("instagram", None)
            else:
                p["instagram"] = nuevo
            _save(PRIV_FILE, priv)
        return p.get("instagram")


async def lanzar_instagram(to, ref, texto):
    """Reserva la publicación (para no duplicarla) y la hace en segundo plano."""
    cur = await estado_ig(ref)
    if cur and cur.get("estado") == "publicado":
        return await send_text(to, f"Ese coche ya está en Instagram: {cur.get('permalink') or '(sin enlace)'}")
    if cur and cur.get("estado") == "en_curso" and time.time() - cur.get("t", 0) < 900:
        return await send_text(to, "Ese coche ya se está publicando en Instagram. Te aviso cuando termine.")
    if cur and cur.get("estado") == "incierto":
        return await send_text(to, "La última vez no pude confirmar si Instagram lo publicó. Mira el perfil antes de repetirlo. "
                                   f"Si no está, escribe *instagram {ref} repetir*.")
    await estado_ig(ref, {"estado": "en_curso", "t": time.time(), "por": WORKERS.get(to, to)})
    await send_text(to, "⏳ Publicando en Instagram. Puede tardar 1-3 minutos; te aviso cuando esté. Mientras, puedes seguir con otro coche.")
    t = asyncio.get_running_loop().create_task(_ig_job(to, ref, texto))
    TAREAS.add(t)
    t.add_done_callback(TAREAS.discard)


async def _ig_job(to, ref, texto):
    cfg = ig.cfg_from_env()
    try:
        coche = next((c for c in _load(JSON_FILE, []) if c["ref"] == ref), None)
        if not coche:
            raise ig.IGError("No encuentro el coche en la web.")
        nuevas = await asyncio.to_thread(ig.preparar_imagenes, WEB, coche)
        if nuevas and not await git_sync(f"Imágenes de Instagram {ref}"):
            raise ig.IGError("No he podido subir las imágenes a la web.")
        urls = [f"{cfg.web_url}/{r}" for r in ig.rutas_ig(coche)]
        async with ig.nuevo_cliente() as client:
            await ig.esperar_urls(client, urls)
            res = await ig.publicar(client, cfg, urls, texto)
        await estado_ig(ref, {"estado": "publicado", "t": time.time(), "media_id": res["media_id"], "permalink": res["permalink"],
                              "por": WORKERS.get(to, to)})
        await send_text(to, f"✅ *Publicado en Instagram*: {coche['marca']} {coche['modelo']}\n{res['permalink'] or ''}".strip())
    except ig.IGError as e:
        log.error("Instagram %s: %s", ref, e)
        await estado_ig(ref, {"estado": "incierto", "t": time.time()} if e.incierto else "limpiar")
        await send_text(to, "⚠️ " + ig.mensaje_usuario(e, ref))
    except Exception:
        log.exception("Instagram %s", ref)
        await estado_ig(ref, "limpiar")
        await send_text(to, "⚠️ Algo ha fallado al publicar en Instagram. Prueba otra vez con *instagram " + ref + "*.")


async def cmd_instagram(to, partes):
    if not ig.cfg_from_env().activo:
        return await send_text(to, "Instagram no está configurado en este bot. Pide al administrador que lo active.")
    if len(partes) < 2:
        return await send_text(to, "Uso: instagram 0148")
    ref = partes[1].zfill(4)
    coche = next((c for c in _load(JSON_FILE, []) if c["ref"] == ref), None)
    if not coche:
        return await send_text(to, "No encuentro esa referencia. Escribe *lista*.")
    cur = await estado_ig(ref)
    if cur and cur.get("estado") == "publicado":
        return await send_text(to, f"Ese coche ya está en Instagram: {cur.get('permalink') or '(sin enlace)'}")
    if cur and cur.get("estado") == "en_curso" and time.time() - cur.get("t", 0) < 900:
        return await send_text(to, "Ese coche ya se está publicando en Instagram. Te aviso cuando termine.")
    if cur and cur.get("estado") == "incierto":
        if len(partes) >= 3 and partes[2] in ("repetir", "otra", "forzar"):
            await estado_ig(ref, "limpiar")          # la persona ha comprobado el perfil y confirma que no está
        else:
            return await send_text(to, "La última vez no pude confirmar si Instagram lo publicó. Mira el perfil de Instagram antes de repetirlo. "
                                       f"Si no está, escribe *instagram {ref} repetir*.")
    s = nueva_sesion(to)
    s.update({"paso": "ig_confirmar", "pub": coche, "ig": {"ref": ref, "caption": _caption(coche)}})
    persist()
    await mostrar_preview_ig(to, s)


# ---------------------------------------------------------------- comandos sueltos
async def estado_cmd(to, partes, campo, valor, nombre):
    if len(partes) < 2:
        return await send_text(to, f"Uso: {nombre} 0148")
    async with LOCK:
        lista = _load(JSON_FILE, [])
        c = next((x for x in lista if x["ref"] == partes[1].zfill(4)), None)
        if not c:
            return await send_text(to, "No encuentro esa referencia. Escribe *lista*.")
        hacer_copia()
        c.pop("vendido", None); c.pop("reservado", None)
        if valor:
            c[campo] = True
        _save(JSON_FILE, lista)
    ok = await git_sync(f"{nombre} {c['ref']}")
    await send_text(to, f"✅ {c['marca']} {c['modelo']} ({c['ref']}): {nombre}." + ("" if ok else "\n⚠️ No he podido subir el cambio a la web. Avisa al administrador."))


async def comando(to, t):
    p = t.lower().split()
    if not p:
        return await menu(to)
    if p[0] == "lista":
        lista, priv = _load(JSON_FILE, []), _load(PRIV_FILE, {})
        est = lambda c: ("🔴 vendido" if c.get("vendido") else "🟠 reservado" if c.get("reservado") else "🟢") + \
                        (" 📸" if priv.get(c["ref"], {}).get("instagram", {}).get("estado") == "publicado" else "")
        return await send_text(to, "\n".join(f"{c['ref']} · {c['marca']} {c['modelo']} · {n(c['precio'])} € {est(c)}" for c in lista[:40]) or "Aún no hay coches.")
    if p[0] in ("vendido", "reservado"):
        return await estado_cmd(to, p, p[0], True, p[0])
    if p[0] == "disponible":
        return await estado_cmd(to, p, "", False, "disponible")
    if p[0] == "instagram":
        return await cmd_instagram(to, p)
    if p[0] == "precio":
        if len(p) != 3 or not _num(p[2]) or not 500 <= _num(p[2]) <= 500000:
            return await send_text(to, "Uso: precio 0148 17900")
        async with LOCK:
            lista = _load(JSON_FILE, [])
            c = next((x for x in lista if x["ref"] == p[1].zfill(4)), None)
            if not c:
                return await send_text(to, "No encuentro esa referencia.")
            hacer_copia(); c["precio"] = _num(p[2]); _save(JSON_FILE, lista)
        ok = await git_sync(f"Precio {c['ref']}")
        return await send_text(to, f"✅ {c['marca']} {c['modelo']}: nuevo precio {n(c['precio'])} €" + ("" if ok else "\n⚠️ No he podido subir el cambio a la web."))
    await menu(to)


# ---------------------------------------------------------------- conversación
def interpretar(msg, paso):
    """Devuelve (texto, acción, opción). La acción sale de un botón o de una palabra equivalente."""
    t = msg["type"]
    if t == "text":
        texto = msg["text"]["body"].strip()
        low = texto.lower()
        return texto, POR_PASO.get(paso, {}).get(low) or SINONIMOS.get(low), None
    if t == "interactive":
        r = msg["interactive"].get("button_reply") or msg["interactive"].get("list_reply") or {}
        i = r.get("id", "")
        if i.startswith("a:"):
            return r.get("title", ""), i[2:], None
        if i.startswith("o:"):
            return i[2:], None, i[2:]
        return r.get("title", ""), None, None
    return "", None, None


async def procesar(msg):
    to = msg["from"]
    if to not in WORKERS:
        if time.time() - RECHAZOS.get(to, 0) > 600:    # no contestamos en bucle a números desconocidos
            RECHAZOS[to] = time.time()
            await send_text(to, "Este número es solo para el equipo del concesionario.")
        return
    if msg["type"] == "reaction":          # un emoji sobre un mensaje no es una orden: no contestamos
        return
    if es_foto(msg):
        # Se anota que hay una foto "en vuelo" para que un 'listo' escrito justo después espere a que termine de llegar.
        fut = asyncio.get_running_loop().create_future()
        EN_VUELO.setdefault(to, set()).add(fut)
        try:
            return await recibir_foto(to, msg)
        finally:
            fut.set_result(None)
            EN_VUELO[to].discard(fut)
            if not EN_VUELO[to]:
                EN_VUELO.pop(to, None)
    pendientes = list(EN_VUELO.get(to, ()))
    if pendientes:                         # fotos aún descargándose: el mensaje de texto espera su turno (máx. 45 s)
        await asyncio.wait(pendientes, timeout=45)
    async with user_lock(to):
        s = SESS.get(to)
        try:
            await _procesar(to, msg, s)
        except Exception:
            log.exception("error procesando mensaje de %s", to)
            s = SESS.get(to)
            try:
                if s:
                    await send_text(to, "Ups, algo ha fallado. No pierdo lo que llevabas. Sigamos:")
                    await repetir(to, s)
                else:
                    await send_text(to, "Ups, algo ha fallado. Prueba otra vez o escribe *ayuda*.")
            except Exception:
                log.exception("fallo al avisar del error")


async def _procesar(to, msg, s):
    paso = s["paso"] if s else None
    if msg["type"] not in ("text", "interactive"):
        return await send_text(to, "Solo entiendo texto, botones y fotos 📷")
    texto, accion, opcion = interpretar(msg, paso)
    if s:
        s["t"] = time.time()

    # --- acciones válidas en cualquier momento
    if accion == "ayuda":
        return await send_text(to, AYUDA) if not s else await repetir(to, s)
    if accion == "cancel":
        if not s:
            return await send_text(to, "No hay ningún alta en curso.")
        cerrar_sesion(to)
        return await send_text(to, "Cancelado. No se ha publicado nada." if paso not in IG_PASOS else "Vale, no se publica en Instagram.")
    if accion in ("nuevo", "nuevo_si"):
        if s and accion == "nuevo" and paso != "nuevo?" and tiene_contenido(s):
            s["paso_prev"] = s["paso"]; s["paso"] = "nuevo?"; persist()
            return await repetir(to, s)
        if s:
            cerrar_sesion(to)
        nueva_sesion(to); persist()
        return await send_text(to, "Vamos a subir un coche 🚗\n\n📸 Envíame las fotos desde la cámara de WhatsApp (hasta 20). "
                                   "La primera será la portada: mejor de 3/4 delantero. Cuando dejes de enviar, te haré unas preguntas.")
    if accion == "nuevo_no" and paso == "nuevo?":
        s["paso"] = s.pop("paso_prev", "fotos"); persist()
        return await repetir(to, s)
    if paso == "nuevo?":
        return await repetir(to, s)

    # --- sin alta en curso: menú y comandos
    if not s:
        if accion == "lista":
            return await comando(to, "lista")
        if accion == "menu":
            return await menu(to)
        return await comando(to, texto)
    if paso in ("ig_oferta", "ig_confirmar") and texto.lower().split()[:1] and \
            texto.lower().split()[0] in ("lista", "vendido", "reservado", "disponible", "precio", "instagram") and msg["type"] == "text":
        cerrar_sesion(to)                     # un comando suelto cierra la oferta de Instagram (se puede retomar con "instagram 0148")
        return await comando(to, texto)

    c = s["c"]
    # --- fotos
    if paso == "fotos":
        if accion == "listo":
            if not c["fotos_tmp"]:
                return await send_text(to, "Necesito al menos una foto 📸")
            return await continuar_a_preguntas(to, s)
        return await repetir(to, s)

    # --- preguntas
    if isinstance(paso, int):
        if accion in ("listo", "menu", "lista"):     # "listo" por costumbre: no es una respuesta
            return await repetir(to, s)
        if accion == "atras":
            if paso == 0:
                return await send_text(to, "Estás en la primera pregunta.")
            s["paso"] = paso - 1; persist()
            return await ask(to, paso - 1)
        st = STEPS[paso]
        if st["kind"] == "text":
            v, err = st["val"](texto)
            if err:
                return await send_text(to, err)
        else:
            v = opcion if opcion in st["opts"] else next((o for o in st["opts"] if o.lower() == texto.lower()), None)
            if v is None:
                return await ask(to, paso)       # respuesta rara: repetimos botones
        c[st["k"]] = v
        if st["k"] == "matricula":
            dup = buscar_duplicado(v, to)
            if dup:
                s["paso"] = "dup"; s["dup_msg"] = f"⚠️ {dup}\n¿Es otro coche distinto?"; persist()
                return await repetir(to, s)
        if paso + 1 < len(STEPS):
            s["paso"] = paso + 1; persist()
            return await ask(to, paso + 1)
        c["descripcion"] = await asyncio.to_thread(descripcion, c)
        s["paso"] = "confirmar"; persist()
        return await mostrar_resumen(to, c)

    if paso == "dup":
        if accion == "dup_si":
            s["dup_ok"] = True; s["paso"] = 1; persist()
            return await ask(to, 1)
        return await repetir(to, s)

    if paso == "confirmar":
        if accion == "pub":
            return await publicar(to, s)
        return await repetir(to, s)

    # --- Instagram
    if paso == "ig_oferta":
        if accion == "ig_si":
            s["ig"] = {"ref": s["pub"]["ref"], "caption": _caption(s["pub"])}
            s["paso"] = "ig_confirmar"; persist()
            return await mostrar_preview_ig(to, s)
        if accion == "ig_no":
            ref = s["pub"]["ref"]; cerrar_sesion(to)
            return await send_text(to, f"Perfecto, solo en la web. Si luego quieres subirlo: *instagram {ref}*")
        return await repetir(to, s)
    if paso == "ig_confirmar":
        if accion == "ig_pub":
            ref, texto_ig = s["ig"]["ref"], s["ig"]["caption"]; cerrar_sesion(to)
            return await lanzar_instagram(to, ref, texto_ig)
        if accion == "ig_texto":
            s["paso"] = "ig_texto"; persist()
            return await repetir(to, s)
        if accion == "ig_no":
            cerrar_sesion(to)
            return await send_text(to, "Vale, no se publica en Instagram.")
        return await repetir(to, s)
    if paso == "ig_texto":
        if not texto or len(texto) > 2000:
            return await send_text(to, "El texto debe tener entre 1 y 2.000 caracteres. Escríbelo otra vez.")
        s["ig"]["caption"] = texto; s["paso"] = "ig_confirmar"; persist()
        return await mostrar_preview_ig(to, s)
    log.error("paso desconocido %r", paso)
    cerrar_sesion(to)
    await send_text(to, "Me he perdido. Empezamos de nuevo: escribe *nuevo*.")


async def seguro(msg):
    try:
        await procesar(msg)
    except Exception:
        log.exception("error")


# ---------------------------------------------------------------- arranque y webhook
async def limpiador():
    while True:
        await asyncio.sleep(1800)
        try:
            caducar_sesiones()
        except Exception:
            log.exception("limpieza")


@asynccontextmanager
async def lifespan(app):
    if not WORKERS:
        log.error("WORKERS está vacío: nadie podrá usar el bot. Añade los móviles del equipo en .env")
    cargar_sesiones()
    caducar_sesiones()
    try:    # una publicación de Instagram que se quedó a medias por un reinicio: no sabemos si llegó a publicarse
        priv = _load(PRIV_FILE, {})
        a_medias = [p["instagram"] for p in priv.values() if p.get("instagram", {}).get("estado") == "en_curso"]
        for i in a_medias:
            i["estado"] = "incierto"
        if a_medias:
            _save(PRIV_FILE, priv)
            log.warning("%d publicación(es) de Instagram quedaron a medias; marcadas como inciertas", len(a_medias))
    except Exception:
        log.exception("revisión de Instagram al arrancar")
    for to, s in SESS.items():          # retoma las altas que estaban esperando el silencio tras las fotos
        if s["paso"] == "fotos" and s["c"]["fotos_tmp"]:
            programar_auto(to, s["v"])
    t = asyncio.create_task(limpiador())
    yield
    t.cancel()


app = FastAPI(lifespan=lifespan)


@app.exception_handler(Exception)
async def unhandled_error(request: Request, exc: Exception):
    """Registrar el detalle en servidor sin filtrarlo al cliente."""
    log.exception("Error no controlado atendiendo %s %s", request.method, request.url.path, exc_info=exc)
    return JSONResponse(status_code=500, content={"detail": "Error interno"})


@app.get("/webhook")
async def verificar(request: Request):
    q = request.query_params
    if VERIFY and q.get("hub.mode") == "subscribe" and q.get("hub.verify_token") == VERIFY:
        return Response(q.get("hub.challenge", ""), media_type="text/plain")
    raise HTTPException(403)


@app.post("/webhook")
async def recibir(request: Request, bg: BackgroundTasks):
    body = await request.body()
    if not SECRET:
        # No aceptar webhooks sin autenticar: de lo contrario cualquiera podría suplantar
        # a un trabajador y enviar mensajes facturables usando la cuenta del negocio.
        raise HTTPException(503, "WA_APP_SECRET no está configurado")
    firma = request.headers.get("X-Hub-Signature-256", "")
    esperada = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(firma, esperada):
        raise HTTPException(403)
    try:
        datos = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(400, "JSON inválido")
    if not isinstance(datos, dict) or not isinstance(datos.get("entry", []), list):
        raise HTTPException(400, "El webhook debe contener una lista entry")
    for e in datos.get("entry", []):
        if not isinstance(e, dict) or not isinstance(e.get("changes", []), list):
            continue
        for ch in e.get("changes", []):
            if not isinstance(ch, dict):
                continue
            value = ch.get("value", {})
            if not isinstance(value, dict) or not isinstance(value.get("messages", []), list):
                continue
            for m in value.get("messages", []):
                if not isinstance(m, dict) or not m.get("id") or not m.get("from") or not m.get("type"):
                    log.warning("Webhook de WhatsApp con un mensaje incompleto; se ignora")
                    continue
                if m["id"] in VISTOS:
                    continue                    # Meta reintenta si tardamos en contestar
                VISTOS.append(m["id"]); del VISTOS[:-1000]
                bg.add_task(seguro, m)
    return {"ok": True}


@app.get("/health")
async def health():
    """Readiness sin exponer valores sensibles."""
    checks = {
        "wa_token": bool(TOKEN),
        "wa_phone_id": bool(PHONE_ID),
        "wa_verify_token": bool(VERIFY),
        "wa_app_secret": bool(SECRET),
        "trabajadores": bool(WORKERS),
        "web_data": WEB.is_dir() and JSON_FILE.is_file(),
    }
    return {
        "estado": "ok" if all(checks.values()) else "configuracion_incompleta",
        "checks": checks,
        "altas_en_curso": len(SESS),
        "instagram": ig.cfg_from_env().activo,
        "git_push": GIT_PUSH,
        "admin_api": bool(ADMIN_API_TOKEN),
    }


def _admin_auth(request: Request):
    """Las escrituras del inventario requieren un token de administrador configurado."""
    token = request.headers.get("Authorization", "")
    supplied = token[7:] if token.startswith("Bearer ") else ""
    if not ADMIN_API_TOKEN:
        raise HTTPException(503, "ADMIN_API_TOKEN no está configurado")
    if not supplied or not hmac.compare_digest(supplied, ADMIN_API_TOKEN):
        raise HTTPException(401, "Token de administrador no válido")


@app.get("/api/vehiculos")
async def api_vehiculos():
    try:
        data = _load(JSON_FILE, [])
    except (OSError, json.JSONDecodeError):
        log.exception("No se pudo leer el inventario para la web")
        raise HTTPException(500, "No se pudo leer el inventario")
    if not isinstance(data, list):
        raise HTTPException(500, "El inventario debe ser una lista")
    return data


@app.patch("/api/vehiculos/{ref}")
async def api_actualizar_vehiculo(ref: str, request: Request):
    _admin_auth(request)
    if not re.fullmatch(r"\d{1,8}", ref):
        raise HTTPException(400, "Referencia no válida")
    try:
        cambios = await request.json()
    except Exception:
        raise HTTPException(400, "JSON no válido")
    if not isinstance(cambios, dict) or not cambios or set(cambios) - {"precio", "estado", "descripcion"}:
        raise HTTPException(400, "Solo se pueden cambiar precio, estado y descripción")
    if "precio" in cambios and (isinstance(cambios["precio"], bool) or not isinstance(cambios["precio"], int) or not 1 <= cambios["precio"] <= 10000000):
        raise HTTPException(400, "El precio debe ser un entero positivo")
    if "estado" in cambios and (not isinstance(cambios["estado"], str) or cambios["estado"] not in {"disponible", "reservado", "vendido"}):
        raise HTTPException(400, "Estado no válido")
    if "descripcion" in cambios and (not isinstance(cambios["descripcion"], str) or len(cambios["descripcion"]) > 3000):
        raise HTTPException(400, "La descripción debe tener hasta 3000 caracteres")

    async with LOCK:
        lista = _load(JSON_FILE, [])
        coche = next((c for c in lista if str(c.get("ref")) == ref), None)
        if coche is None:
            raise HTTPException(404, "Vehículo no encontrado")
        hacer_copia()
        if "precio" in cambios:
            coche["precio"] = cambios["precio"]
        if "estado" in cambios:
            coche.pop("reservado", None)
            coche.pop("vendido", None)
            if cambios["estado"] == "reservado":
                coche["reservado"] = True
            elif cambios["estado"] == "vendido":
                coche["vendido"] = True
        if "descripcion" in cambios:
            coche["descripcion"] = cambios["descripcion"].strip()
        _save(JSON_FILE, lista)
    desplegado = await git_sync(f"Edición panel {ref}")
    return {"vehiculo": coche, "desplegado": desplegado}


@app.get("/")
async def web_inicio():
    from fastapi.responses import FileResponse
    index = WEB / "index.html"
    if not index.is_file():
        raise HTTPException(404, "No se encontró la web")
    return FileResponse(index)


@app.get("/{asset_path:path}")
async def web_assets(asset_path: str):
    from fastapi.responses import FileResponse
    # Limitar los ficheros publicados a la carpeta web para impedir traversal.
    candidate = (WEB / asset_path).resolve()
    if not candidate.is_relative_to(WEB) or not candidate.is_file():
        raise HTTPException(404, "No encontrado")
    return FileResponse(candidate)
