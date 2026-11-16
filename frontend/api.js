// Standalone request layer. Reuse this module when integrating into another frontend.
export function normalizeBase(value) {
  if (!value.trim()) return '';
  const url = new URL(value.trim());
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash || url.pathname !== '/') {
    throw new Error('请填写不带路径、账号或查询参数的 HTTP(S) 后端地址。');
  }
  return url.origin;
}

export function createApi(base = '') {
  return async function request(path, params = {}, signal) {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined && value !== null && value !== '') query.set(key, value);
    }
    const timeout = new AbortController();
    const onAbort = () => timeout.abort();
    if (signal?.aborted) timeout.abort();
    else signal?.addEventListener('abort', onAbort, { once: true });
    const timer = setTimeout(() => timeout.abort(), 15000);
    try {
      const response = await fetch(`${base}${path}${query.size ? `?${query}` : ''}`, {
        headers: { Accept: 'application/json' }, signal: timeout.signal,
      });
      if (!response.ok) {
        const body = await response.json().catch(() => null);
        const messages = {404: '记录不存在或已被移除。', 422: '请求参数不正确，请刷新页面后重试。', 503: '数据库暂不可用，请稍后重试。'};
        throw new Error(body?.detail?.message || messages[response.status] || `请求失败（${response.status}）`);
      }
      return await response.json();
    } catch (error) {
      if (signal?.aborted) throw new DOMException('Cancelled', 'AbortError');
      if (error.name === 'AbortError') throw new Error('请求超时，请检查后端服务。');
      if (error instanceof TypeError) throw new Error('无法连接后端，请检查服务地址、跨域设置及网络。');
      throw error;
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener('abort', onAbort);
    }
  };
}
