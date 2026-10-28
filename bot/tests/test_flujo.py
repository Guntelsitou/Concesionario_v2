"""Pruebas del bot SIN conectar con Meta: se simulan WhatsApp, Instagram y git.

Ejecutar desde la carpeta bot/:   python -m unittest discover -s tests -v
"""
import asyncio, hashlib, hmac, io, json, os, random, shutil, subprocess, sys, tempfile, time, unittest
from collections import defaultdict
from pathlib import Path

RAIZ = Path(tempfile.mkdtemp(prefix="bot-test-"))
os.environ.update(WEB_DIR=str(RAIZ / "web"), DATA_DIR=str(RAIZ / "data"), WEB_URL="https://web.test",
                  WORKERS="34611111111:Manolo,34622222222:Manuel,34633333333:Marta,34644444444:Luis",
                  AUTO_SEGUNDOS="0.25", WA_VERIFY_TOKEN="secreto", WA_APP_SECRET="clave",
                  IG_TOKEN="tok", IG_USER_ID="999", NEGOCIO_NOMBRE="Motor Ribera", NEGOCIO_TEL="600 000 000",
                  NEGOCIO_CIUDAD="Getafe")
os.environ.pop("ANTHROPIC_API_KEY", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
(RAIZ / "web").mkdir()

import httpx
from PIL import Image
import instagram as ig
import whatsapp_bot as bot

M, MA, MR, LU = "34611111111", "34622222222", "34633333333", "34644444444"
OUT = defaultdict(list)
COLORES = {"rojo": (220, 30, 30), "azul": (30, 60, 220), "verde": (30, 180, 60), "gris": (130, 130, 130)}


async def fake_post(p):
    OUT[p["to"]].append(p)


async def fake_download(media_id):
    color, forma, retraso = media_id.split("|")
    await asyncio.sleep(float(retraso))
    if color == "corrupta":
        return b"esto no es una imagen"
    w, h = (900, 600) if forma == "h" else (600, 900)
    buf = io.BytesIO()
    Image.new("RGB", (w, h), COLORES[color]).save(buf, "JPEG")
    return buf.getvalue()


def cuerpo(p):
    return p["text"]["body"] if p["type"] == "text" else p["interactive"]["body"]["text"]


def textos(to):
    return [cuerpo(p) for p in OUT[to]]


def ids_botones(p):
    i = p.get("interactive", {})
    if i.get("type") == "button":
        return [b["reply"]["id"] for b in i["action"]["buttons"]]
    if i.get("type") == "list":
        return [r["id"] for r in i["action"]["sections"][0]["rows"]]
    return []


_n = [0]


def _id():
    _n[0] += 1
    return f"wamid.{_n[0]}"


def txt(to, t):
    return {"from": to, "type": "text", "id": _id(), "text": {"body": t}}


def boton(to, i, titulo=""):
    return {"from": to, "type": "interactive", "id": _id(), "interactive": {"button_reply": {"id": i, "title": titulo}}}


def lista(to, i):
    return {"from": to, "type": "interactive", "id": _id(), "interactive": {"list_reply": {"id": i, "title": i}}}


def img(to, color="rojo", forma="h", retraso=0.0, ts=None):
    m = {"from": to, "type": "image", "id": _id(), "image": {"id": f"{color}|{forma}|{retraso}"}}
    if ts:
        m["timestamp"] = str(ts)
    return m


DATOS = dict(matricula="1234KLM", marca="Seat", modelo="Ibiza", version="1.0 TSI Style 95 CV", anio="2021", km="32.000",
             precio="14900", combustible="Gasolina", cambio="Manual", cv="95", etiqueta="C", carroceria="Utilitario",
             puertas="5", plazas="5", color="Blanco Nevada", propietarios="1", itv="3/27", consumo="5,4",
             equipamiento="Pantalla, CarPlay, Climatizador", obs="Libro de revisiones al día")


async def esperar(cond, tope=4.0):
    t = time.time()
    while time.time() - t < tope:
        if cond():
            return True
        await asyncio.sleep(0.02)
    return False


async def completar(to, **extra):
    """Contesta todas las preguntas con DATOS (más los cambios que se pasen)."""
    d = {**DATOS, **extra}
    while isinstance(bot.SESS.get(to, {}).get("paso"), int):
        st = bot.STEPS[bot.SESS[to]["paso"]]
        v = d[st["k"]]
        await bot.procesar(txt(to, v) if st["kind"] == "text" else (lista(to, "o:" + v) if st["kind"] == "list" else boton(to, "o:" + v, v)))
    return bot.SESS.get(to, {}).get("paso")


async def alta(to, color="rojo", fotos=4, **extra):
    await asyncio.gather(*[bot.procesar(img(to, color, "h" if i % 2 else "v", random.random() * 0.05)) for i in range(fotos)])
    assert await esperar(lambda: isinstance(bot.SESS.get(to, {}).get("paso"), int)), "no arrancaron las preguntas solas"
    assert await completar(to, **extra) == "confirmar"
    await bot.procesar(boton(to, "a:pub", "Publicar"))


def mock_instagram(estado):
    """Servidor falso de Instagram. `estado` recoge las llamadas y permite provocar fallos."""
    estado.setdefault("llamadas", [])
    estado.setdefault("contenedores", {})

    def manejador(req: httpx.Request):
        ruta = req.url.path
        estado["llamadas"].append((req.method, ruta, dict(httpx.QueryParams(req.content.decode())) if req.method == "POST" else dict(req.url.params)))
        if req.url.host == "web.test":                       # las fotos públicas de la web
            n = estado.setdefault("head", 0)
            estado["head"] = n + 1
            if n < estado.get("head_fallos", 0):
                return httpx.Response(404)
            return httpx.Response(200, headers={"content-type": "image/jpeg"})
        if estado.get("fallo_global"):
            return httpx.Response(400, json={"error": {"message": "token caducado", "code": 190}})
        if req.method == "POST" and ruta.endswith("/media_publish"):
            if estado.get("timeout_publish"):
                raise httpx.ReadTimeout("tiempo agotado")
            return httpx.Response(200, json={"id": "MEDIA1"})
        if req.method == "POST" and ruta.endswith("/media"):
            cid = f"C{len(estado['contenedores']) + 1}"
            estado["contenedores"][cid] = dict(httpx.QueryParams(req.content.decode()))
            return httpx.Response(200, json={"id": cid})
        if req.method == "GET" and "MEDIA1" in ruta:
            return httpx.Response(200, json={"permalink": "https://instagram.com/p/ABC/"})
        if req.method == "GET":
            return httpx.Response(200, json={"status_code": estado.get("status", "FINISHED")})
        return httpx.Response(404)

    return manejador


class Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        OUT.clear()
        bot.TOKEN = "x"
        bot._post, bot.download_media = fake_post, fake_download
        bot.LOCK, bot.GIT_LOCK = asyncio.Lock(), asyncio.Lock()
        bot.LOCKS.clear(); bot.SESS.clear(); bot.VISTOS.clear(); bot.RECHAZOS.clear(); bot.TAREAS.clear()
        bot.AUTO_SEGUNDOS = 0.25
        bot.GIT_PUSH = False
        for p in (RAIZ / "web").glob("*"):
            shutil.rmtree(p) if p.is_dir() else p.unlink()
        for p in (RAIZ / "data").glob("*"):
            shutil.rmtree(p) if p.is_dir() else p.unlink()
        for d in (bot.TMP, bot.BACKUPS, bot.FOTOS):
            d.mkdir(parents=True, exist_ok=True)
        os.environ["IG_TOKEN"] = "tok"
        self.ig = {}
        transporte = httpx.MockTransport(mock_instagram(self.ig))
        ig.nuevo_cliente = lambda: httpx.AsyncClient(transport=transporte, follow_redirects=True)
        orig_esperar, orig_pub = ig.esperar_urls, ig.publicar
        self._orig = (orig_esperar, orig_pub)
        ig.esperar_urls = lambda c, u, **k: orig_esperar(c, u, intervalo=0.01, tope=k.get("tope", 3))
        ig.publicar = lambda c, cfg, u, t: orig_pub(c, cfg, u, t, dormir=lambda s: asyncio.sleep(0.01))

    async def asyncTearDown(self):
        for t in list(bot.TAREAS):
            t.cancel()
        ig.esperar_urls, ig.publicar = self._orig
        await asyncio.sleep(0)

    def coches(self):
        p = bot.JSON_FILE
        return json.loads(p.read_text("utf-8")) if p.exists() else []

    def priv(self):
        return json.loads(bot.PRIV_FILE.read_text("utf-8")) if bot.PRIV_FILE.exists() else {}

    def revisar_limites_whatsapp(self):
        for to, ps in OUT.items():
            for p in ps:
                if p["type"] != "interactive":
                    self.assertLessEqual(len(p["text"]["body"]), 4000)
                    continue
                i = p["interactive"]
                self.assertLessEqual(len(i["body"]["text"]), 1024)
                if i["type"] == "button":
                    bs = i["action"]["buttons"]
                    self.assertLessEqual(len(bs), 3)
                    for b in bs:
                        self.assertLessEqual(len(b["reply"]["title"]), 20, b["reply"]["title"])
                else:
                    rows = i["action"]["sections"][0]["rows"]
                    self.assertLessEqual(len(rows), 10)
                    for r in rows:
                        self.assertLessEqual(len(r["title"]), 24, r["title"])


class TestFlujoBasico(Base):
    async def test_desconocido_rechazado_una_vez(self):
        await bot.procesar(txt("34999", "hola")); await bot.procesar(txt("34999", "hola"))
        self.assertEqual(len(OUT["34999"]), 1)
        self.assertEqual(bot.SESS, {})

    async def test_alta_completa_fotos_primero(self):
        await alta(M, fotos=5)
        c = self.coches()
        self.assertEqual(len(c), 1)
        e = c[0]
        self.assertEqual((e["ref"], e["marca"], e["precio"], e["km"], e["anio"], e["itv"], e["consumo"], e["propietarios"]),
                         ("0001", "Seat", 14900, 32000, 2021, "03/2027", 5.4, 1))
        self.assertEqual(len(e["fotos"]), 5)
        for f in e["fotos"]:
            self.assertTrue((bot.WEB / f).exists())
            self.assertLessEqual(max(Image.open(bot.WEB / f).size), 1600)
        self.assertNotIn("matricula", e)
        self.assertEqual(self.priv()["0001"]["matricula"], "1234KLM")
        self.assertEqual(self.priv()["0001"]["alta_por"], "Manolo")
        self.assertEqual(list(bot.TMP.glob("*")), [])                 # no quedan fotos temporales
        self.assertEqual(len(list(bot.BACKUPS.glob("*.json"))), 0)    # primera alta: no había nada que copiar
        # con Instagram activo ofrece subirlo
        self.assertEqual(ids_botones(OUT[M][-1]), ["a:ig_si", "a:ig_no"])
        self.revisar_limites_whatsapp()

    async def test_solo_una_pregunta_auto_pese_a_rafaga_con_pausas(self):
        for _ in range(3):
            await bot.procesar(img(M)); await asyncio.sleep(0.1)       # pausas menores que el silencio
        self.assertEqual(bot.SESS[M]["paso"], "fotos")
        await asyncio.sleep(0.6)
        self.assertEqual(bot.SESS[M]["paso"], 0)
        self.assertEqual(sum("Ahora unas preguntas" in t for t in textos(M)), 1)

    async def test_foto_tras_empezar_preguntas_se_anade(self):
        await bot.procesar(img(M)); await esperar(lambda: bot.SESS[M]["paso"] == 0)
        await bot.procesar(img(M, "azul"))
        self.assertEqual(len(bot.SESS[M]["c"]["fotos_tmp"]), 2)
        self.assertEqual(bot.SESS[M]["paso"], 0)
        await completar(M)
        await bot.procesar(boton(M, "a:pub"))
        self.assertEqual(len(self.coches()[0]["fotos"]), 2)

    async def test_maximo_20_fotos_en_rafaga(self):
        await asyncio.gather(*[bot.procesar(img(M, retraso=random.random() * 0.05)) for i in range(25)])
        self.assertEqual(len(bot.SESS[M]["c"]["fotos_tmp"]), 20)
        self.assertEqual(sum("Máximo 20" in t for t in textos(M)), 1)
        self.assertEqual(len(list(bot.TMP.glob("*"))), 20)             # las 5 sobrantes se borran

    async def test_orden_de_fotos_por_marca_de_tiempo(self):
        await asyncio.gather(bot.procesar(img(M, "azul", ts=3000)), bot.procesar(img(M, "rojo", ts=1000)),
                             bot.procesar(img(M, "verde", ts=2000)))
        await esperar(lambda: bot.SESS[M]["paso"] == 0); await completar(M); await bot.procesar(boton(M, "a:pub"))
        px = [Image.open(bot.WEB / f).getpixel((10, 10)) for f in self.coches()[0]["fotos"]]
        dominante = ["r" if p[0] > 150 else "g" if p[1] > 120 else "b" for p in px]
        self.assertEqual(dominante, ["r", "g", "b"])                   # rojo, verde, azul

    async def test_foto_corrupta_no_rompe_nada(self):
        await bot.procesar(img(M, "rojo"))
        await bot.procesar(img(M, "corrupta"))
        self.assertEqual(len(bot.SESS[M]["c"]["fotos_tmp"]), 1)
        self.assertTrue(any("No he podido recibir una foto" in t for t in textos(M)))

    async def test_validaciones_y_atras(self):
        await bot.procesar(img(M)); await esperar(lambda: bot.SESS[M]["paso"] == 0)
        await bot.procesar(txt(M, "ab"))                                   # matrícula inválida
        self.assertEqual(bot.SESS[M]["paso"], 0)
        await bot.procesar(txt(M, "1234 klm"))
        self.assertEqual(bot.SESS[M]["c"]["matricula"], "1234KLM")
        await bot.procesar(txt(M, "listo"))                                # "listo" por costumbre: no es marca
        self.assertEqual(bot.SESS[M]["paso"], 1)
        await bot.procesar(txt(M, "-"))                                    # un guion no vale como marca
        self.assertEqual(bot.SESS[M]["paso"], 1)
        for t in ("Seat", "Ibiza", "1.0"):
            await bot.procesar(txt(M, t))
        await bot.procesar(txt(M, "1800"))                                 # año imposible
        self.assertEqual(bot.SESS[M]["paso"], 4)
        await bot.procesar(txt(M, "atrás"))
        self.assertEqual(bot.SESS[M]["paso"], 3)
        await bot.procesar(txt(M, "1.0 TSI")); await bot.procesar(txt(M, "2021"))
        await bot.procesar(txt(M, "abc")); self.assertEqual(bot.SESS[M]["paso"], 5)   # km no numérico
        await bot.procesar(txt(M, "32000")); await bot.procesar(txt(M, "100"))         # precio demasiado bajo
        self.assertEqual(bot.SESS[M]["paso"], 6)
        await bot.procesar(txt(M, "14900"))
        await bot.procesar(txt(M, "Eléctrico"))                            # lista escrita a mano: se acepta
        self.assertEqual(bot.SESS[M]["c"]["combustible"], "Eléctrico")
        await bot.procesar(txt(M, "cosa rara"))                            # opción inválida: repite
        self.assertEqual(bot.SESS[M]["paso"], 8)

    async def test_itv_y_consumo(self):
        for t, ok in (("03/2027", "03/2027"), ("3-27", "03/2027"), ("12.2026", "12/2026"), ("-", ""), ("13/2027", None), ("mañana", None)):
            self.assertEqual(bot.v_itv(t)[0], ok, t)
        for t, ok in (("5,4", 5.4), ("6.25", 6.2), ("-", ""), ("100", None), ("poco", None)):
            self.assertEqual(bot.v_consumo(t)[0], ok, t)

    async def test_nuevo_con_alta_en_curso_pide_confirmar(self):
        await bot.procesar(img(M)); await esperar(lambda: bot.SESS[M]["paso"] == 0)
        await bot.procesar(txt(M, "1234KLM"))
        await bot.procesar(txt(M, "nuevo"))
        self.assertEqual(bot.SESS[M]["paso"], "nuevo?")
        await bot.procesar(txt(M, "nuevo"))                                # insistir confirma
        self.assertEqual(bot.SESS[M]["paso"], "fotos")
        self.assertEqual(list(bot.TMP.glob("*")), [])

    async def test_nuevo_no_sigue_donde_estaba(self):
        await bot.procesar(img(M)); await esperar(lambda: bot.SESS[M]["paso"] == 0)
        await bot.procesar(txt(M, "1234KLM")); await bot.procesar(txt(M, "nuevo"))
        await bot.procesar(boton(M, "a:nuevo_no"))
        self.assertEqual(bot.SESS[M]["paso"], 1)
        self.assertEqual(bot.SESS[M]["c"]["matricula"], "1234KLM")

    async def test_cancelar_borra_todo(self):
        await bot.procesar(img(M)); await bot.procesar(img(M))
        await bot.procesar(txt(M, "cancelar"))
        self.assertNotIn(M, bot.SESS)
        self.assertEqual(list(bot.TMP.glob("*")), [])
        await asyncio.sleep(0.5)
        self.assertEqual(sum("Ahora unas preguntas" in t for t in textos(M)), 0)   # el temporizador no revive nada

    async def test_comandos(self):
        await alta(M, fotos=2); await bot.procesar(boton(M, "a:ig_no"))
        await bot.procesar(txt(M, "reservado 1")); self.assertTrue(self.coches()[0]["reservado"])
        await bot.procesar(txt(M, "vendido 0001")); c = self.coches()[0]
        self.assertTrue(c["vendido"]); self.assertNotIn("reservado", c)
        await bot.procesar(txt(M, "disponible 0001")); self.assertNotIn("vendido", self.coches()[0])
        await bot.procesar(txt(M, "precio 0001 13.900")); self.assertEqual(self.coches()[0]["precio"], 13900)
        await bot.procesar(txt(M, "precio 0001 5")); self.assertEqual(self.coches()[0]["precio"], 13900)
        await bot.procesar(txt(M, "lista")); self.assertIn("0001 · Seat Ibiza", textos(M)[-1])
        await bot.procesar(txt(M, "vendido 0099")); self.assertIn("No encuentro", textos(M)[-1])
        self.assertGreaterEqual(len(list(bot.BACKUPS.glob("*.json"))), 4)
        self.revisar_limites_whatsapp()

    async def test_tipos_no_soportados(self):
        await bot.procesar({"from": M, "type": "audio", "id": _id(), "audio": {}})
        self.assertIn("Solo entiendo", textos(M)[-1])


class TestVariosTrabajadoresAlaVez(Base):
    async def test_dos_trabajadores_en_paralelo_no_se_mezclan(self):
        async def flujo(to, color, marca):
            await alta(to, color, fotos=6, marca=marca, matricula={"Manolo": "1111AAA"}.get(marca, "2222BBB"),
                       modelo="Modelo" + marca)
        await asyncio.gather(flujo(M, "rojo", "Manolo"), flujo(MA, "azul", "Manuel"))
        cs = {c["marca"]: c for c in self.coches()}
        self.assertEqual(sorted(cs), ["Manolo", "Manuel"])
        self.assertNotEqual(cs["Manolo"]["ref"], cs["Manuel"]["ref"])
        for marca, color in (("Manolo", "r"), ("Manuel", "b")):
            self.assertEqual(len(cs[marca]["fotos"]), 6)
            for f in cs[marca]["fotos"]:
                r, g, b = Image.open(bot.WEB / f).getpixel((20, 20))
                self.assertTrue((r > 150) if color == "r" else (b > 150), (marca, f, (r, g, b)))
        self.assertEqual(self.priv()[cs["Manolo"]["ref"]]["alta_por"], "Manolo")
        self.assertEqual(self.priv()[cs["Manuel"]["ref"]]["alta_por"], "Manuel")
        self.assertEqual(list(bot.TMP.glob("*")), [])

    async def test_cuatro_a_la_vez_con_ritmos_distintos(self):
        async def flujo(to, color, i):
            await asyncio.sleep(random.random() * 0.1)
            await asyncio.gather(*[bot.procesar(img(to, color, "h", random.random() * 0.08)) for _ in range(7)])
            await esperar(lambda: isinstance(bot.SESS.get(to, {}).get("paso"), int))
            while isinstance(bot.SESS[to]["paso"], int):                  # contesta paso a paso con pausas
                await asyncio.sleep(random.random() * 0.01)
                st = bot.STEPS[bot.SESS[to]["paso"]]
                d = {**DATOS, "matricula": f"{i}{i}{i}{i}ZZZ", "marca": f"Marca{i}"}
                v = d[st["k"]]
                await bot.procesar(txt(to, v) if st["kind"] == "text" else (lista(to, "o:" + v) if st["kind"] == "list" else boton(to, "o:" + v, v)))
            await bot.procesar(boton(to, "a:pub"))
        await asyncio.gather(*[flujo(t, c, i) for i, (t, c) in enumerate([(M, "rojo"), (MA, "azul"), (MR, "verde"), (LU, "gris")], 1)])
        cs = self.coches()
        self.assertEqual(len(cs), 4)
        self.assertEqual(sorted(c["ref"] for c in cs), ["0001", "0002", "0003", "0004"])
        self.assertTrue(all(len(c["fotos"]) == 7 for c in cs))
        nombres = [f for c in cs for f in c["fotos"]]
        self.assertEqual(len(nombres), len(set(nombres)))                  # ninguna foto pisa a otra
        self.assertEqual(len(self.priv()), 4)

    async def test_mensajes_de_un_trabajador_no_afectan_a_otro(self):
        await bot.procesar(img(M)); await bot.procesar(img(MA))
        await esperar(lambda: bot.SESS[M]["paso"] == 0 and bot.SESS[MA]["paso"] == 0)
        await bot.procesar(txt(M, "cancelar"))
        self.assertNotIn(M, bot.SESS); self.assertIn(MA, bot.SESS)
        self.assertEqual(len(bot.SESS[MA]["c"]["fotos_tmp"]), 1)

    async def test_misma_matricula_a_la_vez_avisa(self):
        for to in (M, MA):
            await bot.procesar(img(to)); await esperar(lambda: bot.SESS[to]["paso"] == 0)
        await bot.procesar(txt(M, "1234KLM"))
        await bot.procesar(txt(MA, "1234 klm"))
        self.assertEqual(bot.SESS[MA]["paso"], "dup")
        self.assertIn("Manolo está dando de alta", textos(MA)[-1])
        await bot.procesar(boton(MA, "a:dup_si"))
        self.assertEqual(bot.SESS[MA]["paso"], 1)

    async def test_matricula_ya_publicada_avisa_y_se_puede_cancelar(self):
        await alta(M, fotos=1); await bot.procesar(boton(M, "a:ig_no"))
        await bot.procesar(img(MA)); await esperar(lambda: bot.SESS[MA]["paso"] == 0)
        await bot.procesar(txt(MA, "1234KLM"))
        self.assertEqual(bot.SESS[MA]["paso"], "dup")
        self.assertIn("ref. 0001", textos(MA)[-1])
        await bot.procesar(boton(MA, "a:cancel"))
        self.assertNotIn(MA, bot.SESS)
        self.assertEqual(len(self.coches()), 1)

    async def test_coche_vendido_permite_misma_matricula(self):
        await alta(M, fotos=1); await bot.procesar(boton(M, "a:ig_no")); await bot.procesar(txt(M, "vendido 1"))
        await bot.procesar(img(MA)); await esperar(lambda: bot.SESS[MA]["paso"] == 0)
        await bot.procesar(txt(MA, "1234KLM"))
        self.assertEqual(bot.SESS[MA]["paso"], 1)

    async def test_choque_al_publicar_no_duplica(self):
        """Dos altas con la misma matrícula que se saltaron el aviso: la segunda NO se publica."""
        for to in (M, MA):
            await bot.procesar(img(to)); await esperar(lambda: bot.SESS[to]["paso"] == 0)
        # se fuerza el estado de carrera: ambas llegan a 'confirmar' con la misma matrícula sin pasar por el aviso
        for to in (M, MA):
            s = bot.SESS[to]; s["paso"] = 0
        await completar(M, matricula="9999ZZZ")
        s = bot.SESS[MA]; s["c"]["matricula"] = "9999ZZZ"; s["paso"] = 1
        await completar(MA, matricula="9999ZZZ")
        await bot.procesar(boton(M, "a:pub"))
        await bot.procesar(boton(MA, "a:pub"))
        self.assertEqual(len(self.coches()), 1)
        self.assertIn("No lo he publicado", textos(MA)[-1])
        self.assertNotIn(MA, bot.SESS)

    async def test_publicaciones_simultaneas_refs_distintas(self):
        async def preparar(to, mat):
            await bot.procesar(img(to)); await esperar(lambda: bot.SESS[to]["paso"] == 0)
            await completar(to, matricula=mat)
        await asyncio.gather(preparar(M, "1000AAA"), preparar(MA, "2000BBB"), preparar(MR, "3000CCC"))
        await asyncio.gather(*[bot.procesar(boton(t, "a:pub")) for t in (M, MA, MR)])
        self.assertEqual(sorted(c["ref"] for c in self.coches()), ["0001", "0002", "0003"])

    async def test_ack_de_foto_tardia_una_sola_vez(self):
        await bot.procesar(img(M)); await esperar(lambda: bot.SESS[M]["paso"] == 0)
        await asyncio.gather(*[bot.procesar(img(M)) for _ in range(5)])
        self.assertLessEqual(sum("Foto añadida" in t for t in textos(M)), 1)


class TestRobustez(Base):
    async def test_reinicio_en_mitad_de_las_preguntas(self):
        await bot.procesar(img(M)); await esperar(lambda: bot.SESS[M]["paso"] == 0)
        await bot.procesar(txt(M, "1234KLM")); await bot.procesar(txt(M, "Seat"))
        bot.SESS.clear(); bot.LOCKS.clear()                               # "se apaga y se enciende el servidor"
        bot.cargar_sesiones()
        self.assertEqual(bot.SESS[M]["paso"], 2)
        await completar(M)
        await bot.procesar(boton(M, "a:pub"))
        self.assertEqual(len(self.coches()), 1)
        self.assertEqual(len(self.coches()[0]["fotos"]), 1)

    async def test_reinicio_durante_la_espera_de_fotos(self):
        await bot.procesar(img(M)); await bot.procesar(img(M))
        bot.SESS.clear(); bot.cargar_sesiones()
        bot.programar_auto(M, bot.SESS[M]["v"])                            # lo que hace el arranque
        self.assertTrue(await esperar(lambda: bot.SESS[M]["paso"] == 0))

    async def test_sesiones_de_la_version_anterior_se_leen(self):
        viejo = RAIZ / "data" / "tmp" / "vieja.jpg"; Image.new("RGB", (50, 50)).save(viejo)
        bot._save(bot.SESS_FILE, {M: {"paso": 3, "c": {"fotos_tmp": [str(viejo)], "marca": "Seat"}}})
        bot.cargar_sesiones()
        self.assertEqual(bot.SESS[M]["c"]["fotos_tmp"][0]["p"], str(viejo))
        self.assertIn("v", bot.SESS[M])

    async def test_sesion_caducada_se_limpia(self):
        await bot.procesar(img(M))
        bot.SESS[M]["t"] = time.time() - 13 * 3600
        bot.caducar_sesiones()
        self.assertNotIn(M, bot.SESS); self.assertEqual(list(bot.TMP.glob("*")), [])

    async def test_fallo_al_escribir_el_json_deja_todo_limpio(self):
        await bot.procesar(img(M)); await bot.procesar(img(M)); await esperar(lambda: bot.SESS[M]["paso"] == 0)
        await completar(M)
        original = bot._save

        def roto(p, obj):
            if p == bot.JSON_FILE:
                raise OSError("disco lleno")
            return original(p, obj)
        bot._save = roto
        try:
            await bot.procesar(boton(M, "a:pub"))
        finally:
            bot._save = original
        self.assertEqual(self.coches(), [])
        self.assertEqual(self.priv(), {})                                  # la matrícula tampoco queda
        self.assertEqual(list(bot.FOTOS.glob("*.jpg")), [])                # ni fotos huérfanas
        self.assertEqual(list((bot.FOTOS / "ig").glob("*.jpg")) if (bot.FOTOS / "ig").exists() else [], [])
        self.assertEqual(bot.SESS[M]["paso"], "confirmar")                 # el trabajador puede volver a pulsar Publicar
        self.assertIn("algo ha fallado", textos(M)[-3].lower() + textos(M)[-2].lower() + textos(M)[-1].lower())
        await bot.procesar(boton(M, "a:pub"))
        self.assertEqual(len(self.coches()), 1)

    async def test_json_existente_se_respeta_y_ref_sigue(self):
        shutil.copy(Path(__file__).resolve().parents[2] / "web" / "vehiculos.json", bot.JSON_FILE)
        await alta(M, fotos=1)
        cs = self.coches()
        self.assertEqual(cs[0]["ref"], "0149"); self.assertEqual(len(cs), 10)
        self.assertEqual(len(list(bot.BACKUPS.glob("*.json"))), 1)

    async def test_publicar_dos_veces_el_mismo_boton(self):
        await bot.procesar(img(M)); await esperar(lambda: bot.SESS[M]["paso"] == 0); await completar(M)
        await asyncio.gather(bot.procesar(boton(M, "a:pub")), bot.procesar(boton(M, "a:pub")))
        self.assertEqual(len(self.coches()), 1)

    async def test_error_inesperado_repite_la_pregunta(self):
        await bot.procesar(img(M)); await esperar(lambda: bot.SESS[M]["paso"] == 0)
        orig = bot.buscar_duplicado
        bot.buscar_duplicado = lambda *a: (_ for _ in ()).throw(RuntimeError("fallo raro"))
        try:
            await bot.procesar(txt(M, "1234KLM"))
        finally:
            bot.buscar_duplicado = orig
        self.assertTrue(any("algo ha fallado" in t.lower() for t in textos(M)))
        self.assertIn(M, bot.SESS)

    async def test_peticiones_http_firma_y_duplicados(self):
        from fastapi.testclient import TestClient
        bot.SECRET = "clave"
        bot.TOKEN = "tok"
        bot.PHONE_ID = "999"
        bot.VERIFY = "secreto"
        with TestClient(bot.app) as cli:
            cuerpo_ = json.dumps({"entry": [{"changes": [{"value": {"messages": [txt("34999", "hola")]}}]}]}).encode()
            firma = "sha256=" + hmac.new(b"clave", cuerpo_, hashlib.sha256).hexdigest()
            self.assertEqual(cli.post("/webhook", content=cuerpo_, headers={"X-Hub-Signature-256": "sha256=mala"}).status_code, 403)
            self.assertEqual(cli.post("/webhook", content=cuerpo_, headers={"X-Hub-Signature-256": firma}).status_code, 200)
            self.assertEqual(cli.post("/webhook", content=cuerpo_, headers={"X-Hub-Signature-256": firma}).status_code, 200)
            self.assertEqual(len(OUT["34999"]), 1)                            # el reintento de Meta no se procesa dos veces
            self.assertEqual(cli.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "secreto", "hub.challenge": "42"}).text, "42")
            self.assertEqual(cli.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "no", "hub.challenge": "42"}).status_code, 403)
            self.assertEqual(cli.get("/").json()["instagram"], True)


