import {createChatApi, readChatCredential, conversationDates, validId} from './chat-api.js?v=20261116-6';
import {answerFeedback,qualityReport} from './quality.js';

const labels = {prepared:'资料已准备 · 待生成', executing_or_unfinished:'执行中或结果待核对', completed:'回答已保存',
  abstained:'证据不足', rejected:'结果未通过校验', failed:'本次执行失败', unknown:'响应或费用待核对'};
const modeLabels = {memory_clarification:'追问主题待明确',hybrid_rrf:'关键词＋语义混合检索',hybrid_lexical_fallback:'混合检索已降级为关键词匹配',exact_version:'指定原文版本', explicit_literal:'字面关键词检索', limited_bilingual_terms:'有限双语主题匹配', recent_saved_fallback:'少量最近保存内容'};
const researchLabels={focused:'聚焦问题',summary:'归纳要点',compare:'比较原文',evidence:'核对原文',research:'研究分析（条件推断）'};
const node = (tag, className = '', text) => {
  const value = document.createElement(tag); value.className = className;
  if (text !== undefined) value.textContent = text;
  return value;
};
const when = value => value && !Number.isNaN(new Date(value).valueOf()) ? new Intl.DateTimeFormat('zh-CN', {dateStyle:'short', timeStyle:'short', hour12:false}).format(new Date(value)) : '时间未知';
const personName = person => person?.name_zh || person?.name_original || '人物资料暂不可用';
const statusName = value => labels[value] || '状态未确认';
function sourceLink(label, address) {
  try {
    const url = new URL(address);
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) return null;
    const value = node('a', '', label); value.href = url.href; value.target = '_blank'; value.rel = 'noopener noreferrer'; return value;
  } catch { return null; }
}

// Return only evidence bound to the frozen input and the current conversation.
// Python offsets count Unicode code points, unlike JavaScript string offsets.
export function checkedSource(quote, context, person) {
  const source = context?.sources?.find(item => item.source_ref === quote.source_ref && item.event_id === quote.event_id && item.version_id === quote.version_id);
  if (!source || source.person_id !== person || source.role !== 'main_text' || quote.role !== 'main_text' || quote.valid !== true || typeof source.text !== 'string' || typeof quote.text !== 'string') return null;
  if (![source.start, source.end, quote.start, quote.end].every(Number.isInteger) || quote.start < source.start || quote.end > source.end || quote.start >= quote.end) return null;
  return Array.from(source.text).slice(quote.start - source.start, quote.end - source.start).join('') === quote.text ? source : null;
}

export function checkedBackground(quote,context){
  const source=context?.background_sources?.find(s=>s.source_ref===quote.source_ref);
  if(!source||source.role!=='background'||source.subject_person_id!==null||source.status!=='read'||quote.role!=='background'||quote.valid!==true||quote.not_person_statement!==true||quote.url!==source.url||quote.text_sha256!==source.text_sha256||typeof source.text!=='string'||typeof quote.text!=='string')return null;
  if(![source.start,source.end,quote.start,quote.end].every(Number.isInteger)||quote.start<source.start||quote.end>source.end||quote.start>=quote.end)return null;
  return Array.from(source.text).slice(quote.start-source.start,quote.end-source.start).join('')===quote.text?source:null;
}

