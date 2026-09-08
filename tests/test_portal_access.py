import os
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
import main

class PortalAccess(unittest.TestCase):
    def test_unsigned_access_is_denied(self):
        with TestClient(main.app) as client:
            response=client.get('/api/administracion/status')
            self.assertEqual(response.status_code,403)

    def test_verified_portal_identity_can_use_agent(self):
        with patch.object(main,'verificar_token_access',return_value={'email':'operator@example.com','sub':'test'}), patch.dict(os.environ,{'ADMIN_ALLOWED_EMAILS':''}):
            with TestClient(main.app) as client:
                response=client.get('/api/administracion/status',headers={'cf-access-jwt-assertion':'test-token'})
                self.assertEqual(response.status_code,200)
                self.assertEqual(response.json()['mode'],'draft')

    def test_optional_admin_allowlist_is_enforced(self):
        with patch.object(main,'verificar_token_access',return_value={'email':'other@example.com','sub':'test'}), patch.dict(os.environ,{'ADMIN_ALLOWED_EMAILS':'operator@example.com'}):
            with TestClient(main.app) as client:
                response=client.get('/api/administracion/status',headers={'cf-access-jwt-assertion':'test-token'})
                self.assertEqual(response.status_code,403)
