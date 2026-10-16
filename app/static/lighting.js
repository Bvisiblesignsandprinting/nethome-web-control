/* Lighting drafts never issue AC commands or report a physical light state. */
(() => {
  const $ = id => document.getElementById(id);
  const form = $('lighting-form');
  const field = name => form.elements.namedItem(name);
  let data = null, busy = false, editingId = null, previewGeneration = 0;
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const message = text => { $('lighting-state').textContent = text; };
  const EFFECT_PRESETS = {
    spectrum: {mode:'fade', colors:['#FF0000','#FF8A00','#FFF000','#00D45A','#00A7FF','#7A4DFF'], cycle_seconds:24, transition:100, red_balance:140},
    dreamy: {mode:'fade', colors:['#FF4F9A','#8B5CFF','#2CCBFF','#56E39F'], cycle_seconds:36, transition:100, red_balance:135},
    sunset: {mode:'fade', colors:['#FF1F1F','#FF7300','#FFD23F','#B33DFF'], cycle_seconds:30, transition:100, red_balance:145},
    slow_blink: {mode:'blink', colors:['#FF0000','#005CFF','#00D96F'], cycle_seconds:8, transition:0, red_balance:140},
    quick_blink: {mode:'blink', colors:['#FF1744','#FFEA00','#00E5FF'], cycle_seconds:2, transition:0, red_balance:135},
    pulse: {mode:'pulse', colors:['#7A4DFF','#00B8FF'], cycle_seconds:10, transition:100, red_balance:130},
  };

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
    const effect = desired.effect || {mode:'solid',colors:[],cycle_seconds:12,transition:100,red_balance:140,green_balance:100,blue_balance:100};
    const colors = [...effect.colors, '#FF0000','#00D45A','#00A7FF','#7A4DFF','#FFF000','#FF4F9A'].slice(0,6);
    const effectName = effect.mode === 'solid' ? 'Solid color' : effect.mode[0].toUpperCase() + effect.mode.slice(1);
    return `<form class="card lighting-zone" data-zone="${zone.id}">
      <div class="lighting-zone-head"><div><p class="eyebrow">${rgb ? 'RGB + WHITE' : 'REGULAR LIGHTS'}</p><h2>${escape(zone.name)}</h2></div><span class="lighting-badge">Not connected</span></div>
      <p class="lede">${escape(zone.description)}</p>
      <div class="lighting-swatch${rgb && effect.mode !== 'solid' ? ' effect-' + effect.mode : ''}" style="--light-color:${rgb && desired.color !== 'white' ? escape(desired.color) : '#ffffff'};--effect-colors:${rgb ? colors.join(',') : '#ffffff'};--effect-speed:${rgb ? effect.cycle_seconds : 12}s"><span>Saved preview · ${desired.power ? 'On' : 'Off'}${rgb ? ' · ' + desired.brightness + '% · ' + effectName : ''}</span></div>
      <label>Power setting<select name="power"><option value="true"${desired.power ? ' selected' : ''}>On</option><option value="false"${!desired.power ? ' selected' : ''}>Off</option></select></label>
      ${rgb ? `<div class="lighting-rgb-settings"><label>Color mode<select name="color_mode"><option value="color"${desired.color !== 'white' ? ' selected' : ''}>Color</option><option value="white"${desired.color === 'white' ? ' selected' : ''}>White</option></select></label><label>Color<input name="color" type="color" value="${desired.color === 'white' ? '#ffffff' : escape(desired.color)}"></label><label>Brightness<input name="brightness" type="range" min="0" max="100" value="${desired.brightness}"><output>${desired.brightness}%</output></label></div>` : ''}
      ${rgb ? `<details class="lighting-effects"${effect.mode !== 'solid' ? ' open' : ''}><summary>Color effects</summary><div class="lighting-effect-grid">
        <label>Preset<select name="effect_preset"><option value="custom">Custom</option><option value="spectrum">Smooth spectrum</option><option value="dreamy">Dreamy fade</option><option value="sunset">Sunset fade</option><option value="slow_blink">Slow blink</option><option value="quick_blink">Quick blink</option><option value="pulse">Gentle pulse</option></select></label>
        <label>Effect<select name="effect_mode"><option value="solid"${effect.mode==='solid'?' selected':''}>Solid color</option><option value="fade"${effect.mode==='fade'?' selected':''}>Smooth fade</option><option value="pulse"${effect.mode==='pulse'?' selected':''}>Pulse</option><option value="blink"${effect.mode==='blink'?' selected':''}>Blink</option></select></label>
        <label>Colors<select name="effect_color_count">${[2,3,4,5,6].map(n=>`<option value="${n}"${Math.max(2,effect.colors.length||6)===n?' selected':''}>${n}</option>`).join('')}</select></label>
        <label>Full cycle<input name="cycle_seconds" type="range" min="1" max="60" value="${Math.min(60,effect.cycle_seconds)}"><output>${effect.cycle_seconds}s</output></label>
        <label>Fade softness<input name="transition" type="range" min="0" max="100" value="${effect.transition}"><output>${effect.transition}%</output></label>
      </div><div class="lighting-effect-colors">${colors.map((color,i)=>`<label>Color ${i+1}<input name="effect_color_${i}" type="color" value="${color}"></label>`).join('')}</div>
      <div class="lighting-balance"><strong>Color balance</strong><p>Raise red if it looks darker during fades.</p><label>Red <input name="red_balance" type="range" min="50" max="200" value="${effect.red_balance}"><output>${effect.red_balance}%</output></label><label>Green <input name="green_balance" type="range" min="50" max="200" value="${effect.green_balance}"><output>${effect.green_balance}%</output></label><label>Blue <input name="blue_balance" type="range" min="50" max="200" value="${effect.blue_balance}"><output>${effect.blue_balance}%</output></label></div>
      <p class="lighting-actual">These settings will be shared with the wall device after its Lighting screen is installed.</p></details>` : ''}
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
        if (e.brightness) e.brightness.nextElementSibling.textContent = e.brightness.value + '%';
        for (const name of ['cycle_seconds','transition','red_balance','green_balance','blue_balance']) if (e[name]) e[name].nextElementSibling.textContent = e[name].value + (name === 'cycle_seconds' ? 's' : '%');
        if (e.effect_color_count) card.querySelectorAll('.lighting-effect-colors label').forEach((label,i)=>label.hidden=i>=Number(e.effect_color_count.value));
        if (e.color_mode) {
          e.color.disabled = e.color_mode.value === 'white';
          card.querySelector('.lighting-swatch').style.setProperty('--light-color', e.color_mode.value === 'white' ? '#ffffff' : e.color.value);
        }
        if (e.effect_mode) {
          const count=Number(e.effect_color_count.value), colors=Array.from({length:count},(_,i)=>e['effect_color_'+i].value);
          const swatch=card.querySelector('.lighting-swatch'); swatch.className='lighting-swatch'+(e.effect_mode.value==='solid'?'':' effect-'+e.effect_mode.value);
          swatch.style.setProperty('--effect-colors',colors.join(',')); swatch.style.setProperty('--effect-speed',e.cycle_seconds.value+'s');
        }
        card.querySelector('.lighting-swatch span').textContent = 'Unsaved preview · ' + (e.power.value === 'true' ? 'On' : 'Off') + (e.brightness ? ' · ' + e.brightness.value + '% · ' + (e.effect_mode.value === 'solid' ? 'Solid color' : e.effect_mode.options[e.effect_mode.selectedIndex].text) : '');
      });
      card.elements.effect_preset?.addEventListener('change', event => {
        const preset=EFFECT_PRESETS[event.target.value]; if(!preset)return; const e=card.elements;
        for(const [key,value] of Object.entries(preset)){ if(key==='colors'){value.forEach((c,i)=>e['effect_color_'+i].value=c);e.effect_color_count.value=String(value.length);}else if(e[key])e[key].value=String(value); }
        card.dispatchEvent(new Event('input',{bubbles:true}));
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
        if (e.brightness) {
          const mode=e.effect_mode.value, count=Number(e.effect_color_count.value);
          Object.assign(command, {brightness: Number(e.brightness.value), color: e.color_mode.value === 'white' ? 'white' : e.color.value,
            effect:{mode,colors:mode==='solid'?[]:Array.from({length:count},(_,i)=>e['effect_color_'+i].value),cycle_seconds:Number(e.cycle_seconds.value),transition:mode==='blink'?0:Number(e.transition.value),red_balance:Number(e.red_balance.value),green_balance:Number(e.green_balance.value),blue_balance:Number(e.blue_balance.value)}});
        }
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