class TestGit(Base):
    async def test_git_sube_y_avisa_si_falla(self):
        if not shutil.which("git"):
            self.skipTest("sin git")
        remoto, local = RAIZ / "remoto.git", bot.WEB
        subprocess.run(["git", "init", "--bare", str(remoto)], check=True, capture_output=True)
        run = lambda *a: subprocess.run(a, cwd=local, check=True, capture_output=True)
        rama = "workik/test-sync"
        run("git", "init", "-b", rama); run("git", "config", "user.email", "t@t"); run("git", "config", "user.name", "t")
        run("git", "remote", "add", "origin", str(remoto))
        (local / "x.txt").write_text("1"); run("git", "add", "."); run("git", "commit", "-m", "i"); run("git", "push", "-u", "origin", rama)
        bot.GIT_PUSH = True
        await alta(M, fotos=2)
        log = subprocess.run(["git", "log", rama, "--oneline"], cwd=remoto, capture_output=True, text=True).stdout
        self.assertIn("Alta coche 0001", log)
        self.assertIn("Estará visible", textos(M)[-2])
        run("git", "remote", "set-url", "origin", str(RAIZ / "no-existe.git"))   # el remoto desaparece
        await bot.procesar(txt(M, "vendido 1"))
        self.assertIn("No he podido subir el cambio", textos(M)[-1])
        self.assertTrue(self.coches()[0]["vendido"])                            # el cambio local sí se guarda


