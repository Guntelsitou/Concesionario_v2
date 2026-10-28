# Concesionario: catálogo web + gestión de inventario + asistente de WhatsApp (+ Instagram)

El mismo servicio FastAPI sirve la web, expone el inventario en `GET /api/vehiculos` y mantiene el webhook de WhatsApp. El inventario sigue en `web/vehiculos.json`, así que el bot y el panel editan el mismo catálogo. `GET /health` informa del estado de configuración sin mostrar secretos.

El panel está en `/admin.html`. Define `ADMIN_API_TOKEN` largo y aleatorio en `bot/.env`; permite cambiar el precio, estado y descripción de vehículos publicados. Las altas nuevas y las fotos se gestionan desde el bot.

## Cómo funciona
Trabajador (su WhatsApp) → número del negocio (bot) → `web/vehiculos.json` + `web/fotos/` → Web pública → (opcional) Instagram

1. El trabajador **manda las fotos** del coche al chat del bot. No hace falta escribir nada antes.
2. Cuando deja de enviar fotos (10 s por defecto, o si escribe *listo*), el bot empieza a hacerle **20 preguntas**
   con botones y listas: matrícula (privada), marca, modelo, versión, año, km, precio, combustible, cambio, CV,
   etiqueta DGT, carrocería, puertas, plazas, color, propietarios, ITV, consumo, equipamiento y observaciones.
   Si falta alguna foto, puede seguir enviándola durante las preguntas: se añade sola.
3. Resumen y botón **Publicar**. El anuncio sale en la web (1-2 minutos si hay despliegue automático).
4. Si Instagram está configurado, el bot pregunta **«¿Subirlo también a Instagram?»**, enseña cómo quedaría el post
   (con texto y hashtags generados) y el trabajador puede publicarlo, cambiar el texto o dejarlo.

Otros mensajes: `lista`, `reservado 0148`, `vendido 0148`, `disponible 0148`, `precio 0148 17900`,
`instagram 0148` (subir un coche ya publicado), `atrás`, `cancelar`, `?` (repite la pregunta), `ayuda`.

## Varios trabajadores a la vez
Cada trabajador tiene su propia alta, identificada por su número de móvil. Manolo y Manuel pueden subir coches
al mismo tiempo sin mezclarse: sus fotos y respuestas van cada una a su coche, y las referencias (0148, 0149…) no se repiten.
- Si dos personas empiezan **la misma matrícula**, el bot avisa al segundo («X está dando de alta ahora mismo ese coche») y le deja decidir.
- Un trabajador lleva **un coche cada vez**. Cuando termina, las fotos siguientes empiezan otro.
- Si el servidor se reinicia, las altas a medias se recuperan.
- **Importante:** arranca el bot con **un solo proceso** (`uvicorn whatsapp_bot:app`, sin `--workers`). El orden de los mensajes
  y los cerrojos viven en memoria.

## Puesta en marcha con WhatsApp Business Cloud API (oficial, de Meta)
1. developers.facebook.com → Crear app (tipo Empresa) → añadir **WhatsApp**.
   Para pruebas Meta da un número gratuito y deja añadir hasta 5 móviles. Para producción: número real del concesionario y verificación de empresa.
2. Crear un **usuario del sistema** con token permanente (`whatsapp_business_messaging` y `whatsapp_business_management`).
3. `cp bot/.env.example bot/.env` y rellenar los datos (arriba del todo están los de WhatsApp y `WORKERS`).
   `WA_APP_SECRET` es obligatorio: sin la firma de Meta, el webhook responde 503 y no acepta mensajes.
4. Ejecutar con Docker: `docker compose up --build -d` (requiere Docker Compose y el fichero `bot/.env`).
   Alternativa local: desde la raíz, `pip install -r bot/requirements.txt` y `uvicorn bot.whatsapp_bot:app --host 0.0.0.0 --port 8000`.
   Este servicio también sirve la web: abre `http://localhost:8000/` y el panel `http://localhost:8000/admin.html`.
   Para activar Claude o el bot alternativo de Telegram, instala además `bot/requirements-optional.txt`.
   Necesita una URL pública con HTTPS (VPS con Caddy/nginx, Railway, Render; para pruebas, `ngrok http 8000`). El backend expone `/health` para la comprobación de salud.
