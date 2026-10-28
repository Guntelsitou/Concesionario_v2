"""Bot de Telegram para dar de alta coches en la web del concesionario.

Flujo: /nuevo -> fotos -> preguntas con botones -> vista previa -> Publicar.
Escribe web/vehiculos.json y web/fotos/*.jpg; la web lee ese JSON.
"""
import asyncio, io, json, logging, os, re, subprocess
from pathlib import Path
from dotenv import load_dotenv
from PIL import Image, ImageOps
from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup as M, Update
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes,
                          ConversationHandler, MessageHandler, filters)

load_dotenv()
TOKEN = os.environ["TELEGRAM_TOKEN"]
ALLOWED = {int(x) for x in os.getenv("ALLOWED_IDS", "").replace(" ", "").split(",") if x}
WEB = Path(os.getenv("WEB_DIR", "../web")).resolve()
WEB_URL = os.getenv("WEB_URL", "").rstrip("/")
GIT_PUSH = os.getenv("GIT_PUSH", "false").lower() == "true"
JSON_FILE, FOTOS = WEB / "vehiculos.json", WEB / "fotos"
FOTOS.mkdir(parents=True, exist_ok=True)
log = logging.getLogger("bot")

FOTOS_ST, MARCA, MODELO, VERSION, ANIO, KM, PRECIO, COMB, CAMBIO, CV, ETIQ, COLOR, CARROC, EQUIP, OBS, CONFIRM = range(16)

COMBUSTIBLES = ["Gasolina", "Diésel", "Híbrido", "Eléctrico", "GLP"]
CARROCERIAS = {"Utilitario": "hatch", "Compacto": "hatch", "Berlina": "sedan", "Familiar": "sedan",
               "SUV": "suv", "Monovolumen": "suv", "Furgoneta": "suv", "Descapotable": "sedan"}
ETIQUETAS = ["0", "ECO", "C", "B", "Sin etiqueta"]

# ---------- datos ----------
def cargar():
    return json.loads(JSON_FILE.read_text("utf-8")) if JSON_FILE.exists() else []

def guardar(lista):
    tmp = JSON_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(lista, ensure_ascii=False, indent=1), "utf-8")
    tmp.replace(JSON_FILE)  # escritura atómica: la web nunca lee un JSON a medias

def siguiente_ref(lista):
    return f"{max([int(c['ref']) for c in lista if str(c.get('ref', '')).isdigit()] + [0]) + 1:04d}"

def a_numero(txt):
    s = re.sub(r"[^\d]", "", txt.replace(".", "").replace(",", ""))
    return int(s) if s else None

def publicar_git(msg):
    if not GIT_PUSH:
        return
    for cmd in (["git", "add", "-A"], ["git", "commit", "-m", msg], ["git", "push"]):
        subprocess.run(cmd, cwd=WEB, check=False, capture_output=True)

def descripcion(c):
    base = (f"{c['marca']} {c['modelo']} {c['version']} de {c['anio']} con {c['km']:,} km".replace(",", ".") +
            f", {c['combustible'].lower()} y cambio {c['cambio'].lower()} ({c['cv']} CV). Color {c['color'].lower()}. "
            f"Etiqueta DGT {c['etiqueta']}. " + (c.get("obs") or "") ).strip()
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return base
    try:
        import anthropic
        r = anthropic.Anthropic(api_key=key).messages.create(
            model=os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5"), max_tokens=300,
            messages=[{"role": "user", "content":
                "Redacta en español de España la descripción de un anuncio de coche de ocasión para la web de un "
                "concesionario. 2-3 frases, tono cercano y honesto, sin inventar datos que no estén aquí, sin emojis, "
                f"sin precio. Datos: {json.dumps(c, ensure_ascii=False)}"}])
        return r.content[0].text.strip()
    except Exception as e:  # sin IA seguimos con la plantilla
        log.warning("IA no disponible: %s", e)
        return base

