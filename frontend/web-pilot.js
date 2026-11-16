// Saved extraction evidence only. Opening this panel never fetches an external site.
const node=(tag,cls='',value)=>{const e=document.createElement(tag);e.className=cls;if(value!==undefined)e.textContent=value;return e;};
const when=value=>value?new Date(value).toLocaleString('zh-CN',{hour12:false}):'尚未试跑';
function link(url,label){try{const u=new URL(url);if(u.protocol!=='https:'||u.username||u.password)return node('span','','网址不可用');const a=node('a','',label||u.href);a.href=u.href;a.target='_blank';a.rel='noopener noreferrer';return a;}catch{return node('span','','网址不可用');}}
export function mountWebPilot(dialog,{request}){
 dialog.innerHTML='<div class="dialog-heading"><div><span class="eyebrow">HTML / SOURCE PILOT</span><h2 id="web-pilot-heading">网页来源试接</h2></div><button class="icon-button" id="web-pilot-close" aria-label="关闭网页试接">×</button></div><p>Trafilatura 提取普通网页正文。这里展示有限试跑保存的样本，查看不会抓取外站或调用模型。</p><p class="catalog-notice">人物讲话需核对归属后加入人物库；机构背景单独保存。当前持续配置见渠道卡片，实际在线状态见首页采集进程；试读耗时不代表发布到页面的实时延迟。</p><p id="web-pilot-status" role="status"></p><div id="web-pilot-sources" class="web-pilot-sources"></div><div class="web-pilot-grid"><section><h3>已保存网页样本</h3><div id="web-pilot-list"></div></section><section id="web-pilot-detail" aria-live="polite"><p>选择一个样本，查看提取正文及来源。</p></section></div>';
 const $=s=>dialog.querySelector(s);let controller,detailController,serial=0,detailSerial=0;
 const stop=()=>{serial++;detailSerial++;controller?.abort();detailController?.abort();};
 const field=(host,title,value)=>{const p=node('p','web-pilot-field');p.append(node('strong','',title+'：'));p.append(value instanceof Node?value:node('span','',value??'未提供'));host.append(p);};
 async function detail(id){
  detailController?.abort();detailController=new AbortController();const signal=detailController.signal,sequence=++detailSerial;const host=$('#web-pilot-detail');host.replaceChildren(node('p','','正在读取已保存正文…'));
  try{const row=await request('/api/v1/web-pilot/documents/'+id,{},signal);if(signal.aborted||sequence!==detailSerial)return;if(row.document_id!==id)throw new Error('返回的网页样本不匹配。');
   const d=row.extraction;host.replaceChildren(node('h3','',d.title));field(host,'原网页',link(row.url));field(host,'页面日期',`${d.page_date||'未提供'}（精度：${d.date_precision==='instant'?'页面标注时刻':d.date_precision==='day'?'仅日期，无秒级发布时间':'未知'}）`);field(host,'本机首次观察',when(row.first_observed_at));field(host,'归属',d.person_name||'机构背景 / 待核对');field(host,'归属依据',d.speaker_evidence||d.notice);field(host,'提取器',`${d.extractor} ${d.extractor_version} / ${d.parser_version}`);field(host,'正文字符',String(d.body_characters));field(host,'正文 SHA-256',d.body_sha256);field(host,'HTML SHA-256',d.html_sha256);
   if(row.record_id){field(host,'人物资料库','已加入，可在人物动态和人物问答中检索');field(host,'记录编号',row.record_id);}
   else field(host,'人物资料库','未加入人物原文库；保留为试接样本');
   const body=node('div','web-pilot-body',d.body_text);host.append(node('h4','','提取正文'),body);
  }catch(error){if(!signal.aborted&&sequence===detailSerial)host.replaceChildren(node('p','error-text',error.message));}
 }
 async function show(){
  stop();const sequence=serial;controller=new AbortController();const signal=controller.signal;if(!dialog.open)dialog.showModal();$('#web-pilot-status').textContent='正在读取试接记录…';$('#web-pilot-sources').replaceChildren();$('#web-pilot-list').replaceChildren();$('#web-pilot-detail').replaceChildren(node('p','','选择一个样本，查看提取正文及来源。'));
  try{const [summary,page]=await Promise.all([request('/api/v1/web-pilot/summary',{},signal),request('/api/v1/web-pilot/documents',{limit:30},signal)]);if(signal.aborted||sequence!==serial)return;
   for(const source of summary.items){const card=node('section','web-pilot-card');card.append(node('h3','',source.name),node('p','',source.origin==='catalog'?`来自候选目录第 ${source.catalog_number} 项`:'补充官方来源（原84项之外）'));field(card,'最近有限试跑',source.latest_run?`${when(source.latest_run.finished_at)} · ${source.latest_run.extracted||0} 篇提取成功`:'尚未试跑');field(card,'持续采集',source.continuous_collection_enabled?'已配置（在线状态见首页）':'未启用');if(source.last_continuous_scan_at)field(card,'最近持续检查',when(source.last_continuous_scan_at));field(card,'发现入口',link(source.discovery_url,'打开官方入口'));field(card,'使用依据',link(source.policy_url,'查看来源说明'));card.append(node('small','',source.policy_note));$('#web-pilot-sources').append(card);}
   $('#web-pilot-status').textContent=`已保存 ${page.items.length} 个样本 · 读取费用 US$0 · 不调用模型`;
   for(const row of page.items){const button=node('button','catalog-row');button.type='button';button.dataset.document=row.document_id;button.append(node('strong','',row.title),node('span','',`${row.person_name||'机构背景 / 待核对'} · ${row.page_date||'日期未知'}`),node('small','',`${row.body_characters} 字符 · ${row.record_id?'已加入人物库':'试接样本'}`));button.onclick=()=>detail(row.document_id);$('#web-pilot-list').append(button);}
   if(!page.items.length)$('#web-pilot-list').append(node('p','','尚无保存样本。需先在本机运行有限试接命令。'));
  }catch(error){if(!signal.aborted&&sequence===serial)$('#web-pilot-status').textContent=error.message;}
 }
 $('#web-pilot-close').onclick=()=>dialog.close();dialog.addEventListener('close',stop);
 return {show,close(){if(dialog.open)dialog.close();stop();}};
}
