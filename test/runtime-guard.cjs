const net = require('node:net');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const { syncBuiltinESMExports } = require('node:module');
net.Server.prototype.listen = function () { throw new Error('Unexpected product listening socket'); };
const forbidden = [path.join(os.homedir(), '.kimi'), path.join(os.homedir(), '.kimi-code'), '/Users/aditya/Downloads/kimi-code-main'];
if (process.env.CODERAI_FORBIDDEN_CHECKOUT) forbidden.push(process.env.CODERAI_FORBIDDEN_CHECKOUT);
function check(value) {
  if (typeof value !== 'string' && !(value instanceof URL)) return;
  const target = path.resolve(value instanceof URL ? require('node:url').fileURLToPath(value) : value);
  if (forbidden.some(root => target === root || target.startsWith(root + path.sep))) throw new Error('Forbidden upstream/user-data filesystem access');
}
for (const key of ['readFileSync', 'writeFileSync', 'appendFileSync', 'mkdirSync', 'openSync', 'statSync', 'readdirSync']) {
  const original = fs[key]; fs[key] = function (file, ...args) { check(file); return original.call(this, file, ...args); };
}
for (const key of ['readFile', 'writeFile', 'appendFile', 'mkdir', 'open', 'stat', 'readdir', 'watch']) {
  const original = fs.promises[key]; fs.promises[key] = function (file, ...args) { check(file); return original.call(this, file, ...args); };
}
syncBuiltinESMExports();
