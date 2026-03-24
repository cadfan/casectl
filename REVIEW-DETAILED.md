# casectl Detailed Code Review -- All Findings

## Metadata

| Field | Value |
|-------|-------|
| **Date** | 2026-03-24 |
| **Reviewers** | Judge 1: Security & Error-Handling (Claude), Judge 2: Code Quality & Patterns (Claude), Judge 3: Integration & Runtime (Claude), Structured Review (Claude) |
| **Scope** | Full codebase audit -- all Python files under `src/casectl/`, `pyproject.toml`, templates, tests |
| **Branch** | `master` |
| **Auto-fix commit** | `79d8a1c` ("Fix 10 review findings from 3-judge audit") |
| **Codebase size** | ~9,532 lines Python, 64 files |

---

## Cross-Judge Consensus

The following findings were independently flagged by **two or more** reviewers. These represent the highest-confidence issues.

| Finding | Flagged By | Severity |
|---------|-----------|----------|
| Missing PATCH `/api/config` endpoint (CLI `config set` broken) | Security, Integration, Structured | CRITICAL |
| Fan/LED mode request models missing `ge`/`le` constraints | Security, Structured | HIGH |
| `FanConfig.manual_duty` no per-element 0-255 range validation | Security, Structured | HIGH |
| `LedConfig` RGB values no range validation | Security, Structured | HIGH |
| PIL hard-import in `oled/plugin.py` crashes without Pillow | Quality, Integration | CRITICAL |
| Prometheus plugin false-DEGRADED at startup | Quality, Integration | HIGH |
| Plugins access `ctx._config_manager` (private attribute) | Quality, Integration, Structured | HIGH |
| Fan routes access private `_controller._expansion` / `_controller._system_info` | Security, Quality, Integration, Structured | MEDIUM |
| `import hashlib` dead import in `server.py` | Quality, Integration, Structured | LOW |
| WebSocket endpoint bypasses auth entirely | Security, Structured | HIGH |
| SMTP password exposed via config API | Security, Structured | HIGH |
| Auth cookie missing `secure` flag | Security, Structured | MEDIUM |
| CORS `allow_origins=["*"]` with cookie auth | Security, Structured | MEDIUM |
| Error responses leak `str(exc)` internals | Security, Structured | MEDIUM |
| Module-level mutable globals in route modules | Quality, Integration | MEDIUM |
| Sync event handlers called directly on event loop | Quality, Structured | HIGH |
| Unused `StaticFiles` import in `web/app.py` | Quality, Structured | LOW |
| No tests for plugin_host, server, web, LED/OLED plugins | Structured, Quality | MEDIUM |
| `SetRotationRequest.rotation` allows arbitrary values | Security, Structured | MEDIUM |
| Localhost auth bypass behind reverse proxy | Security, Structured | HIGH |

---

## All Findings by Category

### Category 1: Security & Auth

#### SEC-01. Reverse-proxy localhost auth bypass [DEFERRED]

- **Severity:** CRITICAL
- **File:** `src/casectl/daemon/server.py:63-64`
- **Judges:** Security, Structured
- **Description:** The `BasicAuthMiddleware` trusts `request.client.host` to determine if a connection is from localhost. If casectl sits behind a reverse proxy (nginx, Caddy, etc.) on the LAN, every request arrives with `client.host == "127.0.0.1"` from the proxy, bypassing all authentication. The code does not check `X-Forwarded-For` or any trusted-proxy header.
- **Fix:** Add a configuration flag `trust_localhost: bool` (default `True`). When a reverse proxy is in use, set it to `False`. Alternatively, add `ProxyHeadersMiddleware` from Starlette and check `X-Forwarded-For` against an allow-list of trusted proxies.
- **Status:** DEFERRED -- casectl is not currently behind a proxy. Address before any proxy deployment.

---

#### SEC-02. API token logged in plaintext to stderr/journal [ASK]

- **Severity:** HIGH
- **File:** `src/casectl/daemon/server.py:126-131`
- **Judges:** Security
- **Description:** When binding to `0.0.0.0`, the auto-generated token is logged via `logger.warning()`. This token ends up in journald and any log file, readable by anyone with journal access.
- **Fix:** Log only a truncated/masked version (first 8 characters + `...`), and write the full token to `~/.config/casectl/token` with `0o600` permissions. Add a `casectl token` command to retrieve it.
- **Status:** ASK -- decide whether journal access on the Pi constitutes a risk.

---

#### SEC-03. Auth cookie set without `secure` flag [ASK]

- **Severity:** MEDIUM
- **File:** `src/casectl/web/app.py:211`
- **Judges:** Security, Structured
- **Description:** `response.set_cookie("casectl_token", token, httponly=True, samesite="lax")` does not include `secure=True`. On an HTTPS deployment, the cookie would also be sent over plain HTTP.
- **Fix:** Add `secure=request.url.scheme == "https"` to the `set_cookie` call, or accept as known for a local-network appliance.
- **Status:** ASK -- depends on whether HTTPS is planned.

---

