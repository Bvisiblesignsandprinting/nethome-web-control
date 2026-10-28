"""Quota protection across legacy panel, browser and background entry points."""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from app import main, scheduler
from app.tuya_client import TuyaCloudError


@pytest.fixture
def sensor_mode(monkeypatch):
    config = {"enabled": False, "target_temperature_f": 73}
    sensor = {
        "ok": True, "temperature_f": 72.0, "humidity": 45,
        "online": True, "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    cloud = SimpleNamespace(configured=True, indoor_sensor=Mock(return_value=sensor))
    activity = Mock()
    for module in (main, scheduler):
        monkeypatch.setattr(module, "load_smart_control_config", lambda: config)
        monkeypatch.setattr(module, "tuya", cloud)
        monkeypatch.setattr(module, "add_activity", activity)
    return config, cloud, activity


def test_off_multiple_callers_and_scheduler_make_no_sensor_requests(sensor_mode):
    config, cloud, activity = sensor_mode
    for _ in range(12):
        response = main.indoor_sensor()
        assert response["paused"] and not response["offline"]
        status = main.smart_control_status()
        assert status["sensor_paused"] and status["sensor"] is None
        assert status["sensor_error"] is None and status["status"] == "disabled"
        assert scheduler.run_smart_control()["action"] == "none"
    cloud.indoor_sensor.assert_not_called()
    activity.assert_not_called()


def test_switching_on_resumes_reads_and_switching_off_stops_them(sensor_mode):
    config, cloud, _ = sensor_mode
    config["enabled"] = True
    assert main.indoor_sensor()["temperature_f"] == 72.0
    status = main.smart_control_status()
    assert status["status"] == "monitoring" and not status["sensor_paused"]
    cloud.indoor_sensor.assert_called_with(max_cache_age_seconds=300)
    cloud.indoor_sensor.reset_mock()
    config["enabled"] = False
    assert main.indoor_sensor()["paused"]
    assert main.smart_control_status()["sensor_paused"]
    cloud.indoor_sensor.assert_not_called()


def test_quota_failure_is_visible_only_while_enabled(sensor_mode):
    config, cloud, _ = sensor_mode
    config["enabled"] = True
    cloud.indoor_sensor.side_effect = TuyaCloudError("quota exhausted", code="28841004", retry_after=21600)
    response = main.indoor_sensor()
    assert response["error_code"] == "28841004" and response["retry_after_seconds"] == 21600
    assert main.smart_control_status()["sensor_error"] == "quota exhausted"
    cloud.indoor_sensor.reset_mock()
    config["enabled"] = False
    assert main.indoor_sensor()["paused"]
    assert main.smart_control_status()["sensor_error"] is None
    cloud.indoor_sensor.assert_not_called()


def test_disabled_unconfigured_sensor_still_returns_paused(sensor_mode):
    _, cloud, _ = sensor_mode
    cloud.configured = False
    assert main.indoor_sensor()["paused"]
    cloud.indoor_sensor.assert_not_called()
