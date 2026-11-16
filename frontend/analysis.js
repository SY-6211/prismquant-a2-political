// Read saved results only. This module never generates an analysis or calls a model.
const topics = {monetary_policy:'货币政策', fiscal_policy:'财政政策', trade_policy:'贸易政策', geopolitics:'地缘政治', regulation:'监管', company_mentions:'公司提及', other:'其他'};
const importance = {low:'低', medium:'中', high:'高', uncertain:'尚不确定'};
const statuses = {completed:'分析已保存', prepared_or_unfinished:'已准备 · 结果尚未确认', rejected:'未通过结果校验', failed:'本次分析失败', unknown:'执行结果未知'};
const historyNames = {not_assessed:'未作历史比较', new_vs_provided_history:'相对提供样本有新内容', changed_vs_provided_history:'相对提供样本有变化', repeats_provided_history:'与提供样本有重复'};
const roles = {main_text:'主文', quoted_text:'被引用内容', archive_media_note:'归档附件说明'};
function node(tag, className = '', text) {
  const item = document.createElement(tag);
  item.className = className;
  if (text !== undefined) item.textContent = text;
  return item;
}
function button(label, action) {
  const item = node('button', 'button secondary', label); item.type = 'button'; item.onclick = action; return item;
}
function when(value) {
  const date = new Date(value);
  return !value || Number.isNaN(date.valueOf()) ? '时间未知' : new Intl.DateTimeFormat('zh-CN', {dateStyle:'short', timeStyle:'short', hour12:false}).format(date);
}
function status(value) { return statuses[value] || '状态未知'; }
function sourceLink(label, address) {
  try {
    const url = new URL(address);
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) return null;
    const a = node('a', '', label); a.href = url.href; a.target = '_blank'; a.rel = 'noopener noreferrer'; return a;
  } catch { return null; }
}
function part(title, copy) {
  const section = node('section', 'analysis-section'); section.append(node('h3', '', title));
  if (copy) section.append(node('p', '', copy));
  return section;
}
function notice(text) { return node('p', 'analysis-notice', text); }
function evidence(citations, context) {
  const box = node('details', 'analysis-evidence');
  box.append(node('summary', '', `查看原文依据（${citations.length}）`));
  for (const quote of citations) {
    const source = context.sources?.find(s => s.source_ref === quote.source_ref && s.version_id === quote.version_id && s.event_id === quote.event_id);
    const checked = quote.valid === true && typeof quote.text === 'string' && !!source?.text?.includes(quote.text);
    const row = node('div', 'analysis-source');
    row.append(node('p', 'analysis-source-meta', `${source?.source_name || '来源未匹配'} · ${roles[source?.role] || '独立内容片段'}${source?.reported_author ? ` · 标注作者：${source.reported_author}` : ''}`));
    row.append(node('blockquote', '', quote.text));
    row.append(node('small', '', checked ? '引句已逐字校验；引句中的事实尚未独立核实。' : '此处引用状态未确认，需人工核对。'));
    if (source?.provenance_type === 'third_party_archive') row.append(node('small', '', '第三方归档，未验证原帖。'));
    if (source?.role !== 'main_text') row.append(node('small', '', '引用或附件内容不能直接归为监控人物本人发言。'));
    if (checked) {
      const links = node('div', 'analysis-source-links');
      const saved = sourceLink(source.provenance_type === 'official' ? '官方来源 ↗' : '归档来源 ↗', source.retrieved_url);
      const original = source.original_url !== source.retrieved_url ? sourceLink('原帖链接 ↗', source.original_url) : null;
      if (saved) links.append(saved); if (original) links.append(original); row.append(links);
    }
    box.append(row);
  }
  return box;
}
function renderResult(host, run, event, version, record) {
  const context = run.context;
  const banner = node('div', 'analysis-result-heading');
  banner.append(node('span', `tag ${run.status === 'completed' ? 'official' : 'archive'}`, status(run.status)), node('time', 'muted', when(run.result_created_at || run.created_at)));
  host.replaceChildren(banner);
  const target = context.sources?.find(s => s.event_id === run.event_id && s.version_id === run.version_id);
  host.append(node('p', 'analysis-target', `${record?.person_name_zh || target?.reported_author || '监控人物'} · 发言时间：${when(version.payload.published_at || target?.published_at)}\n${target?.source_name || '已保存来源'}`));
  // The legacy record body and latest observed event can differ. Never silently apply
  // an analysis of one snapshot to either of the other two bodies.
  if (event.version_id !== run.version_id) host.append(notice('这是较早版本的分析；该发言后来已有其他版本。本卡片保留当时结论，不代表最新原文。'));
  if (record && (version.payload.main_text !== record.body_text || version.source_id !== record.source_id)) host.append(notice('本分析对应的原文快照与“原文与来源”中的保存内容不同，请展开下方“分析时的原文快照”核对。'));
  if (run.execution_kind !== 'openrouter' && run.status === 'completed') host.append(notice(run.execution_kind === 'synthetic_fixture' ? '合成测试结果，不是真实模型分析。' : '外部提供的结果，执行来源未经验证。'));
  if (run.status !== 'completed') {
    const messages = {
      prepared_or_unfinished:'上下文已保存，但没有可展示的完成结果；不能据此判断模型请求是否执行或计费。',
      rejected:'本次输出未通过结构或引用校验，不展示为有效结论。原文仍可正常阅读。',
      failed:'本次执行失败，没有可展示的分析结论。原文仍可正常阅读。',
      unknown:'执行或保存结果尚不确定，需核对运行记录；页面不会重试模型请求。',
    };
    host.append(notice(messages[run.status] || '此记录状态尚不支持展示。'));
  } else {
    const a = run.analysis;
    host.append(notice(`${a.needs_review || a.human_review_status !== 'reviewed' ? '待人工核对。' : ''}AI 基于已保存的来源内容归纳；主题与重要程度是模型判断，尚未独立核实内容事实。`));
    if (!context.target_complete || !a.target_complete) host.append(notice('本次仅分析了部分正文，结论可能遗漏未读取内容。'));
    const tags = node('div', 'analysis-tags');
    tags.append(node('span', 'tag', `主题相关性：${a.relevant === true ? '相关' : a.relevant === false ? '不相关' : '尚不确定'}`));
    tags.append(node('span', 'tag', `重要程度：${importance[a.importance] || '尚不确定'}`));
    for (const topic of a.topics || []) tags.append(node('span', 'tag', topics[topic] || '未知主题'));
    host.append(tags, part('中文概括', a.summary_zh || '本次未提供概括。'));
    const facts = part('来源声称的内容', '以下归纳保留发言人或来源的说法，不作为已证实的市场事实。');
    for (const fact of a.facts || []) { const item = node('div', 'analysis-claim'); item.append(node('p', '', fact.text_zh), evidence(fact.citations || [], context)); facts.append(item); }
    if (!a.facts?.length) facts.append(node('p', 'muted', '本次没有提取可展示的声称。'));
    host.append(facts);
    const history = part('有限历史比较', historyNames[a.history_assessment] || '未作历史比较');
    history.append(node('p', '', context.history_refs?.length ? '仅与本次提供的至多一条较早发言比较，未检索人物全部历史。' : '本次没有提供历史发言，不能判断观点是否新增、重复或变化。'));
    if (a.comparison_zh) history.append(node('p', '', a.comparison_zh));
    if (a.comparison_citations?.length) history.append(evidence(a.comparison_citations, context));
    host.append(history);
    const hypotheses = part('有条件的研究假设', '仅供后续核对，不是价格预测或交易指令。');
    for (const hypothesis of a.hypotheses || []) { const item = node('div', 'analysis-claim'); item.append(node('p', '', hypothesis.text_zh), node('p', 'analysis-condition', `成立条件：${hypothesis.conditions_zh}`), evidence(hypothesis.citations || [], context)); hypotheses.append(item); }
    if (!a.hypotheses?.length) hypotheses.append(node('p', 'muted', '本次未提出研究假设。'));
    host.append(hypotheses);
  }
  const snapshot = node('details', 'analysis-snapshot');
  snapshot.append(node('summary', '', `分析时的原文快照 · 版本 ${version.version_number}`), node('p', 'muted', '保存的版本正文；模型实际读取范围以上方“部分正文”提示为准。'), node('p', 'analysis-original', version.payload.main_text || '本版本无主文。'));
  host.append(snapshot);
  const audit = node('details', 'analysis-audit');
  audit.append(node('summary', '', '分析记录信息'), node('p', '', `模型：${run.model_id}\n提示词版本：${run.prompt_version}\n分析准备时间：${when(run.created_at)}\n分析记录：${run.run_id}`));
  host.append(audit);
}

