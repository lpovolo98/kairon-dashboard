from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from datetime import date
import hashlib,json

class Revisar(ValueError): pass
def dec(x):
    try:
        if isinstance(x,bool):raise ValueError()
        d=Decimal(str(x or 0))
    except (InvalidOperation,ValueError,TypeError):raise Revisar('Importe no válido')
    if not d.is_finite(): raise Revisar('Importe no válido')
    return d
def rounded(x): return float(dec(x).quantize(Decimal('.01'),rounding=ROUND_HALF_UP))
def ident(v): return v[0] if isinstance(v,(list,tuple)) and v else v or 0

def price(product,rules,categories,day):
    parents=set();cat=ident(product.get('categ_id'))
    while cat and cat not in parents:
        parents.add(cat);cat=ident(categories.get(cat,{}).get('parent_id'))
    matches=[]
    for r in rules:
        if dec(r.get('min_quantity'))>1: continue
        if r.get('date_start') and r['date_start'][:10]>day:continue
        if r.get('date_end') and r['date_end'][:10]<day:continue
        scope=r['applied_on']
        if scope=='0_product_variant' and ident(r.get('product_id'))!=product['id']:continue
        if scope=='1_product' and ident(r.get('product_tmpl_id'))!=ident(product['product_tmpl_id']):continue
        if scope=='2_product_category' and ident(r.get('categ_id')) not in parents:continue
        if scope not in ('0_product_variant','1_product','2_product_category','3_global'):raise Revisar('Alcance de regla no soportado')
        matches.append(r)
    matches.sort(key=lambda r:(r['applied_on'],-float(r.get('min_quantity') or 0),-ident(r.get('categ_id')),-r['id']))
    if not matches:return dec(product['lst_price']),None
    r=matches[0];kind=r['compute_price']
    if kind=='fixed':return dec(r['fixed_price']),r['id']
    if kind=='percentage':return dec(product['lst_price'])*(1-dec(r['percent_price'])/100),r['id']
    if kind!='formula' or r.get('base') not in ('list_price','standard_price'):raise Revisar('Regla de precios requiere revisión: '+str(r['id']))
    base=dec(product['lst_price'] if r['base']=='list_price' else product['standard_price']);value=base*(1-dec(r['price_discount'])/100)
    step=dec(r.get('price_round'))
    if step:value=(value/step).quantize(Decimal('1'),rounding=ROUND_HALF_UP)*step
    value+=dec(r.get('price_surcharge'))
    if r.get('price_min_margin'):value=max(value,base+dec(r['price_min_margin']))
    if r.get('price_max_margin'):value=min(value,base+dec(r['price_max_margin']))
    return value,r['id']

def final_price(base,taxes):
    # Standard sale taxes in this database: percentages, no compound base.
    if any(t.get('amount_type')!='percent' or t.get('include_base_amount') for t in taxes):
        raise Revisar('Impuestos compuestos o no porcentuales: revisar antes de publicar')
    included=sum((dec(t['amount']) for t in taxes if t.get('price_include')),Decimal(0))
    total=sum((dec(t['amount']) for t in taxes),Decimal(0))
    if not taxes:raise Revisar('Producto sin impuesto de venta: confirmar exención en Odoo')
    if included<=-100 or total<0:raise Revisar('Impuestos no válidos')
    return rounded(dec(base)/(1+included/100)*(1+total/100))

def fingerprint(value):return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()

