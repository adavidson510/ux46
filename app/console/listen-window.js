/* This window hosts only the reader. It never boots a conversation, attaches
   a native worker, or reads/writes a shared desktop arrangement. */
'use strict';
const LISTEN_WINDOW = true, CONTENT_POPOUT = false;
const state = {csrf:'',detail:null,room:'',agent:'',voice:{preferred:''}};
let DEFAULT_AGENT = 'local';
const agentId = () => state.agent || DEFAULT_AGENT;
const agentPath = (agent,path) => agent === DEFAULT_AGENT ? path : '/api/agents/'+encodeURIComponent(agent)+path;
const commandTurnRunning = room => room?.native?.active_run === true || Boolean(room?.native?.active_turn && room.native.active_turn !== room.native_terminal?.turn_id);
function clearSpeech() {}
function replyIcon() {const icon=document.createElement('span');icon.textContent='◖';icon.setAttribute('aria-hidden','true');return icon;}
function flash(message) {document.querySelector('#listenWindowNotice').textContent=message;}
async function api(path,options={}) {
  const response=await fetch(path,{method:options.method||'GET',credentials:'same-origin',cache:'no-store',
    headers:options.body?{'Content-Type':'application/json','X-Atlas-CSRF':state.csrf}:{},body:options.body?JSON.stringify(options.body):undefined});
  const result=await response.json();if(!response.ok)throw Error(result.message||'Could not read this conversation');return result;
}
