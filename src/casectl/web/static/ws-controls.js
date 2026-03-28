/**
 * casectl WebSocket Controls — bidirectional dashboard command channel.
 *
 * Establishes a WebSocket connection to /api/ws and wires interactive
 * dashboard controls (mode dropdowns, sliders, colour pickers) to send
 * commands that invoke the same core functions as CLI commands.
 *
 * When the WebSocket is connected, commands are sent via WS and the
 * corresponding HTMX PUT request is cancelled (preventing duplicate
 * requests).  When the WebSocket is unavailable, HTMX fires normally
 * as a fallback — ensuring the dashboard works with or without WS.
 *
 * Supported commands (matching REST API + CLI parity):
 *   - set_fan_mode   {mode: int}       — same as `casectl fan mode <mode>`
 *   - set_led_mode   {mode: int}       — same as `casectl led mode <mode>`
 *   - set_fan_speed  {duty: [int, ...]} (0-100%)  — same as `casectl fan speed`
 *   - set_led_color  {red, green, blue} (0-255)   — same as `casectl led color`
 */
(function () {
  'use strict';

  // -- State ---------------------------------------------------------------

  var ws = null;
  var wsConnected = false;
  var reconnectDelay = 1000;
  var reconnectTimer = null;
  var MAX_RECONNECT_DELAY = 30000;

  // Track elements whose HTMX requests should be suppressed because
  // the command was already sent via WebSocket.
  var _wsHandledElements = new WeakSet();

  // -- WebSocket URL -------------------------------------------------------

  function getWsUrl() {
    var proto = (location.protocol === 'https:') ? 'wss:' : 'ws:';
    var url = proto + '//' + location.host + '/api/ws';
    var match = document.cookie.match(/casectl_token=([^;]+)/);
    if (match) {
      url += '?token=' + encodeURIComponent(match[1]);
    }
    return url;
  }

  // -- Connection ----------------------------------------------------------

  function connect() {
    if (ws && (ws.readyState === WebSocket.CONNECTING || ws.readyState === WebSocket.OPEN)) {
      return;
    }

    try {
      ws = new WebSocket(getWsUrl());
    } catch (e) {
      scheduleReconnect();
      return;
    }

    ws.onopen = function () {
      wsConnected = true;
      reconnectDelay = 1000;
      document.dispatchEvent(new CustomEvent('casectl:ws-connected'));
    };

    ws.onmessage = function (evt) {
      try {
        var msg = JSON.parse(evt.data);

        // Dispatch DOM event for test observability and external consumers.
        document.dispatchEvent(new CustomEvent('casectl:ws-message', { detail: msg }));

        // On successful command, trigger an HTMX partial refresh so the
        // dashboard reflects the new state immediately.
        if (msg.type === 'command_result' && msg.data && msg.data.command) {
          var cmd = msg.data.command;
          if (cmd === 'set_fan_mode' || cmd === 'set_fan_speed') {
            _refreshPartial('fan-partial');
          } else if (cmd === 'set_led_mode' || cmd === 'set_led_color') {
            _refreshPartial('led-partial');
          } else if (cmd === 'set_oled_screen' || cmd === 'set_oled_power' ||
                     cmd === 'set_oled_rotation' || cmd === 'set_oled_content') {
            _refreshPartial('oled-partial');
          }
        }
      } catch (e) { /* ignore parse errors */ }
    };

    ws.onclose = function () {
      wsConnected = false;
      ws = null;
      document.dispatchEvent(new CustomEvent('casectl:ws-disconnected'));
      scheduleReconnect();
    };

    ws.onerror = function () {
      // onclose fires after onerror, which handles reconnection.
    };
  }

  function scheduleReconnect() {
    if (reconnectTimer) return;
    reconnectTimer = setTimeout(function () {
      reconnectTimer = null;
      connect();
    }, reconnectDelay);
    reconnectDelay = Math.min(reconnectDelay * 2, MAX_RECONNECT_DELAY);
  }

  // -- Command sending -----------------------------------------------------

  /**
   * Send a command over the WebSocket channel.
   *
   * @param {string} command - The command name (e.g. 'set_fan_mode').
   * @param {object} params  - Command parameters.
   * @returns {boolean} True if the command was sent, false if WS is unavailable.
   */
  function sendCommand(command, params) {
    if (!wsConnected || !ws || ws.readyState !== WebSocket.OPEN) {
      return false;
    }
    try {
      var msg = { command: command };
      for (var k in params) {
        if (params.hasOwnProperty(k)) { msg[k] = params[k]; }
      }
      ws.send(JSON.stringify(msg));
      return true;
    } catch (e) {
      return false;
    }
  }

  // -- Control interception ------------------------------------------------

  /**
   * Build command parameters from a control element.
   *
   * @param {string} wsCmd - The WebSocket command name.
   * @param {HTMLElement} el - The control element.
   * @returns {object|null} Parameters object, or null if invalid.
   */
  function _buildParams(wsCmd, el) {
    switch (wsCmd) {
      case 'set_fan_mode':
        return { mode: parseInt(el.value, 10) };
      case 'set_led_mode':
        return { mode: parseInt(el.value, 10) };
      case 'set_fan_speed':
        return { duty: [parseInt(el.value, 10)] };
      case 'set_led_color': {
        var c = el.value;
        if (!c || c.length < 7) return null;
        return {
          red: parseInt(c.substr(1, 2), 16),
          green: parseInt(c.substr(3, 2), 16),
          blue: parseInt(c.substr(5, 2), 16),
        };
      }
      default:
        return null;
    }
  }

  /**
   * Trigger an HTMX partial refresh by element ID.
   */
  function _refreshPartial(id) {
    var el = document.getElementById(id);
    if (!el) return;
    var url = el.getAttribute('hx-get');
    if (url && typeof htmx !== 'undefined') {
      htmx.ajax('GET', url, { target: '#' + id, swap: 'innerHTML' });
    }
  }

  // Wire up elements with data-ws-command attribute on change events.
  // Uses capture phase to intercept before HTMX processes the event.
  // Per-channel fan sliders are handled by the debounced 'input' listener
  // above, so skip them here to avoid duplicate commands.
  document.addEventListener('change', function (evt) {
    var el = evt.target;
    var wsCmd = el.getAttribute('data-ws-command');
    if (!wsCmd) return;

    // Per-channel fan sliders use the debounced input handler; skip here.
    if (el.classList.contains('fan-channel-duty-slider')) return;

    var params = _buildParams(wsCmd, el);
    if (!params) return;

    var sent = sendCommand(wsCmd, params);
    if (sent) {
      // Mark the element so HTMX's request is suppressed (avoids duplicate).
      _wsHandledElements.add(el);

      document.dispatchEvent(new CustomEvent('casectl:command-sent', {
        detail: { command: wsCmd, via: 'websocket', params: params },
      }));
    } else {
      // WebSocket unavailable — HTMX will fire as fallback.
      document.dispatchEvent(new CustomEvent('casectl:command-sent', {
        detail: { command: wsCmd, via: 'htmx-fallback', params: params },
      }));
    }
  }, true);  // capture phase

  // Cancel HTMX PUT requests for elements already handled by WebSocket.
  document.addEventListener('htmx:beforeRequest', function (evt) {
    var el = evt.detail.elt;
    if (el && _wsHandledElements.has(el)) {
      evt.preventDefault();
      _wsHandledElements.delete(el);
    }
  });

  // -----------------------------------------------------------------------
  // Fan speed slider — debounced WebSocket send + live display update
  // -----------------------------------------------------------------------

  /** Debounce timer for fan speed slider changes. */
  var _fanSpeedDebounceTimer = null;
  var _FAN_SPEED_DEBOUNCE_MS = 80;

  /**
   * Send a fan speed command via WebSocket (debounced) or HTMX fallback.
   * @param {number[]} duty - Array of 1-3 duty values (0-100).
   */
  function _sendFanSpeedCommand(duty) {
    if (_fanSpeedDebounceTimer) clearTimeout(_fanSpeedDebounceTimer);
    _fanSpeedDebounceTimer = setTimeout(function () {
      _fanSpeedDebounceTimer = null;
      var params = { duty: duty };
      var sent = sendCommand('set_fan_speed', params);
      if (!sent) {
        // HTMX fallback: PUT to REST API
        if (typeof htmx !== 'undefined') {
          htmx.ajax('PUT', '/api/plugins/fan-control/speed', {
            values: params,
            swap: 'none',
          });
        }
      }
      document.dispatchEvent(new CustomEvent('casectl:command-sent', {
        detail: { command: 'set_fan_speed', via: sent ? 'websocket' : 'htmx-fallback', params: params },
      }));
    }, _FAN_SPEED_DEBOUNCE_MS);
  }

  /**
   * Read current per-channel slider values and return as array.
   */
  function _readChannelDuties() {
    var duties = [];
    for (var ch = 0; ch < 3; ch++) {
      var slider = document.getElementById('fan-' + ch + '-duty-slider');
      if (slider) {
        duties.push(parseInt(slider.value, 10));
      }
    }
    return duties;
  }

  /**
   * Update visual feedback for a fan channel slider.
   */
  function _updateChannelDisplay(channel, value) {
    var display = document.getElementById('fan-' + channel + '-duty-value');
    if (display) display.textContent = value + '%';
    var progressFill = document.getElementById('fan-' + channel + '-progress');
    if (progressFill) progressFill.style.width = value + '%';
    var dutyDisplay = document.getElementById('fan-' + channel + '-duty-display');
    if (dutyDisplay) dutyDisplay.textContent = value + '%';
  }

  // Per-channel fan duty slider: live preview + debounced send.
  document.addEventListener('input', function (evt) {
    var channel = evt.target.getAttribute('data-fan-channel');
    if (channel !== null && evt.target.classList.contains('fan-channel-duty-slider')) {
      var ch = parseInt(channel, 10);
      var value = parseInt(evt.target.value, 10);
      _updateChannelDisplay(ch, value);
      // Send per-channel duties via WebSocket.
      var duties = _readChannelDuties();
      if (duties.length > 0) {
        _sendFanSpeedCommand(duties);
      }
    }
  });

  // Unified "all fans" slider: live preview + sync per-channel sliders.
  document.addEventListener('input', function (evt) {
    if (evt.target.id === 'fan-duty-slider') {
      var value = parseInt(evt.target.value, 10);
      var display = document.getElementById('fan-duty-value');
      if (display) display.textContent = value + '%';
      // Sync per-channel sliders and displays.
      for (var ch = 0; ch < 3; ch++) {
        var slider = document.getElementById('fan-' + ch + '-duty-slider');
        if (slider) slider.value = value;
        _updateChannelDisplay(ch, value);
      }
      // Send uniform duty via WebSocket.
      _sendFanSpeedCommand([value]);
    }
  });

  // -----------------------------------------------------------------------
  // Colour picker component — RGB sliders, hex input, named presets
  // -----------------------------------------------------------------------

  /** Named colour map matching CLI `casectl led color <name>` */
  var NAMED_COLORS = {
    'red':          { r: 255, g: 0,   b: 0   },
    'green':        { r: 0,   g: 255, b: 0   },
    'blue':         { r: 0,   g: 0,   b: 255 },
    'white':        { r: 255, g: 255, b: 255 },
    'yellow':       { r: 255, g: 255, b: 0   },
    'cyan':         { r: 0,   g: 255, b: 255 },
    'magenta':      { r: 255, g: 0,   b: 255 },
    'orange':       { r: 255, g: 165, b: 0   },
    'pink':         { r: 255, g: 105, b: 180 },
    'purple':       { r: 128, g: 0,   b: 128 },
    'teal':         { r: 0,   g: 128, b: 128 },
    'coral':        { r: 255, g: 127, b: 80  },
    'gold':         { r: 255, g: 215, b: 0   },
    'lime':         { r: 0,   g: 255, b: 0   },
    'navy':         { r: 0,   g: 0,   b: 128 },
    'arctic-steel': { r: 138, g: 170, b: 196 },
  };

  /** Debounce timer for slider/input changes. */
  var _colourDebounceTimer = null;
  var _COLOUR_DEBOUNCE_MS = 80;

  /**
   * Convert 0-255 int to 2-char hex.
   */
  function _toHex2(v) {
    var h = Math.max(0, Math.min(255, Math.round(v))).toString(16);
    return h.length < 2 ? '0' + h : h;
  }

  /**
   * Sync all colour picker sub-controls to the given RGB values,
   * optionally skipping a source element to prevent feedback loops.
   */
  function _syncColourControls(r, g, b, skipEl) {
    var hex = '#' + _toHex2(r) + _toHex2(g) + _toHex2(b);

    var picker = document.getElementById('led-color-picker');
    var hexInput = document.getElementById('led-hex-input');
    var sliderR = document.getElementById('led-slider-r');
    var sliderG = document.getElementById('led-slider-g');
    var sliderB = document.getElementById('led-slider-b');
    var valueR = document.getElementById('led-value-r');
    var valueG = document.getElementById('led-value-g');
    var valueB = document.getElementById('led-value-b');
    var preview = document.getElementById('led-preview-strip');

    if (picker && picker !== skipEl) picker.value = hex;
    if (hexInput && hexInput !== skipEl) {
      hexInput.value = hex.toUpperCase();
      hexInput.classList.remove('hex-input--invalid');
    }
    if (sliderR && sliderR !== skipEl) sliderR.value = r;
    if (sliderG && sliderG !== skipEl) sliderG.value = g;
    if (sliderB && sliderB !== skipEl) sliderB.value = b;
    if (valueR) valueR.textContent = r;
    if (valueG) valueG.textContent = g;
    if (valueB) valueB.textContent = b;
    if (preview) preview.style.background = 'rgb(' + r + ',' + g + ',' + b + ')';

    // Update active state on preset swatches
    var swatches = document.querySelectorAll('.preset-swatch');
    for (var i = 0; i < swatches.length; i++) {
      var name = swatches[i].getAttribute('data-color-name');
      var nc = NAMED_COLORS[name];
      if (nc && nc.r === r && nc.g === g && nc.b === b) {
        swatches[i].classList.add('preset-swatch--active');
      } else {
        swatches[i].classList.remove('preset-swatch--active');
      }
    }

    // Update component data attributes for state tracking
    var component = document.getElementById('colour-picker-component');
    if (component) {
      component.setAttribute('data-red', r);
      component.setAttribute('data-green', g);
      component.setAttribute('data-blue', b);
    }
  }

  /**
   * Send an LED colour command via WebSocket (debounced) or HTMX fallback.
   */
  function _sendColourCommand(r, g, b) {
    if (_colourDebounceTimer) clearTimeout(_colourDebounceTimer);
    _colourDebounceTimer = setTimeout(function () {
      _colourDebounceTimer = null;
      var params = { red: r, green: g, blue: b };
      var sent = sendCommand('set_led_color', params);
      if (!sent) {
        // HTMX fallback: PUT to REST API
        if (typeof htmx !== 'undefined') {
          htmx.ajax('PUT', '/api/plugins/led-control/color', {
            values: params,
            swap: 'none',
          });
        }
      }
      document.dispatchEvent(new CustomEvent('casectl:command-sent', {
        detail: { command: 'set_led_color', via: sent ? 'websocket' : 'htmx-fallback', params: params },
      }));
    }, _COLOUR_DEBOUNCE_MS);
  }

  // -- Native colour picker change --
  document.addEventListener('change', function (evt) {
    if (evt.target.id !== 'led-color-picker') return;
    var c = evt.target.value;
    if (!c || c.length < 7) return;
    var r = parseInt(c.substr(1, 2), 16);
    var g = parseInt(c.substr(3, 2), 16);
    var b = parseInt(c.substr(5, 2), 16);
    _syncColourControls(r, g, b, evt.target);
    _sendColourCommand(r, g, b);
  });

  // Live preview update while dragging native colour picker
  document.addEventListener('input', function (evt) {
    if (evt.target.id !== 'led-color-picker') return;
    var c = evt.target.value;
    if (!c || c.length < 7) return;
    var r = parseInt(c.substr(1, 2), 16);
    var g = parseInt(c.substr(3, 2), 16);
    var b = parseInt(c.substr(5, 2), 16);
    _syncColourControls(r, g, b, evt.target);
    _sendColourCommand(r, g, b);
  });

  // -- RGB slider input (live preview + debounced send) --
  document.addEventListener('input', function (evt) {
    var id = evt.target.id;
    if (id !== 'led-slider-r' && id !== 'led-slider-g' && id !== 'led-slider-b') return;
    var r = parseInt((document.getElementById('led-slider-r') || {}).value || 0);
    var g = parseInt((document.getElementById('led-slider-g') || {}).value || 0);
    var b = parseInt((document.getElementById('led-slider-b') || {}).value || 0);
    _syncColourControls(r, g, b, evt.target);
    _sendColourCommand(r, g, b);
  });

  // -- Hex input change --
  document.addEventListener('change', function (evt) {
    if (evt.target.id !== 'led-hex-input') return;
    var val = evt.target.value.trim();
    if (val.charAt(0) !== '#') val = '#' + val;
    var match = val.match(/^#([0-9A-Fa-f]{6})$/);
    if (!match) {
      evt.target.classList.add('hex-input--invalid');
      return;
    }
    evt.target.classList.remove('hex-input--invalid');
    var r = parseInt(match[1].substr(0, 2), 16);
    var g = parseInt(match[1].substr(2, 2), 16);
    var b = parseInt(match[1].substr(4, 2), 16);
    _syncColourControls(r, g, b, evt.target);
    _sendColourCommand(r, g, b);
  });

  // -- Named colour preset click --
  document.addEventListener('click', function (evt) {
    var swatch = evt.target.closest('.preset-swatch');
    if (!swatch) return;
    var name = swatch.getAttribute('data-color-name');
    var nc = NAMED_COLORS[name];
    if (!nc) return;
    _syncColourControls(nc.r, nc.g, nc.b, null);
    // Send named colour via WebSocket for CLI parity
    var params = { red: nc.r, green: nc.g, blue: nc.b, color_name: name };
    var sent = sendCommand('set_led_color', params);
    if (!sent) {
      if (typeof htmx !== 'undefined') {
        htmx.ajax('PUT', '/api/plugins/led-control/color', {
          values: { red: nc.r, green: nc.g, blue: nc.b },
          swap: 'none',
        });
      }
    }
    document.dispatchEvent(new CustomEvent('casectl:command-sent', {
      detail: { command: 'set_led_color', via: sent ? 'websocket' : 'htmx-fallback', params: params },
    }));
  });

  // -----------------------------------------------------------------------
  // OLED controls — screen toggle, power, rotation, content settings
  // -----------------------------------------------------------------------

  /**
   * Toggle an individual OLED screen on or off.
   * @param {number} index - Screen index (0-3).
   * @param {boolean} enabled - Whether to enable or disable.
   */
  function _sendOledScreenToggle(index, enabled) {
    var params = { index: index, enabled: enabled };
    var sent = sendCommand('set_oled_screen', params);
    if (!sent) {
      if (typeof htmx !== 'undefined') {
        htmx.ajax('PUT', '/api/plugins/oled-display/screen', {
          values: JSON.stringify(params),
          headers: { 'Content-Type': 'application/json' },
          swap: 'none',
        });
      }
    }
    document.dispatchEvent(new CustomEvent('casectl:command-sent', {
      detail: { command: 'set_oled_screen', via: sent ? 'websocket' : 'htmx-fallback', params: params },
    }));
  }

  /**
   * Toggle OLED power (enable/disable all screens).
   * @param {boolean} enabled - True to power on, false to power off.
   */
  function _sendOledPower(enabled) {
    var params = { enabled: enabled };
    var sent = sendCommand('set_oled_power', params);
    if (!sent) {
      if (typeof htmx !== 'undefined') {
        htmx.ajax('PUT', '/api/plugins/oled-display/power', {
          values: JSON.stringify(params),
          headers: { 'Content-Type': 'application/json' },
          swap: 'none',
        });
      }
    }
    document.dispatchEvent(new CustomEvent('casectl:command-sent', {
      detail: { command: 'set_oled_power', via: sent ? 'websocket' : 'htmx-fallback', params: params },
    }));
  }

  /**
   * Set OLED rotation.
   * @param {number} rotation - 0 or 180 degrees.
   */
  function _sendOledRotation(rotation) {
    var params = { rotation: rotation };
    var sent = sendCommand('set_oled_rotation', params);
    if (!sent) {
      if (typeof htmx !== 'undefined') {
        htmx.ajax('PUT', '/api/plugins/oled-display/rotation', {
          values: JSON.stringify(params),
          headers: { 'Content-Type': 'application/json' },
          swap: 'none',
        });
      }
    }
    document.dispatchEvent(new CustomEvent('casectl:command-sent', {
      detail: { command: 'set_oled_rotation', via: sent ? 'websocket' : 'htmx-fallback', params: params },
    }));
  }

  /**
   * Update OLED screen content settings (display_time, time_format, etc.).
   * @param {number} screenIndex - Screen index (0-3).
   * @param {object} settings - Content settings to apply.
   */
  function _sendOledContent(screenIndex, settings) {
    var params = { screen_index: screenIndex };
    for (var k in settings) {
      if (settings.hasOwnProperty(k)) { params[k] = settings[k]; }
    }
    var sent = sendCommand('set_oled_content', params);
    if (!sent) {
      if (typeof htmx !== 'undefined') {
        htmx.ajax('PUT', '/api/plugins/oled-display/content', {
          values: JSON.stringify(params),
          headers: { 'Content-Type': 'application/json' },
          swap: 'none',
        });
      }
    }
    document.dispatchEvent(new CustomEvent('casectl:command-sent', {
      detail: { command: 'set_oled_content', via: sent ? 'websocket' : 'htmx-fallback', params: params },
    }));
  }

  // -- OLED screen toggle button clicks --
  document.addEventListener('click', function (evt) {
    var btn = evt.target.closest('[data-oled-screen-toggle]');
    if (!btn) return;
    var index = parseInt(btn.getAttribute('data-oled-screen-index'), 10);
    var currentlyEnabled = btn.getAttribute('data-oled-screen-enabled') === 'true';
    _sendOledScreenToggle(index, !currentlyEnabled);
    // Prevent HTMX from also firing
    evt.preventDefault();
    evt.stopPropagation();
  });

  // -- OLED power toggle button --
  document.addEventListener('click', function (evt) {
    var btn = evt.target.closest('[data-oled-power-toggle]');
    if (!btn) return;
    var currentlyOn = btn.getAttribute('data-oled-power-on') === 'true';
    _sendOledPower(!currentlyOn);
    evt.preventDefault();
    evt.stopPropagation();
  });

  // -- OLED rotation select change --
  document.addEventListener('change', function (evt) {
    if (evt.target.id !== 'oled-rotation-select') return;
    var rotation = parseInt(evt.target.value, 10);
    if (rotation === 0 || rotation === 180) {
      _sendOledRotation(rotation);
    }
  });

  // -- OLED display time change (per-screen) --
  var _oledContentDebounceTimer = null;
  var _OLED_CONTENT_DEBOUNCE_MS = 300;

  document.addEventListener('change', function (evt) {
    var el = evt.target;
    if (!el.classList.contains('oled-display-time-input')) return;
    var screenIndex = parseInt(el.getAttribute('data-screen-index'), 10);
    var displayTime = parseFloat(el.value);
    if (isNaN(screenIndex) || isNaN(displayTime) || displayTime <= 0) return;
    if (_oledContentDebounceTimer) clearTimeout(_oledContentDebounceTimer);
    _oledContentDebounceTimer = setTimeout(function () {
      _oledContentDebounceTimer = null;
      _sendOledContent(screenIndex, { display_time: displayTime });
    }, _OLED_CONTENT_DEBOUNCE_MS);
  });

  // -- OLED time format change (clock screen) --
  document.addEventListener('change', function (evt) {
    if (evt.target.id !== 'oled-time-format-select') return;
    var timeFormat = parseInt(evt.target.value, 10);
    // Time format applies to clock screen (index 0)
    _sendOledContent(0, { time_format: timeFormat });
  });

  // Expose for programmatic use and testing.
  window.casectlWS = {
    connect: connect,
    sendCommand: sendCommand,
    sendFanSpeed: _sendFanSpeedCommand,
    readChannelDuties: _readChannelDuties,
    sendOledScreenToggle: _sendOledScreenToggle,
    sendOledPower: _sendOledPower,
    sendOledRotation: _sendOledRotation,
    sendOledContent: _sendOledContent,
    isConnected: function () { return wsConnected; },
    disconnect: function () {
      if (ws) { try { ws.close(); } catch (e) { /* ignore */ } }
      if (reconnectTimer) {
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
      }
      wsConnected = false;
      ws = null;
    },
  };

  // Backwards-compatible alias (v0.1 used lowercase).
  window.casectlWs = window.casectlWS;

  // Connect on load.
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', connect);
  } else {
    connect();
  }
})();
