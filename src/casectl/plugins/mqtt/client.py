"""MQTT client connection manager for casectl.

Wraps `aiomqtt <https://github.com/sbtinstruments/aiomqtt>`_ (which itself
wraps paho-mqtt) to provide:

* Configurable broker settings (host, port, credentials, TLS)
* QoS 1 by default for reliable delivery
* Retained message support for state publishing
* Automatic reconnection with exponential back-off
* Last Will and Testament (LWT) for availability tracking
* Birth message on connect
* Async context-manager and explicit connect/disconnect API
* Thread-safe message callback registration
"""

from __future__ import annotations

import asyncio
import enum
import logging
import ssl
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Connection state
# ---------------------------------------------------------------------------


class ConnectionState(str, enum.Enum):
    """MQTT connection lifecycle states."""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    DISCONNECTING = "disconnecting"


# ---------------------------------------------------------------------------
# Broker settings dataclass (decoupled from Pydantic config)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BrokerSettings:
    """Immutable snapshot of MQTT broker connection parameters.

    Created from :class:`casectl.config.models.MqttConfig` or constructed
    directly in tests.
    """

    host: str = "localhost"
    port: int = 1883
    username: str = ""
    password: str = ""
    client_id: str = "casectl"
    topic_prefix: str = "casectl"
    ha_discovery_prefix: str = "homeassistant"
    qos: int = 1
    retain: bool = True
    keepalive: int = 60
    reconnect_min_delay: float = 1.0
    reconnect_max_delay: float = 60.0
    tls_enabled: bool = False
    tls_ca_cert: str = ""
    tls_insecure: bool = False
    birth_topic: str = ""
    will_topic: str = ""
    publish_interval: float = 10.0

    @classmethod
    def from_config(cls, cfg: Any) -> BrokerSettings:
        """Build from a :class:`~casectl.config.models.MqttConfig` instance.

        Parameters
        ----------
        cfg:
            An ``MqttConfig`` (or any object with the same attribute names).
        """
        return cls(
            host=cfg.broker_host,
            port=cfg.broker_port,
            username=cfg.username,
            password=cfg.password,
            client_id=cfg.client_id,
            topic_prefix=cfg.topic_prefix,
            ha_discovery_prefix=cfg.ha_discovery_prefix,
            qos=cfg.qos,
            retain=cfg.retain,
            keepalive=cfg.keepalive,
            reconnect_min_delay=cfg.reconnect_min_delay,
            reconnect_max_delay=cfg.reconnect_max_delay,
            tls_enabled=cfg.tls_enabled,
            tls_ca_cert=cfg.tls_ca_cert,
            tls_insecure=cfg.tls_insecure,
            birth_topic=cfg.birth_topic,
            will_topic=cfg.will_topic,
            publish_interval=cfg.publish_interval,
        )

    # -- derived helpers ----------------------------------------------------

    @property
    def status_topic(self) -> str:
        """Resolved availability / status topic."""
        return self.birth_topic or f"{self.topic_prefix}/status"

    @property
    def will_topic_resolved(self) -> str:
        """Resolved Last Will topic (defaults to status topic)."""
        return self.will_topic or self.status_topic


# ---------------------------------------------------------------------------
# Subscription entry
# ---------------------------------------------------------------------------


@dataclass
class _Subscription:
    """A registered topic subscription with its callback."""

    topic: str
    callback: Callable[[str, bytes], Coroutine[Any, Any, None] | None]
    qos: int


# ---------------------------------------------------------------------------
# MQTT Connection Manager
# ---------------------------------------------------------------------------