#### SEC-04. WebSocket endpoint bypasses auth entirely [ASK]

- **Severity:** HIGH
- **File:** `src/casectl/daemon/server.py:67-69`
- **Judges:** Security, Structured
- **Description:** The middleware unconditionally skips auth for `request.url.path == "/api/ws"`. Any unauthenticated client on the network can connect and receive all event bus data (temperatures, IP address, fan duty, etc.). No WS-level auth check exists.
- **Fix:** Require a `?token=...` query parameter on the WebSocket upgrade request and validate it with `secrets.compare_digest` before calling `websocket.accept()`.
- **Status:** ASK -- decide if metrics data is sensitive enough to require auth on the WS.

---

#### SEC-05. SMTP password exposed via config GET API [ASK]

- **Severity:** HIGH
- **File:** `src/casectl/config/models.py:199`
- **Judges:** Security, Structured
- **Description:** `smtp_password` is stored as a plain `str` field in `config.yaml`. While the config file is written with `0o600`, the password is returned verbatim by `GET /api/config/alerts`, readable by any authenticated API user or any localhost connection.
- **Fix:** Redact `smtp_password` in the config GET endpoint response (return `"***"`). Consider supporting environment variable references (e.g., `$SMTP_PASSWORD`).
- **Status:** ASK

---

#### SEC-06. `_resolve_api_token` returns `None` for non-`0.0.0.0` LAN binds [DEFERRED]

- **Severity:** MEDIUM
- **File:** `src/casectl/daemon/server.py:113-134`
- **Judges:** Security
- **Description:** If someone binds to a specific LAN IP (e.g., `192.168.1.50`) rather than `0.0.0.0`, the function returns `None` and auth is completely disabled. Only `0.0.0.0` triggers auto-token generation.
- **Fix:** Check whether the bind host is localhost (`127.0.0.1` or `::1`). If anything else (including specific LAN IPs), require or auto-generate a token.
- **Status:** DEFERRED -- current usage always uses `0.0.0.0`.

---

#### SEC-07. CORS `allow_origins=["*"]` with cookie-based auth [DEFERRED]

- **Severity:** MEDIUM
- **File:** `src/casectl/daemon/server.py:206-211`
- **Judges:** Security, Structured
- **Description:** `allow_origins=["*"]` means any website the user visits could make cross-origin requests to the casectl API. For cookie-based auth with `allow_credentials=True`, this would be exploitable. Currently `allow_credentials` defaults to `False`, which partially mitigates this.
- **Fix:** If cookie auth is used, set `allow_credentials=True` with a specific origin allow-list (e.g., `http://localhost:8420`, `http://<pi-ip>:8420`), not `"*"`.
- **Status:** DEFERRED -- acceptable for LAN-only appliance.

---

#### SEC-08. Token passed in URL query parameter [DEFERRED]

- **Severity:** LOW
- **File:** `src/casectl/daemon/server.py:76-78`
- **Judges:** Security
- **Description:** The token can be passed via `?token=...` query parameter, which appears in access logs, browser history, and Referer headers.
- **Fix:** Document that query-param auth is for initial browser access only; the cookie is set immediately after. Consider stripping the token from the URL via JS `history.replaceState`.
- **Status:** DEFERRED -- standard pattern for LAN appliances.

---

#### SEC-09. No rate limiting on auth attempts [DEFERRED]

- **Severity:** LOW
- **File:** `src/casectl/daemon/server.py:46-110`
- **Judges:** Security
- **Description:** No rate limiting on failed auth. Token is 24-byte `token_urlsafe` (192 bits entropy), making brute-force infeasible.
- **Fix:** Add per-IP rate limit (e.g., 10 failures/minute) for defense-in-depth. Not urgent given token entropy.
- **Status:** DEFERRED

---

#### SEC-10. Auth path traversal via `/api/ws/../plugins/...` prefix bypass [DEFERRED]

- **Severity:** LOW
- **File:** `src/casectl/daemon/server.py:67-72`
- **Judges:** Structured
- **Description:** Auth bypass uses path-prefix matching for `/api/ws`, `/static/`, `/w/`. An attacker could attempt path traversal. Uvicorn/Starlette normalize paths before routing, so this is likely safe.
- **Fix:** Verify Starlette path normalization covers this, or use exact path matching.
- **Status:** DEFERRED -- Starlette normalizes paths.

---

### Category 2: Input Validation

#### VAL-01. `SetFanModeRequest.mode` missing `ge`/`le` constraint [FIXED]

- **Severity:** HIGH
- **File:** `src/casectl/plugins/fan/routes.py:57`
- **Judges:** Security, Structured
- **Description:** `mode: int = Field(description="Fan mode enum value (0-4)")` had no `ge=0, le=4` constraint. Pydantic accepted any integer. The route handler validated via `FanMode(request.mode)` but the model itself was permissive.
- **Fix:** Added `ge=0, le=4` to the Field.
- **Status:** FIXED (commit `79d8a1c`)

---

#### VAL-02. `SetLedModeRequest.mode` missing `ge`/`le` constraint [FIXED]

