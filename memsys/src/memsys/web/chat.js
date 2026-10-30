'use strict';
const $ = id => document.getElementById(id);
const icons = {
  logo:'<path d="M7 6 2 18M15 3 8 21M22 6 17 18"/>',
  plus:'<path d="M12 5v14M5 12h14"/>', search:'<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 4 4"/>',
  panel:'<rect x="3" y="4" width="18" height="16" rx="3"/><path d="M9 4v16"/>', arrow:'<path d="M12 19V5m-6 6 6-6 6 6"/>',
  close:'<path d="m6 6 12 12M6 18 18 6"/>', copy:'<rect x="8" y="8" width="12" height="13" rx="2"/><path d="M16 8V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h3"/>',
  bookmark:'<path d="M6 21V5a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v16l-6-4-6 4Z"/>',
  settings:'<path d="M4 7h16M4 17h16"/><circle cx="9" cy="7" r="3" fill="var(--sidebar)"/><circle cx="15" cy="17" r="3" fill="var(--sidebar)"/>',
  rename:'<path d="m15 4 5 5M5 19l4-1L20 7a2 2 0 0 0-3-3L6 15l-1 4Z"/>', archive:'<rect x="3" y="3" width="18" height="5" rx="1"/><path d="M5 8v12h14V8M10 12h4"/>'
};
function icon(name) { return '<svg viewBox="0 0 24 24" aria-hidden="true">' + icons[name] + '</svg>'; }
function el(tag, className, text) { const node = document.createElement(tag); if (className) node.className = className; if (text !== undefined) node.textContent = text; return node; }
function pref(key, value) { try { if (value !== undefined) localStorage.setItem('memsys.' + key, value); return localStorage.getItem('memsys.' + key); } catch { return null; } }
const state = { ready:false, busy:false, conversations:[], messages:[], active:pref('active') || null, retry:null, rename:null };
for (const [id,name] of [['brand-icon','logo'],['welcome-icon','logo'],['new-icon','plus'],['search-icon','search'],['menu','panel'],['collapse','panel'],['settings-icon','settings'],['send','arrow']]) $(id).innerHTML = icon(name);
document.querySelectorAll('.close-dialog').forEach(node => node.innerHTML = icon('close'));

async function api(path, body) {
  const response = await fetch('/chat-api/' + path, {method:body === undefined ? 'GET' : 'POST', credentials:'same-origin',
    headers:body === undefined ? {} : {'Content-Type':'application/json'}, body:body === undefined ? undefined : JSON.stringify(body)});
  const result = await response.json();
  if (!response.ok) { const error = new Error(result.error || '连接暂时不可用。'); error.status = response.status; throw error; }
  return result;
}
function toast(text) { $('toast').textContent = text; $('toast').hidden = false; clearTimeout(toast.timer); toast.timer = setTimeout(() => $('toast').hidden = true, 2400); }
async function copy(text) { try { await navigator.clipboard.writeText(text); toast('已复制'); } catch { toast('无法访问剪贴板，请手动选择文本复制。'); } }
function notice(text, error=false, retry=false) {
  $('notice').replaceChildren(); $('notice').hidden = !text; $('notice').className = 'notice' + (error ? ' error' : '');
  if (!text) return;
  if (!error && state.busy) $('notice').append(el('span','spinner'));
  $('notice').append(el('span','',text));
  if (retry) { const button = el('button','retry-button','重试'); button.type = 'button'; button.addEventListener('click', () => submit(true)); $('notice').append(button); }
}
function busy(value) {
  state.busy = value; $('message').disabled = value; $('new-chat').disabled = value;
  document.querySelectorAll('.history button').forEach(button => button.disabled = value);
  $('composer').setAttribute('aria-busy', String(value)); updateSend();
}
function updateSend() { $('send').disabled = !state.ready || state.busy || !$('message').value.trim(); }
function resize() { $('message').style.height = 'auto'; $('message').style.height = Math.min($('message').scrollHeight,180) + 'px'; updateSend(); }
$('message').addEventListener('input', resize);
$('message').addEventListener('keydown', event => { if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && event.keyCode !== 229) { event.preventDefault(); if (!$('send').disabled) submit(); } });
$('composer').addEventListener('submit', event => { event.preventDefault(); submit(); });
function syncSidebar() {
  const mobile=innerWidth<=680, open=mobile?document.body.classList.contains('sidebar-open'):!document.body.classList.contains('sidebar-hidden');
  $('sidebar').inert=!open; $('sidebar').setAttribute('aria-hidden',String(!open));
  document.querySelector('.main').inert=mobile && open;
  $('menu').setAttribute('aria-expanded',String(open));
}
function closeSidebar() { document.body.classList.remove('sidebar-open'); $('scrim').hidden = true; syncSidebar(); if(innerWidth<=680) $('menu').focus(); }
$('menu').addEventListener('click', () => { if (innerWidth <= 680) { document.body.classList.add('sidebar-open'); $('scrim').hidden = false; } else document.body.classList.remove('sidebar-hidden'); syncSidebar(); $('collapse').focus(); });
$('collapse').addEventListener('click', () => { if(innerWidth<=680) closeSidebar(); else { document.body.classList.add('sidebar-hidden'); syncSidebar(); $('menu').focus(); } });
$('scrim').addEventListener('click', closeSidebar);
addEventListener('resize', () => { if (innerWidth > 680) closeSidebar(); else syncSidebar(); });
syncSidebar();

