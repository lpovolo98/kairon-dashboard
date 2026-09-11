import os,tempfile,unittest
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from comercial import api

class CommercialAccessTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.env=patch.dict(os.environ,{'COMERCIAL_DATA_DIR':self.temp.name,'COMERCIAL_ALLOWED_EMAILS':''});self.env.start()
        app=FastAPI();app.include_router(api.router)
        @app.middleware('http')
        async def identity(request,call_next):
            if request.headers.get('x-test-owner'):request.state.usuario={'email':request.headers['x-test-owner']}
            return await call_next(request)
        self.client=TestClient(app)
        api.save({'id':'one','owner':'a@test','state':'review','files':[],'campaign':{},'created_at':'2026-09-09'})
    def tearDown(self):self.client.close();self.env.stop();self.temp.cleanup()
    def test_anonymous_rejected(self):self.assertEqual(self.client.get('/api/comercial/ediciones').status_code,401)
    def test_private_draft_not_visible_to_other_owner(self):
        self.assertEqual(self.client.get('/api/comercial/ediciones',headers={'x-test-owner':'b@test'}).json(),[])
        self.assertEqual(self.client.get('/api/comercial/ediciones/one/archivo/catalogo.pdf',headers={'x-test-owner':'b@test'}).status_code,404)
    def test_drafts_not_in_library(self):self.assertEqual(self.client.get('/api/comercial/publicados',headers={'x-test-owner':'a@test'}).json(),[])
    def test_approval_requires_pack_review(self):
        self.assertEqual(self.client.post('/api/comercial/ediciones/one/aprobar',headers={'x-test-owner':'a@test'}).status_code,422)
    def test_settings_invalid_divisor(self):
        r=self.client.post('/api/comercial/equivalencias',json={'divisors':{'1':0}},headers={'x-test-owner':'a@test'})
        self.assertEqual(r.status_code,422)
    def test_settings_persist(self):
        self.client.post('/api/comercial/equivalencias',json={'divisors':{'198':5},'confirmed':True},headers={'x-test-owner':'a@test'})
        self.assertEqual(self.client.get('/api/comercial/equivalencias',headers={'x-test-owner':'a@test'}).json()['divisors'],{'198':5})
