# Unused / deprecated

Code moved here on **2026-04-23** when localization was dropped from the
active feature set.  Nothing here is imported by the running system —
the imports and usages have been commented out (not deleted) in the
original locations, so flipping localization back on is mostly a matter
of un-commenting them and moving the files back.

## What's here

| Path | What it was |
|------|-------------|
| `detection/apriltag_detector.py` | AprilTag detection (pupil-apriltags) — per-frame tag id + pose extraction |
| `infra/position_tracker.py` | World-frame position book-keeping from AprilTag sightings, nearest-robot helpers, zone adjacency |
| `config_data/tag_positions.json` | Tag id → world coords / zone mapping |

## Where the commented-out usages live (search for "# LOCALIZATION:")

- `detection/engine.py` — tag detection + position update inside the mission loop
- `infra/robot_registry.py` — `position`, `last_tag_id`, `position_updated_at`, `turn_offset_deg` fields on `RegisteredRobot`
- `gateway.py` — `/api/positions`, `/api/map_meta`, `/api/tag_positions`, `/api/debug/simulate_tag`, `/api/debug/nearest_support`, heartbeat `turn_offset_deg` handling, `_on_robot_state` subscriber
- `planner/tools.py` — nearest-robot / zone-adjacency imports
- `frontend/index.html` — fetches for `/api/positions` and `/api/tag_positions`

## To re-enable

1. `git mv` the files back to their original locations
2. Grep for `# LOCALIZATION:` and uncomment the blocks
3. Restart