def snapshot(o,day,divisors=None):
    date.fromisoformat(day)
    def read(model,domain,fields):
        if not set(fields)<=set(o.campos(model)):raise Revisar('Faltan campos necesarios en '+model)
        rows=o.call(model,'search_read',[domain],{'fields':fields,'limit':5001,'order':'id'})
        if len(rows)>5000:raise Revisar('Más de 5000 registros: no se generará un catálogo incompleto')
        return rows
    lists=read('product.pricelist',[['active','=',True],['name','=','Canal Proximidad']],['name','currency_id'])
    if len(lists)!=1 or lists[0]['currency_id'][1]!='ARS':raise Revisar('Debe existir una única lista Canal Proximidad en ARS')
    rules=read('product.pricelist.item',[['pricelist_id','=',lists[0]['id']]],['applied_on','product_tmpl_id','product_id','categ_id','min_quantity','compute_price','fixed_price','percent_price','base','price_discount','price_surcharge','price_round','price_min_margin','price_max_margin','date_start','date_end'])
    cats={r['id']:r for r in read('product.category',[],['name','parent_id'])}
    taxes={r['id']:r for r in read('account.tax',[['type_tax_use','=','sale']],['name','amount','amount_type','price_include','include_base_amount','company_id'])}
    products=read('product.product',[['active','=',True],['sale_ok','=',True],['type','!=','service']],['name','default_code','product_tmpl_id','lst_price','standard_price','taxes_id','categ_id','x_studio_unidades_por_caja','currency_id','company_id'])
    rows=[];issues=[]
    for p in products:
        try:
            if not p['default_code']:raise Revisar('Falta código interno')
            if p['currency_id'][1]!='ARS':raise Revisar('Moneda de producto diferente de ARS')
            base,rule=price(p,rules,cats,day)
            if base<=0:raise Revisar('Precio vacío o no positivo')
            taxrows=[taxes[t] for t in p['taxes_id']]
            if len({ident(t['company_id']) for t in taxrows})>1:raise Revisar('Impuestos de distintas empresas')
            pack=dec(p['x_studio_unidades_por_caja'])
            if pack<=0 or pack!=int(pack):raise Revisar('Unidades por caja no válidas')
            divisor=dec((divisors or {}).get(str(p['id']),1))
            if divisor<1 or divisor>1000 or divisor!=int(divisor):raise Revisar('Equivalencia de display inválida')
            sale_price=final_price(base,taxrows)
            rows.append({'id':p['id'],'template_id':ident(p['product_tmpl_id']),'sku':p['default_code'].strip(),'name':p['name'],'category':p['categ_id'][1],'pack':int(pack*divisor),'odoo_pack':int(pack),'divisor':int(divisor),'net':float(base),'sale_price':sale_price,'price':rounded(dec(sale_price)/divisor),'rule_id':rule,'taxes':[{'id':t['id'],'name':t['name'],'rate':t['amount'],'included':t['price_include']} for t in taxrows]})
        except (Revisar,KeyError) as e:issues.append({'id':p['id'],'name':p['name'],'reason':str(e)})
    rows.sort(key=lambda p:(p['category'],p['name'],p['sku']))
    return {'date':day,'pricelist':lists[0],'products':rows,'issues':issues,'hash':fingerprint({'products':rows,'issues':issues})}

def validate_campaign(data,products):
    if not data.get('name','').strip() or len(data['name'])>100:raise Revisar('Indicá un nombre de campaña de hasta 100 caracteres')
    start=date.fromisoformat(data['start']);end=date.fromisoformat(data['end'])
    if end<start:raise Revisar('La vigencia final debe ser posterior al inicio')
    known={p['id']:p for p in products};used=set()
    if len(data.get('actions',[]))>30:raise Revisar('Máximo 30 acciones por campaña')
    for a in data.get('actions',[]):
        if not a.get('title') or len(a['title'])>90:raise Revisar('Cada acción necesita un título de hasta 90 caracteres')
        ids=a.get('products',[])
        if not ids or len(ids)>12 or len(ids)!=len(set(ids)) or any(i not in known for i in ids):raise Revisar('Seleccioná de 1 a 12 productos válidos por acción')
        if used.intersection(ids):raise Revisar('Un producto aparece en dos acciones: resolvé la superposición')
        used.update(ids)
        if len(a.get('conditions',''))>600:raise Revisar('Condiciones demasiado extensas')
        if a.get('kind','discount')=='discount':
            tiers=a.get('tiers',[])
            if not 1<=len(tiers)<=4:raise Revisar('Cada descuento requiere entre 1 y 4 escalas')
            last_min=last_discount=Decimal(-1)
            unit=a.get('unit','units')
            if unit not in ('units','boxes','packs'):raise Revisar('Unidad de escala no válida')
            if unit=='packs' and not 1<=int(a.get('pack_units',0))<=1000:raise Revisar('Indicá unidades por pack')
            for t in tiers:
                qty=dec(t['min']);off=dec(t['discount'])
                if qty!=int(qty) or qty<=last_min or qty<1 or off<0 or off>=100 or off<last_discount:raise Revisar('Escalas: mínimos enteros crecientes y descuentos entre 0 y menos de 100%')
                last_min=qty;last_discount=off
        elif a['kind']=='combo':
            paid=a.get('quantities',{});gift=a.get('gifts',{})
            if not paid or not gift:raise Revisar('El combo necesita cantidades compradas y bonificadas')
            if set(paid).intersection(gift):raise Revisar('Separá productos comprados y bonificados')
            if {int(i) for i in list(paid)+list(gift)}!=set(ids):raise Revisar('Las cantidades del combo deben cubrir exactamente sus productos')
            for q in list(paid.values())+list(gift.values()):
                if dec(q)<=0 or dec(q)!=int(dec(q)):raise Revisar('Cantidades de combo enteras positivas')
        else:raise Revisar('Tipo de acción no válido')
    return data

def discounted(price_value,discount):return rounded(dec(price_value)*(1-dec(discount)/100))