class TestInstagram(Base):
    async def _hasta_preview(self, fotos=4):
        await alta(M, fotos=fotos)
        await bot.procesar(boton(M, "a:ig_si"))

    async def test_oferta_preview_y_publicacion_en_carrusel(self):
        await self._hasta_preview(4)
        prev = textos(M)[-2]
        self.assertIn("Así quedaría en Instagram", prev); self.assertIn("14.900 €", prev); self.assertIn("#seat", prev)
        self.assertIn("ref. 0001", prev); self.assertNotIn("1234KLM", prev)                    # la matrícula nunca sale
        self.assertEqual(ids_botones(OUT[M][-1]), ["a:ig_pub", "a:ig_texto", "a:ig_no"])
        await bot.procesar(boton(M, "a:ig_pub"))
        self.assertTrue(await esperar(lambda: any("Publicado en Instagram" in t for t in textos(M))))
        post = [(m, r) for m, r, _ in self.ig["llamadas"] if m == "POST"]
        hijos = [v for v in self.ig["contenedores"].values() if v.get("is_carousel_item")]
        self.assertEqual(len(hijos), 4)
        self.assertTrue(all(h["image_url"].startswith("https://web.test/fotos/ig/0001-") for h in hijos))
        carrusel = [v for v in self.ig["contenedores"].values() if v.get("media_type") == "CAROUSEL"][0]
        self.assertEqual(carrusel["children"], "C1,C2,C3,C4")
        self.assertIn("14.900 €", carrusel["caption"])
        self.assertEqual(post[-1][1], "/v26.0/999/media_publish")
        self.assertEqual(self.priv()["0001"]["instagram"]["estado"], "publicado")
        self.assertIn("https://instagram.com/p/ABC/", textos(M)[-1])
        for i in range(1, 5):
            im = Image.open(bot.WEB / f"fotos/ig/0001-{i}.jpg")
            self.assertEqual(im.size, (1080, 1080)); self.assertEqual(im.format, "JPEG")
        self.assertNotIn(M, bot.SESS)
        self.revisar_limites_whatsapp()

    async def test_una_sola_foto_va_como_imagen_simple(self):
        await self._hasta_preview(1)
        await bot.procesar(boton(M, "a:ig_pub"))
        await esperar(lambda: any("Publicado en Instagram" in t for t in textos(M)))
        self.assertEqual([v.get("media_type") for v in self.ig["contenedores"].values()], [None])
        self.assertIn("caption", list(self.ig["contenedores"].values())[0])

    async def test_mas_de_10_fotos_solo_10(self):
        await self._hasta_preview(14)
        await bot.procesar(boton(M, "a:ig_pub"))
        await esperar(lambda: any("Publicado en Instagram" in t for t in textos(M)))
        self.assertEqual(len([v for v in self.ig["contenedores"].values() if v.get("is_carousel_item")]), 10)
        self.assertEqual(len(list((bot.FOTOS / "ig").glob("*.jpg"))), 10)

    async def test_cambiar_texto(self):
        await self._hasta_preview(2)
        await bot.procesar(boton(M, "a:ig_texto"))
        self.assertEqual(bot.SESS[M]["paso"], "ig_texto")
        await bot.procesar(txt(M, "x" * 2500)); self.assertEqual(bot.SESS[M]["paso"], "ig_texto")   # demasiado largo
        await bot.procesar(txt(M, "Oferta de la semana 🔥 Seat Ibiza"))
        self.assertEqual(bot.SESS[M]["paso"], "ig_confirmar")
        await bot.procesar(boton(M, "a:ig_pub"))
        await esperar(lambda: any("Publicado en Instagram" in t for t in textos(M)))
        self.assertEqual([v for v in self.ig["contenedores"].values() if v.get("media_type") == "CAROUSEL"][0]["caption"], "Oferta de la semana 🔥 Seat Ibiza")

    async def test_no_subir_deja_la_web_publicada(self):
        await alta(M, fotos=2)
        await bot.procesar(boton(M, "a:ig_no"))
        self.assertNotIn(M, bot.SESS); self.assertEqual(len(self.coches()), 1)
        self.assertEqual(self.ig["llamadas"], [])
        self.assertIn("instagram 0001", textos(M)[-1])

    async def test_comando_instagram_para_coche_antiguo_y_no_duplica(self):
        await alta(M, fotos=3); await bot.procesar(boton(M, "a:ig_no"))
        shutil.rmtree(bot.FOTOS / "ig")                                       # coche anterior a la función: sin imágenes de IG
        await bot.procesar(txt(M, "instagram 1"))
        self.assertEqual(bot.SESS[M]["paso"], "ig_confirmar")
        await bot.procesar(boton(M, "a:ig_pub"))
        await esperar(lambda: any("Publicado en Instagram" in t for t in textos(M)))
        self.assertTrue((bot.FOTOS / "ig" / "0001-1.jpg").exists())         # se generan al momento
        n_antes = len(self.ig["llamadas"])
        await bot.procesar(txt(M, "instagram 1"))
        self.assertIn("ya está en Instagram", textos(M)[-1])
        self.assertEqual(len(self.ig["llamadas"]), n_antes)                   # ni una llamada más a Instagram

    async def test_doble_pulsacion_no_publica_dos_veces(self):
        await self._hasta_preview(2)
        await asyncio.gather(bot.procesar(boton(M, "a:ig_pub")), bot.procesar(boton(M, "a:ig_pub")))
        await esperar(lambda: any("Publicado en Instagram" in t for t in textos(M)))
        await asyncio.sleep(0.2)
        self.assertEqual(sum(1 for m, r, _ in self.ig["llamadas"] if r.endswith("/media_publish")), 1)

    async def test_espera_a_que_las_fotos_esten_en_la_web(self):
        self.ig["head_fallos"] = 3
        await self._hasta_preview(2)
        await bot.procesar(boton(M, "a:ig_pub"))
        self.assertTrue(await esperar(lambda: any("Publicado en Instagram" in t for t in textos(M)), 6))
        self.assertGreater(self.ig["head"], 3)

    async def test_fotos_nunca_disponibles(self):
        self.ig["head_fallos"] = 10 ** 6
        await self._hasta_preview(2)
        await bot.procesar(boton(M, "a:ig_pub"))
        self.assertTrue(await esperar(lambda: any("no han llegado" in t for t in textos(M)), 6))
        self.assertNotIn("instagram", self.priv()["0001"])                    # queda libre para reintentar

    async def test_token_caducado(self):
        self.ig["fallo_global"] = True
        await self._hasta_preview(2)
        await bot.procesar(boton(M, "a:ig_pub"))
        self.assertTrue(await esperar(lambda: any("ha caducado" in t for t in textos(M))))
        self.assertNotIn("instagram", self.priv()["0001"])
        self.assertEqual(len(self.coches()), 1)                              # la web no se toca

    async def test_publicar_con_timeout_queda_incierto_y_no_se_reintenta(self):
        self.ig["timeout_publish"] = True
        await self._hasta_preview(2)
        await bot.procesar(boton(M, "a:ig_pub"))
        self.assertTrue(await esperar(lambda: any("No he podido confirmar" in t for t in textos(M))))
        self.assertEqual(self.priv()["0001"]["instagram"]["estado"], "incierto")
        n = len(self.ig["llamadas"])
        await bot.procesar(txt(M, "instagram 1"))
        self.assertIn("Mira el perfil", textos(M)[-1])
        self.assertEqual(len(self.ig["llamadas"]), n)

    async def test_contenedor_en_error(self):
        self.ig["status"] = "ERROR"
        await self._hasta_preview(2)
        await bot.procesar(boton(M, "a:ig_pub"))
        self.assertTrue(await esperar(lambda: any("no ha podido procesar" in t for t in textos(M))))

    async def test_sin_instagram_configurado(self):
        os.environ["IG_TOKEN"] = ""
        await alta(M, fotos=2)
        self.assertNotIn(M, bot.SESS)                                         # no ofrece nada
        self.assertFalse((bot.FOTOS / "ig").exists())
        await bot.procesar(txt(M, "instagram 1"))
        self.assertIn("no está configurado", textos(M)[-1])

    async def test_foto_nueva_tras_oferta_empieza_otro_coche(self):
        await alta(M, fotos=2)
        self.assertEqual(bot.SESS[M]["paso"], "ig_oferta")
        await bot.procesar(img(M, "azul"))
        self.assertEqual(bot.SESS[M]["paso"], "fotos"); self.assertEqual(len(bot.SESS[M]["c"]["fotos_tmp"]), 1)

    async def test_trabajadores_distintos_publican_en_instagram_a_la_vez(self):
        await asyncio.gather(alta(M, fotos=2, matricula="1111AAA"), alta(MA, "azul", fotos=3, matricula="2222BBB"))
        await asyncio.gather(bot.procesar(boton(M, "a:ig_si")), bot.procesar(boton(MA, "a:ig_si")))
        await asyncio.gather(bot.procesar(boton(M, "a:ig_pub")), bot.procesar(boton(MA, "a:ig_pub")))
        self.assertTrue(await esperar(lambda: any("Publicado en Instagram" in t for t in textos(M)) and any("Publicado en Instagram" in t for t in textos(MA))))
        self.assertEqual(sum(1 for m, r, _ in self.ig["llamadas"] if r.endswith("/media_publish")), 2)


