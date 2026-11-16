/* Presentation preferences are browser-local; live controls use the existing private APIs. */
(() => {
  const $ = id => document.getElementById(id);
  const paths = {
    home:'<path d="m3 11 9-8 9 8v10h-6v-7H9v7H3z"/>',
    bulb:'<path d="M8 15c0-2-4-3-4-7a8 8 0 0 1 16 0c0 4-4 5-4 7v2H8z"/><path d="M9 20h6m-5 3h4" fill="none" stroke="currentColor" stroke-width="2"/>',
    colors:'<circle fill="#f1448b" cx="12" cy="7" r="6"/><circle fill="#04b2d6" cx="7" cy="16" r="6"/><circle fill="#31c95a" cx="17" cy="16" r="6"/>',
    music:'<path d="M9 4v13a4 3 0 1 1-2-2.6V6l13-3v12a4 3 0 1 1-2-2.6V1z"/>',
    waterfall:'<path d="M3 3h18v5h-4v13h-4V9h-2v12H7V8H3z"/><path d="M2 23h20" stroke="currentColor" stroke-width="2"/>',
    balloon:'<path d="M3 8h5V5H3zm13 0h5V5h-5zM3 10h5v10h8V10h5v13H3zM8 11h8V8H8zM4 4l2-3 2 3zm12 0 2-3 2 3z"/>',
    schedule:'<rect x="3" y="5" width="18" height="17" rx="3" fill="none" stroke="currentColor" stroke-width="2"/><path d="M3 10h18M8 2v6m8-6v6M7 14h3m4 0h3m-10 4h3" fill="none" stroke="currentColor" stroke-width="2"/>',
    settings:'<path d="m9 1-1 4-4-1-2 4 3 3-3 3 2 4 4-1 1 5h5l1-5 4 1 3-4-3-3 3-3-3-4-4 1-1-4z"/><circle cx="11.5" cy="11.5" r="3.5" fill="var(--panel)"/>',
    activity:'<path d="M3 5h18M3 12h18M3 19h18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>',
    climate:'<path d="M10 3a3 3 0 0 1 6 0v11a5 5 0 1 1-6 0z" fill="none" stroke="currentColor" stroke-width="2"/><path d="M13 7v11m5-13h4m-4 5h3" fill="none" stroke="currentColor" stroke-width="2"/>',
    smart:'<path d="M17 6c-2-5-13-4-13 2 0 6 13 2 13 9 0 6-11 7-14 1" fill="none" stroke="currentColor" stroke-width="2.7" stroke-linecap="round"/><path d="m19 0 .7 3 3 .7-3 .8-.7 3-.8-3-3-.8 3-.7z"/>',
  };
  function icon(name){return `<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">${paths[name] || paths.home}</svg>`;}
  document.querySelectorAll('[data-icon]').forEach(el=>el.innerHTML=icon(el.dataset.icon));
  const navIcons={dashboard:'home',schedules:'schedule',plugs:'bulb',lighting:'colors',music:'music',activity:'activity',settings:'settings'};
  document.querySelectorAll('.nav').forEach(el=>{el.querySelector('span').innerHTML=icon(navIcons[el.dataset.view]);el.addEventListener('click',()=>window.scrollTo({top:0,behavior:'instant'}));});
  function open(view){document.querySelector(`.nav[data-view="${view}"]`)?.click();window.scrollTo({top:0,behavior:'instant'});}
  document.querySelectorAll('[data-open]').forEach(button=>button.addEventListener('click',()=>open(button.dataset.open)));
  const themes=['sky','blue','aurora','ice','mint','sand','rose','slate','midnight'];
  function pref(key,fallback){try{return localStorage.getItem('nethome:'+key)||fallback;}catch{return fallback;}}
  function save(key,value){try{localStorage.setItem('nethome:'+key,value);}catch{}}
  function theme(name){if(!themes.includes(name))name='sky';document.body.dataset.background=name;document.body.dataset.dark=String(['blue','aurora','slate','midnight'].includes(name));document.querySelectorAll('[data-theme]').forEach(el=>el.setAttribute('aria-pressed',String(el.dataset.theme===name)));}
  theme(pref('appearance','sky'));
  document.querySelectorAll('[data-theme]').forEach(button=>button.addEventListener('click',()=>{theme(button.dataset.theme);save('appearance',button.dataset.theme);}));
  function utility(){const design=$('utility-design').value,label=$('utility-label').value.trim()||(design==='balloon'?'Jumping Balloon':'Waterfall');document.querySelectorAll('[data-utility-name]').forEach(el=>el.textContent=label);$('utility-icon').innerHTML=icon(design==='balloon'?'balloon':'waterfall');save('utility-design',design);save('utility-label',$('utility-label').value.trim());}
  $('utility-design').value=pref('utility-design','waterfall');$('utility-label').value=pref('utility-label','');utility();$('utility-design').addEventListener('change',utility);$('utility-label').addEventListener('input',utility);
  function mirror(source,target){const from=$(source),to=$(target);if(!from||!to)return;const update=()=>to.textContent=from.textContent;new MutationObserver(update).observe(from,{childList:true,characterData:true,subtree:true});update();}
  mirror('dashboard-smart-room','header-room');mirror('dashboard-smart-humidity','header-humidity');mirror('dashboard-smart-outside','header-outside');mirror('dashboard-smart-toggle','home-smart-state');
  function clock(){const el=$('home-clock');if(el)el.textContent=new Date().toLocaleTimeString([],{hour:'numeric',minute:'2-digit'});}clock();setInterval(()=>{if(!document.hidden)clock();},60000);
  const details=document.querySelector('.climate-details');if(details)details.open=false;
  const activity=document.createElement('button');activity.type='button';activity.className='secondary';activity.textContent='View activity history';activity.addEventListener('click',()=>open('activity'));document.querySelector('#settings .page-head').append(activity);
  let snapshot=null,loading=false,loadedAt=0;
  function renderPlugs(){
    if(!snapshot)return;
    for(const target of ['lights','utility']){
      const roles=snapshot.roles.filter(r=>(target==='lights'?['roof','walls']:['utility']).includes(r.role));
      const assigned=roles.length===(target==='lights'?2:1)&&roles.every(r=>r.assigned);
      const ready=assigned&&snapshot.fresh&&roles.every(r=>r.online&&typeof r.power==='boolean');
      const label=!assigned?'Connect '+(target==='lights'?'both light plugs':'your plug'):!ready?'Unavailable · refresh to check':roles.every(r=>r.power)?'On':roles.every(r=>!r.power)?'Off':'Roof and wall differ';
      $('plug-'+target+'-state').textContent=label;$('home-'+target+'-state').textContent=label;
      document.querySelectorAll(`[data-plug="${target}"]`).forEach(b=>b.disabled=loading||!ready);
    }
  }
  async function loadPlugs(force=false){if(loading||(!force&&Date.now()-loadedAt<60000))return;loading=true;renderPlugs();try{const r=await fetch('/api/plugs',{credentials:'same-origin'});if(r.status===401){location.assign('/login');return;}const d=await r.json();if(!r.ok)throw Error(d.detail||'Could not check plugs');snapshot=d;loadedAt=Date.now();$('plugs-message').textContent='';}catch(e){$('plugs-message').textContent=e.message;}finally{loading=false;renderPlugs();}}
  document.querySelector('.nav[data-view="plugs"]').addEventListener('click',()=>loadPlugs());$('plugs-refresh').addEventListener('click',()=>loadPlugs(true));
  document.querySelectorAll('[data-plug]').forEach(button=>button.addEventListener('click',async()=>{
    if(loading||!snapshot)return;loading=true;renderPlugs();$('plugs-message').textContent='Sending and checking…';
    try{const r=await fetch('/api/plugs/command',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({target:button.dataset.plug,power:button.dataset.power==='true',revision:snapshot.revision})});const d=await r.json();if(!r.ok)throw Error(typeof d.detail==='string'?d.detail:'Could not control plug');snapshot=d.state;loadedAt=Date.now();$('plugs-message').textContent=d.message;}catch(e){$('plugs-message').textContent=e.message;loadedAt=0;snapshot=null;document.querySelectorAll('[data-plug]').forEach(b=>b.disabled=true);}finally{loading=false;renderPlugs();}
  }));
  // Browsing supports a compact list or touch-friendly grid, independent of playback.
  const musicView=document.createElement('div');musicView.className='music-view-switch';musicView.setAttribute('aria-label','Music layout');musicView.innerHTML='<button type="button" data-layout="grid">Grid</button><button type="button" data-layout="list">List</button>';document.querySelector('.music-source-tabs').append(musicView);
  function layout(value){$('music').dataset.layout=value==='list'?'list':'grid';musicView.querySelectorAll('button').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.layout===$('music').dataset.layout)));}layout(pref('music-layout','grid'));musicView.querySelectorAll('button').forEach(b=>b.addEventListener('click',()=>{layout(b.dataset.layout);save('music-layout',b.dataset.layout);}));
})();
