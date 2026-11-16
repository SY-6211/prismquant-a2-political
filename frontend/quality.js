// Personal feedback is private; reference labels require separate human reviewers.
const kinds = {useful:'有用',unsupported:'结论缺乏原文支持',wrong_person:'人物或引述归属错误',time_error:'时间或新旧信息错误',missed_context:'遗漏相关资料',irrelevant:'答非所问',other:'其他问题'};
const states = {unreviewed:'待独立复核',provisional:'仅一人复核',consensus:'多人判断一致',disputed:'判断有争议'};
const el = (tag, cls='', text) => {const n=document.createElement(tag);n.className=cls;if(text!==undefined)n.textContent=text;return n;};
function button(text, action) {const b=el('button','button secondary',text);b.type='button';b.onclick=action;return b;}
function download(value, name) {const url=URL.createObjectURL(new Blob([JSON.stringify(value,null,2)],{type:'application/json'}));const a=el('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}

export function answerFeedback(host, run, {request, isCurrent}) {
  if (!run.result_id) return;
  const box=el('details','chat-quality answer-feedback');box.append(el('summary','','评价这条回答 / 查看反馈'));
  const history=el('div','quality-history'), form=el('form','quality-form'), status=el('p','muted');status.setAttribute('role','status');
  const select=el('select');select.setAttribute('aria-label','回答评价类型');for(const [value,label] of Object.entries(kinds)){const o=el('option','',label);o.value=value;select.append(o);}
  const note=el('textarea');note.rows=3;note.maxLength=1000;note.required=true;note.placeholder='说明哪里有帮助，或指出原文、时间、遗漏资料。';note.setAttribute('aria-label','评价说明');
  const send=el('button','button secondary','保存反馈');send.type='submit';form.append(select,note,send);
  const base=`/conversations/${run.conversation_id}/answers/${run.run_id}`;let loaded=false,pending=null,writing=false;
  async function load() {try {const value=await request(`${base}/feedback`);if(!isCurrent()||!box.isConnected)return;history.replaceChildren(el('p','muted','你的反馈只用于待办复核，不会自动改原文、规则或模型。'));
    if(!value.items.length)history.append(el('p','muted','这条回答还没有你的反馈。'));
    for(const item of value.items){const row=el('article','quality-feedback-item');row.append(el('strong','',kinds[item.kind]||'反馈'),el('p','chat-prewrap',item.note));history.append(row);}loaded=true;
  }catch(error){if(isCurrent()&&box.isConnected)status.textContent=error.message;}}
  box.ontoggle=()=>{if(box.open&&!loaded)load();};
  form.onsubmit=async event=>{event.preventDefault();if(writing||!isCurrent()||!box.isConnected)return;
    const body=pending||{client_request_id:crypto.randomUUID(),kind:select.value,note:note.value.trim()};if(!body.note){status.textContent='请填写评价说明。';return;}
    pending=body;writing=true;send.disabled=true;select.disabled=note.disabled=true;status.textContent='正在保存反馈…';
    try{await request(`${base}/feedback`,{method:'POST',body});if(!isCurrent()||!box.isConnected)return;pending=null;note.value='';status.textContent='反馈已保存；读取历史和反馈不会调用模型。';send.textContent='保存反馈';await load();}
    catch(error){if(isCurrent()&&box.isConnected){status.textContent=error.message;if(!error.uncertain)pending=null;send.textContent=pending?'核对并重试同一反馈':'保存反馈';}}
    finally{writing=false;if(isCurrent()&&box.isConnected){send.disabled=false;select.disabled=note.disabled=!!pending;}}
  };
  box.append(history,form,status);
  if(['completed','abstained'].includes(run.status)&&run.answer){
    const evaluation=el('div','quality-case');evaluation.append(el('p','muted','独立评测会冻结本条问题、回答、回答当时参考的相关对话片段和最多12份同人物原文，供已授权标注员查看；不是训练或模型调用。'));
    const label=el('label','chat-check'),consent=el('input');consent.type='checkbox';label.append(consent,document.createTextNode('允许已授权标注员复核本条问题、回答、相关对话片段及原文'));let freezing=false;
    const freeze=button('建立独立评测案例',async()=>{if(freezing||!isCurrent()||!box.isConnected)return;if(!consent.checked){status.textContent='请先确认本条评测资料的复核范围。';return;}
      freezing=true;freeze.disabled=true;
      try{const value=await request(`${base}/quality-case`,{method:'POST',body:{share_with_reviewers:true}});if(!isCurrent()||!box.isConnected)return;
        status.textContent=`评测案例已保存：${value.case_id}。需要独立人工标注后才有质量指标。`;
        evaluation.append(button('下载本条评测快照',()=>{if(isCurrent())download(value,`person-quality-${value.case_id}.json`);}));freeze.hidden=true;
      }catch(error){if(isCurrent()&&box.isConnected)status.textContent=error.message;}
      finally{freezing=false;if(isCurrent()&&box.isConnected)freeze.disabled=false;}
    });evaluation.append(label,freeze);box.append(evaluation);
  }
  host.append(box);
}

export function qualityReport(host,{request,isCurrent}) {
  let serial=0;
  return async()=>{const n=++serial;host.replaceChildren(el('p','muted','正在读取本人质量记录…'));
    try{const value=await request('/quality-report');if(!isCurrent()||n!==serial||!host.isConnected)return;
      host.replaceChildren(el('h3','','回答质量与复核进度'),el('p','chat-review','自动检查只核对引用、位置和保存哈希；不代表结论事实正确。未完成独立复核和真实时效验收，暂不打最终总分。'));
      const auto=value.automatic_checks, real=value.human_consensus_metrics;
      host.append(el('p','',`最近 ${value.sampled_answers} 个已结束结果${value.answer_window_truncated?'（还有更早结果）':''}：自动检查通过 ${auto.passed}，部分可检查 ${auto.partial||0}，异常 ${auto.failed}，无可检查回答 ${auto.no_answer}。`));
      if(auto.partial)host.append(el('p','muted','旧模型回执哈希覆盖完整响应，无法由解析后的回答重算；早期提示词也可能不在当前注册表。已检查的引用与位置和不可检查项目分开记录。'));
      host.append(el('p','',`你已评价 ${value.personal_feedback.distinct_answers} 条回答；双人或多人一致复核的真实案例 ${real.cases} 条。`));
      const labels={precision_in_pool:'候选范围内检索精确率',recall_in_pool:'候选范围内检索召回率',supported:'原文支持',person_correct:'人物归属',time_correct:'时间判断',useful:'研究用途'};
      const list=el('dl','quality-metrics');for(const [key,label] of Object.entries(labels)){const metric=real[key];list.append(el('dt','',label),el('dd','',metric?.value==null?'待独立人工复核':`${(metric.value*100).toFixed(1)}%（${metric.defined_cases} 个案例）`));}host.append(list);
      for(const [kind,count] of Object.entries(value.personal_feedback.latest_kind_counts))host.append(el('p','muted',`${kinds[kind]||kind}：${count} 条（每条回答仅统计最后一次反馈）`));
      if(!value.cases.length)host.append(el('p','muted','尚未建立问答评测案例。可在具体回答下展开“评价这条回答”，确认后建立。'));
      for(const item of value.cases){const row=el('div','quality-case-row');row.append(el('span','',`${states[item.state]||item.state} · ${item.reviewers} 人 · ${item.prompt_version} · ${item.candidate_count} 份候选`),button('下载评测快照',async()=>{try{const packet=await request(`/quality-cases/${item.case_id}`);if(isCurrent()&&host.contains(row))download(packet,`person-quality-${item.case_id}.json`);}catch(error){if(isCurrent()&&host.contains(row))row.append(el('p','error-text',error.message));}}));host.append(row);}
      host.append(el('p','muted',`关键词开发语料 ${value.keyword_corpus_progress.source_samples} 条真实来源快照；人工提交 ${value.keyword_corpus_progress.review_submissions} 次（提交次数不等于已认证样本）。漏报需在既有标注数据集对正例核对后统计。`));
      host.append(button('下载本人质量报告',()=>{if(isCurrent())download(value,'person-quality-report.json');}));
    }catch(error){if(isCurrent()&&n===serial)host.replaceChildren(el('p','error-text',error.message));}
  };
}
