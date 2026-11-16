import {mountCatalog} from './catalog.js';
import {mountWebPilot} from './web-pilot.js';
import {showDiscovery,noticeReceipt,showSourceStatus} from './monitor-speed.js?v=20261116-6';
import {mountReminders} from './reminders.js';
import {mountKnowledge} from './knowledge.js';
import { createApi, normalizeBase } from './api.js';
import { connectUpdates } from './realtime.js';
import { mountAnalysis, mountAnalysisHistory } from './analysis.js';
import { mountChat } from './chat.js?v=20261116-6';

const root = document.querySelector('#pq-monitor');
const $ = (selector) => root.querySelector(selector);
const state = { people: [], records: [], cursor: null, person: '', view: 'feed', selected: null, runs: [], runCursor: null, recordBusy: false, runBusy: false, feedExpanded: false, runsExpanded: false };
let base = '';
try { base = normalizeBase(localStorage.getItem('pq.apiBase') || ''); } catch { /* Use same origin if browser storage is unavailable. */ }
let api = createApi(base);
let feedController, detailController, runsController, runDetailController, bootController;
let toastTimer;
let operationsController;
let baselineController;
let analysisView, analysisHistory, savedAnalysisController;
let chatView;
let catalogView;
let webPilotView;
let reminderView;
let knowledgeView;
let operationsVisible=false;
let sourceStatusController;
async function refreshSourceStatus(){
  sourceStatusController?.abort();
  if(state.view!=='feed')return;
  const controller=sourceStatusController=new AbortController();
  await showSourceStatus($('#source-status'),(path,signal)=>api(path,{person_id:state.person},signal),controller.signal,id=>openRecord(id));
}
function applyProfile(value){
  operationsVisible=value.module==='prismquant-person-monitor'&&value.console_mode==='operations';
  root.dataset.console=operationsVisible?'operations':'research';
  for(const item of root.querySelectorAll('[data-operations-only]'))item.hidden=!operationsVisible;
  if(!operationsVisible&&state.view==='runs')switchView('feed');
}
let initialAnalysis = new URLSearchParams(location.search).get('analysis');
if (!/^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(initialAnalysis || '')) initialAnalysis = null;
let live, liveSyncController, livePreviewController;
let booted = false, syncPending = false, liveNotices = [], liveGeneration = 0;
function timingLabel(metric) {
  return metric?.samples ? `${metric.p95_ms.toFixed(1)} ms（${metric.samples} 个样本${metric.sufficient_for_percentiles?'':'，样本不足'}）` : '暂无有效样本';
}
async function refreshBaseline() {
  if(!operationsVisible)return;
  baselineController?.abort(); const controller=baselineController=new AbortController();
  showDiscovery($('#discovery-speed'),api,controller.signal);
  try {
    const data=await api('/api/v1/monitoring-baseline',{hours:24},controller.signal);
    if(controller.signal.aborted)return;
    const host=$('#baseline-sources');host.replaceChildren();
    for(const source of data.sources){
      const row=el('div','baseline-source');
      row.append(el('strong','',`${source.source_name} · 来源检查间隔 ${source.configured_poll_interval_seconds??'未知'} 秒 · 列表兜底同步 ${data.frontend_list_refresh_seconds} 秒`));
      row.append(el('span','',`列表读取与解析 P95：${timingLabel(source.list_fetch_and_parse)}；详情读取与解析 P95：${timingLabel(source.detail_fetch_and_parse)}；本轮观测至保存返回 P95：${timingLabel(source.observed_in_run_to_save_return)}`));
      row.append(el('p','baseline-note',`详情读取失败 ${source.failures.detail} · 列表失败 ${source.failures.list} · 提交结果未知 ${source.failures.unknown_write} · 无效自动计时 ${source.failures.invalid_timing}；无有效计时明细 ${source.unmeasured_item_rows}，人工/回补明细 ${source.manual_or_history_item_rows}。首次公开时间和浏览器回执未测量。`));
      host.append(row);
    }
    if(data.truncated)host.append(el('p','baseline-note','窗口样本超过查询上限；仅展示最近部分，不能代表完整窗口。'));
  } catch(error) {if(!controller.signal.aborted)text('#baseline-sources','基线暂不可用，请检查 API、数据库和迁移版本；不影响原文列表。');}
}
function measureListPresentation(start,controller) {
  requestAnimationFrame(()=>requestAnimationFrame(()=>{
    if(controller!==feedController || controller.signal.aborted)return;
    text('#baseline-browser',`本次列表请求与呈现：${(performance.now()-start).toFixed(1)} ms（同一浏览器计时，非来源发帖延迟；仅本地显示，不上传）`);
  }));
}
async function refreshOperations() {
  if(!operationsVisible)return;
  operationsController?.abort(); const controller=operationsController=new AbortController();
  const labels={running:'运行中',stopped:'已停止',busy_or_stale:'心跳延迟，需检查',starting:'启动中',unknown:'状态未知',ok:'正常',unavailable:'不可用',start_blocked:'等待人工核对',check_failed:'检查失败'};
  try {
    const data=await api('/api/v1/system-status',{},controller.signal);
    if(controller.signal.aborted)return;
    const database=data.database, collector=data.collector, guardian=data.guardian, backup=data.backup;
    text('#op-database',`数据库：${labels[database.status] || '状态未知'}`);
    text('#op-collector',`采集进程：${labels[collector.status] || '状态未知'}${collector.phase==='checking'?' · 检查来源中':''}`);
    const guardianLabel=guardian.status==='stopped'
      ? (guardian.enabled?'已停止（需重新启动）':'未开启')
      : guardian.status==='running'
        ? (guardian.enabled?'守护中':'正在停止')
        : labels[guardian.status] || '状态未知';
    text('#op-guardian',`自动重启：${guardianLabel}${guardian.collector_status==='start_blocked'?' · 等待人工核对':''}`);
    text('#op-backup',`最近备份：${backup.finished_at?dateTime(backup.finished_at):'尚无成功备份'}${backup.status==='failed'?' · 最近尝试失败':''}`);
    $('#op-backup').title=data.backup.restore_tested?'已通过隔离恢复验证；本机备份仍需异地保管。':'归档校验通过与实际恢复验证分别记录。';
    for(const [id,healthy] of [['#op-database',database.status==='ok'],['#op-collector',collector.status==='running'],['#op-guardian',guardian.enabled && guardian.status==='running'],['#op-backup',backup.status==='ok']]) $(id).classList.toggle('op-warning',!healthy);
  }catch(error){if(!controller.signal.aborted)for(const id of ['#op-database','#op-collector','#op-guardian','#op-backup'])text(id,'运行状态暂不可用');}
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function text(selector, value) { $(selector).textContent = value; }
function message(selector, value = '', error = false) {
  const node = $(selector); node.textContent = value; node.hidden = !value;
  node.classList.toggle('is-error', error);
}
function dateTime(value) {
  if (!value) return '时间未知';
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? '时间未知' : new Intl.DateTimeFormat('zh-CN', {month: '2-digit', day: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false}).format(date);
}
function name(person) { return person?.name_zh || person?.name_original || '人物未知'; }
function displayPerson(record) { return record.person_name_zh || record.person_name_original || '人物未知'; }
function initials(person) { return (person.name_original || '?').split(' ').map(s => s[0]).filter(Boolean).slice(0,2).join(''); }
function badge(record) {
  const official = record.provenance_type === 'official';
  return el('span', `tag ${official ? 'official' : 'archive'}`, official ? '官方来源' : '第三方归档');
}
function link(label, address) {
  try {
    const url = new URL(address);
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) return null;
    const node = el('a', '', label); node.href = url.href; node.target = '_blank'; node.rel = 'noopener noreferrer'; return node;
  } catch { return null; }
}
function stamp() { text('#updated-at', `更新于 ${new Date().toLocaleTimeString('zh-CN', {hour12: false})}`); }
function toast(value) { clearTimeout(toastTimer); text('#toast', value); $('#toast').hidden = false; toastTimer = setTimeout(() => $('#toast').hidden = true, 2200); }
const statusNames = {running: '报告未结束', completed: '已完成', completed_with_issues: '完成 · 有待处理', no_candidates: '没有候选内容', no_new_candidates: '没有新内容', stopped: '已停止', interrupted: '已中断', inserted: '已入库', skipped_existing: '已有完整记录', already_exists: '已存在', skipped_unsupported: '暂不支持', needs_review: '待人工核对', failed: '失败', not_attempted: '尚未尝试'};
function statusLabel(value) { return statusNames[value] || value; }

