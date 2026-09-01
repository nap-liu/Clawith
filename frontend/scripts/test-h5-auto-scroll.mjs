import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import vm from "node:vm";
import { loadCssEntry } from "./load-css-entry.mjs";
import { loadLocalSourceGraph } from "./load-local-source-graph.mjs";

const require = createRequire(import.meta.url);
const ts = require("typescript");
const __dirname = dirname(fileURLToPath(import.meta.url));
const sourcePath = resolve(
  __dirname,
  "../src/features/conversation/autoScroll.ts",
);
const sessionViewerSource = loadLocalSourceGraph(
  resolve(__dirname, "../src/components/SessionViewerDrawer.tsx"),
);
const timelineSource = loadLocalSourceGraph(
  resolve(
    __dirname,
    "../src/features/conversation/web/ConversationTimeline.tsx",
  ),
);
const globalStyles = loadCssEntry(resolve(__dirname, "../src/index.css"));
const compiled = ts.transpileModule(readFileSync(sourcePath, "utf8"), {
  compilerOptions: {
    module: ts.ModuleKind.CommonJS,
    target: ts.ScriptTarget.ES2020,
  },
}).outputText;
const localModule = { exports: {} };
vm.runInNewContext(
  compiled,
  {
    module: localModule,
    exports: localModule.exports,
    require,
  },
  { filename: sourcePath },
);

const {
  alignConversationScrollerToBottom,
  isConversationScrollerAtBottom,
  isConversationScrollbarPointer,
  isConversationScrollKey,
  scheduleStableConversationBottomScroll,
} = localModule.exports;

{
  const scroller = { scrollHeight: 900, scrollTop: 0 };
  alignConversationScrollerToBottom(scroller);
  assert.equal(scroller.scrollTop, 900);
}

{
  assert.equal(
    isConversationScrollerAtBottom({
      scrollHeight: 900,
      scrollTop: 300,
      clientHeight: 600,
    }),
    true,
  );
  assert.equal(
    isConversationScrollerAtBottom({
      scrollHeight: 900,
      scrollTop: 298,
      clientHeight: 600,
    }),
    true,
  );
  assert.equal(
    isConversationScrollerAtBottom({
      scrollHeight: 900,
      scrollTop: 280,
      clientHeight: 600,
    }),
    false,
  );
}

{
  const frames = new Map();
  const timers = new Map();
  let nextHandle = 1;
  let resizeCallback = null;
  let resizeDisconnected = false;
  let alignCount = 0;
  const environment = {
    requestFrame: (callback) => {
      const handle = nextHandle++;
      frames.set(handle, callback);
      return handle;
    },
    cancelFrame: (handle) => frames.delete(handle),
    setTimer: (callback) => {
      const handle = nextHandle++;
      timers.set(handle, callback);
      return handle;
    },
    clearTimer: (handle) => timers.delete(handle),
    observeResize: (_targets, callback) => {
      resizeCallback = callback;
      return () => {
        resizeDisconnected = true;
      };
    },
  };

  const cleanup = scheduleStableConversationBottomScroll({
    alignBottom: () => {
      alignCount += 1;
    },
    resizeTargets: [{}],
    environment,
  });
  assert.equal(
    alignCount,
    1,
    "must align synchronously after the history DOM mounts",
  );

  const firstFrame = [...frames.values()][0];
  frames.clear();
  firstFrame();
  assert.equal(alignCount, 2);
  const secondFrame = [...frames.values()][0];
  frames.clear();
  secondFrame();
  assert.equal(alignCount, 3, "must retry after virtual list measurement");

  resizeCallback();
  const resizeFrame = [...frames.values()][0];
  frames.clear();
  resizeFrame();
  assert.equal(alignCount, 4, "must realign after asynchronous content resize");

  cleanup();
  assert.equal(frames.size, 0);
  assert.equal(timers.size, 0);
  assert.equal(resizeDisconnected, true);
}

