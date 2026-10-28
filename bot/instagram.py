"""Publicación en Instagram con la API oficial de Meta (Instagram Graph API).

Requisitos (se explican en el README):
  - Cuenta de Instagram profesional (empresa o creador), conectada a una página de Facebook.
  - App de Meta con permisos de publicación de contenido (instagram_content_publish
    o instagram_business_content_publish) y un token de acceso válido.
  - Las fotos deben estar en una URL pública en JPEG en el momento de publicar.

Flujo de la API:  crear contenedor(es) -> esperar a que estén listos -> media_publish.
Un carrusel admite de 2 a 10 imágenes; con una sola foto se publica una imagen simple.
Límite de Meta: 100 publicaciones por API cada 24 h (un carrusel cuenta como una).
"""
import asyncio
import logging
import os
import re
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import httpx
from PIL import Image, ImageFilter, ImageOps

log = logging.getLogger("ig")

MAX_FOTOS = 10          # máximo de Instagram en un carrusel
LADO = 1080             # imágenes cuadradas 1:1 (siempre dentro de los formatos admitidos)
MAX_CAPTION = 2200      # límite de Instagram


class IGError(Exception):
    """Fallo al publicar. `incierto=True`: no sabemos si Instagram llegó a publicar (no reintentar solo)."""

    def __init__(self, mensaje, codigo=None, incierto=False):
        super().__init__(mensaje)
        self.codigo = codigo
        self.incierto = incierto


@dataclass
class Config:
    token: str
    user_id: str
    base: str
    web_url: str

    @property
    def activo(self) -> bool:
        return bool(self.token and self.user_id and self.web_url)


def cfg_from_env() -> Config:
    return Config(
        token=os.getenv("IG_TOKEN", ""),
        user_id=os.getenv("IG_USER_ID", ""),
        base=os.getenv("IG_API_BASE", "https://graph.facebook.com/v26.0").rstrip("/"),
        web_url=os.getenv("WEB_URL", "").rstrip("/"),
    )


def nuevo_cliente() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=30, follow_redirects=True)


