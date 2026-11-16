// Public rule hints and browser-local read state. Generation is never requested here.
const labels={active:'关键词线索',pending_review:'待审核，不计新提醒',grouped:'冷却期合并',duplicate:'重复内容',superseded:'来源已有新版本',corrected:'已纠错撤回'};
const priorities={high:'高',medium:'中',low:'低'};
const id=value=>/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(value||'');
const element=(tag,cls='',text)=>{const node=document.createElement(tag);node.className=cls;if(text!==undefined)node.textContent=text;return node;};
const when=value=>value&&!Number.isNaN(new Date(value).valueOf())?new Intl.DateTimeFormat('zh-CN',{dateStyle:'short',timeStyle:'short',hour12:false}).format(new Date(value)):'时间未知';
function button(label,action,cls='button secondary'){const node=element('button',cls,label);node.type='button';node.onclick=action;return node;}
function link(label,address){try{const url=new URL(address);if(!['https:','http:'].includes(url.protocol)||url.username||url.password)return null;const node=element('a','read-link',label);node.href=url.href;node.target='_blank';node.rel='noopener noreferrer';return node;}catch{return null;}}

export function mountReminders(host,{request,getBase,getPeople,submitFeedback}){
  let epoch=0,serial=0,detailSerial=0,controller,detailController,summaryController,paused=false,disposed=false,rows=[],cursor=null,selected=null,pending=null,busy=false;
  const badge=document.querySelector('#reminder-count');
  const key=()=>`pq.reminderRead:${getBase()||location.origin}`;
  function loadRead(){try{const value=JSON.parse(localStorage.getItem(key())||'[]');return new Set(Array.isArray(value)?value.filter(id).slice(-1000):[]);}catch{return new Set();}}
  let read=loadRead();
  const saveRead=()=>{read=new Set([...read].slice(-1000));try{localStorage.setItem(key(),JSON.stringify([...read]));}catch{}};
  const head=element('div','dialog-heading');head.append(element('h2','','站内提醒'),button('×',()=>host.close(),'icon-button'));head.lastChild.setAttribute('aria-label','关闭站内提醒');
  const note=element('p','muted','关键词命中是研究线索，不代表重要性、事实或股价影响已核实。已读状态仅保存在这个浏览器。');
  const state=element('p','reminder-state','正在读取处理状态…');state.setAttribute('role','status');
  const tools=element('div','reminder-tools');
  const people=element('select');people.setAttribute('aria-label','提醒人物');
  const status=element('select');status.setAttribute('aria-label','提醒状态');
  for(const [value,label] of Object.entries(labels)){const option=element('option','',label);option.value=value;status.append(option);}
  const priority=element('select');priority.setAttribute('aria-label','提醒优先级');
  for(const [value,label] of [['','全部优先级'],['high','高优先级'],['medium','中优先级'],['low','低优先级']]){const option=element('option','',label);option.value=value;priority.append(option);}
  tools.append(people,status,priority,button('刷新',()=>{load();refreshBadge();}),button('本页全部已读',()=>{for(const row of rows)read.add(row.reminder_id);saveRead();render();refreshBadge();}));
  const message=element('p','muted');message.setAttribute('role','status');
  const list=element('div','reminder-list');const detail=element('section','reminder-detail');detail.hidden=true;
  const more=button('加载更多',()=>load(true));more.hidden=true;
  host.replaceChildren(head,note,state,tools,message,list,more,detail);
  function personOptions(preferred){people.replaceChildren();const all=element('option','','全部人物');all.value='';people.append(all);for(const person of getPeople()){const option=element('option','',person.name_zh||person.name_original);option.value=person.person_id;people.append(option);}people.value=[...people.options].some(p=>p.value===preferred)?preferred:'';}
  function render(){
    list.replaceChildren();
    for(const row of rows){
      const item=element('article','reminder-row');item.classList.toggle('is-unread',!read.has(row.reminder_id)&&row.status==='active');
      const meta=element('div','reminder-meta');meta.append(element('span',`reminder-priority priority-${row.priority}`,`${priorities[row.priority]||'未知'}优先级`),element('span','',labels[row.status]||'状态未知'),element('span','',row.name_zh||row.name_original||'监控人物'),element('time','',when(row.created_at)));
      item.append(meta,element('h3','',row.evidence.rule_name),element('p','',row.evidence.source_title),element('p','muted',row.grouped_count?`另有 ${row.grouped_count} 条合并或重复命中，可展开查看。`:'按已发布规则判断，优先级来自规则配置。'));
      const actions=element('div','chat-actions');actions.append(button('查看依据',()=>open(row.reminder_id)),button(read.has(row.reminder_id)?'已读':'标为已读',()=>{read.add(row.reminder_id);saveRead();render();refreshBadge();},'text-button'));
      item.append(actions);list.append(item);
    }
  }
  async function load(append=false){
    controller?.abort();const c=controller=new AbortController(),owner=epoch,version=++serial;message.textContent='正在读取提醒…';more.disabled=true;
    try{
      const value=await request('/api/v1/reminders',{person_id:people.value,status:status.value,priority:priority.value,limit:20,cursor:append?cursor:null},c.signal);
      if(disposed||owner!==epoch||version!==serial||c.signal.aborted)return;
      rows=append?[...rows,...value.items.filter(item=>!rows.some(old=>old.reminder_id===item.reminder_id))]:value.items;cursor=value.next_cursor;render();more.hidden=!cursor;
      message.textContent=rows.length?`已加载 ${rows.length} 条 · 优先级高的在前，同优先级按提醒创建时间倒序`:'当前筛选没有提醒。新提醒从启用后的新入库内容产生，旧内容不会自动补发。';
    }catch(error){if(!c.signal.aborted&&owner===epoch&&version===serial){message.textContent=error.message+' 可点击刷新重新读取。';}}
    finally{if(version===serial)more.disabled=false;}
  }
  async function refreshBadge(){
    if(disposed||paused||document.hidden)return;summaryController?.abort();const c=summaryController=new AbortController(),owner=epoch;
    try{
      const [summary,page]=await Promise.all([request('/api/v1/reminders/summary',{},c.signal),request('/api/v1/reminders',{status:'active',limit:50},c.signal)]);
      if(c.signal.aborted||disposed||owner!==epoch)return;
      const unseen=page.items.filter(row=>!read.has(row.reminder_id)).length;badge.textContent=unseen+(page.next_cursor?'+':'');badge.title='最近50条有效提醒中，本浏览器尚未标为已读的数量';
      const worker=summary.pause_reason?'已暂停，需核对内容流':!summary.enabled?'未启用':summary.processor_configured&&summary.processor_active?'最近检查正常':'等待提醒处理进程';
      state.textContent=`提醒处理：${worker} · 已配置 ${summary.configured_rules} 条规则 · 待审核 ${summary.counts.pending_review||0} · 已撤回 ${summary.counts.corrected||0}。仅站内展示，不发送邮件或外部消息。`;
    }catch{if(!c.signal.aborted&&owner===epoch){badge.textContent='—';state.textContent='提醒状态暂不可用；人物原文与聊天功能仍可使用。';}}
  }
  async function open(identity){
    if(!id(identity))return;detailController?.abort();const c=detailController=new AbortController(),owner=epoch,version=++detailSerial;selected=identity;pending=null;detail.hidden=false;detail.replaceChildren(element('p','','正在读取提醒依据…'));
    try{
      const value=await request(`/api/v1/reminders/${identity}`,{},c.signal);if(c.signal.aborted||disposed||owner!==epoch||version!==detailSerial)return;
      read.add(identity);saveRead();render();refreshBadge();detail.replaceChildren(element('h3','',value.evidence.rule_name),element('p','reminder-meta',`${labels[value.status]} · ${value.name_zh||value.name_original} · 提醒于 ${when(value.created_at)}`),element('h4','',value.evidence.source_title),element('p','muted',`来源标注发布于 ${when(value.evidence.published_at)}；这是来源时间，不是本系统首次发现时间。`));
      for(const hit of value.evidence.engine_result.hits){detail.append(element('p','',`命中词：${hit.term}${hit.flags.length?' · 需核对：'+hit.flags.join('、'):''}`),element('blockquote','',hit.normalized_excerpt));}
      detail.append(element('p','muted','以上命中片段经过标准化，位置不是原文字符位置；完整原文绑定下方确切版本。图片说明与被引用内容未合并到主文。'));
      const links=element('div','chat-actions');for(const [label,address] of [['归档或来源 ↗',value.evidence.retrieved_url],['原站链接 ↗',value.evidence.original_url]]){const a=link(label,address);if(a)links.append(a);}
      detail.append(links);
      const full=element('div','reminder-full');full.append(button('查看完整保存版本',async()=>{
        full.replaceChildren(element('p','','正在读取原文…'));
        try{const data=await request(`/api/v1/events/${value.event_id}/versions/${value.version_id}`,{},c.signal);if(c.signal.aborted||disposed||owner!==epoch||version!==detailSerial)return;full.replaceChildren(element('p','muted',`原文版本 ${value.version_id}`),element('pre','',data.payload.main_text||'此版本无主文。'));}
        catch(error){if(!c.signal.aborted)full.replaceChildren(element('p','',error.message));}
      }));detail.append(full);
      for(const child of value.grouped_items){const part=element('div','reminder-grouped');part.append(element('p','',`${labels[child.status]} · ${child.evidence.source_title}`),button('查看这条命中',()=>open(child.reminder_id),'text-button'));detail.append(part);}
      if(value.actions.length){detail.append(element('h4','','更正与审核记录'));for(const action of value.actions)detail.append(element('p','',`${when(action.created_at)} · ${action.note}`));}
      const form=element('form','reminder-feedback');form.append(element('h4','','反馈这条线索'),element('p','muted','使用人物问答中已连接的身份提交。反馈只进入审核，不会直接删除其他人看到的提醒。'));
      const verdict=element('select');verdict.setAttribute('aria-label','提醒反馈类型');for(const [v,label] of [['false_positive','疑似误报'],['useful','有用的线索']]){const option=element('option','',label);option.value=v;verdict.append(option);}
      const note=element('textarea');note.setAttribute('aria-label','提醒反馈说明');note.maxLength=500;note.required=true;note.placeholder='请说明为什么有用或可能误报';
      const submit=button('提交反馈',()=>{});submit.type='submit';const abandon=button('结束本次重试',()=>{if(busy)return;pending=null;verdict.disabled=note.disabled=false;abandon.hidden=true;result.textContent='已结束本页重试；原反馈可能已保存，请勿反复提交。';});abandon.hidden=true;
      const result=element('p');result.setAttribute('role','status');form.append(verdict,note,submit,abandon,result);detail.append(form);
      form.onsubmit=async event=>{
        event.preventDefault();if(busy||selected!==identity)return;if(!pending){if(!note.value.trim()){result.textContent='请填写反馈说明。';return;}pending={reminder_id:identity,client_request_id:crypto.randomUUID(),verdict:verdict.value,note:note.value.trim()};}
        busy=true;submit.disabled=true;verdict.disabled=note.disabled=true;
        try{await submitFeedback(pending);if(owner!==epoch||selected!==identity)return;pending=null;result.textContent='反馈已保存，等待审核；没有调用模型。';abandon.hidden=true;note.value='';verdict.disabled=note.disabled=false;}
        catch(error){if(owner===epoch&&selected===identity){result.textContent=error.message+' 重试会复用原反馈标识。';abandon.hidden=false;}}
        finally{busy=false;if(selected===identity)submit.disabled=false;}
      };
      detail.scrollIntoView({block:'nearest'});
    }catch(error){if(!c.signal.aborted&&owner===epoch&&version===detailSerial)detail.replaceChildren(element('p','',error.message));}
  }
  for(const select of [people,status,priority])select.onchange=()=>{detailController?.abort();detail.hidden=true;selected=null;pending=null;load();};
  host.addEventListener('close',()=>{controller?.abort();detailController?.abort();selected=null;pending=null;detail.hidden=true;detail.replaceChildren();});
  const timer=setInterval(()=>refreshBadge(),15000);
  return {
    show(preferred=''){personOptions(preferred);status.value='active';priority.value='';host.showModal();load();refreshBadge();},
    reset(){epoch++;controller?.abort();detailController?.abort();summaryController?.abort();if(host.open)host.close();read=loadRead();rows=[];list.replaceChildren();detail.replaceChildren();refreshBadge();},
    pause(){paused=true;controller?.abort();detailController?.abort();summaryController?.abort();if(host.open)host.close();},
    resume(){paused=false;refreshBadge();},
    refresh:refreshBadge,
    dispose(){disposed=true;clearInterval(timer);controller?.abort();detailController?.abort();summaryController?.abort();},
  };
}