{
  assert.equal(
    isConversationScrollKey({
      key: "PageUp",
      altKey: false,
      ctrlKey: false,
      metaKey: false,
    }),
    true,
  );
  assert.equal(
    isConversationScrollKey({
      key: "ArrowDown",
      altKey: false,
      ctrlKey: false,
      metaKey: false,
    }),
    true,
  );
  assert.equal(
    isConversationScrollKey({
      key: "a",
      altKey: false,
      ctrlKey: false,
      metaKey: false,
    }),
    false,
  );
  assert.equal(
    isConversationScrollKey({
      key: "PageUp",
      altKey: false,
      ctrlKey: true,
      metaKey: false,
    }),
    false,
  );
}

{
  const element = {
    clientWidth: 280,
    offsetWidth: 300,
    clientHeight: 480,
    offsetHeight: 500,
    getBoundingClientRect: () => ({ right: 400, bottom: 600 }),
  };
  assert.equal(
    isConversationScrollbarPointer(element, {
      clientX: 390,
      clientY: 100,
      pointerType: "mouse",
    }),
    true,
  );
  assert.equal(
    isConversationScrollbarPointer(element, {
      clientX: 200,
      clientY: 590,
      pointerType: "mouse",
    }),
    true,
  );
  assert.equal(
    isConversationScrollbarPointer(element, {
      clientX: 200,
      clientY: 200,
      pointerType: "mouse",
    }),
    false,
  );
  assert.equal(
    isConversationScrollbarPointer(element, {
      clientX: 390,
      clientY: 100,
      pointerType: "touch",
    }),
    false,
  );
}

assert.match(
  sessionViewerSource,
  /useConversationAutoFollow\(\{[\s\S]*?contentKey:\s*timelineScrollAnchor,[\s\S]*?resetKey:\s*`\$\{sessionId \|\| ""\}:\$\{target\?\.anchorMessageId \|\| ""\}:\$\{target\?\.projectRunId \|\| ""\}`,[\s\S]*?enabled:\s*Boolean\([\s\S]*?!loading[\s\S]*?!resolvedAnchorMessageId/,
  "the embedded project group viewer must reuse standard Web Chat auto-follow and reset it for each exact anchor or Run",
);
assert.match(
  sessionViewerSource,
  /className="session-viewer-drawer__messages"[\s\S]*?\{\.\.\.autoFollowInteractionProps\}/,
  "the shared session scroller must preserve standard user-scroll pause and resume behavior",
);
assert.match(
  sessionViewerSource,
  /getConversationScrollAnchor\([\s\S]*?buildConversationEntries\(messages\),[\s\S]*?active/,
  "history, streaming, and standard timeline updates must share the canonical conversation content key",
);
assert.doesNotMatch(
  sessionViewerSource,
  /element\.scrollTop\s*=\s*element\.scrollHeight/,
  "the project viewer must not maintain a second ad-hoc bottom-scroll implementation",
);
assert.match(
  timelineSource,
  /findConversationAnchorEntryIndex\(entries, focusMessageId\)/,
  "the standard timeline must resolve the exact durable message inside a reused session",
);
assert.match(
  timelineSource,
  /rowVirtualizer\.scrollToIndex\(focusEntryIndex, \{ align: "center" \}\)[\s\S]*?element\.dataset\.messageId === focusMessageId[\s\S]*?scrollIntoView\(\{ block: "center", behavior: "auto" \}\)/,
  "exact anchors must reveal the matching virtual row and then center the matching durable message",
);
assert.match(
  globalStyles,
  /\.conversation-timeline__focus-anchor > \.chat-msg-row\s*\{[\s\S]*?animation: conversation-anchor-focus 5\.2s/,
  "the exact message highlight must remain clearly identifiable for more than five seconds",
);
assert.match(
  globalStyles,
  /@media \(prefers-reduced-motion: reduce\)[\s\S]*?conversation-anchor-focus-reduced 5\.2s step-end/,
  "reduced-motion users must receive a stable highlight without flashing animation",
);

console.log("conversation auto-scroll tests passed");
