"""Durable operation journal. An uncertain RPC is never automatically replayed."""
import hashlib
import json
import sqlite3
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from . import cargar

executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='productos')

def revision(plan):
    return hashlib.sha256(json.dumps(plan,sort_keys=True,ensure_ascii=True,default=str).encode()).hexdigest()

def guardar(job_id, **changes):
    from .api import conectar
    with conectar() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT data FROM corridas WHERE id=?',(job_id,)).fetchone()
        data=json.loads(row[0]); data.update(changes, actualizado=datetime.now(timezone.utc).isoformat())
        db.execute('UPDATE corridas SET data=? WHERE id=?',(json.dumps(compactar(data)),job_id))

def compactar(value):
    """Store image bodies once, outside the journal JSON and API responses."""
    from .api import directorio
    if isinstance(value,list):return [compactar(v) for v in value]
    if not isinstance(value,dict):return value
    result={}
    for key,item in value.items():
        if key=='image_1920' and isinstance(item,str) and item:
            digest=hashlib.sha256(item.encode()).hexdigest()
            folder=directorio()/'imagenes';folder.mkdir(exist_ok=True)
            path=folder/(digest+'.b64')
            if not path.exists():path.write_text(item,encoding='ascii')
            result[key]={'archivo_imagen':digest}
        else:result[key]=compactar(item)
    return result

def expandir(value):
    from .api import directorio
    if isinstance(value,list):return [expandir(v) for v in value]
    if not isinstance(value,dict):return value
    if set(value)=={'archivo_imagen'}:
        name=value['archivo_imagen']
        if not isinstance(name,str) or len(name)!=64 or any(c not in '0123456789abcdef' for c in name):raise cargar.Frenar('Referencia de imagen inválida.')
        return (directorio()/'imagenes'/(name+'.b64')).read_text(encoding='ascii')
    return {k:expandir(v) for k,v in value.items()}

class Diario:
    def __init__(self, odoo, job_id):
        self.odoo=odoo; self.job_id=job_id; self.operaciones=[]
    def __getattr__(self,name): return getattr(self.odoo,name)
    def call(self, modelo, metodo, args, kwargs=None):
        if metodo not in ('create','write','unlink'):
            return self.odoo.call(modelo,metodo,args,kwargs)
        if metodo == 'unlink':
            raise cargar.Frenar('La carga no puede eliminar registros.')
        vals=args[0] if metodo=='create' else args[1]
        ids=[] if metodo=='create' else args[0]
        if metodo=='write' and len(ids)!=1: raise cargar.Frenar('Se requiere una operación por registro')
        fields=list(vals)
        if metodo=='create' and 'write_date' in self.odoo.campos(modelo):fields.append('write_date')
        before=self.odoo.call(modelo,'read',[ids],{'fields':fields}) if ids else []
        op={'modelo':modelo,'metodo':metodo,'valores':vals,'campos':fields,'ids':ids,'antes':before,'estado':'enviando'}
        self.operaciones.append(op)
        guardar(self.job_id,operaciones=self.operaciones)
        result=self.odoo.call(modelo,metodo,args,kwargs)
        op['ids']=[result] if metodo=='create' else ids
        op['estado']='confirmada'
        # Persist the returned ID before any follow-up request can fail.
        guardar(self.job_id,operaciones=self.operaciones)
        op['despues']=self.odoo.call(modelo,'read',[op['ids']],{'fields':fields})
        guardar(self.job_id,operaciones=self.operaciones)
        return result

