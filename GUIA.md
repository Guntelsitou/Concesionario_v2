# Guía de funcionamiento y puesta en producción

Esta guía explica qué hace cada parte del concesionario, qué datos hay que preparar y cómo pasar de la demostración a una instalación real. Está escrita para este repositorio y sus nombres de variables actuales.

> **Estado actual:** la web y el flujo de WhatsApp están preparados como base de trabajo, pero el contenido de la web es de demostración. No publiques la página ni conectes un número real hasta sustituir los datos falsos, configurar los secretos y probar el proceso completo.

## 1. Qué incluye el proyecto

### Web pública — `web/index.html`

Es el catálogo que visitan los clientes. Lee el inventario, muestra fichas de vehículos, filtros, favoritos locales, calculadora orientativa de financiación y botones para preguntar por WhatsApp. El formulario de contacto **no guarda solicitudes**: prepara un mensaje y abre WhatsApp en el dispositivo del visitante.

Los datos generales del concesionario están escritos en el propio HTML, en el objeto `NEGOCIO`: nombre, teléfono, WhatsApp, correo, dirección, horario, enlace al mapa e Instagram. También hay textos comerciales fijos en la página. No basta con cambiar el `.env` del bot para cambiar todos esos datos.

### Inventario — `web/vehiculos.json`

Es el catálogo público actual y la fuente común que leen la web y el bot. Cada coche tiene una referencia, datos técnicos, precio, descripción, estado y una lista de rutas de fotos. Las matrículas **no** deben guardarse aquí: el bot las conserva en `bot/data/privado.json`.

El inventario es un fichero JSON, no una base de datos. El backend hace copias antes de editarlo. Esta arquitectura sirve para una instalación pequeña de una sola instancia; no se debe ejecutar con varias réplicas o varios procesos que escriban a la vez.

### Backend — `bot/whatsapp_bot.py`

Aunque su nombre menciona WhatsApp, también es el servidor de la web. Sus rutas principales son:

| Ruta | Para qué sirve | Acceso |
|---|---|---|
| `/` | Página pública del catálogo | Público |
| `/admin.html` | Panel para editar coches publicados | Público para abrir; se necesita token para guardar |
| `GET /api/vehiculos` | Entregar el inventario a la web y al panel | Público; solo datos públicos |
| `PATCH /api/vehiculos/{ref}` | Cambiar precio, estado o descripción | Token de administrador obligatorio |
| `GET /webhook` | Verificar el webhook con Meta | Meta |
| `POST /webhook` | Recibir mensajes y eventos de WhatsApp | Meta; exige firma válida |
| `GET /health` | Revisar qué partes están configuradas | Público; no revela secretos |

El panel edita coches existentes. **Las altas nuevas y las fotos se incorporan con el flujo del bot de WhatsApp.** Si `GIT_PUSH=true`, los cambios del bot y del panel intentan publicarse mediante Git; eso requiere un clon del repositorio con credenciales de escritura. Si el backend sirve la web directamente, los cambios guardados aparecen en ese mismo servidor sin un despliegue Git separado.

### Bot de WhatsApp — flujo en `bot/whatsapp_bot.py`

Un trabajador autorizado inicia una conversación con el número del negocio, envía las fotos y contesta el cuestionario. El bot guarda las fotos, pide la información del vehículo, presenta un resumen y publica el anuncio. También permite listar vehículos y cambiar disponibilidad o precio mediante mensajes. Las matrículas se mantienen en el fichero privado operativo.

El bot procesa los mensajes de cada trabajador en orden y protege las escrituras del inventario con cerrojos en memoria. **Debe ejecutarse con un solo proceso y una sola réplica.**

WhatsApp solo entrega mensajes al webhook cuando el backend es accesible desde Internet por HTTPS y el webhook está registrado en Meta. Para recibir una consulta nueva, el cliente escribe primero al negocio; este proyecto no contiene una campaña de mensajes salientes.

### Instagram opcional — `bot/instagram.py`

Si se configura una cuenta profesional y las credenciales, el bot puede preparar y publicar un coche en Instagram. La publicación necesita fotos accesibles públicamente desde `WEB_URL`; por eso se debe tener resuelto dónde se sirve la web y, si se usa una web estática separada, el proceso de subida de fotos.