function renderPeople() {
  const host = $('#people-list'); host.replaceChildren();
  for (const person of state.people) {
    const button = el('button', `person${state.person === person.person_id ? ' selected' : ''}`);
    button.type = 'button'; button.dataset.person = person.person_id;
    const avatar = el('span', `avatar${person.country_codes?.includes('US') ? ' warm' : ''}`, initials(person));
    const copy = el('span', 'person-copy'); copy.append(el('strong', '', name(person)), el('small', '', person.name_original));
    button.append(avatar, copy); host.append(button);
  }
  $('.all-person').classList.toggle('selected', !state.person);
  text('#people-count', state.people.length); text('#stat-people', state.people.length);
}

async function allPages(path, signal) {
  const items = []; let cursor = null;
  do {
    const page = await api(path, {limit: 100, cursor}, signal);
    items.push(...page.items); cursor = page.next_cursor;
  } while (cursor && items.length < 10000);
  if (cursor) throw new Error('基础资料超过当前页面容量。');
  return items;
}

async function boot() {
  bootController?.abort(); const controller = bootController = new AbortController();
  try{applyProfile(await api('/api/v1/module-info',{},controller.signal));}catch{if(!controller.signal.aborted)applyProfile({});}
  if(controller.signal.aborted)return;
  refreshOperations();refreshBaseline();
  $('#docs-link').href = `${base}/docs`;
  const results = await Promise.allSettled([
    allPages('/api/v1/people', controller.signal),
    allPages('/api/v1/sources', controller.signal),
    operationsVisible?api('/api/v1/collection-rules', {}, controller.signal):Promise.resolve({eligible_count:'—'}),
  ]);
  if (controller.signal.aborted) return;
  const errors = [];
  if (results[0].status === 'fulfilled') { state.people = results[0].value; renderPeople(); chatView?.setPeople(state.people, state.person); knowledgeView?.setPeople(state.people, state.person); }
  else { errors.push(results[0].reason.message); message('#people-list', '人物资料暂不可用', true); text('#stat-people', '—'); }
  if (results[1].status === 'fulfilled') text('#stat-sources', results[1].value.length); else { text('#stat-sources', '—'); errors.push('来源资料暂不可用'); }
  if (results[2].status === 'fulfilled') text('#stat-eligible', results[2].value.eligible_count); else { text('#stat-eligible', '—'); errors.push('采集配置暂不可用'); }
  text('#connection', errors.length ? '部分接口异常' : '接口已连接'); $('#connection').className = `connection ${errors.length ? 'bad' : 'ok'}`;
  message('#global-error', errors.join('；'));
  if (state.view === 'feed') await loadFeed(); else if (state.view === 'runs') await loadRuns();
  if (initialAnalysis && !controller.signal.aborted) {
    const run = initialAnalysis; initialAnalysis = null;
    openSavedAnalysis({run_id:run}).catch(error => message('#global-error', error.message, true));
  }
}

