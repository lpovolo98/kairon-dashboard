import unittest
from unittest.mock import MagicMock
from administracion.cargar import crear_factura, Informe

class PurchaseLinks(unittest.TestCase):
    def test_repeated_product_links_to_distinct_purchase_lines(self):
        o=MagicMock(); o.primer_campo.return_value='product_uom_id'; o.tiene.return_value=True
        o.buscar_leer.return_value=[{'id':101,'product_id':[7,'Product']},{'id':102,'product_id':[7,'Product']}]
        o.crear.return_value=20
        o.uno.side_effect=lambda model,*args: {'name':'P001'} if model=='purchase.order' else {'name':'Draft','state':'draft','amount_total':242,'amount_untaxed':200,'amount_tax':42}
        doc={'comprobante':{'fecha':'2026-09-07','numero':'00012-00005670'},'totales':{'total':242}}
        line={'product_id':7,'producto':{'name':'Product'},'cantidad':1,'precio_unitario':100,'uom_id':1,'factura':{'alicuota_iva':21}}
        crear_factura(o,doc,{'id':4},[line,line],{'por_alicuota':{21:67},'percepciones':[]},{'id':10},{},9,True,Informe())
        vals=o.crear.call_args.args[1]
        self.assertEqual([l[2]['purchase_line_id'] for l in vals['invoice_line_ids']],[101,102])
        self.assertEqual(vals['invoice_origin'],'P001')
        o.call.assert_not_called()
