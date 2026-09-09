"""Odoo en memoria para los tests del agente de productos.

Implementa lo justo de la API XML-RPC que usa el módulo —search, read,
search_read, create, write, fields_get— y además anota cada escritura, para
poder afirmar no solo que el resultado es el esperado sino que no se tocó
nada de más. Sin esto, probar el ABM implicaría escribir en producción.
"""

M2O = {
    "product.template":       {"categ_id": "product.category", "uom_id": "uom.uom"},
    "product.supplierinfo":   {"partner_id": "res.partner", "product_tmpl_id": "product.template"},
    "product.pricelist.item": {"pricelist_id": "product.pricelist", "product_tmpl_id": "product.template"},
}
M2M = {"product.template": ["taxes_id", "supplier_taxes_id"]}

CAMPOS = {
    'purchase.order.line':['product_id'], 'sale.order.line':['product_id'], 'stock.move':['product_id'], 'account.move.line':['product_id'],
    "product.template": ["name", "default_code", "categ_id", "uom_id", "x_studio_unidades_por_caja",
                         "taxes_id", "supplier_taxes_id", "list_price", "standard_price", "weight",
                         "available_in_pos", "image_1920", "active", "type", "is_storable",
                         "invoice_policy", "sale_ok", "purchase_ok"],
    "product.supplierinfo": ["partner_id", "product_tmpl_id", "product_code", "price", "min_qty", "active"],
    "product.pricelist.item": ["pricelist_id", "product_tmpl_id", "applied_on", "compute_price",
                               "fixed_price", "active"],
    "product.category": ["name", "active"],
    "uom.uom": ["name", "active"],
    "account.tax": ["name", "type_tax_use", "active"],
    "product.pricelist": ["name", "active"],
    "res.partner": ["name", "supplier_rank", "active"],
}


class OdooFalso:
    def __init__(self, datos=None):
        self.datos = {m: {} for m in CAMPOS}
        self.escrituras = []          # (modelo, metodo, args)
        self._siguiente = 1
        for modelo, registros in (datos or {}).items():
            for r in registros:
                self.sembrar(modelo, r)

    # ── siembra ──
    def sembrar(self, modelo, valores):
        valores=dict(valores)
        valores.setdefault('active',True)
        id_ = valores.pop("id", None) or self._nuevo_id()
        self.datos[modelo][id_] = dict(valores)
        return id_

    def _nuevo_id(self):
        self._siguiente += 1
        return self._siguiente

    def registro(self, modelo, id_):
        return self.datos[modelo][id_]

    # ── API ──
    def call(self, modelo, metodo, args, kwargs=None):
        kwargs = kwargs or {}
        if metodo in ("create", "write", "unlink"):
            self.escrituras.append((modelo, metodo, args))
        return getattr(self, "_" + metodo)(modelo, args, kwargs)

    def solo_existentes(self, modelo, campos):
        return [c for c in campos if c in CAMPOS.get(modelo, [])]

    def campos(self, modelo):
        return {c: {} for c in CAMPOS.get(modelo, [])}

    # ── operaciones ──
    def _search(self, modelo, args, kwargs):
        domain=list(args[0] if args else [])
        if kwargs.get('context',{}).get('active_test') is False:
            domain.append(['active','in',[True,False]])
        ids = self._filtrar(modelo, domain)
        ids=ids[kwargs.get('offset',0):]
        return ids[: kwargs["limit"]] if kwargs.get("limit") else ids

    def _unlink(self,modelo,args,kwargs):
        for id_ in args[0]: del self.datos[modelo][id_]
        return True

    def _search_count(self,modelo,args,kwargs):
        return len(self._search(modelo,args,{}))

    def _read(self, modelo, args, kwargs):
        ids = args[0] if isinstance(args[0], list) else [args[0]]
        campos = kwargs.get("fields")
        return [self._proyectar(modelo, i, campos) for i in ids if i in self.datos[modelo]]

    def _search_read(self, modelo, args, kwargs):
        ids = self._search(modelo, args, kwargs)
        return [self._proyectar(modelo, i, kwargs.get("fields")) for i in ids]

    def _create(self, modelo, args, kwargs):
        vals = dict(args[0])
        for campo in M2M.get(modelo, []):
            if campo in vals:
                vals[campo] = self._aplicar_m2m(vals[campo], [])
        vals.setdefault("active", True)
        return self.sembrar(modelo, vals)

    def _write(self, modelo, args, kwargs):
        ids, vals = args[0], dict(args[1])
        for id_ in ids:
            reg = self.datos[modelo][id_]
            for campo, valor in vals.items():
                if campo in M2M.get(modelo, []):
                    reg[campo] = self._aplicar_m2m(valor, reg.get(campo) or [])
                else:
                    reg[campo] = valor
        return True

    def _fields_get(self, modelo, args, kwargs):
        return self.campos(modelo)

    # ── auxiliares ──
    @staticmethod
    def _aplicar_m2m(comando, actual):
        # Solo se usa (6, 0, ids): reemplazo total.
        if isinstance(comando, list) and comando and isinstance(comando[0], (list, tuple)):
            for c in comando:
                if c[0] == 6:
                    return list(c[2])
            return actual
        return list(comando or [])

    def _proyectar(self, modelo, id_, campos):
        reg = self.datos[modelo][id_]
        campos = campos or list(reg)
        salida = {"id": id_}
        for c in campos:
            if c == "display_name":
                salida[c] = reg.get("name", str(id_))
                continue
            v = reg.get(c, False)
            destino = M2O.get(modelo, {}).get(c)
            if destino and isinstance(v, int) and v in self.datos[destino]:
                v = [v, self.datos[destino][v].get("name", str(v))]
            elif destino and not v:
                v = False
            salida[c] = v
        return salida

    def _filtrar(self, modelo, dominio):
        # Odoo esconde los archivados salvo que el dominio los mencione.
        menciona_active = any(isinstance(c, (list, tuple)) and c[0] == "active" for c in dominio)
        ids = [i for i, r in self.datos[modelo].items()
               if menciona_active or r.get("active", True)]
        def matches(id_):
            cursor=iter(dominio)
            def evaluate(token):
                if token in ('|','&'):
                    left=evaluate(next(cursor));right=evaluate(next(cursor))
                    return left or right if token=='|' else left and right
                if token=='!':return not evaluate(next(cursor))
                return self._cumple(modelo,id_,*token)
            checks=[]
            for token in cursor:checks.append(evaluate(token))
            return all(checks)
        ids=[id_ for id_ in ids if matches(id_)]
        return sorted(ids)

    def _cumple(self, modelo, id_, campo, op, valor):
        actual = id_ if campo == "id" else self.datos[modelo][id_].get(campo, False)
        if isinstance(actual, list) and len(actual) == 2 and isinstance(actual[0], int):
            actual = actual[0]
        if op == "=":
            if valor is False:
                return not actual
            return actual == valor
        if op == "!=":
            return bool(actual) if valor is False else actual != valor
        if op == "ilike":
            return str(valor).strip().lower() in str(actual or "").lower()
        if op == ">":
            return (actual or 0) > valor
        if op == "in":
            return actual in valor
        return True