class TestTextoInstagram(unittest.TestCase):
    NEG = {"nombre": "Motor Ribera", "tel": "600 000 000", "ciudad": "Getafe"}
    C = dict(ref="0148", marca="Volkswagen", modelo="Golf", version="1.5 TSI Life 130 CV", anio=2020, km=58400, precio=18900,
             combustible="Gasolina", cambio="Manual", cv=130, carroceria="Compacto", equipamiento=["A", "B", "C", "D", "E"],
             descripcion="Un propietario.")

    def test_formato(self):
        t = ig.caption(self.C, self.NEG)
        for s in ("Volkswagen Golf", "2020", "58.400 km", "18.900 €", "600 000 000", "Getafe", "#cochesdeocasion", "#volkswagengolf", "ref. 0148"):
            self.assertIn(s, t)
        self.assertEqual(t.count("•"), 4)
        self.assertLessEqual(t.count("#"), 12)

    def test_limite_2200_recorta_descripcion_no_datos(self):
        c = {**self.C, "descripcion": "Texto largo. " * 400}
        t = ig.caption(c, self.NEG)
        self.assertLessEqual(len(t), 2200)
        self.assertIn("18.900 €", t); self.assertIn("ref. 0148", t); self.assertIn("#cochesdeocasion", t)

    def test_hashtags_sin_acentos_ni_duplicados(self):
        c = {**self.C, "marca": "Citroën", "modelo": "C4 Cactus", "combustible": "Diésel"}
        t = ig.caption(c, self.NEG)
        self.assertIn("#citroen", t); self.assertIn("#diesel", t); self.assertNotIn("ë", t.split("#", 1)[1])


if __name__ == "__main__":
    unittest.main()