export function mountAnalysis(host, {api, record = null, event: suppliedEvent = null, runId = null}) {
  let disposed = false, listController, detailController, event, items = [], cursor = null, selected = runId;
  const title = node('div', 'analysis-header');
  title.append(node('div', '', 'AI 分析'), button('刷新分析记录', () => loadList()));
  const note = node('p', 'analysis-intro', '查看已保存的中文分析。此页面不会发起模型调用；新发言不会因此自动生成分析。');
  const message = node('p', 'analysis-message'); message.setAttribute('role', 'status');
  const controls = node('div', 'analysis-controls');
  const label = node('label', '', '这条发言的分析记录');
  const select = node('select'); select.setAttribute('aria-label', '选择分析记录'); label.append(select);
  const more = button('加载更早分析', () => loadList(true)); more.hidden = true;
  controls.append(label, more);
  const result = node('div', 'analysis-result'); result.setAttribute('aria-live', 'polite');
  host.replaceChildren(title, note, message, controls, result);
  function active(c) { return !disposed && !c.signal.aborted; }
  async function choose(id) {
    detailController?.abort(); const c = detailController = new AbortController(); selected = id;
    result.replaceChildren(node('p', 'analysis-message', '正在读取这次分析…')); select.value = id;
    try {
      const run = await api(`/api/v1/agent-analyses/${encodeURIComponent(id)}`, {}, c.signal);
      if (!active(c)) return;
      if (run.run_id !== id || run.event_id !== event.event_id || run.person_id !== (record?.person_id || event.person_id) || run.context?.version_id !== run.version_id || run.context?.event_id !== run.event_id || run.context?.person_id !== run.person_id) throw new Error('分析与当前人物或原文不匹配，已停止展示。');
      const version = await api(`/api/v1/events/${encodeURIComponent(run.event_id)}/versions/${encodeURIComponent(run.version_id)}`, {}, c.signal);
      if (!active(c)) return;
      if (version.version_id !== run.version_id || version.person_id !== run.person_id || (run.status === 'completed' && !run.analysis)) throw new Error('分析版本信息不完整，请核对后重试。');
      renderResult(result, run, event, version, record);
    } catch (error) { if (active(c)) result.replaceChildren(node('p', 'analysis-message is-error', error.message), button('重新读取这次分析', () => choose(id))); }
  }
  async function loadList(morePages = false) {
    listController?.abort(); detailController?.abort(); const c = listController = new AbortController();
    message.textContent = morePages ? '正在读取更早记录…' : '正在读取分析记录…'; controls.hidden = true;
    result.replaceChildren();
    try {
      event = suppliedEvent || await api(`/api/v1/public-records/${encodeURIComponent(record.record_id)}/event`, {}, c.signal);
      if (!active(c)) return;
      const page = await api('/api/v1/agent-analyses', {event_id:event.event_id, person_id:record?.person_id || event.person_id, limit:20, cursor:morePages ? cursor : null}, c.signal);
      if (!active(c)) return;
      if (page.items.some(item => item.event_id !== event.event_id || item.person_id !== (record?.person_id || event.person_id))) throw new Error('分析列表与当前人物或原文不匹配。');
      items = morePages ? [...new Map([...items, ...page.items].map(r => [r.run_id,r])).values()] : page.items;
      cursor = page.next_cursor;
      message.className = 'analysis-message';
      if (!items.length && !selected) { message.textContent = '这条发言尚无已保存的分析，可先阅读原文。'; return; }
      select.replaceChildren();
      for (const item of items) { const option = node('option', '', `${when(item.created_at)} · ${status(item.status)}`); option.value = item.run_id; select.append(option); }
      // A direct link can refer to a run outside the first history page.
      if (selected && !items.some(r => r.run_id === selected)) { const option = node('option', '', '指定的历史分析'); option.value = selected; select.prepend(option); }
      if (!selected) selected = items[0].run_id;
      controls.hidden = false; more.hidden = !cursor; message.textContent = `已加载 ${items.length} 次分析${cursor ? '，可加载更早记录' : ''}。`;
      await choose(selected);
    } catch (error) { if (active(c)) { message.className = 'analysis-message is-error'; message.textContent = `${error.message} 可继续阅读原文。`; result.replaceChildren(button('重新读取分析记录', () => loadList())); } }
  }
  select.onchange = () => choose(select.value);
  loadList();
  return {refresh:() => loadList(), dispose() { disposed = true; listController?.abort(); detailController?.abort(); }};
}

