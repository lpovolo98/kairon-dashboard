"""Pruebas del agente de productos, contra un Odoo en memoria.

Lo que se busca demostrar, en orden de importancia:
  1. Previsualizar NUNCA escribe.
  2. Una fila sin cambios no se toca.
  3. Los tres problemas reales que ya documentaba cargar_catalogo.py
     —SKU con espacios, nombres ambiguos, impuestos homónimos— siguen
     resueltos después del port.
  4. Toda corrida se puede revertir.
"""

import base64
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from productos import cargar, imagenes            # noqa: E402
from tests.odoo_falso import OdooFalso            # noqa: E402

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64).decode()


def base():
    """Un Odoo chico pero realista: dos categorías, dos unidades, el IVA
    repetido en venta y compra (la ambigüedad más común), y tres productos
    ya cargados."""
    o = OdooFalso()
    cat_pan  = o.sembrar("product.category", {"name": "Panificados"})
    o.sembrar("product.category", {"name": "Galletitas"})
    uom_un   = o.sembrar("uom.uom", {"name": "Unidades"})
    o.sembrar("uom.uom", {"name": "Cajas"})
    iva_v    = o.sembrar("account.tax", {"name": "IVA 21%", "type_tax_use": "sale"})
    iva_c    = o.sembrar("account.tax", {"name": "IVA 21%", "type_tax_use": "purchase"})
    prov     = o.sembrar("res.partner", {"name": "BIO ALIMENTOS SA", "supplier_rank": 1})
    lista    = o.sembrar("product.pricelist", {"name": "Mayorista"})
    p1 = o.sembrar("product.template", {
        "name": "Pan Molde Clásico x500g", "default_code": "400001", "categ_id": cat_pan,
        "uom_id": uom_un, "x_studio_unidades_por_caja": 12, "list_price": 2890.0,
        "standard_price": 1740.0, "weight": 0.5, "taxes_id": [iva_v], "available_in_pos": True})
    p2 = o.sembrar("product.template", {
        "name": "Pan de Campo x600g", "default_code": "400005", "categ_id": cat_pan,
        "uom_id": uom_un, "list_price": 3180.0, "standard_price": 1990.0})
    # Un código cargado con un espacio pegado: problema real de esta base.
    p3 = o.sembrar("product.template", {
        "name": "Chipá x400g", "default_code": "400013 ", "categ_id": cat_pan, "uom_id": uom_un})
    return o, dict(cat=cat_pan, uom=uom_un, iva_v=iva_v, iva_c=iva_c,
                   prov=prov, lista=lista, p1=p1, p2=p2, p3=p3)


def fila(sku, **kw):
    f = {"Referencia interna (SKU)": sku}
    f.update(kw)
    return f


