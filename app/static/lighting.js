/* Lighting drafts never issue AC commands or report a physical light state. */
(() => {
  const $ = id => document.getElementById(id);
  const form = $('lighting-form');
  const field = name => form.elements.namedItem(name);
  let data = null, busy = false, editingId = null, previewGeneration = 0;
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const message = text => { $('lighting-state').textContent = text; };

  async function api(path = '', options = {}) {
    const response = await fetch('/api/lighting' + path, {
      ...options, credentials: 'same-origin', cache: 'no-store',
      headers: {'Content-Type': 'application/json', ...options.headers},
    });
    if (response.status === 401) { location.assign('/login'); throw new Error('Please log in again.'); }
    const result = await response.json();
    if (!response.ok) {
      const error = new Error(typeof result.detail === 'string' ? result.detail : 'Check the schedule fields and try again.');
      error.status = response.status; throw error;
    }
    return result;
  }

  function zoneMarkup(zone) {
    const desired = zone.desired, rgb = zone.kind === 'rgbw';
    return `<form class="card lighting-zone" data-zone="${zone.id}">
      <div class="lighting-zone-head"><div><p class="eyebrow">${rgb ? 'RGB + WHITE' : 'REGULAR LIGHTS'}</p><h2>${escape(zone.name)}</h2></div><span class="lighting-badge">Not connected</span></div>
      <p class="lede">${escape(zone.description)}</p>
      <div class="lighting-swatch" style="--light-color:${rgb && desired.color !== 'white' ? escape(desired.color) : '#ffffff'}"><span>Saved preview · ${desired.power ? 'On' : 'Off'}${rgb ? ' · ' + desired.brightness + '%' : ''}</span></div>
      <label>Power setting<select name="power"><option value="true"${desired.power ? ' selected' : ''}>On</option><option value="false"${!desired.power ? ' selected' : ''}>Off</option></select></label>
      ${rgb ? `<div class="lighting-rgb-settings"><label>Color mode<select name="color_mode"><option value="color"${desired.color !== 'white' ? ' selected' : ''}>Color</option><option value="white"${desired.color === 'white' ? ' selected' : ''}>White</option></select></label><label>Color<input name="color" type="color" value="${desired.color === 'white' ? '#ffffff' : escape(desired.color)}"></label><label>Brightness<input name="brightness" type="range" min="0" max="100" value="${desired.brightness}"><output>${desired.brightness}%</output></label></div>` : ''}
      <p class="lighting-actual">Actual light state: unavailable until connected</p><button type="submit">Save settings</button>
    </form>`;
  }

  function formatOccurrence(item, timezone) {
    return new Intl.DateTimeFormat(undefined, {weekday: 'short', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', timeZone: timezone, timeZoneName: 'short'}).format(new Date(item.utc));
  }

  function render(savedZoneId = null) {
    const drafts = new Map([...$('lighting-zones').querySelectorAll('form[data-dirty]')]
      .filter(card => card.dataset.zone !== savedZoneId)
      .map(card => [card.dataset.zone, Object.fromEntries([...card.elements].filter(e => e.name).map(e => [e.name, e.value]))]));
    $('lighting-zones').innerHTML = data.zones.map(zoneMarkup).join('');
    for (const card of $('lighting-zones').querySelectorAll('form')) {
      card.addEventListener('input', () => {
        card.dataset.dirty = 'true';
        const e = card.elements;
        if (e.brightness) card.querySelector('output').textContent = e.brightness.value + '%';
        if (e.color_mode) {
          e.color.disabled = e.color_mode.value === 'white';
          card.querySelector('.lighting-swatch').style.setProperty('--light-color', e.color_mode.value === 'white' ? '#ffffff' : e.color.value);
        }
        card.querySelector('.lighting-swatch span').textContent = 'Unsaved preview · ' + (e.power.value === 'true' ? 'On' : 'Off') + (e.brightness ? ' · ' + e.brightness.value + '%' : '');
      });
      if (card.elements.color_mode) card.elements.color.disabled = card.elements.color_mode.value === 'white';
      const draft = drafts.get(card.dataset.zone);
      if (draft) {
        for (const [name, value] of Object.entries(draft)) card.elements.namedItem(name).value = value;
        card.dispatchEvent(new Event('input', {bubbles: true}));
      }
      card.addEventListener('submit', async event => {
        event.preventDefault();
        const e = card.elements, command = {power: e.power.value === 'true'};
        if (e.brightness) Object.assign(command, {brightness: Number(e.brightness.value), color: e.color_mode.value === 'white' ? 'white' : e.color.value});
        await mutate('/zones/' + card.dataset.zone, 'PATCH', {command});
      });
    }
    if (!field('zone_id').options.length) {
      for (const zone of data.zones) field('zone_id').add(new Option((zone.kind === 'rgbw' ? 'RGB · ' : 'Regular · ') + zone.name, zone.id));
    }
    $('lighting-schedules').replaceChildren();
    if (!data.schedules.length) {
      const empty = document.createElement('p'); empty.className = 'empty';
      empty.textContent = 'No lighting schedules yet. Add an on time and a separate off time for each group.';
      $('lighting-schedules').append(empty);
    }
    for (const schedule of data.schedules) {
      const zone = data.zones.find(z => z.id === schedule.zone_id);
      const row = document.createElement('article'); row.className = 'card lighting-schedule';
      const days = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
      const repeat = schedule.repeat === 'weekly' ? schedule.days.map(d => days[d]).join(', ') : schedule.repeat === 'one_time' ? schedule.date_local : 'Every day';
      const command = schedule.command;
      const details = [zone.name, command.power === false ? 'Off' : command.power === true ? 'On' : 'Change settings', command.color === 'white' ? 'White' : command.color, command.brightness != null ? command.brightness + '%' : null].filter(Boolean).join(' · ');
      const next = schedule.next_occurrences[0];
      row.innerHTML = `<div><h3>${escape(schedule.name)}</h3><p>${escape(details)}</p><p>${escape(repeat)} · ${escape(schedule.time_local)} · ${escape(schedule.timezone)}</p><p class="lighting-actual">${schedule.enabled ? next ? 'Planned next: ' + escape(formatOccurrence(next, schedule.timezone)) : 'No future occurrence' : 'Disabled'}</p><span class="lighting-badge">${schedule.enabled ? 'Waiting for connection' : 'Disabled'}</span></div><div class="lighting-schedule-actions"><button type="button" class="secondary" data-action="edit">Edit</button><button type="button" class="secondary" data-action="toggle">${schedule.enabled ? 'Disable' : 'Enable'}</button><button type="button" class="secondary" data-action="delete">Delete</button></div>`;
      row.querySelector('[data-action="edit"]').addEventListener('click', () => openForm(schedule));
      row.querySelector('[data-action="toggle"]').addEventListener('click', () => mutate('/schedules/' + schedule.id, 'PUT', {schedule: {...writable(schedule), enabled: !schedule.enabled}}));
      row.querySelector('[data-action="delete"]').addEventListener('click', () => {
        if (confirm('Delete lighting schedule “' + schedule.name + '”?')) mutate('/schedules/' + schedule.id, 'DELETE');
      });
      $('lighting-schedules').append(row);
    }
    $('lighting-add').disabled = false;
  }

  function writable(schedule) {
    return Object.fromEntries(['name','zone_id','command','repeat','time_local','timezone','days','date_local','enabled'].filter(k => schedule[k] != null).map(k => [k, schedule[k]]));
  }

  async function load() {
    if (busy) return;
    busy = true; $('lighting-refresh').disabled = true;
    try { data = await api(); render(); message('Settings loaded. Hardware is not connected yet.'); }
    catch (error) { message(error.message); }
    finally { busy = false; $('lighting-refresh').disabled = false; }
  }

  async function mutate(path, method, payload = {}) {
    if (busy || !data) return false;
    busy = true;
    const controls = [...$('lighting').querySelectorAll('button,input,select')].map(e => [e, e.disabled]);
    for (const [control] of controls) control.disabled = true;
    try {
      const url = method === 'DELETE' ? path + '?revision=' + data.revision : path;
      data = await api(url, {method, ...(method !== 'DELETE' ? {body: JSON.stringify({...payload, revision: data.revision})} : {})});
      render(path.startsWith('/zones/') ? path.slice('/zones/'.length) : null);
      message('Saved. This is a preview; no hardware command was sent.'); return true;
    } catch (error) {
      message(error.message);
      $('lighting-form-error').textContent = error.message; $('lighting-form-error').hidden = form.hidden;
      if (error.status === 409) { try { data = await api(); render(); } catch (_) { data = null; } }
      return false;
    } finally {
      busy = false; for (const [control, disabled] of controls) control.disabled = disabled;
      if (data) $('lighting-add').disabled = false;
    }
  }

  function updateForm() {
    const repeat = field('repeat').value;
    $('lighting-date-wrap').hidden = repeat !== 'one_time';
    field('date_local').disabled = repeat !== 'one_time'; field('date_local').required = repeat === 'one_time';
    $('lighting-days').hidden = repeat !== 'weekly';
    const rgb = data?.zones.find(z => z.id === field('zone_id').value)?.kind === 'rgbw' && field('power').value === 'true';
    $('lighting-schedule-rgb').hidden = !rgb; $('lighting-schedule-rgb').disabled = !rgb;
    field('color').disabled = !rgb || field('color_mode').value === 'white';
    $('lighting-schedule-brightness').textContent = field('brightness').value + '%';
    $('lighting-preview').hidden = true; previewGeneration++;
  }

  function openForm(schedule = null) {
    if (!data) return;
    form.reset(); editingId = schedule?.id ?? null;
    $('lighting-form-title').textContent = schedule ? 'Edit lighting schedule' : 'New lighting schedule';
    if (schedule) {
      for (const key of ['name','zone_id','repeat','time_local','timezone','date_local']) field(key).value = schedule[key] ?? '';
      field('power').value = String(schedule.command.power ?? true);
      field('enabled').checked = schedule.enabled;
      field('color_mode').value = schedule.command.color === 'white' ? 'white' : 'color';
      field('color').value = schedule.command.color === 'white' ? '#ffffff' : schedule.command.color ?? '#45a5ff';
      field('brightness').value = schedule.command.brightness ?? 80;
      for (const box of form.querySelectorAll('[name="days"]')) box.checked = schedule.days.includes(Number(box.value));
    }
    $('lighting-form-error').hidden = true; form.hidden = false; updateForm();
    form.scrollIntoView({behavior: 'smooth', block: 'start'}); field('name').focus();
  }

  function scheduleFromForm() {
    if (!form.reportValidity()) return null;
    const repeat = field('repeat').value;
    const days = repeat === 'weekly' ? [...form.querySelectorAll('[name="days"]:checked')].map(x => Number(x.value)) : [];
    if (repeat === 'weekly' && !days.length) throw new Error('Choose at least one weekday.');
    const command = {power: field('power').value === 'true'};
    if (!$('lighting-schedule-rgb').disabled) Object.assign(command, {color: field('color_mode').value === 'white' ? 'white' : field('color').value, brightness: Number(field('brightness').value)});
    return {name: field('name').value.trim(), zone_id: field('zone_id').value, command, repeat,
      time_local: field('time_local').value, timezone: field('timezone').value.trim(), days,
      ...(repeat === 'one_time' ? {date_local: field('date_local').value} : {}), enabled: field('enabled').checked};
  }

  form.addEventListener('input', updateForm);
  form.addEventListener('submit', async event => {
    event.preventDefault(); $('lighting-form-error').hidden = true;
    try {
      const schedule = scheduleFromForm(); if (!schedule) return;
      const success = await mutate('/schedules' + (editingId ? '/' + editingId : ''), editingId ? 'PUT' : 'POST', {schedule});
      if (success) { form.hidden = true; editingId = null; }
    } catch (error) { $('lighting-form-error').textContent = error.message; $('lighting-form-error').hidden = false; }
  });
  $('lighting-preview-button').addEventListener('click', async () => {
    const generation = ++previewGeneration;
    $('lighting-form-error').hidden = true;
    try {
      const schedule = scheduleFromForm(); if (!schedule) return;
      const result = await api('/schedules/preview', {method: 'POST', body: JSON.stringify(schedule)});
      if (generation !== previewGeneration || form.hidden) return;
      $('lighting-preview').textContent = result.occurrences.length ? 'Planned timing: ' + result.occurrences.map(x => formatOccurrence(x, schedule.timezone)).join(' · ') + '. Waiting for hardware connection.' : 'No future occurrence. Choose a future date or time.';
      $('lighting-preview').hidden = false;
    } catch (error) { if (generation === previewGeneration) { $('lighting-form-error').textContent = error.message; $('lighting-form-error').hidden = false; } }
  });
  $('lighting-cancel').addEventListener('click', () => { form.hidden = true; editingId = null; previewGeneration++; });
  $('lighting-add').addEventListener('click', () => openForm());
  $('lighting-refresh').addEventListener('click', load);
  document.querySelector('[data-view="lighting"]').addEventListener('click', load);
})();
