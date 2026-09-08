const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = readFileSync('static/administracion.js','utf8').split('function choose(')[0];
async function run(response, options = {}) {
  const context = vm.createContext({document: {}, fetch: async () => response});
  vm.runInContext(source, context);
  return context.api('facturas', options);
}
(async () => {
  const response = (status, type, body) => ({status, ok: status < 400, headers:{get:()=>type}, json:async()=>JSON.parse(body)});
  assert.equal((await run(response(200,'application/json','{"ready":true}'))).ready,true);
  await assert.rejects(run(response(502,'text/html','<!DOCTYPE html>'),{method:'POST'}), /historial/);
  await assert.rejects(run(response(200,'text/html','<!DOCTYPE html>')), /Volvé a ingresar/);
  await assert.rejects(run({type:'opaqueredirect'}), /acceso necesita verificación/);
  await assert.rejects(run(response(409,'application/json','{"detail":"Este PDF ya fue recibido"}')), /ya fue recibido/);
  await assert.rejects(run(response(200,'application/json','{')), /incompleta/);
  console.log('6 controles HTTP correctos');
})().catch(error => {console.error(error); process.exitCode=1;});
