// Test-only preload: no app, bot, credentials, or session material is imported.
// Use: node --import ./scripts/offline_node_guard.mjs --test tests/*.test.mjs
import net from 'node:net';
import tls from 'node:tls';
import http from 'node:http';
import https from 'node:https';
import dgram from 'node:dgram';
import dns from 'node:dns';
import path from 'node:path';
import { syncBuiltinESMExports } from 'node:module';

function denyNetwork() {
  throw new Error('Network disabled for offline tests');
}

const workerNames = new Set(['wa_bot.mjs', 'bot.py', 'botfather_bot.py', 'app.py']);
if (workerNames.has(path.basename(process.argv[1] || '').toLowerCase())) {
  throw new Error('Bot/service startup disabled for offline tests');
}

net.connect = denyNetwork;
net.createConnection = denyNetwork;
net.Socket.prototype.connect = denyNetwork;
net.Server.prototype.listen = denyNetwork;
tls.connect = denyNetwork;
http.request = denyNetwork;
http.get = denyNetwork;
https.request = denyNetwork;
https.get = denyNetwork;
dgram.createSocket = denyNetwork;
for (const name of Object.keys(dns)) {
  if (/^(lookup|resolve|reverse)/.test(name) && typeof dns[name] === 'function') dns[name] = denyNetwork;
}
for (const name of Object.keys(dns.promises)) {
  if (/^(lookup|resolve|reverse)/.test(name) && typeof dns.promises[name] === 'function') dns.promises[name] = denyNetwork;
}

globalThis.fetch = denyNetwork;
if (globalThis.WebSocket) globalThis.WebSocket = class { constructor() { denyNetwork(); } };
syncBuiltinESMExports();