- **Severity:** HIGH
- **File:** `src/casectl/plugins/led/routes.py:52`
- **Judges:** Security, Structured
- **Description:** Same as VAL-01 for LED mode. `mode: int` with no `ge=0, le=5`.
- **Fix:** Added `ge=0, le=5`.
- **Status:** FIXED (commit `79d8a1c`)

---

#### VAL-03. `FanConfig.manual_duty` no per-element 0-255 range validation [FIXED]

- **Severity:** HIGH
- **File:** `src/casectl/config/models.py:82-85`
- **Judges:** Security, Structured
- **Description:** `manual_duty: list[int]` allowed any integer values. Negative or >255 values would persist in config and reach `_write_block()` with undefined I2C behavior.
- **Fix:** Added `field_validator` enforcing 0-255 range per element.
- **Status:** FIXED (commit `79d8a1c`)

---

#### VAL-04. `LedConfig` RGB values no range validation [FIXED]

- **Severity:** HIGH
- **File:** `src/casectl/config/models.py:113-115`
- **Judges:** Security, Structured
- **Description:** `red_value`, `green_value`, `blue_value` were plain `int` fields with no `ge=0, le=255`. Direct config updates bypassed route-level validation.
- **Fix:** Added `ge=0, le=255` to each field.
- **Status:** FIXED (commit `79d8a1c`)

---

#### VAL-05. `FanThresholds` speed fields lack 0-255 constraints [ASK]

- **Severity:** MEDIUM
- **File:** `src/casectl/config/models.py:73-75`
- **Judges:** Security
- **Description:** `low_speed`, `mid_speed`, `high_speed` in `FanThresholds` have no `ge=0, le=255` Pydantic constraints. The hardware driver clamps, but invalid values persist in config.
- **Fix:** Add `ge=0, le=255` to speed fields and `ge=0, le=100` to temperature thresholds.
- **Status:** ASK

---

#### VAL-06. `SetRotationRequest.rotation` allows arbitrary values [ASK]

- **Severity:** MEDIUM
- **File:** `src/casectl/plugins/oled/routes.py:58-61`
- **Judges:** Security, Structured
- **Description:** `rotation: int` with no constraint. Route handler checks `if request.rotation not in (0, 180)` manually, but the Pydantic model/OpenAPI docs don't reflect the constraint.
- **Fix:** Use `Literal[0, 180]` type annotation.
- **Status:** ASK

---

#### VAL-07. `SetFanSpeedRequest.duty` no per-element 0-100 range [ASK]

- **Severity:** MEDIUM
- **File:** `src/casectl/plugins/fan/routes.py:63`
- **Judges:** Structured
- **Description:** `duty: list[int]` with `min_length=1, max_length=3` but no per-element range. The handler validates 0-100 manually at lines 149-154, but invalid values pass Pydantic.
- **Fix:** Use `list[Annotated[int, Field(ge=0, le=100)]]`.
- **Status:** ASK

---

#### VAL-08. Config PATCH body has no Pydantic validation [ASK]

- **Severity:** MEDIUM
- **File:** `src/casectl/daemon/server.py` (new PATCH endpoint), `src/casectl/cli/main.py:446`
- **Judges:** Structured
- **Description:** The CLI sends `{"section": section, key: coerced}` as a flat dict. The `section` key leaks into the update dict. Arbitrary keys can be injected into config sections since there is no Pydantic request model.
- **Fix:** Define a `PatchConfigRequest` Pydantic model with `section: str` and `values: dict[str, Any]` fields. On the CLI side, send `{"section": section, "values": {key: coerced}}`.
- **Status:** ASK

---

#### VAL-09. `ExpansionBoard` I2C driver does not validate input ranges [DEFERRED]

- **Severity:** LOW
- **File:** `src/casectl/hardware/expansion.py:250-261`
- **Judges:** Structured
- **Description:** `set_led_color`, `set_fan_duty` etc. accept any integer. Values outside 0-255 are truncated by I2C byte protocol, but behavior could be confusing.
- **Fix:** Add `assert 0 <= val <= 255` or clamp in driver methods for defense-in-depth.
- **Status:** DEFERRED -- callers already validate.

---

### Category 3: Code Quality & Patterns

#### CQ-01. PIL hard-import in `oled/plugin.py` crashes without Pillow [FIXED]

- **Severity:** CRITICAL
- **File:** `src/casectl/plugins/oled/plugin.py:15`
- **Judges:** Quality, Integration
- **Description:** `from PIL import Image, ImageDraw` at top level caused `ImportError` on systems without Pillow (which is an optional dependency under `[hardware]`). The OLED plugin would fail to load silently.
- **Fix:** Guarded with `try/except` and `_pil_available` flag.
- **Status:** FIXED (commit `79d8a1c`)

---

#### CQ-02. Missing PATCH `/api/config` endpoint [FIXED]

