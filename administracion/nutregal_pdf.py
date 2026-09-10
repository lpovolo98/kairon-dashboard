"""Strict text reader for Alimentos Argentinos NU invoices."""
import io
import re
from datetime import datetime
from pypdf import PdfReader


def parse(text):
    def match(pattern):
        found = re.search(pattern, text, re.M)
        if not found:
            raise ValueError('La factura de Alimentos Argentinos NU requiere revisión: dato o columna no reconocido')
        return found.group(1).strip()
    def number(value):
        return float(value.replace(',', '.'))
    def day(value, fmt='%d/%m/%Y'):
        return datetime.strptime(value, fmt).date().isoformat()
    match(r'^\s*(A)\s+FACTURA\s*$')
    match(r'COD\.AFIP:(1)\s*$')
    match(r'\b(1\s*/\s*1)\s*$')
    cuits = re.findall(r'CUIT:\s*([\d-]+)', text)
    if len(cuits) != 2 or cuits[0] != '30-71849589-6':
        raise ValueError('No se identificaron inequívocamente los CUIT emisor y receptor')
    row_pattern = r'^\s*(\d{9})\s+(.+?)\s{2,}(\d+)\s+x\s+(\d+)\s+(21)\s+(\d+,\d{2,3})\s+(\d+,\d{2})\s*$'
    rows = re.findall(row_pattern, text, re.M)
    body = text.split('SUBTOTAL', 1)[1].split('Horario:', 1)[0]
    printed = [line for line in body.splitlines() if line.strip()]
    if not rows or len(rows) != len(printed):
        raise ValueError('Hay líneas, descuentos o impuestos internos no reconocidos en la factura NU')
    totals = match(r'^\s*(\d+,\d{2}\s+\d+,\d{2})\s*$').split()
    lines = [{'codigo_proveedor':code, 'descripcion':' '.join(desc.split()),
              'bultos':int(boxes), 'unidades_por_bulto':int(pack),
              'cantidad_unidades':int(boxes)*int(pack), 'precio_unitario':number(price),
              'bonificacion_pct':0, 'alicuota_iva':21, 'subtotal':number(total)}
             for code,desc,boxes,pack,vat,price,total in rows]
    return {'comprador_cuit':cuits[1],
            'proveedor':{'nombre':match(r'^\s*(ALIMENTOS ARGENTINOS NU)\s+'), 'cuit':cuits[0],
                         'inicio_actividades':day(match(r'INICIO DE ACT:(\d{2}/\d{2}/\d{4})'))},
            'comprobante':{'clase':'fiscal','tipo':'FACTURA','letra':'A',
                'numero':match(r'^\s*(\d{4}-\d{8})\s+'),
                'fecha':day(match(r'FECHA:(\d{2}/\d{2}/\d{4})')),
                'condicion_venta':match(r'Condicion de Venta:\s*(.*?)\s{2,}Rep:'),
                'cae':match(r'Numero CAE:\s*(\d{14})'),
                'cae_vencimiento':day(match(r'Vencimiento:\s*(\d{8})'),'%Y%m%d'), 'moneda':'ARS'},
            'lineas':lines, 'totales':{'neto':number(totals[0]),
                'iva':number(match(r'IVA 21%\s+(\d+\.\d{2})')),
                'percepciones':[{'detalle':'PERC. IVA RG 5329', 'alicuota':3,
                    'importe':number(match(r'PERC\. IVA RG 5329 3%:\s*(\d+\.\d{2})'))}],
                'total':number(totals[1])}}


def read(pdf):
    pages = [page.extract_text(extraction_mode='layout') or '' for page in PdfReader(io.BytesIO(pdf)).pages]
    if not any('ALIMENTOS ARGENTINOS NU' in p and '30-71849589-6' in p for p in pages):
        return None
    if len(pages) != 1:
        raise ValueError('Las facturas NU de varias páginas requieren revisión')
    return parse(pages[0])
