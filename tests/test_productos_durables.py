import json
import os
import tempfile
import unittest
from unittest.mock import patch
from tests.test_productos import base, fila
from productos import api, cargar, trabajos

class Durables(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.env=patch.dict(os.environ,{'PRODUCTOS_DATA_DIR':self.tmp.name});self.env.start()
        self.o,self.d=base()
    def tearDown(self):self.env.stop();self.tmp.cleanup()
    def new(self):
        with api.conectar() as db:
            return db.execute('INSERT INTO corridas(owner,data) VALUES (?,?)',('test',json.dumps({'estado':'pendiente'}))).lastrowid
    def read(self,id):
        with api.conectar() as db:return json.loads(db.execute('SELECT data FROM corridas WHERE id=?',(id,)).fetchone()[0])
    def runjob(self,hojas,hook=None):
        revision=trabajos.revision(cargar.previsualizar(self.o,hojas));id=self.new()
        if hook:hook()
        with patch.object(api,'_odoo',return_value=self.o):trabajos.ejecutar(id,hojas,revision)
        return id,self.read(id)
    def test_success_journal_and_undo(self):
        id,data=self.runjob({'productos':[fila('400001',**{'Precio de venta':3000})]})
        self.assertEqual(data['estado'],'completada');self.assertEqual(len(data['operaciones']),1)
        trabajos.revertir(self.o,id,data)
        self.assertEqual(self.o.registro('product.template',self.d['p1'])['list_price'],2890)
    def test_later_edit_blocks_undo(self):
        id,data=self.runjob({'productos':[fila('400001',**{'Precio de venta':3000})]})
        self.o.registro('product.template',self.d['p1'])['list_price']=3500
        with self.assertRaises(cargar.Frenar):trabajos.revertir(self.o,id,data)
        self.assertEqual(self.o.registro('product.template',self.d['p1'])['list_price'],3500)
    def test_changed_preview_never_writes(self):
        _,data=self.runjob({'productos':[fila('400001',**{'Precio de venta':3000})]},lambda:self.o.registro('product.template',self.d['p1']).update(list_price=3500))
        self.assertEqual(data['estado'],'rechazada');self.assertEqual(self.o.escrituras,[])
    def test_interrupted_rpc_keeps_previous_and_intent(self):
        real=self.o.call
        def call(model,method,args,kwargs=None):
            if method=='write' and args[0]==[self.d['p2']]:raise TimeoutError()
            return real(model,method,args,kwargs)
        self.o.call=call
        id,data=self.runjob({'productos':[fila('400001',**{'Precio de venta':3000}),fila('400005',**{'Precio de venta':3500})]})
        self.assertEqual(data['estado'],'incierta')
        self.assertEqual([op['estado'] for op in data['operaciones']],['confirmada','enviando'])
        self.assertEqual(data['operaciones'][0]['antes'][0]['list_price'],2890)
        with self.assertRaises(cargar.Frenar):trabajos.revertir(self.o,id,data)
    def test_relation_undo_deletes_only_new_relation(self):
        id,data=self.runjob({'proveedores':[{'SKU (igual al de Productos)':'400001','Proveedor':'BIO ALIMENTOS SA','Código del proveedor':'P1','Precio':10}]})
        self.assertEqual(data['estado'],'completada')
        new=data['operaciones'][0]['ids'][0]
        trabajos.revertir(self.o,id,data)
        self.assertNotIn(new,self.o.datos['product.supplierinfo'])
        self.assertIn(self.d['p1'],self.o.datos['product.template'])
    def test_case_duplicates_block_all(self):
        _,data=self.runjob({'productos':[fila(s,**{'Nombre':'Producto','Unidades por caja':12}) for s in ['ABC','abc']]})
        self.assertEqual(data['estado'],'rechazada');self.assertEqual(self.o.escrituras,[])
    def test_invalid_numbers_and_archived_block(self):
        for n in ['abc',float('nan'),-1,0]:
            result=cargar.previsualizar(self.o,{'productos':[fila('400001',**{'Unidades por caja':n})]})
            self.assertEqual(result['productos'][0]['estado'],'error')
        self.o.registro('product.template',self.d['p1'])['active']=False
        result=cargar.previsualizar(self.o,{'productos':[fila('400001',**{'Nombre':'Duplicado'})]})
        self.assertEqual(result['productos'][0]['estado'],'error')
    def test_wrong_tax_scope_blocks(self):
        errors=[];r=cargar.Resolvedor(self.o).impuestos(f'IVA (id {self.d["iva_c"]})','sale',errors,'Venta')
        self.assertEqual(r,[]);self.assertTrue(errors)
    def test_idempotency_returns_one_job(self):
        body={'productos':[fila('400001',**{'Precio de venta':3000})]}
        body.update(revision=trabajos.revision(cargar.previsualizar(self.o,body)),request_id='test-request-123456789')
        with patch.object(trabajos.executor,'submit') as submit:
            first=api.aplicar(body,owner='test');second=api.aplicar(body,owner='test')
            self.assertEqual(first['id'],second['id']);submit.assert_called_once()
    def test_restart_never_replays(self):
        id=self.new();trabajos.guardar(id,estado='ejecutando',operaciones=[{'estado':'enviando'}])
        api.recuperar_trabajos();self.assertEqual(self.read(id)['estado'],'incierta')
    def test_batched_lookup_preserves_existing_products(self):
        rows=[fila('400001',**{'Precio de venta':3000})]+[fila(f'NEW{i}',**{'Nombre':'Nuevo','Unidades por caja':12}) for i in range(25)]
        plan=cargar.previsualizar(self.o,{'productos':rows})
        self.assertEqual(plan['productos'][0]['estado'],'modificacion')
        self.assertEqual(len([f for f in plan['productos'] if f['estado']=='alta']),25)
    def test_photo_journal_uses_files_and_can_restore(self):
        from tests.test_productos import PNG
        id=self.new();self.o.registro('product.template',self.d['p1'])['image_1920']='OLD'
        with patch.object(api,'_odoo',return_value=self.o):
            trabajos.ejecutar(id,[{'sku':'400001','imagen':PNG}],None,True)
        data=self.read(id)
        self.assertEqual(data['estado'],'completada')
        self.assertIn('archivo_imagen',data['operaciones'][0]['valores']['image_1920'])
        trabajos.revertir(self.o,id,data)
        self.assertEqual(self.o.registro('product.template',self.d['p1'])['image_1920'],'OLD')
    def test_used_new_product_blocks_archive(self):
        id,data=self.runjob({'productos':[fila('NEW',**{'Nombre':'Nuevo','Unidades por caja':12})]})
        real=self.o.call
        def call(model,method,args,kwargs=None):
            if model=='stock.move' and method=='search_count':return 1
            return real(model,method,args,kwargs)
        self.o.call=call
        with self.assertRaises(cargar.Frenar):trabajos.revertir(self.o,id,data)
        self.assertTrue(self.o.registro('product.template',data['creados'][0]['id'])['active'])