### Alternativa Telegram — `bot/alternativa_telegram.py`

Es otro bot opcional, no el servidor de la web. Requiere sus dependencias y configuración propias. No hace falta instalarlo para usar la web o WhatsApp.

## 2. Qué significa «modo demo» en este proyecto

Hay más de una cosa de demostración:

1. En `web/index.html`, `MODO_DEMO` aparece como `true`. Al activarlo, el código puede recurrir al array `DEMO` si falla la carga del inventario. En este repositorio `DEMO` está vacío, así que no es un catálogo alternativo completo.
2. La web usa datos comerciales de ejemplo en el bloque `NEGOCIO`; incluye un correo terminado en `.example`, un teléfono ficticio, una dirección de ejemplo y un enlace de mapa genérico.
3. `web/vehiculos.json` contiene vehículos de ejemplo. Sus fichas no deben presentarse como coches reales disponibles sin comprobarlos y reemplazarlos.
4. La página indica que los datos e imágenes son de demostración. Las fichas usan ilustraciones si `fotos` está vacío.
5. En las fichas se muestra **«Garantía: 12 meses» como texto fijo**. El valor `GARANTIA_TXT` del `.env` sí se usa en los textos del bot/Instagram, pero no modifica automáticamente este campo de la web.
6. El simulador de cuotas es una estimación matemática y no está conectado a una entidad financiera. El botón de contacto abre WhatsApp; no existe una bandeja de solicitudes ni CRM.

### Pasos para quitar la presentación de demostración

1. Reemplaza `web/vehiculos.json` por inventario real y revisado. Verifica referencias, precio, kilometraje, estado, equipamiento, descripción y fotos de cada coche.
2. Pon fotos reales en `web/fotos/` y enlázalas en la propiedad `fotos` de cada coche. Comprueba que las rutas se pueden abrir desde la página publicada.
3. Edita el objeto `NEGOCIO` en `web/index.html` con la identidad y vías de contacto reales. El WhatsApp se escribe con prefijo de país y dígitos, sin `+` ni espacios (por ejemplo, `34...`).
4. Cambia en el HTML los textos de demostración, el texto de garantía fijo, horario, correo, dirección, enlaces y cualquier promesa comercial que no corresponda exactamente al negocio.
5. Cambia `MODO_DEMO = true` a `MODO_DEMO = false`. La API y el fichero de inventario siguen siendo la fuente de datos del catálogo; el modo demo solo controla el respaldo del array `DEMO`.
6. Configura `bot/.env` con valores reales. No copies los valores literales de `.env.example` a producción: son ejemplos o están vacíos.
7. Abre la web desde el servidor real y comprueba en `/health` que aparecen activos los componentes que vayas a utilizar. No basta con que la página abra: hay que probar una edición del panel y, si se activa WhatsApp, un intercambio real de webhook.

No añadas una garantía de 12 meses, financiación aprobada, disponibilidad o equipamiento por defecto. Publica únicamente condiciones confirmadas por el concesionario.

## 3. Configuración de `bot/.env`

Primero crea el fichero desde la plantilla:

```powershell
Copy-Item bot/.env.example bot/.env
```

`bot/.env` está excluido por Git. Guárdalo en el servidor como secreto, no lo subas al repositorio, no lo envíes por WhatsApp/correo y no pegues tokens en capturas ni en incidencias.

### WhatsApp y seguridad

