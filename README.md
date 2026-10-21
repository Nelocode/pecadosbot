# Bot AutoReply Madrid

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
Las aplicaciones confirmadas se deduplican por cuenta y logo. El estado muestra
la última aplicación comprobada; no vigila cambios manuales posteriores del perfil.

WhatsApp conserva la aplicación al abrir la conexión principal: los candidatos
en modo `WA_LINK_ONLY` no cambian la foto. El estado se guarda por cuenta y hash
del logo. Una confirmación significa que WhatsApp aceptó la operación; no es una
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
