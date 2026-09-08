"""Read the Starbread layout and match reviewed supplier aliases, never guessed IDs."""
import hashlib
import io
import re
import unicodedata
from datetime import datetime, timedelta
from decimal import Decimal
from pypdf import PdfReader

def normalize(text):
    return re.sub(r'[^a-z0-9]','',unicodedata.normalize('NFKD',text).encode('ascii','ignore').decode().lower())

# Reviewed against supplierinfo in Kairon. These are mapping aliases, not PDF codes.
ALIASES = {
 'Pan Molde Clásico':'123456', 'Pan Molde Semilla':'400002', 'Pan Molde Integral':'400007',
 'Pan Tortuga':'400003','Pan Tortuga Sésamo':'400004','Pan Campero Clásico':'400005',
 'Pan Campero Semilla':'400006','Pan Minguitas':'400009','Pan Gourmet':'400010','Pan Panchitos':'400008',
 'Premezcla Bizcochuelo Vainilla':'400012','Premezcla Bizcochuelo Choco':'400014',
 'Premezcla Pizza':'400011','Premezcla Pan de Queso':'400013','Tostadas Clásicas':'400015',
 'Tostadas Semillas':'400017','Tostadas Dulces':'400016','Minitostaditas Queso':'400018',
 'Minitostaditas Pizza':'400019','Minitostaditas Jamón':'400020','Minitostaditas Tomate Albahaca':'400021',
 'Migas Clásico':'400022','Migas Provenzal':'400023','Migas Semillas':'400024',
 'Avena Tradicional Bio 500g':'400027','Copos de Avena Bio 500g':'400028',
 'Salvado de Avena Bio 500g':'400026',
 'Avena Extrafina Bio 500g':'400025', # Confirmado expresamente por el usuario.
 'Pasta Di Oro Spaghetti':'400029','Pasta For Kids Alphabet':'400030',
 'Pasta Green Pea Penne':'400031','Pasta Red Lentil Fusilli':'400032',
}
APPROVED_DISCOUNTS = {'0a49dbb115f29745fcd1904c7cf7b44876f11bec31352d039c5493d5187a5028': Decimal('5')}


def read(pdf):
    texts=[p.extract_text(extraction_mode='layout') or '' for p in PdfReader(io.BytesIO(pdf)).pages]
    if not texts or not all('STARBREAD' in t and '30-71437923-9' in t for t in texts): return None
    def find(pattern,text):
        m=re.search(pattern,text,re.M)
        if not m: raise ValueError('Falta un dato obligatorio en el formato STARBREAD')
        return m.group(1)
    def number(s): return Decimal(s.replace(',',''))
    headers=[]; groups=[]
    for text in texts:
        header=(find(r'\bA(\d{5}-\d{8})',text),find(r'Fecha:\s*([\d/]+)',text),find(r'C\.A\.E\.:\s*(\d{14})',text))
        headers.append(header)
        rows=re.findall(r'^\s*(\d+\.\d{4})\s+(.+?)\s{2,}([\d,]+\.\d{5})\s+([\d,]+\.\d{2})\s*$',text,re.M)
        if not rows: raise ValueError('No se reconocieron los artículos de STARBREAD')
        signature=tuple((q,normalize(d),p,a) for q,d,p,a in rows)
        subtotal=number(find(r'SUBTOTAL\s*:\s*([\d,.]+)',text))
        total_match=re.search(r'^\s*TOTAL\s*:\s*([\d,.]+)',text,re.M)
        total=number(total_match.group(1)) if total_match else None
        if not any(g[0]==signature and g[2]==subtotal and g[3]==total for g in groups):
            groups.append((signature,rows,subtotal,total,text))
    if any(h!=headers[0] for h in headers): raise ValueError('El PDF mezcla comprobantes diferentes')
    total_pages=[g for g in groups if g[3] is not None]
    if len(total_pages)!=1: raise ValueError('No se identificó un único total de factura')
    final=total_pages[0]; all_rows=[r for g in groups for r in g[1]]
    gross=sum(number(r[3]) for r in all_rows)
    if gross!=final[2]: raise ValueError('Las páginas no suman el subtotal impreso')
    iva=number(find(r'IVA\s+21%\s*:\s*([\d,.]+)',final[4]))
    perception=number(find(r'PERC\s+IVA\s+3%\s*:\s*([\d,.]+)',final[4]))
    discount=APPROVED_DISCOUNTS.get(hashlib.sha256(pdf).hexdigest(),Decimal(0))
    net=gross*(1-discount/100)
    if abs(net+iva+perception-final[3])>Decimal('.01'):
        raise ValueError('El subtotal, impuestos y total no coinciden. Confirmar descuento con el usuario; no se infiere.')
    aliases={normalize(k):v for k,v in ALIASES.items()}
    lines=[]
    for q,desc,p,a in all_rows:
        code=aliases.get(normalize(desc))
        if not code: raise ValueError('Falta confirmar el producto de Odoo para: '+desc)
        qty,price,amount=number(q),number(p),number(a)
        if qty*price!=amount: raise ValueError('Cantidad por precio no coincide: '+desc)
        lines.append({'codigo_proveedor':code,'descripcion':' '.join(desc.split()),'cantidad':float(qty),
            'precio_unitario':float(price),'subtotal':float(amount*(1-discount/100)),
            'subtotal_bruto':float(amount),'bonificacion_pct':float(discount),'alicuota_iva':21})
    issued=datetime.strptime(headers[0][1],'%d/%m/%Y').date()
    days=int(find(r'COND\.VENTA:\s*(\d+)\s+DIAS',texts[0]))
    return {'comprador_cuit':find(r'IVA:Responsable[^\n]*CUIT:\s*([\d-]+)',texts[0]),
        'proveedor':{'nombre':'STARBREAD S.A.','cuit':'30-71437923-9','tipo_persona':'empresa',
            'calle':find(r'^\s*(LISANDRO[^\n]+)',texts[0]).strip(), 'localidad':'Pilar',
            'provincia':'Buenos Aires','codigo_postal':find(r'\b(C\d{4})\s+PILAR',texts[0]),
            'condicion_iva':'IVA Responsable Inscripto','ingresos_brutos':find(r'ING\.BRUTOS\s+CM:\s*([\d-]+)',texts[0]),
            'inicio_actividades':datetime.strptime(find(r'INICIO\s+DE\s+ACT:\s*([\d/]+)',texts[0]),'%d/%m/%Y').date().isoformat()},
        'comprobante':{'clase':'fiscal','tipo':'FACTURA','letra':'A','numero':headers[0][0],
            'fecha':issued.isoformat(),'fecha_vencimiento':(issued+timedelta(days=days)).isoformat(),
            'condicion_venta':str(days)+' DIAS FECHA FACTURA','cae':headers[0][2],
            'cae_vencimiento':datetime.strptime(find(r'VTO:\s*([\d/]+)',texts[0]),'%d/%m/%Y').date().isoformat(),'moneda':'ARS'},
        'lineas':lines,'totales':{'neto':float(net),'iva':float(iva),'total':float(final[3]),
            'percepciones':[{'detalle':'PERC IVA 3%','alicuota':3,'importe':float(perception)}]},
        'observaciones_carga':f'Descuento general {discount}% confirmado por el usuario para este PDF. Subtotal bruto {gross}. Códigos resueltos por equivalencias del proveedor; no impresos en PDF.'}
