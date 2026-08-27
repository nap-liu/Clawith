import Editor, { DiffEditor, loader, type Monaco } from "@monaco-editor/react";
import * as localMonaco from "monaco-editor";
import CssWorker from "monaco-editor/esm/vs/language/css/css.worker?worker";
import EditorWorker from "monaco-editor/esm/vs/editor/editor.worker?worker";
import HtmlWorker from "monaco-editor/esm/vs/language/html/html.worker?worker";
import JsonWorker from "monaco-editor/esm/vs/language/json/json.worker?worker";
import TypeScriptWorker from "monaco-editor/esm/vs/language/typescript/ts.worker?worker";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  DOCUMENT_THEME_CHANGE_EVENT,
  type ResolvedTheme,
} from "../../../utils/themeMode";

loader.config({ monaco: localMonaco });

window.MonacoEnvironment = {
  getWorker: (_moduleId, label) => {
    if (label === "json") return new JsonWorker();
    if (["css", "scss", "less"].includes(label)) return new CssWorker();
    if (["html", "handlebars", "razor"].includes(label))
      return new HtmlWorker();
    if (["typescript", "javascript"].includes(label))
      return new TypeScriptWorker();
    return new EditorWorker();
  },
};

type Props = {
  path: string;
  value: string;
  readOnly?: boolean;
  onChange?: (value: string) => void;
  ariaLabel?: string;
};

type DiffProps = {
  path: string;
  original: string;
  modified: string;
  ariaLabel?: string;
};

const languageByExtension: Record<string, string> = {
  bash: "shell",
  c: "c",
  cc: "cpp",
  cpp: "cpp",
  cs: "csharp",
  css: "css",
  csv: "plaintext",
  dockerfile: "dockerfile",
  env: "ini",
  go: "go",
  gql: "graphql",
  graphql: "graphql",
  h: "c",
  hpp: "cpp",
  htm: "html",
  html: "html",
  ini: "ini",
  java: "java",
  js: "javascript",
  json: "json",
  jsonc: "json",
  jsx: "javascript",
  kt: "kotlin",
  less: "less",
  lua: "lua",
  md: "markdown",
  markdown: "markdown",
  mjs: "javascript",
  php: "php",
  properties: "ini",
  py: "python",
  rb: "ruby",
  rs: "rust",
  scss: "scss",
  sh: "shell",
  sql: "sql",
  swift: "swift",
  toml: "ini",
  ts: "typescript",
  tsx: "typescript",
  txt: "plaintext",
  vue: "html",
  xml: "xml",
  yaml: "yaml",
  yml: "yaml",
};

function languageForPath(path: string) {
  const name = path.split("/").pop()?.toLowerCase() || "";
  if (name === "dockerfile") return "dockerfile";
  if (name === "makefile") return "plaintext";
  const extension = name.includes(".") ? name.split(".").pop() || "" : "";
  return languageByExtension[extension] || "plaintext";
}

function currentTheme(): ResolvedTheme {
  return document.documentElement.getAttribute("data-theme") === "dark"
    ? "dark"
    : "light";
}

function toMonacoColor(value: string): string | undefined {
  const probe = document.createElement("span");
  probe.style.color = value;
  probe.style.display = "none";
  document.body.appendChild(probe);
  const resolved = getComputedStyle(probe).color;
  probe.remove();
  const channels = resolved.match(/[\d.]+/g)?.map(Number);
  if (
    !channels ||
    channels.length < 3 ||
    channels.slice(0, 3).some((channel) => !Number.isFinite(channel))
  )
    return undefined;
  const bytes = channels
    .slice(0, 3)
    .map((channel) => Math.max(0, Math.min(255, Math.round(channel))));
  if (channels.length > 3 && channels[3] < 1)
    bytes.push(Math.max(0, Math.min(255, Math.round(channels[3] * 255))));
  return `${String.fromCharCode(35)}${bytes.map((channel) => channel.toString(16).padStart(2, "0")).join("")}`;
}

function defineClawithTheme(monaco: Monaco, theme: ResolvedTheme) {
  const rootStyle = getComputedStyle(document.documentElement);
  const colors: Record<string, string> = {};
  const tokenMap: Record<string, string> = {
    "editor.background": "--bg-primary",
    "editor.foreground": "--text-primary",
    "editorLineNumber.foreground": "--text-tertiary",
    "editorLineNumber.activeForeground": "--text-secondary",
    "editor.lineHighlightBackground": "--bg-tertiary",
    "editor.selectionBackground": "--border-strong",
    "editor.inactiveSelectionBackground": "--border-default",
    "editorCursor.foreground": "--info",
    "editorIndentGuide.background1": "--border-subtle",
    "editorIndentGuide.activeBackground1": "--border-strong",
    "scrollbarSlider.background": "--border-subtle",
    "scrollbarSlider.hoverBackground": "--border-strong",
  };
  Object.entries(tokenMap).forEach(([editorColor, cssToken]) => {
    const tokenValue = rootStyle.getPropertyValue(cssToken).trim();
    const resolved = tokenValue ? toMonacoColor(`var(${cssToken})`) : undefined;
    if (resolved) colors[editorColor] = resolved;
  });
  monaco.editor.defineTheme(`clawith-${theme}`, {
    base: theme === "dark" ? "vs-dark" : "vs",
    inherit: true,
    rules: [],
    colors,
  });
}

