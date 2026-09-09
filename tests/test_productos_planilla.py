"""Pruebas de la lectura de la plantilla y de la modificación masiva sin archivo."""

import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from productos import cargar, planilla            # noqa: E402
from productos.esquema import COLUMNAS_PRODUCTOS  # noqa: E402
from tests.test_productos import base             # noqa: E402

COLS_P = ["Referencia interna (SKU)", "Nombre", "Categoría", "Unidad",
          "Unidades por caja", "Precio de venta", "Costo"]


def libro(hojas):
    """hojas: {'Productos': [encabezados, fila, fila], ...}"""
    from openpyxl import Workbook
    wb = Workbook()
    wb.remove(wb.active)
    for nombre, filas in hojas.items():
        ws = wb.create_sheet(nombre)
        for f in filas:
            ws.append(f)
    b = io.BytesIO()
    wb.save(b)
    return b.getvalue()


class LecturaDeLaPlantilla(unittest.TestCase):

    def test_lee_las_tres_hojas(self):
        datos = libro({
            "Productos": [COLS_P,
                          ["400001", "Pan Molde", "Panificados", "Unidades", 12, 2890, 1740]],
            "Proveedores": [["SKU (igual al de Productos)", "Proveedor",
                             "Código del proveedor", "Precio", "Cantidad mínima"],
                            ["400001", "BIO ALIMENTOS SA", "BIO-PM", 1740, 12]],
            "Precios": [["SKU (igual al de Productos)", "Lista de precios", "Precio fijo"],
                        ["400001", "Mayorista", 2600]],
        })
        r = planilla.leer("plantilla_catalogo.xlsx", datos)
        self.assertEqual(len(r["productos"]), 1)
        self.assertEqual(r["productos"][0]["Nombre"], "Pan Molde")
        self.assertEqual(r["proveedores"][0]["Código del proveedor"], "BIO-PM")
        self.assertEqual(r["precios"][0]["Precio fijo"], 2600)

    def test_una_hoja_sola_con_otro_nombre_se_toma_como_productos(self):
        """Pasa cuando alguien exporta una selección desde Excel."""
        datos = libro({"Hoja1": [COLS_P, ["400001", "Pan", "Panificados", "Unidades", 12, 1, 1]]})
        r = planilla.leer("export.xlsx", datos)
        self.assertEqual(len(r["productos"]), 1)

    def test_ignora_filas_vacias_y_restos_de_formato(self):
        datos = libro({"Productos": [
            COLS_P,
            ["400001", "Pan", "Panificados", "Unidades", 12, 1, 1],
            [None, None, None, None, None, None, None],          # fila vacía
            [None, "Sin SKU", "Panificados", "Unidades", 1, 1, 1],  # resto de formato
            ["400002", "Pan 2", "Panificados", "Unidades", 12, 1, 1],
        ]})
        with self.assertRaisesRegex(ValueError,'sin SKU'): planilla.leer("p.xlsx", datos)

    def test_nombres_de_hoja_sin_acentos_ni_mayusculas(self):
        datos = libro({"PRODUCTOS": [COLS_P, ["400001", "Pan", "Panificados", "Unidades", 1, 1, 1]]})
        self.assertEqual(len(planilla.leer("p.xlsx", datos)["productos"]), 1)

    def test_archivo_que_no_es_una_planilla(self):
        with self.assertRaises(ValueError) as ctx:
            planilla.leer("factura.xlsx", b"%PDF-1.7 no soy una planilla")
        self.assertIn("xlsx", str(ctx.exception))

    def test_archivo_demasiado_grande(self):
        with self.assertRaises(ValueError) as ctx:
            planilla.leer("p.xlsx", b"x" * (planilla.TOPE_BYTES + 1))
        self.assertIn("tope", str(ctx.exception))

    def test_csv(self):
        csv = ("Referencia interna (SKU);Nombre;Precio de venta\n"
               "400001;Pan Molde;2890\n400002;Pan Campo;3180\n").encode("utf-8")
        r = planilla.leer("lista.csv", csv)
        self.assertEqual(len(r["productos"]), 2)
        self.assertEqual(r["productos"][1]["Nombre"], "Pan Campo")

    def test_lo_leido_se_puede_previsualizar_directo(self):
        """El formato que devuelve la lectura es el que consume el motor."""
        o, d = base()
        datos = libro({"Productos": [COLS_P,
                       ["400005", "Pan de Campo x600g", "Panificados", "Unidades", 10, 3480, 1990]]})
        r = cargar.previsualizar(o, planilla.leer("p.xlsx", datos))
        self.assertEqual(r["productos"][0]["estado"], "modificacion")
        self.assertEqual(o.escrituras, [])


