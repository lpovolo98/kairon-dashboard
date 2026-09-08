import unittest
from unittest.mock import MagicMock
from administracion.suppliers import resolve
from administracion.cargar import Informe, Frenar

class Suppliers(unittest.TestCase):
    def test_exact_existing_cuit_keeps_existing_contact(self):
        o=MagicMock(); o.call.return_value=[{'id':12,'name':'Existing','vat':'30714379239','active':True}]
        result=resolve(o,{'proveedor':{'nombre':'STARBREAD','cuit':'30-71437923-9'}},MagicMock(),Informe())
        self.assertEqual(result['id'],12); o.crear.assert_not_called()

    def test_archived_cuit_never_creates_duplicate(self):
        o=MagicMock(); o.call.return_value=[{'id':12,'name':'Existing','vat':'30714379239','active':False}]
        with self.assertRaises(Frenar): resolve(o,{'proveedor':{'nombre':'STARBREAD','cuit':'30-71437923-9'}},MagicMock(),Informe())
        o.crear.assert_not_called()

    def test_new_contact_uses_invoice_fields(self):
        o=MagicMock(); o.call.return_value=[]; o.crear.return_value=999
        o.uno.return_value={'id':1}; o.buscar_leer.return_value=[{'id':554}]
        save=MagicMock()
        resolve(o,{'proveedor':{'nombre':'STARBREAD S.A.','cuit':'30-71437923-9','tipo_persona':'empresa',
            'calle':'LISANDRO DE LA TORRE 1165','localidad':'Pilar','provincia':'Buenos Aires','codigo_postal':'C1629',
            'condicion_iva':'IVA Responsable Inscripto','ingresos_brutos':'30-71437923-9'}},save,Informe())
        vals=o.crear.call_args.args[1]
        self.assertEqual(vals['street'],'LISANDRO DE LA TORRE 1165')
        self.assertEqual(vals['vat'],'30714379239'); self.assertEqual(vals['supplier_rank'],1)
        self.assertNotIn('email',vals); self.assertEqual(vals['zip'],'C1629')
        self.assertEqual(save.call_args_list[-1].kwargs['supplier_id'],999)

    def test_invalid_cuit_no_creation(self):
        o=MagicMock(); o.call.return_value=[]
        with self.assertRaises(Frenar): resolve(o,{'proveedor':{'nombre':'Wrong','cuit':'30-71437923-0'}},MagicMock(),Informe())
        o.crear.assert_not_called()