class Previsualizacion(unittest.TestCase):

    def test_separa_alta_modificacion_y_sin_cambios(self):
        o, d = base()
        r = cargar.previsualizar(o, {"productos": [
            # idéntico al que ya está: no debe contar como cambio
            fila("400001", **{"Nombre": "Pan Molde Clásico x500g", "Categoría": "Panificados",
                              "Unidad": "Unidades", "Unidades por caja": 12,
                              "Precio de venta": 2890, "Costo": 1740, "Peso (kg)": 0.5}),
            # sube el precio
            fila("400005", **{"Nombre": "Pan de Campo x600g", "Categoría": "Panificados",
                              "Unidad": "Unidades", "Unidades por caja": 10, "Precio de venta": 3480}),
            # no existe
            fila("400099", **{"Nombre": "Pan Nuevo", "Categoría": "Panificados",
                              "Unidad": "Unidades", "Unidades por caja": 8, "Precio de venta": 1000}),
        ]})
        estados = {f["sku"]: f["estado"] for f in r["productos"]}
        self.assertEqual(estados, {"400001": "igual", "400005": "modificacion", "400099": "alta"})

        mod = next(f for f in r["productos"] if f["sku"] == "400005")
        self.assertEqual(mod["campos"]["Precio de venta"]["viejo"], 3180)
        self.assertEqual(mod["campos"]["Precio de venta"]["nuevo"], 3480)
        # El nombre no cambió: no lleva "viejo" y no se va a escribir.
        self.assertNotIn("viejo", mod["campos"]["Nombre"])

    def test_previsualizar_nunca_escribe(self):
        """La garantía más importante de la pantalla."""
        o, d = base()
        cargar.previsualizar(o, {
            "productos": [fila("400001", **{"Nombre": "Otro nombre", "Precio de venta": 99}),
                          fila("400077", **{"Nombre": "Nuevo", "Categoría": "Panificados",
                                            "Unidad": "Unidades", "Unidades por caja": 5})],
            "proveedores": [{"SKU (igual al de Productos)": "400001",
                             "Proveedor": "BIO ALIMENTOS SA", "Código del proveedor": "X", "Precio": 10}],
            "precios": [{"SKU (igual al de Productos)": "400001",
                         "Lista de precios": "Mayorista", "Precio fijo": 50}],
        })
        self.assertEqual(o.escrituras, [], "la previsualización escribió en Odoo")

    def test_solo_lectura_revienta_si_alguien_intenta_escribir(self):
        o, d = base()
        lector = cargar.SoloLectura(o)
        with self.assertRaises(AssertionError):
            lector.call("product.template", "write", [[d["p1"]], {"name": "x"}])
        self.assertEqual(o.escrituras, [])

    def test_sku_con_espacios_no_crea_duplicado(self):
        """En Odoo está como '400013 '. Buscarlo exacto no lo encontraría y se
        crearía un segundo producto con el mismo código a la vista."""
        o, d = base()
        r = cargar.previsualizar(o, {"productos": [
            fila("400013", **{"Nombre": "Chipá x400g", "Precio de venta": 4200})]})
        f = r["productos"][0]
        self.assertEqual(f["estado"], "modificacion")
        self.assertEqual(f["_id"], d["p3"])

    def test_sku_ambiguo_frena(self):
        o, d = base()
        o.sembrar("product.template", {"name": "Duplicado", "default_code": " 400001"})
        r = cargar.previsualizar(o, {"productos": [fila("400001", **{"Nombre": "X"})]})
        f = r["productos"][0]
        self.assertEqual(f["estado"], "error")
        self.assertIn("coincide", f["errores"][0])

    def test_categoria_inexistente_sugiere_la_parecida(self):
        o, d = base()
        r = cargar.previsualizar(o, {"productos": [
            fila("400099", **{"Nombre": "X", "Categoría": "Panificado", "Unidad": "Unidades",
                              "Unidades por caja": 1})]})
        f = r["productos"][0]
        self.assertEqual(f["estado"], "error")
        self.assertIn("¿Quisiste decir: Panificados?", " ".join(f["errores"]))

    def test_impuesto_homonimo_se_resuelve_por_alcance(self):
        """«IVA 21%» existe en venta y en compra. Si la caché no distingue el
        alcance, el segundo hereda el id del primero sin ningún aviso."""
        o, d = base()
        r = cargar.previsualizar(o, {"productos": [
            fila("400099", **{"Nombre": "X", "Categoría": "Panificados", "Unidad": "Unidades",
                              "Unidades por caja": 1, "Impuestos de venta": "IVA 21%",
                              "Impuestos de compra": "IVA 21%"})]})
        f = r["productos"][0]
        self.assertEqual(f["errores"], [])
        self.assertEqual(f["_escribir"]["taxes_id"], [(6, 0, [d["iva_v"]])])
        self.assertEqual(f["_escribir"]["supplier_taxes_id"], [(6, 0, [d["iva_c"]])])
        self.assertNotEqual(d["iva_v"], d["iva_c"])

    def test_id_explicito_desambigua(self):
        o, d = base()
        otra = o.sembrar("product.category", {"name": "Panificados"})   # homónima
        r = cargar.previsualizar(o, {"productos": [
            fila("400099", **{"Nombre": "X", "Categoría": f"Panificados (id {otra})",
                              "Unidad": "Unidades", "Unidades por caja": 1})]})
        self.assertEqual(r["productos"][0]["errores"], [])
        self.assertEqual(r["productos"][0]["_escribir"]["categ_id"], otra)

    def test_numeros_de_excel_no_generan_cambios_falsos(self):
        """1740, '1740', '1740.00' y '1.740,00' son el mismo costo. Sin esto
        toda fila parece modificada y se reescribe el catálogo entero."""
        o, d = base()
        for valor in (1740, 1740.0, "1740", "1740.00", "1.740,00"):
            r = cargar.previsualizar(o, {"productos": [
                fila("400001", **{"Costo": valor})]})
            self.assertEqual(r["productos"][0]["estado"], "igual", f"falló con {valor!r}")

    def test_fila_parcial_modifica_solo_lo_que_trae(self):
        """Pegar dos columnas desde Excel para corregir un precio es un caso
        normal. Las columnas ausentes significan «no lo toques», no «lo estás
        borrando»: exigir Nombre ahí convertía toda modificación en un error."""
        o, d = base()
        r = cargar.previsualizar(o, {"productos": [fila("400005", **{"Precio de venta": 3480})]})
        f = r["productos"][0]
        self.assertEqual(f["estado"], "modificacion")
        self.assertEqual(f["errores"], [])
        self.assertEqual(list(f["_escribir"]), ["list_price"], "tocó campos que no venían en la fila")

    def test_columna_requerida_vacia_de_forma_explicita_si_es_error(self):
        o, d = base()
        r = cargar.previsualizar(o, {"productos": [fila("400005", **{"Nombre": ""})]})
        self.assertEqual(r["productos"][0]["estado"], "error")
        self.assertIn("Falta «Nombre»", " ".join(r["productos"][0]["errores"]))

    def test_sku_repetido_en_el_archivo(self):
        o, d = base()
        r = cargar.previsualizar(o, {"productos": [
            fila("400099", **{"Nombre": "A"}), fila("400099", **{"Nombre": "B"})]})
        self.assertEqual(r["productos"][1]["estado"], "error")
        self.assertIn("más de una vez", r["productos"][1]["errores"][0])

    def test_falta_unidades_por_caja_avisa_en_altas(self):
        o, d = base()
        r = cargar.previsualizar(o, {"productos": [
            fila("400099", **{"Nombre": "X", "Categoría": "Panificados",
                              "Unidad": "Unidades", "Unidades por caja": ""})]})
        self.assertEqual(r["productos"][0]["estado"], "error")
        self.assertIn("1 unidad = 1 caja", " ".join(r["productos"][0]["errores"]))

    def test_proveedor_de_producto_inexistente_frena(self):
        o, d = base()
        r = cargar.previsualizar(o, {"proveedores": [
            {"SKU (igual al de Productos)": "999999", "Proveedor": "BIO ALIMENTOS SA",
             "Código del proveedor": "X", "Precio": 1}]})
        self.assertEqual(r["proveedores"][0]["estado"], "error")


