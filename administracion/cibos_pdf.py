"""Strict reader for text-based Cibos A invoices; never supplies sample values."""
import io
import re
from datetime import datetime
from pypdf import PdfReader


def read(pdf):
    pages = [page.extract_text(extraction_mode='layout') or '' for page in PdfReader(io.BytesIO(pdf)).pages]
    if not pages or not all('CIBOS SRL' in text and '30-71615646-6' in text for text in pages):
        return None
    def parse(text):
        def match(pattern):
            value = re.search(pattern, text, re.M)
            if not value:
                raise ValueError('El formato de Cibos requiere revisión: falta un dato obligatorio')
            return value.group(1).strip()
        def money(value): return float(value.replace('.', '').replace(',', '.'))
        def date(value): return datetime.strptime(value, '%d/%m/%Y').date().isoformat()
        if 'FACTURA A' not in text or len(re.findall(r'FACTURA A',text)) != 1:
            raise ValueError('El documento no es una factura A de Cibos reconocida')
        number = match(r'N[^\n]*?\s(\d{4}-\d{8})\s*$')
        rows = re.findall(r'^\s*(EM\d+)\s+(.+?)\s{2,}([\d.,]+)\s+\$\s*([\d.,]+)\s+([\d.,]+)\s*%\s+\$\s*([\d.,]+)\s*$', text, re.M)
        if not rows or len(rows) != len(re.findall(r'^\s*EM\d+',text,re.M)):
            raise ValueError('No se pudieron leer todas las líneas de Cibos')
        perceptions = match(r'Percepciones:([^\n]*)')
        if perceptions.replace('$','').strip() not in ('', '0,00'):
            raise ValueError('Las percepciones de este formato requieren revisión')
        if money(match(r'I\.V\.A\.\s*10,5%:\s*\$\s*([\d.,]+)')) != 0:
            raise ValueError('Las líneas con IVA mixto requieren revisión')
        if money(match(r'Dto:\s*([\d.,]+)\s*%')) != 0:
            raise ValueError('El descuento global requiere revisión')
        return {
            'comprador_cuit': match(r'Cliente:[^\n]*CUIT:\s*([\d-]+)'),
            'proveedor': {'nombre':match(r'^\s*(CIBOS SRL)\s+'), 'cuit':match(r'^\s*CUIT:\s*([\d-]+)')},
            'comprobante': {'clase':'fiscal','tipo':'FACTURA','letra':'A','numero':number,
                'fecha':date(match(r'Fecha emisi.n:\s*(\d{2}/\d{2}/\d{4})')),
                'fecha_vencimiento':date(match(r'Vencimiento:\s*(\d{2}/\d{2}/\d{4})')),
                'cae':match(r'/ CAE:\s*(\d{14})'),
                'cae_vencimiento':date(match(r'Fecha Vto\. CAE:\s*(\d{2}/\d{2}/\d{4})')),
                'moneda':'ARS'},
            'lineas':[{'codigo_proveedor':code,'descripcion':' '.join(desc.split()),
                'cantidad':money(qty),'precio_unitario':money(price),'bonificacion_pct':money(discount),
                'alicuota_iva':21,'subtotal':money(total)} for code,desc,qty,price,discount,total in rows],
            'totales':{'neto':money(match(r'\bNeto:\s*\$\s*([\d.,]+)')),
                'iva':money(match(r'I\.V\.A\.\s*21%:\s*\$\s*([\d.,]+)')),
                'percepciones':[], 'total':money(match(r'\bTOTAL:\s*\$\s*([\d.,]+)'))}
        }
    documents = [parse(text) for text in pages]
    if any(doc != documents[0] for doc in documents[1:]):
        raise ValueError('Las páginas contienen datos distintos: revisar antes de cargar')
    if len(documents) > 1 and not all(re.search(r'\b(ORIGINAL|DUPLICADO|TRIPLICADO)\b',text) for text in pages):
        raise ValueError('No se pudieron identificar las copias del comprobante')
    return documents[0]
