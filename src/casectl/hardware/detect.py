"""Hardware detection utilities for casectl.

All functions return safe defaults (``False``, empty dicts) on failure and
never raise exceptions, making them suitable for use during startup probing
on both Raspberry Pi and non-Pi hosts.
"""

from __future__ import annotations

import logging
import os
import socket
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# sysfs / devfs paths
# ---------------------------------------------------------------------------
DEVICE_MODEL_PATH = Path("/sys/firmware/devicetree/base/model")
DEVICE_REVISION_PATH = Path("/sys/firmware/devicetree/base/system/linux,revision")
DEVICE_SERIAL_PATH = Path("/sys/firmware/devicetree/base/serial-number")
I2C_DEVICE_PATH = Path("/dev/i2c-1")

EXPANSION_BOARD_ADDRESS = 0x21
OLED_ADDRESS = 0x3C
I2C_BUS = 1


# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------


def is_raspberry_pi() -> bool:
    """Return ``True`` if the host appears to be a Raspberry Pi.

    Checks ``/sys/firmware/devicetree/base/model`` for the string
    ``"Raspberry Pi"``.
    """
    try:
        model = DEVICE_MODEL_PATH.read_text(errors="replace").strip().rstrip("\x00")
        return "Raspberry Pi" in model
    except Exception:
        logger.debug("Could not read device model — assuming not a Raspberry Pi", exc_info=True)
        return False


# ---------------------------------------------------------------------------
# I2C device probing
# ---------------------------------------------------------------------------


def _probe_i2c_address(bus: int, address: int) -> bool:
    """Attempt to open an smbus2 connection and read from *address*.

    Returns ``True`` if the device acknowledges, ``False`` otherwise.
    """
    try:
        import smbus2
    except ImportError:
        logger.debug("smbus2 not installed — cannot probe I2C address 0x%02X", address)
        return False

    try:
        with smbus2.SMBus(bus) as smb:
            # A 1-byte read is the lightest-weight probe.  The register
            # value (0x00) does not matter — we only care whether the
            # device ACKs its address.
            smb.read_byte(address)
            return True
    except OSError:
        logger.debug("No I2C device at bus %d, address 0x%02X", bus, address)
        return False
    except Exception:
        logger.debug("Unexpected error probing I2C 0x%02X", address, exc_info=True)
        return False


def is_case_hardware_present() -> bool:
    """Return ``True`` if the STM32 expansion board responds on I2C.

    Probes address ``0x21`` on bus ``1``.
    """
    return _probe_i2c_address(I2C_BUS, EXPANSION_BOARD_ADDRESS)


def is_oled_present() -> bool:
    """Return ``True`` if the SSD1306 OLED display responds on I2C.

    Probes address ``0x3C`` on bus ``1``.
    """
    return _probe_i2c_address(I2C_BUS, OLED_ADDRESS)


# ---------------------------------------------------------------------------
# Platform information
# ---------------------------------------------------------------------------


def get_platform_info() -> dict[str, str]:
    """Collect platform information from sysfs and the OS.

    Returns a dictionary with keys ``model``, ``revision``, ``serial``, and
    ``hostname``.  Missing values are represented as empty strings.
    """
    info: dict[str, str] = {
        "model": "",
        "revision": "",
        "serial": "",
        "hostname": "",
    }

    try:
        info["model"] = DEVICE_MODEL_PATH.read_text(errors="replace").strip().rstrip("\x00")
    except Exception:
        logger.debug("Could not read device model", exc_info=True)

    try:
        raw_bytes = DEVICE_REVISION_PATH.read_bytes()
        # The revision is stored as a big-endian 32-bit integer in the DT.
        info["revision"] = raw_bytes.hex()
    except Exception:
        logger.debug("Could not read device revision", exc_info=True)

    try:
        info["serial"] = DEVICE_SERIAL_PATH.read_text(errors="replace").strip().rstrip("\x00")
    except Exception:
        logger.debug("Could not read device serial", exc_info=True)

    try:
        info["hostname"] = socket.gethostname()
    except Exception:
        logger.debug("Could not determine hostname", exc_info=True)

    return info


# ---------------------------------------------------------------------------
# Permission checks
# ---------------------------------------------------------------------------


def check_i2c_permissions() -> tuple[bool, str]:
    """Check whether the I2C bus device exists and is accessible.

    Returns
    -------
    tuple[bool, str]
        ``(True, "")`` if ``/dev/i2c-1`` exists and can be opened for
        read/write, otherwise ``(False, <helpful message>)``.
    """
    if not I2C_DEVICE_PATH.exists():
        return (
            False,
            f"{I2C_DEVICE_PATH} does not exist. "
            "Ensure I2C is enabled: run 'sudo raspi-config' and enable I2C "
            "under Interface Options, or add 'dtparam=i2c_arm=on' to "
            "/boot/firmware/config.txt and reboot.",
        )

    if not os.access(I2C_DEVICE_PATH, os.R_OK | os.W_OK):
        return (
            False,
            f"{I2C_DEVICE_PATH} exists but is not accessible. "
            f"Add the current user to the 'i2c' group: "
            f"'sudo usermod -aG i2c $USER' and then log out and back in.",
        )

    return (True, "")
