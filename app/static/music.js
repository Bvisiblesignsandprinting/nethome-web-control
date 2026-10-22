/* Music is isolated from AC control and never sends appliance commands. */
(() => {
  const $ = id => document.getElementById(id);
  const audio = $('music-audio');
  let tracks = [], nextOffset = null, currentId = null, playbackGeneration = 0;
  let loading = false, uploading = false;
  let radioStations = [], currentRadio = null, radioLoading = false, radioVisible = false;
  let radioRetryTimer = null, radioRetries = 0;
  const cancelRadioRetry = () => { clearTimeout(radioRetryTimer); radioRetryTimer = null; };
  const state = message => { $('music-state').textContent = message; };
  const trackPath = id => id.split('/').map(encodeURIComponent).join('/');
  const icon = (name) => {
    const paths = name === 'play' ? '<path d="M8 5v14l11-7z"/>' :
      name === 'trash' ? '<path d="M4 7h16M9 7V4h6v3m-8 0 1 13h8l1-13M10 11v5m4-5v5"/>' :
      '<path d="M3 6h7l2 2h9v11H3z"/>';
    return '<svg viewBox="0 0 24 24" aria-hidden="true">' + paths + '</svg>';
  };

  async function api(path, options = {}) {
    const response = await fetch('/api/music' + path, {
      ...options, credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', ...options.headers},
    });
    if (response.status === 401) { location.assign('/login'); throw new Error('Please log in again.'); }
    if (response.redirected && new URL(response.url, location.origin).pathname === '/login') {
      location.assign('/login'); throw new Error('Please log in again.');
    }
    if (!response.headers.get('content-type')?.includes('application/json')) {
      throw new Error('Music service is temporarily unavailable. Please refresh and try again.');
    }
    const body = await response.json();
    if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : 'Music request failed.');
    return body;
  }

  function navigation() {
    const queue = currentRadio ? radioStations : tracks;
    const index = queue.findIndex(track => track.id === (currentRadio || currentId));
    $('music-previous').disabled = index <= 0;
    $('music-next').disabled = index < 0 || index >= queue.length - 1;
  }

  function render() {
    const library = $('music-library');
    library.replaceChildren();
    const query = $('music-search').value.trim().toLowerCase();
    const visible = query ? tracks.filter(track => (track.title + ' ' + track.folder).toLowerCase().includes(query)) : tracks;
    if (!visible.length) {
      const message = document.createElement('p');
      message.className = 'music-empty';
      message.textContent = tracks.length ? 'No songs match your search.' : 'No music uploaded yet. Choose songs or a complete folder above.';
      library.append(message);
    }
    for (const track of visible) {
      const row = document.createElement('article'); row.className = 'music-track';
      if (track.id === currentId) row.classList.add('is-playing');
      const art = document.createElement('div'); art.className = 'music-track-art'; art.innerHTML = icon('folder');
      const copy = document.createElement('div'); copy.className = 'music-track-copy';
      const title = document.createElement('strong'); title.textContent = track.title;
      const folder = document.createElement('span'); folder.textContent = track.folder === 'Unsorted' ? 'Unsorted songs' : track.folder.replaceAll('/', ' · ');
      copy.append(title, folder);
      const play = document.createElement('button'); play.type = 'button'; play.className = 'music-row-button'; play.innerHTML = icon('play') + '<span>Play</span>';
      play.setAttribute('aria-label', 'Play ' + track.title);
      play.addEventListener('click', () => playTrack(track));
      const remove = document.createElement('button'); remove.type = 'button';
      remove.className = 'secondary music-row-button'; remove.innerHTML = icon('trash') + '<span>Delete</span>';
      remove.setAttribute('aria-label', 'Delete ' + track.title);
      remove.addEventListener('click', () => deleteTrack(track, remove));
      row.append(art, copy, play, remove); library.append(row);
    }
    $('music-load-more').hidden = radioVisible || nextOffset === null;
    navigation();
  }

  async function load(append = false) {
    if (loading || uploading) return;
    loading = true;
    $('music-refresh').disabled = $('music-load-more').disabled = true;
    try {
      const status = await api('/status');
      $('music-files').disabled = $('music-folder').disabled = !status.storage_ready;
      $('music-upload-button').disabled = !status.storage_ready || !selectedFiles().length;
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
    cancelRadioRetry(); currentRadio = null; radioRetries = 0; renderRadio();
    const generation = ++playbackGeneration;
    audio.pause();
    state('Loading ' + track.title + '…');
    try {
      const result = await api('/tracks/' + trackPath(track.id) + '/playback', {method: 'POST'});
      if (generation !== playbackGeneration) return;
      currentId = track.id; audio.src = result.url;
      $('music-now-playing').textContent = track.title;
      $('music-now-folder').textContent = track.folder === 'Unsorted' ? 'Unsorted songs' : track.folder.replaceAll('/', ' · ');
      render();
      navigation();
      await audio.play();
      state('Playing ' + track.title + ' in this browser.');
    } catch (error) {
      if (generation === playbackGeneration) state(error.name === 'NotAllowedError' ? 'Press Play on the audio player to start.' : error.message);
    }
  }

  function renderRadio() {
    const list = $('music-radio-stations'); list.replaceChildren();
    const query = $('music-radio-search').value.trim().toLowerCase();
    const visible = radioStations.filter(row => (row.name + ' ' + row.title).toLowerCase().includes(query));
    if (!visible.length) {
      const empty = document.createElement('p'); empty.className = 'music-empty';
      empty.textContent = radioLoading ? 'Loading stations…' : radioStations.length ? 'No stations match your search.' : 'Music could not be loaded. Select Yiddish24 to retry.';
      list.append(empty);
    }
    for (const row of visible) {
      const item = document.createElement('article'); item.className = 'music-track music-radio-row';
      if (row.id === currentRadio) item.classList.add('is-playing');
      const art = document.createElement('div'); art.className = 'music-track-art'; art.textContent = 'Y24';
      const copy = document.createElement('div'); copy.className = 'music-track-copy';
      const name = document.createElement('strong'); name.textContent = row.name;
      const title = document.createElement('span'); title.textContent = row.title; title.className = 'music-radio-title';
      const live = document.createElement('span'); live.textContent = 'LIVE'; live.className = 'music-radio-live'; copy.append(name,title,live);
      const button = document.createElement('button'); button.type = 'button'; button.className = 'music-row-button'; button.innerHTML = icon('play') + '<span>Listen</span>';
      button.setAttribute('aria-label','Listen to ' + row.name); button.addEventListener('click',() => playRadio(row));
      item.append(art,copy,button); list.append(item);
    }
  }

  function validStation(row) {
    if (!row || typeof row.id !== 'string' || typeof row.name !== 'string' || typeof row.title !== 'string') return false;
    try {
      const url = new URL(row.url);
      return url.protocol === 'https:' && ['music.y24.app','music2.y24.app'].includes(url.hostname) && /^\/\d+$/.test(url.pathname) && !url.port && !url.username && !url.password && !url.search && !url.hash;
    } catch { return false; }
  }

  async function loadRadio() {
    if (radioLoading) return;
    radioLoading = true; renderRadio();
    try {
      // This catalog contains only public provider streams. Serve it with the
      // player assets so listing and playing them needs no extra API roundtrip.
      const response = await fetch('/static/yiddish24-stations.json', {cache:'no-cache', credentials:'same-origin'});
      if (!response.ok || !response.headers.get('content-type')?.includes('application/json')) throw new Error('Yiddish24 music could not be loaded. Select Yiddish24 to retry.');
      const rows = await response.json();
      if (!Array.isArray(rows) || !rows.length || !rows.every(validStation)) throw new Error('Yiddish24 music list is unavailable. Please refresh and try again.');
      radioStations = rows;
      state(rows.length + ' Yiddish24 music streams loaded.');
    }
    catch (error) { state(error.message); }
    finally { radioLoading = false; renderRadio(); navigation(); }
  }

  function sourceRadio(enabled) {
    radioVisible = enabled;
    $('music-radio-pane').hidden = !enabled;
    for (const id of ['music-upload','music-files-heading','music-library','music-load-more']) $(id).hidden = enabled || (id === 'music-load-more' && nextOffset === null);
    $('music-source-radio').setAttribute('aria-pressed',String(enabled));
    $('music-source-library').setAttribute('aria-pressed',String(!enabled));
    if (enabled) loadRadio(); else load();
  }

  async function playRadio(station, retry = false) {
    cancelRadioRetry(); if (!retry) radioRetries = 0;
    const generation = ++playbackGeneration;
    currentRadio = station.id; currentId = null; audio.pause();
    state('Connecting to ' + station.name + '…');
    try {
      if (!validStation(station)) throw new Error('This music stream is unavailable.');
      audio.src = station.url;
      $('music-now-playing').textContent = station.name;
      $('music-now-folder').textContent = 'Yiddish24 · Live music';
      render(); renderRadio(); navigation();
      await audio.play();
      if (generation === playbackGeneration) state('Playing ' + station.name + ' · Yiddish24 live.');
    } catch (error) {
      if (generation === playbackGeneration) state(error.name === 'NotAllowedError' ? 'Press Play on the audio player to listen.' : error.message);
    }
  }

  function stopRadio() {
    if (!currentRadio) return;
    cancelRadioRetry(); ++playbackGeneration; currentRadio = null; radioRetries = 0;
    audio.pause(); audio.removeAttribute('src'); audio.load();
    $('music-now-playing').textContent = 'Choose music'; $('music-now-folder').textContent = 'Songs and live music';
    render(); renderRadio(); navigation(); state('Radio stopped.');
  }

  function reconnectRadio() {
    const station = radioStations.find(row => row.id === currentRadio);
    if (!station || radioRetryTimer) return;
    if (radioRetries >= 3) { state('Music stream interrupted. Select it again to reconnect.'); return; }
    const generation = playbackGeneration, id = currentRadio;
    const delay = [3000,10000,30000][radioRetries++]; state('Music stream interrupted. Reconnecting…');
    radioRetryTimer = setTimeout(() => {
      radioRetryTimer = null;
      if (generation === playbackGeneration && id === currentRadio) playRadio(station,true);
    },delay);
  }

  $('music-source-radio').addEventListener('click',() => sourceRadio(true));
  $('music-source-library').addEventListener('click',() => sourceRadio(false));
  $('music-radio-search').addEventListener('input',renderRadio);
  $('music-radio-stop').addEventListener('click',stopRadio);

  async function deleteTrack(track, button) {
    if (!confirm('Delete “' + track.title + '” from your music library?')) return;
    button.disabled = true;
    try {
      await api('/tracks/' + trackPath(track.id), {method: 'DELETE'});
      if (currentId === track.id) {
        ++playbackGeneration; audio.pause(); audio.removeAttribute('src'); audio.load(); currentId = null;
        $('music-now-playing').textContent = 'Choose a song';
        $('music-now-folder').textContent = 'Select a track from your library';
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

  const selectedFiles = () => [...$('music-files').files, ...$('music-folder').files];
  function updateSelection() {
    const files = selectedFiles();
    const folderFiles = [...$('music-folder').files];
    $('music-selection').textContent = files.length ? files.length + ' song' + (files.length === 1 ? '' : 's') + (folderFiles.length ? ' from folder' : '') + ' selected' : 'Nothing selected';
    $('music-upload-button').disabled = uploading || !files.length || $('music-files').disabled;
  }

  $('music-upload').addEventListener('submit', async event => {
    event.preventDefault(); if (uploading) return;
    const files = selectedFiles(); if (!files.length) return;
    if (files.some(file => !file.name.toLowerCase().endsWith('.mp3') || file.size === 0 || file.size > 25 * 1024 * 1024)) {
      state('Choose nonempty MP3 files no larger than 25 MB each.'); return;
    }
    uploading = true;
    $('music-upload-button').disabled = $('music-files').disabled = $('music-folder').disabled = true;
    $('music-progress').hidden = false; $('music-progress').value = 0;
    let completed = 0, failure = '';
    try {
      for (const file of files) {
        state('Uploading ' + file.name + '…');
        const relativePath = file.webkitRelativePath || null;
        const ticket = await api('/uploads', {method: 'POST', body: JSON.stringify({filename: file.name, size: file.size, relative_path: relativePath})});
        await uploadFile(ticket.upload_url, file, completed, files.length); completed++;
      }
      $('music-files').value = '';
      $('music-folder').value = '';
    } catch (error) { failure = error.message; }
    finally {
      uploading = false; $('music-files').disabled = $('music-folder').disabled = false;
      $('music-progress').hidden = true;
      updateSelection();
      await load();
      state((completed ? completed + ' file(s) uploaded. ' : '') + failure);
    }
  });

  $('music-previous').addEventListener('click', () => {
    const queue = currentRadio ? radioStations : tracks;
    const index = queue.findIndex(track => track.id === (currentRadio || currentId));
    if (index > 0) currentRadio ? playRadio(queue[index-1]) : playTrack(queue[index-1]);
  });
  const next = () => {
    const queue = currentRadio ? radioStations : tracks;
    const index = queue.findIndex(track => track.id === (currentRadio || currentId));
    if (index >= 0 && index + 1 < queue.length) currentRadio ? playRadio(queue[index+1]) : playTrack(queue[index+1]);
  };
  $('music-next').addEventListener('click', next);
  audio.addEventListener('ended', () => currentRadio ? reconnectRadio() : next());
  audio.addEventListener('error', () => currentRadio ? reconnectRadio() : state('Unable to play this file. Check that it is a valid MP3, or select Play again to refresh its link.'));
  $('music-refresh').addEventListener('click', () => radioVisible ? loadRadio() : load());
  $('music-files').addEventListener('change', () => { $('music-folder').value = ''; updateSelection(); });
  $('music-folder').addEventListener('change', () => { $('music-files').value = ''; updateSelection(); });
  $('music-search').addEventListener('input', render);
  $('music-load-more').addEventListener('click', () => load(true));
  document.querySelector('[data-view="music"]').addEventListener('click', () => load());
})();