function renderList() {
  $('history').replaceChildren();
  const query = $('search').value.toLocaleLowerCase().trim();
  const rows = state.conversations.filter(row => row.title.toLocaleLowerCase().includes(query));
  if (!rows.length) $('history').append(el('p','history-empty',query ? '没有找到匹配的对话' : '还没有对话。\n从下面的输入框开始吧。'));
  for (const row of rows) {
    const item = el('div','history-item' + (row.id === state.active ? ' active' : ''));
    const open = el('button','history-open'); open.append(el('span','history-title',row.title)); open.title = row.title; open.disabled = state.busy; open.addEventListener('click', () => choose(row.id)); item.append(open);
    const memoryLabel={pending:'原文已保存 · 待整理',queued:'原文已保存 · 等待整理',processing:'原文已保存 · 正在整理',ready:'整理已完成',failed:'原文已保存 · 整理失败'}[row.memory?.status];
    if(memoryLabel) { const label=el('span','history-memory'+(row.memory.status==='failed'?' failed':''),memoryLabel); if(row.memory.last_duration_s!==null && row.memory.last_duration_s!==undefined) label.title='最近整理用时 '+row.memory.last_duration_s+' 秒'; open.append(label); }
    if(row.memory?.can_retry) { const retry=el('button','history-action'); retry.innerHTML=icon('bookmark'); retry.title='重试记忆整理'; retry.setAttribute('aria-label','重试记忆整理：'+row.title); retry.disabled=state.busy; retry.addEventListener('click',()=>retryMemory(row)); item.append(retry); }
    for (const [name,label,action] of [['rename','重命名对话',() => rename(row)],['archive','归档对话',() => archive(row)]]) {
      const button = el('button','history-action'); button.innerHTML = icon(name); button.setAttribute('aria-label', label + '：' + row.title); button.title = label; button.disabled = state.busy; button.addEventListener('click',action); item.append(button);
    }
    $('history').append(item);
  }
}
$('search').addEventListener('input',renderList);
async function list() {
  state.conversations = (await api('conversations')).conversations; renderList(); clearTimeout(list.timer);
  if(state.conversations.some(row=>['queued','processing'].includes(row.memory?.status))) list.timer=setTimeout(()=>list().catch(()=>{}),3000);
}
async function retryMemory(row) { try { await api('retry-memory',{conversation_id:row.id}); await list(); toast('已重新开始整理记忆'); } catch(error) { notice(error.message,true); } }
function scrollDown() { requestAnimationFrame(() => $('conversation-scroll').scrollTo({top:$('conversation-scroll').scrollHeight, behavior:'auto'})); }
function inline(text, target) {
  const pattern = /`([^`]+)`|\*\*([^*]+)\*\*|\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g;
  let last=0, match;
  while ((match=pattern.exec(text))) {
    target.append(document.createTextNode(text.slice(last,match.index)));
    if (match[1]) target.append(el('code','',match[1]));
    else if (match[2]) target.append(el('strong','',match[2]));
    else { const link = el('a','',match[3]); link.href = match[4]; link.target = '_blank'; link.rel = 'noopener noreferrer'; target.append(link); }
    last=pattern.lastIndex;
  }
  target.append(document.createTextNode(text.slice(last)));
}
function markdown(text) {
  const root=el('div','answer'), lines=text.replace(/\r\n/g,'\n').split('\n'); let i=0;
  while (i < lines.length) {
    const line=lines[i];
    if (!line.trim()) { i++; continue; }
    if (line.startsWith('```')) {
      const language=line.slice(3).trim(); i++; const code=[];
      while (i<lines.length && !lines[i].startsWith('```')) code.push(lines[i++]);
      if (i<lines.length) i++;
      const block=el('div','code-block'), header=el('div','code-header'), button=el('button'); button.type='button'; button.innerHTML=icon('copy'); button.append(document.createTextNode('复制')); button.addEventListener('click',() => copy(code.join('\n')));
      header.append(el('span','',language || '代码'),button); const pre=el('pre'); pre.append(el('code','',code.join('\n'))); block.append(header,pre); root.append(block); continue;
    }
    const heading=/^(#{1,3})\s+(.+)$/.exec(line);
    if (heading) { const node=el('h' + (heading[1].length+1)); inline(heading[2],node); root.append(node); i++; continue; }
    const listMatch=/^(?:[-*]|\d+\.)\s+(.+)$/.exec(line);
    if (listMatch) { const ordered=/^\d/.test(line), node=el(ordered?'ol':'ul'); while(i<lines.length) { const match=(ordered?/^\d+\.\s+(.+)$/ : /^[-*]\s+(.+)$/).exec(lines[i]); if(!match) break; const li=el('li'); inline(match[1],li); node.append(li); i++; } root.append(node); continue; }
    if (line.startsWith('> ')) { const node=el('blockquote'); inline(line.slice(2),node); root.append(node); i++; continue; }
    const paragraph=[];
    while(i<lines.length && lines[i].trim() && !/^(?:```|#{1,3}\s|[-*]\s|\d+\.\s|>\s)/.test(lines[i])) paragraph.push(lines[i++]);
    if(!paragraph.length) paragraph.push(lines[i++]);
    const node=el('p'); inline(paragraph.join('\n'),node); root.append(node);
  }
  return root;
}
function renderMessages() {
  $('welcome').hidden = state.messages.length > 0;
  document.querySelector('.main').classList.toggle('is-empty',state.messages.length === 0);
  $('messages').replaceChildren();
  for (const message of state.messages) {
    const node=el('article','message ' + message.role); node.setAttribute('aria-label',message.role==='user'?'你的消息':'MemSys 的回复');
    if (message.role==='user') node.append(el('div','user-content',message.content));
    else {
      const mark=el('span','assistant-mark'); mark.innerHTML=icon('logo'); mark.setAttribute('aria-hidden','true'); node.append(mark); const body=el('div','assistant-body'); body.append(markdown(message.content));
      const tools=el('div','message-tools'), button=el('button','icon-button'); button.type='button'; button.innerHTML=icon('copy'); button.title='复制回复'; button.setAttribute('aria-label','复制回复'); button.addEventListener('click',() => copy(message.content)); tools.append(button); body.append(tools);
      if (message.memory_items?.length) {
        const details=el('details','memory-reference'), summary=el('summary'); summary.innerHTML=icon('bookmark'); summary.append(document.createTextNode('参考了 ' + message.memory_items.length + ' 条记忆')); const items=el('ul');
        for (const item of message.memory_items) { const li=el('li','',item.text || '相关对话片段'); if(item.date) li.append(el('time','',item.date)); items.append(li); }
        details.append(summary,items); body.append(details);
      }
      node.append(body);
    }
    $('messages').append(node);
  }
}
function loading(stage) {
  let node=$('reply-loading');
  if (!node) { node=el('article','message assistant'); node.id='reply-loading'; const mark=el('span','assistant-mark'); mark.innerHTML=icon('logo'); mark.setAttribute('aria-hidden','true'); node.append(mark); const text=el('div','loading-message'); text.append(el('span','spinner'),el('span','', '正在回复…')); node.append(text); $('messages').append(node); }
  node.lastChild.lastChild.textContent = stage==='memory'?'正在查找相关记忆…':'正在回复…'; scrollDown();
}
async function closeActive(nextKey=null) {
  if(state.active && state.active!==nextKey && state.messages.length) await api('close',{conversation_id:state.active});
}
async function choose(key) {
  if(state.busy) return;
  busy(true);
  try { const data=await api('messages?id=' + encodeURIComponent(key)); await closeActive(key); state.active=key; pref('active',key); state.messages=data.messages; state.retry=data.retry; renderList(); renderMessages(); notice(data.retry?'上一条回复还未完成，可以重试。':'',true,!!data.retry); closeSidebar(); scrollDown(); $('message').focus(); }
  catch(error) { notice(error.message,true); }
  finally { busy(false); }
}
async function newChat() {
  if(state.busy) return;
  busy(true);
  try { await closeActive(); state.active=null; pref('active',''); state.messages=[]; state.retry=null; $('message').value=''; resize(); renderList(); renderMessages(); notice(''); closeSidebar(); $('message').focus(); }
  catch(error) { notice(error.message,true); }
  finally { busy(false); }
}
$('new-chat').addEventListener('click',newChat);
addEventListener('keydown',event => { if((event.metaKey || event.ctrlKey) && event.key.toLowerCase()==='k') { event.preventDefault(); newChat(); } if(event.key==='Escape') closeSidebar(); });
function rename(row) { state.rename=row.id; $('rename-input').value=row.title; $('rename-dialog').showModal(); $('rename-input').select(); }
$('rename-cancel').addEventListener('click',() => $('rename-dialog').close());
$('rename-form').addEventListener('submit',async event => { event.preventDefault(); try { await api('rename',{conversation_id:state.rename,title:$('rename-input').value}); $('rename-dialog').close(); await list(); toast('名称已更新'); } catch(error) { toast(error.message); } });
async function archive(row) { try { await api('archive',{conversation_id:row.id}); if(state.active===row.id) { state.active=null; pref('active',''); state.messages=[]; state.retry=null; renderMessages(); notice(''); } await list(); toast('对话已归档，已有记忆仍保留'); } catch(error) { notice(error.message,true); } }
$('settings-button').addEventListener('click',() => $('settings').showModal());

async function stream(body) {
  const response=await fetch('/chat-api/send',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({...body,stream:true})});
  if(!response.ok) { const data=await response.json(); throw new Error(data.error || '连接暂时不可用。'); }
  const reader=response.body.getReader(), decoder=new TextDecoder(); let buffer='',result=null;
  while(true) {
    const chunk=await reader.read(); if(chunk.done) break; buffer+=decoder.decode(chunk.value,{stream:true});
    let end;
    while((end=buffer.indexOf('\n\n'))!==-1) {
      const frame=buffer.slice(0,end); buffer=buffer.slice(end+2); const type=frame.split('\n').find(line=>line.startsWith('event:'))?.slice(6).trim();
      const raw=frame.split('\n').filter(line=>line.startsWith('data:')).map(line=>line.slice(5).trim()).join('\n'); if(!raw) continue;
      const data=JSON.parse(raw); if(type==='progress') loading(data.stage); if(type==='done') result=data; if(type==='error') throw new Error(data.error);
    }
  }
  if(!result) throw new Error('连接中断，消息会保留。可以重试上一条消息。'); return result;
}
async function submit(retry=false) {
  if(!state.ready || state.busy) return;
  const pending=retry?state.retry:null, message=pending?.message || $('message').value.trim(); if(!message) return;
  busy(true); notice('');
  try {
    if(!state.active) { const row=await api('conversations',{}); state.active=row.id; pref('active',row.id); }
    const body=pending || {conversation_id:state.active,message,request_id:crypto.randomUUID()}; state.retry=body;
    if(!pending) { state.messages.push({role:'user',content:message}); $('message').value=''; resize(); renderMessages(); }
    loading('memory');
    await stream(body);
    const data=await api('messages?id=' + encodeURIComponent(state.active)); state.messages=data.messages; state.retry=data.retry; renderMessages(); notice(''); scrollDown(); await list();
  } catch(error) {
    $('reply-loading')?.remove(); notice(error.message,true,!!state.retry); try { await list(); } catch {}
  } finally { busy(false); $('message').focus(); }
}
async function ready() {
  const me=await api('me'); $('model-name').textContent=me.model; $('settings-model').textContent=me.model; $('profile-name').textContent=me.name; $('settings-user').textContent=me.name;
  state.ready=true; updateSend(); await list();
  if(state.active && state.conversations.some(row=>row.id===state.active)) await choose(state.active); else { state.active=null; pref('active',''); renderMessages(); }
}
$('connect-dialog').addEventListener('cancel',event=>event.preventDefault());
$('connect-form').addEventListener('submit',async event=> { event.preventDefault(); try { await api('connect',{token:$('connect-token').value}); $('connect-token').value=''; $('connect-dialog').close(); await ready(); } catch(error) { $('connect-error').textContent=error.message; } });
(async () => {
  try { await api('connect',{}); await ready(); }
  catch(error) { if(error.status===401) $('connect-dialog').showModal(); else { $('model-name').textContent='连接未完成'; notice(error.message,true); } }
})();
