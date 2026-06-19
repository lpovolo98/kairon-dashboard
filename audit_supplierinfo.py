"""
Verifica el modelo product.supplierinfo (pestaña "Compras" > "Proveedor"
en la ficha del producto) para evaluar si podemos usarlo como fuente de
verdad del proveedor, en vez de mantener PROD_MAP a mano en el frontend.
"""
import xmlrpc.client
import os
from dotenv import load_dotenv

load_dotenv()
ODOO_URL  = os.getenv("ODOO_URL")
ODOO_DB   = os.getenv("ODOO_DB")
ODOO_USER = os.getenv("ODOO_USER")
ODOO_PASS = os.getenv("ODOO_PASSWORD")

common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASS, {})
models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
print(f"Conectado como UID {uid}\n")

# 1. Ver los campos de product.supplierinfo
print("=" * 90)
print("Campos disponibles en product.supplierinfo:")
print("=" * 90)
fields_info = models.execute_kw(
    ODOO_DB, uid, ODOO_PASS,
    "product.supplierinfo", "fields_get", [],
    {"attributes": ["string", "type", "relation"]}
)
for fname in ["partner_id", "product_id", "product_tmpl_id", "price", "delay"]:
    if fname in fields_info:
        print(f"  {fname}: {fields_info[fname]}")

# 2. Traer TODOS los registros de supplierinfo con su producto y proveedor
print("\n" + "=" * 90)
print("Todos los productos con su proveedor cargado en Compras:")
print("=" * 90)
supplierinfos = models.execute_kw(
    ODOO_DB, uid, ODOO_PASS,
    "product.supplierinfo", "search_read",
    [[]],
    {"fields": ["id", "partner_id", "product_tmpl_id", "product_id", "price"], "limit": 300}
)
print(f"Total registros de supplierinfo: {len(supplierinfos)}\n")
for s in supplierinfos:
    prov = s["partner_id"][1] if s["partner_id"] else "?"
    prod = s["product_tmpl_id"][1] if s.get("product_tmpl_id") else (s["product_id"][1] if s.get("product_id") else "?")
    print(f"  {prod[:55]:<57} -> {prov}")

# 3. Comparar contra el total de productos vendibles (para ver cobertura)
print("\n" + "=" * 90)
print("Cobertura: productos vendibles SIN proveedor cargado en Compras")
print("=" * 90)
productos = models.execute_kw(
    ODOO_DB, uid, ODOO_PASS,
    "product.product", "search_read",
    [[["sale_ok", "=", True], ["active", "=", True]]],
    {"fields": ["id", "name", "product_tmpl_id"]}
)
tmpl_ids_con_proveedor = set()
for s in supplierinfos:
    if s.get("product_tmpl_id"):
        tmpl_ids_con_proveedor.add(s["product_tmpl_id"][0])

sin_proveedor = [p for p in productos if p["product_tmpl_id"][0] not in tmpl_ids_con_proveedor]
print(f"Total productos vendibles: {len(productos)}")
print(f"Sin proveedor cargado en Compras: {len(sin_proveedor)}")
for p in sin_proveedor:
    print(f"  {p['name']}")

# 4. Nombre EXACTO de los proveedores existentes (para mapear bien proveedor->segmento si hace falta)
print("\n" + "=" * 90)
print("Nombres exactos de proveedores (res.partner) usados en supplierinfo:")
print("=" * 90)
proveedor_ids = list({s["partner_id"][0] for s in supplierinfos if s["partner_id"]})
if proveedor_ids:
    provs = models.execute_kw(
        ODOO_DB, uid, ODOO_PASS,
        "res.partner", "search_read",
        [[["id", "in", proveedor_ids]]],
        {"fields": ["id", "name"]}
    )
    for p in provs:
        print(f"  ID {p['id']}: '{p['name']}'")
