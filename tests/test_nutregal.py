import unittest
from pathlib import Path
from administracion.nutregal_pdf import parse
from administracion.service import Document, loader


class Nutregal(unittest.TestCase):
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
