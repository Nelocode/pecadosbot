# Bot AutoReply Madrid

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
