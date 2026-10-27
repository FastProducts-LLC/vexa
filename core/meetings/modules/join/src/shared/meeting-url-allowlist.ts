/**
 * Meeting-URL allowlist — defence in depth for security finding #1.
 *
 * meeting-api validates every meeting URL before it reaches a bot (meeting_url_policy.py); this is
 * the bot-side twin, so a bot never navigates its browser to a host that is not a meeting platform
 * even if a URL slipped past (an old control plane, a hand-built invocation). Same rules:
 *   - https only; no userinfo, no explicit port, no IP literal, no trailing dot, ASCII host only;
 *   - no backslash / whitespace / control characters, no percent-encoding in the authority, no
 *     encoded dot/slash or dot segments in the path;
 *   - the host equals an allowlisted host or is a subdomain of an allowlisted suffix (dot boundary);
 *   - when a platform is named, the host must belong to it.
 * Jitsi: meet.jit.si and 8x8.vc (+subdomains) are inferable; a SELF-HOSTED Jitsi lives on an
 * arbitrary host that only meeting-api knows (VEXA_JITSI_HOSTS), so with an explicit
 * platform "jitsi" any structurally valid host passes here and meeting-api is authoritative.
 */

export type AllowlistedPlatform = "google_meet" | "teams" | "zoom" | "jitsi";

const PLATFORM_HOSTS: Record<AllowlistedPlatform, { exact: string[]; suffixes: string[] }> = {
  google_meet: { exact: ["meet.google.com"], suffixes: [] },
  // The join layer's Teams web-client hosts: world-wide, personal, cloud.microsoft, GCC High/DoD.
  teams: {
    exact: [],
    suffixes: ["teams.microsoft.com", "teams.live.com", "teams.cloud.microsoft", "teams.microsoft.us"],
  },
  zoom: { exact: [], suffixes: ["zoom.us", "zoomgov.com"] },
  jitsi: { exact: ["meet.jit.si"], suffixes: ["8x8.vc"] },
};

export class MeetingUrlNotAllowedError extends Error {
  constructor(message: string) {
    super(`meeting URL refused: ${message}`);
    this.name = "MeetingUrlNotAllowedError";
  }
}

const FORBIDDEN_RAW = /[\\\u0000- \u007f]/;
const FORBIDDEN_ENCODED = /%(?:5c|[01][0-9a-f]|7f)/i;
const ENCODED_PATH_TRICK = /%(?:2e|2f)/i;
const HOST_LABEL = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/;

/** The platform an (already lower-case ASCII) host belongs to, or null. */
export function allowlistedPlatformForHost(host: string): AllowlistedPlatform | null {
  for (const [platform, { exact, suffixes }] of Object.entries(PLATFORM_HOSTS)) {
    if (exact.includes(host) || suffixes.some((s) => host === s || host.endsWith(`.${s}`))) {
      return platform as AllowlistedPlatform;
    }
  }
  return null;
}

/**
 * Throw MeetingUrlNotAllowedError unless `raw` is an https meeting link on an allowlisted host
 * (and on `platform`'s host when one is given). Returns the platform the host belongs to.
 */
export function assertAllowedMeetingUrl(raw: unknown, platform?: string): AllowlistedPlatform {
  if (typeof raw !== "string" || raw.length === 0) throw new MeetingUrlNotAllowedError("not a string");
  if (raw.length > 2048) throw new MeetingUrlNotAllowedError("too long");
  if (FORBIDDEN_RAW.test(raw) || FORBIDDEN_ENCODED.test(raw)) {
    throw new MeetingUrlNotAllowedError("backslash, whitespace or control character");
  }
  if (!/^https:\/\//i.test(raw)) throw new MeetingUrlNotAllowedError("not https");
  const rest = raw.slice("https://".length);
  const authority = rest.split(/[/?#]/, 1)[0];
  const rawPath = rest.slice(authority.length).split(/[?#]/, 1)[0];
  if (!authority || authority.includes("@") || authority.includes("%") || authority.includes(":")) {
    throw new MeetingUrlNotAllowedError("credentials, encoded host or explicit port");
  }
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    throw new MeetingUrlNotAllowedError("does not parse");
  }
  const host = url.hostname;
  // WHATWG URL maps IDN / full-width dots / numeric IPv4 spellings; any rewrite means a look-alike.
  if (host !== authority.toLowerCase()) throw new MeetingUrlNotAllowedError("non-canonical host");
  if (url.protocol !== "https:" || url.username || url.password || url.port) {
    throw new MeetingUrlNotAllowedError("scheme, credentials or port");
  }
  const labels = host.split(".");
  if (labels.length < 2 || !labels.every((l) => HOST_LABEL.test(l)) || /^[0-9.]+$/.test(host)) {
    throw new MeetingUrlNotAllowedError("invalid host or IP literal");
  }
  if (ENCODED_PATH_TRICK.test(rawPath) || rawPath.split("/").some((seg) => seg === "." || seg === "..")) {
    throw new MeetingUrlNotAllowedError("dot segments or encoded separators in the path");
  }
  const hostPlatform = allowlistedPlatformForHost(host);
  if (!hostPlatform && platform === "jitsi") return "jitsi"; // self-hosted: meeting-api allowlists it
  if (!hostPlatform) throw new MeetingUrlNotAllowedError(`host ${host} is not a meeting platform host`);
  if (platform && platform !== hostPlatform) {
    throw new MeetingUrlNotAllowedError(`host ${host} is not a ${platform} host`);
  }
  return hostPlatform;
}
