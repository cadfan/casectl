"""ExpansionBoard I2C driver for the STM32 expansion board (Freenove FNK0107B).

Communicates with the STM32 co-processor at I2C address 0x21 on bus 1.
Provides control of RGB LEDs, cooling fans, and temperature monitoring.
"""

from __future__ import annotations

import asyncio
import logging
import time
from enum import IntEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from smbus2 import SMBus

try:
    import smbus2

    _available = True
except ImportError:
    smbus2 = None  # type: ignore[assignment]
    _available = False

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# I2C register map
# ---------------------------------------------------------------------------

# Write registers
REG_SET_LED_COLOR = 0x01  # [led_id, r, g, b]
REG_SET_ALL_LED_COLOR = 0x02  # [r, g, b]
REG_LED_MODE = 0x03  # [mode]
REG_FAN_MODE = 0x04  # [mode]
REG_FAN_FREQ = 0x05  # [hi, lo]
REG_FAN_DUTY = 0x06  # [d0, d1, d2] 0-255
REG_FAN_THRESHOLD = 0x07  # [lo, hi]
REG_FAN_TEMP_SPEED = 0x09  # [lo, mid, hi]
REG_FAN_POWER = 0x0A  # [switch]
REG_FAN_PI_FOLLOW = 0x0B  # [min, max]

# Read registers
REG_READ_TEMP = 0xFC  # 1 byte
REG_READ_FAN_MODE = 0xF7  # 1 byte
REG_READ_FAN_DUTY = 0xF9  # 3 bytes
REG_READ_MOTOR_SPEED = 0xF2  # 6 bytes (3 x 16-bit)
REG_READ_LED_MODE = 0xF6  # 1 byte
REG_READ_LED_COLOR = 0xF5  # 3 bytes

# Bus parameters
DEFAULT_BUS = 1
DEFAULT_ADDRESS = 0x21
INTER_TRANSACTION_DELAY = 0.01  # 10 ms between consecutive I2C transactions
RETRY_DELAY = 0.1  # 100 ms before retry on OSError
MAX_CONSECUTIVE_ERRORS = 3


class LedHwMode(IntEnum):
    """Hardware LED operating modes."""

    CLOSE = 0
    RGB = 1
    FOLLOWING = 2
    BREATHING = 3
    RAINBOW = 4


class FanHwMode(IntEnum):
    """Hardware fan operating modes."""

    CLOSE = 0
    MANUAL = 1
    AUTO_TEMP = 2
    PI_FOLLOWING = 3


