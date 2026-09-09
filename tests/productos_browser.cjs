const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const assert=require('node:assert/strict');
(async()=>{
 const b=await chromium.launch({channel:'msedge',headless:true});
 try{
 const p=await b.newPage({viewport:{width:1440,height:1000}});const errors=[];p.on('pageerror',e=>errors.push(e.message));p.on('dialog',d=>d.accept());
 await p.goto('http://127.0.0.1:8767/productos');
 await p.waitForFunction(()=>ESTADO.catalogo?.categorias?.length>0);
 // A dropped file must be the one actually sent, not only a visual filename.
 await p.evaluate(()=>{const dt=new DataTransfer();dt.items.add(new File(['Referencia interna (SKU);Nombre;Unidades por caja;Precio de venta\nE2E-NEW;Producto de prueba;12;100'], 'catalogo.csv',{type:'text/csv'}));document.querySelector('#drop-productos').dispatchEvent(new DragEvent('drop',{dataTransfer:dt,bubbles:true,cancelable:true}));});
 await p.locator('#btn-leer-productos').click();await p.waitForFunction(()=>ESTADO.revision&&!ESTADO.busy);
 assert.equal(await p.locator('#tabla-productos .estado').textContent(),'Alta');
 await p.locator('#btn-aplicar').click();
 await p.waitForFunction(()=>document.querySelector('#historial-cuerpo').textContent.includes('completada'),{timeout:20000});
 // Populate all three sheets through the same preview endpoint used by XLSX.
 await p.evaluate(()=>previsualizar({productos:[{'Referencia interna (SKU)':'400001','Precio de venta':3000}],proveedores:[{'SKU (igual al de Productos)':'400001','Proveedor':'BIO ALIMENTOS SA','Código del proveedor':'P1','Precio':100}],precios:[{'SKU (igual al de Productos)':'400001','Lista de precios':'Mayorista','Precio fijo':150}]}));
 await p.locator('[data-tab="proveedores"]').click();
 await p.locator('#proveedores-cuerpo [data-col="Precio"] input').fill('200');
 await p.locator('#proveedores-cuerpo [data-col="Precio"] input').dispatchEvent('change');
 await p.waitForFunction(()=>ESTADO.revision&&!ESTADO.busy);
 assert.equal(await p.evaluate(()=>ESTADO.crudas.proveedores[0].Precio),'200');
 await p.locator('[data-tab="precios"]').click();
 await p.locator('#precios-cuerpo [data-col="Precio fijo"] input').fill('175');
 await p.locator('#precios-cuerpo [data-col="Precio fijo"] input').dispatchEvent('change');
 await p.waitForFunction(()=>ESTADO.revision&&!ESTADO.busy);
 assert.match(await p.locator('#accionbar-texto').textContent(),/1 cambios en productos.*1 cambios en proveedores.*1 cambios en precios/);
 await p.locator('#btn-aplicar').click();
 await p.waitForFunction(()=>[...document.querySelectorAll('#historial-cuerpo h3')].filter(e=>e.textContent.includes('completada')).length===2,{timeout:20000});
 await p.locator('#historial-cuerpo button').first().click();
 await p.waitForFunction(()=>document.querySelector('#historial-cuerpo').textContent.includes('revertida'),{timeout:20000});
 await p.reload();await p.waitForFunction(()=>document.querySelector('#historial-cuerpo').textContent.includes('revertida'));
 await p.screenshot({path:process.env.PRODUCTOS_SCREENSHOT||'productos-verificado.png',fullPage:true});
 assert.deepEqual(errors,[]);console.log('Navegador OK: archivo arrastrado, tres hojas, edición, aplicación, historial y reversión.');
 }finally{await b.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
