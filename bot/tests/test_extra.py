"""Pruebas adicionales: fotos en vuelo, imágenes como archivo, reacciones, Instagram 'incierto' y una prueba aleatoria
con varios trabajadores a la vez.  Se ejecutan junto a las demás:   python -m pytest tests -q
"""
import asyncio, random, unittest

import test_flujo as T          # reutiliza el entorno simulado (WhatsApp, Instagram y git falsos)
from test_flujo import M, MA, MR, LU, OUT, bot, txt, boton, img, textos, esperar, completar, alta


def doc(to, mime="image/jpeg", media="rojo|h|0"):
    return {"from": to, "type": "document", "id": T._id(), "document": {"id": media, "mime_type": mime, "filename": "x"}}


class TestExtra(T.Base):
    async def test_listo_escrito_justo_despues_de_las_fotos_espera_a_que_lleguen(self):
        bot.AUTO_SEGUNDOS = 30                      # que sea el "listo" quien las dé por terminadas
        tareas = [asyncio.create_task(bot.procesar(img(M, "rojo", "h", 0.3))) for _ in range(3)]
        await asyncio.sleep(0.02)                   # las fotos aún se están descargando…
        await bot.procesar(txt(M, "listo"))         # …y el trabajador ya ha escrito "listo"
        await asyncio.gather(*tareas)
        self.assertEqual(bot.SESS[M]["paso"], 0)
        self.assertEqual(len(bot.SESS[M]["c"]["fotos_tmp"]), 3)
        self.assertFalse(any("al menos una foto" in t for t in textos(M)))

    async def test_imagen_enviada_como_archivo_se_acepta(self):
        await bot.procesar(doc(M))
        self.assertEqual(len(bot.SESS[M]["c"]["fotos_tmp"]), 1)
        await bot.procesar(doc(M, "application/pdf"))                 # un PDF no es una foto
        self.assertEqual(len(bot.SESS[M]["c"]["fotos_tmp"]), 1)
        self.assertIn("Solo entiendo", textos(M)[-1])

    async def test_imagen_como_archivo_ilegible_da_pista(self):
        await bot.procesar(doc(M, media="corrupta|h|0"))
        self.assertIn("como foto normal", textos(M)[-1])

    async def test_reaccion_no_genera_respuesta(self):
        await bot.procesar({"from": M, "type": "reaction", "id": T._id(), "reaction": {"emoji": "👍", "message_id": "x"}})
        self.assertEqual(OUT[M], [])
        self.assertNotIn(M, bot.SESS)

    async def test_health_no_expone_credenciales(self):
        from fastapi.testclient import TestClient
        bot.TOKEN = "token-de-prueba"
        bot.PHONE_ID = "phone-id-de-prueba"
        bot.VERIFY = "verify-de-prueba"
        bot.SECRET = "secreto-de-prueba"
        with TestClient(bot.app) as client:
            response = client.get("/health")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("checks", body)
        self.assertTrue(all(isinstance(v, bool) for v in body["checks"].values()))
        self.assertNotIn("token-de-prueba", response.text)
        self.assertNotIn("secreto-de-prueba", response.text)

    async def test_webhook_rechaza_firma_no_configurada(self):
        from fastapi.testclient import TestClient
        original = bot.SECRET
        bot.SECRET = ""
        try:
            with TestClient(bot.app) as client:
                response = client.post("/webhook", content=b'{"entry":[]}')
        finally:
            bot.SECRET = original
        self.assertEqual(response.status_code, 503)

    async def test_incierto_se_puede_repetir_tras_comprobar_el_perfil(self):
        self.ig["timeout_publish"] = True
        await alta(M, fotos=2); await bot.procesar(boton(M, "a:ig_si")); await bot.procesar(boton(M, "a:ig_pub"))
        self.assertTrue(await esperar(lambda: any("No he podido confirmar" in t for t in textos(M))))
        self.assertIn("instagram 0001 repetir", textos(M)[-1])
        self.assertEqual(self.priv()["0001"]["instagram"]["estado"], "incierto")
        self.ig["timeout_publish"] = False
        await bot.procesar(txt(M, "instagram 1"))
        self.assertIn("instagram 0001 repetir", textos(M)[-1])         # sin la palabra "repetir" sigue bloqueado
        await bot.procesar(txt(M, "instagram 1 repetir"))
        self.assertEqual(bot.SESS[M]["paso"], "ig_confirmar")
        await bot.procesar(boton(M, "a:ig_pub"))
        self.assertTrue(await esperar(lambda: any("Publicado en Instagram" in t for t in textos(M))))
        self.assertEqual(self.priv()["0001"]["instagram"]["estado"], "publicado")

    async def test_reinicio_con_instagram_en_curso_lo_marca_incierto(self):
        await alta(M, fotos=2); await bot.procesar(boton(M, "a:ig_no"))
        p = self.priv(); p["0001"]["instagram"] = {"estado": "en_curso", "t": 1}; bot._save(bot.PRIV_FILE, p)
        async with bot.lifespan(bot.app):
            pass
        self.assertEqual(self.priv()["0001"]["instagram"]["estado"], "incierto")
        await bot.procesar(txt(M, "instagram 1"))
        self.assertIn("repetir", textos(M)[-1])

    async def test_cuatro_trabajadores_aleatorios_a_la_vez(self):
        """Cada uno con su ritmo: unos escriben 'listo', otros esperan al avance automático, con ráfagas y mensajes sueltos."""
        rnd = random.Random(7)
        bot.AUTO_SEGUNDOS = 0.3
        plan = {M: ("rojo", 6, True), MA: ("azul", 9, False), MR: ("verde", 3, True), LU: ("gris", 12, False)}
        mats = {M: "1111AAA", MA: "2222BBB", MR: "3333CCC", LU: "4444DDD"}

        async def trabajador(to):
            color, n, con_listo = plan[to]
            tareas = []
            for i in range(n):
                tareas.append(asyncio.create_task(bot.procesar(img(to, color, "h" if i % 2 else "v", rnd.random() * 0.08))))
                await asyncio.sleep(rnd.random() * 0.02)
            if con_listo:
                await bot.procesar(txt(to, "listo"))
            await asyncio.gather(*tareas)
            self.assertTrue(await esperar(lambda: isinstance(bot.SESS.get(to, {}).get("paso"), int), 6))
            await bot.procesar(txt(to, "lista"))                       # un mensaje suelto en mitad del alta no la rompe
            self.assertEqual(await completar(to, matricula=mats[to], marca=f"Marca{to[-1]}"), "confirmar")
            await bot.procesar(boton(to, "a:pub", "Publicar"))

        await asyncio.gather(*[trabajador(to) for to in plan])
        coches = self.coches()
        self.assertEqual(len(coches), 4)
        self.assertEqual(len({c["ref"] for c in coches}), 4)                               # referencias únicas
        for to, (color, n, _) in plan.items():
            c = next(x for x in coches if x["marca"] == f"Marca{to[-1]}")
            self.assertEqual(len(c["fotos"]), n, f"{to}: fotos perdidas o mezcladas")
            self.assertTrue(all((bot.WEB / f).exists() for f in c["fotos"]))
            self.assertEqual(self.priv()[c["ref"]]["matricula"], mats[to])
            self.assertEqual(self.priv()[c["ref"]]["tel"], to)                              # cada alta, de su trabajador
        self.assertEqual(len(list(bot.FOTOS.glob("*.jpg"))), sum(n for _, n, _ in plan.values()))
        self.assertEqual(list(bot.TMP.glob("*.jpg")), [])                                   # sin restos temporales
        self.revisar_limites_whatsapp()


if __name__ == "__main__":
    unittest.main()
