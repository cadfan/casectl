"""Tests for casectl.hardware.expansion — ExpansionBoard I2C driver."""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from casectl.hardware.expansion import (
    DEFAULT_ADDRESS,
    MAX_CONSECUTIVE_ERRORS,
    REG_FAN_DUTY,
    REG_READ_MOTOR_SPEED,
    ExpansionBoard,
    FanHwMode,
    LedHwMode,
)


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------


class TestInitWithoutSmbus2:
    """Verify graceful degradation when smbus2 is not installed."""

    def test_init_without_smbus2(self) -> None:
        """When smbus2 is not importable, board.connected should be False."""
        import casectl.hardware.expansion as exp_mod

        original = exp_mod._available
        exp_mod._available = False
        try:
            board = ExpansionBoard.__new__(ExpansionBoard)
            board._bus_number = 1
            board._address = 0x21
            board._bus = None
            board._consecutive_errors = 0
            board._degraded = False
            board._closed = False
            board._last_transaction = 0.0
            board._i2c_lock = threading.Lock()

            assert board.connected is False
            assert board.degraded is False
        finally:
            exp_mod._available = original

    def test_init_with_oserror_on_bus_open(self) -> None:
        """If opening the I2C bus raises OSError, connected is False."""
        mock_smbus2 = MagicMock()
        mock_smbus2.SMBus.side_effect = OSError("No such device")

        import casectl.hardware.expansion as exp_mod

        original_available = exp_mod._available
        original_smbus2 = exp_mod.smbus2
        exp_mod._available = True
        exp_mod.smbus2 = mock_smbus2
        try:
            board = ExpansionBoard(bus=1, address=0x21)
            assert board.connected is False
        finally:
            exp_mod._available = original_available
            exp_mod.smbus2 = original_smbus2


# ---------------------------------------------------------------------------
# Connected property
# ---------------------------------------------------------------------------


class TestConnectedProperty:
    """Verify the `connected` property reflects bus state."""

    def test_connected_true_when_bus_open(self, mock_expansion: ExpansionBoard) -> None:
        assert mock_expansion.connected is True

    def test_connected_false_when_bus_none(self, mock_expansion: ExpansionBoard) -> None:
        mock_expansion._bus = None
        assert mock_expansion.connected is False

    def test_connected_false_when_closed(self, mock_expansion: ExpansionBoard) -> None:
        mock_expansion._closed = True
        assert mock_expansion.connected is False

    def test_connected_false_after_close(self, mock_expansion: ExpansionBoard, mock_smbus: MagicMock) -> None:
        mock_expansion.close()
        assert mock_expansion.connected is False
        mock_smbus.close.assert_called_once()


# ---------------------------------------------------------------------------
# set_fan_duty
# ---------------------------------------------------------------------------


class TestSetFanDuty:
    """Verify set_fan_duty writes correct bytes to register 0x06."""

    def test_set_fan_duty_writes_correct_bytes(
        self, mock_expansion: ExpansionBoard, mock_smbus: MagicMock
    ) -> None:
        mock_expansion.set_fan_duty(100, 150, 200)
        mock_smbus.write_i2c_block_data.assert_called_once_with(
            DEFAULT_ADDRESS, REG_FAN_DUTY, [100, 150, 200]
        )

    def test_set_fan_duty_zero(
        self, mock_expansion: ExpansionBoard, mock_smbus: MagicMock
    ) -> None:
        mock_expansion.set_fan_duty(0, 0, 0)
        mock_smbus.write_i2c_block_data.assert_called_once_with(
            DEFAULT_ADDRESS, REG_FAN_DUTY, [0, 0, 0]
        )

    def test_set_fan_duty_max(
        self, mock_expansion: ExpansionBoard, mock_smbus: MagicMock
    ) -> None:
        mock_expansion.set_fan_duty(255, 255, 255)
        mock_smbus.write_i2c_block_data.assert_called_once_with(
            DEFAULT_ADDRESS, REG_FAN_DUTY, [255, 255, 255]
        )

    def test_set_fan_duty_raises_when_bus_none(self, mock_expansion: ExpansionBoard) -> None:
        mock_expansion._bus = None
        with pytest.raises(OSError, match="I2C bus is not open"):
            mock_expansion.set_fan_duty(100, 100, 100)


# ---------------------------------------------------------------------------
# get_motor_speed
# ---------------------------------------------------------------------------