export function mountAnalysisHistory(host, {api, personId, scopeName, personName, open}) {
  let disposed = false, controller, cursor = null, busy = false;
  const automationController = new AbortController();
  const automation = node('div', 'analysis-automation');
  const automationText = node('p', 'analysis-intro', '正在读取自动分析状态…');
  const automationRefresh = button('刷新任务状态', loadAutomation);
  automation.append(automationText, automationRefresh);
  const title = node('p', 'analysis-intro', `${scopeName || '全部人物'}的已保存分析，按准备时间倒序。读取不会调用模型。`);
  const message = node('p', 'analysis-message'); message.setAttribute('role', 'status');
  const list = node('div', 'analysis-history-list');
  const more = button('加载更早记录', () => load(true)); more.hidden = true;
  host.replaceChildren(title, automation, message, list, more);
  async function loadAutomation() {
    automationRefresh.disabled = true;
    try {
      const value = await api('/api/v1/agent-analysis-automation', {}, automationController.signal);
      if (disposed) return;
      const policies = value.policies.filter(p => !personId || p.person_id === personId);
      const state = policies.some(p => p.status === 'needs_review') ? '费用待核对，已停止新生成' : policies.some(p => p.status === 'busy') ? '任务执行中或结果待核对' : policies.some(p => p.status === 'ready') ? '规则可用（持续工作进程尚未核对）' : '未启用或额度不可用';
      const jobs = value.jobs;
      automationText.textContent = `自动分析：${state}。全局任务：排队 ${(jobs.queued||0)+(jobs.preparing||0)+(jobs.prepared||0)}，执行 ${jobs.running||0}，已完成 ${jobs.completed||0}，缓存 ${jobs.cached||0}，失败 ${(jobs.failed||0)+(jobs.rejected||0)}，费用待核对 ${jobs.unknown||0}。查看状态不会调用模型。`;
    } catch { if (!disposed) automationText.textContent = '自动分析状态暂不可用；已保存分析仍可阅读。'; }
    finally { if (!disposed) automationRefresh.disabled = false; }
  }
  async function load(append = false) {
    if (busy) return;
    busy = true; more.disabled = true; controller?.abort(); const c = controller = new AbortController();
    message.textContent = '正在读取分析历史…';
    try {
      const page = await api('/api/v1/agent-analyses', {person_id:personId, limit:20, cursor:append ? cursor : null}, c.signal);
      if (disposed || c.signal.aborted) return;
      if (!append) list.replaceChildren();
      for (const item of page.items) {
        const row = node('button', 'analysis-history-row'); row.type = 'button'; row.dataset.analysisRun = item.run_id;
        row.append(node('span', 'analysis-source-meta', `${personName?.(item.person_id) || '监控人物'} · ${when(item.created_at)} · ${status(item.status)}`));
        row.append(node('strong', '', item.status === 'completed' ? item.summary_zh || '查看中文分析' : status(item.status)));
        row.append(node('span', 'read-link', '查看这次分析 ↗'));
        row.onclick = async () => { row.disabled = true; message.textContent = '正在打开分析…'; try { await open(item); } catch (error) { if (!disposed) message.textContent = error.message; } finally { row.disabled = false; } };
        list.append(row);
      }
      cursor = page.next_cursor; more.hidden = !cursor;
      message.textContent = list.childElementCount ? '' : '当前人物还没有保存的分析。';
    } catch (error) { if (!disposed && !c.signal.aborted) { message.textContent = `${error.message} 请关闭后重新打开。`; } }
    finally { busy = false; more.disabled = false; }
  }
  if(document.querySelector('#pq-monitor')?.dataset.console!=='operations')automation.hidden=true;
  load(); if(!automation.hidden)loadAutomation();
  return {dispose() { disposed = true; controller?.abort(); automationController.abort(); }};
}
