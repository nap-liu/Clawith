/*! Clawith SDK — 中立平台能力层：身份(OAuth) + 触发(webhook hook) + 水印。零依赖。 */
(function () {
  "use strict";

  var script = document.currentScript;
  var HOOK = script ? script.getAttribute("data-hook") : null;
  var API_BASE = (script && script.getAttribute("data-api-base")) || ""; // 默认同源

  var state = { user: null, readyPromise: null };

  function shortIdFromUrl() {
    var m = location.pathname.match(/\/p\/([A-Za-z0-9_-]+)/);
    return m ? m[1] : null;
  }

  function cleanCodeFromUrl() {
    try {
      var u = new URL(location.href);
      u.searchParams.delete("code");
      u.searchParams.delete("state");
      var qs = u.searchParams.toString();
      history.replaceState(null, "", u.pathname + (qs ? "?" + qs : "") + u.hash);
    } catch (e) { /* ignore */ }
  }

  function exchange(code, st) {
    return fetch(API_BASE + "/api/sdk/auth/exchange", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code: code, state: st })
    }).then(function (r) {
      if (!r.ok) throw new Error("clawith: exchange failed " + r.status);
      return r.json();
    }).then(function (info) {
      state.user = { userId: info.userId, userName: info.userName, mobile: info.mobile };
      cleanCodeFromUrl();
      return state.user;
    });
  }

  function startOAuth() {
    var returnTo = location.href.split("#")[0].split("?")[0]; // 干净 URL（不含 query/fragment，兼容 opaque origin）
    location.assign(API_BASE + "/api/sdk/auth/start?return_to=" + encodeURIComponent(returnTo));
    return new Promise(function () { /* 页面即将卸载，永不 resolve */ });
  }

  function ready() {
    if (state.readyPromise) return state.readyPromise;
    var params = new URLSearchParams(location.search);
    var code = params.get("code");
    var st = params.get("state");
    if (code && st) {
      state.readyPromise = exchange(code, st).catch(function (err) {
        state.readyPromise = null; // 允许下次 ready() 重试
        throw err;
      });
    } else {
      state.readyPromise = startOAuth();
    }
    return state.readyPromise;
  }

  function triggerHook(token, payload) {
    payload = payload || {};
    var body = {};
    for (var k in payload) { if (Object.prototype.hasOwnProperty.call(payload, k)) body[k] = payload[k]; }
    // 自动注入 report 上下文（隔离 Reflection session 靠它定位是哪个报告）
    body.report = Object.assign({ short_id: shortIdFromUrl(), title: document.title }, payload.report || {});
    return fetch(API_BASE + "/api/webhooks/t/" + token, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    }).then(function (r) {
      return r.json().catch(function () { return { ok: r.ok }; });
    });
  }

  // ─────────────────────────────────────────────────────────────────────────
  // 水印（防泄密）：用户名 + 手机尾号，全屏平铺斜向，叠在页面最顶层。
  //
  // 诚实上限：客户端水印挡不住铁了心的人——禁 JS、持久化改 DOM、或拦截 OAuth
  // 回调都能绕过。下面的加固只把"随手 F12 删节点 / toggle display"这类轻量操作
  // 堵死，不是密码学/取证保证。取证级溯源需服务端把身份渲进内容，而 /p/ 服务端
  // 渲染时并不知道访客是谁（身份是客户端 OAuth 才拿到），故本期不做。
  // ─────────────────────────────────────────────────────────────────────────

  var WM_ATTR = "data-clawith-wm";
  var wmEl = null;       // 覆盖层节点（JS 持引用，不依赖 DOM 查询重连）
  var wmCss = "";        // 当前规范 cssText
  var wmObserver = null; // 单例守护

  function xmlEsc(s) {
    return String(s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function lastDigits(mobile, n) {
    var d = String(mobile == null ? "" : mobile).replace(/\D/g, "");
    return d.slice(-n);
  }

  // 纯函数：user → 水印文本 | null。规则见 spec §6。
  function computeWatermarkText(user) {
    if (!user) return null;
    var name = user.userName != null && String(user.userName).trim() ? String(user.userName).trim() : "";
    var tail = lastDigits(user.mobile, 4);
    if (name && tail) return name + " " + tail;
    if (name) return name;
    if (user.userId != null && String(user.userId).trim()) return String(user.userId).trim();
    return null;
  }

  // 纯函数：文本 → 单块平铺图的 SVG data: URI（旋转、半透明）。
  function buildTileDataUri(text, opts) {
    opts = opts || {};
    var W = opts.tileWidth || 170;
    var H = opts.tileHeight || 115;
    var fontSize = opts.fontSize || 14;
    var color = opts.color || "rgba(0,0,0,0.10)";
    var angle = opts.angle != null ? opts.angle : -22;
    var svg =
      "<svg xmlns='http://www.w3.org/2000/svg' width='" + W + "' height='" + H + "'>" +
      "<text x='50%' y='50%' fill='" + color + "' font-size='" + fontSize + "' " +
      "font-family='-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica Neue,Arial,sans-serif' " +
      "text-anchor='middle' dominant-baseline='middle' " +
      "transform='rotate(" + angle + " " + (W / 2) + " " + (H / 2) + ")'>" +
      xmlEsc(text) + "</text></svg>";
    return "data:image/svg+xml," + encodeURIComponent(svg);
  }

  function wmCanonicalCss(bgUri) {
    return [
      "position:fixed", "top:0", "left:0", "right:0", "bottom:0",
      "width:100vw", "height:100vh", "margin:0", "padding:0", "border:0",
      "pointer-events:none", "user-select:none", "z-index:2147483647",
      "display:block", "visibility:visible", "opacity:1",
      "background-repeat:repeat", "background-position:0 0", "background-size:auto",
      'background-image:url("' + bgUri + '")'
    ].map(function (r) { return r + " !important"; }).join(";");
  }

  function applyWmStyle() {
    if (wmEl) wmEl.style.cssText = wmCss;
  }

  // 关键属性是否被改坏（语义判断，非整串比较 → 重置后收敛、不死循环）。
  function wmDrifted() {
    if (!wmEl) return false;
    var s = wmEl.style;
    return (
      s.getPropertyValue("display") !== "block" ||
      s.getPropertyValue("visibility") !== "visible" ||
      s.getPropertyValue("position") !== "fixed" ||
      s.getPropertyValue("pointer-events") !== "none" ||
      s.getPropertyPriority("display") !== "important" ||
      s.getPropertyValue("background-image").indexOf("data:image/svg+xml") === -1
    );
  }

  function startWmGuard(root) {
    if (wmObserver) return;                           // 单例守护
    if (typeof MutationObserver === "undefined") return;
    wmObserver = new MutationObserver(function () {
      if (wmEl && !root.contains(wmEl)) root.appendChild(wmEl); // 被删 → 重建
      if (wmDrifted()) applyWmStyle();                          // 被改样式 → 重置
    });
    wmObserver.observe(root, { childList: true });
    wmObserver.observe(wmEl, { attributes: true, attributeFilter: ["style", "class", WM_ATTR] });
  }

  // 副作用：渲染/更新水印并启动守护。幂等——多次调用只保留一个覆盖层。
  function renderWatermark(text, opts) {
    var root = document.documentElement;
    wmCss = wmCanonicalCss(buildTileDataUri(text, opts));
    if (!wmEl) {
      wmEl = document.querySelector("[" + WM_ATTR + "]") || document.createElement("div");
      wmEl.setAttribute(WM_ATTR, "1");
      wmEl.setAttribute("aria-hidden", "true");
    }
    applyWmStyle();
    if (!root.contains(wmEl)) root.appendChild(wmEl);
    startWmGuard(root);
  }

  // 公开接口。opts.text 显式给文本（绕过 OAuth，便于测试/特殊场景）；
  // opts.user 显式给身份；否则 ready() 取当前登录用户。
  function watermark(opts) {
    opts = opts || {};
    if (opts.text != null) {
      renderWatermark(String(opts.text), opts);
      return Promise.resolve(true);
    }
    if (opts.user) {
      var t0 = computeWatermarkText(opts.user);
      if (t0) renderWatermark(t0, opts);
      return Promise.resolve(!!t0);
    }
    return ready().then(function (u) {
      var t = computeWatermarkText(u);
      if (t) renderWatermark(t, opts);
      return !!t;
    });
  }

  window.Clawith = {
    hook: HOOK,
    get user() { return state.user; },
    ready: ready,
    onReady: function (cb) { return ready().then(cb); },
    triggerHook: triggerHook,
    watermark: watermark,
    _computeWatermarkText: computeWatermarkText // 暴露纯函数供 e2e 断言
  };

  // 声明式开启：<script ... data-watermark> → 加载即自动取身份并渲染水印。
  if (script && script.hasAttribute("data-watermark")) {
    try { watermark(); } catch (e) { /* 不阻断页面 */ }
  }
})();