class Escritura(unittest.TestCase):

    def test_no_escribe_las_filas_sin_cambios(self):
        o, d = base()
        r = cargar.aplicar(o, {"productos": [
            fila("400001", **{"Nombre": "Pan Molde Clásico x500g", "Costo": 1740}),   # igual
            fila("400005", **{"Nombre": "Pan de Campo x600g", "Precio de venta": 3480}),
        ]})
        self.assertEqual(len(r["modificados"]), 1)
        self.assertEqual(r["modificados"][0]["sku"], "400005")
        tocados = [a[2][0] for a in o.escrituras if a[1] == "write"]
        self.assertNotIn([d["p1"]], tocados, "se escribió un producto sin cambios")

    def test_alta_aplica_los_fijos_de_kairon(self):
        o, d = base()
        cargar.aplicar(o, {"productos": [
            fila("400099", **{"Nombre": "Pan Nuevo", "Categoría": "Panificados",
                              "Unidad": "Unidades", "Unidades por caja": 8, "Precio de venta": 1000})]})
        nuevo = next(r for r in o.datos["product.template"].values()
                     if r.get("default_code") == "400099")
        self.assertEqual(nuevo["type"], "consu")
        self.assertTrue(nuevo["is_storable"])
        self.assertEqual(nuevo["invoice_policy"], "order")
        self.assertEqual(nuevo["x_studio_unidades_por_caja"], 8.0)

    def test_frena_sin_escribir_si_hay_una_sola_fila_con_error(self):
        o, d = base()
        with self.assertRaises(cargar.Frenar):
            cargar.aplicar(o, {"productos": [
                fila("400099", **{"Nombre": "Bueno", "Categoría": "Panificados",
                                  "Unidad": "Unidades", "Unidades por caja": 1}),
                fila("400098", **{"Nombre": "Malo", "Categoría": "No existe",
                                  "Unidad": "Unidades", "Unidades por caja": 1}),
            ]})
        self.assertEqual(o.escrituras, [], "escribió pese al error")

    def test_tope_de_registros(self):
        o, d = base()
        filas = [fila(f"5000{i:02d}", **{"Nombre": f"P{i}", "Categoría": "Panificados",
                                         "Unidad": "Unidades", "Unidades por caja": 1})
                 for i in range(10)]
        with self.assertRaises(cargar.Frenar):
            cargar.aplicar(o, {"productos": filas}, tope=5)
        self.assertEqual(o.escrituras, [])

    def test_proveedores_crea_una_vez_y_despues_actualiza(self):
        o, d = base()
        hoja = [{"SKU (igual al de Productos)": "400001", "Proveedor": "BIO ALIMENTOS SA",
                 "Código del proveedor": "BIO-PM-500", "Precio": 1740, "Cantidad mínima": 12}]
        r1 = cargar.aplicar(o, {"proveedores": hoja})
        self.assertEqual(len(r1["creados"]), 1)

        r2 = cargar.aplicar(o, {"proveedores": hoja})            # idéntico
        self.assertEqual(r2["creados"], [])
        self.assertEqual(r2["modificados"], [])

        hoja[0]["Precio"] = 1890                                  # cambió el precio
        r3 = cargar.aplicar(o, {"proveedores": hoja})
        self.assertEqual(len(r3["modificados"]), 1)
        info = o.datos["product.supplierinfo"][r1["creados"][0]["id"]]
        self.assertEqual(info["price"], 1890.0)
        self.assertEqual(info["product_code"], "BIO-PM-500")

    def test_precios_crea_y_despues_actualiza_la_regla(self):
        o, d = base()
        hoja = [{"SKU (igual al de Productos)": "400001",
                 "Lista de precios": "Mayorista", "Precio fijo": 2600}]
        r1 = cargar.aplicar(o, {"precios": hoja})
        regla = o.datos["product.pricelist.item"][r1["creados"][0]["id"]]
        self.assertEqual(regla["applied_on"], "1_product")
        self.assertEqual(regla["compute_price"], "fixed")

        hoja[0]["Precio fijo"] = 2750
        r2 = cargar.aplicar(o, {"precios": hoja})
        self.assertEqual(len(r2["modificados"]), 1)
        self.assertEqual(o.datos["product.pricelist.item"][r1["creados"][0]["id"]]["fixed_price"], 2750.0)

    def test_producto_creado_en_la_misma_corrida_sirve_para_las_otras_hojas(self):
        o, d = base()
        r = cargar.aplicar(o, {
            "productos": [fila("400099", **{"Nombre": "Pan Nuevo", "Categoría": "Panificados",
                                            "Unidad": "Unidades", "Unidades por caja": 8})],
            "proveedores": [{"SKU (igual al de Productos)": "400099", "Proveedor": "BIO ALIMENTOS SA",
                             "Código del proveedor": "BIO-NUEVO", "Precio": 500}],
        })
        self.assertEqual(len(r["creados"]), 2)
        info = next(x for x in r["creados"] if x["hoja"] == "proveedores")
        prod = next(x for x in r["creados"] if x["hoja"] == "productos")
        self.assertEqual(o.datos["product.supplierinfo"][info["id"]]["product_tmpl_id"], prod["id"])


    def test_precio_de_un_producto_que_se_crea_en_la_misma_corrida(self):
        o, d = base()
        r = cargar.previsualizar(o, {
            "productos": [fila("400099", **{"Nombre": "Pan Nuevo", "Categoría": "Panificados",
                                            "Unidad": "Unidades", "Unidades por caja": 8})],
            "precios": [{"SKU (igual al de Productos)": "400099",
                         "Lista de precios": "Mayorista", "Precio fijo": 2600}],
        })
        # Todavía no existe en Odoo, pero va a existir: los productos se
        # escriben antes que las otras dos hojas.
        self.assertEqual(r["precios"][0]["estado"], "alta")
        self.assertEqual(r["precios"][0]["errores"], [])