class TestGetMotorSpeed:
    """Verify get_motor_speed reads 6 bytes and decodes 16-bit RPM."""

    def test_get_motor_speed_decodes_correctly(
        self, mock_expansion: ExpansionBoard, mock_smbus: MagicMock
    ) -> None:
        # Motor 0: 0x04 0xB0 = 1200 RPM
        # Motor 1: 0x06 0x40 = 1600 RPM
        # Motor 2: 0x09 0x60 = 2400 RPM
        mock_smbus.read_i2c_block_data.return_value = [0x04, 0xB0, 0x06, 0x40, 0x09, 0x60]
        speeds = mock_expansion.get_motor_speed()
        assert speeds == (1200, 1600, 2400)

    def test_get_motor_speed_zeros(
        self, mock_expansion: ExpansionBoard, mock_smbus: MagicMock
    ) -> None:
        mock_smbus.read_i2c_block_data.return_value = [0, 0, 0, 0, 0, 0]
        speeds = mock_expansion.get_motor_speed()
        assert speeds == (0, 0, 0)

    def test_get_motor_speed_max_values(
        self, mock_expansion: ExpansionBoard, mock_smbus: MagicMock
    ) -> None:
        mock_smbus.read_i2c_block_data.return_value = [0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF]
        speeds = mock_expansion.get_motor_speed()
        assert speeds == (65535, 65535, 65535)

    def test_get_motor_speed_calls_correct_register(
        self, mock_expansion: ExpansionBoard, mock_smbus: MagicMock
    ) -> None:
        mock_smbus.read_i2c_block_data.return_value = [0, 0, 0, 0, 0, 0]
        mock_expansion.get_motor_speed()
        mock_smbus.read_i2c_block_data.assert_called_once_with(
            DEFAULT_ADDRESS, REG_READ_MOTOR_SPEED, 6
        )


# ---------------------------------------------------------------------------
# Retry on OSError
# ---------------------------------------------------------------------------


class TestRetryOnOsError:
    """Verify that write/read operations retry once on OSError."""

    def test_write_retry_on_first_oserror(
        self, mock_expansion: ExpansionBoard, mock_smbus: MagicMock
    ) -> None:
        """First write call fails, retry succeeds."""
        mock_smbus.write_i2c_block_data.side_effect = [OSError("bus fault"), None]
        # Should not raise — the retry succeeds
        mock_expansion.set_fan_duty(100, 100, 100)
        assert mock_smbus.write_i2c_block_data.call_count == 2

    def test_read_retry_on_first_oserror(
        self, mock_expansion: ExpansionBoard, mock_smbus: MagicMock
    ) -> None:
        """First read call fails, retry succeeds."""
        mock_smbus.read_i2c_block_data.side_effect = [
            OSError("bus fault"),
            [0x00, 0x64, 0x00, 0xC8, 0x01, 0x2C],
        ]
        speeds = mock_expansion.get_motor_speed()
        assert speeds == (100, 200, 300)
        assert mock_smbus.read_i2c_block_data.call_count == 2

    def test_write_raises_after_both_attempts_fail(
        self, mock_expansion: ExpansionBoard, mock_smbus: MagicMock
    ) -> None:
        mock_smbus.write_i2c_block_data.side_effect = OSError("persistent fault")
        with pytest.raises(OSError):
            mock_expansion.set_fan_duty(100, 100, 100)

    def test_read_raises_after_both_attempts_fail(
        self, mock_expansion: ExpansionBoard, mock_smbus: MagicMock
    ) -> None:
        mock_smbus.read_i2c_block_data.side_effect = OSError("persistent fault")
        with pytest.raises(OSError):
            mock_expansion.get_motor_speed()


# ---------------------------------------------------------------------------
# Degraded mode tracking
# ---------------------------------------------------------------------------


class TestDegradedMode:
    """Verify degraded mode enters/exits based on consecutive error count."""

    def test_degraded_after_consecutive_errors(
        self, mock_expansion: ExpansionBoard, mock_smbus: MagicMock
    ) -> None:
        """After MAX_CONSECUTIVE_ERRORS failures, degraded becomes True."""
        mock_smbus.write_i2c_block_data.side_effect = OSError("persistent fault")
        for _ in range(MAX_CONSECUTIVE_ERRORS):
            with pytest.raises(OSError):
                mock_expansion.set_fan_duty(100, 100, 100)
        assert mock_expansion.degraded is True

    def test_not_degraded_before_threshold(
        self, mock_expansion: ExpansionBoard, mock_smbus: MagicMock
    ) -> None:
        """Fewer than MAX_CONSECUTIVE_ERRORS failures: not degraded."""
        mock_smbus.write_i2c_block_data.side_effect = OSError("fault")
        for _ in range(MAX_CONSECUTIVE_ERRORS - 1):
            with pytest.raises(OSError):
                mock_expansion.set_fan_duty(100, 100, 100)
        assert mock_expansion.degraded is False

    def test_degraded_resets_on_success(
        self, mock_expansion: ExpansionBoard, mock_smbus: MagicMock
    ) -> None:
        """A successful operation after degraded state clears the flag."""
        # Force degraded state
        mock_expansion._consecutive_errors = MAX_CONSECUTIVE_ERRORS
        mock_expansion._degraded = True

        # Next call succeeds
        mock_smbus.write_i2c_block_data.side_effect = None
        mock_expansion.set_fan_duty(100, 100, 100)

        assert mock_expansion.degraded is False
        assert mock_expansion._consecutive_errors == 0

    def test_error_counter_resets_on_success_after_partial_errors(
        self, mock_expansion: ExpansionBoard, mock_smbus: MagicMock
    ) -> None:
        """A single success resets the consecutive error counter to 0."""
        mock_expansion._consecutive_errors = 2
        mock_smbus.write_i2c_block_data.side_effect = None
        mock_expansion.set_fan_duty(50, 50, 50)
        assert mock_expansion._consecutive_errors == 0