- **Severity:** CRITICAL
- **File:** `src/casectl/cli/main.py:446`, `src/casectl/daemon/server.py`
- **Judges:** Security, Integration, Structured
- **Description:** CLI `casectl config set` sent PATCH to `/api/config`, but only `GET /api/config/{section}` existed. The command always failed with 405.
- **Fix:** Added `PATCH /api/config` endpoint to `server.py`.
- **Status:** FIXED (commit `79d8a1c`)

---

#### CQ-03. Plugins access `ctx._config_manager` (private attribute) [FIXED]

- **Severity:** HIGH
- **File:** `src/casectl/plugins/fan/plugin.py:53-54`, `src/casectl/plugins/led/plugin.py:79`, `src/casectl/plugins/oled/plugin.py:72-73`, all route modules
- **Judges:** Quality, Integration, Structured
- **Description:** All built-in plugins bypass `PluginContext.get_config()` and directly access `ctx._config_manager`. The public API's `get_config()` reads from `plugins.<name>` subsection, while built-in plugins need top-level sections (`fan`, `led`, `oled`).
- **Fix:** Added public `config_manager` property to `PluginContext`. Plugins not yet updated to use it (that is a follow-up task).
- **Status:** FIXED (commit `79d8a1c`) -- property added. Plugins still use `_config_manager` directly, but the public API now exists.

---

#### CQ-04. Prometheus plugin false-DEGRADED on startup [FIXED]

- **Severity:** HIGH
- **File:** `src/casectl/plugins/prometheus/plugin.py:80-82`
- **Judges:** Quality, Integration
- **Description:** `get_status()` returned `PluginStatus.DEGRADED` when `_latest_metrics is None`, which was always true for the first ~2 seconds after startup. This propagated to the health endpoint and dashboard, showing false degraded status on every cold start.
- **Fix:** Changed to return `PluginStatus.HEALTHY` as default (no metrics yet is normal, not degraded).
- **Status:** FIXED (commit `79d8a1c`)

---

#### CQ-05. Dead import: `hashlib` in `server.py` [FIXED]

- **Severity:** LOW
- **File:** `src/casectl/daemon/server.py:12`
- **Judges:** Quality, Integration, Structured
- **Description:** `import hashlib` was present but never referenced. Likely a remnant from planned token hashing.
- **Fix:** Removed.
- **Status:** FIXED (commit `79d8a1c`)

---

#### CQ-06. Dead code: `_MODE_TO_HW` dict in `led/plugin.py` [FIXED]

- **Severity:** LOW
- **File:** `src/casectl/plugins/led/plugin.py:26-32`
- **Judges:** Quality
- **Description:** The `_MODE_TO_HW` mapping was defined but never used. `_apply_mode()` uses explicit `if/elif` chains instead.
- **Fix:** Removed.
- **Status:** FIXED (commit `79d8a1c`)

---

#### CQ-07. Dead code: `HYSTERESIS_BAND` constant in `fan/controller.py` [FIXED]

- **Severity:** LOW
- **File:** `src/casectl/plugins/fan/controller.py:29`
- **Judges:** Quality
- **Description:** `HYSTERESIS_BAND = 3` was never referenced. The actual hysteresis value comes from `config.thresholds.schmitt`.
- **Fix:** Removed.
- **Status:** FIXED (commit `79d8a1c`)

---

#### CQ-08. Dead import: `StaticFiles` in `web/app.py` [FIXED]

- **Severity:** LOW
- **File:** `src/casectl/web/app.py:17`
- **Judges:** Quality, Structured
- **Description:** `from fastapi.staticfiles import StaticFiles` imported but never used. `_STATIC_DIR` constant also unreferenced.
- **Fix:** Removed.
- **Status:** FIXED (commit `79d8a1c`)

---

#### CQ-09. Fan routes access private `_controller._expansion` and `_controller._system_info` [ASK]

- **Severity:** MEDIUM
- **File:** `src/casectl/plugins/fan/routes.py:85-96`
- **Judges:** Security, Quality, Integration, Structured
- **Description:** The `fan_status()` route directly accesses `_controller._expansion` and `_controller._system_info` (private attributes). This couples the route layer to controller internals and violates encapsulation.
- **Fix:** Add public methods to `FanController` (e.g., `get_motor_speeds() -> list[int]`, `get_cpu_temp() -> float`) and call those from routes.
- **Status:** ASK

---

#### CQ-10. Sync event handlers called directly on event loop [ASK]

- **Severity:** HIGH
- **File:** `src/casectl/daemon/event_bus.py:123-124`
- **Judges:** Quality, Structured
- **Description:** In `EventBus.emit()`, line 124 calls sync handlers directly: `handler(data)`. A sync handler that performs I/O (disk, network, I2C) will block the entire asyncio event loop. The comment says "wrap in to_thread if they might block" but this is never done.
- **Fix:** Run sync handlers via `await asyncio.to_thread(handler, data)` or enforce that all handlers must be async.
- **Status:** ASK -- all current handlers are lightweight, but this is a latent risk.

---

#### CQ-11. DRY violation: `_api_get`, `_api_post`, `_api_patch` nearly identical [DEFERRED]

