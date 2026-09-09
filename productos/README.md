# Agente de productos

## Flujo

Archivo XLSX/CSV, pegado o modificación por filtro → revisión completa de las tres hojas → confirmación → trabajo en segundo plano → historial persistente.

La revisión devuelve una huella que se comprueba nuevamente contra Odoo antes de ejecutar. Cada escritura registra su intención antes del RPC, su resultado y el valor previo. Una petición con la misma clave devuelve la misma corrida. Los resultados inciertos nunca se reejecutan automáticamente.

## Reversión

Las corridas nuevas comprueban conflictos antes de revertir. Se restauran los campos modificados, se archivan los productos nuevos y se eliminan únicamente los vínculos comerciales nuevos que no cambiaron. La pantalla muestra esas acciones antes de confirmarlas. Una interrupción o un conflicto requiere revisión: no se promete una reversión incondicional. Las corridas anteriores sin registro verificable requieren revisión manual en Odoo.

Todas las escrituras de catálogo, fotos y reversión usan el mismo bloqueo. Las ediciones realizadas directamente en Odoo se comprueban antes de escribir; XML-RPC no ofrece una transacción única con SQLite.

## Validación

Máximo de 300 filas entre las tres hojas por defecto. Cantidades por caja positivas, números finitos no negativos, booleanos explícitos, impuestos separados por alcance e IDs validados. Los SKU se comparan sin mayúsculas y se consultan también archivados. Reglas ambiguas requieren revisión.

Una celda opcional vacía conserva el dato. Para quitar impuestos o un texto opcional, usar `[VACIAR]`. Se rechazan columnas desconocidas y filas con datos sin SKU. Los filtros demasiado amplios se rechazan explícitamente; no se truncan silenciosamente. Los catálogos se consultan por páginas y las previsualizaciones grandes agrupan búsquedas de productos.

## Imágenes

La pantalla reduce las fotos y el servidor las decodifica, valida y normaliza. Límite de lote 20 MB y de imagen 4 MB, con hasta 25 millones de píxeles de entrada. Cada SKU recibe una única foto por lote. Las sugerencias por nombre requieren selección. El historial guarda referencias a archivos de imágenes, no sus cuerpos completos en las respuestas.

## Administración

Un comprobante con códigos faltantes queda esperando productos antes de crear su compra. La cola conserva proveedor, código y referencias de los comprobantes. Desde Productos se prepara un alta o un vínculo con un SKU existente. Al resolver todas las equivalencias, se retoma el documento original y se repiten los controles de Odoo. Si ya tiene compra o factura, no se retoma automáticamente.

El PDF original se conserva en el directorio administrativo para permitir esta continuación. No se vuelven a inferir sus datos durante la continuación.

## Configuración

Se reutilizan ODOO_URL, ODOO_DB, ODOO_USER y ODOO_KEY u ODOO_PASSWORD. La identidad se verifica en el middleware del portal. PRODUCTOS_ALLOWED_EMAILS restringe acceso si se configura.

PRODUCTOS_DATA_DIR: por defecto /data/productos cuando hay volumen. PRODUCTOS_TOPE: 300. Se requiere un único worker ASGI y almacenamiento persistente para conservar corridas, operaciones y fotos anteriores. Los trabajos interrumpidos por un reinicio quedan rechazados o inciertos, nunca se reproducen a ciegas.

## Pruebas

Suite unittest en tests/test_*.py. Incluye cortes de RPC, idempotencia, conflictos, imágenes, campos inválidos y continuación de comprobantes.

Para navegador: iniciar tests/serve_productos_test.py en 127.0.0.1:8767 y ejecutar tests/productos_browser.cjs con Playwright. Ese servidor usa exclusivamente Odoo simulado y no importa credenciales de producción.