// Private requests are explicit and same-origin; execution requires a server grant.
const uuid = '[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}';
const conversation = `/conversations/${uuid}`;
const routes = {
  GET: new RegExp(`^/(?:me|generation-budget|quality-report|quality-cases/${uuid}|conversations|conversations/${uuid}(?:/messages|/answers(?:/${uuid}(?:/feedback)?)?)?)$`, 'i'),
  POST: new RegExp(`^(?:/reminder-feedback|/conversations|${conversation}/(?:messages|answers)|${conversation}/answers/${uuid}/(?:execute|feedback|quality-case))$`, 'i'),
  PATCH: new RegExp(`^${conversation}$`, 'i'),
};
export const validId = value => typeof value === 'string' && new RegExp(`^${uuid}$`, 'i').test(value);

export function readChatCredential(value) {
  if (typeof value !== 'string' || value.length > 65536) throw new Error('请选择有效的会话凭证 JSON 文件，或粘贴会话凭证。');
  let token = value.trim();
  if (token.startsWith('{')) {
    try { token = JSON.parse(token).access_token; } catch { token = null; }
  }
  if (typeof token !== 'string' || !/^pqchat_[A-Za-z0-9_-]{43}$/.test(token)) {
    throw new Error('这不是会话用户凭证。请使用 chat-credentials 中的凭证，不要填写模型 API Key 或数据库密码。');
  }
  return token;
}

export function conversationDates(since, until) {
  // Dates filter conversation creation, not statement publication. End is inclusive
  // in the form, converted to the next local calendar day for the exclusive API.
  const parse = value => {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) throw new Error('请选择有效的会话创建日期。');
    const [year, month, day] = value.split('-').map(Number);
    const date = new Date(year, month - 1, day);
    if (date.getFullYear() !== year || date.getMonth() !== month - 1 || date.getDate() !== day) throw new Error('请选择有效日期。');
    return date;
  };
  const start = since ? parse(since) : null, end = until ? parse(until) : null;
  if (start && end && start > end) throw new Error('开始日期不能晚于结束日期。');
  if (end) end.setDate(end.getDate() + 1);
  return { since: start?.toISOString(), until: end?.toISOString() };
}

export class ChatError extends Error {
  constructor(message, status = 0, uncertain = false) { super(message); this.name = 'ChatError'; this.status = status; this.uncertain = uncertain; }
}

export function createChatApi(base, credential, origin = globalThis.location.origin) {
  const address = new URL(base || origin);
  if (address.origin !== origin || address.pathname !== '/' || address.search || address.hash || address.username || address.password) {
    throw new Error('私有问答当前需要与后端同源。请在后端的 /monitor/ 打开，或通过同源反向代理整合。');
  }
  const token = readChatCredential(credential);
  return async (path, {method = 'GET', params = {}, body, signal} = {}) => {
    if (!routes[method]?.test(path)) throw new Error('不支持的会话操作。');
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) if (value !== '' && value !== null && value !== undefined) query.set(key, value);
    const controller = new AbortController();
    const cancel = () => controller.abort();
    if (signal?.aborted) cancel(); else signal?.addEventListener('abort', cancel, {once: true});
    const timer = setTimeout(cancel, method==='POST'&&path.endsWith('/answers')&&body?.read_official_sources?65000:15000);
    try {
      const response = await fetch(`${address.origin}/api/v1/agent-chat${path}${query.size ? `?${query}` : ''}`, {
        method, headers: {Accept: 'application/json', Authorization: `Bearer ${token}`, ...(body ? {'Content-Type': 'application/json'} : {})},
        body: body ? JSON.stringify(body) : undefined, signal: controller.signal,
        credentials: 'omit', cache: 'no-store', redirect: 'error', mode: 'same-origin', referrerPolicy: 'no-referrer',
      });
      if (!response.ok) {
        const messages = {401: '会话凭证已失效，请重新导入有效凭证。', 404: '会话或回答不存在，或不属于当前用户。',
          409: '会话或提交状态已变化，请刷新核对后再操作。', 422: '提交内容或范围不正确，请检查长度、人物与日期。',
          503: '会话服务暂不可用，请稍后核对历史记录。'};
        throw new ChatError(messages[response.status] || `会话请求失败（${response.status}）。`, response.status, method !== 'GET' && response.status >= 500);
      }
      return await response.json();
    } catch (error) {
      if (signal?.aborted) throw new DOMException('已取消', 'AbortError');
      if (error instanceof ChatError) throw error;
      throw new ChatError(method === 'GET' ? '暂时无法读取会话，请检查服务后重试。' : '保存结果尚未确认。请保留本页并重试核对，重复提交会复用原标识。', 0, method !== 'GET');
    } finally { clearTimeout(timer); signal?.removeEventListener('abort', cancel); }
  };
}