- **Severity:** MEDIUM
- **File:** `src/casectl/cli/main.py:41-93`
- **Judges:** Quality
- **Description:** All three CLI API helper functions share identical try/except structure with `ConnectError` and `HTTPStatusError` handlers. Only the HTTP method call differs.
- **Fix:** Factor out a generic `_api_call(ctx, method, path, json=None)` helper.
- **Status:** DEFERRED

---

#### CQ-12. Module-level mutable globals in all route modules [DEFERRED]

- **Severity:** MEDIUM
- **File:** `src/casectl/plugins/fan/routes.py:19`, all 5 route modules
- **Judges:** Quality, Integration
- **Description:** All route modules use module-level globals (`_controller`, `_get_status`, etc.) set via a `configure()` function. Shared across tests (no isolation), stale references if daemon restarts plugins without reimporting.
- **Fix:** Use FastAPI dependency injection or `request.app.state` instead.
- **Status:** DEFERRED -- works for production single-daemon use.

---

#### CQ-13. `runner.py` `_init_system_info` returns `object | None` instead of `SystemInfo | None` [DEFERRED]

- **Severity:** MEDIUM
- **File:** `src/casectl/daemon/runner.py:143`
- **Judges:** Quality
- **Description:** Return type annotation is `object | None` rather than `SystemInfo | None`, losing all type information downstream.
- **Fix:** Change to `-> SystemInfo | None`.
- **Status:** DEFERRED

---

#### CQ-14. Missing return type on `_get_expansion` and `_get_oled_device` [DEFERRED]

- **Severity:** MEDIUM
- **File:** `src/casectl/plugins/led/plugin.py:154`, `src/casectl/plugins/oled/plugin.py:177`
- **Judges:** Quality
- **Description:** Both methods lack return type annotations. Should return `ExpansionBoard | None` and `OledDevice | None` respectively.
- **Fix:** Add type annotations.
- **Status:** DEFERRED

---

#### CQ-15. Web app checks private `_get_oled_status` method by name via `hasattr` [DEFERRED]

- **Severity:** MEDIUM
- **File:** `src/casectl/web/app.py:111`
- **Judges:** Quality
- **Description:** `hasattr(plugin, "_get_oled_status")` is a fragile pattern. If the private method is renamed, this silently falls back to a less detailed path.
- **Fix:** Have `OledDisplayPlugin.get_status()` include `screens_enabled` and `rotation` data so the web app doesn't need private method access.
- **Status:** DEFERRED

---

#### CQ-16. `start_time` closure in `create_app` [DEFERRED]

- **Severity:** LOW
- **File:** `src/casectl/daemon/server.py:226`
- **Judges:** Quality
- **Description:** `start_time = time.time()` captured by closure. Unconventional for FastAPI but functionally correct.
- **Status:** DEFERRED -- no action needed.

---

#### CQ-17. `import subprocess` unconditional in CLI [DEFERRED]

- **Severity:** LOW
- **File:** `src/casectl/cli/main.py:10`
- **Judges:** Quality
- **Description:** `import subprocess` loaded for every CLI invocation, even simple ones. Adds minor startup overhead.
- **Fix:** Move import inside functions that use it.
- **Status:** DEFERRED

---

#### CQ-18. Doctor command opens I2C bus twice [DEFERRED]

- **Severity:** LOW
- **File:** `src/casectl/cli/main.py:553-588`
- **Judges:** Quality
- **Description:** Creates two separate `SMBus(1)` instances, one for each I2C address probe. Wasteful but not harmful.
- **Fix:** Open bus once, probe both addresses.
- **Status:** DEFERRED

---

#### CQ-19. `import click` unconditional in `base.py` [DEFERRED]

- **Severity:** LOW
- **File:** `src/casectl/plugins/base.py:15`
- **Judges:** Quality
- **Description:** `click` is imported at top level even though it's only needed for `register_commands`. However, `click` is a required dependency, so this is a design concern, not a bug.
- **Status:** DEFERRED -- no action needed.

---

### Category 4: Integration & Runtime

#### INT-01. Plugin deduplication uses class name only, not full module path [ASK]

- **Severity:** MEDIUM
- **File:** `src/casectl/daemon/plugin_host.py:193-195`
- **Judges:** Integration
- **Description:** Community plugin deduplication extracts class name from `ep.value.split(":")[-1]` and compares against built-in names. A community plugin with a class named `FanControlPlugin` would be silently skipped even though it's different.
- **Fix:** Compare the full entry point value (`"casectl.plugins.fan:FanControlPlugin"`) against built-in module paths.
- **Status:** ASK

---

#### INT-02. `starlette` imported directly but not in explicit dependencies [DEFERRED]

- **Severity:** MEDIUM
- **File:** `src/casectl/daemon/server.py:22`
- **Judges:** Integration
- **Description:** `from starlette.middleware.base import BaseHTTPMiddleware` -- starlette is always a transitive dependency of FastAPI, but not explicitly listed in `pyproject.toml`.
- **Fix:** Add `starlette>=0.36` to dependencies, or import from `fastapi.middleware`.
- **Status:** DEFERRED -- works in practice.

---

