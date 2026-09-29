import { describe, expect, it } from "vitest";

import { describeMediaError, extensionFor, formatClock, pickMimeType, recordingFilename, unsupportedReason } from "./recording";

describe("pickMimeType", () => {
  it("prefers webm/opus, falls back in order, and lets the browser choose when none match", () => {
    expect(pickMimeType(() => true)).toBe("audio/webm;codecs=opus");
    expect(pickMimeType((t) => t === "audio/mp4")).toBe("audio/mp4");
    expect(pickMimeType(() => false)).toBe("");
    expect(pickMimeType(undefined)).toBeNull();
  });
});

describe("recording files", () => {
  it("names the file after the container", () => {
    expect(extensionFor("audio/webm;codecs=opus")).toBe("webm");
    expect(extensionFor("audio/ogg")).toBe("ogg");
    expect(extensionFor("audio/mp4")).toBe("m4a");
    expect(recordingFilename("audio/mp4", new Date(2026, 8, 29, 14, 5))).toBe("recording-2026-09-29-1405.m4a");
  });

  it("formats the timer", () => {
    expect(formatClock(0)).toBe("00:00");
    expect(formatClock(75.6)).toBe("01:15");
    expect(formatClock(3725)).toBe("1:02:05");
  });
});

describe("unsupportedReason", () => {
  const devices = { getUserMedia: () => Promise.reject(new Error()) } as unknown as MediaDevices;
  it("explains what's missing", () => {
    expect(unsupportedReason({ isSecureContext: false, MediaRecorder: {}, mediaDevices: devices }, "microphone")).toMatch(/secure/);
    expect(unsupportedReason({ mediaDevices: devices }, "microphone")).toMatch(/MediaRecorder/);
    expect(unsupportedReason({ MediaRecorder: {}, mediaDevices: {} }, "microphone")).toMatch(/microphone/);
    expect(unsupportedReason({ MediaRecorder: {}, mediaDevices: devices }, "tab")).toMatch(/tab or screen audio/);
    expect(unsupportedReason({ MediaRecorder: {}, mediaDevices: devices, isSecureContext: true }, "microphone")).toBeNull();
  });
});

describe("describeMediaError", () => {
  it("turns permission and device errors into clear messages", () => {
    expect(describeMediaError(new DOMException("", "NotAllowedError"), "microphone")).toMatch(/blocked/);
    expect(describeMediaError(new DOMException("", "NotAllowedError"), "tab")).toMatch(/cancelled or blocked/);
    expect(describeMediaError(new DOMException("", "NotFoundError"), "microphone")).toMatch(/No microphone/);
    expect(describeMediaError(new DOMException("", "NoAudioTrack"), "tab")).toMatch(/Share tab audio/);
    expect(describeMediaError(new Error("boom"), "microphone")).toBe("Recording failed: boom");
  });
});