function renderRecords() {
  const query = $('#record-search').value.trim().toLocaleLowerCase();
  const provenance = $('#provenance-filter').value;
  const records = state.records.filter(record => (!query || record.title.toLocaleLowerCase().includes(query)) && (!provenance || record.provenance_type === provenance));
  const host = $('#record-list'); host.replaceChildren();
  text('#record-count', `${records.length} 条已加载`);
  text('#filter-note', query || provenance ? '筛选仅应用于已加载内容，可继续加载更多' : '时间按本机时区显示');
  for (const record of records) {
    const button = el('button', `record${record.record_id === state.selected ? ' selected' : ''}`);
    button.type = 'button'; button.dataset.record = record.record_id;
    button.setAttribute('aria-pressed', String(record.record_id === state.selected));
    const meta = el('div', 'record-meta'); meta.append(el('span', 'person-tag', displayPerson(record)), el('span', 'meta-dot', '·'), el('time', '', dateTime(record.published_at)));
    const bottom = el('div', 'record-bottom'); bottom.append(badge(record), el('span', 'read-link', '阅读全文 ↗'));
    button.append(meta, el('h3', '', record.title), bottom); host.append(button);
  }
  if (!records.length && !state.recordBusy) message('#feed-message', state.records.length ? '已加载内容中没有匹配项。' : '暂时没有已保存的公开发言。');
  else if (!state.recordBusy) message('#feed-message');
  $('#load-more').hidden = !state.cursor; $('#load-more').disabled = state.recordBusy;
}

async function loadFeed(more = false) {
  if(!more)refreshSourceStatus();
  if (more && (state.recordBusy || !state.cursor)) return;
  liveSyncController?.abort();
  feedController?.abort(); const controller = feedController = new AbortController();
  const requestStarted=performance.now();
  state.recordBusy = true; $('#load-more').disabled = true; message('#feed-message', more ? '正在加载更多…' : '正在读取公开发言…');
  try {
    const page = await api('/api/v1/public-records', {person_id: state.person, limit: 20, order: 'published_desc', cursor: more ? state.cursor : null}, controller.signal);
    if (controller.signal.aborted) return;
    state.records = more ? [...new Map([...state.records, ...page.items].map(item => [item.record_id, item])).values()] : page.items;
    state.feedExpanded = more;
    state.cursor = page.next_cursor; state.recordBusy = false; renderRecords(); stamp();
    measureListPresentation(requestStarted,controller);
  } catch (error) {
    if (controller.signal.aborted) return;
    message('#feed-message', `${error.message}${state.records.length ? ' 当前保留上次成功读取的内容。' : ''}`, true);
  } finally { if (feedController === controller) { state.recordBusy = false; $('#load-more').disabled = false; if(syncPending) { syncPending=false; live?.sync(); } } }
}

