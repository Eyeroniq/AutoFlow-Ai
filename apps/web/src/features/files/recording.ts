/** Browser audio recording helpers (MediaRecorder), kept free of React for testing. */

export type RecordingSource = "microphone" | "tab";

/** Container/codec pairs to try, best first. The server detects the real type from the bytes. */
export const RECORDING_TYPES = ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus", "audio/mp4", "audio/mpeg"];

/** The first type this browser can record, "" to let it choose, or null when it can't record at all. */
export function pickMimeType(isTypeSupported: ((type: string) => boolean) | undefined): string | null {
  if (!isTypeSupported) return null;
  return RECORDING_TYPES.find((type) => isTypeSupported(type)) ?? "";
}

export function extensionFor(mimeType: string): string {
  const base = mimeType.split(";")[0].trim().toLowerCase();
  if (base === "audio/ogg") return "ogg";
  if (base === "audio/mp4") return "m4a";
  if (base === "audio/mpeg") return "mp3";
  return "webm";
}

/** e.g. "recording-2026-09-29-1405.webm" */
export function recordingFilename(mimeType: string, now: Date = new Date()): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  const stamp = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}-${pad(now.getHours())}${pad(now.getMinutes())}`;
  return `recording-${stamp}.${extensionFor(mimeType)}`;
}

/** Why recording can't start in this browser, or null when it can. */
export function unsupportedReason(
  env: { mediaDevices?: Partial<MediaDevices>; MediaRecorder?: unknown; isSecureContext?: boolean },
  source: RecordingSource,
): string | null {
  if (env.isSecureContext === false) return "Recording needs a secure page (https:// or localhost).";
  if (!env.MediaRecorder) return "This browser can't record audio (no MediaRecorder). Upload a file instead.";
  if (source === "microphone" && !env.mediaDevices?.getUserMedia) {
    return "This browser doesn't give pages access to a microphone. Upload a file instead.";
  }
  if (source === "tab" && !env.mediaDevices?.getDisplayMedia) {
    return "This browser can't record tab or screen audio (Chrome and Edge on desktop can). Use the microphone or upload a file.";
  }
  return null;
}

/** A clear message for a getUserMedia/getDisplayMedia/MediaRecorder failure. */
export function describeMediaError(error: unknown, source: RecordingSource): string {
  const name = error instanceof DOMException || error instanceof Error ? error.name : "";
  const what = source === "microphone" ? "microphone" : "tab or screen";
  switch (name) {
    case "NotAllowedError":
    case "SecurityError":
      return source === "microphone"
        ? "Microphone access was blocked. Allow it from the icon in the address bar, then try again."
        : "Sharing was cancelled or blocked. Try again and pick a tab or screen to share.";
    case "NotFoundError":
    case "OverconstrainedError":
      return source === "microphone" ? "No microphone was found. Plug one in, or upload a file." : "Nothing was shared to record.";
    case "NotReadableError":
    case "AbortError":
      return `The ${what} is in use by another app or couldn't be opened. Close other apps using it and try again.`;
    case "NoAudioTrack":
      return 'The shared tab or screen has no audio. Share again and tick "Share tab audio" (or "Share system audio").';
    case "NotSupportedError":
      return "This browser can't record in any format the server accepts. Upload a file instead.";
    default:
      return `Recording failed: ${error instanceof Error ? error.message : String(error)}`;
  }
}

export function formatClock(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  const pad = (n: number) => String(n).padStart(2, "0");
  return s >= 3600 ? `${Math.floor(s / 3600)}:${pad(Math.floor((s % 3600) / 60))}:${pad(s % 60)}` : `${pad(Math.floor(s / 60))}:${pad(s % 60)}`;
}
