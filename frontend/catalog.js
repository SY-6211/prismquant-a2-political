// Read-only candidate inventory. Administration never uses a chat or model key.
const decisions={needs_review:'待审核',awaiting_access:'等待访问条件',out_of_scope:'暂不纳入',pilot_candidate:'可试接（未接入）'};
const issues={missing_url:'缺少网址',homepage_only:'仅首页地址',profile_only:'人物介绍页',identity_needs_review:'身份待核实',publisher_speaker_boundary:'发布者与发言人需区分',documentation_only:'说明页，非发布流',entry_not_verified:'发布入口未验证',access_not_verified:'使用条件未验证',latency_not_measured:'更新速度未测量'};
const kinds={missing:'缺失',account:'账号地址',profile:'人物介绍页',homepage:'网站首页',webpage:'网页地址'};
const node=(tag,cls='',value)=>{const n=document.createElement(tag);n.className=cls;if(value!==undefined)n.textContent=value;return n;};
function link(value){try{const u=new URL(value);if(!['https:','http:'].includes(u.protocol)||u.username||u.password)return null;const a=node('a','',u.href);a.href=u.href;a.target='_blank';a.rel='noopener noreferrer';return a;}catch{return null;}}
const when=value=>value?new Date(value).toLocaleString('zh-CN',{hour12:false}):'尚无审核';
export function mountCatalog(dialog,{request}){
  dialog.innerHTML=`<div class="dialog-heading"><div><span class="eyebrow">SOURCE DIRECTORY / CANDIDATES</span><h2 id="catalog-heading">候选来源目录</h2></div><button id="catalog-close" class="icon-button" aria-label="关闭来源目录">×</button></div>
    <p class="catalog-notice">这里整理待接入来源。导入、审核和“可试接”均不会启用采集；原表核实标签不代表系统已验证入口、权限或时效。</p>
    <div id="catalog-summary" class="catalog-stats" role="status"></div>
    <form id="catalog-search" class="catalog-filters"><label>名称、原标签或说明<input id="catalog-q" type="search" maxlength="100" placeholder="例如 Musk、财报、Bloomberg"></label><label>来源类型<select id="catalog-type"><option value="">全部类型</option></select></label><label>审核状态<select id="catalog-decision"><option value="">全部状态</option><option value="needs_review">待审核</option><option value="awaiting_access">等待访问条件</option><option value="out_of_scope">暂不纳入</option><option value="pilot_candidate">可试接（未接入）</option></select></label><label>原始初筛问题<select id="catalog-issue"><option value="">全部问题</option></select></label><button class="button secondary" type="submit">筛选</button></form>
    <p id="catalog-status" role="status"></p><div class="catalog-grid"><div><div id="catalog-list"></div><button id="catalog-more" class="button secondary" hidden>加载更多来源</button></div><div id="catalog-detail" class="catalog-detail" aria-label="候选来源详情"><p>选择来源，查看原始记录、接入缺口与审核历史。</p></div></div>`;
  const $=selector=>dialog.querySelector(selector);
  let rows=[],cursor=null,filters={},generation=0,detailGeneration=0,listController,detailController,summaryController;
  const text=(selector,value)=>{$(selector).textContent=value;};
  const stop=()=>{generation++;detailGeneration++;listController?.abort();detailController?.abort();summaryController?.abort();};
  function field(host,label,value){const row=node('div','catalog-field');row.append(node('strong','',label));if(value instanceof Node)row.append(value);else row.append(node('p','',value||'未填写'));host.append(row);}
  function render(){const host=$('#catalog-list');host.replaceChildren();for(const item of rows){const b=node('button','catalog-row');b.type='button';b.dataset.candidate=item.candidate_id;b.append(node('strong','',`${item.number}. ${item.name}`),node('span','',`${item.source_type} · ${decisions[item.decision]||'状态未知'}`),node('small','',`${kinds[item.triage.entry_kind]||'地址未分类'} · ${(item.triage.issues||[]).filter(v=>!['entry_not_verified','access_not_verified','latency_not_measured'].includes(v)).map(v=>issues[v]||v).join('、')||'仍需核实具体发布入口'}`));b.onclick=()=>detail(item.candidate_id);host.append(b);}text('#catalog-status',rows.length?`已加载 ${rows.length} 组候选，按导入批次及原编号排列。`:'没有符合条件的候选来源。');$('#catalog-more').hidden=!cursor;}
  async function load(more=false){
    listController?.abort();const controller=listController=new AbortController(),serial=++generation;$('#catalog-more').disabled=true;text('#catalog-status','正在读取来源目录…');
    if(!more){rows=[];cursor=null;render();text('#catalog-status','正在读取来源目录…');detailGeneration++;detailController?.abort();$('#catalog-detail').replaceChildren(node('p','','选择来源查看详情。'));}
    try{const page=await request('/api/v1/source-catalog',{limit:20,...filters,cursor:more?cursor:null},controller.signal);if(controller.signal.aborted||serial!==generation)return;rows=more?[...new Map([...rows,...page.items].map(v=>[v.candidate_id,v])).values()]:page.items;cursor=page.next_cursor;render();}
    catch(error){if(!controller.signal.aborted&&serial===generation)text('#catalog-status',error.message);}
    finally{if(serial===generation)$('#catalog-more').disabled=false;}
  }
  async function detail(id){
    detailController?.abort();const controller=detailController=new AbortController(),serial=++detailGeneration;
    const host=$('#catalog-detail');host.replaceChildren(node('p','','正在读取候选详情…'));
    try{
      const item=await request(`/api/v1/source-catalog/${id}`,{},controller.signal);
      if(controller.signal.aborted||serial!==detailGeneration)return;
      if(item.candidate_id!==id)throw new Error('返回的候选来源不匹配。');
      host.replaceChildren(node('h3','',`${item.number}. ${item.name}`),node('p','catalog-decision',decisions[item.decision]||'状态未知'));
      field(host,'原表来源类型',item.source_type);field(host,'原表网址',link(item.original.reference_url)||'未提供网址');
      field(host,'原表核实标签（表作者记录）',item.original.reported_url_status);field(host,'地址形态（自动初判）',kinds[item.triage.entry_kind]||'未分类');
      field(host,'初筛范围',item.triage.relevance==='person_monitoring'?'人物监控候选':'背景或后续扩展候选');
      field(host,'原始标签',item.original.labels.map(v=>v.label).join('、'));field(host,'原表备注',item.original.note);field(host,'原表链接依据',item.original.verification_refs);
      field(host,'接入缺口（导入时初筛，后续以审核意见为准）',(item.triage.issues||[]).map(v=>issues[v]||v).join('；'));
      field(host,'采集方法',item.triage.method_hint);
      const review=item.review;
      const reviewSection=node('section','catalog-review');reviewSection.append(node('h3','','最新审核意见'));
      if(review){field(reviewSection,'审核版本',String(item.revision));field(reviewSection,'发布时间入口',link(review.discovery_url)||'未填写');field(reviewSection,'计划读取方式',review.transport);field(reviewSection,'涉及人物',review.person_names?.join('、'));field(reviewSection,'归属核对',review.attribution);field(reviewSection,'使用条件',review.access);field(reviewSection,'依据',review.evidence_urls?.join('\n'));field(reviewSection,'审核说明',review.note);field(reviewSection,'记录人和时间',`${item.actor} · ${when(item.reviewed_at)}`);}
      else reviewSection.append(node('p','','尚无人工审核记录。原表中的“已核实”不会自动变成系统审核通过。'));
      host.append(reviewSection);
      const audit=node('details','catalog-audit');audit.append(node('summary','','查看审核历史与导入追溯'));host.append(audit);
      field(audit,'导入批次',item.import_id);field(audit,'原表位置',`84個來源，第 ${item.original.sheet_row} 行；候选ID ${item.candidate_id}`);
      let before=null,loaded=false;const history=node('div',''),moreButton=node('button','text-button','加载审核历史');moreButton.type='button';
      const readHistory=async()=>{moreButton.disabled=true;try{const page=await request(`/api/v1/source-catalog/${id}/reviews`,{limit:10,before},controller.signal);if(controller.signal.aborted||serial!==detailGeneration)return;loaded=true;for(const r of page.items){const row=node('div','catalog-history-row');field(row,`审核 v${r.revision} · ${when(r.created_at)}`,`${decisions[r.review.decision]||r.review.decision} · ${r.actor}\n${r.review.note}`);history.append(row);}if(!page.items.length&&!before)history.append(node('p','','尚无历史记录。'));before=page.next_before;moreButton.hidden=!before;moreButton.textContent='加载更早审核';}catch(error){if(!controller.signal.aborted){loaded=false;history.append(node('p','error-text',error.message));}}finally{if(serial===detailGeneration)moreButton.disabled=false;}};
      moreButton.onclick=readHistory;audit.append(history,moreButton);audit.addEventListener('toggle',()=>{if(audit.open&&!loaded)readHistory();});
    }catch(error){if(!controller.signal.aborted&&serial===detailGeneration)host.replaceChildren(node('p','error-text',error.message));}
  }
  async function show(){stop();dialog.showModal();rows=[];cursor=null;filters={};$('#catalog-search').reset();$('#catalog-detail').replaceChildren(node('p','','选择来源查看详情。'));text('#catalog-summary','正在读取统计…');const controller=summaryController=new AbortController();
    try{const summary=await request('/api/v1/source-catalog/summary',{},controller.signal);if(controller.signal.aborted)return;const host=$('#catalog-summary');host.replaceChildren();for(const [label,value] of [['候选来源',summary.candidate_count],['缺少网址',summary.issues.missing_url||0],['人工已审核',summary.reviewed_count],['由目录启用采集',0]]){const metric=node('div','');metric.append(node('strong','',String(value)),node('span','',label));host.append(metric);}const types=$('#catalog-type');types.replaceChildren(node('option','','全部类型'));types.firstChild.value='';for(const value of Object.keys(summary.types).sort()){const o=node('option','',value);o.value=value;types.append(o);}const choices=$('#catalog-issue');choices.replaceChildren(node('option','','全部问题'));choices.firstChild.value='';for(const [value,label] of Object.entries(issues)){const o=node('option','',`${label}（${summary.issues[value]||0}）`);o.value=value;choices.append(o);}
      const metadata=node('p','catalog-import-note',summary.imports.map(v=>`${v.workbook_name} · ${v.original_label_count} 个原始标签 · 文件SHA-256 ${v.workbook_sha256}`).join('\n'));host.append(metadata);
    }catch(error){if(!controller.signal.aborted)text('#catalog-summary',error.message);}if(!controller.signal.aborted)await load();
  }
  $('#catalog-close').onclick=()=>dialog.close();dialog.addEventListener('close',stop);
  $('#catalog-search').onsubmit=event=>{event.preventDefault();filters={q:$('#catalog-q').value.trim(),source_type:$('#catalog-type').value,decision:$('#catalog-decision').value,issue:$('#catalog-issue').value};load();};
  $('#catalog-more').onclick=()=>load(true);
  return {show,stop,close(){dialog.close();stop();}};
}