function sortRecords(records) {
  return records.sort((a,b) => {
    const left=a.published_at?Date.parse(a.published_at):-Infinity, right=b.published_at?Date.parse(b.published_at):-Infinity;
    return left===right?(a.record_id===b.record_id?0:a.record_id>b.record_id?-1:1):left>right?-1:1;
  });
}
function renderLiveRecords() {
  const anchor=[...root.querySelectorAll('.record')].find(node=>node.getBoundingClientRect().bottom>0 && node.getBoundingClientRect().top<innerHeight);
  const id=anchor?.dataset.record, top=anchor?.getBoundingClientRect().top;
  renderRecords();
  const updated=id?[...root.querySelectorAll('.record')].find(node=>node.dataset.record===id):null;
  if(updated && window.scrollY>0)window.scrollBy(0,updated.getBoundingClientRect().top-top);
}
async function syncLiveFeed(signal) {
  if (!booted) { booted=true; await boot(); return; }
  if (document.hidden || $('#settings-dialog').open) return;
  if (state.recordBusy) { syncPending=true; return; }
  const controller=liveSyncController=new AbortController(), person=state.person;
  const abort=()=>controller.abort(); signal.addEventListener('abort',abort,{once:true});
  if(signal.aborted)controller.abort();
  const anchor=state.records[0]?.record_id;
  const found=[]; let cursor=null, complete=false, next=null;
  try {
    // Bound gap recovery to 1,000 records. Existing pages/cursor/detail remain in place.
    for(let pageNumber=0;pageNumber<50;pageNumber++) {
      const page=await api('/api/v1/public-records',{person_id:person,limit:20,order:'published_desc',cursor},controller.signal);
      if(controller.signal.aborted || person!==state.person)return;
      found.push(...page.items); next=page.next_cursor;
      if(!anchor || page.items.some(item=>item.record_id===anchor) || !next) { complete=true; break; }
      cursor=next;
    }
    if(!anchor)state.cursor=next;
    state.records=sortRecords([...new Map([...state.records,...found].map(item=>[item.record_id,item])).values()]);
    renderLiveRecords(); stamp();
    if(!complete)text('#live-message','离线期间更新较多；已同步最近 1,000 条，请刷新列表并继续加载历史。');
  } catch(error) { if(!controller.signal.aborted) throw error; }
  finally { signal.removeEventListener('abort',abort); }
}
function renderLiveNotices() {
  text('#live-count',liveNotices.length); const host=$('#live-items'); host.replaceChildren();
  if(!liveNotices.length)host.append(el('p','muted','本页尚未收到新提示。已有内容可在发言列表查看。'));
  for(const item of [...liveNotices].reverse()) {
    const row=el('div','live-item');
    row.append(el('strong','',item.kind==='new'?'新增内容已保存':`内容修订${item.review_status==='needs_review'?' · 待核对':''}`),el('small','muted',dateTime(item.created_at)));
    const button=el('button','button secondary',item.kind==='new' && item.record_id?'阅读原文':'查看该版本');
    button.onclick=()=>{if(item.kind==='new' && item.record_id){$('#live-dialog').close();openRecord(item.record_id);}else previewVersion(item);};
    row.append(button);host.append(row);
  }
}
async function previewVersion(item) {
  livePreviewController?.abort();const controller=livePreviewController=new AbortController();
  const host=$('#live-preview');host.replaceChildren(el('p','muted','正在读取该版本…'));
  try {
    const version=await api(`/api/v1/events/${encodeURIComponent(item.event_id)}/versions/${encodeURIComponent(item.version_id)}`,{},controller.signal);
    if(controller.signal.aborted)return;
    const payload=version.payload;
    host.replaceChildren(el('h3','',payload.title),el('p','attribution-note',`版本 ${version.version_number} · ${version.review_status==='needs_review'?'待人工核对':'已保存'} · ${payload.provenance_type==='official'?'官方来源':'第三方归档，未验证原帖'}。以下为该版本快照，未替换正在阅读的原文。`),section('该版本主文',payload.main_text || '未提供主文'));
    for(const quote of payload.quoted_content || [])host.append(section('引用内容 · 单独展示',quote.text || '未提供文本'));
    for(const note of payload.media_notes || [])host.append(section('归档附件说明 · 不属于主文',note.text || '未提供说明'));
    const source=link('打开该版本来源 ↗',payload.retrieved_url);if(source)host.append(source);
  } catch(error) {if(!controller.signal.aborted)host.replaceChildren(el('p','error-text',error.message));}
}
async function applyLiveChange(item, signal, identity) {
  const noticeStarted=performance.now();
  const uuid=/^[0-9a-f]{8}-[0-9a-f-]{27}$/i;
  if(!['new','revision'].includes(item.kind) || !uuid.test(item.event_id) || !uuid.test(item.version_id))throw new Error('更新数据无效');
  if(item.kind==='new' && item.record_id && item.review_status==='accepted') {
    const generation=liveGeneration, person=state.person;
    const record=await api(`/api/v1/public-records/${encodeURIComponent(item.record_id)}`,{},signal);
    if(signal.aborted || generation!==liveGeneration)return;
    if(!person || record.person_id===person) {
      // A load already in flight can replace this insertion; request a reconciliation too.
      if(state.recordBusy)syncPending=true;
      if(person===state.person) {
        state.records=sortRecords([...new Map([...state.records,record].map(row=>[row.record_id,row])).values()]);
        renderLiveRecords();stamp();
      }
    }
  }
  if(!liveNotices.some(row=>row.cursor===identity)) {
    liveNotices.push({...item,cursor:identity});liveNotices=liveNotices.slice(-20);renderLiveNotices();
    text('#live-message',item.kind==='new'?'新内容已同步；当前筛选与阅读位置保留。':'收到内容修订，可在“查看更新”中核对该版本。');
    noticeReceipt(base,item,noticeStarted,signal);
  }
}
function startLive() {
  live?.stop();liveGeneration++;booted=false;syncPending=false;liveNotices=[];renderLiveNotices();
  live=connectUpdates({base,api,onChange:applyLiveChange,onSync:syncLiveFeed,
    onReset:()=>text('#live-message','连接中断较久或服务数据已重置，正在补同步列表；期间修订提示可能不完整。'),
    onStatus:status=>{const labels={connecting:'更新连接中',live:'实时更新已连接',reconnecting:'更新重连中',fallback:'定时同步中'};text('#live-status',labels[status]);$('#live-status').className=`connection ${status==='live'?'ok':'bad'}`;},
  });
}

