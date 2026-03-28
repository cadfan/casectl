/**
 * casectl real-time dashboard updates via Server-Sent Events (SSE).
 *
 * Connects to /api/sse and triggers HTMX partial refreshes when the
 * server pushes state-change events.  This achieves <500ms round-trip
 * from hardware change to dashboard reflection.
 *
 * Falls back to HTMX polling (already configured in the HTML) if SSE
 * is unavailable or the connection drops.
 */
(function () {
  "use strict";

  // -- Configuration -------------------------------------------------------

  /** Map SSE event names to HTMX partial element IDs to refresh. */
  var REFRESH_MAP = {
    "refresh:fan":     ["fan-partial"],
    "refresh:led":     ["led-partial"],
    "refresh:oled":    ["oled-partial"],
    "refresh:monitor": ["monitor-partial"],
    "refresh:all":     ["fan-partial", "led-partial", "oled-partial", "monitor-partial"],
  };

  /** Reconnect delay in ms after connection loss. */
  var RECONNECT_DELAY = 2000;

  /** Maximum reconnect delay (exponential backoff cap). */
  var MAX_RECONNECT_DELAY = 30000;

  // -- State ---------------------------------------------------------------

  var eventSource = null;
  var reconnectDelay = RECONNECT_DELAY;
  var reconnectTimer = null;
  var connected = false;

  // -- Helpers -------------------------------------------------------------

  /**
   * Trigger an HTMX refresh on the given element ID.
   * Uses htmx.trigger() to fire a custom event that the element
   * listens for, or falls back to direct htmx.ajax() GET.
   */
  function refreshPartial(elementId) {
    var el = document.getElementById(elementId);
    if (!el) return;

    // Get the hx-get URL from the element.
    var url = el.getAttribute("hx-get");
    if (url && typeof htmx !== "undefined") {
      htmx.ajax("GET", url, { target: "#" + elementId, swap: "innerHTML" });
    }
  }

  /**
   * Handle an SSE event by refreshing the relevant dashboard partials.
   */
  function handleEvent(eventName, data) {
    var targets = REFRESH_MAP[eventName];
    if (!targets) return;

    for (var i = 0; i < targets.length; i++) {
      refreshPartial(targets[i]);
    }

    // Dispatch a custom DOM event for latency measurement in tests.
    var detail = { event: eventName, ts: Date.now() };
    try {
      var parsed = JSON.parse(data);
      detail.serverTs = parsed.ts;
      detail.roundTripMs = detail.ts - (parsed.ts * 1000);
    } catch (e) {
      // Ignore parse errors.
    }
    document.dispatchEvent(new CustomEvent("casectl:sse-update", { detail: detail }));
  }

  // -- SSE Connection ------------------------------------------------------

  function connect() {
    if (eventSource) {
      try { eventSource.close(); } catch (e) { /* ignore */ }
    }

    eventSource = new EventSource("/api/sse");

    eventSource.onopen = function () {
      connected = true;
      reconnectDelay = RECONNECT_DELAY;
      document.dispatchEvent(new CustomEvent("casectl:sse-connected"));
    };

    eventSource.onerror = function () {
      connected = false;
      eventSource.close();
      document.dispatchEvent(new CustomEvent("casectl:sse-disconnected"));
      scheduleReconnect();
    };

    // Listen for each refresh event type.
    var eventNames = Object.keys(REFRESH_MAP);
    for (var i = 0; i < eventNames.length; i++) {
      (function (name) {
        eventSource.addEventListener(name, function (e) {
          handleEvent(name, e.data);
        });
      })(eventNames[i]);
    }
  }

  function scheduleReconnect() {
    if (reconnectTimer) return;
    reconnectTimer = setTimeout(function () {
      reconnectTimer = null;
      connect();
    }, reconnectDelay);
    // Exponential backoff.
    reconnectDelay = Math.min(reconnectDelay * 1.5, MAX_RECONNECT_DELAY);
  }

  // -- Lifecycle -----------------------------------------------------------

  /** Start the SSE connection when the DOM is ready. */
  function init() {
    if (typeof EventSource === "undefined") {
      // Browser doesn't support SSE; rely on HTMX polling fallback.
      return;
    }
    connect();
  }

  // Expose for testing.
  window.casectlSSE = {
    connect: connect,
    isConnected: function () { return connected; },
    disconnect: function () {
      if (eventSource) eventSource.close();
      if (reconnectTimer) {
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
      }
      connected = false;
    },
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
