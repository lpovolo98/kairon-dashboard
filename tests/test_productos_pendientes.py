import json,os,tempfile,unittest
from unittest.mock import patch
from pathlib import Path
from productos import api
from administracion import api as admin,service
from tests.test_productos import base
from tests.test_administracion import document

class Pendientes(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.env=patch.dict(os.environ,{'PRODUCTOS_DATA_DIR':self.tmp.name+'/p','ADMIN_DATA_DIR':self.tmp.name+'/a'});self.env.start()
        self.o,self.d=base();self.job='a'*64
    def tearDown(self):self.env.stop();self.tmp.cleanup()
    def test_invoice_missing_product_stops_and_can_resume(self):
        doc=document()
        with admin.connect() as db:db.execute('INSERT INTO jobs VALUES (?,?,?)',(self.job,'test',json.dumps({'state':'recibido'})))
        missing=service.ProductosPendientes([{'codigo_proveedor':'NEW','descripcion':'Producto','precio_unitario':10}],{'id':self.d['prov']})
        with patch.object(service,'extract',return_value=doc),patch.object(service,'process',side_effect=missing):admin.work(self.job,b'pdf')
        items=api.pendientes(owner='test');self.assertEqual(len(items),1)
        self.assertEqual(api.pendientes(owner='another'),[])
        self.assertTrue((admin.directory()/(self.job+'.pdf')).exists())
        with patch.object(admin.executor,'submit') as submit:
            api.resolver_pendientes(self.o);submit.assert_not_called()
            self.o.call('product.supplierinfo','create',[{'partner_id':self.d['prov'],'product_code':'NEW','product_tmpl_id':self.d['p1']}])
            api.resolver_pendientes(self.o);submit.assert_called_once()
            api.resolver_pendientes(self.o);submit.assert_called_once()
            self.assertEqual(submit.call_args.args[3],doc)
        self.assertEqual(api.pendientes(owner='test'),[])
    def test_existing_purchase_never_resumes(self):
        with admin.connect() as db:db.execute('INSERT INTO jobs VALUES (?,?,?)',(self.job,'test',json.dumps({'state':'esperando_productos','purchase_id':42})))
        (admin.directory()/(self.job+'.pdf')).write_bytes(b'pdf')
        with patch.object(admin.executor,'submit') as submit:
            admin.continuar_productos(self.job);submit.assert_not_called()
    def test_multiple_invoices_share_one_mapping(self):
        for id in ['a','b']:
            api.encolar({'proveedor':'BIO','proveedor_id':self.d['prov'],'codigo_proveedor':'NEW','owner':'test','job_id':id})
        self.assertEqual(len(api.pendientes(owner='test')),1)
        self.assertEqual(len(api.pendientes(owner='test')[0]['facturas']),2)
