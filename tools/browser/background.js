/* ============================================================================
 *  奶我助 · 扩展的后台
 * ----------------------------------------------------------------------------
 *  为什么要有这个文件
 *
 *    浏览器不允许**网页**随手往 http://127.0.0.1 写数据——那正是跨域规则在管的事。
 *    要放行，本地服务就得加 CORS 头；而只要加了，你浏览器里访问的任何一个网页
 *    都能往你的本地服务 POST，伪造日程或者把你的课表读走。
 *
 *    扩展的后台（service worker）不受网页那套跨域规则约束：manifest 里声明了
 *    host_permissions，它就能直接请求 127.0.0.1。所以跨域这一步放在这里做，
 *    本地服务**一行 CORS 都不用加**，那个风险面也就不存在。
 *
 *  内容脚本负责在日历页上把数据读出来，然后交给这里发出去。
 *  分工是刻意的：把"碰网络"这件事集中在后台，内容脚本只干取数这一件事。
 * ========================================================================== */

const TIMEOUT_MS = 20000;

chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  if (!msg || msg.type !== 'naiwozhu-sync') return;

  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), TIMEOUT_MS);

  fetch(msg.url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: msg.body,
    signal: ctl.signal,
  })
    .then(async r => {
      const text = await r.text();
      reply({ ok: r.ok, status: r.status, text });
    })
    .catch(e => {
      const aborted = e && e.name === 'AbortError';
      reply({ ok: false, error: aborted ? '本地服务没响应（超时）' : '连不上本地服务' });
    })
    .finally(() => clearTimeout(timer));

  return true;   // 异步回复，必须返回 true 才能保住这个通道
});
