import type { CSSProperties, ReactNode } from "react";

import "./ProjectAgentCapabilityPanel.css";

export type ProjectAgentCapabilityTab<Value extends string> = {
  value: Value;
  icon: ReactNode;
  label: string;
  count: ReactNode;
};

export default function ProjectAgentCapabilityPanel<Value extends string>({
  value,
  tabs,
  ariaLabel,
  onChange,
  children,
  className = "",
  bodyClassName = "",
}: {
  value: Value;
  tabs: readonly ProjectAgentCapabilityTab<Value>[];
  ariaLabel: string;
  onChange: (value: Value) => void;
  children: ReactNode;
  className?: string;
  bodyClassName?: string;
}) {
  const style = {
    "--project-agent-capability-columns": tabs.length,
  } as CSSProperties;

  return (
    <div
      className={`project-agent-capability-panel${className ? ` ${className}` : ""}`}
      style={style}
    >
      <nav
        className="project-workspace__member-capability-rail"
        aria-label={ariaLabel}
      >
        {tabs.map((tab) => (
          <button
            key={tab.value}
            type="button"
            className={value === tab.value ? "is-active" : ""}
            aria-pressed={value === tab.value}
            onClick={() => onChange(tab.value)}
          >
            {tab.icon}
            <strong>{tab.label}</strong>
            <small>{tab.count}</small>
          </button>
        ))}
      </nav>
      <div
        className={`project-workspace__member-capability-body${bodyClassName ? ` ${bodyClassName}` : ""}`}
      >
        {children}
      </div>
    </div>
  );
}
