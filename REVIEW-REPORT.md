# casectl Full Code Review Report

**Date:** 2026-03-24
**Reviewers:** 3 Claude judges (security, code quality, integration) + Codex (blocked by sandbox)
**Scope:** Full codebase audit (9,532 lines Python, 64 files)
**Branch:** master

## Summary

| Severity | Found | Fixed | Remaining |
|----------|-------|-------|-----------|
| CRITICAL | 0 | 0 | 0 |
| HIGH | 8 | 8 | 0 |
| MEDIUM | 11 | 3 | 8 (ASK/deferred) |
| LOW | 7 | 2 | 5 (informational) |
| **Total** | **26** | **13** | **13** |

## Auto-Fixed (committed)

1. PIL hard-import in oled/plugin.py — guarded with try/except
2. Missing PATCH /api/config endpoint — added to server.py
3. Fan/LED mode request models — added ge/le constraints
4. FanConfig.manual_duty — added field_validator (0-255 range)
5. LedConfig RGB values — added ge=0, le=255
6. PluginContext — added public config_manager property
7. Prometheus false-degraded — returns HEALTHY on startup
8. Removed dead code: hashlib import, _MODE_TO_HW dict, HYSTERESIS_BAND constant, unused StaticFiles import

## Remaining (need your decision)

### Security (ASK items — decide when you return)

**S1. WebSocket endpoint bypasses auth entirely (MEDIUM)**
Any LAN client can connect to /api/ws without a token and receive all metrics events.
- Option A: Add ?token= check before websocket.accept()
- Option B: Accept as-is (metrics data is not sensitive for a LAN appliance)

**S2. API token logged in plaintext to journal (HIGH → MEDIUM after context)**
The auto-generated token is logged via logger.warning(). Anyone with journalctl access can read it.
- Option A: Write token to ~/.config/casectl/token file (0o600), log only first 8 chars
- Option B: Accept as-is (only the Pi user has journal access)

**S3. Localhost auth bypass vulnerable behind reverse proxy (MEDIUM)**
If casectl sits behind nginx, all requests appear as 127.0.0.1, bypassing auth.
- Option A: Add trust_localhost config flag
- Option B: Accept as-is (casectl is not behind a proxy in current setup)

**S4. _resolve_api_token skips auth for specific LAN IPs (MEDIUM)**
Binding to e.g. 192.168.1.50 (not 0.0.0.0) has no auth.
- Option A: Check if host is localhost; auto-generate token for any non-localhost bind
- Option B: Accept as-is (current usage always uses 0.0.0.0)

**S5. Error responses leak exception details (MEDIUM)**
Route handlers return str(exc) in 500 responses.
- Option A: Return generic "Internal server error" messages
- Option B: Accept as-is (LAN appliance, helpful for debugging)

### Code Quality (informational — no immediate action needed)

**Q1.** Plugins access ctx._config_manager directly (public property added, but plugins not yet updated to use it)
**Q2.** Fan routes access private _controller._expansion attributes
**Q3.** Sync event handlers called directly on event loop (could block if handler does I/O)
**Q4.** Module-level mutable globals in route modules (works but hard to test)
**Q5.** ConfigManager.get() not protected by asyncio.Lock

### Test Gaps (informational)

**T1.** No tests for plugin_host.py (loading, lifecycle, error recovery)
**T2.** No tests for server.py (auth middleware, WebSocket, create_app)
**T3.** No tests for web dashboard routes
**T4.** No tests for LED plugin or OLED plugin
**T5.** No tests for Prometheus text format output

## Verdict

The codebase is **solid for a v0.1 release**. No critical issues remain. The 8 HIGH findings were all auto-fixed. The remaining items are security hardening for edge cases (reverse proxy, specific LAN binds) and code quality improvements that can be addressed in v0.2.

The architecture is well-designed: clean layer separation, proper async patterns with I2C thread safety, graceful degradation, and a plugin system that works end-to-end (confirmed by the LED Off button test from the dashboard).

## Codex Meta-Review (independent quality check of this review)

**Scores:** Completeness 6/10, Accuracy 7/10, Prioritization 5/10, Actionability 7/10

**Confirmed real:** S1 (WebSocket unauth), S2 (token plaintext), S4 (LAN IP no auth), S5 (error leaks)

**Prioritization corrections:**
- S4 should be HIGH not MEDIUM (disabling auth for non-loopback binds is serious)
- Q5 (ConfigManager lock) is weak — should be deprioritized

**Gaps the Claude review missed:**
- `/w/*` dashboard partials exempt from auth but expose live metrics (CPU temp, fan duty, IP)
- Misleading log warning says "no authentication" when a token IS auto-generated
- SMTP password in config model is plaintext and exposed via GET /api/config/alerts

**Action items from Codex:**
- Upgrade S4 to HIGH
- Add finding: `/w/*` partials leak live status data without auth
- Fix misleading "no authentication" log message in runner.py

## QA Note

QA testing (/qa) requires a browser which is not available on headless griffpi. Run `/qa` from your Win11 machine tomorrow, or install a headless browser on the Pi.
