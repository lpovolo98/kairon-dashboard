# Agente administrativo

Pantalla `/administracion`, integrada al portal existente. Un PDF inicia lectura,
validación, orden de compra confirmada, recepción pendiente, factura vinculada en borrador y adjunto.
El modo activo, solicitado para probar contra producción, NO contabiliza. La función
interna solo permite contabilizar si un llamador pasa explícitamente `post=True`;
ningún endpoint de esta prueba lo hace.
La compra se confirma para generar la recepción. La recepción de mercadería NO se
valida. Cada línea de factura se vincula mediante `purchase_line_id` y se verifica
el vínculo después de crear. El historial enlaza compra, recepción y factura.

Si el proveedor falta, se crea con CUIT, razón social, domicilio, localidad, provincia,
código postal, condición de IVA y otros datos presentes en el PDF. No se inventan
teléfonos ni correos. Un CUIT archivado, duplicado o inválido exige revisión. No se
sobrescriben fichas existentes. Si luego faltan productos, la ficha creada se conserva
y el historial registra su ID: no se repite la creación.

## Activación

Esta versión está integrada a la rama de producción del portal y reutiliza la
identidad que su middleware ya verifica con Cloudflare Access. No requiere otra
clave en el navegador. `ADMIN_ALLOWED_EMAILS` permite restringir el módulo a una
lista de correos; si no está definida, se aplica el acceso existente del portal.
Las variables Odoo existentes se reutilizan. Los lectores locales Cibos y Starbread
no requieren OpenAI. Los parámetros OpenAI son necesarios para otros formatos.
Ejecutar un solo worker y réplica, con volumen `/data`, para conservar el historial.

En Railway, conservar las variables Odoo existentes. Agregar:

- `OPENAI_API_KEY`: clave de un proyecto de API con facturación habilitada.
- `ADMIN_MODEL`: modelo habilitado que admita PDF y Responses API.
- `CF_ACCESS_TEAM_DOMAIN`: dominio de equipo, sin https (ejemplo: equipo.cloudflareaccess.com).
- `CF_ACCESS_AUD`: audiencia de la aplicación Cloudflare Access del portal.
- `ADMIN_ALLOWED_EMAILS`: correos autorizados, separados por coma.
- `ADMIN_DATA_DIR=/data/administracion`: montar volumen persistente en `/data`.
- Alternativa para pruebas: `ADMIN_ACCESS_TOKEN`, secreto aleatorio largo, ingresado
  en la sección de acceso alternativo. No guardar secretos en HTML, repositorio o chat.

Usar una sola réplica y un solo worker ASGI. No hacer despliegues mientras existan
cargas activas. Un reinicio marca tareas incompletas como resultado incierto; nunca
las reejecuta. El historial y la exclusión de duplicados requieren conservar el volumen.

`config.json` copia los diarios, impuestos y convención de cajas del cargador Kairon
al 27/08/2026. Revisar esos valores antes de activar en producción. Se puede indicar
otro archivo mediante `ADMIN_CONFIG`. No incluye credenciales. La empresa y moneda
se comprueban contra el usuario Odoo y el CUIT receptor del PDF.

Instalar `requirements.txt` y publicar con el procedimiento existente de Railway.
La integración sigue local; se completó una prueba autorizada con escrituras en
Odoo producción para STARBREAD (ver resultados abajo).

## Alcance y errores

- PDF de hasta 15 MB, 20 páginas, sin contraseña, un comprobante por archivo.
- Primera versión: factura A en ARS y comprobantes internos X; otras clases, monedas,
  notas de crédito, percepciones distintas de IVA configurado o datos ambiguos se frenan.
- Los formatos de Cibos y Starbread se leen localmente desde el texto real del PDF; original y
  duplicado se comparan y se toman una sola vez. No se reemplaza el contenido por
  datos de ejemplo. Otros formatos requieren OpenAI (Responses con `store=false`). Se adjunta
  a Odoo una vez creada la factura. El historial local guarda datos extraídos y controles.
- No se contabiliza si el total difiere en más de un centavo o existen alertas.
- En modo borrador, las diferencias de precio se muestran como avisos; se usa el precio
  del PDF. Para contabilizar con `post=True` se mantienen como bloqueantes.
- Las operaciones XML-RPC no son una transacción única. Si hay una caída después de
  crear una OC/factura/adjunto o al contabilizar, revisar Odoo y los IDs del historial.
  No hay reintento automático ni eliminación automática de documentos parciales.
- Un mismo PDF no se vuelve a cargar. Para un intento fallido previo a cualquier
  escritura, el administrador debe conciliar el estado antes de habilitar un nuevo intento.
- Los controles de extracción y aritmética no garantizan lectura perfecta. Antes de
  habilitar el uso general, probar documentos de cada proveedor en una base de prueba.

Documentación del formato de entrada de PDF:
https://developers.openai.com/api/docs/guides/file-inputs

## Pruebas

### Prueba real contra producción, solo borradores

STARBREAD 00012-00005670: compra P00093 (id 93) confirmada, recepción WH/IN/00192
(id 1897) pendiente, factura id 5420 en borrador. Total 2.784.768,44; 32 líneas
vinculadas, descuento general 5% confirmado por el usuario, CAE y vencimientos
verificados y PDF original adjunto comprobado por checksum. La equivalencia
«Avena Extrafina» → 400025 también fue confirmada por el usuario. Se reutilizó
el proveedor 4023. No se validó el ingreso ni se contabilizó la factura.

La creación de proveedores nuevos se verificó con pruebas simuladas: no se creó
un proveedor de prueba en producción. Los artículos siguen requiriendo un mapeo
inequívoco al catálogo del proveedor; no se crean productos automáticamente.

`python tests/local-production.py` sirve `http://127.0.0.1:8766/administracion`.
Usa las credenciales Odoo del `.env` del portal, nunca las expone al navegador,
y mantiene un historial separado. Solo admite accesos desde localhost. Este
lanzador es para la máquina del operador, no para Railway. El endpoint sigue
los controles reales; las facturas existentes se muestran sin crear otra copia.

Comprobación realizada con el PDF Cibos 0014-00000314: lectura de ambos ejemplares,
una línea, total 441600.24, registro existente en producción 5363 contabilizado.
No se creó una factura nueva ni se cambió ese registro. Para probar una creación
en borrador se necesita un comprobante no cargado y su mapeo de productos correcto.

### Prueba interactiva aislada

`node tests/demo-local.cjs` abre `http://127.0.0.1:8765/administracion`.
Este servidor separado nunca importa el cliente Odoo ni llama al lector de PDF.
La carga usa datos de ejemplo, claramente identificados, y permite elegir cuatro
escenarios. El PDF se recibe en memoria para comprobar la carga y su hash; no se
extrae su contenido ni se guarda. El historial se borra con «Reiniciar prueba».
Solo escucha en la interfaz local. No usar este servidor para producción.

Esto verifica la interfaz; no reemplaza probar extracción de PDFs reales y el
circuito contable en una base Odoo de prueba antes de habilitar producción.

`python -m unittest discover -s tests -v`

Para verificar la pantalla con Playwright y Microsoft Edge instalado:
`node tests/preview.cjs`. Requiere el paquete `playwright`; alternativamente,
`PLAYWRIGHT_MODULE` puede apuntar al módulo de una instalación existente.

Pruebas con conexiones simuladas: validación del documento, bloqueo previo a escritura,
contabilización con total conciliado, diferencias, autorización, PDFs inválidos,
deduplicación y recuperación tras reinicio. No usan claves ni escriben en Odoo real.
