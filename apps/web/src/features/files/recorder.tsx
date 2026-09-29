"use client";

import { Mic, MonitorSpeaker, Square, X } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";

import {
  type RecordingSource,
  describeMediaError,
  formatClock,
  pickMimeType,
  recordingFilename,
  unsupportedReason,
} from "./recording";

interface RecorderProps {
  /** Called with the finished recording; the caller uploads it. */
  onRecorded: (file: File) => void;
  onClose: () => void;
  testId?: string;
}

function stopTracks(stream: MediaStream | null) {
  stream?.getTracks().forEach((track) => track.stop());
}

/**
 * Records audio from the microphone, or from a tab/screen via getDisplayMedia where the
 * browser allows it. Recording can't start until the consent box is ticked.
 */
export function Recorder({ onRecorded, onClose, testId }: RecorderProps) {
  const [consent, setConsent] = useState(false);
  const [source, setSource] = useState<RecordingSource>("microphone");
  const [status, setStatus] = useState<"idle" | "starting" | "recording">("idle");
  const [error, setError] = useState<string | null>(null);
  const [elapsed, setElapsed] = useState(0);
  const recorder = useRef<MediaRecorder | null>(null);
  const stream = useRef<MediaStream | null>(null);
  const discard = useRef(false);
  const consentId = useId();

  // Leaving the form mid-recording stops the tracks and drops the recording.
  useEffect(
    () => () => {
      discard.current = true;
      if (recorder.current?.state === "recording") recorder.current.stop();
      stopTracks(stream.current);
    },
    [],
  );

  useEffect(() => {
    if (status !== "recording") return;
    const started = Date.now();
    const timer = window.setInterval(() => setElapsed((Date.now() - started) / 1000), 250);
    return () => window.clearInterval(timer);
  }, [status]);

  const start = async () => {
    if (!consent) return;
    setError(null);
    const env = {
      mediaDevices: typeof navigator !== "undefined" ? navigator.mediaDevices : undefined,
      MediaRecorder: typeof MediaRecorder !== "undefined" ? MediaRecorder : undefined,
      isSecureContext: typeof window !== "undefined" ? window.isSecureContext : undefined,
    };
    const unsupported = unsupportedReason(env, source);
    if (unsupported) {
      setError(unsupported);
      return;
    }
    const mimeType = pickMimeType((type) => MediaRecorder.isTypeSupported(type));
    if (mimeType === null) {
      setError(describeMediaError(new DOMException("", "NotSupportedError"), source));
      return;
    }
    setStatus("starting");
    let media: MediaStream | null = null;
    try {
      if (source === "microphone") {
        media = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
      } else {
        // Browsers only offer tab/system audio alongside video; keep the audio only.
        media = await navigator.mediaDevices.getDisplayMedia({ video: true, audio: true });
        media.getVideoTracks().forEach((track) => {
          track.stop();
          media?.removeTrack(track);
        });
        if (media.getAudioTracks().length === 0) throw new DOMException("no audio", "NoAudioTrack");
      }
      stream.current = media;
      const rec = new MediaRecorder(media, mimeType ? { mimeType, audioBitsPerSecond: 64_000 } : undefined);
      const chunks: Blob[] = [];
      rec.ondataavailable = (event) => {
        if (event.data.size > 0) chunks.push(event.data);
      };
      rec.onerror = (event) => {
        setError(describeMediaError((event as Event & { error?: unknown }).error ?? new Error("the recorder stopped"), source));
      };
      rec.onstop = () => {
        stopTracks(stream.current);
        stream.current = null;
        setStatus("idle");
        if (discard.current) return;
        const type = rec.mimeType || mimeType || "audio/webm";
        const blob = new Blob(chunks, { type });
        if (blob.size === 0) {
          setError("Nothing was recorded. Check the input and try again.");
          return;
        }
        onRecorded(new File([blob], recordingFilename(type), { type: type.split(";")[0] }));
      };
      // Ending the share from the browser's own "Stop sharing" bar finishes the recording too.
      media.getAudioTracks().forEach((track) => {
        track.onended = () => {
          if (rec.state === "recording") rec.stop();
        };
      });
      recorder.current = rec;
      discard.current = false;
      rec.start(1000);
      setElapsed(0);
      setStatus("recording");
    } catch (err) {
      stopTracks(media);
      setStatus("idle");
      setError(describeMediaError(err, source));
    }
  };

  const stop = () => {
    if (recorder.current?.state === "recording") recorder.current.stop();
  };

  const recording = status === "recording";
  return (
    <div className="space-y-2 rounded-md border border-slate-200 bg-slate-50 p-2.5" data-testid={testId}>
      <div className="flex items-center justify-between">
        <span className="text-xs font-medium text-slate-700">Record audio</span>
        <button
          type="button"
          onClick={() => {
            discard.current = true;
            stop();
            onClose();
          }}
          className="rounded p-0.5 text-slate-400 hover:bg-slate-200 hover:text-slate-700"
          aria-label="Close recorder"
        >
          <X className="size-3.5" />
        </button>
      </div>

      <label htmlFor={consentId} className="flex items-start gap-2 text-xs text-slate-700">
        <input
          id={consentId}
          type="checkbox"
          checked={consent}
          disabled={recording}
          onChange={(e) => setConsent(e.target.checked)}
          className="mt-0.5"
          data-testid={testId ? `${testId}-consent` : undefined}
        />
        <span>Everyone being recorded has been informed</span>
      </label>

      <fieldset className="flex gap-1.5" disabled={recording || status === "starting"}>
        <legend className="sr-only">What to record</legend>
        {(
          [
            ["microphone", "Microphone", Mic],
            ["tab", "Tab or screen audio", MonitorSpeaker],
          ] as const
        ).map(([value, label, Icon]) => (
          <button
            key={value}
            type="button"
            aria-pressed={source === value}
            onClick={() => setSource(value)}
            className={`flex items-center gap-1 rounded-md border px-2 py-1 text-xs ${
              source === value ? "border-indigo-400 bg-indigo-50 text-indigo-800" : "border-slate-300 bg-white text-slate-700 hover:bg-slate-100"
            }`}
          >
            <Icon className="size-3.5" aria-hidden /> {label}
          </button>
        ))}
      </fieldset>

      <div className="flex items-center gap-2">
        {recording ? (
          <button
            type="button"
            onClick={stop}
            className="flex items-center gap-1 rounded-md bg-red-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-red-700"
            data-testid={testId ? `${testId}-stop` : undefined}
          >
            <Square className="size-3" aria-hidden /> Stop and upload
          </button>
        ) : (
          <button
            type="button"
            onClick={() => void start()}
            disabled={!consent || status === "starting"}
            title={consent ? undefined : "Tick the box first"}
            className="flex items-center gap-1 rounded-md bg-indigo-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-indigo-700 disabled:cursor-not-allowed disabled:opacity-50"
            data-testid={testId ? `${testId}-start` : undefined}
          >
            <Mic className="size-3" aria-hidden /> {status === "starting" ? "Waiting for permission…" : "Start recording"}
          </button>
        )}
        {recording && (
          <span className="flex items-center gap-1.5 text-xs tabular-nums text-red-700" role="status" aria-live="polite">
            <span className="size-2 animate-pulse rounded-full bg-red-600" aria-hidden /> Recording {formatClock(elapsed)}
          </span>
        )}
      </div>
      {!consent && !recording && <p className="text-[11px] text-slate-500">Tick the box to enable recording.</p>}
      {error && (
        <p className="text-[11px] text-red-600" role="alert" data-testid={testId ? `${testId}-error` : undefined}>
          {error}
        </p>
      )}
    </div>
  );
}
