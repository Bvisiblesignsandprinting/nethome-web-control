# Proposed update: lighting controls and schedules before hardware connection

Add a Lighting tab to the existing NetHome website with two RGB groups (Walls and Waterfall), regular Walls + Roof, and Chandeliers. RGB controls save power, color/white and brightness; regular lights save on/off only. Schedule create/edit/enable/disable/delete supports daily, selected weekdays and one-time timing, with the next three planned occurrences.

All changes currently save previews. Hardware state stays unknown, every zone says Not connected, and schedules say Waiting for connection. This update contains no physical lighting delivery or AC-queue commands. Roof remains grouped with the regular walls. Existing music playback, AC control and the panel firmware manifest are preserved.

## Validation completed

- Complete Python suite: 61 passed.
- JavaScript syntax check: passed.
- UI script exercised through a DOM emulator against the real API/SQLite test server: RGB save/reload; retention of other-zone drafts; weekly timing preview; schedule create/edit/disable/delete; regular-light capabilities; no AC requests or script errors.
- Revision conflicts, concurrent updates, input validation, authentication and DST timing covered by tests.
- No schema migration, new credentials or dependency changes are needed.

The temporary Google Voice bridge has also been retired now that direct Twilio SMS is connected. Its settings card, configuration endpoints, generated Apps Script, and inbound webhook are removed. The Twilio webhook and saved SMS configuration remain unchanged.

Hardware connection and activation remain later work. Bluetooth audio from the panel is a separate implementation/testing step.