def ejecutar(job_id, hojas, expected_revision, fotos=False):
    from .api import _odoo, directorio, TOPE_REGISTROS
    journal=None
    try:
        with closing(sqlite3.connect(directorio()/'escritor.sqlite',timeout=600)) as guard, guard:
            guard.execute('CREATE TABLE IF NOT EXISTS mutex (id INTEGER)'); guard.execute('BEGIN IMMEDIATE')
            guardar(job_id,estado='validando')
            o=_odoo(); plan=cargar.previsualizar(o,hojas) if not fotos else None
            if not fotos and revision(plan)!=expected_revision:
                raise cargar.Frenar('Odoo cambió desde la revisión. Volvé a previsualizar antes de aplicar.')
            journal=Diario(o,job_id)
            guardar(job_id,estado='ejecutando')
            if fotos:
                from .imagenes import aplicar
                result=aplicar(journal,hojas)
            else:
                result=cargar.aplicar(journal,hojas,tope=TOPE_REGISTROS,plan=plan)
            # Undo is reconstructed from the durable operations, not browser data.
            result.pop('deshacer',None)
            guardar(job_id,estado='completada',**result)
            # A failure to notify never changes the successful catalogue outcome.
            if not fotos:
                try:
                    from .api import resolver_pendientes
                    resolver_pendientes(o)
                except Exception:
                    guardar(job_id,mensaje='Catálogo aplicado. Revisá los pendientes para retomar los comprobantes.')
    except Exception as exc:
        operations=journal.operaciones if journal else []
        uncertain=any(op['estado']=='enviando' for op in operations)
        message=str(exc) if isinstance(exc,cargar.Frenar) else 'La conexión se interrumpió. El avance quedó registrado; revisá la corrida antes de repetir.'
        guardar(job_id,estado='incierta' if uncertain else 'parcial' if operations else 'rechazada',mensaje=message[:1000])

def plan_reversion(o, data):
    data=expandir(data)
    ops=data.get('operaciones',[])
    if not ops:
        raise cargar.Frenar('Esta corrida no tiene un registro verificable para revertir automáticamente.')
    if any(op.get('estado')=='enviando' for op in ops):
        raise cargar.Frenar('Hay una escritura incierta. Requiere comprobar el resultado en Odoo antes de revertir.')
    result=[]
    for op in reversed(ops):
        if op.get('revertida'): continue
        if not op.get('despues'): raise cargar.Frenar('Falta verificar una escritura. Revisá el registro en Odoo.')
        actual=o.call(op['modelo'],'read',[op['ids']],{'fields':op.get('campos',list(op['valores'])), 'context':{'active_test':False}})
        if actual!=op['despues']:
            raise cargar.Frenar('Conflicto: el registro '+str(op['ids'][0])+' cambió después de esta corrida. No se revertirá automáticamente.')
        if op['metodo']=='create' and op['modelo']=='product.template':
            for model in ('purchase.order.line','sale.order.line','stock.move','account.move.line'):
                if o.call(model,'search_count',[[['product_id.product_tmpl_id','=',op['ids'][0]]]]):
                    raise cargar.Frenar('El producto nuevo ya está vinculado a una operación de Odoo. Revisá sus documentos antes de archivarlo.')
        result.append({'modelo':op['modelo'],'id':op['ids'][0],
            'accion':'restaurar' if op['metodo']=='write' else 'archivar' if op['modelo']=='product.template' else 'eliminar vínculo nuevo'})
    return result

def revertir(o, job_id, data):
    data=expandir(data)
    plan_reversion(o,data)
    ops=data['operaciones']
    for op in reversed(ops):
        if op.get('revertida'): continue
        op['reversion_estado']='enviando'; guardar(job_id,operaciones=ops,estado='revirtiendo')
        if op['metodo']=='write':
            vals={k:cargar._revertible(v) for k,v in op['antes'][0].items() if k!='id'}
            o.call(op['modelo'],'write',[op['ids'],vals])
        elif op['modelo']=='product.template':
            o.call(op['modelo'],'write',[op['ids'],{'active':False}])
        else:
            o.call(op['modelo'],'unlink',[op['ids']])
        op['revertida']=True; op['reversion_estado']='confirmada'; guardar(job_id,operaciones=ops)
    guardar(job_id,estado='revertida',revertida=datetime.now(timezone.utc).isoformat())
