import unittest
from unittest.mock import Mock
from pathlib import Path
from administracion.nutregal_pdf import parse
from administracion.service import Document, loader


class Nutregal(unittest.TestCase):
    def test_confirmed_box_has_real_stock_conversion(self):
        o = Mock()
        o.uno.return_value = {'name':'Caja','factor':16,'relative_uom_id':[1,'Units']}
        cfg = {'unidades_compra_por_proveedor':{'30718495896':{'16':30}}}
        doc = {'proveedor':{'cuit':'30-71849589-6'}}
        self.assertEqual(loader.unidad_compra_confirmada(o,doc,cfg,16,1,1),(30,'Caja',16))
        o.uno.return_value['factor'] = 12
        with self.assertRaises(loader.Frenar):
            loader.unidad_compra_confirmada(o,doc,cfg,16,1,1)
        with self.assertRaises(loader.Frenar):
            loader.unidad_compra_confirmada(o,doc,cfg,24,1,1)

    def setUp(self):
        self.text = (Path(__file__).parent/'fixtures/nutregal.txt').read_text('utf-8')

    def test_invoice_quantities_and_taxes(self):
        doc = Document.model_validate(parse(self.text)).model_dump(mode='json', exclude_none=True)
        loader.validar_aritmetica(doc, loader.Informe())
        self.assertEqual(len(doc['lineas']), 8)
        self.assertEqual(sum(p['cantidad_unidades'] for p in doc['lineas']), 1280)
        self.assertEqual(doc['totales']['total'], 2487643.87)
        self.assertEqual(doc['totales']['percepciones'][0]['importe'], 60184.93)
        self.assertEqual(doc['comprador_cuit'], '30-71912311-9')

    def test_unknown_row_is_not_omitted(self):
        with self.assertRaises(ValueError):
            parse(self.text.replace('201700701', 'OTHER-CODE'))

    def test_discount_column_requires_review(self):
        with self.assertRaises(ValueError):
            parse(self.text.replace('1786,979', '1786,979 5', 1))

    def test_total_mismatch_stops_before_odoo(self):
        doc = parse(self.text.replace('2487643,87', '2487640,87'))
        with self.assertRaises(loader.Frenar):
            loader.validar_aritmetica(doc, loader.Informe())
