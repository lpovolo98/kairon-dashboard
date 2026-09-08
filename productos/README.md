# Agente de productos

Pantalla `/productos`, integrada al portal. Alta y modificación masiva de
artículos en Odoo desde un archivo, un pegado de Excel o el formulario, más la
carga de fotos.

Porta la lógica de `cargar_catalogo.py` —el script que hasta ahora se corría a
mano— y agrega lo que ese script no hacía: comparar contra el producto
existente para separar altas de modificaciones. El script original, si el SKU
ya existía, lo salteaba: modificar era trabajo manual en Odoo.

## Garantías

**Previsualizar nunca escribe.** No es una convención: `previsualizar()` recibe
el cliente de Odoo envuelto en `SoloLectura`, que levanta `AssertionError` ante
cualquier `create`, `write` o `unlink`. Está cubierto por dos tests.

**Una fila sin cambios no se toca.** Cada campo se compara con el valor real en
Odoo, normalizando números (`1740`, `"1740.00"` y `"1.740,00"` son lo mismo).
Sin esa comparación toda fila parecería modificada y se reescribiría el
catálogo entero.

**El navegador manda filas, no escrituras.** `aplicar` vuelve a previsualizar
del lado del servidor y recién ahí escribe: un cuerpo manipulado no puede pedir
una escritura arbitraria. Las claves internas del plan no salen en la respuesta.

**Toda corrida se revierte.** Se guarda el valor previo de cada campo
modificado y el id de cada registro creado. Revertir devuelve los campos y
*archiva* lo creado: no borra, porque un producto con movimientos no se puede
borrar y archivar sí es reversible.

**Con una sola fila en error no se escribe nada.** Ni las filas buenas.

## Problemas reales que resuelve, heredados del script

- **SKU con espacios.** Hay códigos cargados en Odoo con un espacio pegado. Una
  búsqueda exacta no los encuentra y se crea un duplicado con el mismo código a
  la vista. La búsqueda es tolerante y, si hay más de una coincidencia, frena.
- **Nombres ambiguos.** Dos categorías con el mismo nombre frenan la fila y
  piden desambiguar con `Categoría (id 42)`.
- **Impuestos homónimos.** «IVA 21%» existe en venta y en compra. La caché del
  resolvedor distingue el alcance; si no, el segundo hereda el id del primero y
  asigna el impuesto equivocado sin ningún aviso.
- **Nombres que no existen.** El error propone los parecidos, y en la pantalla
  la celda es directamente un desplegable con los valores válidos de Odoo.

## Imágenes

El achicado a 1600 px ocurre **en el navegador**, con los mismos parámetros que
`achicar_imagenes.py`: el original nunca sale de la máquina. El servidor valida
firma y tamaño como red de contención, no como mecanismo principal.

El diccionario `MAPEO` de `organizar_imagenes.py` lo reemplaza un emparejado
automático: primero busca el SKU dentro del nombre del archivo, después compara
palabra por palabra contra los nombres de producto, dando prioridad a las
palabras que identifican a un solo producto. Lo que no reconoce se resuelve en
un tablero de dos clicks. La lista de productos sin foto se consulta a Odoo, así
que tampoco hay que mantener `SIN_IMAGEN` a mano.

Al pisar una foto se guarda la anterior: revertir la devuelve.

## Valores fijos al crear

Están en `esquema.py`, visibles y discutibles, no escondidos en el código:

    type=consu · is_storable=True · invoice_policy=order · sale_ok · purchase_ok

## Configuración

Reutiliza las variables de Odoo que ya existen y la identidad que el middleware
del portal verifica contra Cloudflare Access: no hay login propio.

- `PRODUCTOS_ALLOWED_EMAILS`: correos habilitados, separados por coma. Si no
  está definida, alcanza con el acceso del portal.
- `PRODUCTOS_TOPE`: máximo de registros por corrida (default 300). Protege de un
  pegado accidental.
- `PRODUCTOS_DATA_DIR`: dónde vive el historial de corridas. En Railway,
  `/data/productos` con volumen persistente. Sin volumen no se puede revertir
  una corrida después de un reinicio.

Un solo worker ASGI: `aplicar` toma un mutex en SQLite para que dos corridas en
paralelo no creen el mismo producto dos veces.

## Pendiente

- Lectura del `.xlsx` (hoy anda el pegado desde Excel).
- Modificación masiva sin archivo: filtrar en Odoo y aplicar una operación.
- La cola con el agente administrativo tiene el endpoint y la función
  `encolar()`, pero el agente administrativo todavía no la llama.

## Pruebas

    python -m pytest tests/test_productos.py          # 34, con un Odoo en memoria
    python tests/e2e_productos.py                     # navegador -> API -> motor
