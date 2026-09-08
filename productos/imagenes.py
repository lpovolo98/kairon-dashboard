"""Carga de fotos de producto.

El achicado ocurre en el navegador, antes de subir: acá llega una imagen ya
redimensionada. El tope es una red de contención por si alguien llama a la
API sin pasar por la pantalla, no el mecanismo principal de control.
"""

import base64
import binascii
import re

from .cargar import buscar_producto_por_sku

TOPE_BYTES = 4 * 1024 * 1024      # el cliente manda ~300 KB; 4 MB es holgado
FIRMAS = {b"\xff\xd8\xff": "jpeg", b"\x89PNG\r\n\x1a\n": "png", b"RIFF": "webp"}
_DATA_URL = re.compile(r"^data:image/[a-z+]+;base64,", re.I)


def decodificar(imagen):
    """Acepta un data URL del navegador o base64 pelado. Valida que sea una
    imagen de verdad: el contenido manda, no el nombre ni el tipo declarado."""
    if not isinstance(imagen, str) or not imagen:
        raise ValueError("Imagen vacía.")
    limpio = _DATA_URL.sub("", imagen).strip()
    try:
        datos = base64.b64decode(limpio, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("La imagen no está codificada en base64.")
    if not datos:
        raise ValueError("Imagen vacía.")
    if len(datos) > TOPE_BYTES:
        raise ValueError(f"La imagen pesa {len(datos) // 1024} KB y el tope es {TOPE_BYTES // 1024} KB.")
    if not any(datos.startswith(f) for f in FIRMAS):
        raise ValueError("El archivo no es una imagen JPG, PNG ni WEBP.")
    return limpio, datos


def sin_imagen(odoo, limite=500):
    """Productos activos que todavía no tienen foto. Reemplaza al diccionario
    SIN_IMAGEN que había que mantener a mano."""
    filas = odoo.call("product.template", "search_read",
                      [[["image_1920", "=", False], ["default_code", "!=", False]]],
                      {"fields": ["default_code", "name"], "limit": limite, "order": "default_code"})
    return [{"sku": (f.get("default_code") or "").strip(), "nombre": f.get("name") or ""} for f in filas]


def aplicar(odoo, asignaciones):
    """asignaciones: [{'sku': ..., 'imagen': <data url o base64>}]"""
    subidas, errores, deshacer = [], [], {"modificados": []}
    for a in asignaciones:
        sku = str(a.get("sku") or "").strip()
        if not sku:
            continue
        try:
            b64, _ = decodificar(a.get("imagen"))
        except ValueError as e:
            errores.append(f"{sku}: {e}")
            continue

        tmpl = buscar_producto_por_sku(odoo, sku, errores)
        if not tmpl:
            errores.append(f"No hay ningún producto con la Referencia interna «{sku}».")
            continue

        # La imagen anterior se guarda para poder revertir: pisar una foto
        # buena por error es el accidente más probable de esta pantalla.
        previo = odoo.call("product.template", "read", [[tmpl]], {"fields": ["image_1920"]})
        anterior = (previo[0].get("image_1920") if previo else False) or False

        odoo.call("product.template", "write", [[tmpl], {"image_1920": b64}])
        deshacer["modificados"].append(
            {"modelo": "product.template", "id": tmpl, "valores": {"image_1920": anterior}})
        subidas.append({"sku": sku, "id": tmpl, "pisaba_una_foto": bool(anterior)})

    return {"subidas": subidas, "errores": errores, "deshacer": deshacer}
