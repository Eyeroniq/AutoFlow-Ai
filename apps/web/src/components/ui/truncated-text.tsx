"use client";

import { useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

const SHOW_DELAY_MS = 250;
const GAP = 6;
const MARGIN = 8;

/**
 * One line of text, cut off with an ellipsis where it runs out of room. Hovering it shows
 * the whole text in a tooltip, but only when something was actually cut off. The tooltip
 * is portaled to <body> so neighbouring canvas nodes can't cover it, and it stays readable
 * at any zoom level.
 */
export function TruncatedText({ text, className = "", testId }: { text: string; className?: string; testId?: string }) {
  const anchor = useRef<HTMLParagraphElement>(null);
  const bubble = useRef<HTMLDivElement>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [box, setBox] = useState<DOMRect | null>(null);
  const [position, setPosition] = useState<{ left: number; top: number } | null>(null);

  const cancel = () => {
    if (timer.current) clearTimeout(timer.current);
    timer.current = null;
  };
  const hide = () => {
    cancel();
    setBox(null);
    setPosition(null);
  };
  const show = () => {
    cancel();
    timer.current = setTimeout(() => {
      const el = anchor.current;
      // Layout sizes, so this holds under the canvas's zoom transform too.
      if (el && el.scrollWidth > el.clientWidth) setBox(el.getBoundingClientRect());
    }, SHOW_DELAY_MS);
  };

  // Below the text; flipped above it near the bottom of the window, kept inside it sideways.
  useLayoutEffect(() => {
    if (!box || !bubble.current) return;
    const { width, height } = bubble.current.getBoundingClientRect();
    const left = Math.min(Math.max(MARGIN, box.left), window.innerWidth - width - MARGIN);
    const below = box.bottom + GAP;
    const top = below + height > window.innerHeight - MARGIN ? Math.max(MARGIN, box.top - GAP - height) : below;
    setPosition({ left, top });
  }, [box]);

  useLayoutEffect(() => cancel, []);

  return (
    <>
      <p
        ref={anchor}
        className={`truncate ${className}`}
        onMouseEnter={show}
        onMouseLeave={hide}
        onMouseDown={hide}
        onWheel={hide}
        data-testid={testId}
      >
        {text}
      </p>
      {box &&
        createPortal(
          <div
            ref={bubble}
            role="tooltip"
            data-testid={testId ? `${testId}-tooltip` : undefined}
            style={position ?? { left: box.left, top: box.bottom + GAP, visibility: "hidden" }}
            className="pointer-events-none fixed z-[1000] max-w-md whitespace-pre-wrap wrap-anywhere rounded-md bg-slate-900 px-2.5 py-1.5 font-mono text-[11px] leading-4 text-slate-50 shadow-lg"
          >
            {text}
          </div>,
          document.body,
        )}
    </>
  );
}
