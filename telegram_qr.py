"""Renderiza el reto QR en memoria, sin enviar el token a terceros."""

import base64
import io
import re

import qrcode
from qrcode.image.svg import SvgPathFillImage


def qr_image_data_url(url: str) -> str:
    if not re.fullmatch(r"tg://login\?token=[A-Za-z0-9_-]{20,512}={0,2}", url):
        raise ValueError("Reto QR no válido")
    qr = qrcode.QRCode(border=4, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(url)
    qr.make(fit=True)
    output = io.BytesIO()
    qr.make_image(image_factory=SvgPathFillImage).save(output)
    return "data:image/svg+xml;base64," + base64.b64encode(output.getvalue()).decode("ascii")