function resetDetail() {
  detailController?.abort(); savedAnalysisController?.abort(); analysisView?.dispose(); analysisView = null; state.selected = null;
  const host = $('#detail-panel'); const box = el('div', 'detail-empty');
  box.append(el('span', 'empty-symbol', '↗'), el('h2', '', '阅读原文与归档详情'), el('p', '', '选择一条发言，查看完整正文、来源依据和附件说明。'), el('div', 'empty-rule'), el('span', 'micro', 'CONTEXT MATTERS'));
  host.replaceChildren(box);
}
function section(title, content) {
  const node = el('section', 'detail-section'); node.append(el('h3', '', title), el('p', '', content)); return node;
}
function renderDetail(record, runId = null) {
  const host = $('#detail-panel'); host.replaceChildren();
  const toolbar = el('div', 'detail-toolbar'); toolbar.append(el('span', '', '发言详情 / FULL TEXT'));
  const close = el('button', 'text-button', '关闭 ×'); close.onclick = () => { resetDetail(); renderRecords(); }; toolbar.append(close);
  const body = el('article', 'detail-body'); body.append(badge(record), el('h2', '', record.title));
  body.append(el('div', 'detail-byline', `${displayPerson(record)} · ${dateTime(record.published_at)}\n${record.source_name}`));
  const attribution = record.verification_status === 'archive_only' ? '第三方归档，未验证原帖。以下正文与归档附件说明分别保存。' : record.verification_status === 'verified_page_attribution' ? '官方页面归属匹配；不代表内容事实已独立核实。' : `归属状态：${record.verification_status}`;
  body.append(el('div', 'attribution-note', attribution), el('div', 'body-text', record.body_text));
  const archive = record.archive_post;
  if (archive) {
    const summary = section('归档信息', `账号：${archive.archive_handle}\n图片 ${archive.image_count} · 视频 ${archive.video_count}\n归档捕获：${dateTime(archive.archive_captured_at)}`);
    body.append(summary);
    for (const quote of archive.inline_quotes || []) body.append(section('主帖内引用 · 单独展示', quote.text));
    for (const quote of archive.quoted_posts || []) {
      body.append(section(`被引用帖子 · ${quote.archive_author || '作者未知'}`, quote.text || '未保存文本'));
    }
    if (archive.archive_media_notes?.length) {
      const notes = section('归档附件说明', '以下为归档站提供的图片描述或视频转写，未经媒体或发言人核对，不作为主帖正文。');
      for (const note of archive.archive_media_notes) {
        const disclosure = el('details'); disclosure.append(el('summary', '', `${note.kind === 'archive_transcript' ? '视频转写' : note.kind === 'archive_image_description' ? '图片描述' : '附件说明'}${note.association_status === 'unresolved' ? ' · 所属关系未确定' : ''}`), el('p', '', note.text || '暂无说明文本'));
        notes.append(disclosure);
      }
      body.append(notes);
    }
  }
  if (record.speaker_evidence) body.append(section('归属依据', record.speaker_evidence));
  const links = el('div', 'detail-links');
  const retrieved = link(record.provenance_type === 'official' ? '打开官方来源 ↗' : '打开归档页面 ↗', record.retrieved_url);
  const original = record.original_url !== record.retrieved_url ? link('打开原帖链接 ↗', record.original_url) : null;
  if (retrieved) links.append(retrieved); if (original) links.append(original);
  const copy = el('button', 'button secondary', '复制正文'); copy.onclick = async () => { try { await navigator.clipboard.writeText(record.body_text); toast('正文已复制'); } catch { toast('浏览器未允许复制，请手动选择正文。'); } }; links.append(copy);
  body.append(links);
  const analysis = el('div', 'analysis-panel');
  const tabs = el('div', 'detail-tabs'); tabs.setAttribute('role', 'group'); tabs.setAttribute('aria-label', '选择阅读内容');
  const originalTab = el('button', '', '原文与来源'), analysisTab = el('button', '', 'AI 分析');
  originalTab.type = analysisTab.type = 'button';
  function showAnalysis(show) { body.hidden = show; analysis.hidden = !show; originalTab.setAttribute('aria-pressed', String(!show)); analysisTab.setAttribute('aria-pressed', String(show)); }
  originalTab.onclick = () => showAnalysis(false); analysisTab.onclick = () => showAnalysis(true);
  tabs.append(originalTab, analysisTab); showAnalysis(!!runId);
  host.append(toolbar, tabs, body, analysis); host.scrollTop = 0;
  analysisView?.dispose(); analysisView = mountAnalysis(analysis, {api, record, runId});
}
async function openRecord(id, runId = null, navigationController = null) {
  if (savedAnalysisController !== navigationController) savedAnalysisController?.abort();
  analysisView?.dispose(); analysisView = null;
  detailController?.abort(); const controller = detailController = new AbortController();
  const cancelNavigation = () => controller.abort();
  navigationController?.signal.addEventListener('abort', cancelNavigation, {once:true});
  if (navigationController?.signal.aborted) controller.abort();
  state.selected = id; renderRecords();
  $('#detail-panel').replaceChildren(el('p', 'feed-message', '正在读取完整正文…'));
  try { const record = await api(`/api/v1/public-records/${encodeURIComponent(id)}`, {}, controller.signal); if (!controller.signal.aborted) renderDetail(record, runId); }
  catch (error) { if (!controller.signal.aborted) $('#detail-panel').replaceChildren(el('p', 'feed-message is-error', error.message)); }
  finally {
    navigationController?.signal.removeEventListener('abort', cancelNavigation);
    if (detailController === controller && controller.signal.aborted && navigationController?.signal.aborted) { resetDetail(); renderRecords(); }
  }
  if (!controller.signal.aborted && matchMedia('(max-width: 950px)').matches) $('#detail-panel').scrollIntoView({behavior: 'smooth', block: 'start'});
}