function useProjectMonacoTheme() {
  const [theme, setTheme] = useState<ResolvedTheme>(currentTheme);

  useEffect(() => {
    const update = () => setTheme(currentTheme());
    const observer = new MutationObserver(update);
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["data-theme"],
    });
    window.addEventListener(DOCUMENT_THEME_CHANGE_EVENT, update);
    return () => {
      observer.disconnect();
      window.removeEventListener(DOCUMENT_THEME_CHANGE_EVENT, update);
    };
  }, []);

  useEffect(() => {
    defineClawithTheme(localMonaco, theme);
    localMonaco.editor.setTheme(`clawith-${theme}`);
  }, [theme]);

  return theme;
}

export default function ProjectCodeEditor({
  path,
  value,
  readOnly = false,
  onChange,
  ariaLabel,
}: Props) {
  const { t } = useTranslation();
  const theme = useProjectMonacoTheme();
  const language = languageForPath(path);

  return (
    <div
      className="project-file-workspace__monaco"
      aria-label={ariaLabel || t("projectWorkspaceFiles.editorAria")}
    >
      <Editor
        key={`${path || "untitled.txt"}:${language}`}
        width="100%"
        height="100%"
        path={path || "untitled.txt"}
        language={languageForPath(path)}
        value={value}
        theme={`clawith-${theme}`}
        beforeMount={(monaco) => defineClawithTheme(monaco, theme)}
        onMount={(editor, monaco) => {
          const model = editor.getModel();
          if (model && model.getLanguageId() !== language) {
            monaco.editor.setModelLanguage(model, language);
          }
        }}
        onChange={(next) => onChange?.(next ?? "")}
        loading={
          <span className="project-file-workspace__editor-loading">
            {t("projectWorkspaceFiles.loadingEditor")}
          </span>
        }
        options={{
          automaticLayout: true,
          readOnly,
          domReadOnly: readOnly,
          fontFamily: "var(--font-mono)",
          fontLigatures: true,
          fontSize: 13,
          lineHeight: 21,
          lineNumbersMinChars: 3,
          minimap: { enabled: false },
          overviewRulerBorder: false,
          padding: { top: 14, bottom: 14 },
          renderLineHighlight: "line",
          scrollBeyondLastLine: false,
          smoothScrolling: true,
          stickyScroll: { enabled: true },
          wordWrap: "on",
          wrappingIndent: "indent",
          accessibilitySupport: "auto",
        }}
      />
    </div>
  );
}

export function ProjectCodeDiffEditor({
  path,
  original,
  modified,
  ariaLabel,
}: DiffProps) {
  const { t } = useTranslation();
  const theme = useProjectMonacoTheme();
  const language = languageForPath(path);

  return (
    <div
      className="project-file-workspace__monaco project-git-diff__monaco"
      aria-label={ariaLabel || t("projectWorkspaceFiles.diffAria")}
    >
      <DiffEditor
        width="100%"
        height="100%"
        original={original}
        modified={modified}
        originalLanguage={language}
        modifiedLanguage={language}
        originalModelPath={`git-original://${path}`}
        modifiedModelPath={`git-modified://${path}`}
        theme={`clawith-${theme}`}
        beforeMount={(monaco) => defineClawithTheme(monaco, theme)}
        loading={
          <span className="project-file-workspace__editor-loading">
            {t("projectWorkspaceFiles.loadingDiff")}
          </span>
        }
        options={{
          automaticLayout: true,
          readOnly: true,
          domReadOnly: true,
          renderSideBySide: true,
          enableSplitViewResizing: true,
          fontFamily: "var(--font-mono)",
          fontLigatures: true,
          fontSize: 13,
          lineHeight: 21,
          lineNumbersMinChars: 3,
          minimap: { enabled: false },
          overviewRulerBorder: false,
          renderOverviewRuler: false,
          scrollBeyondLastLine: false,
          smoothScrolling: true,
          stickyScroll: { enabled: false },
          wordWrap: "on",
          wrappingIndent: "indent",
          accessibilitySupport: "auto",
        }}
      />
    </div>
  );
}
