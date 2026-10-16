# NetHome music library

This first stage adds MP3 uploads and browser playback to the existing dashboard.
It does not enable Bluetooth pairing or audio playback on the installed panel.
No AC commands, schedules, Smart Room logic, OTA manifest, or panel UI are changed.

## Deployment setup

1. Use the existing NetHome project `nznkspwklxeclbgsulox`. Do not substitute a
   B Visible Operations or store database. The current connected Supabase accounts
   do not list this project, so live Storage setup remains unverified.
2. Configure server-only environment variables through the deployment settings:
   `NETHOME_MUSIC_STORAGE_URL=https://nznkspwklxeclbgsulox.supabase.co` and
   `NETHOME_MUSIC_STORAGE_KEY` (the project's service-role JWT). Never paste the
   key into source, browser JavaScript, logs, or a handoff. Optional bucket setting:
   `NETHOME_MUSIC_BUCKET` defaults to `nethome-music`.
3. With those variables configured locally, run `python scripts/setup_music_storage.py`.
   It creates a private bucket restricted to `audio/mpeg` with a 25 MiB file limit.
   Existing buckets are verified without modifying them. Do not add public object
   policies for this bucket. Upload URLs authorize only individual random paths.
4. Inspect any pre-existing `storage.objects` policies: ensure anonymous users
   cannot insert, update, list, read, or delete objects in this bucket through broad
   policies. Keep the project-wide Storage upload limit at least 25 MiB.
5. Test a deployment using isolated preview credentials. Current preview settings
   share DATABASE_URL and AC write configuration with production, and application
   startup invokes the pre-existing schedule profile. Do not start a preview against
   production data without checking that behavior first.
   Automatic deployment of `feature/music-library` is disabled in `vercel.json`
   until isolated preview credentials and storage are configured.
6. After review, merge the PR with the owner's approval; `main` auto-deploys.

## Flow and acceptance

1. Log in using the existing dashboard login and open Music.
2. Upload a valid MP3. The browser sends bytes directly to the private bucket using
   a short-lived upload ticket, avoiding the Vercel function request-body limit.
   MIME and extension checks are format filters, not a full MP3 decoder validation.
3. Reload the page and confirm the track persists. Duplicate names receive unique
   object paths. Filenames are normalized to portable ASCII track names.
4. Play/pause/seek/change volume using the audio player; select another song or use
   Previous/Next. At the end of a song, the next loaded song plays. Load more retrieves
   another 100 records. Browser autoplay restrictions may require pressing Play.
5. Playback URLs expire after an hour; selecting Play again refreshes the URL.
   These URLs are bearer capabilities and must not be shared or logged.
6. Delete a track and confirm it is removed after refresh. Deletion stops local
   playback. Already downloaded bytes cannot be recalled from other clients.
7. Confirm logged-out clients cannot access any music API. Confirm AC dashboard,
   schedules, SMS, and Smart Room retain their current behavior.

## Panel work still required

Retrieve the private v1.4.7 build source package. It is absent from this repository.
Do not replace the firmware baseline with a newly invented project.

Prototype HTTP MP3 decoding plus A2DP source playback, measure free heap and UI
latency during Wi-Fi and AC polling, handle speaker disconnect/reconnect, and add
authenticated commands and real panel playback acknowledgements. Only then expose
panel playback controls on the website. Bluetooth capability is established; usable
music playback on this board is not yet established.

The same firmware work must address the requested duplicate controls entry, fan
speed selector, and distinct Cool/Heat/Dry/Fan/Auto/Off colors. Keep the centered
thermostat layout, Wi-Fi profiles, lock behavior, and asynchronous network queue.
Compile using PlatformIO and test the physical panel before any OTA publication.
