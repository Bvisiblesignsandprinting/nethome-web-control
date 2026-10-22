const fs=require('fs'),vm=require('vm'),assert=require('assert');
class Element{
 constructor(id=''){this.id=id;this.listeners={};this.children=[];this.value='';this.files=[];this.hidden=false;this.disabled=false;this.textContent='';this.attrs={};this.classList={add:()=>{}};}
 addEventListener(k,fn){(this.listeners[k]??=[]).push(fn)}
 async emit(k){for(const fn of this.listeners[k]??[])await fn({preventDefault(){}});await flush()}
 append(...items){this.children.push(...items)}
 replaceChildren(...items){this.children=[...items]}
 setAttribute(k,v){this.attrs[k]=v}
 removeAttribute(k){if(k==='src')this.src='';delete this.attrs[k]}
 pause(){this.paused=true}load(){}async play(){this.paused=false}
}
const elements={};const el=id=>elements[id]??=new Element(id);
const rows=[{id:'y24-1',name:'General',title:'אלגעמיינע'},{id:'y24b-37',name:'Lebedig',title:'לעבעדיג'}];
const calls=[];let held=null,delayPlayback=false;
const timers=new Map();let timerId=0;
async function flush(){for(let i=0;i<8;i++)await Promise.resolve()}
const ctx={document:{getElementById:el,createElement:()=>new Element(),querySelector:()=>el('music-menu')},location:{assign:()=>{}},confirm:()=>false,XMLHttpRequest:function(){},console,
 setTimeout(fn,ms){timers.set(++timerId,{fn,ms});return timerId},clearTimeout(id){timers.delete(id)},
 async fetch(url,opt){calls.push(url);let body={};if(url.endsWith('/radio/stations'))body={stations:rows};
 else if(url.includes('/radio/stations/')&&url.endsWith('/playback')){
  const id=url.split('/').at(-2);body={url:id==='y24-1'?'https://music.y24.app/1':'https://music2.y24.app/37'};
  if(delayPlayback)await new Promise(r=>held=r);
 }else if(url.endsWith('/status'))body={storage_ready:true};else if(url.includes('/tracks?'))body={tracks:[],next_offset:null};
 return {status:200,ok:true,json:async()=>body};
 }};
vm.runInNewContext(fs.readFileSync(process.argv[2],'utf8'),ctx);
(async()=>{
 await el('music-source-radio').emit('click');assert.equal(el('music-radio-pane').hidden,false);assert.equal(el('music-upload').hidden,true);assert.equal(el('music-radio-stations').children.length,2);
 await el('music-radio-stations').children[0].children[2].emit('click');assert.equal(el('music-audio').src,'https://music.y24.app/1');assert.equal(el('music-now-playing').textContent,'General');
 await el('music-next').emit('click');assert.equal(el('music-audio').src,'https://music2.y24.app/37');
 await el('music-previous').emit('click');assert.equal(el('music-audio').src,'https://music.y24.app/1');
 await el('music-audio').emit('error');assert.equal(timers.size,1);
 await el('music-radio-stop').emit('click');assert.equal(timers.size,0);assert.equal(el('music-audio').src,'');
 delayPlayback=true;const pending=el('music-radio-stations').children[0].children[2].emit('click');await flush();
 await el('music-radio-stop').emit('click');held();await pending;assert.equal(el('music-audio').src,'');
 el('music-radio-search').value='Lebedig';await el('music-radio-search').emit('input');assert.equal(el('music-radio-stations').children.length,1);
 await el('music-source-library').emit('click');assert.equal(el('music-radio-pane').hidden,true);assert.equal(el('music-upload').hidden,false);
 assert(!calls.some(u=>u.includes('/device')||u.includes('/command')));
 console.log('PASS: radio selection, source tabs, next/previous, interrupted-stream retry cancellation, stop and stale-play protection; no AC requests.');
})().catch(e=>{console.error(e);process.exitCode=1});
