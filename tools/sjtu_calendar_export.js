/* ============================================================================
 *  奶我助 · 交我办日历导出脚本（极简版）
 * ----------------------------------------------------------------------------
 *  怎么用
 *    1. 浏览器登录 https://calendar.sjtu.edu.cn/ui/calendar （走 jAccount 登录）
 *    2. 在这张页面上按 F12 打开开发者工具，切到「控制台 / Console」
 *    3. 把本文件全部内容粘进去，回车
 *    4. 会下载一个 sjtu-calendar-<时间戳>.json，把它拖进「奶我助」网页的导入区
 *
 *  为什么不需要插件、不需要密码
 *    脚本在**这个页面自己的源里**运行，fetch 会自动带上你登录时拿到的
 *    jAccount 会话 cookie。脚本本身不登录、不读 cookie、不保存任何凭据，
 *    也没有把数据发给任何第三方——它只是把服务端返回的原始数据打包成文件。
 *
 *  如果想让它在打开页面时自动跑
 *    装一个用户脚本管理器（Tampermonkey 等），新建脚本，把本文件内容贴进去，
 *    在开头加上：
 *      // @match        https://calendar.sjtu.edu.cn/ui/calendar*
 *      // @run-at       document-idle
 *
 *  后端怎么认这个文件
 *    文件名和文件里的 exported_at 都带时间戳。奶我助按时间戳判断新旧，
 *    更旧的文件会被忽略，所以重复导入是安全的。
 * ========================================================================== */
