import base64,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch,MagicMock
from comercial import creative

class FullFlyers(unittest.TestCase):
    def setUp(self):
        self.p=[{'id':205,'sku':'S1','name':'Wakas Maiz 400g','price':3250,'pack':12}]
        self.a={'title':'Wakas','products':[205],'kind':'discount','unit':'units',
                'tiers':[{'min':6,'discount':3},{'min':12,'discount':5},{'min':24,'discount':7}]}
        self.c={'start':'2026-09-01','end':'2026-09-30'}

    def test_exact_prices_and_thresholds(self):
        data=creative.offer_data(self.a,self.p,self.c)['productos'][0]
        self.assertEqual([t['precio_por_unidad'] for t in data['escalas']],['$ 3.152,50','$ 3.087,50','$ 3.022,50'])
        self.assertEqual(data['escalas'][0]['minimo'],'Desde 6 unidades')

    def test_edits_receives_real_images_and_references(self):
        with tempfile.TemporaryDirectory() as folder,patch.dict(os.environ,{'OPENAI_API_KEY':'test','COMERCIAL_IMAGE_MODEL':'test-model'}),patch.object(creative.httpx,'Client') as client:
            p=Path(folder)/'product.png';p.write_bytes(b'input')
            response=MagicMock();response.is_success=True;response.json.return_value={'data':[{'b64_json':base64.b64encode(b'output').decode()}]}
            client.return_value.__enter__.return_value.post.return_value=response
            result=creative.generate_flyer(self.a,self.p,Path(folder)/'result.png',{205:p},self.c)
            call=client.return_value.__enter__.return_value.post.call_args
            self.assertTrue(call.args[0].endswith('/images/edits'))
            self.assertEqual(len(call.kwargs['files']),4)
            self.assertIn('$ 3.152,50',call.kwargs['data']['prompt'])
            self.assertEqual(result['render_mode'],'full_flyer_v1')

    def test_missing_photo_stops_before_paid_call(self):
        with patch.dict(os.environ,{'OPENAI_API_KEY':'test','COMERCIAL_IMAGE_MODEL':'test-model'}),patch.object(creative.httpx,'Client') as client:
            with self.assertRaises(ValueError):creative.generate_flyer(self.a,self.p,'unused',{},self.c)
            client.assert_not_called()
