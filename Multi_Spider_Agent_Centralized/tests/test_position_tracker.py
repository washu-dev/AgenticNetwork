"""
Unit tests for position_tracker and apriltag_detector.

Run: python -m pytest tests/test_position_tracker.py -v
Or:  python tests/test_position_tracker.py  (runs a simple self-test without pytest)
"""

import json
import math
import sys
import time
from pathlib import Path

# Make Command_Center importable
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from Command_Center.infra import position_tracker as pt
from Command_Center.infra.robot_registry import RobotRegistry, RegisteredRobot, robot_registry
from Command_Center.detection import apriltag_detector as at


# ---------------------------------------------------------------------------
# euclidean
# ---------------------------------------------------------------------------

def test_euclidean_basic():
    assert pt.euclidean((0, 0), (3, 4)) == 5.0
    assert pt.euclidean((0, 0), (0, 0)) == 0.0
    assert math.isclose(pt.euclidean((1.5, 2.5), (4.5, 6.5)), 5.0)


def test_euclidean_negative():
    assert pt.euclidean((-1, -1), (2, 3)) == 5.0


# ---------------------------------------------------------------------------
# zone adjacency (loaded from config_data/zone_adjacency.json)
# ---------------------------------------------------------------------------

def test_adjacency_loaded():
    adj = pt.load_zone_adjacency()
    assert "A" in adj and "B" in adj and "C" in adj


def test_get_neighbors():
    pt.load_zone_adjacency()
    # Linear A-B-C layout from the default config
    assert "B" in pt.get_neighbors("A")
    assert set(pt.get_neighbors("B")) == {"A", "C"}
    assert pt.get_neighbors("unknown_zone") == []


def test_are_adjacent():
    pt.load_zone_adjacency()
    assert pt.are_adjacent("A", "B") is True
    assert pt.are_adjacent("B", "A") is True
    assert pt.are_adjacent("A", "C") is False   # not directly adjacent in linear layout
    assert pt.are_adjacent("A", "A") is True    # same zone always "adjacent" to itself


# ---------------------------------------------------------------------------
# position updates + find_nearest_robot
# ---------------------------------------------------------------------------

def _register(robot_id: str, zone: str = ""):
    """Directly insert a test robot into the singleton registry."""
    robot = RegisteredRobot(
        robot_id=robot_id,
        host="127.0.0.1",
        port=9004,
        zone=zone,
        registered_at=time.time(),
        last_heartbeat=time.time(),
        status="online",
    )
    robot_registry._robots[robot_id] = robot
    return robot


def _cleanup(*ids):
    for rid in ids:
        robot_registry._robots.pop(rid, None)


_TEST_ROBOT_PREFIXES = ("test-", "near", "far", "r1", "r2", "busy-bot", "idle-bot", "no-pos-bot")


def _cleanup_all_test_robots():
    """Remove every robot this test file might have inserted (safe to call between tests)."""
    to_remove = [
        rid for rid in list(robot_registry._robots.keys())
        if rid.startswith(_TEST_ROBOT_PREFIXES) or rid in {"near", "far", "r1", "r2"}
    ]
    for rid in to_remove:
        robot = robot_registry._robots.get(rid)
        if robot:
            robot.busy = False
            robot.current_mission_id = None
        robot_registry._robots.pop(rid, None)


def test_update_robot_position():
    _register("test-bot-1", zone="A")
    try:
        pt.update_robot_position("test-bot-1", tag_id=10, pos=(1.0, 2.0), zone="A")
        r = robot_registry.get_robot("test-bot-1")
        assert r.position == (1.0, 2.0)
        assert r.last_tag_id == 10
        assert r.position_updated_at is not None
    finally:
        _cleanup("test-bot-1")


def test_update_position_for_unknown_robot_is_safe():
    """Should log and no-op, not crash."""
    pt.update_robot_position("nonexistent-bot", tag_id=1, pos=(0, 0))


def test_zone_updates_when_robot_moves():
    """Bug fix: zone should refresh on every tag sighting, not stay stale."""
    _register("test-mover", zone="A")
    try:
        pt.update_robot_position("test-mover", tag_id=10, pos=(1.0, 2.0), zone="A")
        assert robot_registry.get_robot("test-mover").zone == "A"
        # Robot moves to a different zone — its zone must update
        pt.update_robot_position("test-mover", tag_id=20, pos=(5.0, 2.0), zone="B")
        assert robot_registry.get_robot("test-mover").zone == "B"
    finally:
        _cleanup("test-mover")


def test_find_nearest_robot_picks_closest():
    _register("near", zone="B")
    _register("far", zone="C")
    try:
        pt.update_robot_position("near", tag_id=21, pos=(5.0, 5.0))
        pt.update_robot_position("far", tag_id=30, pos=(10.0, 10.0))

        result = pt.find_nearest_robot((4.0, 5.0))
        assert result is not None
        assert result["robot_id"] == "near"
        assert math.isclose(result["distance"], 1.0)
    finally:
        _cleanup("near", "far")


