/*! Clawith SDK — 中立平台能力层：身份(OAuth) + 触发(webhook hook)。零依赖。 */
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
    var returnTo = location.origin + location.pathname; // 干净 URL（不含 query）
    location.assign(API_BASE + "/api/sdk/auth/start?return_to=" + encodeURIComponent(returnTo));
    return new Promise(function () { /* 页面即将卸载，永不 resolve */ });
  }

  function ready() {
    if (state.readyPromise) return state.readyPromise;
    var params = new URLSearchParams(location.search);
    var code = params.get("code");
    var st = params.get("state");
    if (code && st) {
      state.readyPromise = exchange(code, st);
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
    return fetch(API_BASE + "/api/webhooks/t/" + encodeURIComponent(token), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    }).then(function (r) {
      return r.json().catch(function () { return { ok: r.ok }; });
    });
  }

  window.Clawith = {
    hook: HOOK,
    get user() { return state.user; },
    ready: ready,
    onReady: function (cb) { return ready().then(cb); },
    triggerHook: triggerHook
  };
})();
