# PecadosVIP — Bot AutoReply

## Pruebas sin servicios externos

En un checkout limpio, instala `requirements.txt`, `pytest>=9,<10` y las
dependencias Node mediante `npm ci --ignore-scripts --include=optional`.
Ejecuta `python scripts/run_offline_tests.py` y
`node --import ./scripts/offline_node_guard.mjs --test tests/*.test.mjs`.
Los runners bloquean conexiones con proveedores y el inicio de workers;
no deben ejecutarse sobre una carpeta con sesiones productivas. GitHub Actions
ejecuta estas comprobaciones con Python 3.12 y Node 20, sin secretos de despliegue.

## Logotipo de PecadosVIP al vincular cuentas

El panel muestra `assets/pecadosvip-profile-logo.png`, el logotipo oficial existente
(SHA-256 `0413728e94269b3e433fa46ea014a75d7afbb6cbe63f3dc551346b7d3ea2f3c8`),
y avisa de su aplicación antes de vincular. No genera una imagen nueva.

Telegram aplica la foto a la **cuenta de usuario MTProto** confirmada por código,
QR o 2FA; el flujo de cambio de cuenta espera a que termine la promoción de la
cuenta candidata. No modifica el bot de BotFather. Reiniciar el worker, recuperar
una sesión ya autorizada o consultar el panel no crea solicitudes de foto.
El worker exige que el teléfono de su sesión coincida con la solicitud y verifica
que la primera foto devuelta por Telegram tenga el ID de la foto recién subida.
La petición caduca en diez minutos. Un resultado incierto no se reintenta solo.
Cada vinculación nueva solicita otra aplicación, aunque exista un éxito anterior
para esa cuenta y logo. Una solicitud pendiente no se duplica. Si el arranque
del worker falla tras verificar una cuenta nueva, se conserva la solicitud durante
sus diez minutos de validez para que pueda consumirla al recuperarse. El estado
muestra la última operación comprobada; no vigila cambios manuales posteriores.

WhatsApp solicita la foto únicamente después de promover y comprobar en línea
una cuenta vinculada desde este panel. La petición identifica PecadosVIP, la
cuenta y el hash del logo y caduca en diez minutos. Abrir una sesión existente
no crea esa petición; los candidatos `WA_LINK_ONLY` tampoco cambian la foto.
El estado se guarda por cuenta, hash del logo e identificador de solicitud. Una
vinculación nueva puede reaplicar el logo; una reconexión de la misma solicitud
no repite una actualización confirmada. Una confirmación significa que
WhatsApp aceptó la operación; no es una
comparación visual de la imagen remota. Una desconexión o tiempo de espera agotado
se muestra como incierto y no provoca un reintento automático. Se conserva la
espera por límites de WhatsApp.

Los estados aparecen debajo de cada cuenta en **Configurar** y se consultan con
el estado de los canales. Un fallo de foto no deshace la vinculación. Los errores
no exponen teléfonos, credenciales, rutas locales ni respuestas crudas del proveedor.
`TG_PROFILE_PICTURE_ENABLED=0` y `WA_PROFILE_PICTURE_ENABLED=0` desactivan la
aplicación respectiva. Los overrides `TG_PROFILE_PICTURE_PATH` y
`WA_PROFILE_PICTURE_PATH` solo se aceptan si sus bytes coinciden con el asset
oficial; una ruta antigua a Las Fiesteras queda bloqueada y se muestra el error.

La publicación requiere desplegar esta revisión del panel y ambos workers,
incluyendo el asset oficial, con las sesiones y datos persistentes existentes.
Antes de publicar, revisar los overrides de imagen y flags de desactivación.
No eliminar los archivos de estado para forzar reintentos: primero verificar el
resultado remoto cuando el estado sea incierto. La comprobación final requiere
una vinculación autorizada con cada proveedor; las pruebas locales usan clientes
simulados y no cambian perfiles reales.

## Vídeos y audios opcionales en Telegram

En la cuenta Telegram MTProto vinculada y en WhatsApp, las respuestas con vídeo
configurado intentan los componentes en este orden: **vídeo → texto → audio**.
Se espera el resultado del intento de vídeo antes de iniciar el texto. El audio
opcional se conserva, pero no se antepone al vídeo. Sin vídeo, los pasos mantienen
texto → audio y las llamadas audio → texto.

Un vídeo ausente o un envío fallido no se presenta como entrega completa: se
informa del fallo y se intentan los demás componentes configurados. Telegram
conserva los identificadores por evento/componente al reintentar una entrega
parcial. WhatsApp conserva la deduplicación del evento y no reintenta
automáticamente envíos inciertos; procesar un evento no acredita su entrega.
Si se agota el tiempo de espera sin confirmación del proveedor, el orden de los
intentos queda controlado, pero no puede garantizarse el orden de llegada remoto.
El diagnóstico de llamadas WhatsApp en `/api/wa_call_health` distingue el resumen
`pipeline.delivery` y la entrega `pipeline.video_delivery` del indicador `video`
de videollamada entrante. Este cambio cubre Telegram MTProto vinculado al panel;
el bot opcional de BotFather conserva su flujo independiente.