class MqttConnectionManager:
    """Manages an MQTT client connection with automatic reconnection.

    This class does **not** import ``aiomqtt`` at module level so that the
    rest of casectl can load without the optional dependency installed.

    Usage::

        mgr = MqttConnectionManager(settings)
        await mgr.connect()      # or use  async with mgr: ...
        await mgr.publish("casectl/fan/duty", "128")
        await mgr.subscribe("casectl/fan/set", handler)
        await mgr.disconnect()

    Parameters
    ----------
    settings:
        A :class:`BrokerSettings` instance describing the broker.
    """

    def __init__(self, settings: BrokerSettings) -> None:
        self._settings = settings
        self._state = ConnectionState.DISCONNECTED
        self._client: Any | None = None  # aiomqtt.Client
        self._subscriptions: list[_Subscription] = []
        self._state_listeners: list[Callable[[ConnectionState], Any]] = []
        self._reconnect_task: asyncio.Task[None] | None = None
        self._listener_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._connected_event = asyncio.Event()
        self._reconnect_count: int = 0
        self._lock = asyncio.Lock()

    # -- properties ---------------------------------------------------------

    @property
    def state(self) -> ConnectionState:
        """Current connection state."""
        return self._state

    @property
    def is_connected(self) -> bool:
        """Whether the client is currently connected to the broker."""
        return self._state == ConnectionState.CONNECTED

    @property
    def settings(self) -> BrokerSettings:
        """The broker settings this manager was created with."""
        return self._settings

    @property
    def reconnect_count(self) -> int:
        """Number of reconnection attempts since last successful connect."""
        return self._reconnect_count

    # -- state management ---------------------------------------------------

    def _set_state(self, new_state: ConnectionState) -> None:
        """Update state and notify listeners."""
        if self._state == new_state:
            return
        old_state = self._state
        self._state = new_state
        logger.debug("MQTT state: %s → %s", old_state.value, new_state.value)
        for listener in self._state_listeners:
            try:
                result = listener(new_state)
                if asyncio.iscoroutine(result):
                    asyncio.ensure_future(result)
            except Exception:
                logger.exception("State listener error")

    def on_state_change(self, listener: Callable[[ConnectionState], Any]) -> None:
        """Register a callback invoked whenever the connection state changes.

        Parameters
        ----------
        listener:
            A callable ``(new_state: ConnectionState) -> None`` (may be async).
        """
        self._state_listeners.append(listener)

    # -- TLS helper ---------------------------------------------------------

    def _build_tls_context(self) -> ssl.SSLContext | None:
        """Build an SSL context from settings, or return ``None`` if TLS is disabled."""
        if not self._settings.tls_enabled:
            return None

        ctx = ssl.create_default_context()
        if self._settings.tls_ca_cert:
            ca_path = Path(self._settings.tls_ca_cert)
            if ca_path.is_file():
                ctx.load_verify_locations(str(ca_path))
            else:
                logger.warning("TLS CA cert not found: %s — using system CAs", ca_path)
        if self._settings.tls_insecure:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    # -- connect / disconnect -----------------------------------------------

    async def connect(self) -> None:
        """Connect to the MQTT broker.

        Publishes a birth message on success and starts the message listener
        and reconnection tasks.

        Raises
        ------
        ImportError
            If ``aiomqtt`` is not installed.
        ConnectionError
            If the initial connection attempt fails.
        """
        if self._state in (ConnectionState.CONNECTED, ConnectionState.CONNECTING):
            logger.debug("Already %s — ignoring connect()", self._state.value)
            return

        self._set_state(ConnectionState.CONNECTING)
        self._stop_event.clear()
        self._connected_event.clear()

        try:
            await self._do_connect()
        except Exception as exc:
            self._set_state(ConnectionState.DISCONNECTED)
            raise ConnectionError(
                f"Failed to connect to MQTT broker at "
                f"{self._settings.host}:{self._settings.port}: {exc}"
            ) from exc

    async def _do_connect(self) -> None:
        """Perform the actual connection (shared by connect and reconnect)."""
        try:
            import aiomqtt
        except ImportError as exc:
            raise ImportError(
                "aiomqtt is required for MQTT support. "
                "Install it with: pip install aiomqtt"
            ) from exc

        s = self._settings
        tls_ctx = self._build_tls_context()

        # Build Will message
        will = aiomqtt.Will(
            topic=s.will_topic_resolved,
            payload=b"offline",
            qos=s.qos,
            retain=s.retain,
        )

        self._client = aiomqtt.Client(
            hostname=s.host,
            port=s.port,
            username=s.username or None,
            password=s.password or None,
            identifier=s.client_id,
            keepalive=s.keepalive,
            will=will,
            tls_context=tls_ctx,
        )

        await self._client.__aenter__()

        self._reconnect_count = 0
        self._set_state(ConnectionState.CONNECTED)
        self._connected_event.set()

        # Re-subscribe to any registered subscriptions
        await self._restore_subscriptions()

        # Publish birth message
        await self._publish_birth()

        # Start message listener task
        if self._listener_task is None or self._listener_task.done():
            self._listener_task = asyncio.create_task(
                self._message_loop(), name="mqtt-message-loop"
            )

        logger.info("Connected to MQTT broker at %s:%d", s.host, s.port)

    async def disconnect(self) -> None:
        """Gracefully disconnect from the broker.

        Publishes an offline status message before disconnecting, and cancels
        background tasks.
        """
        if self._state == ConnectionState.DISCONNECTED:
            return

        self._set_state(ConnectionState.DISCONNECTING)
        self._stop_event.set()

        # Cancel reconnect task if running
        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
            try:
                await self._reconnect_task
            except asyncio.CancelledError:
                pass
            self._reconnect_task = None

        # Cancel listener task
        if self._listener_task and not self._listener_task.done():
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass
            self._listener_task = None

        # Publish offline status and disconnect
        if self._client is not None:
            try:
                await self._client.publish(
                    self._settings.status_topic,
                    payload=b"offline",
                    qos=self._settings.qos,
                    retain=self._settings.retain,
                )
            except Exception:
                logger.debug("Could not publish offline status before disconnect")

            try:
                await self._client.__aexit__(None, None, None)
            except Exception:
                logger.debug("Error during MQTT client disconnect")
            self._client = None

        self._connected_event.clear()
        self._set_state(ConnectionState.DISCONNECTED)
        logger.info("Disconnected from MQTT broker")

    # -- async context manager ----------------------------------------------

    async def __aenter__(self) -> MqttConnectionManager:
        await self.connect()
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.disconnect()

    # -- publish ------------------------------------------------------------

    async def publish(
        self,
        topic: str,
        payload: str | bytes,
        *,
        qos: int | None = None,
        retain: bool | None = None,
    ) -> None:
        """Publish a message to a topic.

        Parameters
        ----------
        topic:
            The MQTT topic string.
        payload:
            Message payload (str will be encoded to UTF-8).
        qos:
            QoS level override (defaults to ``settings.qos``).
        retain:
            Retain flag override (defaults to ``settings.retain``).

        Raises
        ------
        RuntimeError
            If not connected.
        """
        if not self.is_connected or self._client is None:
            raise RuntimeError("MQTT client is not connected")

        if isinstance(payload, str):
            payload = payload.encode("utf-8")

        resolved_qos = qos if qos is not None else self._settings.qos
        resolved_retain = retain if retain is not None else self._settings.retain

        await self._client.publish(
            topic,
            payload=payload,
            qos=resolved_qos,
            retain=resolved_retain,
        )
        logger.debug("Published to %s (qos=%d, retain=%s)", topic, resolved_qos, resolved_retain)

    # -- subscribe / unsubscribe --------------------------------------------

    async def subscribe(
        self,
        topic: str,
        callback: Callable[[str, bytes], Coroutine[Any, Any, None] | None],
        *,
        qos: int | None = None,
    ) -> None:
        """Subscribe to a topic and register a message callback.

        Parameters
        ----------
        topic:
            The MQTT topic (may include wildcards ``+`` and ``#``).
        callback:
            An async or sync callable ``(topic: str, payload: bytes) -> None``.
        qos:
            QoS level override (defaults to ``settings.qos``).
        """
        resolved_qos = qos if qos is not None else self._settings.qos

        sub = _Subscription(topic=topic, callback=callback, qos=resolved_qos)
        self._subscriptions.append(sub)

        if self.is_connected and self._client is not None:
            await self._client.subscribe(topic, qos=resolved_qos)
            logger.debug("Subscribed to %s (qos=%d)", topic, resolved_qos)

    async def unsubscribe(self, topic: str) -> None:
        """Unsubscribe from a topic and remove all registered callbacks for it.

        Parameters
        ----------
        topic:
            The MQTT topic to unsubscribe from.
        """
        self._subscriptions = [s for s in self._subscriptions if s.topic != topic]

        if self.is_connected and self._client is not None:
            await self._client.unsubscribe(topic)
            logger.debug("Unsubscribed from %s", topic)

    # -- internal: subscription restore -------------------------------------

    async def _restore_subscriptions(self) -> None:
        """Re-subscribe to all registered topics (called after reconnect)."""
        if not self._client:
            return
        for sub in self._subscriptions:
            try:
                await self._client.subscribe(sub.topic, qos=sub.qos)
                logger.debug("Restored subscription: %s", sub.topic)
            except Exception:
                logger.warning("Failed to restore subscription: %s", sub.topic)

    # -- internal: birth message --------------------------------------------

    async def _publish_birth(self) -> None:
        """Publish a birth (online) message to the status topic."""
        if not self._client:
            return
        try:
            await self._client.publish(
                self._settings.status_topic,
                payload=b"online",
                qos=self._settings.qos,
                retain=self._settings.retain,
            )
            logger.debug("Published birth message to %s", self._settings.status_topic)
        except Exception:
            logger.warning("Failed to publish birth message")

    # -- internal: message loop ---------------------------------------------

    async def _message_loop(self) -> None:
        """Listen for incoming messages and dispatch to callbacks."""
        if self._client is None:
            return

        try:
            async for message in self._client.messages:
                if self._stop_event.is_set():
                    break
                topic_str = str(message.topic)
                payload = bytes(message.payload) if message.payload else b""
                await self._dispatch_message(topic_str, payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self._stop_event.is_set():
                logger.warning("MQTT message loop error: %s", exc)
                # Trigger reconnection
                await self._schedule_reconnect()

    async def _dispatch_message(self, topic: str, payload: bytes) -> None:
        """Route an incoming message to matching subscription callbacks."""
        import asyncio as _asyncio
        import fnmatch
        import inspect

        for sub in self._subscriptions:
            # Convert MQTT wildcards to fnmatch patterns
            pattern = sub.topic.replace("+", "*").replace("#", "**")
            if fnmatch.fnmatch(topic, pattern) or topic == sub.topic:
                try:
                    result = sub.callback(topic, payload)
                    if inspect.iscoroutine(result):
                        await result
                except Exception:
                    logger.exception(
                        "Error in MQTT callback for topic %s", topic
                    )

    # -- internal: reconnection ---------------------------------------------

    async def _schedule_reconnect(self) -> None:
        """Start the reconnection task if not already running."""
        if self._stop_event.is_set():
            return
        if self._reconnect_task and not self._reconnect_task.done():
            return

        self._set_state(ConnectionState.RECONNECTING)
        self._connected_event.clear()
        self._reconnect_task = asyncio.create_task(
            self._reconnect_loop(), name="mqtt-reconnect"
        )

    async def _reconnect_loop(self) -> None:
        """Attempt to reconnect with exponential back-off.

        Runs until either a connection succeeds or :meth:`disconnect` is called.
        The delay between attempts starts at ``reconnect_min_delay`` and doubles
        each time, capped at ``reconnect_max_delay``.
        """
        delay = self._settings.reconnect_min_delay

        while not self._stop_event.is_set():
            self._reconnect_count += 1
            logger.info(
                "MQTT reconnect attempt %d (delay %.1fs)",
                self._reconnect_count,
                delay,
            )

            # Clean up old client
            if self._client is not None:
                try:
                    await self._client.__aexit__(None, None, None)
                except Exception:
                    pass
                self._client = None

            try:
                await self._do_connect()
                logger.info("MQTT reconnected after %d attempts", self._reconnect_count)
                return
            except Exception as exc:
                logger.warning("MQTT reconnect failed: %s", exc)

            # Exponential back-off with jitter
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=delay,
                )
                # stop_event was set — exit
                return
            except asyncio.TimeoutError:
                pass

            delay = min(delay * 2, self._settings.reconnect_max_delay)

        self._set_state(ConnectionState.DISCONNECTED)

    # -- utility ------------------------------------------------------------

    async def wait_connected(self, timeout: float = 30.0) -> bool:
        """Wait until the client is connected or timeout expires.

        Parameters
        ----------
        timeout:
            Maximum seconds to wait.

        Returns
        -------
        bool
            ``True`` if connected, ``False`` if timed out.
        """
        try:
            await asyncio.wait_for(self._connected_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    def get_status(self) -> dict[str, Any]:
        """Return diagnostic information about the connection.

        Returns
        -------
        dict
            Keys: ``state``, ``broker``, ``port``, ``client_id``,
            ``reconnect_count``, ``subscriptions``.
        """
        return {
            "state": self._state.value,
            "broker": self._settings.host,
            "port": self._settings.port,
            "client_id": self._settings.client_id,
            "reconnect_count": self._reconnect_count,
            "subscriptions": [s.topic for s in self._subscriptions],
        }
