from app.scheduler import _adaptive_fan, _outside_is_mild


def test_adaptive_fan_thresholds():
    assert _adaptive_fan(1.1) == "low"
    assert _adaptive_fan(2.9) == "low"
    assert _adaptive_fan(3.0) == "medium"
    assert _adaptive_fan(5.9) == "medium"
    assert _adaptive_fan(6.0) == "high"
    assert _adaptive_fan(10.0) == "high"


def test_outside_mild_window():
    assert _outside_is_mild(74, 74) is True
    assert _outside_is_mild(68, 74) is True
    assert _outside_is_mild(80, 74) is True
    assert _outside_is_mild(67, 74) is False
    assert _outside_is_mild(81, 74) is False
    assert _outside_is_mild(None, 74) is False
