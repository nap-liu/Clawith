import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type CSSProperties,
  type ReactNode,
} from "react";

import "./ResizableSplitPane.css";

type Props = {
  first: ReactNode;
  second: ReactNode;
  ariaLabel: string;
  className?: string;
  defaultSize?: number;
  minSize?: number;
  maxSize?: number;
  storageKey?: string;
};

const clamp = (value: number, minimum: number, maximum: number) =>
  Math.min(Math.max(value, minimum), maximum);

export default function ResizableSplitPane({
  first,
  second,
  ariaLabel,
  className = "",
  defaultSize = 280,
  minSize = 220,
  maxSize = 520,
  storageKey,
}: Props) {
  const rootRef = useRef<HTMLDivElement>(null);
  const dragStartRef = useRef<{ clientX: number; size: number } | null>(null);
  const pendingSizeRef = useRef<number | null>(null);
  const animationFrameRef = useRef<number | null>(null);
  const [firstSize, setFirstSize] = useState(() => {
    if (!storageKey) return defaultSize;
    const stored = Number(window.localStorage.getItem(storageKey));
    return Number.isFinite(stored)
      ? clamp(stored, minSize, maxSize)
      : defaultSize;
  });

  const updateSize = useCallback(
    (nextSize: number) => {
      const available = rootRef.current?.clientWidth || maxSize * 2;
      const responsiveMaximum = Math.max(
        minSize,
        Math.min(maxSize, available - minSize),
      );
      setFirstSize(clamp(nextSize, minSize, responsiveMaximum));
    },
    [maxSize, minSize],
  );

  useEffect(() => {
    if (!storageKey) return;

    const persistenceTimer = window.setTimeout(() => {
      window.localStorage.setItem(storageKey, String(Math.round(firstSize)));
    }, 160);

    return () => window.clearTimeout(persistenceTimer);
  }, [firstSize, storageKey]);

  useEffect(() => {
    const root = rootRef.current;
    if (!root) return;
    const observer = new ResizeObserver(() => {
      setFirstSize((current) => {
        const available = root.clientWidth || maxSize * 2;
        const responsiveMaximum = Math.max(
          minSize,
          Math.min(maxSize, available - minSize),
        );
        return clamp(current, minSize, responsiveMaximum);
      });
    });
    observer.observe(root);
    return () => observer.disconnect();
  }, [maxSize, minSize]);

  useEffect(
    () => () => {
      if (animationFrameRef.current !== null) {
        window.cancelAnimationFrame(animationFrameRef.current);
      }
      document.body.classList.remove("is-resizing-split-pane");
    },
    [],
  );

  const scheduleSizeUpdate = (nextSize: number) => {
    pendingSizeRef.current = nextSize;
    if (animationFrameRef.current !== null) return;
    animationFrameRef.current = window.requestAnimationFrame(() => {
      animationFrameRef.current = null;
      const pendingSize = pendingSizeRef.current;
      pendingSizeRef.current = null;
      if (pendingSize !== null) updateSize(pendingSize);
    });
  };

  const stopDragging = () => {
    if (pendingSizeRef.current !== null) {
      updateSize(pendingSizeRef.current);
      pendingSizeRef.current = null;
    }
    if (animationFrameRef.current !== null) {
      window.cancelAnimationFrame(animationFrameRef.current);
      animationFrameRef.current = null;
    }
    dragStartRef.current = null;
    document.body.classList.remove("is-resizing-split-pane");
  };

  return (
    <div
      ref={rootRef}
      className={["resizable-split-pane", className].filter(Boolean).join(" ")}
      style={{ "--split-pane-first-size": `${firstSize}px` } as CSSProperties}
    >
      {first}
      <div
        className="resizable-split-pane__separator"
        role="separator"
        aria-label={ariaLabel}
        aria-orientation="vertical"
        aria-valuemin={minSize}
        aria-valuemax={maxSize}
        aria-valuenow={Math.round(firstSize)}
        tabIndex={0}
        onDoubleClick={() => updateSize(defaultSize)}
        onKeyDown={(event) => {
          const step = event.shiftKey ? 32 : 8;
          if (event.key === "ArrowLeft") {
            event.preventDefault();
            updateSize(firstSize - step);
          } else if (event.key === "ArrowRight") {
            event.preventDefault();
            updateSize(firstSize + step);
          } else if (event.key === "Home") {
            event.preventDefault();
            updateSize(minSize);
          } else if (event.key === "End") {
            event.preventDefault();
            updateSize(maxSize);
          }
        }}
        onPointerDown={(event) => {
          dragStartRef.current = { clientX: event.clientX, size: firstSize };
          event.currentTarget.setPointerCapture(event.pointerId);
          document.body.classList.add("is-resizing-split-pane");
        }}
        onPointerMove={(event) => {
          const dragStart = dragStartRef.current;
          if (!dragStart) return;
          scheduleSizeUpdate(
            dragStart.size + event.clientX - dragStart.clientX,
          );
        }}
        onPointerUp={stopDragging}
        onPointerCancel={stopDragging}
      >
        <span aria-hidden="true" />
      </div>
      {second}
    </div>
  );
}
