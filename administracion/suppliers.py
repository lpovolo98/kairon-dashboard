"""Find by exact CUIT, including archived contacts; create only from invoice data."""
import html
import re
from .cargar import Frenar


def resolve(o, doc, save, inf):
    supplier = doc['proveedor']
    cuit = re.sub(r'\D', '', supplier['cuit'])
    fields = ['name','vat','active','parent_id','commercial_partner_id',
              'property_purchase_currency_id','property_supplier_payment_term_id']
    contacts = o.call('res.partner','search_read',[[['vat','!=',False]]],
                      {'fields':o.solo_existentes('res.partner',fields),'context':{'active_test':False}})
    matches = [p for p in contacts if re.sub(r'\D','',p.get('vat') or '') == cuit and not p.get('parent_id')]
    if len(matches) > 1:
        raise Frenar('Hay varios proveedores con ese CUIT. Revisalos antes de cargar.')
    if matches:
        if matches[0].get('active') is False:
            raise Frenar('El proveedor está archivado. Revisalo antes de cargar.')
        inf.ok('Proveedor existente: ' + matches[0]['name'])
        return matches[0]
    if any(re.sub(r'\D','',p.get('vat') or '') == cuit for p in contacts):
        raise Frenar('El CUIT está en un contacto dependiente. Revisá la ficha principal.')
    checksum = 11 - sum(int(n)*w for n,w in zip(cuit[:10],[5,4,3,2,7,6,5,4,3,2])) % 11
    checksum = 0 if checksum == 11 else 9 if checksum == 10 else checksum
    if len(cuit)!=11 or checksum!=int(cuit[-1]):
        raise Frenar('No se crea el proveedor: el CUIT no pasa la validación.')
    country = o.uno('res.country',[['code','=','AR']],['id'])
    if not country:
        raise Frenar('No se encontró Argentina para completar la ficha del proveedor.')
    values = {'name':supplier['nombre'],'vat':cuit,'supplier_rank':1,'country_id':country['id']}
    if supplier.get('tipo_persona'):
        values['company_type'] = 'company' if supplier['tipo_persona']=='empresa' else 'person'
    for source,target in [('calle','street'),('localidad','city'),('codigo_postal','zip'),('email','email'),('telefono','phone')]:
        if supplier.get(source): values[target]=supplier[source]
    if supplier.get('provincia'):
        states=o.buscar_leer('res.country.state',[['country_id','=',country['id']],['name','=ilike',supplier['provincia']]],['id'])
        if len(states)!=1: raise Frenar('No se pudo identificar la provincia del proveedor.')
        values['state_id']=states[0]['id']
    if o.tiene('res.partner','l10n_ar_afip_responsibility_type_id'):
        codes={'IVA Responsable Inscripto':'1','IVA Sujeto Exento':'4','Responsable Monotributo':'6'}
        code=codes.get(supplier.get('condicion_iva'))
        if not code: raise Frenar('Falta una condición de IVA reconocida para crear el proveedor.')
        responsibility=o.uno('l10n_ar.afip.responsibility.type',[['code','=',code]],['id'])
        if not responsibility: raise Frenar('No está configurada la condición de IVA del proveedor.')
        values['l10n_ar_afip_responsibility_type_id']=responsibility['id']
    if o.tiene('res.partner','l10n_latam_identification_type_id'):
        kind=o.uno('l10n_latam.identification.type',[['name','=','CUIT']],['id'])
        if not kind: raise Frenar('No se encontró el tipo de identificación CUIT.')
        values['l10n_latam_identification_type_id']=kind['id']
    if supplier.get('ingresos_brutos') and o.tiene('res.partner','l10n_ar_gross_income_number'):
        values['l10n_ar_gross_income_number']=supplier['ingresos_brutos']
    if supplier.get('inicio_actividades') and o.tiene('res.partner','comment'):
        values['comment']='Inicio de actividades según factura: ' + html.escape(supplier['inicio_actividades'])
    save(state='creando_proveedor',message='Creando proveedor con los datos de la factura',document=doc)
    partner_id=o.crear('res.partner',values)
    save(state='validando',message='Proveedor creado; controlando los artículos',supplier_id=partner_id)
    inf.ok('Proveedor creado con CUIT ' + cuit)
    return {'id':partner_id,**values}