export function mountChat(host, {getBase, publicApi}) {
  const $ = selector => host.querySelector(selector);
  let api = null, identity = null, people = [], epoch = 0, active = false;
  let listSerial = 0, conversationSerial = 0, answerSerial = 0, resultSerial = 0, qualitySerial = 0;
  let conversations = [], cursor = null, filters = {}, current = null, messages = [], after = null;
  let question = null, answers = [], answerCursor = null, selectedRun = null, busy = false, pending = null;
  let budget = null, selectedStatus = null;
  let statusRefreshBusy=false;
  const controllers = new Set();
  const text = (selector, value = '') => { $(selector).textContent = value; };
  function notice(value = '', error = false) {
    text('#chat-notice', value); $('#chat-notice').hidden = !value; $('#chat-notice').classList.toggle('is-error', error);
  }
  function cancelAll() { for (const controller of controllers) controller.abort(); controllers.clear(); }
  function disconnect(message = '') {
    qualitySerial++;
    $('#chat-quality-panel').open=false;$('#chat-quality-report').replaceChildren();
    epoch++; cancelAll(); api = null; identity = null; current = null; question = null; selectedRun = null;
    budget=null;selectedStatus=null;
    conversations = []; messages = []; answers = []; cursor = after = answerCursor = null; pending = null; busy = false;
    listSerial++; conversationSerial++; answerSerial++; resultSerial++;
    $('#chat-auth').hidden = false; $('#chat-connected').hidden = true; $('#chat-conversation').hidden = true; $('#chat-empty').hidden = false;
    for (const selector of ['#chat-conversations','#chat-messages','#chat-answer']) $(selector).replaceChildren();
    for (const selector of ['#chat-identity','#chat-conversation-title','#chat-conversation-meta','#chat-question-text','#chat-question-meta','#chat-answer-status','#chat-person-label']) text(selector);
    for (const selector of ['#chat-token','#chat-file','#chat-question','#chat-title','#chat-query','#chat-search','#chat-since','#chat-until','#chat-source-since','#chat-source-until','#chat-rename']) $(selector).value = '';
    $('#chat-archived').checked = false; filters = {}; $('#chat-question-detail').hidden = true; $('#chat-rename-form').hidden = true;
    $('#chat-answer-version').replaceChildren(); text('#chat-auth-status', message); notice(); updateControls();
    text('#chat-budget-status','连接身份后读取本轮在线回答额度。');
  }
  async function request(path, options = {}) {
    const ownerEpoch = epoch, client = api, controller = new AbortController(); controllers.add(controller);
    if (!client) { controllers.delete(controller); throw new Error('请先连接会话身份。'); }
    try { return await client(path, {...options, signal:controller.signal}); }
    catch (error) { if (ownerEpoch === epoch && error.status === 401) disconnect(error.message); throw error; }
    finally { controllers.delete(controller); }
  }
  const live = ownerEpoch => ownerEpoch === epoch && !!identity;
  function updateControls() {
    const blocked = busy || !!pending;
    for (const selector of ['#chat-person','#chat-title','#chat-rename-toggle','#chat-archive']) $(selector).disabled = blocked;
    $('#chat-create').disabled = busy || (!!pending && pending.kind !== 'create') || !people.length;
    $('#chat-create').textContent = pending?.kind === 'create' ? '重试创建会话' : '创建会话';
    $('#chat-abandon-create').hidden = pending?.kind !== 'create'; $('#chat-abandon-create').disabled = busy;
    const canCompose = !!current && !current.archived && !!current.person_id;
    for (const selector of ['#chat-question','#chat-query','#chat-retrieval-mode','#chat-source-since','#chat-source-until']) $(selector).disabled = !canCompose || blocked;
    $('#chat-research-mode').disabled=!canCompose||blocked||$('#chat-retrieval-mode').value!=='hybrid';
    $('#chat-use-history').disabled=!canCompose||blocked;
    $('#chat-read-official').disabled=!canCompose||blocked||$('#chat-retrieval-mode').value!=='hybrid';
    $('#chat-submit').disabled = busy || !canCompose || (!!pending && pending.kind !== 'question');
    $('#chat-stop-retry').hidden = !pending || pending.kind === 'create'; $('#chat-stop-retry').disabled = busy;
    $('#chat-retry-prepare').hidden = pending?.kind !== 'reprepare'; $('#chat-retry-prepare').disabled = busy;
    $('#chat-reprepare').disabled = busy || !!pending || !canCompose || !question;
    $('#chat-generate').hidden = selectedStatus!=='prepared'; $('#chat-generate').disabled = busy || !!pending || !canCompose || !budget?.can_generate;
    $('#chat-submit').textContent = pending?.kind==='question'?'继续提交并核对':budget?.can_generate?'发送并回答':'提交问题（模型暂不生成）';
    text('#chat-composer-note', current?.archived ? '会话已归档，历史仍可阅读；取消归档后可继续提问。' : !current?.person_id ? '此旧会话未绑定人物。请新建一个人物会话后提问。' : pending?.kind === 'question' ? '提交结果待确认。继续提交会复用原标识，不会自动生成或重复保存问题。' : budget?.can_generate ? '发送后基于已保存原文生成回答，计入本轮有限额度。回答及引用会保留。' : '当前模型生成额度不可用。仍可询问资料去哪里找；官方来源导航不消耗模型额度。其他问题可先保存。');
    for (const button of host.querySelectorAll('[data-chat-conversation]')) button.disabled = blocked;
  }
  function setPeople(values, preferred = '') {
    people = values; const previous = $('#chat-person').value; $('#chat-person').replaceChildren();
    if (!people.length) { const option = node('option', '', '人物资料尚未载入'); option.value = ''; $('#chat-person').append(option); }
    for (const person of people) { const option = node('option', '', personName(person)); option.value = person.person_id; $('#chat-person').append(option); }
    if (people.some(item => item.person_id === previous)) $('#chat-person').value = previous;
    else if (people.some(item => item.person_id === preferred)) $('#chat-person').value = preferred;
    if (current) renderConversation(); updateControls();
  }
  async function connect(value) {
    disconnect(); const ownerEpoch = epoch;
    try {
      const token = readChatCredential(value); api = createChatApi(getBase(), token); text('#chat-auth-status', '正在核对会话身份…');
      const user = await request('/me'); if (ownerEpoch !== epoch) return;
      identity = user; $('#chat-auth').hidden = true; $('#chat-connected').hidden = false;
      text('#chat-identity', `私有会话 · ${user.display_name}`); text('#chat-auth-status');
      await refreshBudget();if(ownerEpoch!==epoch)return;
      await loadConversations();
    } catch (error) { if (ownerEpoch === epoch && error.name !== 'AbortError') disconnect(error.message); }
  }
  function renderConversations() {
    const list = $('#chat-conversations'); list.replaceChildren();
    for (const item of conversations) {
      const button = node('button', `chat-conversation-row${current?.conversation_id === item.conversation_id ? ' selected' : ''}`);
      button.type = 'button'; button.dataset.chatConversation = item.conversation_id; button.setAttribute('aria-pressed', String(current?.conversation_id === item.conversation_id));
      button.append(node('strong', '', item.title), node('span', '', `${personName(people.find(p => p.person_id === item.person_id))} · ${item.message_count} 个问题`), node('time', '', `${when(item.created_at)}${item.archived ? ' · 已归档' : ''}`));
      button.onclick = () => openConversation(item.conversation_id); list.append(button);
    }
    text('#chat-list-status', conversations.length ? `已加载 ${conversations.length} 个会话 · 按创建时间倒序` : '没有符合条件的会话。可以新建会话，或调整搜索条件。');
    $('#chat-conversations-more').hidden = !cursor; updateControls();
  }
  async function loadConversations(more = false) {
    if (!identity || (more && !cursor)) return;
    const ownerEpoch = epoch, serial = ++listSerial; text('#chat-list-status', '正在读取我的会话…'); $('#chat-conversations-more').disabled = true;
    try {
      const page = await request('/conversations', {params:{limit:20, ...filters, cursor:more ? cursor : null}});
      if (!live(ownerEpoch) || serial !== listSerial) return;
      conversations = more ? [...new Map([...conversations,...page.items].map(item => [item.conversation_id,item])).values()] : page.items;
      cursor = page.next_cursor; renderConversations();
    } catch (error) { if (live(ownerEpoch) && serial === listSerial) text('#chat-list-status', error.message); }
    finally { if (live(ownerEpoch) && serial === listSerial) $('#chat-conversations-more').disabled = false; }
  }
  function renderConversation() {
    if (!current) return;
    $('#chat-empty').hidden = true; $('#chat-conversation').hidden = false;
    text('#chat-conversation-title', current.title); text('#chat-person-label', personName(people.find(person => person.person_id === current.person_id)));
    text('#chat-conversation-meta', `创建于 ${when(current.created_at)} · ${current.message_count} 个已保存问题${current.archived ? ' · 已归档' : ''}`);
    text('#chat-archive', current.archived ? '取消归档' : '归档'); updateControls();
  }
  function clearQuestion() {
    question = null; answers = []; selectedRun = null; answerCursor = null; answerSerial++; resultSerial++;
    selectedStatus=null;
    $('#chat-question-detail').hidden = true; $('#chat-answer').replaceChildren(); $('#chat-answer-version').replaceChildren(); text('#chat-answer-status');
  }
  async function openConversation(id) {
    if (busy || pending || !validId(id)) return;
    const ownerEpoch = epoch, serial = ++conversationSerial; clearQuestion(); current = null; messages = []; after = null;
    $('#chat-conversation').hidden = true; $('#chat-empty').hidden = false;
    $('#chat-empty').replaceChildren(node('p', '', '正在打开会话…')); $('#chat-rename-form').hidden = true;
    $('#chat-question').value = ''; notice();
    try {
      const page = await request(`/conversations/${id}/messages`, {params:{limit:50}});
      if (!live(ownerEpoch) || serial !== conversationSerial) return;
      if (page.conversation.conversation_id !== id) throw new Error('返回的会话与选择不匹配。');
      current = page.conversation; messages = page.items; after = page.next_after;
      renderConversation(); renderMessages(); renderConversations();
      if (messages.length) await selectQuestion(messages.at(-1).message_id);
    } catch (error) { if (live(ownerEpoch) && serial === conversationSerial) { notice(error.message, true); $('#chat-empty').replaceChildren(node('p', '', '暂时无法打开会话，请从左侧重新选择。')); } }
  }
  function renderMessages() {
    const host = $('#chat-messages'); host.replaceChildren();
    for (const message of messages) {
      const button = node('button', `chat-message-row${question?.message_id === message.message_id ? ' selected' : ''}`); button.type = 'button';
      button.dataset.chatMessage = message.message_id; button.setAttribute('aria-pressed', String(question?.message_id === message.message_id));
      button.append(node('span', 'chat-message-number', `问 ${message.position}`), node('span', 'chat-message-preview', message.content), node('time', '', when(message.created_at)));
      button.onclick = () => selectQuestion(message.message_id); host.append(button);
    }
    if (!messages.length) host.append(node('p', 'muted', '还没有问题，在下方开始提问。'));
    $('#chat-messages-more').hidden = after === null; updateControls();
  }
  async function moreMessages() {
    if (!current || after === null) return;
    const ownerEpoch = epoch, serial = conversationSerial, id = current.conversation_id, position = after;
    $('#chat-messages-more').disabled = true;
    try {
      const page = await request(`/conversations/${id}/messages`, {params:{limit:50,after:position}});
      if (!live(ownerEpoch) || serial !== conversationSerial || current?.conversation_id !== id) return;
      current = page.conversation; messages = [...new Map([...messages,...page.items].map(item => [item.message_id,item])).values()]; after = page.next_after;
      renderConversation(); renderMessages();
    } catch (error) { if (live(ownerEpoch) && serial === conversationSerial) notice(error.message, true); }
    finally { if (live(ownerEpoch) && serial === conversationSerial) $('#chat-messages-more').disabled = false; }
  }
  async function selectQuestion(id, preferredRun = null) {
    const value = messages.find(item => item.message_id === id); if (!value || !current) return;
    clearQuestion(); question = value; $('#chat-question-detail').hidden = false;
    text('#chat-question-meta', `问题 ${value.position} · ${when(value.created_at)}`); text('#chat-question-text', value.content); renderMessages();
    await loadAnswers(false, preferredRun);
  }
  function renderAnswerOptions() {
    const select = $('#chat-answer-version'); select.replaceChildren();
    for (const answer of answers) {
      const option = node('option', '', `${when(answer.created_at)} · ${statusName(answer.status)} · ${answer.run_id.slice(0,8)}`);
      option.value = answer.run_id; select.append(option);
    }
    if (selectedRun) select.value = selectedRun;
    select.disabled = !answers.length; $('#chat-answers-more').hidden = !answerCursor;
  }
  async function loadAnswers(more = false, preferredRun = null) {
    if (!question || !current || (more && !answerCursor)) return;
    const ownerEpoch = epoch, serial = ++answerSerial, id = current.conversation_id, messageId = question.message_id;
    text('#chat-answer-status', '正在读取保存的回答版本…'); $('#chat-answers-more').disabled = true;
    try {
      const page = await request(`/conversations/${id}/answers`, {params:{message_id:messageId,limit:10,cursor:more ? answerCursor : null}});
      if (!live(ownerEpoch) || serial !== answerSerial || current?.conversation_id !== id || question?.message_id !== messageId) return;
      answers = more ? [...new Map([...answers,...page.items].map(item => [item.run_id,item])).values()] : page.items;
      if (answers.some(item => item.message_id !== messageId)) throw new Error('回答与当前问题不匹配。');
      answerCursor = page.next_cursor;
      selectedRun = answers.some(item => item.run_id === preferredRun) ? preferredRun : answers.some(item => item.run_id === selectedRun) ? selectedRun : answers[0]?.run_id || null;
      renderAnswerOptions(); text('#chat-answer-status', answers.length ? `已加载 ${answers.length} 个版本；较早回答保留当时的资料和结论。` : '这个问题还没有准备或保存的回答。');
      if (selectedRun) await loadResult(selectedRun); else $('#chat-answer').replaceChildren();
    } catch (error) { if (live(ownerEpoch) && serial === answerSerial) text('#chat-answer-status', error.message); }
    finally { if (live(ownerEpoch) && serial === answerSerial) $('#chat-answers-more').disabled = false; }
  }
  async function loadResult(id) {
    if (!validId(id) || !current || !question) return;
    const ownerEpoch = epoch, serial = ++resultSerial, conversationId = current.conversation_id, messageId = question.message_id;
    selectedRun = id; $('#chat-answer').replaceChildren(node('p', 'muted', '正在读取这次回答与依据…'));
    try {
      const run = await request(`/conversations/${conversationId}/answers/${id}`);
      if (!live(ownerEpoch) || serial !== resultSerial || current?.conversation_id !== conversationId || question?.message_id !== messageId) return;
      if (run.run_id !== id || run.conversation_id !== conversationId || run.message_id !== messageId || run.context?.person_id !== current.person_id || run.context?.message_id !== messageId || run.context?.conversation_id !== conversationId) throw new Error('回答的会话、问题或人物不匹配，已停止显示。');
      renderResult(run);
    } catch (error) { if (live(ownerEpoch) && serial === resultSerial) $('#chat-answer').replaceChildren(node('p', 'error-text', error.message)); }
  }
  function citationBox(quotes, context) {
    const box = node('details', 'chat-evidence'); box.append(node('summary', '', `查看原文依据（${quotes.length}）`));
    for (const quote of quotes) {
      const source = checkedSource(quote, context, current.person_id), row = node('div', 'chat-source');
      row.append(node('blockquote', '', quote.text));
      if (!source) { row.append(node('p', 'error-text', '引用与冻结资料的版本、位置或人物不匹配，需人工核对。')); box.append(row); continue; }
      row.append(node('p', 'muted', `${source.source_name} · 来源标注发布于 ${when(source.published_at)}${source.truncated ? ' · 当前为部分正文' : ''}`));
      row.append(node('p', 'chat-review', source.provenance_type === 'third_party_archive' ? '第三方归档，未验证原帖；引句存在不代表内容事实已独立核实。' : '引用来自保存的原文；内容事实未因引用校验而获独立核实。'));
      if(source.historical_version)row.append(node('p','chat-review','这是同一发言的其他保存版本，不是另一条新发言，也不证明立场发生变化。'));
      const links = node('div', 'chat-actions');
      for (const [label, url] of [['查看来源 ↗',source.retrieved_url],['原始链接 ↗',source.original_url]]) { const link = sourceLink(label,url); if (link) links.append(link); }
      row.append(links);
      const snapshot = node('details', 'chat-snapshot'); snapshot.append(node('summary', '', '当时读取的原文片段'), node('p', 'chat-prewrap', source.text), node('p', 'muted', `原文版本 ${source.version_id} · 事件 ${source.event_id}`)); row.append(snapshot);
      const full = node('button', 'text-button', '查看完整保存版本'); full.type = 'button'; const preview = node('div', 'chat-full-version');
      full.onclick = () => showSourceVersion(source, full, preview); row.append(full, preview); box.append(row);
    }
    return box;
  }
  async function showSourceVersion(source, button, preview) {
    if (!validId(source.event_id) || !validId(source.version_id)) return;
    const ownerEpoch = epoch, serial = resultSerial, controller = new AbortController(); controllers.add(controller); button.disabled = true;
    preview.replaceChildren(node('p', 'muted', '正在读取保存版本…'));
    try {
      // Public evidence requests intentionally carry no private credential.
      const version = await publicApi(`/api/v1/events/${source.event_id}/versions/${source.version_id}`, controller.signal);
      if (!live(ownerEpoch) || serial !== resultSerial || !preview.isConnected) return;
      if (version.version_id !== source.version_id || version.person_id !== source.person_id || typeof version.payload?.main_text !== 'string') throw new Error('保存版本与引用不匹配。');
      preview.replaceChildren(node('p', 'chat-prewrap', version.payload.main_text));
    } catch (error) { if (live(ownerEpoch) && serial === resultSerial && preview.isConnected) preview.replaceChildren(node('p', 'error-text', error.message)); }
    finally { controllers.delete(controller); if (live(ownerEpoch) && serial === resultSerial) button.disabled = false; }
  }
  function renderNavigation(box,context,answer){
    const lookup=context.source_lookup;
    const resources=answer.source_resources||[],items=answer.source_items||[];
    const same=lookup&&JSON.stringify(resources)===JSON.stringify(lookup.resources)&&JSON.stringify(items)===JSON.stringify(lookup.items);
    if(!same){box.append(node('p','error-text','来源入口与本次保存资料不匹配，已停止显示。'));return;}
    box.append(node('h3','','资料去哪里找'),node('p','',answer.summary_zh||answer.answer_zh),node('p','chat-review','本次资料查找不调用模型，不消耗模型生成额度。'));
    const state=context.memory?.research_state;
    const search=context.source_lookup?.search;
    if(search){const labels={not_configured:'补充搜索尚未配置，当前使用官方目录',disabled:'本轮未开启补充搜索',ok:'已取得搜索候选，需核对相关性',no_approved_results:'没有找到获准读取的搜索候选',json_disabled_or_denied:'搜索服务未开放JSON或拒绝请求',rate_limited:'搜索服务限流',unavailable:'搜索服务暂不可用',invalid_response:'搜索响应格式异常',response_too_large:'搜索响应超过本轮上限'};box.append(node('p','chat-review',labels[search.status]||'搜索状态待核对'));}
    if(state?.item_count)box.append(node('p','muted',`已承接前文${state.origin==='analysis_evidence_gaps'?'资料缺口':'清单'} ${state.item_count} 项${state.selection.startsWith('checklist_item_')?'（仅查所选项目）':''}。`));
    for(const item of items){
      const section=node('section','chat-answer-point');section.append(node('h4','',`${item.number>0?item.number+'. ':''}${item.data_zh}`));
      if(!item.resource_keys.length)section.append(node('p','chat-review','当前目录没有适用入口；需要补充来源，不会以不相关网站代替。'));
      for(const key of item.resource_keys){
        const resource=resources.find(r=>r.resource_key===key);if(!resource)continue;
        const jump=node('button','text-button',resource.name+' · 查看入口与步骤 ↓');jump.type='button';jump.onclick=()=>$('#navigation-'+key)?.scrollIntoView({behavior:'smooth',block:'start'});section.append(jump);
      }
      box.append(section);
    }
    if(resources.length)box.append(node('h3','','来源入口与查找步骤'));
    for(const resource of resources){
        const card=node('section','chat-source');card.id='navigation-'+resource.resource_key;const link=sourceLink(resource.name+' ↗',resource.url);if(link)card.append(link);
        const status={read:'已读取来源页面 · 具体数据尚未核验',unavailable:'本次读取未成功 · 可手动打开',suggested:'目录提供的候选入口 · 本次未读取'};
        card.append(node('p','chat-review',status[resource.status]||'状态待核对'));
        if(resource.content_kind==='official_background_document'){
          card.append(node('p','chat-review',`${resource.issuer_type==='company'?'公司官方披露 · 报告期 '+resource.reporting_period:'美国官方背景资料'} · 发布日期 ${resource.source_date||'待核对'}；不是当前人物的原话，不代表最新市场数据。`));
          for(const snippet of resource.evidence_snippets||[])card.append(node('blockquote','chat-prewrap',snippet.text));
          card.append(node('p','muted','以上为实际读取的英文正文片段；内容真实性及经济含义仍需核对。'));
        }
        const list=node('ol');for(const step of resource.steps||[])list.append(node('li','',step));card.append(list,node('p','muted',resource.coverage_zh));
        if(resource.status==='read'){
          const excerpt=node('details');excerpt.append(node('summary','','查看实际读取的页面摘录'),node('p','chat-prewrap',resource.text),node('p','muted',`读取于 ${when(resource.read_at)} · 页面摘录哈希 ${resource.text_sha256}`));card.append(excerpt);
        }
        box.append(card);
    }
    if(resources.some(r=>r.status==='read'&&r.content_kind==='official_background_document')){
      const again=node('button','button secondary','用已找到的资料继续分析');again.type='button';again.disabled=!!current?.archived;
      again.onclick=()=>{if(busy||pending)return;$('#chat-question').value='结合已找到的官方背景资料，继续分析前文讨论的行业影响，区分人物原话、背景事实和条件推断。';$('#chat-retrieval-mode').value='hybrid';$('#chat-research-mode').value='research';$('#chat-read-official').checked=true;updateControls();$('#chat-question').scrollIntoView({behavior:'smooth',block:'center'});$('#chat-question').focus();notice('已填入综合分析问题。提交后准备人物原文和背景资料；生成回答需要可用额度。');};box.append(again);
    }
  }
  function backgroundCitations(quotes,context){
    const box=node('details','chat-evidence');box.append(node('summary','',`查看背景资料依据（${quotes.length}）`));
    for(const q of quotes){const s=checkedBackground(q,context);if(!s)continue;const row=node('div','chat-source');row.append(node('blockquote','chat-prewrap',q.text),node('p','muted',`${s.source_name} · 来源日期 ${s.source_date||'未知'} · 读取于 ${when(s.read_at)}`),node('p','chat-review','其他官方资料，不是所选人物的原话；引用匹配不代表事实已独立核实。'));const link=sourceLink('打开背景来源 ↗',s.url);if(link)row.append(link);box.append(row);}return box;
  }
  function renderSynthesis(box,context,answer){
    const people=answer.points||[],background=answer.background_points||[],analyses=answer.analysis||[],steps=answer.research_steps||[];
    const str=v=>typeof v==='string'&&!!v.trim();
    const refs=(a,n)=>Array.isArray(a)&&new Set(a).size===a.length&&a.every(i=>Number.isInteger(i)&&i>=1&&i<=n);
    const linked=(p,b)=>refs(p,people.length)&&refs(b,background.length)&&(p.length+b.length>0);
    const valid=answer.research_task===context.research_task&&str(answer.direct_answer_zh)&&
      people.every(p=>str(p.text_zh)&&p.citations?.length&&p.citations.every(q=>checkedSource(q,context,current.person_id)))&&
      background.every(p=>str(p.text_zh)&&p.citations?.length&&p.citations.every(q=>checkedBackground(q,context)))&&
      (!(people.length+background.length)||linked(answer.answer_person_basis,answer.answer_background_basis))&&
      analyses.every(a=>str(a.text_zh)&&linked(a.person_basis,a.background_basis)&&a.assumptions_zh?.length&&a.assumptions_zh.every(str)&&a.counterfactors_zh?.length&&a.counterfactors_zh.every(str))&&
      steps.every(s=>['data_zh','purpose_zh','how_to_check_zh'].every(k=>str(s[k])));
    if(!valid){box.append(node('p','error-text','综合回答的来源、人物或依据关联不匹配，已停止显示结论。'));return;}
    box.append(node('h3','','回答'),node('p','chat-prewrap',answer.direct_answer_zh));
    if(context.background_sources?.length)box.append(node('p','muted','本次只读取有限正文片段；未检索到不代表全文或其他渠道未披露。'));
    if(analyses.length)box.append(node('h3','','影响分析（条件推断）'));
    for(const a of analyses){const row=node('section','chat-answer-point');row.append(node('p','chat-prewrap',a.text_zh),node('p','','成立条件：'+a.assumptions_zh.join('；')),node('p','','反向因素：'+a.counterfactors_zh.join('；')));box.append(row);}
    if(people.length||background.length){const facts=node('details','chat-evidence');facts.append(node('summary','',`查看事实与来源（人物 ${people.length} · 背景 ${background.length}）`));for(const p of people)facts.append(node('h4','','人物原文所述'),node('p','',p.text_zh),citationBox(p.citations,context));for(const p of background)facts.append(node('h4','','官方背景资料所述'),node('p','',p.text_zh),backgroundCitations(p.citations,context));box.append(facts);}
    if(steps.length){box.append(node('h3','','研究步骤（方法建议）'));const list=node('ol');for(const s of steps){const row=node('li');row.append(node('strong','',s.data_zh),node('p','','用途：'+s.purpose_zh),node('p','','查找与核对：'+s.how_to_check_zh));list.append(row);}box.append(list);}
    const gaps=node('details','chat-evidence');gaps.append(node('summary','','资料缺口与下一步'));
    for(const gap of answer.evidence_gaps_zh||[]){const row=node('div','chat-source');row.append(node('p','',gap));const find=node('button','button secondary','查找这项资料');find.type='button';find.disabled=!!current?.archived;find.onclick=()=>{if(busy||pending)return;$('#chat-question').value='请查找以下待核对资料的官方入口：'+gap;$('#chat-retrieval-mode').value='hybrid';$('#chat-research-mode').value='auto';updateControls();$('#chat-question').focus();$('#chat-question').scrollIntoView({block:'center'});notice('已填入补查问题，提交后查找来源；不会自动调用模型。');};row.append(find);gaps.append(row);}
    for(const t of answer.limitations_zh||[])gaps.append(node('p','muted',t));box.append(gaps,node('p','chat-review','分析尚未独立复核；背景资料可能早于当前市场，具体公司数据仍需核对。'));
  }
  function renderResult(run) {
    const box = $('#chat-answer'), context = run.context, answer = run.answer; box.replaceChildren();
    selectedStatus=run.status;updateControls();
    const heading = node('div', 'chat-answer-heading'); heading.append(node('span', `tag ${run.status === 'completed' ? 'official' : 'archive'}`, statusName(run.status)), node('time', 'muted', when(run.result_created_at || run.created_at))); box.append(heading);
    box.append(node('p', 'chat-review', '历史回答依据生成当时读取的资料，不代表当前最新观点。'));
    if (run.execution_kind === 'synthetic_fixture') box.append(node('p', 'chat-review', '合成测试结果，不代表真实模型能力。'));
    if (run.prompt_version === 'person_qa_v2' && run.status === 'completed') box.append(node('p', 'chat-review', '较早提示词版本；时间判断需结合原文日期和资料读取时间核对。可准备新版本，旧回答仍保留。'));
    if(['completed','abstained'].includes(run.status)&&answer?.answer_mode==='source_navigation'){
      renderNavigation(box,context,answer);
    } else if(['completed','abstained'].includes(run.status)&&answer?.answer_mode==='synthesis'){
      renderSynthesis(box,context,answer);
    } else if (['completed','abstained'].includes(run.status) && answer) {
      const points = answer.points || [];
      const methodTask=context.research_task==='method_checklist'&&answer.research_task==='method_checklist';
      const steps=answer.research_steps||[];
      const methodsValid=methodTask&&Array.isArray(steps)&&steps.length>0&&steps.every(s=>s&&['data_zh','purpose_zh','how_to_check_zh'].every(k=>typeof s[k]==='string'&&s[k].trim())&&Array.isArray(s.premise_points)&&(s.basis_type==='general_method'?s.premise_points.length===0:s.basis_type==='source_linked'&&s.premise_points.length>0&&s.premise_points.every(i=>Number.isInteger(i)&&i>=1&&i<=points.length)));
      const allChecked = points.every(point => point.citations?.length && point.citations.every(quote => checkedSource(quote, context, current.person_id)));
      if (!allChecked || (run.status === 'completed' && !points.length&&!methodsValid)) {
        box.append(node('p', 'error-text', '回答引用未能与保存资料匹配，已停止显示结论，请核对原文。'));
      } else if (run.status === 'abstained') {
        box.append(node('p', 'chat-prewrap', answer.answer_zh || '本次检索范围内没有足够原文证据。'));
      } else {
        if(answer.answer_mode==='research'&&points.length)box.append(node('h3','','原文事实（来源所述）'));
        for (const [i,point] of points.entries()) { const section = node('section', 'chat-answer-point'); if(answer.answer_mode==='research')section.append(node('strong','',`事实 ${i+1}`)); section.append(node('p', '', point.text_zh), citationBox(point.citations, context)); box.append(section); }
        if(answer.answer_mode==='research'){
          const refs=(values,count)=>Array.isArray(values)&&values.length>0&&values.every(n=>Number.isInteger(n)&&n>=1&&n<=count);
          const str=v=>typeof v==='string'&&v.trim().length>0;
          const strs=v=>Array.isArray(v)&&v.length>0&&v.every(str);
          const analyses=answer.analysis||[],scenarios=answer.scenarios||[];
          const fullAnalysis=Array.isArray(analyses)&&Array.isArray(scenarios)&&analyses.length&&scenarios.length&&analyses.every(a=>a&&refs(a.premise_points,points.length)&&str(a.text_zh)&&str(a.mechanism_zh)&&strs(a.assumptions_zh)&&strs(a.counterfactors_zh))&&scenarios.every(s=>s&&refs(s.analysis_indices,analyses.length)&&str(s.condition_zh)&&str(s.implication_zh))&&refs(answer.conclusion_basis,analyses.length);
          const linked=(fullAnalysis||methodsValid&&analyses.length===0&&scenarios.length===0&&answer.conclusion_basis?.length===0)&&strs(answer.evidence_gaps_zh)&&str(answer.conclusion_zh)&&(!methodTask||methodsValid);
          if(!linked)box.append(node('p','error-text','研究分析的依据关联不完整，已停止显示推断。'));
          else{
            if(analyses.length)box.append(node('h3','','条件分析（模型推断）'));
            analyses.forEach((a,i)=>{const section=node('section','chat-answer-point');section.append(node('strong','',`分析 ${i+1} · 依据事实 ${a.premise_points.join('、')}`),node('p','',a.text_zh),node('p','','传导机制：'+a.mechanism_zh),node('p','','成立假设：'+a.assumptions_zh.join('；')),node('p','','反向因素：'+a.counterfactors_zh.join('；')));if(a.evidence_citations?.length&&a.evidence_citations.every(q=>checkedSource(q,context,current.person_id)))section.append(citationBox(a.evidence_citations,context));box.append(section);});
            if(scenarios.length)box.append(node('h3','','情景与可能影响'));
            for(const s of scenarios)box.append(node('p','chat-prewrap',`条件：${s.condition_zh}\n可能影响：${s.implication_zh}`));
            if(methodsValid){
              box.append(node('h3','','待核对数据清单（尚未核验）'));
              for(const step of steps){const section=node('section','chat-answer-point');section.append(node('strong','',step.data_zh),node('p','','用途：'+step.purpose_zh),node('p','','核对方式：'+step.how_to_check_zh),node('p','chat-review',step.basis_type==='general_method'?'一般研究方法，非人物原话或已核验事实':`原文相关研究方向 · 依据事实 ${step.premise_points.join('、')}；具体数据尚未核验`));if(step.evidence_citations?.length&&step.evidence_citations.every(q=>checkedSource(q,context,current.person_id)))section.append(citationBox(step.evidence_citations,context));box.append(section);}
            }
            box.append(node('h3','','还需核对的资料'));
            for(const [index,gap] of (answer.evidence_gaps_zh||[]).entries()){
              const row=node('div','chat-source');row.append(node('p','',`${index+1}. ${gap}`));
              const find=node('button','button secondary','查找这项资料');find.type='button';
              find.disabled=!!current?.archived;
              find.onclick=()=>{if(busy||pending)return;$('#chat-question').value='请查找以下待核对资料的官方入口：'+gap;$('#chat-retrieval-mode').value='hybrid';$('#chat-research-mode').value='auto';updateControls();$('#chat-question').scrollIntoView({behavior:'smooth',block:'center'});$('#chat-question').focus();notice('已填入补查问题，点击提交后查找来源；本轮操作不会自动请求模型。');};
              row.append(find);box.append(row);
            }
            box.append(node('h3','',methodTask?'研究说明与下一步':'有条件的研究结论'),node('p','chat-prewrap',answer.conclusion_zh),node('p','chat-review','推断仍需核实；引用位置通过校验不代表经济因果关系已获证实。'));
          }
        }
      }
      if (answer.limitations_zh?.length) { const section = node('section', 'chat-limitations'); section.append(node('h3', '', '资料与结论的限制')); for (const item of answer.limitations_zh) section.append(node('p', '', item)); box.append(section); }
      box.append(node('p', 'chat-review', run.execution_kind === 'local_abstention' ? '本机检索未取得可用原文，本次没有请求模型。' : '回答尚未人工认证，需结合原文核对。'));
    } else {
      const messages = {prepared:budget?.can_generate?'问题和资料已保存，可以点击“生成这条回答”。':'问题和资料已保存。当前生成额度不可用，此状态不表示模型正在作答。', executing_or_unfinished:'回答已提交，可能执行中或结果待核对；刷新只读取状态，不重复调用。', rejected:'本次结果未通过校验，不作为有效回答展示。', failed:'本次没有完成有效回答；是否发送过请求需核对回执。', unknown:'执行响应或费用未确认，应先核对原回执。'};
      const failureMessages={qa_output_truncated:'回答达到输出长度上限，尚未完整返回，因此未作为有效回答展示。可准备新回答版本；本次没有自动重试。',qa_response_incomplete:'模型没有完整结束回答，本次未展示结论。请核对后再准备新版本。',qa_response_contract_rejected:'模型返回的身份或响应结构不符合要求，本次未展示结论。',qa_schema_rejected:'回答的结构或依据关联不符合要求，已保留诊断信息待修正。本次没有自动重试。',qa_citation_rejected:'回答中的引文未能与本次原文片段精确匹配，已停止展示结论并保留诊断信息。',qa_invalid_json:'模型没有返回有效的结构化回答，已保留诊断信息。本次没有自动重试。'};
      box.append(node('p', 'chat-prewrap', failureMessages[run.failure_code] || messages[run.status] || '结果尚未确认。'));
    }
    if(run.status==='prepared'&&context.sources?.length){
      const prepared=node('details','chat-retrieved-sources');prepared.append(node('summary','',`查看检索片段（${context.sources.length}）`),node('p','chat-review','以下是准备好的原文片段，不是AI回答。检索排序不代表事实置信度；生成时仍须核对支持关系。'));
      for(const source of context.sources){
        if(source.person_id!==current.person_id||source.role!=='main_text'||!validId(source.event_id)||!validId(source.version_id)||typeof source.text!=='string'||!Number.isInteger(source.start)||!Number.isInteger(source.end)||source.start<0||source.end-source.start!==Array.from(source.text).length){prepared.append(node('p','error-text','片段与人物或版本位置不匹配，已停止显示。'));continue;}
        const row=node('section','chat-source');row.append(node('h3','',source.title||'已保存原文'),node('p','muted',`${source.source_name} · ${when(source.published_at)} · ${source.provenance_type==='official'?'官方来源':'第三方归档，未独立核实'}`),node('p','chat-prewrap',source.text),node('p','muted',`原文字符 ${source.start}–${source.end} · 版本 ${source.version_id}`));
        if(source.historical_version)row.append(node('p','chat-review','同一发言的其他保存版本，不能当作另一条发言或独立佐证。'));
        const full=node('button','text-button','查看完整保存版本');full.type='button';const preview=node('div','chat-full-version');full.onclick=()=>showSourceVersion(source,full,preview);row.append(full,preview);prepared.append(row);
      }
      box.append(prepared);
    }
    const retrieval = context.retrieval || {}, scope = node('details', 'chat-scope'); scope.append(node('summary', '', '查看本次资料范围与生成记录'));
    scope.append(node('p', '', `资料准备时间：${when(context.captured_at)}；检索方式：${modeLabels[retrieval.mode] || '有限检索'}。`));
    if(context.research_workflow){scope.append(node('p','',`综合研究：人物原文 ${context.sources?.length||0} 段，官方背景 ${context.background_sources?.length||0} 段；本轮最多补读 ${context.research_workflow.web_read_limit} 页，最多生成 1 次。`));const search=context.research_workflow.steps?.find(s=>s.search)?.search;if(search?.status==='not_configured')scope.append(node('p','chat-review','SearXNG尚未配置，本次按获准官方目录准备资料。'));}
    scope.append(node('p', '', `读取 ${context.sources?.length || 0} 条原文，每条最多 ${retrieval.characters_per_source || 1500} 字符；不是完整全网检索，不能据此断言人物没有发表其他内容。`));
    scope.append(node('p', '', `原文日期范围：${retrieval.since ? when(retrieval.since) : '未限定'} 至 ${retrieval.until ? when(retrieval.until) + '（不含）' : '未限定'}；关键词：${retrieval.queries?.filter(Boolean).join('、') || '未明确限定'}。`));
    if(retrieval.version==='person_hybrid_rrf_v1'){
      scope.append(node('p','',`关键词候选 ${retrieval.lexical_candidates??0} 个，语义候选 ${retrieval.semantic_candidates??0} 个；融合按排名排序，每个原文版本保留一个相关片段。`));
      if(retrieval.semantic_state!=='available')scope.append(node('p','chat-review','本次本地向量检索未就绪或CPU忙，已明确降级为关键词检索；可以稍后准备新版本，旧记录保留。'));
      if(retrieval.index_coverage_complete===false)scope.append(node('p','chat-review',`当前范围可用向量覆盖 ${retrieval.ready_versions_in_scope} / ${retrieval.eligible_versions_in_scope} 个版本；未索引内容仍可能通过关键词匹配进入。`));
      if(retrieval.query_truncated)scope.append(node('p','chat-review','问题较长，检索仅使用其前部及有界关键词；回答仍保留完整问题。可缩短问题后重新准备。'));
      if(retrieval.lexical_truncated||retrieval.semantic_truncated)scope.append(node('p','chat-review','候选超过检索窗口，仅融合各路前60项，不能代表穷尽检索。'));
    }
    scope.append(node('p', '', context.source_lookup?`处理方式：官方来源导航（模型请求 0 次）；回答记录：${run.run_id}。`:`模型：${run.model_id}；提示词：${run.prompt_version}；回答记录：${run.run_id}。`));
    scope.append(node('p', '', '历史对话用于理解追问，不作为事实证据；引用绑定当时保存的版本。'));
    if(context.memory){
      const memory=node('details','chat-memory');memory.append(node('summary','',`本次参考的历史（${context.prior_conversation?.length||0}）`));
      memory.append(node('p','',`仅限本人当前会话；从最近最多 ${context.memory.scan_limit} 个问题中选择。历史回答不作为事实证据。`));
      if(context.memory.enabled===false)memory.append(node('p','chat-review','本次已关闭历史参考，没有读取前文；已有历史记录仍保留。'));
      if(context.memory.query_expanded_from_user_history)memory.append(node('p','chat-review','检测到追问，已用前面的用户问题补充检索主题；没有把旧回答当作原文。'));
      if(context.memory.topic_reset)memory.append(node('p','chat-review','本次明确切换话题，未沿用旧主题。'));
      if(context.memory.unresolved_followup)memory.append(node('p','chat-review','尚无可定位的前文，请明确要继续研究的主题。'));
      if(context.memory.window_truncated||context.memory.omitted_for_context_budget)memory.append(node('p','chat-review','历史只读取有限窗口；部分内容因上下文预算未带入本次回答。'));
      for(const entry of context.prior_conversation||[])memory.append(node('p','chat-prewrap',`问题 ${entry.position??''}：${entry.question}${entry.question_truncated?'（摘录）':''}`));
      scope.append(memory);
    }
    if(context.plan){
      const plan=node('details','chat-plan');plan.append(node('summary','',`研究方式：${researchLabels[context.plan.strategy]||'聚焦问题'} · 读取过程`));
      plan.append(node('p','',context.source_lookup?`官方目录与获准来源；最多读取 ${context.source_lookup.max_reads||3} 页。本轮网页请求 ${context.source_lookup.network_requests} 次，搜索请求 ${context.source_lookup.search?.requests||0} 次，模型请求 0 次。`:`本地规则选择步骤，最多 ${context.plan.tool_call_limit} 次只读工具调用；未请求模型规划，也未联网搜索。`));
      const purposes={find_relevant_versions:'查找相关原文版本',read_matching_segment:'读取匹配片段',expand_original_context:'补读原文上下文',expand_research_context:'补读研究所需前后文',read_summary_context:'补读归纳所需正文',inspect_saved_revisions:'检查其他保存版本',read_saved_revision:'读取其他保存版本'};
      if(context.research_task==='method_checklist')plan.append(node('p','','研究任务：待核对数据清单；方法建议与已取得的事实分别展示。'));
      for(const step of context.plan.steps||[])plan.append(node('p','',`${step.number}. ${purposes[step.purpose]||step.tool} · ${step.status==='ok'?'完成':step.status==='not_found'?'当前范围内不可用':step.status==='stopped'?'达到本轮上限':'未完成'}`));
      if(context.plan.omitted_primary_candidates||context.retrieval?.omitted_due_context_limit)plan.append(node('p','chat-review','部分候选因任务策略或上下文上限未纳入，本次并未读完整个资料库。'));
      if(context.plan.execution_path)plan.append(node('p','chat-review','本次为兼容字面或精确版本读取，没有运行扩展读取策略。'));
      scope.append(plan);
    }
    if(context.prompt_contract)scope.append(node('p','muted',`提示词配置：${context.prompt_contract.version} · ${researchLabels[context.prompt_contract.strategy]||context.prompt_contract.strategy} · 内容哈希 ${context.prompt_contract.system_sha256}`));
    const cost = run.receipt?.reported_cost_usd;
    if (cost !== null && cost !== undefined) scope.append(node('p', '', `该回答回执已报费用：US$${cost}（不代表账户余额）。`));
    box.append(scope);
    const ownerEpoch=epoch, selected=selectedRun;
    answerFeedback(box,run,{request,isCurrent:()=>live(ownerEpoch)&&selectedRun===selected&&current?.conversation_id===run.conversation_id});
  }
  async function mutate(operation, afterSuccess) {
    if (busy) return; busy = true; updateControls(); const ownerEpoch = epoch;
    try { const value = await operation(() => live(ownerEpoch)); if (!live(ownerEpoch)) return; pending = null; busy = false; updateControls(); await afterSuccess(value, () => live(ownerEpoch)); }
    catch (error) { if (live(ownerEpoch)) { if (pending?.kind === 'create' && error.status >= 400 && error.status < 500) pending = null; busy = false; updateControls(); notice(error.message, true); } }
    finally { if (live(ownerEpoch)) { busy = false; updateControls(); } }
  }
  async function refreshBudget() {
    if(!identity)return;const ownerEpoch=epoch;
    try{const value=await request('/generation-budget');if(!live(ownerEpoch))return;budget=value;text('#chat-budget-status',`${value.message}${value.remaining_requests!==undefined?' · 剩余 '+value.remaining_requests+' 次':''}${value.remaining_usd!==undefined?' · 可用 US$'+value.remaining_usd:''}${value.expires_at?' · 到期 '+when(value.expires_at):''}`);}
    catch(error){if(live(ownerEpoch)){budget=null;text('#chat-budget-status','在线额度暂不可用；不会自动调用模型。');}}
    if(live(ownerEpoch))updateControls();
  }
  async function generate(runId=selectedRun) {
    if(busy||pending||!current||!question||!validId(runId))return;
    const ownerEpoch=epoch,id=current.conversation_id,messageId=question.message_id;
    await refreshBudget();if(!live(ownerEpoch)||current?.conversation_id!==id||question?.message_id!==messageId)return;
    if(!budget?.can_generate){notice(budget?.message||'本轮额度不可用，未请求模型。',true);return;}
    busy=true;updateControls();notice('正在提交回答任务…');
    try{
      await request(`/conversations/${id}/answers/${runId}/execute`,{method:'POST'});
      if(!live(ownerEpoch))return;
      notice('回答任务已提交，正在等待模型及引用校验。离开页面不会自动取消或重发。');
      for(let i=0;i<120;i++){
        if(!live(ownerEpoch)||current?.conversation_id!==id||question?.message_id!==messageId||document.hidden)return;
        await loadResult(runId);
        if(!live(ownerEpoch)||current?.conversation_id!==id||question?.message_id!==messageId)return;
        if(!['prepared','executing_or_unfinished'].includes(selectedStatus)){notice(selectedStatus==='completed'?'回答已保存，可以继续追问并查看原文依据。':selectedStatus==='abstained'?'本次证据不足，说明已保存。':'本次未得到可用回答，请查看保存的状态和回执。',!['completed','abstained'].includes(selectedStatus));await loadAnswers(false,runId);return;}
        await new Promise(resolve=>setTimeout(resolve,1500));
      }
      notice('等待窗口已结束，任务可能仍在执行或待核对。可手动刷新历史，请勿重复新建回答。',true);
    }catch(error){if(live(ownerEpoch))notice(error.message+' 请刷新当前回答核对；不会自动重试模型请求。',true);}
    finally{if(live(ownerEpoch)){busy=false;await refreshBudget();updateControls();}}
  }
  async function refreshPendingStatus(){
    if(!active||!identity||!current||!selectedRun||document.hidden||busy||statusRefreshBusy||selectedStatus!=='executing_or_unfinished')return;
    const ownerEpoch=epoch,run=selectedRun;statusRefreshBusy=true;
    try{
      await loadResult(run);
      if(live(ownerEpoch)&&selectedRun===run&&selectedStatus!=='executing_or_unfinished'){
        await loadAnswers(false,run);await refreshBudget();
        notice(selectedStatus==='completed'?'回答已保存，可以查看结果。':selectedStatus==='abstained'?'证据不足说明已保存。':'任务已结束，请查看最终状态与回执。',!['completed','abstained'].includes(selectedStatus));
      }
    }finally{statusRefreshBusy=false;}
  }
  const statusPoll=setInterval(refreshPendingStatus,5000);
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)refreshPendingStatus();});
  window.addEventListener('pagehide',event=>{if(!event.persisted)clearInterval(statusPoll);});
  async function createConversation(event) {
    event.preventDefault(); if (busy || (pending && pending.kind !== 'create')) return;
    if (!pending) {
      const title = $('#chat-title').value.trim(), person = $('#chat-person').value;
      if (!title || /[\x00-\x1f]/.test(title) || !validId(person)) { notice('请填写单行会话标题并选择监控人物。', true); return; }
      pending = {kind:'create',body:{client_request_id:crypto.randomUUID(),title,person_id:person}};
    }
    notice();
    await mutate(() => request('/conversations', {method:'POST',body:pending.body}), async (value, isCurrent) => {
      $('#chat-title').value = ''; $('#chat-new').open = false; notice('会话已保存。'); await loadConversations(); if (isCurrent()) await openConversation(value.conversation.conversation_id);
    });
  }
  $('#chat-retrieval-mode').addEventListener('change',updateControls);
  function searchRange() {
    const retrieval_mode=$('#chat-retrieval-mode').value;
    const research_mode=retrieval_mode==='hybrid'?$('#chat-research-mode').value:'auto';
    return {retrieval_mode,research_mode,use_history:$('#chat-use-history').checked,read_official_sources:retrieval_mode==='hybrid'&&$('#chat-read-official').checked,query:$('#chat-query').value.trim() || undefined, ...conversationDates($('#chat-source-since').value, $('#chat-source-until').value), limit:retrieval_mode==='hybrid'?6:3};
  }
  async function submitQuestion(event) {
    event.preventDefault(); if (busy || !current || current.archived || (pending && pending.kind !== 'question')) return;
    if (!pending) {
      const content = $('#chat-question').value;
      if (!content.trim() || content.includes('\0') || Array.from(content).length > 4000) { notice('请输入 1–4000 字符的问题。', true); return; }
      try { pending = {kind:'question',conversationId:current.conversation_id,message:{client_request_id:crypto.randomUUID(),content},prepare:{client_request_id:crypto.randomUUID(),...searchRange()},messageId:null}; }
      catch (error) { notice(error.message, true); return; }
    }
    const job = pending; notice();
    await mutate(async isCurrent => {
      if (!job.messageId) { const saved = await request(`/conversations/${job.conversationId}/messages`, {method:'POST',body:job.message}); job.messageId = saved.message.message_id; }
      if (!isCurrent()) throw new DOMException('已取消', 'AbortError');
      return await request(`/conversations/${job.conversationId}/answers`, {method:'POST',body:{...job.prepare,message_id:job.messageId}});
    }, async (run, isCurrent) => {
      $('#chat-question').value = ''; notice(run.execution_kind==='local_source_navigation'?'官方来源查找结果已保存；本次没有调用模型。':run.status === 'abstained' ? '问题已保存；本次未检索到足够原文，已保存证据不足说明。' : '问题与资料已保存。回答待生成，本次未调用模型。');
      await refreshConversation(job.messageId, run.run_id); if (isCurrent()) {await loadConversations();await refreshBudget();if(run.status==='prepared'&&budget?.can_generate)await generate(run.run_id);}
    });
  }
  async function refreshConversation(preferredQuestion = question?.message_id, preferredRun = selectedRun) {
    if (!current) return; const ownerEpoch = epoch, serial = ++conversationSerial, id = current.conversation_id;
    try {
      const value = await request(`/conversations/${id}`); if (!live(ownerEpoch) || serial !== conversationSerial || current?.conversation_id !== id) return;
      current = value; renderConversation();
      // Keep the loaded range and page forward, bounded by the server's 1000-question limit.
      let position = 0, next = null, loaded = [];
      do {
        const page = await request(`/conversations/${id}/messages`, {params:{limit:100,after:position}});
        if (!live(ownerEpoch) || serial !== conversationSerial || current?.conversation_id !== id) return;
        loaded.push(...page.items); next = page.next_after; position = next;
      } while (next !== null && loaded.length < Math.max(messages.length, preferredQuestion && !messages.some(item => item.message_id === preferredQuestion) ? current.message_count : 50));
      messages = loaded; after = next; renderMessages();
      const target = messages.some(item => item.message_id === preferredQuestion) ? preferredQuestion : messages.at(-1)?.message_id;
      if (target) await selectQuestion(target, preferredRun); else clearQuestion();
    } catch (error) { if (live(ownerEpoch) && serial === conversationSerial) notice(error.message, true); }
  }
  async function prepareAgain() {
    if (busy || !question || !current || current.archived || pending) return;
    const id = current.conversation_id, messageId = question.message_id; let range;
    try { range = searchRange(); } catch (error) { notice(error.message, true); return; }
    pending = {kind:'reprepare',conversationId:id,body:{client_request_id:crypto.randomUUID(),message_id:messageId,...range}};
    await retryPreparation();
  }
  async function retryPreparation() {
    const job = pending; if (!job || job.kind !== 'reprepare') return;
    await mutate(() => request(`/conversations/${job.conversationId}/answers`, {method:'POST',body:job.body}), async run => {
      notice('新版本资料已保存，不会覆盖旧回答；本次未请求模型。'); await selectQuestion(job.body.message_id,run.run_id);
    });
    if (pending?.kind === 'reprepare') notice('新版本准备结果待确认。点击“继续核对新版本”复用原标识重试；请勿重复新建。', true);
  }
  async function edit(body) {
    if (!current || pending || busy) return;
    const ownerEpoch = epoch, id = current.conversation_id; busy = true; updateControls();
    try {
      const value = await request(`/conversations/${id}`, {method:'PATCH',body:{expected_revision:current.revision,...body}});
      if (!live(ownerEpoch) || current?.conversation_id !== id) return;
      current = value; $('#chat-rename-form').hidden = true; renderConversation(); notice('会话设置已保存。'); await loadConversations();
    } catch (error) {
      if (live(ownerEpoch)) { notice(error.message + ' 请先刷新会话核对，修改不会自动重试。', true); await refreshConversation(); }
    } finally { if (live(ownerEpoch)) { busy = false; updateControls(); } }
  }
  $('#chat-file').onchange = async event => {
    const file = event.target.files?.[0]; event.target.value = ''; if (!file) return;
    if (file.size > 65536) { text('#chat-auth-status', '凭证文件过大，请选择会话凭证 JSON。'); return; }
    const ownerEpoch = epoch;
    try { const value = await file.text(); if (epoch === ownerEpoch) await connect(value); }
    catch { if (epoch === ownerEpoch) text('#chat-auth-status', '无法读取凭证文件，请重新选择。'); }
  };
  $('#chat-auth-form').onsubmit = event => { event.preventDefault(); const value = $('#chat-token').value; $('#chat-token').value = ''; connect(value); };
  $('#chat-disconnect').onclick = () => disconnect('已断开身份。重新导入后可继续查看已保存历史。');
  $('#chat-new-form').onsubmit = createConversation;
  $('#chat-abandon-create').onclick = () => { if (busy) return; pending = null; updateControls(); notice('已结束创建重试，请先在历史列表核对是否已保存。'); loadConversations(); };
  $('#chat-search-form').onsubmit = event => {
    event.preventDefault(); try { filters = {q:$('#chat-search').value.trim(),archived:$('#chat-archived').checked,...conversationDates($('#chat-since').value,$('#chat-until').value)}; loadConversations(); }
    catch (error) { notice(error.message,true); }
  };
  $('#chat-clear-search').onclick = () => { for (const selector of ['#chat-search','#chat-since','#chat-until']) $(selector).value = ''; $('#chat-archived').checked = false; filters = {}; loadConversations(); };
  $('#chat-list-refresh').onclick = () => loadConversations(); $('#chat-conversations-more').onclick = () => loadConversations(true);
  $('#chat-messages-more').onclick = moreMessages; $('#chat-refresh').onclick = () => refreshConversation();
  $('#chat-answer-version').onchange = event => loadResult(event.target.value); $('#chat-answers-more').onclick = () => loadAnswers(true);
  $('#chat-question-form').onsubmit = submitQuestion;
  $('#chat-reprepare').onclick = prepareAgain;
  $('#chat-generate').onclick = () => generate();
  $('#chat-budget-refresh').onclick = refreshBudget;
  $('#chat-stop-retry').onclick = () => {
    if (busy) return;
    pending = null; updateControls(); notice('已结束本页重试。后端可能已有记录，请先刷新问题与回答核对，再决定是否新增。'); refreshConversation();
  };
  $('#chat-retry-prepare').onclick = retryPreparation;
  $('#chat-archive').onclick = () => edit({archived:!current.archived});
  $('#chat-rename-toggle').onclick = () => { $('#chat-rename').value = current.title; $('#chat-rename-form').hidden = false; $('#chat-rename').focus(); };
  $('#chat-rename-cancel').onclick = () => { $('#chat-rename-form').hidden = true; };
  $('#chat-rename-form').onsubmit = event => { event.preventDefault(); const title = $('#chat-rename').value.trim(); if (title && !/[\x00-\x1f]/.test(title)) edit({title}); else notice('标题需为非空单行文字。',true); };
  $('#chat-quality-panel').ontoggle=()=>{
    if(!$('#chat-quality-panel').open||!identity)return;
    const ownerEpoch=epoch, serial=++qualitySerial;
    qualityReport($('#chat-quality-report'),{request,isCurrent:()=>live(ownerEpoch)&&serial===qualitySerial})().catch(()=>{});
  };
  disconnect();
  return {
    setPeople,
    show(preferred = '') { active = true; if (people.some(person => person.person_id === preferred)) $('#chat-person').value = preferred; if (identity) loadConversations(); },
    hide() { active = false; },
    async refresh() { if (!active || !identity) return; await refreshBudget();await loadConversations(); if (current) await refreshConversation(); },
    disconnect,
    async submitReminderFeedback(body) {
      if(!identity)throw new Error('请先在“人物问答”连接已有开发身份，再提交反馈；无需另注册账号。');
      return request('/reminder-feedback',{method:'POST',body});
    },
    dispose() { disconnect(); },
  };
}