def test_find_nearest_robot_excludes_ids():
    _register("r1", zone="A")
    _register("r2", zone="B")
    try:
        pt.update_robot_position("r1", tag_id=10, pos=(1.0, 1.0))
        pt.update_robot_position("r2", tag_id=20, pos=(5.0, 5.0))

        result = pt.find_nearest_robot((0, 0), exclude_ids=["r1"])
        assert result["robot_id"] == "r2"
    finally:
        _cleanup("r1", "r2")


def test_find_nearest_robot_skips_busy_when_require_idle():
    _register("busy-bot", zone="A")
    _register("idle-bot", zone="B")
    try:
        pt.update_robot_position("busy-bot", tag_id=10, pos=(1.0, 1.0))
        pt.update_robot_position("idle-bot", tag_id=20, pos=(10.0, 10.0))
        robot_registry.set_busy("busy-bot", "mission-x")

        result = pt.find_nearest_robot((0, 0), require_idle=True)
        assert result["robot_id"] == "idle-bot"   # closer one is busy, idle one wins

        result_any = pt.find_nearest_robot((0, 0), require_idle=False)
        assert result_any["robot_id"] == "busy-bot"
    finally:
        robot_registry.clear_busy("busy-bot")
        _cleanup("busy-bot", "idle-bot")


def test_find_nearest_robot_returns_none_when_no_positions():
    _register("no-pos-bot", zone="A")
    try:
        result = pt.find_nearest_robot((0, 0))
        # No robots with positions → None
        assert result is None or result["robot_id"] != "no-pos-bot"
    finally:
        _cleanup("no-pos-bot")


# ---------------------------------------------------------------------------
# apriltag_detector config loading
# ---------------------------------------------------------------------------

def test_tag_positions_loaded():
    positions = at.load_tag_positions()
    # Default config has 9 placeholder tags (10,11,12,20,21,22,30,31,32)
    assert len(positions) >= 9
    # Metadata keys (_comment, _how_to_fill) must be filtered out
    assert not any(k.startswith("_") for k in positions.keys())


def test_get_tag_info():
    at.load_tag_positions()
    info = at.get_tag_info(10)
    assert info is not None
    assert "zone" in info and "pos" in info
    assert info["zone"] == "A"


def test_get_tag_info_unknown():
    assert at.get_tag_info(999999) is None


def test_tag_positions_custom_file(tmp_path):
    """Load a custom config file (used for integration tests with real tag measurements)."""
    custom = tmp_path / "custom_tags.json"
    custom.write_text(json.dumps({
        "_comment": "test",
        "100": {"zone": "X", "pos": [1.5, 2.5]},
    }))
    positions = at.load_tag_positions(custom)
    assert "100" in positions
    assert positions["100"]["pos"] == [1.5, 2.5]
    # Restore default for subsequent tests
    at.load_tag_positions()


# ---------------------------------------------------------------------------
# RegisteredRobot fields (integration with new position fields)
# ---------------------------------------------------------------------------

def test_registered_robot_defaults():
    r = RegisteredRobot(robot_id="x", host="h")
    assert r.position is None
    assert r.last_tag_id is None
    assert r.position_updated_at is None


def test_to_dict_includes_position():
    r = RegisteredRobot(robot_id="x", host="h")
    r.position = (1.0, 2.0)
    r.last_tag_id = 42
    r.position_updated_at = 1234.5
    d = r.to_dict()
    assert d["position"] == [1.0, 2.0]
    assert d["last_tag_id"] == 42
    assert d["position_updated_at"] == 1234.5


# ---------------------------------------------------------------------------
# Simple self-test runner (for environments without pytest)
# ---------------------------------------------------------------------------

def _run_all():
    import tempfile
    tests = [
        test_euclidean_basic, test_euclidean_negative,
        test_adjacency_loaded, test_get_neighbors, test_are_adjacent,
        test_update_robot_position, test_update_position_for_unknown_robot_is_safe,
        test_zone_updates_when_robot_moves,
        test_find_nearest_robot_picks_closest, test_find_nearest_robot_excludes_ids,
        test_find_nearest_robot_skips_busy_when_require_idle,
        test_find_nearest_robot_returns_none_when_no_positions,
        test_tag_positions_loaded, test_get_tag_info, test_get_tag_info_unknown,
        test_registered_robot_defaults, test_to_dict_includes_position,
    ]

    passed, failed = 0, 0
    for fn in tests:
        try:
            fn()
            print(f"  ✓ {fn.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  ✗ {fn.__name__}  — {e}")
            failed += 1
        except Exception as e:
            print(f"  ✗ {fn.__name__}  — UNEXPECTED: {type(e).__name__}: {e}")
            failed += 1
        finally:
            _cleanup_all_test_robots()  # Guarantee isolation even if a test crashes

    # Run the tmp_path test manually
    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            test_tag_positions_custom_file(Path(tmpdir))
            print("  ✓ test_tag_positions_custom_file")
            passed += 1
        except Exception as e:
            print(f"  ✗ test_tag_positions_custom_file  — {e}")
            failed += 1
        finally:
            _cleanup_all_test_robots()

    print(f"\n  {passed} passed, {failed} failed")
    return failed == 0


if __name__ == "__main__":
    ok = _run_all()
    sys.exit(0 if ok else 1)
