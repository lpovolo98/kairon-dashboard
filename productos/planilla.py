"""Lectura de la plantilla y generación de la plantilla en blanco.

La plantilla vigente —tres hojas: Productos, Proveedores, Precios— se lee tal
cual, sin pedirle al usuario que la cambie. La que se descarga se arma desde
el esquema y trae los valores válidos de Odoo adentro, que es lo que hacía
descubrir_catalogo.py.
"""

import csv
import io

from .esquema import HOJAS, columna_clave

TOPE_BYTES = 8 * 1024 * 1024
TOPE_FILAS = 5000

# Los nombres de hoja se comparan sin distinguir mayúsculas ni acentos.
ALIAS = {"productos": "productos", "proveedores": "proveedores", "precios": "precios",
         "listas": "precios", "lista de precios": "precios"}


def _normalizar(s):
    import unicodedata
    return unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().strip().lower()


def _filas_de_hoja(ws, clave):
    encabezados = [c.value for c in ws[1]] if ws.max_row else []
    if not any(encabezados):
        return []
    filas = []
    for cruda in ws.iter_rows(min_row=2, values_only=True):
        if all(v is None for v in cruda):
            continue
        registro = {h: v for h, v in zip(encabezados, cruda) if h}
        # Igual que el script original: una fila sin la columna clave es un
        # resto de formato, no un dato.
        if not registro.get(clave):
            continue
        filas.append({k: ("" if v is None else v) for k, v in registro.items()})
        if len(filas) > TOPE_FILAS:
            raise ValueError(f"La hoja «{ws.title}» tiene más de {TOPE_FILAS} filas.")
    return filas


def leer_xlsx(datos):
    """Devuelve {'productos': [...], 'proveedores': [...], 'precios': [...]}."""
    if len(datos) > TOPE_BYTES:
        raise ValueError(f"El archivo pesa {len(datos) // 1024 // 1024} MB y el tope es "
                         f"{TOPE_BYTES // 1024 // 1024} MB.")
    try:
        from openpyxl import load_workbook
    except ImportError:  # pragma: no cover
        raise ValueError("Falta openpyxl en el servidor para leer archivos .xlsx.")
    try:
        wb = load_workbook(io.BytesIO(datos), data_only=True, read_only=True)
    except Exception:
        raise ValueError("No se pudo abrir el archivo. ¿Es un .xlsx válido y sin contraseña?")

    salida = {h: [] for h in HOJAS}
    reconocidas = 0
    for ws in wb.worksheets:
        destino = ALIAS.get(_normalizar(ws.title))
        if not destino:
            continue
        salida[destino] = _filas_de_hoja(ws, columna_clave(destino))
        reconocidas += 1

    # Un archivo de una sola hoja con otro nombre se toma como Productos: es
    # lo que pasa cuando alguien exporta una selección desde Excel.
    if not reconocidas and wb.worksheets:
        salida["productos"] = _filas_de_hoja(wb.worksheets[0], columna_clave("productos"))

    if not any(salida.values()):
        raise ValueError("No encontré filas. Revisá que la primera fila sean los encabezados.")
    return salida


def leer_csv(datos):
    texto = datos.decode("utf-8-sig", errors="replace")
    muestra = texto[:4096]
    try:
        dialecto = csv.Sniffer().sniff(muestra, delimiters=",;\t")
    except csv.Error:
        dialecto = csv.excel
    filas = [dict(f) for f in csv.DictReader(io.StringIO(texto), dialect=dialecto)]
    clave = columna_clave("productos")
    filas = [f for f in filas if str(f.get(clave) or "").strip()][:TOPE_FILAS]
    if not filas:
        raise ValueError(f"No encontré filas con la columna «{clave}».")
    return {"productos": filas, "proveedores": [], "precios": []}


def leer(nombre, datos):
    if (nombre or "").lower().endswith(".csv"):
        return leer_csv(datos)
    return leer_xlsx(datos)


def generar_plantilla(catalogo):
    """Plantilla en blanco con los encabezados de cada hoja y, aparte, los
    valores que Odoo acepta hoy en cada desplegable."""
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill
    except ImportError:  # pragma: no cover
        raise ValueError("Falta openpyxl en el servidor para generar la plantilla.")

    wb = Workbook()
    wb.remove(wb.active)
    encabezado = Font(bold=True, color="FFFFFF")
    fondo = PatternFill("solid", fgColor="1F2F28")

    for hoja, conf in HOJAS.items():
        ws = wb.create_sheet(hoja.capitalize())
        columnas = [c["col"] for c in conf["columnas"]]
        ws.append(columnas)
        for i, texto in enumerate(columnas, start=1):
            celda = ws.cell(row=1, column=i)
            celda.font, celda.fill = encabezado, fondo
            ws.column_dimensions[celda.column_letter].width = max(14, min(34, len(texto) + 4))
        ws.freeze_panes = "A2"

    ws = wb.create_sheet("Valores válidos")
    ws.append(["Campo", "Valor que acepta Odoo"])
    for c in ws[1]:
        c.font, c.fill = encabezado, fondo
    ws.column_dimensions["A"].width = 22
    ws.column_dimensions["B"].width = 46
    etiquetas = {"categorias": "Categoría", "unidades": "Unidad",
                 "impuestos_venta": "Impuestos de venta", "impuestos_compra": "Impuestos de compra",
                 "listas": "Lista de precios", "proveedores": "Proveedor"}
    for clave, etiqueta in etiquetas.items():
        for item in catalogo.get(clave, []):
            ws.append([etiqueta, item["name"]])
    ws.freeze_panes = "A2"

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()