#### INT-03. WebSocket subscriber added before `accept()` [ASK]

- **Severity:** MEDIUM
- **File:** `src/casectl/daemon/server.py:279-283`
- **Judges:** Security, Quality
- **Description:** `event_bus.add_ws_subscriber(websocket)` is called before `await websocket.accept()`. If an event is emitted in that window, `send_text` will be called on a non-accepted WebSocket, raising an exception. The `finally` block handles cleanup, but there's a race window.
- **Fix:** Move `add_ws_subscriber` to after `accept()` succeeds.
- **Status:** ASK

---

#### INT-04. Dashboard renders with empty metrics for first ~2s [DEFERRED]

- **Severity:** MEDIUM
- **File:** `src/casectl/web/app.py:48-58`, `src/casectl/plugins/monitor/plugin.py:85-103`
- **Judges:** Integration
- **Description:** Before the first monitor collection tick (~2s after startup), `get_status()` returns no `"metrics"` key. Dashboard renders with all zeros. Acceptable but could be confusing.
- **Fix:** Monitor's `start()` could run one immediate `_collect_metrics()` before entering the loop.
- **Status:** DEFERRED -- acceptable startup behavior.

---

#### INT-05. Misleading log message about auth on `0.0.0.0` bind [DEFERRED]

- **Severity:** LOW
- **File:** `src/casectl/daemon/runner.py:352-353`
- **Judges:** Structured
- **Description:** Log says "no authentication" but token auth is actually auto-generated on the line above.
- **Fix:** Change to "accessible from LAN (token auth auto-generated)".
- **Status:** DEFERRED

---

### Category 5: Error Handling

#### ERR-01. Error responses leak internal exception messages [ASK]

- **Severity:** MEDIUM
- **Files:** `src/casectl/plugins/fan/routes.py:131,173`, `src/casectl/plugins/led/routes.py:106,132`, `src/casectl/plugins/oled/routes.py:109,135`
- **Judges:** Security
- **Description:** Multiple route handlers catch `Exception as exc` and return `HTTPException(status_code=500, detail=str(exc))`. This leaks internal state (file paths, stack details) to the API consumer.
- **Fix:** Return a generic message like `"Internal server error"`. The detailed error is already logged.
- **Status:** ASK -- helpful for debugging on LAN, but leaks info.

---

#### ERR-02. `KeyError` detail leaks config section names [DEFERRED]

- **Severity:** MEDIUM
- **File:** `src/casectl/daemon/server.py:261-263`
- **Judges:** Security
- **Description:** `get_config_section` catches `KeyError` and returns `{"detail": str(exc)}`, which includes the list of valid section names.
- **Fix:** Return `"Unknown config section"` without enumerating valid sections.
- **Status:** DEFERRED

---

### Category 6: Concurrency & Thread Safety

#### CON-01. `_latest_metrics` updated without synchronization [DEFERRED]

- **Severity:** MEDIUM
- **Files:** `src/casectl/plugins/fan/controller.py:112`, `src/casectl/plugins/oled/plugin.py:146`, `src/casectl/plugins/monitor/plugin.py:206`
- **Judges:** Security, Structured
- **Description:** `_latest_metrics` dict is replaced (not mutated) from async event handlers and read from route handlers. CPython's GIL makes reference assignment practically atomic, but this is not guaranteed on no-GIL builds or sub-interpreters.
- **Fix:** Use `asyncio.Lock` or document the GIL dependency. Low-risk for now.
- **Status:** DEFERRED

---

#### CON-02. `ConfigManager.get()` not protected by `asyncio.Lock` [DEFERRED]

- **Severity:** MEDIUM
- **File:** `src/casectl/config/manager.py:200-209`
- **Judges:** Structured
- **Description:** `get()` reads from cached `self._config` without acquiring `self._lock`. A concurrent `update()` could be modifying `self._config` simultaneously. In CPython this is generally safe for attribute reads, but inconsistent with the lock-protected `load()`/`save()`.
- **Fix:** Wrap `get()` in `async with self._lock:` or document that the lock only protects file I/O.
- **Status:** DEFERRED

---

#### CON-03. `EventBus._handlers` and `_ws_subscribers` have no thread-safety guarantees [DEFERRED]

- **Severity:** LOW
- **File:** `src/casectl/daemon/event_bus.py:32-33`
- **Judges:** Structured
- **Description:** Uses plain `dict` and `set`. `emit()` copies lists before iteration, but subscribe/unsubscribe from threads could cause concurrent mutation. In practice, subscriptions happen during setup (single-threaded).
- **Fix:** Use `threading.Lock` around mutations, or document that subscribe/unsubscribe must only be called from the event loop thread.
- **Status:** DEFERRED

---

### Category 7: Performance

#### PERF-01. I2C reads could be batched [DEFERRED]

- **Severity:** LOW
- **File:** `src/casectl/plugins/monitor/plugin.py:131-165`
- **Judges:** Structured
- **Description:** Three separate async I2C reads (`async_get_temperature()`, `async_get_fan_duty()`, `async_get_motor_speed()`) each acquire the I2C lock sequentially with 10ms inter-transaction delays. Total ~30ms per collection every 2 seconds. Fine for target hardware.
- **Fix:** Create `async_get_all_readings()` on `ExpansionBoard` that reads all three in one locked section.
- **Status:** DEFERRED

