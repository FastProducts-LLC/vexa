/**
 * vnc-config — the bot live view (x11vnc + noVNC) must be fail-closed: never `-nopw`, VNC on
 * loopback, noVNC on loopback unless explicitly overridden, password never on the command line.
 *
 * Run: npx tsx src/shared/vnc-config.test.ts
 */
import * as assert from 'assert';
import { vncPasswordFrom, vncPlan } from './vnc-config';

const bytes = (n: number) => Uint8Array.from({ length: n }, (_, i) => (i * 37 + 11) & 0xff);
let failed = 0;
function check(name: string, fn: () => void) {
  try { fn(); console.log(`ok   - ${name}`); } catch (e: any) { failed++; console.error(`FAIL - ${name}: ${e?.message}`); }
}

check('default plan: password required, x11vnc loopback, noVNC loopback', () => {
  const p = vncPlan({ display: ':99', env: {}, passwdFile: '/tmp/x/passwd', randomBytes: bytes(8) });
  assert.ok(!p.x11vncArgs.includes('-nopw'), 'must never run -nopw');
  assert.ok(p.x11vncArgs.includes('-localhost'), 'x11vnc must listen on loopback');
  const i = p.x11vncArgs.indexOf('-passwdfile');
  assert.ok(i >= 0 && p.x11vncArgs[i + 1] === 'rm:/tmp/x/passwd', 'password via self-deleting file');
  assert.strictEqual(p.listenHost, '127.0.0.1');
  assert.deepStrictEqual(p.websockifyArgs, ['127.0.0.1:6080', '127.0.0.1:5900']);
  assert.strictEqual(p.password.length, 8);
  assert.ok(!p.x11vncArgs.some((a) => a.includes(p.password)), 'password must not appear in argv');
  assert.ok(!p.websockifyArgs.some((a) => a.includes(p.password)));
});

check('password is per-process random (different bytes → different password)', () => {
  const a = vncPasswordFrom(bytes(8));
  const b = vncPasswordFrom(Uint8Array.from([200, 1, 2, 3, 4, 5, 6, 7]));
  assert.notStrictEqual(a, b);
  assert.ok(!a.startsWith('#'), 'a leading # would be an x11vnc passwdfile comment');
});

check('an injected per-bot password (VEXA_VNC_PASSWORD) wins over the random one', () => {
  const p = vncPlan({ display: ':99', env: { VEXA_VNC_PASSWORD: 'owner-pw' }, passwdFile: '/f', randomBytes: bytes(8) });
  assert.strictEqual(p.password, 'owner-pw');
});

check('explicit listen-host override (local debug harness) + novnc web dir', () => {
  const p = vncPlan({ display: ':1', env: { VEXA_VNC_LISTEN_HOST: '0.0.0.0' }, passwdFile: '/f',
    randomBytes: bytes(8), novncWebDir: '/usr/share/novnc' });
  assert.deepStrictEqual(p.websockifyArgs, ['--web', '/usr/share/novnc', '0.0.0.0:6080', '127.0.0.1:5900']);
  assert.ok(p.x11vncArgs.includes('-localhost'), 'x11vnc stays loopback even when noVNC is widened');
});

check('rejects too few random bytes', () => {
  assert.throws(() => vncPasswordFrom(new Uint8Array(4)));
});

if (failed) { console.error(`${failed} failed`); process.exit(1); }
console.log('vnc-config: all passed');