# ---------------------------------------------------------------------------
# I2C lock prevents concurrent access
# ---------------------------------------------------------------------------


class TestI2cLock:
    """Verify threading.Lock is used to serialise I2C transactions."""

    def test_i2c_lock_is_acquired_during_write(
        self, mock_expansion: ExpansionBoard, mock_smbus: MagicMock
    ) -> None:
        lock = mock_expansion._i2c_lock
        acquired_in_write = []

        original_write = mock_smbus.write_i2c_block_data

        def recording_write(*args: object, **kwargs: object) -> None:
            # The lock should already be held by _write_block
            acquired_in_write.append(not lock.acquire(blocking=False))
            # If we did acquire it, release it to avoid deadlock
            if not acquired_in_write[-1]:
                lock.release()
            original_write(*args, **kwargs)

        mock_smbus.write_i2c_block_data = recording_write
        mock_expansion.set_fan_duty(100, 100, 100)

        # The lock was held when our recording function ran
        assert acquired_in_write[0] is True

    def test_i2c_lock_is_acquired_during_read(
        self, mock_expansion: ExpansionBoard, mock_smbus: MagicMock
    ) -> None:
        lock = mock_expansion._i2c_lock
        acquired_in_read = []

        original_read = mock_smbus.read_i2c_block_data

        def recording_read(*args: object, **kwargs: object) -> list[int]:
            acquired_in_read.append(not lock.acquire(blocking=False))
            if not acquired_in_read[-1]:
                lock.release()
            return original_read(*args, **kwargs)

        mock_smbus.read_i2c_block_data = recording_read
        mock_expansion.get_motor_speed()

        assert acquired_in_read[0] is True

    def test_concurrent_writes_are_serialised(
        self, mock_expansion: ExpansionBoard, mock_smbus: MagicMock
    ) -> None:
        """Two threads writing simultaneously must not interleave."""
        call_order: list[int] = []

        original_write = mock_smbus.write_i2c_block_data

        def slow_write(*args: object, **kwargs: object) -> None:
            thread_id = threading.current_thread().ident
            call_order.append(thread_id)
            time.sleep(0.05)
            call_order.append(thread_id)

        mock_smbus.write_i2c_block_data = slow_write

        t1 = threading.Thread(target=mock_expansion.set_fan_duty, args=(100, 100, 100))
        t2 = threading.Thread(target=mock_expansion.set_fan_duty, args=(200, 200, 200))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # Calls should be paired: [A, A, B, B] or [B, B, A, A]
        # — never interleaved like [A, B, A, B].
        assert call_order[0] == call_order[1]
        assert call_order[2] == call_order[3]


# ---------------------------------------------------------------------------
# Context manager
# ---------------------------------------------------------------------------


class TestContextManager:
    """Verify context manager protocol closes the bus."""

    def test_context_manager_closes_bus(self, mock_smbus: MagicMock) -> None:
        import casectl.hardware.expansion as exp_mod

        board = ExpansionBoard.__new__(ExpansionBoard)
        board._bus_number = 1
        board._address = 0x21
        board._bus = mock_smbus
        board._consecutive_errors = 0
        board._degraded = False
        board._closed = False
        board._last_transaction = 0.0
        board._i2c_lock = threading.Lock()

        with board:
            assert board.connected is True
        assert board.connected is False


# ---------------------------------------------------------------------------
# LED / Fan mode reads
# ---------------------------------------------------------------------------


class TestModeReads:
    """Verify mode read helpers decode correctly."""

    def test_get_fan_mode(self, mock_expansion: ExpansionBoard, mock_smbus: MagicMock) -> None:
        mock_smbus.read_i2c_block_data.return_value = [1]
        mode = mock_expansion.get_fan_mode()
        assert mode == FanHwMode.MANUAL

    def test_get_led_mode(self, mock_expansion: ExpansionBoard, mock_smbus: MagicMock) -> None:
        mock_smbus.read_i2c_block_data.return_value = [4]
        mode = mock_expansion.get_led_mode()
        assert mode == LedHwMode.RAINBOW

    def test_get_fan_duty(self, mock_expansion: ExpansionBoard, mock_smbus: MagicMock) -> None:
        mock_smbus.read_i2c_block_data.return_value = [50, 100, 200]
        duty = mock_expansion.get_fan_duty()
        assert duty == (50, 100, 200)
