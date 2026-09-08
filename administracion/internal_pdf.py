"""Read the reviewed internal layout; an anonymous issuer is never guessed."""
import hashlib
import io
import re
from datetime import datetime
from decimal import Decimal
from pypdf import PdfReader

# The issuer is absent from this document. Confirmation applies only to its hash.
APPROVED = {'33b45393883e03c86d87525badd41b0aeeabb779989d1120fcbddac35f03d5b2'}
ALIASES = {'Snacks Caja x 12 - Sesamo': '7798371600013',
           'Snacks Caja x 12 - Tomate y Ro': '7798371600020'}

def read(pdf):
    if hashlib.sha256(pdf).hexdigest() not in APPROVED:
        return None
    texts = [p.extract_text(extraction_mode='layout') or '' for p in PdfReader(io.BytesIO(pdf)).pages]
    if len(texts) != 1:
        raise ValueError('El presupuesto requiere revisar sus páginas')
    text = texts[0]
    def find(pattern):
        match = re.search(pattern, text, re.M)
        if not match: raise ValueError('Falta un dato del presupuesto interno')
        return match.group(1)
    def number(value): return Decimal(value.replace('.', '').replace(',', '.'))
    rows = re.findall(r'^\s*(\d+\.\d{5})\s+(.+?)\s{2,}([\d.]+,\d{2})\s+([\d.]+,\d{2})\s*$', text, re.M)
    if len(rows) != 2: raise ValueError('No se reconocieron los dos artículos del presupuesto')
    lines = []
    for q, description, p, amount in rows:
        description = ' '.join(description.split())
        code = ALIASES.get(description)
        if not code: raise ValueError('Falta equivalencia de producto: ' + description)
        boxes, price, subtotal = Decimal(q), number(p), number(amount)
        if boxes * price != subtotal: raise ValueError('Cantidad por precio no coincide en el presupuesto')
        lines.append({'codigo_proveedor': code, 'descripcion': description,
                      'cantidad': float(boxes * 12), 'bultos': float(boxes), 'unidades_por_bulto':12,
                      'precio_unitario':float(price / 12), 'alicuota_iva':0, 'subtotal':float(subtotal)})
    total = number(find(r'^\s*TOTAL\s+([\d.]+,\d{2})\s*$'))
    net = number(find(r'^\s*SUBTOTAL\s+([\d.]+,\d{2})\s*$'))
    if sum(Decimal(str(line['subtotal'])) for line in lines) != net or total != net:
        raise ValueError('Los importes del presupuesto no coinciden')
    return {'comprador_cuit':find(r'(30-71912311-9)'),
            'proveedor':{'nombre':'Alimentos Almadre', 'cuit':'30716639009'},
            'comprobante':{'clase':'interno','tipo':'PRESUPUESTO','letra':'X',
                'numero':find(r'\b(\d{5}-\d{8})\b'),
                'fecha':datetime.strptime(find(r'\b(\d{2}/\d{2}/\d{4})\b'),'%d/%m/%Y').date().isoformat(), 'moneda':'ARS'},
            'lineas':lines,'totales':{'neto':float(net),'iva':0,'total':float(total),'percepciones':[]},
            'observaciones_carga':'Proveedor Almadre confirmado por el usuario; CUIT obtenido de su ficha existente en Odoo, no impreso en PDF. Cantidades confirmadas: 4 cajas de 12 de cada sabor, equivalentes a 48 unidades por artículo. Precios convertidos de caja a unidad. Comprobante sin validez fiscal.'}
