# Lighting software before hardware connection

The authenticated Lighting page stores shared settings for four logical groups:

| ID | Group | Current controls |
| --- | --- | --- |
| rgb-walls | RGB Walls | On/off, RGB color, white channel, brightness |
| rgb-waterfall | RGB Waterfall | On/off, RGB color, white channel, brightness |
| regular-walls-roof | Regular Walls + Roof | On/off |
| chandeliers | Chandeliers | On/off |

Three existing FUT038 receivers belong to two RGB groups; the software does not invent a third RGB zone or guess receiver-to-zone pairing. Physical pairing is a later connection step. Regular walls and roof stay grouped. Regular-light dimming is not exposed.

## Current behavior

`GET /api/lighting` returns desired settings, a revision number, schedules and explicit `not_connected` / `preview_only` state. Reported hardware state is always null. Authenticated updates save a preview, never send network or AC commands, and never append to the AC execution queue. Nothing automatically applies saved power settings on a future connection.

The page supports save/reload, RGB/white selection, brightness, and schedule create/edit/disable/delete. Timing can be daily, selected weekdays or a specific date, in a validated IANA timezone. The default is America/New_York. Preview returns the next three planned instants. Nonexistent spring-forward times are skipped; a repeated fall-back time occurs only once, in the first hour. Past enabled one-time schedules are rejected. Saving an enabled schedule records intent for later activation, not active hardware automation.

## Persistence

No schema migration or API-key change is needed. A single `lighting-config` / `settings` document uses the app's existing private `activity` configuration pattern, with bounded schedules (100), optimistic revision checks and atomic read-modify-write. PostgreSQL uses a dedicated transaction advisory lock; SQLite uses BEGIN IMMEDIATE. Revision conflicts return 409, not silent overwrites. Only readable save events appear in Activity; the internal configuration document is excluded.

All lighting routes use the existing login/token dependency. Clients cannot set connected state or insert hardware endpoints. No secrets or credentials were added. The music upload/player implementation, AC control/scheduler and firmware manifest are preserved.

## Connection phase still required

After the exact gateway/relay devices are selected, implement their authenticated delivery adapter and capability mapping. Keep desired state separate from delivery acknowledgment and actual reported state. Connect only through a reachable local bridge or supported vendor API; a Vercel server cannot directly reach a private LAN address. Verify receiver grouping and regular-light circuits, then explicitly activate schedules. Do not replay missed runs or apply old saved previews on connection. Hardware delivery, a live lighting scheduler and ESP32 Lighting UI are not part of this software-only website update. Browser music continues to work; Bluetooth audio from the panel remains a separate implementation/test step.

## Validation

Run `.venv/bin/python -m pytest tests/test_lighting.py tests/test_music.py -q` and `node --check app/static/lighting.js`. Tests cover auth, real SQLite persistence, concurrent revisions, CRUD, capability validation, AC-queue isolation, and DST timing. Production PostgreSQL credentials are not used by local tests.

The complete Python suite passed (61 tests). A DOM-based interaction check against the real lighting API/SQLite test server passed RGB save/reload, retention of unsaved settings in another zone, weekly preview and schedule create/edit/disable/delete without AC requests or script errors. Browser access to the local server was unavailable. A real PostgreSQL run was not completed: the local runtime could not change database-process ownership, and transfer to a temporary external test environment was blocked. Live PostgreSQL and visual browser verification remain pending. Automatic deployment from this feature branch is disabled; merging into main retains the existing production deployment workflow.

## RGB effects

Each RGB zone stores a hardware-independent effect definition shared by website and future wall-panel clients. Modes are solid, fade, pulse, and blink. Effects carry two to eight colors, a 1–120 second cycle, transition softness, and independent red/green/blue balance from 50–200%. The default red balance is 140% to compensate for the installed strips appearing dimmer on red. Presets remain editable and are saved as ordinary effect values, so later gateway adapters do not depend on website-only preset names.

## RGB zone linking

`links.walls_waterfall` is part of the shared lighting document. Enabling it copies the current Walls settings to Waterfall. While enabled, a zone update from either side is mirrored atomically to both RGB zones under the same document revision. Unlinking preserves the last shared state and returns to independent updates. The link is exposed in the API snapshot for website, touchscreen, and hardware-adapter clients.

## Multi-group schedules

Lighting schedules store `zone_ids`, a unique list of one to four groups. The schedule form renders one checkbox per group, allowing Walls + Roof and Chandeliers—or any other combination—to share one on/off time. Existing `zone_id` schedules are converted in memory to a one-item `zone_ids` list and persist in the new shape on the next write. RGB settings are available when at least one selected group supports RGB; switch-only combinations accept power only.