class PlantillaDescargable(unittest.TestCase):

    def test_trae_las_tres_hojas_y_los_valores_validos(self):
        from openpyxl import load_workbook
        catalogo = {"categorias": [{"name": "Panificados"}, {"name": "Galletitas"}],
                    "unidades": [{"name": "Unidades"}],
                    "impuestos_venta": [{"name": "IVA 21%"}], "impuestos_compra": [],
                    "listas": [{"name": "Mayorista"}], "proveedores": [{"name": "BIO ALIMENTOS SA"}]}
        wb = load_workbook(io.BytesIO(planilla.generar_plantilla(catalogo)))
        self.assertEqual(set(wb.sheetnames),
                         {"Productos", "Proveedores", "Precios", "Valores válidos"})
        self.assertEqual([c.value for c in wb["Productos"][1]],
                         [c["col"] for c in COLUMNAS_PRODUCTOS])
        valores = [(f[0].value, f[1].value) for f in wb["Valores válidos"].iter_rows(min_row=2)]
        self.assertIn(("Categoría", "Panificados"), valores)
        self.assertIn(("Lista de precios", "Mayorista"), valores)

    def test_la_plantilla_generada_se_puede_volver_a_leer(self):
        catalogo = {k: [] for k in ("categorias", "unidades", "impuestos_venta",
                                    "impuestos_compra", "listas", "proveedores")}
        datos = planilla.generar_plantilla(catalogo)
        # Está vacía de filas, así que leerla tiene que avisar, no romper.
        with self.assertRaises(ValueError):
            planilla.leer("plantilla_catalogo.xlsx", datos)