# ---------------------------------------------------------------- texto
def _slug(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _n(x) -> str:
    return f"{int(x):,}".replace(",", ".")


def caption(c: dict, negocio: dict) -> str:
    """Texto del post a partir de un coche (formato de vehiculos.json)."""
    cabecera = f"🚗 {c['marca']} {c['modelo']} {c.get('version', '')}".strip()
    datos = (f"📅 {c['anio']}  ·  🛣️ {_n(c['km'])} km  ·  ⛽ {c['combustible']}  ·  "
             f"⚙️ {c['cambio']}  ·  {c['cv']} CV")
    precio = f"💶 {_n(c['precio'])} € (IVA incluido)"
    garantia = f"✅ {negocio.get('garantia') or 'Garantía de 12 meses'} · revisado en nuestro taller"
    equip = [e for e in (c.get("equipamiento") or []) if e][:4]
    bloque_equip = ("\n".join(f"• {e}" for e in equip)) if equip else ""
    contacto = []
    if negocio.get("tel"):
        contacto.append(f"📲 Información y cita: WhatsApp {negocio['tel']}")
    if negocio.get("ciudad"):
        contacto.append(f"📍 {negocio['ciudad']}")
    contacto.append(f"🔗 Ficha completa y más fotos: enlace de la bio (ref. {c['ref']})")
    tags = ["cochesdeocasion", "segundamano", "ocasion", "coches", _slug(c["marca"]),
            _slug(c["marca"] + c["modelo"]), _slug(c["combustible"]), _slug(c.get("carroceria", "")),
            _slug(negocio.get("ciudad", "")), _slug(negocio.get("nombre", ""))]
    vistos, hashtags = set(), []
    for t in tags:
        if t and t not in vistos:
            vistos.add(t)
            hashtags.append("#" + t)
    cola = "\n".join(contacto) + "\n\n" + " ".join(hashtags[:12])

    desc = (c.get("descripcion") or "").strip()

    def armar(d):
        partes = [cabecera, "", datos, precio, garantia]
        if bloque_equip:
            partes += ["", bloque_equip]
        if d:
            partes += ["", d]
        partes += ["", cola]
        return "\n".join(partes)

    texto = armar(desc)
    while len(texto) > MAX_CAPTION and len(desc) > 40:   # recorta la descripción, nunca los datos
        desc = desc[: max(40, len(desc) - 150)].rstrip(" ,.;") + "…"
        texto = armar(desc)
    return texto[:MAX_CAPTION]


# ---------------------------------------------------------------- imágenes
def rutas_ig(c: dict) -> list[str]:
    """Rutas relativas (dentro de web/) de las imágenes de Instagram de un coche."""
    n = min(len(c.get("fotos") or []), MAX_FOTOS)
    return [f"fotos/ig/{c['ref']}-{i}.jpg" for i in range(1, n + 1)]


def _cuadrada(origen: Path, destino: Path):
    """Foto en 1:1 sin recortar el coche: foto completa centrada sobre su propio fondo desenfocado."""
    with Image.open(origen) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        fondo = ImageOps.fit(im, (LADO, LADO), method=Image.LANCZOS).filter(ImageFilter.GaussianBlur(38))
        fondo = Image.blend(fondo, Image.new("RGB", fondo.size, (10, 14, 18)), 0.35)
        frente = im.copy()
        frente.thumbnail((LADO, LADO), Image.LANCZOS)
        fondo.paste(frente, ((LADO - frente.width) // 2, (LADO - frente.height) // 2))
    destino.parent.mkdir(parents=True, exist_ok=True)
    tmp = destino.with_suffix(".tmp.jpg")
    fondo.save(tmp, "JPEG", quality=88, optimize=True)
    tmp.replace(destino)


def preparar_imagenes(web_dir: Path, c: dict) -> list[str]:
    """Crea las imágenes de Instagram que falten. Devuelve las rutas NUEVAS (para subirlas con git)."""
    nuevas = []
    for src_rel, dst_rel in zip((c.get("fotos") or [])[:MAX_FOTOS], rutas_ig(c)):
        dst = web_dir / dst_rel
        if dst.exists():
            continue
        src = web_dir / src_rel
        if not src.exists():
            raise IGError(f"No encuentro la foto {src_rel} en el servidor.")
        _cuadrada(src, dst)
        nuevas.append(dst_rel)
    if not rutas_ig(c):
        raise IGError("Este coche no tiene fotos.")
    return nuevas


# ---------------------------------------------------------------- API
def _error(r: httpx.Response) -> IGError:
    try:
        e = r.json().get("error", {})
    except Exception:
        e = {}
    msg = e.get("error_user_msg") or e.get("message") or f"HTTP {r.status_code}"
    return IGError(msg, codigo=e.get("code"))


async def _llamar(client, cfg, metodo, ruta, incierto_si_red=False, **params):
    params["access_token"] = cfg.token
    url = f"{cfg.base}/{ruta}"
    try:
        r = await (client.post(url, data=params) if metodo == "POST" else client.get(url, params=params))
    except httpx.HTTPError as e:   # red caída, timeout…
        raise IGError(f"Sin conexión con Instagram ({type(e).__name__}).", incierto=incierto_si_red)
    try:
        cuerpo = r.json()
    except Exception:
        cuerpo = {}
    if r.status_code >= 400 or "error" in cuerpo:
        raise _error(r)
    return cuerpo


async def esperar_urls(client, urls, tope=480, intervalo=10, dormir=asyncio.sleep):
    """Espera a que todas las fotos estén públicas (tras el despliegue de la web, 1-3 min)."""
    limite = time.monotonic() + tope
    pendientes = list(urls)
    while True:
        faltan = []
        for u in pendientes:
            try:
                r = await client.head(u)
                if r.status_code in (405, 501):
                    r = await client.get(u)
                ok = r.status_code == 200 and r.headers.get("content-type", "").lower().startswith("image/")
            except httpx.HTTPError:
                ok = False
            if not ok:
                faltan.append(u)
        pendientes = faltan
        if not pendientes:
            return
        if time.monotonic() >= limite:
            raise IGError("Las fotos no han llegado a estar disponibles en la web a tiempo. "
                          "Prueba otra vez en unos minutos.")
        await dormir(intervalo)


async def esperar_contenedor(client, cfg, cid, dormir=asyncio.sleep, esperas=(2, 4, 8, 15, 30, 30, 30, 60, 60, 60)):
    for espera in (0,) + tuple(esperas):
        if espera:
            await dormir(espera)
        d = await _llamar(client, cfg, "GET", cid, fields="status_code")
        estado = d.get("status_code")
        if estado == "FINISHED":
            return
        if estado in ("ERROR", "EXPIRED"):
            raise IGError("Instagram no ha podido procesar las fotos.")
    raise IGError("Instagram tarda demasiado en procesar las fotos. Prueba otra vez en unos minutos.")


async def publicar(client, cfg, urls, texto, dormir=asyncio.sleep) -> dict:
    """Publica una imagen o un carrusel. Devuelve {"media_id", "permalink"}."""
    if not urls:
        raise IGError("No hay fotos para publicar.")
    urls = urls[:MAX_FOTOS]
    uid = cfg.user_id
    if len(urls) == 1:
        d = await _llamar(client, cfg, "POST", f"{uid}/media", image_url=urls[0], caption=texto)
        contenedor = d["id"]
    else:
        hijos = []
        for u in urls:
            d = await _llamar(client, cfg, "POST", f"{uid}/media", image_url=u, is_carousel_item="true")
            hijos.append(d["id"])
        for h in hijos:
            await esperar_contenedor(client, cfg, h, dormir)
        d = await _llamar(client, cfg, "POST", f"{uid}/media", media_type="CAROUSEL",
                          children=",".join(hijos), caption=texto)
        contenedor = d["id"]
    await esperar_contenedor(client, cfg, contenedor, dormir)
    # Si esta llamada falla por red no sabemos si Instagram publicó: se marca como incierto y NO se reintenta sola.
    d = await _llamar(client, cfg, "POST", f"{uid}/media_publish", incierto_si_red=True, creation_id=contenedor)
    media_id = d["id"]
    permalink = ""
    try:
        p = await _llamar(client, cfg, "GET", media_id, fields="permalink")
        permalink = p.get("permalink", "")
    except IGError:
        log.warning("Publicado %s pero no se pudo leer el enlace", media_id)
    return {"media_id": media_id, "permalink": permalink}


def mensaje_usuario(e: IGError, ref: str = "") -> str:
    if e.incierto:
        return ("No he podido confirmar si Instagram llegó a publicarlo. Mira primero el perfil de Instagram "
                f"para no duplicar el post. Si no está, escribe *instagram {ref or '0148'} repetir*.")
    if e.codigo == 190:
        return "El acceso del concesionario a Instagram ha caducado. Avisa al administrador para renovarlo."
    if e.codigo in (4, 9, 17, 32, 613):
        return "Instagram ha limitado las publicaciones por ahora. Prueba más tarde."
    return f"Instagram no ha dejado publicarlo: {str(e)[:300]}"
