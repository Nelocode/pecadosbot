# Recursos de marca PecadosVIP

`pecadosvip-profile-logo.png` es el logotipo oficial existente: manzana dorada,
256 × 256. Su SHA-256 es
`0413728e94269b3e433fa46ea014a75d7afbb6cbe63f3dc551346b7d3ea2f3c8`.

`audio-cover.jpg` es una conversión técnica de ese PNG para la miniatura de audio
de Telegram: mismo dibujo, fondo negro para componer la transparencia, JPEG de
256 × 256 y 10.629 bytes. No se generó ni redibujó el logotipo. Su SHA-256 es
`ed668aa3761d2d782a95469cede220767b5ee8241b89dbf69e1a484e656d5696`.

Conversión realizada con FFmpeg 8.1:

```sh
ffmpeg -v error -f lavfi -i 'color=c=black:s=256x256' \
  -i assets/pecadosvip-profile-logo.png \
  -filter_complex '[0:v][1:v]overlay=format=auto,format=yuvj420p' \
  -frames:v 1 -q:v 4 -map_metadata -1 -y assets/audio-cover.jpg
```

Se sustituyó la portada heredada y se retiró el logo de perfil heredado sin
referencias de código. El lector de configuración rechaza por hash esas dos
imágenes antiguas incluso si aparecen con otro nombre en un override externo;
no borra ni modifica archivos de configuración persistentes.
