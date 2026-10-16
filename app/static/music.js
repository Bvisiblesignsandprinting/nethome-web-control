/* Music is isolated from AC control and never sends appliance commands. */
(() => {
  const $ = id => document.getElementById(id);
  const audio = $('music-audio');
  let tracks = [], nextOffset = null, currentId = null, playbackGeneration = 0;
  let loading = false, uploading = false;
  const state = message => { $('music-state').textContent = message; };

  async function api(path, options = {}) {
    const response = await fetch('/api/music' + path, {
      ...options, credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', ...options.headers},
    });
    if (response.status === 401) { location.assign('/login'); throw new Error('Please log in again.'); }
    const body = await response.json();
    if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : 'Music request failed.');
    return body;
  }

  function navigation() {
    const index = tracks.findIndex(track => track.id === currentId);
    $('music-previous').disabled = index <= 0;
    $('music-next').disabled = index < 0 || index >= tracks.length - 1;
  }

  function render() {
    const library = $('music-library');
    library.replaceChildren();
    if (!tracks.length) {
      const message = document.createElement('p');
      message.textContent = 'No music uploaded yet.';
      library.append(message);
    }
    for (const track of tracks) {
      const row = document.createElement('div'); row.className = 'music-track';
      const title = document.createElement('span'); title.textContent = track.title;
      const play = document.createElement('button'); play.type = 'button'; play.textContent = 'Play';
      play.setAttribute('aria-label', 'Play ' + track.title);
      play.addEventListener('click', () => playTrack(track));
      const remove = document.createElement('button'); remove.type = 'button';
      remove.className = 'secondary'; remove.textContent = 'Delete';
      remove.setAttribute('aria-label', 'Delete ' + track.title);
      remove.addEventListener('click', () => deleteTrack(track, remove));
      row.append(title, play, remove); library.append(row);
    }
    $('music-load-more').hidden = nextOffset === null;
    navigation();
  }

  async function load(append = false) {
    if (loading || uploading) return;
    loading = true;
    $('music-refresh').disabled = $('music-load-more').disabled = true;
    try {
      const status = await api('/status');
      $('music-files').disabled = $('music-upload-button').disabled = !status.storage_ready;
      if (!status.storage_ready) {
        state('Music storage is not connected yet. Uploads will be available after setup.');
        return;
      }
      const result = await api('/tracks?offset=' + (append ? nextOffset : 0));
      tracks = append ? [...tracks, ...result.tracks] : result.tracks;
      nextOffset = result.next_offset;
      render(); state(tracks.length + ' song' + (tracks.length === 1 ? '' : 's') + ' loaded.');
    } catch (error) { state(error.message); }
    finally { loading = false; $('music-refresh').disabled = $('music-load-more').disabled = false; }
  }

  async function playTrack(track) {
    const generation = ++playbackGeneration;
    audio.pause();
    state('Loading ' + track.title + '…');
    try {
      const result = await api('/tracks/' + encodeURIComponent(track.id) + '/playback', {method: 'POST'});
      if (generation !== playbackGeneration) return;
      currentId = track.id; audio.src = result.url;
      $('music-now-playing').textContent = track.title;
      navigation();
      await audio.play();
      state('Playing ' + track.title + ' in this browser.');
    } catch (error) {
      if (generation === playbackGeneration) state(error.name === 'NotAllowedError' ? 'Press Play on the audio player to start.' : error.message);
    }
  }

  async function deleteTrack(track, button) {
    if (!confirm('Delete “' + track.title + '” from your music library?')) return;
    button.disabled = true;
    try {
      await api('/tracks/' + encodeURIComponent(track.id), {method: 'DELETE'});
      if (currentId === track.id) {
        ++playbackGeneration; audio.pause(); audio.removeAttribute('src'); audio.load(); currentId = null;
        $('music-now-playing').textContent = 'Choose a song';
      }
      tracks = tracks.filter(item => item.id !== track.id);
      // Reset pagination after mutation so a shifted offset cannot skip a track.
      await load(); state('Deleted ' + track.title + '.');
    } catch (error) { state(error.message); button.disabled = false; }
  }

  function uploadFile(url, file, base, total) {
    return new Promise((resolve, reject) => {
      const request = new XMLHttpRequest();
      request.open('PUT', url); request.timeout = 10 * 60 * 1000;
      request.setRequestHeader('Content-Type', 'audio/mpeg');
      request.setRequestHeader('x-upsert', 'false');
      request.upload.onprogress = event => {
        if (event.lengthComputable) $('music-progress').value = ((base + event.loaded / event.total) / total) * 100;
      };
      request.onload = () => request.status >= 200 && request.status < 300 ? resolve() : reject(new Error('Upload failed for ' + file.name + '. Please retry.'));
      request.onerror = request.ontimeout = () => reject(new Error('Upload interrupted. Please retry.'));
      request.send(file);
    });
  }

  $('music-upload').addEventListener('submit', async event => {
    event.preventDefault(); if (uploading) return;
    const files = [...$('music-files').files]; if (!files.length) return;
    if (files.some(file => !file.name.toLowerCase().endsWith('.mp3') || file.size === 0 || file.size > 25 * 1024 * 1024)) {
      state('Choose nonempty MP3 files no larger than 25 MB each.'); return;
    }
    uploading = true;
    $('music-upload-button').disabled = $('music-files').disabled = true;
    $('music-progress').hidden = false; $('music-progress').value = 0;
    let completed = 0, failure = '';
    try {
      for (const file of files) {
        state('Uploading ' + file.name + '…');
        const ticket = await api('/uploads', {method: 'POST', body: JSON.stringify({filename: file.name, size: file.size})});
        await uploadFile(ticket.upload_url, file, completed, files.length); completed++;
      }
      $('music-files').value = '';
    } catch (error) { failure = error.message; }
    finally {
      uploading = false; $('music-upload-button').disabled = $('music-files').disabled = false;
      $('music-progress').hidden = true;
      await load();
      state((completed ? completed + ' file(s) uploaded. ' : '') + failure);
    }
  });

  $('music-previous').addEventListener('click', () => {
    const index = tracks.findIndex(track => track.id === currentId); if (index > 0) playTrack(tracks[index - 1]);
  });
  const next = () => {
    const index = tracks.findIndex(track => track.id === currentId); if (index >= 0 && index + 1 < tracks.length) playTrack(tracks[index + 1]);
  };
  $('music-next').addEventListener('click', next); audio.addEventListener('ended', next);
  audio.addEventListener('error', () => state('Unable to play this file. Check that it is a valid MP3, or select Play again to refresh its link.'));
  $('music-refresh').addEventListener('click', () => load());
  $('music-load-more').addEventListener('click', () => load(true));
  document.querySelector('[data-view="music"]').addEventListener('click', () => load());
})();
