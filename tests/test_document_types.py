import copy
import unittest
from unittest.mock import MagicMock, patch
from administracion import service, refunds
from test_administracion import document

class DocumentTypes(unittest.TestCase):
    def test_credit_requires_original_and_reason(self):
        doc = document(); doc['comprobante']['tipo'] = 'NOTA_CREDITO'
        with self.assertRaises(ValueError): service.Document.model_validate(doc)
        doc['comprobante'].update(factura_original='00001-00000001', motivo_credito='diferencia_precio')
        service.Document.model_validate(doc)
        doc['comprobante']['motivo_credito'] = 'devolucion'
        with self.assertRaises(ValueError): service.Document.model_validate(doc)

    def test_internal_has_no_taxes(self):
        o = MagicMock()
        self.assertEqual(service.loader.resolver_impuestos(o, {'comprobante':{'clase':'interno'}}, {}, MagicMock()), {'por_alicuota':{},'percepciones':[]})
        o.call.assert_not_called()

    def test_credit_only_creates_linked_draft(self):
        doc = document(); doc['comprobante'].update(tipo='NOTA_CREDITO', factura_original='00001-00000001', motivo_credito='diferencia_precio')
        o = MagicMock()
        o.buscar_leer.return_value = [{'id':42,'ref':'00001-00000001','amount_total':99999999}]
        o.uno.return_value = {'state':'draft','move_type':'in_refund','amount_total':doc['totales']['total'],'reversed_entry_id':[42,'Original']}
        states = []
        with patch.object(service.loader,'crear_factura',return_value=43) as create:
            refunds.process_credit(o,doc,b'pdf',lambda **kw:states.append(kw),{'id':4},[],{}, {},{},service.loader.Informe(),1)
        self.assertEqual(create.call_args.kwargs, {'original_id':42})
        self.assertEqual(create.call_args.args[7:9], (None,False))
        o.call.assert_not_called()
        self.assertEqual(o.crear.call_args.args[0], 'ir.attachment')
        self.assertEqual(states[-1]['state'], 'borrador')

    def test_missing_original_stops_before_mutation(self):
        doc = document(); doc['comprobante']['factura_original'] = '00001-00000001'
        o=MagicMock(); o.buscar_leer.return_value=[]
        with self.assertRaises(service.loader.Frenar):
            refunds.process_credit(o,doc,b'pdf',MagicMock(),{'id':4},[],{},{},{},service.loader.Informe(),1)
        o.crear.assert_not_called(); o.call.assert_not_called()
