"""Vendor price credits: no purchase, picking, payment or reconciliation."""
import base64
import re
from . import cargar as loader

def process_credit(o, doc, pdf, save, supplier, lines, taxes, journal, cfg, inf, company_id, *, validate_only=False):
    digits = lambda s: re.sub(r'\D', '', s or '').lstrip('0')
    number = digits(doc['comprobante']['factura_original'])
    candidates = o.buscar_leer('account.move', [['partner_id','=',supplier['id']],
        ['company_id','=',company_id], ['move_type','=','in_invoice'], ['state','!=','cancel']],
        ['ref','name','l10n_latam_document_number','state','amount_total'], limite=0)
    originals = [row for row in candidates if any(digits(row.get(field)) == number
                 for field in ('ref','name','l10n_latam_document_number') if row.get(field))]
    if len(originals) != 1:
        raise loader.Frenar('No se encontró una única factura original del proveedor para vincular el reembolso')
    original = originals[0]
    if doc['totales']['total'] > original['amount_total']:
        raise loader.Frenar('El reembolso supera el total de la factura original')
    if not o.tiene('account.move','reversed_entry_id'):
        raise loader.Frenar('Odoo no permite vincular el reembolso con la factura original')
    inf.ok('Reembolso por diferencia de precio. Sin compra ni movimiento de stock.')
    if validate_only:
        return {'supplier_id':supplier['id'], 'original_move_id':original['id'], 'line_count':len(lines), 'report':inf.lineas}
    save(state='creando_factura', message='Creando reembolso por diferencia de precio', original_move_id=original['id'])
    move_id = loader.crear_factura(o, doc, supplier, lines, taxes, journal, cfg, None, False, inf, original_id=original['id'])
    save(state='verificando', message='Verificando reembolso y factura original', move_id=move_id)
    o.crear('ir.attachment', {'name':'nota-credito-proveedor.pdf','type':'binary',
        'datas':base64.b64encode(pdf).decode(),'res_model':'account.move','res_id':move_id,'mimetype':'application/pdf'})
    move = o.uno('account.move', [['id','=',move_id]], ['state','move_type','amount_total','reversed_entry_id'])
    if not move or move['state'] != 'draft' or move['move_type'] != 'in_refund' or (move.get('reversed_entry_id') or [None])[0] != original['id']:
        raise RuntimeError('No se pudo verificar el reembolso vinculado en borrador')
    if abs(move['amount_total'] - doc['totales']['total']) > .01 or inf.alertas:
        save(state='revision', message='Reembolso en borrador: revisar diferencias de importe.', report=inf.lineas)
        return
    save(state='borrador', message='Reembolso creado en borrador y vinculado a la factura original. Sin movimiento de stock.', report=inf.lineas)