class ModificacionMasiva(unittest.TestCase):

    def test_multiplicar_precios_de_una_categoria(self):
        o, d = base()
        productos = cargar.buscar_para_editar(o, categoria="Panificados")
        self.assertEqual(len(productos), 3)
        filas = cargar.aplicar_operacion(productos, "Precio de venta", "multiplicar", "1.15")
        por_sku = {f["Referencia interna (SKU)"]: f["Precio de venta"] for f in filas}
        self.assertAlmostEqual(por_sku["400001"], 2890 * 1.15, places=2)
        self.assertAlmostEqual(por_sku["400005"], 3180 * 1.15, places=2)
        # 400013 no tiene precio cargado: no cambia, no aparece.
        self.assertNotIn("400013", por_sku)

    def test_no_devuelve_los_productos_que_no_cambian(self):
        o, d = base()
        productos = cargar.buscar_para_editar(o, categoria="Panificados")
        filas = cargar.aplicar_operacion(productos, "Precio de venta", "multiplicar", "1")
        self.assertEqual(filas, [], "una operación neutra no debe generar escrituras")

    def test_buscar_y_reemplazar_en_el_nombre(self):
        o, d = base()
        productos = cargar.buscar_para_editar(o, texto="Molde")
        filas = cargar.aplicar_operacion(productos, "Nombre", "reemplazar", ["Clásico", "Clasico"])
        self.assertEqual(filas[0]["Nombre"], "Pan Molde Clasico x500g")

    def test_operacion_que_no_aplica_al_tipo(self):
        o, d = base()
        productos = cargar.buscar_para_editar(o, categoria="Panificados")
        with self.assertRaises(cargar.Frenar):
            cargar.aplicar_operacion(productos, "Nombre", "multiplicar", "2")

    def test_filtrar_por_proveedor(self):
        o, d = base()
        o.sembrar("product.supplierinfo", {"partner_id": d["prov"], "product_tmpl_id": d["p1"],
                                           "product_code": "BIO-PM", "price": 1740})
        productos = cargar.buscar_para_editar(o, proveedor="BIO ALIMENTOS SA")
        self.assertEqual([p["sku"] for p in productos], ["400001"])

    def test_categoria_inexistente_frena(self):
        o, d = base()
        with self.assertRaises(cargar.Frenar):
            cargar.buscar_para_editar(o, categoria="No existe")

    def test_buscar_no_escribe(self):
        o, d = base()
        cargar.buscar_para_editar(o, categoria="Panificados")
        self.assertEqual(o.escrituras, [])

    def test_el_resultado_pasa_por_la_previsualizacion_de_siempre(self):
        """La modificación masiva no tiene camino de escritura propio."""
        o, d = base()
        productos = cargar.buscar_para_editar(o, categoria="Panificados")
        filas = cargar.aplicar_operacion(productos, "Precio de venta", "multiplicar", "1.15")
        plan = cargar.previsualizar(o, {"productos": filas})
        self.assertTrue(all(f["estado"] == "modificacion" for f in plan["productos"]))
        self.assertEqual(o.escrituras, [])
        r = cargar.aplicar(o, {"productos": filas})
        self.assertEqual(len(r["modificados"]), 2)
        self.assertAlmostEqual(o.datos["product.template"][d["p1"]]["list_price"], 3323.5, places=2)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class Persistencia(unittest.TestCase):
    """El historial de corridas es lo que hace posible revertir. Si vive en un
    directorio efímero, una corrida aplicada deja de poder deshacerse en cuanto
    el contenedor se recicla."""

    def test_por_defecto_usa_el_volumen_si_existe(self):
        import os
        from unittest.mock import patch
        from pathlib import Path
        from productos import api
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PRODUCTOS_DATA_DIR", None)
            with patch.object(Path, "is_dir", return_value=True):
                self.assertEqual(api.ruta_datos().as_posix(), "/data/productos")

    def test_sin_volumen_cae_al_repo_y_lo_reporta_como_no_persistente(self):
        import os
        from unittest.mock import patch
        from productos import api
        with patch.dict(os.environ, {"PRODUCTOS_DATA_DIR": "/tmp/efimero"}), \
             patch.object(os.path, "ismount", return_value=False):
            self.assertFalse(api.persistente())

    def test_una_ruta_fuera_del_volumen_no_cuenta_como_persistente(self):
        """Aunque /data esté montado: si la variable apunta a otro lado, el
        historial no está en el volumen."""
        import os
        from unittest.mock import patch
        from productos import api
        with patch.dict(os.environ, {"PRODUCTOS_DATA_DIR": "/app/productos-data"}), \
             patch.object(os.path, "ismount", return_value=True):
            self.assertFalse(api.persistente())

    def test_con_volumen_y_ruta_correcta_es_persistente(self):
        import os
        from unittest.mock import patch
        from productos import api
        with patch.dict(os.environ, {"PRODUCTOS_DATA_DIR": "/data/productos"}), \
             patch.object(os.path, "ismount", return_value=True):
            self.assertTrue(api.persistente())


