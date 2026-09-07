import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { loadTypeScriptModule } from "./load-typescript-module.mjs";

const {
  alignConversationScrollerToBottom,
  isConversationScrollerAtBottom,
  isConversationScrollbarPointer,
  isConversationScrollKey,
  scheduleStableConversationBottomScroll,
} = loadTypeScriptModule(fileURLToPath(
  new URL("../src/features/conversation/autoScroll.ts", import.meta.url),
));

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
  const scroller = { scrollHeight: 900, scrollTop: 0, clientHeight: 600 };
  const alignBottom = () => {
    alignConversationScrollerToBottom(scroller);
    // A DOM scroller clamps the assigned scrollTop to its available range.
    scroller.scrollTop = Math.min(scroller.scrollTop, scroller.scrollHeight - scroller.clientHeight);
  };
  const flushFrames = () => {
    for (let pass = 0; frames.size; pass += 1) {
      assert.ok(pass < 20, "scroll scheduling must settle");
      const pending = [...frames.values()];
      frames.clear();
      pending.forEach((callback) => callback());
    }
  };
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
    alignBottom,
    resizeTargets: [scroller],
    environment,
  });
  assert.equal(scroller.scrollTop, 300, "history mounts at the bottom synchronously");

  scroller.scrollHeight = 1200;
  flushFrames();
  assert.equal(scroller.scrollTop, 600, "settle at the measured content bottom");

  scroller.scrollHeight = 1800;
  resizeCallback();
  flushFrames();
  assert.equal(scroller.scrollTop, 1200, "follow late content expansion");

  resizeCallback();
  const pendingCallbacks = [...frames.values(), ...timers.values()];
  cleanup();
  assert.equal(frames.size, 0);
  assert.equal(timers.size, 0);
  assert.equal(resizeDisconnected, true);
  scroller.scrollTop = 100;
  scroller.scrollHeight = 2400;
  pendingCallbacks.forEach((callback) => callback());
  assert.equal(scroller.scrollTop, 100, "disposed work must preserve user scroll position");
  cleanup();
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

console.log("conversation auto-scroll tests passed");
