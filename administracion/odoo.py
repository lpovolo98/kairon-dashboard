"""Cliente XML-RPC de Odoo para las herramientas de administración de Kairon.

Regla de oro de esta instancia (saas-18.4): nunca asumir que un campo existe.
Todo acceso a campos pasa por `campos()` / `tiene()` / `primer_campo()`, que
consultan `fields_get` una sola vez por modelo y lo cachean.
"""

from __future__ import annotations

import json
import os
import xmlrpc.client
from pathlib import Path

RAIZ = Path(__file__).resolve().parent


class TimedTransport(xmlrpc.client.SafeTransport):
    def make_connection(self, host):
        connection = super().make_connection(host)
        connection.timeout = 60
        return connection


class OdooError(RuntimeError):
    """Error de conexión o de datos contra Odoo."""


class Odoo:
    def __init__(self, url: str, db: str, usuario: str, clave: str):
        self.url = url.rstrip("/")
        self.db = db
        self.usuario = usuario
        self._clave = clave
        self._campos: dict[str, dict] = {}

        try:
            common = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/common", allow_none=True, transport=TimedTransport())
            self.version = common.version()
            self.uid = common.authenticate(db, usuario, clave, {})
        except Exception as e:  # noqa: BLE001
            raise OdooError(
                f"No se pudo hablar con {self.url}. "
                f"Si el error es 403 o un túnel rechazado, el host está fuera de la lista de "
                f"egreso del entorno desde el que estás corriendo esto — no es un problema de "
                f"credenciales.\nDetalle: {type(e).__name__}: {e}"
            ) from e

        if not self.uid:
            raise OdooError(
                f"Odoo rechazó las credenciales de {usuario} en la base {db}. "
                f"Revisá que la API key esté vigente en Preferencias → Seguridad de la cuenta."
            )

        self._models = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/object", allow_none=True, transport=TimedTransport())

    # ---------------------------------------------------------------- fábrica

    @classmethod
    def desde_config(cls, ruta: Path | None = None) -> "Odoo":
        """Toma las credenciales del entorno o de credenciales.json.

        Prioridad: variables de entorno > credenciales.json junto a este archivo.
        """
        datos = {
            "url": os.environ.get("ODOO_URL"),
            "db": os.environ.get("ODOO_DB"),
            "usuario": os.environ.get("ODOO_USER"),
            "clave": os.environ.get("ODOO_KEY"),
        }
        if not all(datos.values()):
            ruta = ruta or (RAIZ / "credenciales.json")
            if not ruta.exists():
                raise OdooError(
                    f"Faltan credenciales. Definí ODOO_URL / ODOO_DB / ODOO_USER / ODOO_KEY "
                    f"como variables de entorno, o creá {ruta} a partir de credenciales.ejemplo.json."
                )
            archivo = json.loads(ruta.read_text(encoding="utf-8"))
            datos = {k: datos[k] or archivo.get(k) for k in datos}

        faltan = [k for k, v in datos.items() if not v]
        if faltan:
            raise OdooError(f"Faltan estos datos de conexión: {', '.join(faltan)}")
        return cls(**datos)

    # ------------------------------------------------------------- primitivas

    def call(self, modelo: str, metodo: str, args: list, kwargs: dict | None = None):
        try:
            return self._models.execute_kw(
                self.db, self.uid, self._clave, modelo, metodo, args, kwargs or {}
            )
        except xmlrpc.client.Fault as e:
            raise OdooError(f"{modelo}.{metodo} falló: {e.faultString.strip().splitlines()[-1]}") from e

    def buscar_leer(self, modelo, dominio, campos, limite=0, orden=None) -> list[dict]:
        kw = {"fields": self.solo_existentes(modelo, campos)}
        if limite:
            kw["limit"] = limite
        if orden:
            kw["order"] = orden
        return self.call(modelo, "search_read", [dominio], kw)

    def uno(self, modelo, dominio, campos, orden=None) -> dict | None:
        r = self.buscar_leer(modelo, dominio, campos, limite=1, orden=orden)
        return r[0] if r else None

    def crear(self, modelo, vals) -> int:
        return self.call(modelo, "create", [vals])

    # ------------------------------------------------------ campos (saas-18.4)

    def campos(self, modelo: str) -> dict:
        if modelo not in self._campos:
            self._campos[modelo] = self.call(
                modelo, "fields_get", [], {"attributes": ["string", "type", "relation", "required"]}
            )
        return self._campos[modelo]

    def tiene(self, modelo: str, campo: str) -> bool:
        return campo in self.campos(modelo)

    def primer_campo(self, modelo: str, *candidatos: str) -> str | None:
        """Devuelve el primer campo de la lista que exista en el modelo.

        Es el antídoto contra los renombres de saas-18.4: `product_uom` pasó a
        `product_uom_id`, `uom_po_id` desapareció, etc.
        """
        campos = self.campos(modelo)
        for c in candidatos:
            if c in campos:
                return c
        return None

    def solo_existentes(self, modelo: str, campos: list[str]) -> list[str]:
        disponibles = self.campos(modelo)
        return [c for c in campos if c in disponibles]

    # ------------------------------------------------------------- utilidades

    @staticmethod
    def solo_digitos(texto: str) -> str:
        return "".join(c for c in (texto or "") if c.isdigit())

    def buscar_proveedor(self, cuit: str, nombre: str = "") -> dict | None:
        """Busca un proveedor por CUIT, comparando solo dígitos.

        En l10n_ar el CUIT vive en `res.partner.vat`, y se guarda indistintamente
        con guiones o sin ellos según quién lo haya cargado.
        """
        cuit_limpio = self.solo_digitos(cuit)
        campos = ["name", "vat", "property_purchase_currency_id", "property_supplier_payment_term_id"]

        candidatos = self.buscar_leer(
            "res.partner", [["vat", "!=", False]], campos + ["parent_id"], limite=0
        )
        for c in candidatos:
            if self.solo_digitos(c.get("vat") or "") == cuit_limpio:
                return c

        if nombre:
            aprox = self.buscar_leer(
                "res.partner", [["name", "ilike", nombre.split()[0]]], campos, limite=5
            )
            if len(aprox) == 1:
                return aprox[0]
        return None

    def producto_por_codigo_proveedor(self, partner_id: int, codigo: str) -> dict | None:
        """Resuelve un código del proveedor a un producto vía product.supplierinfo.

        Es el mapeo correcto: el código que imprime el proveedor en su factura
        no es el `default_code` de Odoo.
        """
        campo_prod = self.primer_campo("product.supplierinfo", "product_id", "product_tmpl_id")
        campo_codigo = self.primer_campo("product.supplierinfo", "product_code", "code")
        if not campo_codigo:
            return None

        filas = self.buscar_leer(
            "product.supplierinfo",
            [["partner_id", "=", partner_id], [campo_codigo, "=", codigo]],
            [campo_prod, "product_tmpl_id", "price", "min_qty", "delay", campo_codigo],
            limite=1,
        )
        return filas[0] if filas else None