async function openSavedAnalysis(item) {
  savedAnalysisController?.abort(); const controller = savedAnalysisController = new AbortController();
  const run = await api(`/api/v1/agent-analyses/${encodeURIComponent(item.run_id)}`, {}, controller.signal);
  if (controller.signal.aborted) return;
  const event = await api(`/api/v1/events/${encodeURIComponent(run.event_id)}`, {}, controller.signal);
  if (controller.signal.aborted) return;
  switchView('feed');
  if (event.linked_record_ids?.length) {
    await openRecord(event.linked_record_ids[0], run.run_id, controller);
  } else {
    // Native-only events need no legacy record. Read their saved version directly.
    detailController?.abort(); analysisView?.dispose(); state.selected = null; renderRecords();
    const host = $('#detail-panel'), toolbar = el('div', 'detail-toolbar');
    const close = el('button', 'text-button', '关闭 ×'); close.type = 'button'; close.onclick = () => resetDetail();
    toolbar.append(el('span', '', '已保存的分析'), close);
    const panel = el('div', 'analysis-panel'); host.replaceChildren(toolbar, panel);
    analysisView = mountAnalysis(panel, {api, event:{...event, person_id:run.person_id}, runId:run.run_id});
  }
  if (!controller.signal.aborted && savedAnalysisController === controller) {
    $('#analysis-history-dialog').close();
    $('#detail-panel').scrollIntoView({behavior:'instant', block:'start'});
  }
}

function showAnalysisHistory() {
  analysisHistory?.dispose(); savedAnalysisController?.abort();
  analysisHistory = mountAnalysisHistory($('#analysis-history'), {api, personId:state.person,
    scopeName:state.person ? name(state.people.find(p => p.person_id === state.person)) : '全部人物',
    personName:id => name(state.people.find(p => p.person_id === id)), open:openSavedAnalysis});
  $('#analysis-history-dialog').showModal();
}