| Variable | Qué es | De dónde sale / qué poner |
|---|---|---|
| `WA_TOKEN` | Token de acceso Bearer usado para llamar a WhatsApp Cloud API | En la app de Meta, producto WhatsApp → API Setup/Getting Started hay un token de usuario para empezar a probar. Para producción crea un token de usuario del sistema desde la configuración del portfolio empresarial y asigna los activos/permisos necesarios. El token temporal no sirve como secreto de producción. |
| `WA_PHONE_ID` | ID técnico del número de teléfono conectado a Cloud API | En WhatsApp → API Setup/Getting Started o WhatsApp Manager, asociado al número empresarial. No es el número escrito con prefijo. |
| `WA_VERIFY_TOKEN` | Frase secreta que eliges tú para verificar el webhook | Genera una cadena aleatoria larga, guárdala aquí y escribe exactamente la misma en la configuración del webhook de Meta. No es el token de acceso ni el App Secret. |
| `WA_APP_SECRET` | Secreto de la aplicación Meta para comprobar la firma HMAC de cada webhook | En Meta for Developers → tu aplicación → App settings/Configuración → Basic/Básica → App Secret. Meta puede pedir que vuelvas a autenticarte para mostrarlo. Este programa exige el secreto y rechaza mensajes sin firma válida. |
| `WORKERS` | Lista de números autorizados a usar el bot | La defines tú. Formato: `34611111111:Nombre,34622222222:Nombre`. Usa el móvil del trabajador con código de país y sin `+`, espacios ni guiones. Un número no incluido no podrá dar de alta ni modificar coches. |
| `WA_API_BASE` | Base de la Graph API de Meta | Normalmente se deja como viene en la plantilla. Si Meta publica una versión nueva compatible, actualízala deliberadamente y prueba el bot antes de desplegar. |

El usuario del sistema de Meta debe tener asignados la aplicación y la cuenta de WhatsApp correctas, además de permisos de mensajería y administración de WhatsApp que correspondan al uso. En el entorno de pruebas puede usarse el número de prueba de Meta y destinatarios permitidos por Meta; eso no equivale a tener el número real listo para clientes.

### Administración y archivos

| Variable | Qué es | Qué poner |
|---|---|---|
| `ADMIN_API_TOKEN` | Clave propia para guardar cambios desde `/admin.html` | Genera una contraseña aleatoria larga y única. No es ninguna credencial Meta. Sin ella las escrituras de la API devuelven error; la lectura pública del catálogo puede funcionar igual. |
| `WEB_DIR` | Carpeta con `index.html`, `vehiculos.json` y fotos | En Docker Compose ya se establece como `/app/web`. En ejecución local se puede omitir para usar la carpeta `web` del proyecto. |
| `DATA_DIR` | Carpeta para sesiones y datos privados operativos | En Docker Compose ya se establece como `/data` y se monta en un volumen persistente. En local, por defecto está en `bot/data/`. Incluye matrículas: limita quién puede leerla y guarda copias seguras. |
| `WEB_URL` | URL pública de la web | Pon la URL HTTPS definitiva, por ejemplo `https://www.tudominio.es`. Se usa en enlaces del bot y en el flujo de Instagram. |
| `GIT_PUSH` | Publicación automática del contenido modificado en el repositorio Git | `false` para guardar solo en el servidor que sirve la web. `true` solo si `web/` pertenece a un checkout Git del servidor con credenciales de push configuradas. No lo actives sin verificar el remoto y sus permisos. |

### Datos comerciales del bot

`NEGOCIO_NOMBRE`, `NEGOCIO_TEL`, `NEGOCIO_CIUDAD` y `GARANTIA_TXT` personalizan textos del bot y de Instagram. El teléfono va en formato humano según el texto que se vaya a publicar. **Estos valores no sustituyen el objeto `NEGOCIO` de `web/index.html` ni la garantía fija de la ficha:** actualiza ambos sitios.

### Integraciones opcionales

| Variable | Uso | Requisitos |
|---|---|---|
| `IG_TOKEN`, `IG_USER_ID`, `IG_API_BASE` | Publicar vehículos en Instagram | Cuenta profesional de Instagram (empresa o creador) y permisos/API de Instagram configurados en Meta. El ID es el ID de la cuenta, no el nombre `@usuario`. El token debe tener vigencia adecuada. Deja ambos campos de credenciales vacíos para mantener Instagram desactivado. |
| `ANTHROPIC_API_KEY`, `CLAUDE_MODEL` | Redacción asistida opcional de textos | Una clave de API de Anthropic con acceso y facturación habilitados. La clave se obtiene en la consola de Anthropic; se factura aparte. Sin clave, el bot usa la redacción local que incluye el código. |
| `AUTO_SEGUNDOS` | Tiempo de espera tras las fotos para iniciar las preguntas | Número de segundos; valor de partida `10`. |
| `SESION_HORAS` | Caducidad de una sesión de alta abandonada | Número de horas; valor de partida `12`. |

