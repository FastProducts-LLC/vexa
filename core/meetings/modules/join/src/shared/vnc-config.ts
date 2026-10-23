/**
 * vnc-config — the pure (no I/O) plan for the bot's live-view stack (x11vnc + websockify/noVNC).
 *
 * The live view drives the bot's browser — i.e. the meeting and any signed-in session in it — so it
 * is FAIL-CLOSED by default:
 *   • x11vnc requires a password (never `-nopw`) and listens on loopback only (`-localhost`);
 *   • the password is random per bot process (or VEXA_VNC_PASSWORD, injected by whatever control
 *     plane later hands it to the meeting's OWNER) and is passed via a self-deleting file
 *     (`-passwdfile rm:<path>`) — never on the command line, never logged;
 *   • websockify (noVNC, :6080) binds 127.0.0.1 unless VEXA_VNC_LISTEN_HOST says otherwise (the
 *     local `Dockerfile.debug` harness sets 0.0.0.0 inside its container and publishes on host
 *     loopback only).
 */

export const VNC_RFB_PORT = 5900;
export const NOVNC_PORT = 6080;

// Classic VNC auth only uses the first 8 characters of the password — draw them from a wide alphabet.
// (No "#": a leading "#" makes an x11vnc -passwdfile line a comment.)
const ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789%*+-=?@^_.";

/** An 8-char VNC password from caller-supplied random bytes (crypto.randomBytes in production). */
export function vncPasswordFrom(bytes: Uint8Array): string {
  if (bytes.length < 8) throw new Error("need >= 8 random bytes");
  let out = "";
  for (let i = 0; i < 8; i++) out += ALPHABET[bytes[i] % ALPHABET.length];
  return out;
}

export interface VncPlan {
  password: string;
  x11vncArgs: string[];
  websockifyArgs: string[];
  listenHost: string;
}

export function vncPlan(opts: {
  display: string;
  env: Record<string, string | undefined>;
  passwdFile: string;
  randomBytes: Uint8Array;
  novncWebDir?: string;
}): VncPlan {
  const fromEnv = (opts.env.VEXA_VNC_PASSWORD || "").trim();
  const password = fromEnv || vncPasswordFrom(opts.randomBytes);
  const listenHost = (opts.env.VEXA_VNC_LISTEN_HOST || "").trim() || "127.0.0.1";
  const x11vncArgs = [
    "-display", opts.display, "-forever", "-shared",
    "-localhost", "-rfbport", String(VNC_RFB_PORT),
    "-passwdfile", `rm:${opts.passwdFile}`,
  ];
  const listen = `${listenHost}:${NOVNC_PORT}`;
  const target = `127.0.0.1:${VNC_RFB_PORT}`;
  const websockifyArgs = opts.novncWebDir
    ? ["--web", opts.novncWebDir, listen, target]
    : [listen, target];
  return { password, x11vncArgs, websockifyArgs, listenHost };
}
