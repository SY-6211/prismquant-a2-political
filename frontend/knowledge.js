// Local semantic retrieval of public main text. Does not call chat/generation APIs.
export function mountKnowledge(host,{request,getBase,getPeople}) {
  const node=(tag,cls,text)=>{const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n;};
  let controller,serial=0,detailSerial=0;
  const heading=node('div','dialog-heading');heading.append(node('h2','','原文知识库'));
  const close=node('button','icon-button','×');close.type='button';close.setAttribute('aria-label','关闭知识库');close.onclick=()=>host.close();heading.append(close);
  const summary=node('p','muted','正在读取索引状态…');summary.id='knowledge-summary';
  const note=node('p','muted','用中文检索已保存的英文或中文原文。这里只展示匹配片段；人物问答已接入关键词和向量混合检索。');
  const form=node('form','knowledge-form');const person=node('select');person.id='knowledge-person';person.required=true;
  const label=node('label','','选择人物');label.htmlFor=person.id;
  const query=node('textarea');query.id='knowledge-query';query.maxLength=500;query.required=true;query.rows=3;query.placeholder='例如：关税和贸易政策；利率与通胀';
  const qlabel=node('label','','想查找的内容');qlabel.htmlFor=query.id;
  const dates=node('div','knowledge-dates');const inputs=[];
  for(const title of ['发布日期起（UTC，含）','发布日期止（UTC，含）']){const l=node('label','',title);const d=node('input');d.type='date';l.append(d);dates.append(l);inputs.push(d);}
  const submit=node('button','button primary','检索原文');submit.type='submit';
  form.append(label,person,qlabel,query,dates,submit);
  const message=node('p','muted');message.setAttribute('role','status');
  const results=node('div','knowledge-results');results.id='knowledge-results';
  const original=node('section','knowledge-original');original.hidden=true;
  host.append(heading,note,summary,form,message,results,original);
  const cancel=()=>{serial++;detailSerial++;controller?.abort();submit.disabled=false;};
  function populate(rows,preferred=''){const previous=person.value;person.replaceChildren();for(const p of rows){const option=node('option','',p.name_zh||p.name_original);option.value=p.person_id;person.append(option);}const selected=preferred||previous;if(rows.some(p=>p.person_id===selected))person.value=selected;submit.disabled=!rows.length;if(!rows.length)message.textContent='人物资料尚未载入；稍候，或关闭窗口并刷新页面。';else if(message.textContent.startsWith('人物资料尚未载入'))message.textContent='';}
  form.addEventListener('input',()=>{cancel();message.textContent='';results.replaceChildren();original.hidden=true;});
  function scopeDate(value,exclusive=false){if(!value)return null;const d=new Date(`${value}T00:00:00Z`);if(exclusive)d.setUTCDate(d.getUTCDate()+1);return d.toISOString();}
  async function status(signal){const data=await request('/api/v1/knowledge/status',{},signal);if(signal.aborted)return;const counts=data.latest_states||{};
    summary.textContent=`已索引 ${counts.ready||0} / ${data.eligible_latest_versions} 个当前有效版本 · 保存 ${data.stored_chunks_including_history} 个片段（含历史） · ${data.dimensions} 维本地模型。${data.processor_configured&&data.control?.enabled?'已配置增量索引':'增量索引未启用'}。`;
    if(counts.failed||counts.indexing)summary.append(` 失败 ${counts.failed||0}，处理中 ${counts.indexing||0}；未就绪版本不会混用旧版本。`);
  }
  form.onsubmit=async event=>{event.preventDefault();cancel();const id=serial,c=controller=new AbortController();submit.disabled=true;results.replaceChildren();original.hidden=true;original.replaceChildren();message.textContent='本地语义检索中…';
    const timer=setTimeout(()=>c.abort(),30000);
    try{
      const base=new URL(getBase()||location.origin,location.origin);
      if(base.origin!==location.origin)throw new Error('语义检索需要同源后端；请打开后端 /monitor/ 或由总平台代理此接口。');
      const response=await fetch(`${base.origin}/api/v1/knowledge/search`,{method:'POST',headers:{'Content-Type':'application/json','Accept':'application/json'},body:JSON.stringify({person_id:person.value,query:query.value,limit:5,since:scopeDate(inputs[0].value),until:scopeDate(inputs[1].value,true)}),signal:c.signal,redirect:'error',cache:'no-store'});
      const data=await response.json();if(c.signal.aborted||id!==serial)return;
      if(!response.ok)throw new Error(data.detail?.message||'检索暂不可用。');
      message.textContent=data.items.length?`从 ${data.candidate_chunks} 个当前版本片段中返回 ${data.items.length} 个。相似度不是事实置信度；结果可能不相关，请阅读原文核对。`:'此人物及日期范围还没有可检索片段，请检查索引覆盖率。';
      for(const item of data.items){const card=node('article','knowledge-result');card.append(node('h3','',item.title||'无标题'),node('p','muted',`${item.name_zh||item.name_original} · ${item.source_name} · ${item.published_at||'发布时间未知'} · ${item.provenance_type==='official'?'官方来源':'第三方归档，未独立核实'}`),node('p','knowledge-excerpt',item.text),node('p','muted',`余弦相似度 ${item.cosine_similarity.toFixed(3)} · 原文字符 ${item.start_offset}–${item.end_offset}（Unicode，止位不含） · 版本 ${item.version_id}`));
        const open=node('button','button secondary','查看绑定原文');open.type='button';open.onclick=async()=>{const detailId=++detailSerial;original.hidden=false;original.replaceChildren(node('p','','正在读取此版本…'));try{const detail=await request(`/api/v1/events/${encodeURIComponent(item.event_id)}/versions/${encodeURIComponent(item.version_id)}`,{},c.signal);if(c.signal.aborted||id!==serial||detailId!==detailSerial)return;original.replaceChildren(node('h3','','此版本完整主帖正文'),node('p','knowledge-excerpt',detail.payload.main_text||'此版本没有主帖正文。'));original.scrollIntoView({block:'nearest'});}catch(error){if(!c.signal.aborted&&id===serial&&detailId===detailSerial)original.replaceChildren(node('p','error-text',error.message));}};card.append(open);results.append(card);
      }
      await status(c.signal);
    }catch(error){if(id===serial)message.textContent=c.signal.aborted?'检索已取消或超时，请手动重试。':error.message;}
    finally{clearTimeout(timer);if(id===serial)submit.disabled=false;}
  };
  host.addEventListener('close',cancel);
  return {show(preferred=''){cancel();populate(getPeople(),preferred);host.showModal();const c=controller=new AbortController();status(c.signal).catch(()=>{if(!c.signal.aborted)summary.textContent='索引状态暂不可用，请检查后端及数据库迁移。';});},setPeople:populate,reset(){cancel();if(host.open)host.close();results.replaceChildren();original.replaceChildren();original.hidden=true;},dispose:cancel};
}