function renderRuns() {
  const host = $('#run-list'); host.replaceChildren();
  for (const run of state.runs) {
    const button = el('button', 'run-row'); button.type = 'button'; button.dataset.run = run.run_id;
    const identity = el('span', '', run.source === 'ecb' ? '欧洲央行 · 拉加德' : '第三方归档 · 特朗普'); identity.append(el('small', '', run.mode === 'automatic_batch_import' ? '自动采集' : run.mode === 'manual_history_import' ? '历史回补' : '手动采集'));
    const time = el('span', '', dateTime(run.started_at)); time.append(el('small', '', run.finished_at ? `结束 ${dateTime(run.finished_at)}` : '结束时间未知'));
    const counts = el('span', '', run.summary ? `新增 ${run.summary.inserted} · 待核对 ${run.summary.needs_review}` : '计数未知'); counts.append(el('small', '', statusLabel(run.status)));
    button.append(identity, time, counts, el('span', 'read-link', '查看明细 ↗')); host.append(button);
  }
  $('#runs-more').hidden = !state.runCursor; $('#runs-more').disabled = state.runBusy;
}
async function loadRuns(more = false) {
  if(!operationsVisible)return;
  if (more && (state.runBusy || !state.runCursor)) return;
  runsController?.abort(); const controller = runsController = new AbortController(); state.runBusy = true;
  $('#runs-more').disabled = true; message('#runs-message', '正在读取采集记录…');
  try {
    const page = await api('/api/v1/collection-runs', {limit: 20, source: $('#run-source').value, cursor: more ? state.runCursor : null}, controller.signal);
    if (controller.signal.aborted) return;
    state.runs = more ? [...new Map([...state.runs, ...page.items].map(item => [item.run_id, item])).values()] : page.items;
    state.runsExpanded = more;
    state.runCursor = page.next_cursor; state.runBusy = false; renderRuns(); stamp();
    message('#runs-message', state.runs.length ? '' : '暂无采集运行记录。');
  } catch (error) { if (!controller.signal.aborted) message('#runs-message', error.message, true); }
  finally { if (runsController === controller) { state.runBusy = false; $('#runs-more').disabled = false; } }
}
async function openRun(id) {
  runDetailController?.abort(); const controller = runDetailController = new AbortController();
  const host = $('#run-detail'); host.hidden = false; host.replaceChildren(el('p', '', '正在读取运行明细…'));
  try {
    const run = await api(`/api/v1/collection-runs/${encodeURIComponent(id)}`, {}, controller.signal);
    if (controller.signal.aborted) return;
    host.replaceChildren(el('h3', '', `${run.source.toUpperCase()} · ${statusLabel(run.status)}`), el('p', '', `运行编号 ${run.run_id}`));
    if (run.summary) host.append(el('p', '', `尝试 ${run.summary.attempted} · 新增 ${run.summary.inserted} · 待核对 ${run.summary.needs_review} · 失败 ${run.summary.failed}`));
    else host.append(el('p', '', '报告尚未完整保存，处理计数未知。'));
    if (run.stop_reason) host.append(el('p', '', run.stop_reason));
    for (const item of run.items || []) {
      const row = el('div', 'run-item'); row.append(el('strong', '', statusLabel(item.status)), el('p', '', item.reason || '无附加原因'));
      const source = link(item.url, item.url); if (source) row.append(source);
      if (item.record_id) { const open = el('button', 'button secondary', '查看内容'); open.onclick = () => { switchView('feed'); openRecord(item.record_id); }; row.append(open); }
      host.append(row);
    }
  } catch (error) { if (!controller.signal.aborted) host.replaceChildren(el('p', 'error-text', error.message)); }
}

function switchView(view) {
  state.view = view;
  for (const nav of root.querySelectorAll('[data-view]')) nav.classList.toggle('active', nav.dataset.view === view);
  $('#feed-view').hidden = view !== 'feed'; $('#runs-view').hidden = view !== 'runs'; $('#chat-view').hidden = view !== 'chat';
  root.classList.toggle('is-chat', view === 'chat');
  $('#source-status').hidden=view!=='feed';
  if(view==='feed')refreshSourceStatus();else sourceStatusController?.abort();
  if (view === 'chat') chatView?.show(state.person); else chatView?.hide();
  const person = state.people.find(p => p.person_id === state.person);
  text('#page-title', view === 'chat' ? '人物问答' : view === 'runs' ? '采集记录' : person ? `${name(person)} · 公开动态` : '人物动态');
  text('#page-description', view === 'chat' ? '带着问题阅读原文，让每一次回答留下依据。' : view === 'runs' ? '每次检查与入库，都保留可核对的运行记录。' : '追踪公开发言，保留原文与来源。');
  if (view === 'runs') loadRuns();
}