Un campo `audio: ""` omite el audio y permite enviar el texto y el vídeo del paso
o llamada. Los campos ausentes o inválidos conservan sus valores predeterminados.
No se borran nombres de audio existentes: si se configuró un archivo que falta,
se informa del error y se intentan los demás componentes. La interacción solo se
confirma al completar todos los componentes configurados; mientras no se repare
el archivo faltante, una interacción nueva puede volver a utilizar ese paso.

La subida de medios tiene un presupuesto separado del envío MTProto. Los
reintentos conservan el identificador del evento y componente para evitar
duplicados; agotar el tiempo de envío deja un resultado incierto, nunca un éxito
inventado. Los guardados de mensajes y medios del panel se serializan para
conservar referencias de vídeo ante operaciones simultáneas en los cuatro hilos
del único proceso Gunicorn declarado en `entrypoint.sh`. Esto no coordina varios
procesos o instancias que compartan datos; esas instancias deben seguir aisladas.

## Separación de instancias y marcas

Este repositorio corresponde a PecadosVIP. Sus defaults, portada de audio y
logotipo de perfil pertenecen a esa marca. La portada JPEG deriva del PNG
oficial existente; su procedencia está en `assets/README.md`. Las etiquetas y
dos imágenes heredadas conocidas de Las Fiesteras no se reutilizan, incluso si
permanecen en overrides. Otras personalizaciones se conservan.

El panel fija las rutas del worker WhatsApp a su propio directorio de datos.
El worker valida esas rutas antes de abrir la sesión: rechaza `BOT_DIR` de otro
checkout, escapes de rutas y enlaces a carpetas externas. Telegram comprueba
que la identidad real de la sesión coincida con el teléfono indicado, tanto al
vincular como antes de procesar interacciones. El control de procesos comprueba
el script y la instancia a la que pertenece un PID antes de detenerlo.
El arranque del contenedor valida las rutas antes de copiar defaults o leer
configuración persistente; los logs de los workers permanecen en el `data` de
esa instancia, sin compartir nombres globales en `/tmp`.

Estas comprobaciones no demuestran que los montajes de producción sean
independientes. Cada instancia debe tener su propio directorio/volumen `data`,
sesiones, configuración y procesos; copiar el mismo volumen entre marcas no es
una forma de vincular cuentas. Tampoco se deduce la marca de una cuenta solo de
su nombre público: el operador debe seleccionar la cuenta correcta al vincular.

El único workflow versionado ejecuta pruebas. GitHub no contiene una definición
de despliegue ni registros visibles que acrediten los montajes o servicios.
`deploy.sh` es un script legado externo, con `/opt/bot-autoreply` y nombres
systemd genéricos: no proporciona aislamiento entre marcas y no debe usarse
para publicar esta revisión. Su reemplazo por una publicación específica de
PecadosVIP requiere revisar el destino y autorizar ese alcance por separado.

## Vincular Telegram mediante QR

En **Configurar → Telegram (User Bot)**, completa `api_id`, `api_hash` y el
teléfono internacional de la cuenta que quieres vincular. Pulsa **Vincular con
QR**. En Telegram móvil, selecciona esa cuenta y abre **Ajustes → Dispositivos →
Vincular dispositivo**; escanea y confirma el QR. Si Telegram pide la contraseña
de verificación en dos pasos, introdúcela en el panel.

El QR se genera en el servidor y se renueva automáticamente. El intento dura
hasta diez minutos; puede cancelarse desde la pestaña que lo inició. Una cuenta
distinta de la indicada se rechaza. Las credenciales sólo se guardan y el servicio
se inicia después de confirmar la cuenta. Las cuentas ya vinculadas conservan su
flujo de cambio de cuenta por código.

La imagen se genera en memoria y no se envía a servicios externos. Después de
actualizar el código, instala `requirements.txt` y reinicia el panel para mostrar
la nueva opción. La confirmación final requiere tu aplicación de Telegram;
las pruebas automatizadas usan cuentas y respuestas simuladas.

## Adaptación de idioma

El bot evalúa localmente evidencia Unicode para español, inglés y francés. El
idioma sugerido por el prefijo de WhatsApp —o español en Telegram cuando aún no
hay texto— es sólo provisional. El primer texto identificable lo corrige de
inmediato, incluso si es breve, como `Hi` o `How much?`. Una señal débil mantiene
el nuevo idioma provisional hasta confirmarlo. Si el idioma anterior ya estaba
confirmado, era heredado o lo había indicado el operador, cambiarlo sigue
requiriendo evidencia fuerte, una petición expresa o dos mensajes débiles
consecutivos. El vocabulario cubre consultas como `Are you free tonight?` y
`Looking for a girl tonight`. Los textos ambiguos no fuerzan un cambio. El
detector es conservador y no pretende acertar en todos los textos breves o mezclados.

El estado persistente guarda únicamente el idioma, su origen y, cuando aplica,
un candidato con su contador. No guarda el texto, los tokens ni las reglas que
coincidieron.
