# NetHome Web Control — web-first starter

This replaces the desktop-GUI direction with a browser-based control system while keeping the tested Python/Midea path.

## Architecture

Browser / future ChatGPT / future SMS -> FastAPI control service -> NetHome Plus/Midea cloud -> US-OSK105 -> mini-split

Schedules and activity logs are stored in local SQLite for the first milestone. The API and database layer are separated so moving the service to a 24/7 cloud host later does not require rebuilding the user interface.

## Safety state

- Read-only device status is implemented.
- AC write commands are intentionally locked.
- `NETHOME_ALLOW_WRITES` defaults to `false`.
- The write adapter intentionally raises `NotImplementedError` even if the flag is changed. We unlock it only after the AC is back online and read-only status is proven.
- NetHome Plus credentials are kept out of the browser and source code.
- On Windows, `setup_credentials.py` uses the operating system credential store via `keyring`.

## Windows setup

1. Extract the folder, for example to `C:\NetHomeWebControl`.
2. Open PowerShell in that folder.
3. Run `Set-ExecutionPolicy -Scope Process Bypass` if PowerShell blocks local scripts.
4. Run `./setup.ps1`.
5. Run `./setup_credentials.ps1` and enter the NetHome Plus email/password locally.
6. Run `./run.ps1`.
7. Open `http://127.0.0.1:8765`.

## Current endpoints

- `GET /api/health`
- `GET /api/device/devices`
- `GET /api/device/status`
- `POST /api/device/command` (locked)
- `GET /api/schedules`
- `POST /api/schedules`
- `PATCH /api/schedules/{id}`
- `DELETE /api/schedules/{id}`
- `GET /api/activity`

## ChatGPT plan

The same API will be used for ChatGPT. Before remote access we will:

1. replace the placeholder bearer token,
2. add HTTPS and user authentication,
3. separate read and write permissions,
4. add explicit confirmation rules for destructive actions,
5. deploy the service to an always-on host,
6. move schedules from local SQLite to managed Postgres or another durable cloud database.

## Next milestone

Restore Wi-Fi to the US-OSK105, then use the website's **Refresh status** button. Once read-only status works, implement and test one safe write command before enabling the rest of the controls or schedule execution.

## UI update — schedule builder

The schedule page now supports:
- Specific one-time dates
- Weekdays (Mon-Fri)
- Every day
- Weekly schedules with selectable weekday buttons
- Optional start/end date ranges for recurring schedules
- A large 12-hour digital-style time picker with AM/PM
- Edit, enable/disable, and delete actions from the schedule list

Existing schedule data is preserved. On startup the SQLite schema is upgraded in place with the new schedule fields.