root.addEventListener('click', event => {
  const person = event.target.closest('[data-person]');
  if (person) {
    state.person = person.dataset.person; state.records = []; state.cursor = null; state.feedExpanded = false;
    $('#record-search').value = ''; $('#provenance-filter').value = '';
    resetDetail(); renderPeople(); renderRecords(); switchView('feed'); loadFeed();
  }
  const record = event.target.closest('[data-record]'); if (record) openRecord(record.dataset.record);
  const run = event.target.closest('[data-run]'); if (run) openRun(run.dataset.run);
  const nav = event.target.closest('[data-view]'); if (nav) switchView(nav.dataset.view);
});
$('#record-search').addEventListener('input', renderRecords);
$('#provenance-filter').addEventListener('change', renderRecords);
$('#load-more').onclick = () => loadFeed(true);
$('#runs-more').onclick = () => loadRuns(true);
$('#run-source').onchange = () => { runDetailController?.abort(); $('#run-detail').hidden = true; state.runs = []; state.runCursor = null; renderRuns(); loadRuns(); };
$('#refresh').onclick = async () => { $('#refresh').disabled = true; try { await boot(); analysisView?.refresh(); await chatView?.refresh(); } finally { $('#refresh').disabled = false; } };
$('#settings-toggle').onclick = () => { $('#api-base').value = base; text('#settings-error', ''); $('#settings-dialog').showModal(); };
$('#settings-close').onclick = () => $('#settings-dialog').close();
$('#settings-form').onsubmit = event => {
  event.preventDefault();
  try {
    const value = normalizeBase($('#api-base').value);
    chatView?.disconnect('后端设置已变更，请在同源页面重新连接会话身份。');
    catalogView?.close();
    webPilotView?.close();
    try { localStorage.setItem('pq.apiBase', value); } catch { /* Session-only configuration still works. */ }
    feedController?.abort(); runsController?.abort(); detailController?.abort(); runDetailController?.abort();
    live?.stop();liveSyncController?.abort();livePreviewController?.abort();$('#live-dialog').close();
    analysisHistory?.dispose(); savedAnalysisController?.abort(); $('#analysis-history-dialog').close();
    bootController?.abort();operationsController?.abort();baselineController?.abort();
    base = value; api = createApi(base); state.people = []; state.person = ''; state.records = []; state.cursor = null; state.runs = []; state.runCursor = null;
    reminderView?.reset();
    knowledgeView?.reset();
    resetDetail(); renderPeople(); renderRecords(); renderRuns(); $('#run-detail').hidden = true; switchView('feed');
    $('#settings-dialog').close(); startLive();
  } catch (error) { text('#settings-error', error.message); }
};
const poll = setInterval(() => {
  if (document.hidden || $('#settings-dialog').open) return;
  if (state.view === 'runs' && !state.runBusy && !state.runsExpanded) loadRuns();
}, 60000);
const operationsPoll=setInterval(()=>{if(!document.hidden && !$('#settings-dialog').open)refreshOperations();},15000);
const baselinePoll=setInterval(()=>{if(!document.hidden && !$('#settings-dialog').open)refreshBaseline();},60000);
const sourceStatusPoll=setInterval(()=>{if(!document.hidden&&!$('#settings-dialog').open)refreshSourceStatus();},30000);
window.addEventListener('pagehide',event=>{if(!event.persisted)clearInterval(sourceStatusPoll);sourceStatusController?.abort();});
window.addEventListener('pagehide',event=>{if(!event.persisted)clearInterval(baselinePoll);baselineController?.abort();});
window.addEventListener('pagehide',event=>{if(!event.persisted)clearInterval(operationsPoll);operationsController?.abort();});
window.addEventListener('pagehide', event => { if(!event.persisted)clearInterval(poll); for (const controller of [bootController, feedController, detailController, runsController, runDetailController]) controller?.abort(); });
$('#live-open').onclick=()=>{$('#live-preview').replaceChildren();renderLiveNotices();$('#live-dialog').showModal();};
$('#live-close').onclick=()=>{livePreviewController?.abort();$('#live-dialog').close();};
document.addEventListener('visibilitychange',()=>{if(!document.hidden)live?.sync();});
window.addEventListener('pagehide',event=>{if(event.persisted)live?.pause();else live?.stop();liveSyncController?.abort();livePreviewController?.abort();});
window.addEventListener('pageshow',event=>{if(event.persisted)live?.resume();});
$('#analysis-open').onclick = showAnalysisHistory;
$('#analysis-history-close').onclick = () => $('#analysis-history-dialog').close();
$('#analysis-history-dialog').addEventListener('close', () => { analysisHistory?.dispose(); savedAnalysisController?.abort(); });
window.addEventListener('pagehide', event => { if(!event.persisted) analysisView?.dispose(); analysisHistory?.dispose(); savedAnalysisController?.abort(); $('#analysis-history-dialog').close(); });
window.addEventListener('pageshow', event => { if(event.persisted) analysisView?.refresh(); });
chatView = mountChat($('#chat-view'), {getBase:() => base, publicApi:(path,signal) => api(path,{},signal)});
window.addEventListener('pagehide', () => chatView.disconnect());
catalogView = mountCatalog($('#catalog-dialog'), {request:(path,params,signal) => api(path,params,signal)});
webPilotView = mountWebPilot($('#web-pilot-dialog'), {request:(path,params,signal) => api(path,params,signal)});
$('#web-pilot-open').onclick = () => webPilotView.show();
window.addEventListener('pagehide', () => webPilotView.close());
$('#catalog-open').onclick = () => catalogView.show();
window.addEventListener('pagehide', () => catalogView.close());
reminderView=mountReminders($('#reminders-dialog'),{request:(path,params,signal)=>api(path,params,signal),getBase:()=>base,getPeople:()=>state.people,submitFeedback:body=>chatView.submitReminderFeedback(body)});
$('#reminders-open').onclick=()=>reminderView.show(state.person);
knowledgeView=mountKnowledge($('#knowledge-dialog'),{request:(path,params,signal)=>api(path,params,signal),getBase:()=>base,getPeople:()=>state.people});
$('#knowledge-open').onclick=()=>knowledgeView.show(state.person);
window.addEventListener('pagehide',()=>knowledgeView.dispose());
window.addEventListener('pagehide',event=>{if(event.persisted)reminderView.pause();else reminderView.dispose();});
window.addEventListener('pageshow',event=>{if(event.persisted)reminderView.resume();});
reminderView.refresh();
startLive();
