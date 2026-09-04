"""Scheduling configuration validation follows runtime parser behavior."""

from app.services.schedule_config_validation import validate_schedule_value


def test_cron_validation_uses_runtime_cron_parser():
    assert validate_schedule_value("cron", "0 9 * * MON-FRI", "Asia/Shanghai")
    assert not validate_schedule_value("cron", "not a cron", "Asia/Shanghai")
    assert not validate_schedule_value("cron", "0 0 31 2 *", "Asia/Shanghai")


def test_datetime_validation_accepts_runtime_supported_iso_values():
    assert validate_schedule_value("datetime", "2026-09-05", "Asia/Shanghai")
    assert validate_schedule_value("datetime", "2026-09-05T09:00:00+08:00", "UTC")
    assert not validate_schedule_value("datetime", "0000-01-01T00:00:00Z", "UTC")


def test_timezone_validation_uses_zoneinfo():
    assert validate_schedule_value("timezone", "Asia/Shanghai", "Asia/Shanghai")
    assert not validate_schedule_value("timezone", "Mars/Olympus", "Mars/Olympus")