(() => {
  'use strict';

  // ============================== 配置 ==============================
  const DAYS_BACK = 7;        // 往前抓几天（用来回看刚过去的安排）
  const DAYS_FORWARD = 90;    // 往后抓几天

  const ORIGIN = 'https://calendar.sjtu.edu.cn';

  // ============================== 小工具 ==============================
  const pad = n => String(n).padStart(2, '0');
  const ymd = d => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const stamp = d => `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}`
                   + `-${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}`;
  const isoLocal = d => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
                      + `T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
                      + (() => { const o = -d.getTimezoneOffset(); const s = o >= 0 ? '+' : '-';
                                 const a = Math.abs(o);
                                 return `${s}${pad(Math.floor(a / 60))}:${pad(a % 60)}`; })();

  /** 交我办的响应外层是 {success, msg, data}，这里剥掉外层。 */
  const unwrap = j => (j && typeof j === 'object' && 'data' in j &&
                       ('success' in j || 'msg' in j)) ? j.data : j;

  /** 按月切段，避免一次性区间太长被服务端截断。 */
  function monthChunks(from, to) {
    const out = [];
    let cur = new Date(from.getFullYear(), from.getMonth(), 1);
    while (cur <= to) {
      const s = new Date(cur.getFullYear(), cur.getMonth(), 1);
      const e = new Date(cur.getFullYear(), cur.getMonth() + 1, 0);
      out.push([s < from ? from : s, e > to ? to : e]);
      cur = new Date(cur.getFullYear(), cur.getMonth() + 1, 1);
    }
    return out;
  }

  /** 带 cookie 请求一个接口，返回剥好的 data。 */
  async function api(path) {
    const r = await fetch(ORIGIN + path, {
      credentials: 'include',                 // 关键：带上你的登录会话
      headers: { 'Accept': 'application/json, text/plain, */*' },
    });
    if (r.redirected && /login|jaccount/i.test(r.url)) {
      throw new Error('会话已过期或被重定向到登录页，请先重新登录再跑一次。');
    }
    const text = await r.text();
    let j;
    try { j = JSON.parse(text); }
    catch (e) {
      throw new Error(`返回的不是 JSON（HTTP ${r.status}）。大概率是登录已失效，` +
                      `请刷新页面确认还在登录状态。开头 120 字：${text.slice(0, 120)}`);
    }
    if (r.status !== 200) throw new Error(`HTTP ${r.status}：${JSON.stringify(j).slice(0, 200)}`);
    return unwrap(j);
  }

  // ============================== 主流程 ==============================
  async function main() {
    if (location.origin !== ORIGIN) {
      console.error(`[奶我助] 请先打开 ${ORIGIN}/ui/calendar 并登录，再在这里运行脚本。`);
      return;
    }

    const now = new Date();
    const from = new Date(now); from.setDate(from.getDate() - DAYS_BACK);
    const to = new Date(now); to.setDate(to.getDate() + DAYS_FORWARD);

    console.log('%c[奶我助] 开始读取交我办日历…', 'color:#1B3A6B;font-weight:bold');

    // 1) 有哪些日历
    const listData = await api('/api/calendar/list');
    const my = (listData && listData.my) || [];
    const share = (listData && listData.share) || [];
    console.log(`  日历列表：我的 ${my.length} 个，共享 ${share.length} 个`);

    // 2) 事件按月份段抓
    const chunks = monthChunks(from, to);
    const byId = new Map();
    let schoolCalendar = null;
    const errors = [];

    for (const [s, e] of chunks) {
      const qs = 'startDate=' + encodeURIComponent(ymd(s) + ' 00:00')
               + '&endDate=' + encodeURIComponent(ymd(e) + ' 23:59')
               + '&weekly=false&ids=';
      try {
        const d = await api('/api/event/list?' + qs);
        const evs = (d && d.events) || [];
        for (const ev of evs) {
          const key = ev.eventId != null ? String(ev.eventId) : JSON.stringify(ev);
          if (!byId.has(key)) byId.set(key, ev);
        }
        if (d && d.schoolCalendar && !schoolCalendar) schoolCalendar = d.schoolCalendar;
        console.log(`  ${ymd(s)} ~ ${ymd(e)}：${evs.length} 条`);
      } catch (err) {
        errors.push({ range: `${ymd(s)} ~ ${ymd(e)}`, error: String(err.message || err) });
        console.warn(`  ${ymd(s)} ~ ${ymd(e)} 抓取失败：${err.message || err}`);
      }
    }

    const events = [...byId.values()];

    // 记下第一条事件的字段名，方便后端/排查时确认接口有没有改版
    const keysSeen = events.length ? Object.keys(events[0]).sort() : [];

    const payload = {
      tool: '奶我助 · 交我办日历导出',
      version: 1,
      exported_at: isoLocal(new Date()),
      source: location.origin + location.pathname,
      range: { start: ymd(from), end: ymd(to), days_back: DAYS_BACK, days_forward: DAYS_FORWARD },
      calendars: { my, share },
      events,
      school_calendar: schoolCalendar,
      keys_seen: keysSeen,
      errors,
    };

    // 3) 下载
    const filename = `sjtu-calendar-${stamp(new Date())}.json`;
    const blob = new Blob([JSON.stringify(payload, null, 2)],
                          { type: 'application/json;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = filename;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60_000);

    // 万一浏览器拦了下载，把数据留在控制台里，还能手动救
    window.__naiwozhu_export = payload;

    console.log('%c[奶我助] 完成 ✓', 'color:#2E7D5B;font-weight:bold',
                `共 ${events.length} 条事件，已下载 ${filename}`);
    if (errors.length) console.warn('[奶我助] 有抓取失败的区间：', errors);
    console.log('  如果没看到下载，数据已放在 window.__naiwozhu_export，'
              + '执行下面这行可复制到剪贴板：');
    console.log('  copy(JSON.stringify(window.__naiwozhu_export))');
  }

  main().catch(err => {
    console.error('%c[奶我助] 出错：', 'color:#C0392B;font-weight:bold', err.message || err);
    console.error('  排查顺序：① 页面还在登录状态吗 ② 地址栏是 '
                + ORIGIN + '/ui/calendar 吗 ③ 网络面板里 /api/event/list 返回了什么');
  });
})();
