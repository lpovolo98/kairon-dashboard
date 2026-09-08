"""Qué columna de la plantilla va a qué campo de Odoo.

Es la única fuente de verdad del mapeo: la plantilla que se descarga, la
validación y la comparación contra Odoo salen todas de acá. Agregar una
columna nueva es agregar una entrada.
"""

# Valores que Kairon aplica a todo producto nuevo. Estaban fijos y ocultos
# dentro de cargar_catalogo.py; acá son visibles y se pueden discutir.
FIJOS_AL_CREAR = {
    "type": "consu",
    "is_storable": True,        # mercadería física: se rastrea inventario
    "invoice_policy": "order",  # facturar por cantidad ordenada
    "sale_ok": True,
    "purchase_ok": True,
}

# tipo: texto | numero | booleano | m2o | m2m
# opciones: nombre de la lista del catálogo que alimenta el desplegable
COLUMNAS_PRODUCTOS = [
    {"col": "Referencia interna (SKU)", "campo": "default_code", "tipo": "texto", "clave": True},
    {"col": "Nombre",             "campo": "name",           "tipo": "texto", "requerido": True},
    {"col": "Categoría",          "campo": "categ_id",       "tipo": "m2o", "modelo": "product.category",
     "opciones": "categorias", "etiqueta": "Categoría"},
    {"col": "Unidad",             "campo": "uom_id",         "tipo": "m2o", "modelo": "uom.uom",
     "opciones": "unidades", "etiqueta": "Unidad"},
    {"col": "Unidades por caja",  "campo": "x_studio_unidades_por_caja", "tipo": "numero",
     "aviso_si_falta": "Sin ese dato Odoo trata 1 unidad = 1 caja y los números salen mal sin avisar."},
    {"col": "Impuestos de venta", "campo": "taxes_id",          "tipo": "m2m", "modelo": "account.tax",
     "alcance": "sale", "opciones": "impuestos_venta", "etiqueta": "Impuestos de venta"},
    {"col": "Impuestos de compra","campo": "supplier_taxes_id", "tipo": "m2m", "modelo": "account.tax",
     "alcance": "purchase", "opciones": "impuestos_compra", "etiqueta": "Impuestos de compra"},
    {"col": "Precio de venta",    "campo": "list_price",     "tipo": "numero"},
    {"col": "Costo",              "campo": "standard_price", "tipo": "numero"},
    {"col": "Peso (kg)",          "campo": "weight",         "tipo": "numero"},
    {"col": "Disponible en PdV (Sí/No)", "campo": "available_in_pos", "tipo": "booleano"},
]

COLUMNAS_PROVEEDORES = [
    {"col": "SKU (igual al de Productos)", "campo": "_sku", "tipo": "texto", "clave": True},
    {"col": "Proveedor", "campo": "partner_id", "tipo": "m2o", "modelo": "res.partner",
     "opciones": "proveedores", "etiqueta": "Proveedor", "requerido": True},
    {"col": "Código del proveedor", "campo": "product_code", "tipo": "texto",
     "aviso_si_falta": "Sin eso, la carga de facturas de ese proveedor no va a poder mapear este producto."},
    {"col": "Precio",          "campo": "price",   "tipo": "numero"},
    {"col": "Cantidad mínima", "campo": "min_qty", "tipo": "numero"},
]

COLUMNAS_PRECIOS = [
    {"col": "SKU (igual al de Productos)", "campo": "_sku", "tipo": "texto", "clave": True},
    {"col": "Lista de precios", "campo": "pricelist_id", "tipo": "m2o", "modelo": "product.pricelist",
     "opciones": "listas", "etiqueta": "Lista de precios", "requerido": True},
    {"col": "Precio fijo", "campo": "fixed_price", "tipo": "numero", "requerido": True},
]

HOJAS = {
    "productos":   {"columnas": COLUMNAS_PRODUCTOS,   "modelo": "product.template"},
    "proveedores": {"columnas": COLUMNAS_PROVEEDORES, "modelo": "product.supplierinfo"},
    "precios":     {"columnas": COLUMNAS_PRECIOS,     "modelo": "product.pricelist.item"},
}


def columna_clave(hoja):
    return next(c["col"] for c in HOJAS[hoja]["columnas"] if c.get("clave"))
