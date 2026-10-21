"""Ejecuta los controladores reales del panel con un DOM y red controlados."""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import app as app_module


@unittest.skipUnless(shutil.which("node"), "Node.js no está disponible")
class TelegramQrUiTestCase(unittest.TestCase):
    def run_ui(self, scenario):
        template = app_module.TEMPLATE
        render = template[template.index("function renderChannelState("):template.index("async function loadChannelState(")]
        auth = template[template.index("let tgRetryTimer ="):template.index("function showTelegramSwitch(")]
        code = render + "\n" + auth
        harness = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const elements = new Map();
const storage = new Map();
let nextTimer = 0;
const timers = new Map();
const element = id => {
  if (!elements.has(id)) elements.set(id, {
    style: {}, value: '', innerHTML: '', textContent: '', disabled: false,
    getAttribute(name) { return this[name] || null; },
    removeAttribute(name) { delete this[name]; }, focus() {}
  });
  return elements.get(id);
};
const context = vm.createContext({
  document: {getElementById: element},
  sessionStorage: {getItem: key => storage.get(key), setItem: (key,value) => storage.set(key,value), removeItem: key => storage.delete(key)},
  setTimeout: fn => { timers.set(++nextTimer, fn); return nextTimer; },
  clearTimeout: id => timers.delete(id),
  setInterval: fn => { timers.set(++nextTimer, fn); return nextTimer; },
  clearInterval: id => timers.delete(id),
  loadChannelState: async () => {}, channelHeaders: () => ({}),
  toast() {}, setAdminAccessStatus() {}, loadTestMode() {}, renderTestModeState() {},
  safeAccountLabel: () => 'Cuenta', revealWaQrCard() {}, beginWaSwitchPolling() {},
  waSwitchPolling: false, fetch: async () => { throw new Error('unexpected network'); }
});
vm.runInContext('let channelState = null; let channelCsrf = "test"; let tgAuthMode = "link";', context);
vm.runInContext(CODE, context);
const run = code => vm.runInContext(code, context);
(async () => { SCENARIO })().catch(error => { console.error(error); process.exitCode = 1; });
"""
        harness = harness.replace("CODE, context", json.dumps(code) + ", context")
        harness = harness.replace("SCENARIO", scenario)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "qr-ui.cjs"
            path.write_text(harness, encoding="utf-8")
            result = subprocess.run([shutil.which("node"), str(path)], capture_output=True, text=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_periodic_status_does_not_overwrite_code_or_qr_authentication(self):
        self.run_ui(r"""
for (const method of ['code', 'qr']) {
  run(`rememberTgAttempt('link', 'owned-token'); rememberTgMethod('${method}');`);
  element('tg-link-status').textContent = 'Esperando confirmación';
  element('tg-link-status').innerHTML = 'Esperando confirmación';
  element('tg-link-btn').innerHTML = 'Espera 20s';
  run('renderChannelState({can_manage: true, telegram: {linked:false}, whatsapp:{}})');
  assert.equal(element('tg-link-status').innerHTML, 'Esperando confirmación');
  assert.equal(element('tg-link-btn').innerHTML, 'Espera 20s');
}
run('clearTgAttempt(); renderChannelState({can_manage:true, telegram:{linked:false}, whatsapp:{}})');
assert.match(element('tg-link-status').innerHTML, /Sin vincular/);
""")

    def test_qr_acceptance_shows_2fa_and_completion_removes_the_image(self):
        self.run_ui(r"""
await run(`showTelegramQrOutcome({ok:true, needs_qr:true, auth_attempt:'owned-token', expires_in:30, qr_image:'data:image/svg+xml;base64,PHN2Zy8+'})`);
assert.equal(element('tg-qr-img').style.display, 'block');
assert.equal(element('tg-code-section').style.display, 'none');
await run('showTelegramQrOutcome({ok:false, needs_password:true})');
assert.equal(element('tg-2fa-section').style.display, 'block');
assert.equal(element('tg-qr-img').getAttribute('src'), null);
element('tg-api-hash').value = 'private-hash';
await run('showTelegramQrOutcome({ok:true, authorized:true})');
assert.equal(element('tg-api-hash').value, '');
assert.equal(element('tg-qr-section').style.display, 'none');
assert.equal(element('tg-2fa-section').style.display, 'none');
assert.equal(run('tgAuthAttempt'), null);
""")

    def test_late_poll_response_cannot_restore_a_cancelled_qr(self):
        self.run_ui(r"""
let resolveRequest;
context.fetch = () => new Promise(resolve => { resolveRequest = resolve; });
run("rememberTgAttempt('link', 'old-attempt'); rememberTgMethod('qr');");
const request = run('pollTelegramQr(tgQrGeneration)');
let duplicateFetched = false;
const originalFetch = context.fetch;
context.fetch = (...args) => { duplicateFetched = true; return originalFetch(...args); };
await run('pollTelegramQr(tgQrGeneration)');
assert.equal(duplicateFetched, false);
run('clearTgAttempt()');
resolveRequest({json:async () => ({ok:true, needs_qr:true, auth_attempt:'old-attempt', expires_in:30, qr_image:'data:image/svg+xml;base64,PHN2Zy8+'})});
await request;
assert.equal(run('tgAuthAttempt'), null);
assert.equal(element('tg-qr-img').getAttribute('src'), null);
assert.equal(element('tg-qr-section').style.display, 'none');
assert.equal(timers.size, 0);
""")


if __name__ == "__main__":
    unittest.main()