class ExpansionBoard:
    """Driver for the STM32 expansion board over I2C.

    Provides synchronous methods for all hardware operations plus ``async_*``
    wrappers that delegate to :func:`asyncio.to_thread` for use in async code.

    Parameters
    ----------
    bus:
        I2C bus number (default ``1``).
    address:
        7-bit I2C address of the expansion board (default ``0x21``).
    """

    def __init__(self, bus: int = DEFAULT_BUS, address: int = DEFAULT_ADDRESS) -> None:
        self._bus_number = bus
        self._address = address
        self._bus: SMBus | None = None
        self._consecutive_errors: int = 0
        self._degraded: bool = False
        self._closed: bool = False
        self._last_transaction: float = 0.0

        if not _available:
            logger.warning("smbus2 is not installed — expansion board will be unavailable")
            return

        try:
            self._bus = smbus2.SMBus(bus)
            logger.info("Expansion board opened on bus %d, address 0x%02X", bus, address)
        except OSError:
            logger.warning(
                "Failed to open I2C bus %d — expansion board unavailable", bus, exc_info=True
            )
            self._bus = None

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        """Return ``True`` if the I2C bus handle is open and usable."""
        return self._bus is not None and not self._closed

    @property
    def degraded(self) -> bool:
        """Return ``True`` after too many consecutive I2C errors."""
        return self._degraded

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> ExpansionBoard:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying I2C bus handle."""
        if self._bus is not None:
            try:
                self._bus.close()
            except OSError:
                logger.debug("Error closing I2C bus", exc_info=True)
            finally:
                self._bus = None
                self._closed = True
        logger.debug("Expansion board closed")

    # ------------------------------------------------------------------
    # Low-level I2C helpers (with retry + degraded tracking)
    # ------------------------------------------------------------------

    def _inter_transaction_wait(self) -> None:
        """Enforce a minimum delay between consecutive I2C transactions."""
        now = time.monotonic()
        elapsed = now - self._last_transaction
        if elapsed < INTER_TRANSACTION_DELAY:
            time.sleep(INTER_TRANSACTION_DELAY - elapsed)
        self._last_transaction = time.monotonic()

    def _write_block(self, register: int, data: list[int]) -> None:
        """Write a block of bytes to *register* with retry-once semantics."""
        if self._bus is None:
            raise OSError("I2C bus is not open")

        self._inter_transaction_wait()

        try:
            self._bus.write_i2c_block_data(self._address, register, data)
            self._on_success()
        except OSError:
            logger.debug(
                "I2C write to 0x%02X failed, retrying in %.1fs", register, RETRY_DELAY
            )
            time.sleep(RETRY_DELAY)
            try:
                self._bus.write_i2c_block_data(self._address, register, data)
                self._on_success()
            except OSError:
                self._on_error()
                raise

    def _read_block(self, register: int, length: int) -> list[int]:
        """Read *length* bytes from *register* with retry-once semantics."""
        if self._bus is None:
            raise OSError("I2C bus is not open")

        self._inter_transaction_wait()

        try:
            data: list[int] = self._bus.read_i2c_block_data(self._address, register, length)
            self._on_success()
            return data
        except OSError:
            logger.debug(
                "I2C read from 0x%02X failed, retrying in %.1fs", register, RETRY_DELAY
            )
            time.sleep(RETRY_DELAY)
            try:
                data = self._bus.read_i2c_block_data(self._address, register, length)
                self._on_success()
                return data
            except OSError:
                self._on_error()
                raise

    def _on_success(self) -> None:
        """Reset error tracking on a successful transaction."""
        if self._consecutive_errors > 0:
            logger.debug("I2C recovered after %d consecutive errors", self._consecutive_errors)
        self._consecutive_errors = 0
        if self._degraded:
            logger.info("Expansion board recovered from degraded state")
            self._degraded = False

    def _on_error(self) -> None:
        """Track consecutive errors and enter degraded mode when threshold is hit."""
        self._consecutive_errors += 1
        logger.warning(
            "I2C error #%d (degraded threshold: %d)",
            self._consecutive_errors,
            MAX_CONSECUTIVE_ERRORS,
        )
        if self._consecutive_errors >= MAX_CONSECUTIVE_ERRORS and not self._degraded:
            self._degraded = True
            logger.error(
                "Expansion board entering degraded mode after %d consecutive errors",
                self._consecutive_errors,
            )

    # ------------------------------------------------------------------
    # LED control (sync)
    # ------------------------------------------------------------------

    def set_led_color(self, led_id: int, r: int, g: int, b: int) -> None:
        """Set a single LED to an RGB colour.

        Parameters
        ----------
        led_id:
            Zero-based index of the LED.
        r, g, b:
            Colour channel values 0-255.
        """
        self._write_block(REG_SET_LED_COLOR, [led_id, r, g, b])
        logger.debug("LED %d set to (%d, %d, %d)", led_id, r, g, b)

    def set_all_led_color(self, r: int, g: int, b: int) -> None:
        """Set all LEDs to the same RGB colour."""
        self._write_block(REG_SET_ALL_LED_COLOR, [r, g, b])
        logger.debug("All LEDs set to (%d, %d, %d)", r, g, b)

    def set_led_mode(self, mode: LedHwMode | int) -> None:
        """Set the LED operating mode."""
        self._write_block(REG_LED_MODE, [int(mode)])
        logger.debug("LED mode set to %s", LedHwMode(mode).name)

    # ------------------------------------------------------------------
    # Fan control (sync)
    # ------------------------------------------------------------------

    def set_fan_mode(self, mode: FanHwMode | int) -> None:
        """Set the fan operating mode."""
        self._write_block(REG_FAN_MODE, [int(mode)])
        logger.debug("Fan mode set to %s", FanHwMode(mode).name)

    def set_fan_freq(self, frequency: int) -> None:
        """Set the fan PWM frequency (16-bit value, big-endian)."""
        hi = (frequency >> 8) & 0xFF
        lo = frequency & 0xFF
        self._write_block(REG_FAN_FREQ, [hi, lo])
        logger.debug("Fan frequency set to %d", frequency)

    def set_fan_duty(self, d0: int, d1: int, d2: int) -> None:
        """Set fan duty cycles for the three fan channels (0-255 each)."""
        self._write_block(REG_FAN_DUTY, [d0, d1, d2])
        logger.debug("Fan duty set to (%d, %d, %d)", d0, d1, d2)

    def set_fan_threshold(self, lo: int, hi: int) -> None:
        """Set fan auto-temperature threshold low/high."""
        self._write_block(REG_FAN_THRESHOLD, [lo, hi])
        logger.debug("Fan threshold set to lo=%d, hi=%d", lo, hi)

    def set_fan_temp_speed(self, lo: int, mid: int, hi: int) -> None:
        """Set fan speed mapping for low/mid/high temperature bands."""
        self._write_block(REG_FAN_TEMP_SPEED, [lo, mid, hi])
        logger.debug("Fan temp speed set to lo=%d, mid=%d, hi=%d", lo, mid, hi)

    def set_fan_power(self, switch: int) -> None:
        """Set the fan power switch (0 = off, 1 = on)."""
        self._write_block(REG_FAN_POWER, [switch])
        logger.debug("Fan power set to %d", switch)

    def set_fan_pi_follow(self, min_val: int, max_val: int) -> None:
        """Configure PI-following mode min/max parameters."""
        self._write_block(REG_FAN_PI_FOLLOW, [min_val, max_val])
        logger.debug("Fan PI follow set to min=%d, max=%d", min_val, max_val)

    # ------------------------------------------------------------------
    # Sensor / state reads (sync)
    # ------------------------------------------------------------------

    def get_temperature(self) -> int:
        """Read the on-board temperature sensor (degrees C)."""
        data = self._read_block(REG_READ_TEMP, 1)
        temp = data[0]
        logger.debug("Board temperature: %d°C", temp)
        return temp

    def get_fan_mode(self) -> FanHwMode:
        """Read the current fan mode."""
        data = self._read_block(REG_READ_FAN_MODE, 1)
        mode = FanHwMode(data[0])
        logger.debug("Fan mode: %s", mode.name)
        return mode

    def get_fan_duty(self) -> tuple[int, int, int]:
        """Read the current fan duty cycles for three channels."""
        data = self._read_block(REG_READ_FAN_DUTY, 3)
        duty = (data[0], data[1], data[2])
        logger.debug("Fan duty: %s", duty)
        return duty

    def get_motor_speed(self) -> tuple[int, int, int]:
        """Read RPM for three motors (16-bit big-endian per motor)."""
        data = self._read_block(REG_READ_MOTOR_SPEED, 6)
        speeds = (
            (data[0] << 8) | data[1],
            (data[2] << 8) | data[3],
            (data[4] << 8) | data[5],
        )
        logger.debug("Motor speeds: %s RPM", speeds)
        return speeds

    def get_led_mode(self) -> LedHwMode:
        """Read the current LED mode."""
        data = self._read_block(REG_READ_LED_MODE, 1)
        mode = LedHwMode(data[0])
        logger.debug("LED mode: %s", mode.name)
        return mode

    def get_led_color(self) -> tuple[int, int, int]:
        """Read the current LED colour (r, g, b)."""
        data = self._read_block(REG_READ_LED_COLOR, 3)
        color = (data[0], data[1], data[2])
        logger.debug("LED color: %s", color)
        return color

    # ------------------------------------------------------------------
    # Async wrappers
    # ------------------------------------------------------------------

    async def async_set_led_color(self, led_id: int, r: int, g: int, b: int) -> None:
        """Async wrapper for :meth:`set_led_color`."""
        await asyncio.to_thread(self.set_led_color, led_id, r, g, b)

    async def async_set_all_led_color(self, r: int, g: int, b: int) -> None:
        """Async wrapper for :meth:`set_all_led_color`."""
        await asyncio.to_thread(self.set_all_led_color, r, g, b)

    async def async_set_led_mode(self, mode: LedHwMode | int) -> None:
        """Async wrapper for :meth:`set_led_mode`."""
        await asyncio.to_thread(self.set_led_mode, mode)

    async def async_set_fan_mode(self, mode: FanHwMode | int) -> None:
        """Async wrapper for :meth:`set_fan_mode`."""
        await asyncio.to_thread(self.set_fan_mode, mode)

    async def async_set_fan_freq(self, frequency: int) -> None:
        """Async wrapper for :meth:`set_fan_freq`."""
        await asyncio.to_thread(self.set_fan_freq, frequency)

    async def async_set_fan_duty(self, d0: int, d1: int, d2: int) -> None:
        """Async wrapper for :meth:`set_fan_duty`."""
        await asyncio.to_thread(self.set_fan_duty, d0, d1, d2)

    async def async_set_fan_threshold(self, lo: int, hi: int) -> None:
        """Async wrapper for :meth:`set_fan_threshold`."""
        await asyncio.to_thread(self.set_fan_threshold, lo, hi)

    async def async_set_fan_temp_speed(self, lo: int, mid: int, hi: int) -> None:
        """Async wrapper for :meth:`set_fan_temp_speed`."""
        await asyncio.to_thread(self.set_fan_temp_speed, lo, mid, hi)

    async def async_set_fan_power(self, switch: int) -> None:
        """Async wrapper for :meth:`set_fan_power`."""
        await asyncio.to_thread(self.set_fan_power, switch)

    async def async_set_fan_pi_follow(self, min_val: int, max_val: int) -> None:
        """Async wrapper for :meth:`set_fan_pi_follow`."""
        await asyncio.to_thread(self.set_fan_pi_follow, min_val, max_val)

    async def async_get_temperature(self) -> int:
        """Async wrapper for :meth:`get_temperature`."""
        return await asyncio.to_thread(self.get_temperature)

    async def async_get_fan_mode(self) -> FanHwMode:
        """Async wrapper for :meth:`get_fan_mode`."""
        return await asyncio.to_thread(self.get_fan_mode)

    async def async_get_fan_duty(self) -> tuple[int, int, int]:
        """Async wrapper for :meth:`get_fan_duty`."""
        return await asyncio.to_thread(self.get_fan_duty)

    async def async_get_motor_speed(self) -> tuple[int, int, int]:
        """Async wrapper for :meth:`get_motor_speed`."""
        return await asyncio.to_thread(self.get_motor_speed)

    async def async_get_led_mode(self) -> LedHwMode:
        """Async wrapper for :meth:`get_led_mode`."""
        return await asyncio.to_thread(self.get_led_mode)

    async def async_get_led_color(self) -> tuple[int, int, int]:
        """Async wrapper for :meth:`get_led_color`."""
        return await asyncio.to_thread(self.get_led_color)