5. En Meta → WhatsApp → Configuración → Webhook: `https://tu-dominio/webhook`, token de verificación = `WA_VERIFY_TOKEN`, suscripción al campo **messages**.
6. El panel funciona desde el mismo host del backend. Si publicas la web por separado en Cloudflare Pages o Netlify, el catálogo puede seguir leyendo `vehiculos.json`, pero la gestión necesita abrirse en el backend o configurar una integración de origen/API. Para que los cambios del bot o del panel se publiquen en un sitio estático, habilita `GIT_PUSH=true` en un clon del repositorio con permisos de push.

## Instagram (opcional)
Qué necesitas:
1. Una **cuenta de Instagram profesional** (empresa o creador). Con el flujo de Facebook, conectada a una página de Facebook.
2. Una **app de Meta** (puede ser la misma de WhatsApp) con el producto de Instagram y el permiso de publicar contenido:
   `instagram_content_publish` (más `instagram_basic` y `pages_read_engagement`) con el flujo de Facebook, o `instagram_business_content_publish`
   con el flujo de Instagram (en ese caso, configura la base que admite tu app en `IG_API_BASE`, por ejemplo `https://graph.instagram.com/v26.0`).
   Meta decide si basta con el acceso estándar o hace falta revisión de la app: **compruébalo en tu panel** antes de prometérselo a un cliente.
3. `IG_USER_ID` (el número de la cuenta, no el @usuario) y `IG_TOKEN` (de larga duración o de usuario del sistema) en `.env`.
4. **Despliegue automático de la web (`GIT_PUSH=true`)**: Instagram descarga las fotos desde `WEB_URL`, así que el bot las sube a la web
   (`web/fotos/ig/`, en formato cuadrado 1080×1080 con fondo desenfocado, sin recortar el coche) y espera a que estén públicas.

Cómo se comporta: carrusel de hasta 10 fotos (una sola foto se publica como imagen simple); si algo falla, la web **no se toca**;
no publica dos veces el mismo coche; si no puede confirmar si Instagram publicó (corte de red), lo marca como **incierto** y pide mirar
el perfil antes de repetir con `instagram 0148 repetir`. Meta limita las publicaciones por API (en su documentación, entre 50 y 100 al día).
Los textos del post incluyen la garantía y el teléfono configurados en `.env`: revísalos para que digan lo que el concesionario ofrece de verdad.

## Despliegue y datos
- La primera instalación ya trae `web/vehiculos.json`; el estado operativo vive en `bot/data/` y las fotos reales en `web/fotos/`.
- En Docker, los datos de sesión y matrícula privada persisten en el volumen `bot-data`; el proyecto se monta desde el host para que inventario, Git y fotos mantengan su comportamiento actual.
- El webhook requiere `WA_APP_SECRET` y valida `X-Hub-Signature-256`. No desactives esa validación en producción.
- Para editar el inventario desde `/admin.html`, configura `ADMIN_API_TOKEN` y publica el servicio bajo HTTPS. La API pública no expone matrículas; se guardan aparte en `bot/data/privado.json`.
- El bot requiere un único proceso/replica: sus cerrojos y deduplicación residen en memoria.
- El endpoint `/health` indica qué variables/configuración operativa falta, sin devolver secretos.

## Pruebas
`cd bot && pip install -r requirements-dev.txt && python -m pytest tests -q` (pruebas sin conectar con Meta: WhatsApp, Instagram y Git se simulan).

## Costes
Meta cobra por mensaje en la API de WhatsApp Business; las respuestas dentro de las 24 h siguientes a un mensaje del trabajador
suelen ser gratuitas, pero revisa la tarifa vigente. Servidor 0-5 €/mes, dominio ~10 €/año. Cloudflare Pages/GitHub: 0 €.

## Importante
- El inventario, nombre, teléfono, correo, dirección, horario y textos legales actuales contienen datos de demostración. Sustitúyelos por datos reales y revisados antes de promocionar la web. Verifica también las condiciones de garantía y financiación, y prepara los textos de privacidad, cookies y aviso legal adecuados al negocio.
- El formulario de contacto abre WhatsApp con un mensaje preparado; no almacena solicitudes. El simulador de financiación es orientativo y no solicita ni confirma financiación.
- El panel actualiza precio, disponibilidad y descripción. Las altas nuevas y fotos se realizan por WhatsApp y requieren credenciales de Meta, webhook HTTPS y un número conectado.
- WhatsApp no deja usar un número que ya esté en la app normal: se usa un número nuevo (o una opción de migración/convivencia de Meta; comprueba las condiciones vigentes).
- Los trabajadores usan su WhatsApp personal o de empresa; solo escriben a ese número.
- `alternativa_telegram.py` es la misma idea en Telegram (sin Instagram ni sesiones en paralelo reforzadas).
