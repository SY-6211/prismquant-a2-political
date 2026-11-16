// Discovery cadence and processing stages are separate from publication latency.
const labels={ecb:'ECB：官方索引＋RSS兜底',trump:'特朗普：第三方归档RSS（快源未接通）',fed:'美联储：官方HTML讲话索引',eia:'EIA：官方HTML能源背景',treasury:'财政部：官方HTML声明（归属待核对）'};
const results={completed:'检查完成',baseline_created:'旧内容基线已建立',no_new_candidates:'本轮没有新内容',completed_with_issues:'检查完成，部分内容需核对',failed:'本轮读取失败',not_due:'尚未到读取时间',paused:'已暂停待核对',cancelled:'当前轮已停止',stopped:'当前轮停止',skipped_busy:'同源任务执行中，本次跳过',blocked_unfinished_run:'上次运行未结束，需核对'};
const node=(tag,cls,text)=>{const n=document.createElement(tag);n.className=cls;if(text!==undefined)n.textContent=text;return n;};
const when=value=>value?new Date(value).toLocaleString('zh-CN',{hour12:false}):'尚未运行';
export async function showSourceStatus(host,request,signal,openRecord){
 try{
  const data=await request('/api/v1/monitoring-status',signal);if(signal.aborted)return;
  host.replaceChildren(node('h3','','来源检查与最近入库'));
  const labels={watching:'持续检查中',disabled:'已关闭',paused:'已暂停，待核对',not_scheduled:'未接入持续调度',offline:'采集进程未在线',backoff:'读取失败，退避中',needs_review:'本轮异常，待核对',stale:'检查记录已过期',awaiting_check:'等待首次成功检查'};
  for(const row of data.items||[]){
   const card=node('div','source-status-card');card.dataset.source=row.source;
   card.append(node('strong','',`${row.person_name} · ${row.method}`),node('span','source-state',labels[row.status]||'状态待确认'));
   card.append(node('p','muted',`最近成功检查：${row.last_checked_at?when(row.last_checked_at):'尚无记录'} · 配置间隔 ${row.interval_seconds} 秒`));
   if(row.last_check_result==='no_new_content')card.append(node('p','muted','最近一轮检查没有新增人物内容。'));
   const saved=row.latest_saved;
   if(saved){
    const button=node('button','text-button source-latest',`最近已入库：${saved.title}`);button.type='button';
    button.onclick=()=>openRecord(saved.record_id);card.append(button,node('p','muted',`首次获取：${when(saved.first_seen_at)} · 入库记录：${when(saved.stored_at)}`));
    card.append(node('p','muted',`来源标注发布：${saved.published_at?when(saved.published_at):saved.page_date?saved.page_date+'（仅日期）':'未提供可靠时间'}`));
    if(saved.detected_at)card.append(node('p','muted',`${saved.is_catchup?'历史回补':'持续采集记录'} · 发现：${when(saved.detected_at)} · 提交返回：${when(saved.commit_returned_at)}`));
   }else card.append(node('p','muted','该人物来源尚无已入库内容。'));
   card.append(node('small','muted',row.source_delay_note+' 发布至页面延迟尚未验收。'));host.append(card);
  }
  if(!data.items?.length)host.append(node('p','muted','该人物尚无已配置的持续采集渠道。'));
 }catch(error){if(!signal.aborted)host.replaceChildren(node('p','muted','来源状态暂不可用；已保存内容仍可阅读。'));}
}
export async function showDiscovery(host,request,signal){
 try{const data=await request('/api/v1/monitor-speed',{},signal);if(signal.aborted)return;host.replaceChildren();
  host.append(node('p','baseline-note',`持续发现配置：${data.runtime_configured?'已接入采集进程':'未启用'}；进程是否在线见上方“采集进程”。来源间隔、内部耗时与发布时效分别展示。`));
  for(const row of data.items){const card=node('div','speed-source');card.append(node('strong','',labels[row.source]||row.source));
   const status={disabled:'已关闭',not_due:'等待下轮',backoff:'失败退避中',paused:'已暂停',due:'等待检查'}[row.status]||'状态待核对';card.append(node('p','',`${row.enabled?'允许调度':'已关闭'} · ${status} · 检查间隔 ${row.interval_seconds} 秒 · 最近调度检查 ${when(row.last_finished_at)}${row.pause_reason?' · 需核对':''}`));
   const result=row.last_result||{};card.append(node('small','',`最近结果：${results[result.status]||result.status||'尚无'} · 待处理 ${row.pending} · 已测内部处理 ${row.delivery_samples} 条 · 浏览器提示回执 ${row.browser_notices} 条`));
   if(result.frontier?.baseline)card.append(node('p','baseline-note','已建立旧内容基线；这些记录不计作实时新增。'));
   const p95=row.discovery_to_commit?.p95_ms;const basis=row.measurement_start==='collection_round_start'?'轮次开始至提交上界':'首次发现至提交上界';card.append(node('p','baseline-note',`${basis} P95：${p95==null?'暂无有效样本':p95+' ms'}；公开发布至页面：未测量。`));
   if(row.source==='trump')card.append(node('p','baseline-note','归档站自身的收录延迟尚不可控，不能保证人物发帖后立即可见。'));
   if(row.pause_reason)card.append(node('p','error-text','暂停原因：'+row.pause_reason));host.append(card);
  }
 }catch(error){if(!signal.aborted)host.replaceChildren(node('p','baseline-note','持续发现状态暂不可用；不影响已保存原文阅读。'));}
}
const sessionId=globalThis.crypto?.randomUUID?.();let sent=0;
export function noticeReceipt(base,item,started,signal){
 if(!sessionId||document.hidden||signal.aborted||sent>=60)return;
 // A receipt only states that the update notice was offered for presentation, not that a human read it.
 requestAnimationFrame(()=>requestAnimationFrame(()=>{
  if(document.hidden||signal.aborted)return;const ms=performance.now()-started;if(ms<0||ms>60000)return;
  const box=document.querySelector('#live-message')?.getBoundingClientRect();if(!box||box.bottom<=0||box.top>=innerHeight)return;
  if(base&&new URL(base,location.href).origin!==location.origin)return;sent++;
  fetch(`${base}/api/v1/monitor-speed/receipts`,{method:'POST',signal,headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id:sessionId,version_id:item.version_id,notice_render_ms:ms})}).catch(()=>{});
 }));
}
