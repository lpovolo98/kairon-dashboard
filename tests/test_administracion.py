import copy
import io
import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pypdf import PdfWriter
from administracion import api, service


def document():
    data = json.loads((service.ROOT / 'ejemplo.json').read_text('utf-8'))
    data.pop('_origen'); data.pop('_nota'); data['comprobante'].pop('codigo_afip')
    data['comprador_cuit'] = '30-12345678-9'
    return data


class Workflow(unittest.TestCase):
    def test_reject_nonfinite(self):
        data = document(); data['lineas'][0]['precio_unitario'] = float('nan')
        with self.assertRaises(ValueError): service.Document.model_validate(data)

    def test_reject_uncertain_reading(self):
        data = document(); data['dudas'] = ['Cantidad ilegible']
        with self.assertRaises(ValueError): service.Document.model_validate(data)

    def test_reject_wrong_quantity(self):
        data = document(); data['lineas'][0]['cantidad_unidades'] = 3
        with self.assertRaises(ValueError): service.Document.model_validate(data)

    def test_reject_wrong_total_before_connection(self):
        data = document(); data['totales']['total'] += 30
        o = MagicMock()
        with self.assertRaises(service.loader.Frenar):
            service.process(data, b'pdf', MagicMock(), o=o)
        o.uno.assert_not_called()

    def run_workflow(self, delta=0, price_alert=False, duplicate=False, buyer='30-12345678-9', post=True, existing=None):
        o = MagicMock(); o.uid = 1
        o.buscar_leer.return_value = existing or []
        def uno(model, *args):
            if model == 'res.users': return {'company_id':[1, 'Kairon']}
            if model == 'res.company': return {'vat':buyer, 'currency_id':[1,'ARS']}
            if model == 'res.currency': return {'name':'ARS'}
            if model == 'account.journal': return {'company_id':[1,'Kairon'], 'currency_id':False}
            if model == 'purchase.order': return {'name':'P00011','state':'purchase','picking_ids':[22]}
            return {'amount_total':document()['totales']['total'] + delta, 'state':'posted' if post else 'draft', 'name':'BILL/1'}
        o.uno.side_effect = uno
        states = []
        with ExitStack() as stack:
            stack.enter_context(patch('administracion.suppliers.resolve', return_value={'id':4}))
            for name, value in [('resolver_proveedor', {'id':4}), ('resolver_lineas', []),
                                ('elegir_diario', {'id':10}), ('resolver_impuestos', {}),
                                ('crear_orden_compra', 11), ('crear_factura', 12)]:
                stack.enter_context(patch.object(service.loader, name, return_value=value))
            dup = stack.enter_context(patch.object(service.loader, 'verificar_duplicado'))
            if duplicate: dup.side_effect = service.loader.Frenar('Duplicada')
            control = stack.enter_context(patch.object(service.loader, 'control_precios'))
            if price_alert: control.side_effect = lambda o,l,p,c,inf: inf.alerta('Cambió el precio')
            try:
                service.process(document(), b'pdf', lambda **kw: states.append(kw), o=o, post=post)
            except service.loader.Frenar:
                pass
        return o, states

    def test_posts_when_total_matches_and_attaches_pdf(self):
        o, states = self.run_workflow()
        o.call.assert_any_call('purchase.order', 'button_confirm', [[11]])
        o.call.assert_any_call('account.move', 'action_post', [[12]])
        self.assertEqual(o.crear.call_args.args[0], 'ir.attachment')
        self.assertEqual(states[-1]['state'], 'completado')

    def test_total_difference_keeps_draft(self):
        o, states = self.run_workflow(delta=2)
        self.assertNotIn('action_post',[c.args[1] for c in o.call.call_args_list])
        self.assertEqual(states[-1]['state'], 'revision')

    def test_draft_mode_never_posts(self):
        o, states = self.run_workflow(post=False)
        o.call.assert_called_once_with('purchase.order', 'button_confirm', [[11]])
        self.assertEqual(states[-1]['state'], 'borrador')

    def test_existing_invoice_returns_link_without_writes(self):
        o, states = self.run_workflow(post=False, existing=[{'id':5363,'ref':'A0003-00000018','state':'posted','amount_total':725379.99}])
        o.call.assert_not_called(); o.crear.assert_not_called()
        self.assertEqual(states[-1]['state'], 'existente')
        self.assertEqual(states[-1]['move_id'], 5363)

    def test_duplicate_stops_before_writes(self):
        o, states = self.run_workflow(duplicate=True)
        self.assertFalse(states); o.crear.assert_not_called(); o.call.assert_not_called()

    def test_price_alert_stops_before_writes(self):
        o, states = self.run_workflow(price_alert=True)
        self.assertFalse(states); o.crear.assert_not_called(); o.call.assert_not_called()

    def test_wrong_recipient_stops_before_writes(self):
        o, states = self.run_workflow(buyer='30-00000000-0')
        self.assertFalse(states); o.crear.assert_not_called(); o.call.assert_not_called()


class API(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'ADMIN_DATA_DIR':self.tmp.name,
            'ADMIN_ACCESS_TOKEN':'test-secret', 'OPENAI_API_KEY':'fake', 'ADMIN_MODEL':'fake',
            'ODOO_URL':'https://example.odoo.com', 'ODOO_DB':'fake', 'ODOO_USER':'fake', 'ODOO_PASSWORD':'fake'})
        self.env.start()
        app = FastAPI(); app.include_router(api.router)
        self.client = TestClient(app)
        self.headers = {'Authorization':'Bearer test-secret','Content-Type':'application/pdf'}

    def tearDown(self):
        self.client.close(); self.env.stop(); self.tmp.cleanup()

    def test_unauthorized(self):
        self.assertEqual(self.client.get('/api/administracion/facturas').status_code, 401)

    def test_forged_cloudflare_header_not_trusted(self):
        self.assertEqual(self.client.get('/api/administracion/facturas', headers={'cf-access-authenticated-user-email':'admin@example.com'}).status_code, 401)

    def test_invalid_pdf(self):
        response = self.client.post('/api/administracion/facturas', headers=self.headers, content=b'not a PDF')
        self.assertEqual(response.status_code, 422)

    def test_duplicate_pdf_and_restart(self):
        writer = PdfWriter(); writer.add_blank_page(width=100, height=100)
        stream = io.BytesIO(); writer.write(stream)
        with patch.object(api.executor, 'submit') as submit:
            first = self.client.post('/api/administracion/facturas', headers=self.headers, content=stream.getvalue())
            second = self.client.post('/api/administracion/facturas', headers=self.headers, content=stream.getvalue())
            self.assertEqual(first.status_code, 202); self.assertEqual(second.status_code, 409)
            submit.assert_called_once()
        api.recover()
        history = self.client.get('/api/administracion/facturas', headers=self.headers).json()
        self.assertEqual(history[0]['state'], 'resultado_incierto')

    def test_partial_write_is_uncertain(self):
        with api.connect() as db:
            db.execute('INSERT INTO jobs VALUES (?,?,?)', ('test','administracion',json.dumps({'state':'recibido'})))
        def partial(doc, pdf, save):
            save(state='creando_factura', purchase_id=11)
            raise TimeoutError()
        with patch.object(service, 'extract', return_value=document()), patch.object(service, 'process', side_effect=partial):
            api.work('test', b'pdf')
        rows = self.client.get('/api/administracion/facturas', headers=self.headers).json()
        self.assertEqual(rows[0]['state'], 'resultado_incierto')
        self.assertEqual(rows[0]['purchase_id'], 11)


if __name__ == '__main__': unittest.main()