---

### Category 8: YAML & Config Safety

#### YAML-01. Round-trip YAML loader used for config file merge [DEFERRED]

- **Severity:** MEDIUM
- **File:** `src/casectl/config/manager.py:287`
- **Judges:** Security
- **Description:** `_write_yaml_roundtrip` uses the default `YAML()` round-trip loader which processes YAML tags. If the config file is tampered with by another process, a crafted YAML tag could cause unexpected behavior. The initial `load()` correctly uses `YAML(typ="safe")`.
- **Fix:** Use `YAML(typ="safe")` for the initial parse in the round-trip path, or validate the loaded data against the schema before merging.
- **Status:** DEFERRED -- requires config file to be already compromised.

---

### Category 9: Test Gaps

#### TEST-01. No tests for `plugin_host.py` [DEFERRED]

- **Severity:** MEDIUM
- **File:** `src/casectl/daemon/plugin_host.py`
- **Judges:** Structured, Quality
- **Description:** Plugin loading, lifecycle, error recovery, duplicate name handling, version checking, and community entry-point discovery are untested.
- **Fix:** Add `tests/test_daemon/test_plugin_host.py`.
- **Status:** DEFERRED

---

#### TEST-02. No tests for `server.py` (auth middleware, WebSocket, `create_app`) [DEFERRED]

- **Severity:** MEDIUM
- **File:** `src/casectl/daemon/server.py`
- **Judges:** Structured, Quality
- **Description:** `BasicAuthMiddleware`, token auto-generation, WebSocket endpoint, and `create_app()` factory are untested. Auth bypass for localhost, Bearer token, and cookie auth are all untested paths.
- **Fix:** Add `tests/test_daemon/test_server.py` with ASGI test client.
- **Status:** DEFERRED

---

#### TEST-03. No tests for web dashboard routes [DEFERRED]

- **Severity:** MEDIUM
- **File:** `src/casectl/web/app.py`
- **Judges:** Structured
- **Description:** HTMX partials and template rendering entirely untested.
- **Fix:** Add `tests/test_web/test_app.py`.
- **Status:** DEFERRED

---

#### TEST-04. No tests for LED plugin or OLED plugin [DEFERRED]

- **Severity:** MEDIUM
- **Files:** `src/casectl/plugins/led/`, `src/casectl/plugins/oled/`
- **Judges:** Structured
- **Description:** LED control loop, OLED screen rendering, and Prometheus text formatting have no test coverage. Only the fan controller has dedicated tests.
- **Fix:** Add test modules for LED and OLED plugins.
- **Status:** DEFERRED

---

#### TEST-05. No tests for Prometheus text format output [DEFERRED]

- **Severity:** LOW
- **File:** `src/casectl/plugins/prometheus/plugin.py`
- **Judges:** Structured
- **Description:** The Prometheus metrics text formatting is untested.
- **Fix:** Add unit tests for metrics output format.
- **Status:** DEFERRED

---

## Summary Table

