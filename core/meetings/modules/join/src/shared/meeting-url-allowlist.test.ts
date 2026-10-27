/**
 * Standalone test for the bot-side meeting-URL allowlist (security finding #1).
 *
 * Run: npx tsx src/shared/meeting-url-allowlist.test.ts
 */
import { assertAllowedMeetingUrl, MeetingUrlNotAllowedError } from "./meeting-url-allowlist";
import { resolvePlatform } from "../index";

let passed = 0;
let failed = 0;

function ok(name: string, cond: boolean, detail = "") {
  if (cond) {
    passed++;
  } else {
    failed++;
    console.log(`  \x1b[31mFAIL\x1b[0m  ${name} ${detail}`);
  }
}

const VALID: Array<[string, string]> = [
  ["https://meet.google.com/abc-defg-hij", "google_meet"],
  ["https://meet.google.com/abc-defg-hij?authuser=0", "google_meet"],
  ["https://teams.microsoft.com/l/meetup-join/19%3ameeting_x%40thread.v2/0?context=%7b%7d", "teams"],
  ["https://teams.live.com/meet/9351274713?p=abc", "teams"],
  ["https://teams.cloud.microsoft/meet/9351274713", "teams"],
  ["https://gov.teams.microsoft.us/l/meetup-join/x", "teams"],
  ["https://us05web.zoom.us/j/84335626851?pwd=x", "zoom"],
  ["https://app.zoom.us/wc/84335626851/join", "zoom"],
  ["https://frbmeetings.zoomgov.com/j/12345678901", "zoom"],
  ["https://meet.jit.si/VexaStandup", "jitsi"],
  ["https://8x8.vc/vpaas-magic/room", "jitsi"],
  ["https://video.corp.example/daily", "jitsi"],   // self-hosted: explicit platform only
];

const EVIL = [
  "https://meet.google.com.evil.tld/abc-defg-hij",
  "https://evil.tld/meet.google.com",
  "https://meet.google.com@evil.tld/abc",
  "https://@meet.google.com/abc",
  "http://meet.google.com/abc-defg-hij",
  "https://meet.google.com:8443/abc",
  "https://meet.google.com:443/abc",
  "https://[::1]/abc",
  "https://127.0.0.1/abc",
  "https://2130706433/abc",
  "https://localhost/abc",
  "https://meet.google.com./abc",
  "https://meet.gооgle.com/abc",
  "https://meet。google。com/abc",
  "https://xn--mt-xka.google.com/abc",
  "https://meet.google.com\\@evil.tld/abc",
  "https://meet%2egoogle%2ecom/abc",
  "https://meet.google.com/%2e%2e/evil",
  "https://meet.google.com/a/../../evil",
  "https://meet.google.com/%5cevil",
  "https://evilzoom.us/j/1",
  "https://zoom-lfx.platform.linuxfoundation.org/meeting/96088138284",
  "javascript:alert(1)",
  "about:blank",
  "",
];

for (const [url, platform] of VALID) {
  let got: string | null = null;
  try { got = assertAllowedMeetingUrl(url, platform); } catch (e) { got = String(e); }
  ok(`valid ${url}`, got === platform, `got ${got}`);
}
for (const url of EVIL) {
  let threw = false;
  try { assertAllowedMeetingUrl(url); } catch (e) { threw = e instanceof MeetingUrlNotAllowedError; }
  ok(`evil ${url}`, threw);
}
// platform mismatch
let mismatch = false;
try { assertAllowedMeetingUrl("https://meet.google.com/abc-defg-hij", "zoom"); } catch { mismatch = true; }
ok("platform mismatch refused", mismatch);
// a self-hosted jitsi host is never INFERRED (no platform named) and never passes as another platform
let selfHosted = false;
try { assertAllowedMeetingUrl("https://video.corp.example/daily"); } catch { selfHosted = true; }
ok("self-hosted jitsi host not inferable", selfHosted);
let selfHostedZoom = false;
try { assertAllowedMeetingUrl("https://video.corp.example/daily", "zoom"); } catch { selfHostedZoom = true; }
ok("arbitrary host refused for zoom", selfHostedZoom);
let jitsiEvil = false;
try { assertAllowedMeetingUrl("https://meet.jit.si@evil.tld/x", "jitsi"); } catch { jitsiEvil = true; }
ok("jitsi still refuses userinfo", jitsiEvil);
ok("resolvePlatform jitsi", resolvePlatform("https://meet.jit.si/Room") === "jitsi");
// resolvePlatform no longer matches on a substring
let resolved: string;
try { resolved = resolvePlatform("https://meet.google.com.evil.tld/abc"); } catch { resolved = "threw"; }
ok("resolvePlatform refuses a look-alike host", resolved === "threw", `got ${resolved}`);
ok("resolvePlatform teams", resolvePlatform("https://teams.live.com/meet/1") === "teams");

console.log(`\nmeeting-url-allowlist: ${passed} passed, ${failed} failed`);
if (failed > 0) process.exit(1);