Los campos de Instagram no activan nada por sí solos: también hacen falta permisos válidos, una cuenta correctamente vinculada y que las fotos sean accesibles desde Internet para Meta. Revisa en el panel de Meta los permisos y el nivel de acceso disponible para tu app; el acceso necesario depende de si publicas solo en activos propios o en cuentas de terceros.

## 4. Dónde conseguir las credenciales de WhatsApp

Los nombres de menú de Meta cambian con el tiempo, pero el proceso general es:

1. Inicia sesión en [Meta for Developers](https://developers.facebook.com/) y crea una aplicación empresarial o selecciona la que pertenece al concesionario.
2. Añade el producto **WhatsApp** y sigue su configuración inicial. Para una prueba, Meta proporciona activos de prueba y muestra un token temporal y el ID del teléfono de prueba.
3. Para producción, configura un portfolio empresarial de Meta, una cuenta de WhatsApp Business (WABA) y el número del negocio que se utilizará con Cloud API. Completa la verificación empresarial y los pasos que Meta pida para ese portfolio/número.
4. En la configuración empresarial, crea un usuario del sistema y asígnale la aplicación y los activos de WhatsApp correctos. Genera un token con los permisos de WhatsApp que requiere este servicio (`whatsapp_business_messaging` y `whatsapp_business_management`). Revisa la caducidad/rotación en el panel y planifica cómo renovar el token.
5. Copia el **Phone Number ID** mostrado por Meta en `WA_PHONE_ID`; copia el token en `WA_TOKEN`. No confundas el ID con el teléfono que marcan los clientes.
6. En **App settings → Basic**, copia el App Secret y guárdalo en `WA_APP_SECRET`.
7. Crea un `WA_VERIFY_TOKEN` propio (cadena aleatoria que no use nadie más). Lo pondrás tanto en el entorno del servidor como en la pantalla del webhook.
8. Añade los trabajadores permitidos a `WORKERS` y guarda `bot/.env` en el entorno donde corre el servicio.

La colección oficial de Meta en Postman explica qué activos necesitas, dónde aparecen el token/IDs y los dos permisos de WhatsApp: [WhatsApp Cloud API — colección oficial de Meta](https://www.postman.com/meta/whatsapp-business-platform/collection/wlk6lh4/whatsapp-cloud-api). Meta indica allí que el token de usuario de prueba caduca a las 24 horas y describe el uso de tokens de usuario del sistema para una integración persistente. Consulta también la [documentación de WhatsApp Cloud API](https://developers.facebook.com/docs/whatsapp/cloud-api/overview) y el [inicio rápido](https://developers.facebook.com/docs/whatsapp/cloud-api/get-started).

### Registrar el webhook

1. Despliega el backend en un dominio público con certificado HTTPS. La dirección de callback es `https://TU-DOMINIO/webhook`.
2. En la configuración de Webhooks de la app de Meta, selecciona WhatsApp Business Account, pega esa URL y escribe el mismo `WA_VERIFY_TOKEN` que guardaste en `bot/.env`.
3. Meta hará una petición GET para verificarlo. Este backend devuelve el desafío solo si el token coincide.
4. Suscribe la app/cuenta al campo **messages**.
5. Meta enviará POST al mismo endpoint. El backend valida `X-Hub-Signature-256` con `WA_APP_SECRET`; una firma inválida se rechaza.
6. Comprueba `https://TU-DOMINIO/health`. Para WhatsApp deben aparecer activos `wa_token`, `wa_phone_id`, `wa_verify_token`, `wa_app_secret` y `trabajadores`. El estado puede seguir incompleto si otros campos que el endpoint considera necesarios faltan.
7. Manda un mensaje desde un teléfono autorizado y revisa los logs del servidor. Durante la configuración inicial usa los destinatarios de prueba que permita Meta.

La verificación del GET y la firma de POST son dos mecanismos distintos: `WA_VERIFY_TOKEN` no sustituye a `WA_APP_SECRET`. La [documentación del SDK de WhatsApp mantenido por Meta](https://whatsapp.github.io/WhatsApp-Nodejs-SDK/api-reference/webhooks/start/) describe ambas comprobaciones.

## 5. Pasos para pasar a producción

### A. Preparar el contenido

- Sustituir los datos y coches demo detallados en la sección 2.
- Revisar que cada foto, precio y descripción corresponde al vehículo real.
- Revisar enlaces, números, garantía, financiación y textos comerciales.
- Preparar páginas y textos legales de privacidad, cookies y aviso legal para el negocio. El proyecto no los genera ni garantiza que los textos de muestra sean adecuados.

### B. Preparar y arrancar el servidor

Requisitos del despliegue: Python 3.12/Docker, acceso persistente a los ficheros, una URL HTTPS pública para WhatsApp y un único proceso del bot. Con Docker Desktop instalado en Windows:

```powershell
Copy-Item bot/.env.example bot/.env
# Editar bot/.env y reemplazar cada valor de ejemplo
docker compose up --build -d
docker compose logs -f backend
```

La web queda en `http://localhost:8000/` y el panel en `http://localhost:8000/admin.html`. Esto sirve para revisar el servicio local. La dirección `localhost` no es pública y Meta no puede alcanzar el webhook; para eso se necesita el backend desplegado y HTTPS.

Para arrancar sin Docker, desde la raíz del repositorio:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
py -m pip install -r bot/requirements.txt
py -m uvicorn bot.whatsapp_bot:app --host 0.0.0.0 --port 8000
```

Si `py` no existe, instala Python 3.12 antes. Para WhatsApp de producción, deja el servidor ejecutándose con un gestor de procesos o despliega la imagen Docker en un servicio que mantenga el volumen persistente. No ejecutes `uvicorn` con `--workers` y no escales horizontalmente esta versión.

### C. Probar el panel de inventario

1. Define `ADMIN_API_TOKEN` en el entorno del backend y reinícialo.
2. Abre `/admin.html`, pega el token y conecta.
3. Cambia un precio/estado/descripción de un coche de prueba y guarda.
4. Revisa el catálogo público y el fichero actualizado. Si `GIT_PUSH=true`, revisa además los logs para confirmar que el push funcionó.
5. Revoca o cambia el token si se compartió accidentalmente.

No uses un token de ejemplo en el servidor público. Aunque el catálogo es público, las escrituras del panel alteran precios y disponibilidad.

### D. Habilitar WhatsApp real

Completa los pasos de las secciones 3 y 4 con el número empresarial real y credenciales de producción. Primero prueba en un número/entorno de prueba de Meta. Después configura el número real, instala el webhook de producción, activa HTTPS y confirma que `WORKERS` contiene solo a personal autorizado.

El backend puede recibir mensajes del trabajador y responder durante la conversación. Asegúrate de que el equipo entiende el flujo y de que el número utilizado no tiene un conflicto con el uso previo en WhatsApp. Revisa en Meta el estado, límites y precios aplicables a tu cuenta antes de operar; pueden cambiar.

### E. Habilitar publicación de cambios en una web estática

Si el catálogo está separado del backend en Cloudflare Pages/Netlify, el navegador lee `vehiculos.json` desde el host de la web. Para publicar automáticamente los cambios hechos en el servidor, `GIT_PUSH=true` necesita que:

- El servidor tenga un clon de Git válido.
- Ese clon contenga la carpeta `web/` que se publica.
- Git tenga un remoto y credenciales de escritura vigentes.
- El proveedor de web esté conectado a ese repositorio y despliegue los cambios.

Si no se cumple todo lo anterior, los cambios pueden guardarse en el servidor sin llegar a la web estática. En la configuración más sencilla, sirve web y backend desde el mismo host y no actives Git Push.

## 6. Instagram opcional

Para publicar coches desde el bot, se necesita una cuenta profesional de Instagram, permisos de publicación para la integración elegida por Meta, `IG_USER_ID` y un token válido en `IG_TOKEN`. La cuenta debe tener las vinculaciones que requiera el método de API configurado. El backend necesita que las imágenes estén en una URL pública HTTPS; `WEB_URL` debe apuntar a la web en producción.

Meta mantiene la guía y colección de solicitudes en [Instagram API — documentación oficial de Meta en Postman](https://www.postman.com/meta/instagram/documentation/6yqw8pt/instagram-api). La colección indica que la publicación requiere una cuenta profesional y describe el permiso/modelo actual. Comprueba el panel de Meta para saber si tu cuenta necesita revisión, acceso avanzado o tareas adicionales. Instagram es opcional: deja `IG_TOKEN` e `IG_USER_ID` vacíos si no lo vas a usar.

## 7. Qué hace falta conseguir fuera del código

- **Datos del negocio:** nombre legal/comercial, teléfono real, WhatsApp, correo, dirección, horario, mapa y garantías reales.
- **Inventario real:** coches disponibles, información comprobada, fotos y permisos para publicarlas.
- **Dominio y alojamiento:** dominio que controle el concesionario y backend con HTTPS permanente. HTTPS es imprescindible para registrar el webhook de producción.
- **Cuenta de Meta:** portfolio empresarial, app de desarrollador, WABA, número autorizado y token de larga duración apropiado.
- **Responsable de atención:** trabajadores autorizados, proceso para atender solicitudes y proceso para cambiar disponibilidad/precio.
- **Revisión comercial/legal:** condiciones de financiación/garantía, información de precios e impuestos, privacidad, cookies y aviso legal aplicables al negocio.
- **Instagram/Anthropic solo si se desean esas funciones:** son servicios externos separados; comprueba permisos, costes y expiración de credenciales.

## 8. Problemas habituales

| Síntoma | Causa probable / qué revisar |
|---|---|
| `GET /health` da `configuracion_incompleta` | Abre el JSON de `/health` y revisa los checks uno por uno. No contiene secretos. Comprueba variables, rutas y que existe `web/vehiculos.json`. |
| `POST /webhook` responde 503 | Falta `WA_APP_SECRET`; el código no permite procesar webhooks sin firma. |
| Meta no verifica el webhook | URL pública HTTPS, ruta `/webhook`, disponibilidad del servicio y coincidencia exacta de `WA_VERIFY_TOKEN`. |
| El bot recibe mensajes pero no responde | Token o Phone Number ID erróneo, permisos del token, teléfono remitente no incluido en `WORKERS`, token caducado o error de Meta. Revisa los logs. |
| No se puede guardar en `/admin.html` | Falta `ADMIN_API_TOKEN`, el token no coincide o se está abriendo el panel desde un host estático distinto al backend. |
| El cambio se guarda pero la web estática no cambia | `GIT_PUSH` está desactivado, no hay permisos de push, el proveedor no despliega ese repo o el backend y la web usan copias distintas de `web/`. |
| Instagram no publica | Revisa tipo de cuenta, permisos/acceso de Meta, vigencia del token, `IG_USER_ID`, `WEB_URL` e imágenes públicas HTTPS. |
| La matrícula aparece en la web | No incluyas la matrícula en la ficha pública. Revisa el inventario y limita el acceso a `bot/data/privado.json`; el bot usa ese fichero para guardarla por separado. |

## 9. Referencias oficiales

- [WhatsApp Cloud API: descripción](https://developers.facebook.com/docs/whatsapp/cloud-api/overview)
- [WhatsApp Cloud API: primeros pasos](https://developers.facebook.com/docs/whatsapp/cloud-api/get-started)
- [Colección de WhatsApp Cloud API mantenida por Meta en Postman](https://www.postman.com/meta/whatsapp-business-platform/collection/wlk6lh4/whatsapp-cloud-api)
- [Webhooks del SDK de WhatsApp mantenido por Meta](https://whatsapp.github.io/WhatsApp-Nodejs-SDK/api-reference/webhooks/start/)
- [Instagram API mantenida por Meta en Postman](https://www.postman.com/meta/instagram/documentation/6yqw8pt/instagram-api)

Los nombres de pantallas, permisos, políticas, costes y requisitos de Meta pueden cambiar. Cuando una indicación de esta guía y el panel/documentación vigente de Meta difieran, sigue los requisitos que Meta muestre para la cuenta del concesionario.