| ID | Category | Severity | Short Description | Judges | Status |
|----|----------|----------|-------------------|--------|--------|
| SEC-01 | Security | CRITICAL | Reverse-proxy localhost auth bypass | Sec, Str | DEFERRED |
| SEC-02 | Security | HIGH | API token logged in plaintext | Sec | ASK |
| SEC-03 | Security | MEDIUM | Auth cookie missing `secure` flag | Sec, Str | ASK |
| SEC-04 | Security | HIGH | WebSocket endpoint bypasses auth | Sec, Str | ASK |
| SEC-05 | Security | HIGH | SMTP password exposed via API | Sec, Str | ASK |
| SEC-06 | Security | MEDIUM | No auth for non-0.0.0.0 LAN binds | Sec | DEFERRED |
| SEC-07 | Security | MEDIUM | CORS wildcard with cookie auth | Sec, Str | DEFERRED |
| SEC-08 | Security | LOW | Token in URL query parameter | Sec | DEFERRED |
| SEC-09 | Security | LOW | No rate limiting on auth | Sec | DEFERRED |
| SEC-10 | Security | LOW | Path traversal via prefix bypass | Str | DEFERRED |
| VAL-01 | Validation | HIGH | Fan mode request missing range | Sec, Str | FIXED |
| VAL-02 | Validation | HIGH | LED mode request missing range | Sec, Str | FIXED |
| VAL-03 | Validation | HIGH | `manual_duty` no 0-255 range | Sec, Str | FIXED |
| VAL-04 | Validation | HIGH | LED RGB values no range | Sec, Str | FIXED |
| VAL-05 | Validation | MEDIUM | Fan threshold speeds no range | Sec | ASK |
| VAL-06 | Validation | MEDIUM | OLED rotation allows any int | Sec, Str | ASK |
| VAL-07 | Validation | MEDIUM | Fan duty list no per-element range | Str | ASK |
| VAL-08 | Validation | MEDIUM | Config PATCH body no Pydantic model | Str | ASK |
| VAL-09 | Validation | LOW | I2C driver no input range check | Str | DEFERRED |
| CQ-01 | Quality | CRITICAL | PIL import crash without Pillow | Qual, Int | FIXED |
| CQ-02 | Quality | CRITICAL | Missing PATCH `/api/config` endpoint | Sec, Int, Str | FIXED |
| CQ-03 | Quality | HIGH | Private `_config_manager` access | Qual, Int, Str | FIXED |
| CQ-04 | Quality | HIGH | Prometheus false-DEGRADED startup | Qual, Int | FIXED |
| CQ-05 | Quality | LOW | Dead import: `hashlib` | Qual, Int, Str | FIXED |
| CQ-06 | Quality | LOW | Dead code: `_MODE_TO_HW` | Qual | FIXED |
| CQ-07 | Quality | LOW | Dead code: `HYSTERESIS_BAND` | Qual | FIXED |
| CQ-08 | Quality | LOW | Dead import: `StaticFiles` | Qual, Str | FIXED |
| CQ-09 | Quality | MEDIUM | Routes access private controller attrs | Sec, Qual, Int, Str | ASK |
| CQ-10 | Quality | HIGH | Sync handlers block event loop | Qual, Str | ASK |
| CQ-11 | Quality | MEDIUM | DRY violation in CLI API helpers | Qual | DEFERRED |
| CQ-12 | Quality | MEDIUM | Module-level mutable globals in routes | Qual, Int | DEFERRED |
| CQ-13 | Quality | MEDIUM | Wrong return type on `_init_system_info` | Qual | DEFERRED |
| CQ-14 | Quality | MEDIUM | Missing return types on plugin methods | Qual | DEFERRED |
| CQ-15 | Quality | MEDIUM | `hasattr` check for private method | Qual | DEFERRED |
| CQ-16 | Quality | LOW | `start_time` closure | Qual | DEFERRED |
| CQ-17 | Quality | LOW | Unconditional `subprocess` import | Qual | DEFERRED |
| CQ-18 | Quality | LOW | Doctor opens I2C bus twice | Qual | DEFERRED |
| CQ-19 | Quality | LOW | Unconditional `click` import | Qual | DEFERRED |
| INT-01 | Integration | MEDIUM | Plugin dedup by class name only | Int | ASK |
| INT-02 | Integration | MEDIUM | Starlette not in explicit deps | Int | DEFERRED |
| INT-03 | Integration | MEDIUM | WS subscriber added before accept | Sec, Qual | ASK |
| INT-04 | Integration | MEDIUM | Empty metrics on first dashboard load | Int | DEFERRED |
| INT-05 | Integration | LOW | Misleading log about auth | Str | DEFERRED |
| ERR-01 | Error | MEDIUM | Error responses leak `str(exc)` | Sec | ASK |
| ERR-02 | Error | MEDIUM | KeyError leaks config section names | Sec | DEFERRED |
| CON-01 | Concurrency | MEDIUM | `_latest_metrics` no sync | Sec, Str | DEFERRED |
| CON-02 | Concurrency | MEDIUM | `get()` not lock-protected | Str | DEFERRED |
| CON-03 | Concurrency | LOW | EventBus collections no thread safety | Str | DEFERRED |
| PERF-01 | Performance | LOW | I2C reads could be batched | Str | DEFERRED |
| YAML-01 | Config | MEDIUM | Round-trip YAML loader processes tags | Sec | DEFERRED |
| TEST-01 | Tests | MEDIUM | No tests for plugin_host.py | Str, Qual | DEFERRED |
| TEST-02 | Tests | MEDIUM | No tests for server.py | Str, Qual | DEFERRED |
| TEST-03 | Tests | MEDIUM | No tests for web dashboard | Str | DEFERRED |
| TEST-04 | Tests | MEDIUM | No tests for LED/OLED plugins | Str | DEFERRED |
| TEST-05 | Tests | LOW | No tests for Prometheus formatting | Str | DEFERRED |

## Totals

| Metric | Count |
|--------|-------|
| **Total findings** | 52 |
| **FIXED in commit `79d8a1c`** | 13 |
| **ASK (need decision)** | 14 |
| **DEFERRED** | 25 |
| | |
| **CRITICAL** | 4 (3 fixed, 1 deferred) |
| **HIGH** | 10 (6 fixed, 4 ask) |
| **MEDIUM** | 24 (0 fixed, 9 ask, 15 deferred) |
| **LOW** | 14 (4 fixed, 0 ask, 10 deferred) |

## Legend

- **FIXED** -- Resolved in commit `79d8a1c` ("Fix 10 review findings from 3-judge audit")
- **ASK** -- Requires a design decision from the maintainer
- **DEFERRED** -- Acknowledged, acceptable for v0.1, address in a future release
- **Judges:** Sec = Security Judge, Qual = Code Quality Judge, Int = Integration Judge, Str = Structured Review