class Revertir(unittest.TestCase):

    def test_devuelve_los_valores_previos_y_archiva_lo_creado(self):
        o, d = base()
        r = cargar.aplicar(o, {"productos": [
            fila("400005", **{"Nombre": "Pan de Campo x600g", "Precio de venta": 3480,
                              "Categoría": "Galletitas"}),
            fila("400099", **{"Nombre": "Pan Nuevo", "Categoría": "Panificados",
                              "Unidad": "Unidades", "Unidades por caja": 8}),
        ]})
        creado = r["creados"][0]["id"]
        self.assertEqual(o.datos["product.template"][d["p2"]]["list_price"], 3480.0)

        cargar.revertir(o, r["deshacer"])

        viejo = o.datos["product.template"][d["p2"]]
        self.assertEqual(viejo["list_price"], 3180.0)
        self.assertEqual(viejo["categ_id"], d["cat"], "el m2o no volvió a su valor previo")
        self.assertFalse(o.datos["product.template"][creado]["active"],
                         "el producto creado tendría que quedar archivado, no borrado")
        self.assertIn(creado, o.datos["product.template"], "no se debe borrar: se archiva")

    def test_revertir_restaura_los_impuestos(self):
        o, d = base()
        r = cargar.aplicar(o, {"productos": [
            fila("400001", **{"Impuestos de venta": ""})]})
        self.assertEqual(o.datos["product.template"][d["p1"]]["taxes_id"], [])
        cargar.revertir(o, r["deshacer"])
        self.assertEqual(o.datos["product.template"][d["p1"]]["taxes_id"], [d["iva_v"]])