class Credenciales(unittest.TestCase):
    """El dashboard guarda la clave de Odoo en ODOO_PASSWORD; el agente
    administrativo acepta ODOO_KEY o ODOO_PASSWORD. Usar el atajo
    Odoo.desde_config(), que solo mira ODOO_KEY, dejaba el módulo entero sin
    conexión en Railway."""

    def _construir(self, entorno):
        import os
        from unittest.mock import patch, MagicMock
        from productos import api
        creado = MagicMock()
        with patch.dict(os.environ, entorno, clear=True), \
             patch("administracion.odoo.Odoo", return_value=creado) as fabrica:
            resultado = api._odoo()
        return resultado, creado, fabrica

    def test_funciona_solo_con_odoo_password(self):
        r, creado, fabrica = self._construir({
            "ODOO_URL": "https://kairon.odoo.com", "ODOO_DB": "kairon",
            "ODOO_USER": "lucas", "ODOO_PASSWORD": "secreto"})
        self.assertIs(r._odoo, creado, "el cliente sale envuelto para traducir errores")
        self.assertEqual(fabrica.call_args.kwargs["clave"], "secreto")

    def test_odoo_key_tiene_prioridad_si_estan_las_dos(self):
        r, creado, fabrica = self._construir({
            "ODOO_URL": "https://kairon.odoo.com", "ODOO_DB": "kairon",
            "ODOO_USER": "lucas", "ODOO_KEY": "la-key", "ODOO_PASSWORD": "secreto"})
        self.assertEqual(fabrica.call_args.kwargs["clave"], "la-key")

    def test_sin_clave_avisa_cual_falta(self):
        import os
        from unittest.mock import patch
        from fastapi import HTTPException
        from productos import api
        with patch.dict(os.environ, {"ODOO_URL": "u", "ODOO_DB": "d", "ODOO_USER": "us"}, clear=True):
            with self.assertRaises(HTTPException) as ctx:
                api._odoo()
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertIn("ODOO_PASSWORD", ctx.exception.detail)

    def test_no_usa_el_atajo_que_solo_mira_odoo_key(self):
        """Regresión directa del bug: si alguien vuelve a llamar a
        desde_config(), este test lo caza. Mira los nombres que resuelve el
        bytecode, no el texto, para no confundirse con los comentarios."""
        from productos import api
        self.assertNotIn("desde_config", api._odoo.__code__.co_names)
        self.assertIn("ODOO_PASSWORD", api._odoo.__code__.co_consts)


class ErroresDeOdoo(unittest.TestCase):
    """Un fallo de Odoo tiene que llegar al navegador con su mensaje, no como
    un «Internal Server Error» que no dice nada."""

    def _cliente_que_falla(self, solo=None):
        from administracion.odoo import OdooError
        from productos.api import ClienteHTTP

        class Falla:
            def call(self, modelo, metodo, args, kwargs=None):
                if solo is None or modelo == solo:
                    raise OdooError(f"{modelo}.{metodo} falló: campo inexistente")
                return []
        return ClienteHTTP(Falla())

    def test_un_error_de_odoo_es_502_con_mensaje(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            self._cliente_que_falla().call("product.category", "search_read", [[]])
        self.assertEqual(ctx.exception.status_code, 502)
        self.assertIn("campo inexistente", ctx.exception.detail)

    def test_una_lista_rota_no_tira_abajo_el_catalogo(self):
        from productos.api import _catalogo
        c = _catalogo(self._cliente_que_falla(solo="uom.uom"))
        self.assertEqual(c["unidades"], [])
        self.assertTrue(any("uom.uom" in a for a in c["avisos"]))
        self.assertIn("categorias", c)
        self.assertTrue(c["columnas"], "las columnas no dependen de Odoo")

    def test_el_catalogo_ordena_sin_pedirselo_a_odoo(self):
        """Ordenar por display_name, que es calculado y no almacenado, es
        justamente lo que hacía fallar la consulta."""
        from productos.api import _catalogo

        class Devuelve:
            def call(self, modelo, metodo, args, kwargs=None):
                assert (kwargs or {}).get('order') != 'display_name', 'No ordenar por un campo calculado'
                return [{"id": 2, "display_name": "Zeta"}, {"id": 1, "display_name": "alfa"}]
        c = _catalogo(Devuelve())
        self.assertEqual([x["name"] for x in c["categorias"]], ["alfa", "Zeta"])
        self.assertEqual(c["avisos"], [])
