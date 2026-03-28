"""Thin wrapper around luma.oled for the SSD1306 OLED display.

Provides synchronous and async methods for rendering PIL images to the
128x64 I2C OLED at address 0x3C.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PIL import Image

try:
    from luma.core.interface.serial import i2c as luma_i2c
    from luma.oled.device import ssd1306

    _luma_available = True
except ImportError:
    _luma_available = False

logger = logging.getLogger(__name__)


class OledDevice:
    """SSD1306 OLED display driver using luma.oled.

    Parameters
    ----------
    bus:
        I2C bus number (default ``1``).
    address:
        7-bit I2C address of the OLED (default ``0x3C``).
    rotation:
        Display rotation in degrees (``0``, ``1``, ``2``, or ``3``
        representing 0, 90, 180, 270 degree rotations).
    """

    MAX_CONSECUTIVE_ERRORS: int = 3

    def __init__(
        self,
        bus: int = 1,
        address: int = 0x3C,
        rotation: int = 0,
    ) -> None:
        self._available: bool = False
        self._device: ssd1306 | None = None  # type: ignore[name-defined]
        self._consecutive_errors: int = 0

        if not _luma_available:
            logger.warning(
                "luma.oled / luma.core not installed — OLED display unavailable"
            )
            return

        try:
            serial = luma_i2c(port=bus, address=address)
            self._device = ssd1306(serial, rotate=rotation)
            self._available = True
            logger.info(
                "OLED display initialised on bus %d, address 0x%02X, rotation %d",
                bus,
                address,
                rotation,
            )
        except Exception:
            logger.warning("Failed to initialise OLED display", exc_info=True)
            self._device = None
            self._available = False

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def available(self) -> bool:
        """Return ``True`` if the OLED display was initialised successfully."""
        return self._available

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> OledDevice:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Release the display device resources."""
        if self._device is not None:
            try:
                self._device.cleanup()
            except Exception:
                logger.debug("Error during OLED cleanup", exc_info=True)
            finally:
                self._device = None
                self._available = False
        logger.debug("OLED device closed")

    # ------------------------------------------------------------------
    # Display operations
    # ------------------------------------------------------------------

    def render_image(self, image: Image.Image) -> None:
        """Write a PIL image frame to the OLED display.

        Parameters
        ----------
        image:
            A :class:`PIL.Image.Image` to render.  Should be mode ``"1"``
            (1-bit) at 128x64 for best results, though luma will convert
            other modes automatically.
        """
        if self._device is None:
            logger.debug("render_image called but OLED is not available")
            return

        try:
            self._device.display(image)
            self._consecutive_errors = 0
            self._available = True
        except Exception:
            self._consecutive_errors += 1
            if self._consecutive_errors >= self.MAX_CONSECUTIVE_ERRORS:
                logger.error(
                    "OLED marked unavailable after %d consecutive errors",
                    self._consecutive_errors,
                )
                self._available = False
            else:
                logger.warning(
                    "OLED render error %d/%d",
                    self._consecutive_errors,
                    self.MAX_CONSECUTIVE_ERRORS,
                )

    async def async_render_image(self, image: Image.Image) -> None:
        """Async wrapper for :meth:`render_image`."""
        await asyncio.to_thread(self.render_image, image)

    def clear(self) -> None:
        """Blank the display."""
        if self._device is None:
            logger.debug("clear called but OLED is not available")
            return

        try:
            self._device.hide()
            self._device.show()
        except Exception:
            logger.warning("Failed to clear OLED display", exc_info=True)
            self._available = False
