import { useEffect, useState, type CSSProperties } from "react";

import "./Avatar.css";

type AvatarProps = {
  src?: string | null;
  name?: string | null;
  alt?: string;
  className?: string;
  style?: CSSProperties;
};

function avatarFallback(name?: string | null): string {
  return String(name || "?").trim().slice(0, 1).toLocaleUpperCase() || "?";
}

export default function Avatar({
  src,
  name,
  alt = "",
  className = "",
  style,
}: AvatarProps) {
  const [failed, setFailed] = useState(false);
  const token =
    typeof window === "undefined"
      ? ""
      : window.localStorage.getItem("token") || "";
  const resolvedSrc =
    src && src.startsWith("/api") && token
      ? `${src}${src.includes("?") ? "&" : "?"}token=${encodeURIComponent(token)}`
      : src;

  useEffect(() => setFailed(false), [resolvedSrc]);

  return (
    <span
      className={`ui-avatar ${className}`.trim()}
      style={style}
      aria-hidden={alt ? undefined : true}
    >
      {resolvedSrc && !failed ? (
        <img
          src={resolvedSrc}
          alt={alt}
          loading="lazy"
          decoding="async"
          referrerPolicy="no-referrer"
          onError={() => setFailed(true)}
        />
      ) : (
        <span className="ui-avatar__fallback">{avatarFallback(name)}</span>
      )}
    </span>
  );
}