class Imagenes(unittest.TestCase):

    def test_sube_y_guarda_la_anterior_para_revertir(self):
        o, d = base()
        o.datos["product.template"][d["p1"]]["image_1920"] = "FOTOVIEJA"
        r = imagenes.aplicar(o, [{"sku": "400001", "imagen": "data:image/png;base64," + PNG}])
        self.assertEqual(r["errores"], [])
        self.assertTrue(r["subidas"][0]["pisaba_una_foto"])
        self.assertEqual(o.datos["product.template"][d["p1"]]["image_1920"], PNG)

        cargar.revertir(o, r["deshacer"])
        self.assertEqual(o.datos["product.template"][d["p1"]]["image_1920"], "FOTOVIEJA")

    def test_acepta_base64_pelado_y_sku_con_espacios(self):
        o, d = base()
        r = imagenes.aplicar(o, [{"sku": " 400013 ", "imagen": PNG}])
        self.assertEqual(r["errores"], [])
        self.assertEqual(o.datos["product.template"][d["p3"]]["image_1920"], PNG)

    def test_rechaza_lo_que_no_es_una_imagen(self):
        o, d = base()
        pdf = base64.b64encode(b"%PDF-1.7 no soy una imagen").decode()
        r = imagenes.aplicar(o, [{"sku": "400001", "imagen": pdf}])
        self.assertEqual(r["subidas"], [])
        self.assertIn("no es una imagen", r["errores"][0])
        self.assertEqual(o.escrituras, [])

    def test_rechaza_una_imagen_por_encima_del_tope(self):
        o, d = base()
        gigante = base64.b64encode(b"\xff\xd8\xff" + b"\x00" * (imagenes.TOPE_BYTES + 10)).decode()
        r = imagenes.aplicar(o, [{"sku": "400001", "imagen": gigante}])
        self.assertIn("tope", r["errores"][0])
        self.assertEqual(o.escrituras, [])

    def test_sku_sin_producto_no_frena_a_los_demas(self):
        o, d = base()
        r = imagenes.aplicar(o, [{"sku": "999999", "imagen": PNG},
                                 {"sku": "400001", "imagen": PNG}])
        self.assertEqual(len(r["subidas"]), 1)
        self.assertEqual(len(r["errores"]), 1)

    def test_lista_de_productos_sin_foto(self):
        o, d = base()
        o.datos["product.template"][d["p1"]]["image_1920"] = PNG
        skus = [x["sku"] for x in imagenes.sin_imagen(o)]
        self.assertNotIn("400001", skus)
        self.assertIn("400005", skus)
        self.assertIn("400013", skus, "el SKU se devuelve sin el espacio pegado")