# ---------- seguridad ----------
def autorizado(fn):
    async def w(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        if ALLOWED and uid not in ALLOWED:
            await update.effective_message.reply_text(f"No tienes permiso. Pide que añadan tu ID: {uid}")
            return ConversationHandler.END
        return await fn(update, ctx)
    return w

def teclado(opciones, cols=3):
    f = [B(o, callback_data=o) for o in opciones]
    return M([f[i:i + cols] for i in range(0, len(f), cols)])

# ---------- conversación ----------
@autorizado
async def nuevo(u, ctx):
    ctx.user_data["c"] = {"fotos_tmp": []}
    await u.message.reply_text("Vamos a subir un coche.\n\n📸 Envía las fotos (hasta 20). La primera será la portada: "
                               "mejor la de 3/4 delantero. Cuando termines pulsa /listo.\n/cancelar para salir.")
    return FOTOS_ST

async def foto(u, ctx):
    f = u.message.photo[-1] if u.message.photo else None
    if not f:
        return FOTOS_ST
    lista = ctx.user_data["c"]["fotos_tmp"]
    if len(lista) >= 20:
        await u.message.reply_text("Máximo 20 fotos. Pulsa /listo.")
        return FOTOS_ST
    data = await (await f.get_file()).download_as_bytearray()
    lista.append(bytes(data))
    if len(lista) in (1, 5, 10, 15, 20):
        await u.message.reply_text(f"✅ {len(lista)} foto(s) recibida(s). Sigue enviando o pulsa /listo.")
    return FOTOS_ST

async def listo(u, ctx):
    if not ctx.user_data["c"]["fotos_tmp"]:
        await u.message.reply_text("Necesito al menos una foto.")
        return FOTOS_ST
    await u.message.reply_text("Marca del coche (ej. Volkswagen):")
    return MARCA

def texto(campo, siguiente, pregunta, teclado_fn=None, convertir=None):
    async def h(u, ctx):
        v = u.message.text.strip()
        if convertir:
            v = convertir(v)
            if v is None:
                await u.message.reply_text("No lo entiendo, escríbelo solo con números.")
                return ctx.user_data["estado"]
        ctx.user_data["c"][campo] = v
        ctx.user_data["estado"] = siguiente
        await u.message.reply_text(pregunta, reply_markup=teclado_fn() if teclado_fn else None)
        return siguiente
    return h

def boton(campo, siguiente, pregunta, teclado_fn=None):
    async def h(u, ctx):
        q = u.callback_query
        await q.answer()
        ctx.user_data["c"][campo] = q.data
        await q.edit_message_text(f"{campo.capitalize()}: {q.data}")
        ctx.user_data["estado"] = siguiente
        await q.message.reply_text(pregunta, reply_markup=teclado_fn() if teclado_fn else None)
        return siguiente
    return h

async def equip(u, ctx):
    t = u.message.text.strip()
    ctx.user_data["c"]["equipamiento"] = [] if t == "-" else [x.strip() for x in re.split(r"[,\n;]", t) if x.strip()]
    await u.message.reply_text("¿Alguna observación? (ej. un propietario, libro de revisiones). Escribe - si no hay.")
    return OBS

async def obs(u, ctx):
    c = ctx.user_data["c"]
    t = u.message.text.strip()
    c["obs"] = "" if t == "-" else t
    c["descripcion"] = descripcion({k: v for k, v in c.items() if k != "fotos_tmp"})
    ctx.user_data["estado"] = CONFIRM
    n = lambda x: f"{x:,}".replace(",", ".")
    await u.message.reply_text(
        f"📋 Resumen\n{c['marca']} {c['modelo']} {c['version']}\n{c['anio']} · {n(c['km'])} km · {c['combustible']} · {c['cambio']} · {c['cv']} CV\n"
        f"Etiqueta {c['etiqueta']} · {c['color']} · {c['carroceria']}\n💶 {n(c['precio'])} €\n"
        f"📸 {len(c['fotos_tmp'])} fotos\n\n{c['descripcion']}",
        reply_markup=M([[B("✅ Publicar", callback_data="pub"), B("❌ Cancelar", callback_data="can")]]))
    return CONFIRM

async def confirmar(u, ctx):
    q = u.callback_query
    await q.answer()
    if q.data != "pub":
        ctx.user_data.clear()
        await q.edit_message_text("Cancelado. No se ha publicado nada.")
        return ConversationHandler.END
    c = ctx.user_data["c"]
    lista = cargar()
    ref = siguiente_ref(lista)
    nombres = []
    for i, raw in enumerate(c.pop("fotos_tmp"), 1):
        im = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")
        im.thumbnail((1600, 1600))
        n = f"{ref}-{i}.jpg"
        await asyncio.to_thread(im.save, FOTOS / n, "JPEG", quality=82, optimize=True)
        nombres.append(f"fotos/{n}")
    c.pop("estado", None)
    c.update(ref=ref, fotos=nombres, tipo=CARROCERIAS.get(c["carroceria"], "hatch"), nuevo=True)
    c.pop("obs", None)
    lista.insert(0, c)
    guardar(lista)
    await asyncio.to_thread(publicar_git, f"Alta coche {ref}: {c['marca']} {c['modelo']}")
    ctx.user_data.clear()
    enlace = f"{WEB_URL}/#coche-{ref}" if WEB_URL else f"(ref. {ref})"
    await q.edit_message_text(f"🎉 Publicado: {c['marca']} {c['modelo']}, ref. {ref}\n{enlace}\n"
                              f"Si la web usa despliegue automático, estará visible en 1-2 minutos.")
    return ConversationHandler.END

async def cancelar(u, ctx):
    ctx.user_data.clear()
    await u.message.reply_text("Cancelado. No se ha publicado nada.")
    return ConversationHandler.END

# ---------- gestión rápida ----------
async def estado(u, ctx, campo, valor, txt):
    if not ctx.args:
        await u.message.reply_text(f"Uso: /{txt} 0148")
        return
    lista = cargar()
    for c in lista:
        if c["ref"] == ctx.args[0].zfill(4):
            c.update({"vendido": False, "reservado": False}); c[campo] = valor
            guardar(lista); await asyncio.to_thread(publicar_git, f"{txt} {c['ref']}")
            await u.message.reply_text(f"✅ {c['marca']} {c['modelo']} ({c['ref']}) marcado como {txt}.")
            return
    await u.message.reply_text("No encuentro esa referencia. Mira /lista")

@autorizado
async def vendido(u, ctx): await estado(u, ctx, "vendido", True, "vendido")
@autorizado
async def reservado(u, ctx): await estado(u, ctx, "reservado", True, "reservado")
@autorizado
async def disponible(u, ctx): await estado(u, ctx, "reservado", False, "disponible")

@autorizado
async def precio(u, ctx):
    if len(ctx.args) != 2 or not a_numero(ctx.args[1]):
        await u.message.reply_text("Uso: /precio 0148 17900")
        return
    lista = cargar()
    for c in lista:
        if c["ref"] == ctx.args[0].zfill(4):
            c["precio"] = a_numero(ctx.args[1]); guardar(lista)
            await asyncio.to_thread(publicar_git, f"Precio {c['ref']}")
            await u.message.reply_text(f"✅ {c['marca']} {c['modelo']}: nuevo precio {c['precio']:,} €".replace(",", "."))
            return
    await u.message.reply_text("No encuentro esa referencia.")

@autorizado
async def lista_cmd(u, ctx):
    l = cargar()
    if not l:
        await u.message.reply_text("Aún no hay coches.")
        return
    est = lambda c: "🔴 vendido" if c.get("vendido") else "🟠 reservado" if c.get("reservado") else "🟢"
    await u.message.reply_text("\n".join(f"{c['ref']} · {c['marca']} {c['modelo']} · {c['precio']:,} € {est(c)}".replace(",", ".") for c in l[:40]))

async def mi_id(u, ctx):
    await u.message.reply_text(f"Tu ID de Telegram es {u.effective_user.id}")

async def ayuda(u, ctx):
    await u.message.reply_text("/nuevo – subir un coche\n/lista – ver coches\n/reservado 0148\n/vendido 0148\n"
                               "/disponible 0148\n/precio 0148 17900\n/id – tu ID")

def main():
    logging.basicConfig(level=logging.INFO)
    app = Application.builder().token(TOKEN).build()
    ftx = filters.TEXT & ~filters.COMMAND
    cv = ConversationHandler(
        entry_points=[CommandHandler("nuevo", nuevo)],
        states={
            FOTOS_ST: [MessageHandler(filters.PHOTO, foto), CommandHandler("listo", listo)],
            MARCA: [MessageHandler(ftx, texto("marca", MODELO, "Modelo (ej. Golf):"))],
            MODELO: [MessageHandler(ftx, texto("modelo", VERSION, "Versión / acabado (ej. 1.5 TSI Life 130 CV):"))],
            VERSION: [MessageHandler(ftx, texto("version", ANIO, "Año de matriculación (ej. 2020):"))],
            ANIO: [MessageHandler(ftx, texto("anio", KM, "Kilómetros (ej. 58400):", convertir=a_numero))],
            KM: [MessageHandler(ftx, texto("km", PRECIO, "Precio de venta en € (ej. 18900):", convertir=a_numero))],
            PRECIO: [MessageHandler(ftx, texto("precio", COMB, "Combustible:", lambda: teclado(COMBUSTIBLES), a_numero))],
            COMB: [CallbackQueryHandler(boton("combustible", CAMBIO, "Cambio:", lambda: teclado(["Manual", "Automático"], 2)))],
            CAMBIO: [CallbackQueryHandler(boton("cambio", CV, "Potencia en CV (ej. 130):"))],
            CV: [MessageHandler(ftx, texto("cv", ETIQ, "Etiqueta DGT:", lambda: teclado(ETIQUETAS), a_numero))],
            ETIQ: [CallbackQueryHandler(boton("etiqueta", COLOR, "Color (ej. Gris Indium):"))],
            COLOR: [MessageHandler(ftx, texto("color", CARROC, "Carrocería:", lambda: teclado(list(CARROCERIAS))))],
            CARROC: [CallbackQueryHandler(boton("carroceria", EQUIP, "Equipamiento separado por comas (ej. Navegador, Cámara trasera, Climatizador). Escribe - para saltar."))],
            EQUIP: [MessageHandler(ftx, equip)],
            OBS: [MessageHandler(ftx, obs)],
            CONFIRM: [CallbackQueryHandler(confirmar)],
        },
        fallbacks=[CommandHandler("cancelar", cancelar)], allow_reentry=True)
    app.add_handler(cv)
    for n, f in [("lista", lista_cmd), ("vendido", vendido), ("reservado", reservado), ("disponible", disponible),
                 ("precio", precio), ("id", mi_id), ("start", ayuda), ("ayuda", ayuda)]:
        app.add_handler(CommandHandler(n, f))
    app.run_polling()

if __name__ == "__main__":
    main()
