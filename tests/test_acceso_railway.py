"""La URL de Railway no pasa por Cloudflare, asi que llega sin el JWT de
Access. Estos tests fijan que por esa puerta no se entre a nada, y que las
dos rutas que tienen que seguir abiertas sigan abiertas."""
import unittest
from fastapi.testclient import TestClient
import main

PRIVADAS = ["/", "/portal", "/dashboard", "/administracion",
            "/static/index.html", "/static/portal.html",
            "/api/stock", "/api/me", "/api/mapa/clientes"]


class AccesoSinCloudflare(unittest.TestCase):
    def test_access_queda_configurado_por_default(self):
        # Si esto se rompe, el middleware falla abierto y no bloquea nada.
        self.assertTrue(main.access_configurado())

    def test_rutas_privadas_dan_403(self):
        with TestClient(main.app) as c:
            for ruta in PRIVADAS:
                self.assertEqual(c.get(ruta).status_code, 403, ruta)

    def test_token_invalido_no_pasa(self):
        with TestClient(main.app) as c:
            self.assertEqual(
                c.get("/", headers={"cf-access-jwt-assertion": "no.es.un.jwt"}).status_code, 403)
            self.assertEqual(
                c.get("/", cookies={"CF_Authorization": "tampoco.es.un.jwt"}).status_code, 403)

    def test_status_sigue_publico_y_dice_si_access_esta_activo(self):
        # Es el healthcheck de Railway y el unico modo de ver desde afuera
        # que version esta corriendo.
        with TestClient(main.app) as c:
            r = c.get("/api/status")
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.json()["access"]["activo"])


if __name__ == "__main__":
    unittest.main()
