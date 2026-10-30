// Exercise the actual navigation functions without a browser or model calls.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../src/memsys/web/chat.js'), 'utf8');
const navigation = source.slice(source.indexOf('async function closeActive('), source.indexOf("$('new-chat').addEventListener"));

function setup({active='old', messages=[{role:'user',content:'remember this'}], failClose=false, failRead=false}={}) {
  const calls = [], notices = [], input = {value:'draft',focus(){}};
  const state = {active,messages,busy:false,retry:null};
  const context = {
    state, encodeURIComponent, $:()=>input,
    busy(value){state.busy=value;},
    async api(endpoint,body){
      calls.push({endpoint,body});
      if(endpoint==='close' && failClose) throw new Error('close rejected');
      if(endpoint.startsWith('messages') && failRead) throw new Error('missing conversation');
      return {messages:[{role:'user',content:'target history'}],retry:null};
    },
    notice(...args){notices.push(args);}, pref(){}, resize(){}, renderList(){},
    renderMessages(){}, closeSidebar(){}, scrollDown(){}
  };
  vm.createContext(context);
  vm.runInContext(navigation,context);
  return {context,state,calls,notices,input};
}

test('sidebar switch closes the conversation being left',async()=>{
  const {context,state,calls}=setup();
  await context.choose('existing');
  assert.deepEqual(calls.map(c=>c.endpoint),['messages?id=existing','close']);
  assert.equal(calls[1].body.conversation_id,'old');
  assert.equal(state.active,'existing');
  assert.equal(state.busy,false);
});
test('refreshing the same conversation does not close it',async()=>{
  const {context,calls}=setup();
  await context.choose('old');
  assert.deepEqual(calls.map(c=>c.endpoint),['messages?id=old']);
});
test('initial load has no previous conversation to close',async()=>{
  const {context,calls}=setup({active:null,messages:[]});
  await context.choose('existing');
  assert.deepEqual(calls.map(c=>c.endpoint),['messages?id=existing']);
});
test('failed close keeps the current conversation selected',async()=>{
  const {context,state,notices}=setup({failClose:true});
  await context.choose('existing');
  assert.equal(state.active,'old');
  assert.equal(state.messages[0].content,'remember this');
  assert.equal(state.busy,false);
  assert.equal(notices.at(-1)[0],'close rejected');
});
test('invalid target does not close the current conversation',async()=>{
  const {context,state,calls}=setup({failRead:true});
  await context.choose('missing');
  assert.equal(state.active,'old');
  assert.deepEqual(calls.map(c=>c.endpoint),['messages?id=missing']);
});
test('new conversation uses the same close path',async()=>{
  const {context,state,calls,input}=setup();
  await context.newChat();
  assert.deepEqual(calls.map(c=>c.endpoint),['close']);
  assert.equal(calls[0].body.conversation_id,'old');
  assert.equal(state.active,null);
  assert.equal(state.messages.length,0);
  assert.equal(input.value,'');
});
test('reply and navigation in progress prevent another switch',async()=>{
  const {context,state,calls}=setup();
  state.busy=true;
  await context.choose('existing');
  await context.newChat();
  assert.equal(calls.length,0);
  assert.equal(state.active,'old');
});