class Acceso(unittest.TestCase):
    """El módulo no tiene login propio: confía en el middleware del portal,
    que ya verificó la firma de Cloudflare."""

    def _cliente(self):
        from fastapi.testclient import TestClient
        import main
        return TestClient(main.app)

    def test_sin_identidad_no_se_entra(self):
        with self._cliente() as c:
            self.assertEqual(c.get("/api/productos/status").status_code, 403)
            self.assertEqual(c.post("/api/productos/aplicar", json={"productos": []}).status_code, 403)

    def test_con_identidad_verificada_responde(self):
        import main
        with patch.object(main, "verificar_token_access",
                          return_value={"email": "lucas@kaironsrl.com.ar", "sub": "t"}), \
             patch.dict(os.environ, {"PRODUCTOS_ALLOWED_EMAILS": ""}):
            with self._cliente() as c:
                r = c.get("/api/productos/status", headers={"cf-access-jwt-assertion": "t"})
                self.assertEqual(r.status_code, 200)
                self.assertIn("ready", r.json())

    def test_lista_blanca_opcional(self):
        import main
        with patch.object(main, "verificar_token_access",
                          return_value={"email": "otro@example.com", "sub": "t"}), \
             patch.dict(os.environ, {"PRODUCTOS_ALLOWED_EMAILS": "lucas@kaironsrl.com.ar"}):
            with self._cliente() as c:
                r = c.get("/api/productos/status", headers={"cf-access-jwt-assertion": "t"})
                self.assertEqual(r.status_code, 403)

    def test_el_cuerpo_no_puede_pedir_una_escritura_arbitraria(self):
        """El navegador manda filas, nunca escrituras: el servidor vuelve a
        previsualizar antes de tocar Odoo."""
        import main
        from productos import api
        o, d = base()
        with patch.object(main, "verificar_token_access",
                          return_value={"email": "lucas@kaironsrl.com.ar", "sub": "t"}), \
             patch.dict(os.environ, {"PRODUCTOS_ALLOWED_EMAILS": ""}), \
             patch.object(api, "_odoo", return_value=o):
            with self._cliente() as c:
                r = c.post("/api/productos/previsualizar",
                           json={"productos": [fila("400001", **{"Costo": 1740})]},
                           headers={"cf-access-jwt-assertion": "t"})
                self.assertEqual(r.status_code, 200)
                fila_r = r.json()["productos"][0]
                self.assertEqual(fila_r["estado"], "igual")
                # Las claves internas no salen al navegador.
                self.assertNotIn("_escribir", fila_r)
                self.assertNotIn("_id", fila_r)


if __name__ == "__main__":
    unittest.main(verbosity=2)
