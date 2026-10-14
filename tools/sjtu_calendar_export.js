// ==UserScript==
// @name         奶我助 · 交我办日历同步
// @namespace    naiwozhu
// @version      1.0.0
// @description  把交我办日历的日程推给本机运行的「奶我助」，打开页面就自动同步
// @match        https://calendar.sjtu.edu.cn/*
// @grant        GM_xmlhttpRequest
// @grant        GM_getValue
// @grant        GM_setValue
// @connect      127.0.0.1
// @connect      localhost
// @run-at       document-idle
// ==/UserScript==
/* ============================================================================
 *  奶我助 · 交我办日历同步
 * ----------------------------------------------------------------------------
 *  一个文件，两种跑法
 *
 *   ① 装进 Tampermonkey（推荐，装一次，以后零点击）
 *      装好用户脚本管理器，把本文件装进去。之后你只要打开
 *      https://calendar.sjtu.edu.cn/ui/calendar，脚本自己读数据、
 *      自己推给本机运行的「奶我助」。右下角有个小面板显示结果，
 *      也可以手动点「立即同步」。
 *
 *   ② 贴进控制台（不想装扩展时用）
 *      在交我办日历页面上按 F12 → Console，把本文件全部内容粘进去回车。
 *      会下载一个 sjtu-calendar-<时间戳>.json，拖进「奶我助」网页。
 *
 *  两种跑法的区别只在**最后一步数据送到哪**：
 *      装了管理器 → GM_xmlhttpRequest 直接 POST 给本机服务（不受 CORS 限制）
 *      贴在控制台 → 浏览器不给网页随便写本机服务，所以落成一个文件让你拖
 *
 *  为什么不需要密码、也不需要抓 cookie
 *    脚本在你**已经登录的这个页面自己的源里**发请求，浏览器自动带上会话。
 *    脚本不登录、不读也不导出 cookie、不碰密码，也不把数据发给任何第三方
 *    （推送目标是 127.0.0.1，就是你自己电脑上的奶我助）。
 *
 *  数据不新鲜怎么办
 *    脚本和「奶我助」都按 exported_at 判断新旧，旧数据会被忽略。
 *    重复推送是安全的，所以自动同步不需要很小心。
 * ========================================================================== */
(() => {
  'use strict';

  // ============================== 配置 ==============================
  const SYNC_URL = 'http://127.0.0.1:5173/api/import';  // 改端口就改这里
  const DAYS_BACK = 7;         // 往前抓几天（用来回看刚过去的安排）
  const DAYS_FORWARD = 90;     // 往后抓几天
  const MIN_INTERVAL_MS = 30 * 60 * 1000;   // 自动同步的最短间隔
  const STATE_KEY = 'naiwozhu.lastSync';

  const ORIGIN = location.origin;

  // 装了用户脚本管理器时这两个才有定义；贴在控制台里就是 undefined
  const hasGM = typeof GM_xmlhttpRequest === 'function';

  // 贴在控制台时不去动交我办页面自己的 localStorage，只在内存里记
  const mem = {};
  const store = {
    get(k, d) { try { return hasGM ? GM_getValue(k, d) : (k in mem ? mem[k] : d); }
                catch (e) { return d; } },
    set(k, v) { try { hasGM ? GM_setValue(k, v) : (mem[k] = v); } catch (e) {} },
  };

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
  const hhmm = d => `${pad(d.getHours())}:${pad(d.getMinutes())}`;

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
      throw new Error('登录已失效，请先在这个页面重新登录。');
    }
    const text = await r.text();
    let j;
    try { j = JSON.parse(text); }
    catch (e) {
      throw new Error(`返回的不是 JSON（HTTP ${r.status}）。大概率是登录已失效，`
                    + `请刷新页面确认还在登录状态。开头 120 字：${text.slice(0, 120)}`);
    }
    if (r.status !== 200) throw new Error(`HTTP ${r.status}：${JSON.stringify(j).slice(0, 200)}`);
    return unwrap(j);
  }

  // ============================== 采集 ==============================
  /** 读交我办日历，返回可以直接交给后端的 payload。 */
  async function collect() {
    const now = new Date();
    const from = new Date(now); from.setDate(from.getDate() - DAYS_BACK);
    const to = new Date(now); to.setDate(to.getDate() + DAYS_FORWARD);

    // 1) 有哪些日历
    const listData = await api('/api/calendar/list');
    const my = (listData && listData.my) || [];
    const share = (listData && listData.share) || [];

    // 2) 事件按月分段抓
    const byId = new Map();
    let schoolCalendar = null;
    const errors = [];

    for (const [s, e] of monthChunks(from, to)) {
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
      } catch (err) {
        errors.push({ range: `${ymd(s)} ~ ${ymd(e)}`, error: String(err.message || err) });
      }
    }

    const events = [...byId.values()];
    const finishedAt = new Date();

    return {
      filename: `sjtu-calendar-${stamp(finishedAt)}.json`,
      payload: {
        tool: '奶我助 · 交我办日历导出',
        version: 1,
        exported_at: isoLocal(finishedAt),
        source: ORIGIN + location.pathname,
        range: { start: ymd(from), end: ymd(to),
                 days_back: DAYS_BACK, days_forward: DAYS_FORWARD },
        calendars: { my, share },
        events,
        school_calendar: schoolCalendar,
        // 记下第一条事件的字段名，方便后端/排查时确认接口有没有改版
        keys_seen: events.length ? Object.keys(events[0]).sort() : [],
        errors,
      },
    };
  }

  // ============================== 推送 ==============================
  /**
   * 交给本机的「奶我助」。
   *
   * 这里用 GM_xmlhttpRequest，由用户脚本管理器代发，不受网页自身的跨域限制，
   * 所以本地服务**一行 CORS 都不用加**。
   * 不加 CORS，就没有"随便哪个网页都能往你本地服务写数据"这个风险。
   */
  function pushToLocal(body) {
    return new Promise((resolve, reject) => {
      GM_xmlhttpRequest({
        method: 'POST',
        url: SYNC_URL,
        headers: { 'Content-Type': 'application/json' },
        data: body,
        timeout: 20000,
        onload: r => resolve(r),
        onerror: () => reject(new Error('连不上本地服务')),
        ontimeout: () => reject(new Error('本地服务没响应（超时）')),
      });
    });
  }

  async function sync() {
    const { payload, filename } = await collect();
    const r = await pushToLocal(JSON.stringify({ payload, filename }));

    let d = {};
    try { d = JSON.parse(r.responseText); } catch (e) {}
    if (r.status !== 200 || !d.ok) {
      throw new Error(d.error || `本地服务返回 HTTP ${r.status}`);
    }
    store.set(STATE_KEY, Date.now());
    return { count: payload.events.length, message: d.message || '已同步', stale: !!d.stale };
  }

  // ============================== 下载（控制台模式的出口）================
  function download({ payload, filename }) {
    const blob = new Blob([JSON.stringify(payload, null, 2)],
                          { type: 'application/json;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = filename;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60_000);
    // 万一浏览器拦了下载，把数据留在控制台里，还能手动救
    window.__naiwozhu_export = payload;
  }

  // ============================== 面板（只在装了管理器时出现）=============
  let ui = null;

  function mountPanel() {
    const host = document.createElement('div');
    host.style.cssText = 'position:fixed;right:18px;bottom:18px;z-index:2147483647';
    // 用 Shadow DOM 隔开样式，免得和交我办自己的 CSS 互相影响
    const root = host.attachShadow({ mode: 'open' });
    root.innerHTML = `
      <style>
        .box{font:13px/1.6 -apple-system,"Noto Sans CJK SC","Microsoft YaHei",sans-serif;
             background:#fff;border:1px solid #E8EBF0;border-radius:10px;
             box-shadow:0 4px 18px rgba(20,30,50,.12);width:252px;overflow:hidden;color:#15191F}
        .h{display:flex;align-items:center;gap:7px;padding:9px 12px;background:#1B3A6B;color:#fff;
           font-weight:600;font-size:12.5px;cursor:pointer;user-select:none}
        .h .sp{flex:1}
        .h .min{opacity:.7;font-weight:400}
        .b{padding:11px 12px}
        .s{font-size:12.5px;color:#6B7480;line-height:1.65;min-height:34px;white-space:pre-wrap;
           word-break:break-word}
        .s.err{color:#C0392B}
        .s.ok{color:#2E7D5B}
        .btns{display:flex;gap:7px;margin-top:9px}
        button{font:inherit;font-size:12.5px;font-weight:600;padding:6px 11px;border-radius:6px;
               border:1px solid #E8EBF0;background:#fff;color:#1B3A6B;cursor:pointer}
        button:hover{border-color:#1B3A6B;background:#EDF2F9}
        button[disabled]{opacity:.5;cursor:default}
        button.p{background:#1B3A6B;color:#fff;border-color:#1B3A6B}
        button.p:hover{background:#122747}
        .f{padding:7px 12px;border-top:1px solid #F1F4F8;font-size:11.5px;color:#9AA2AE}
      </style>
      <div class="box">
        <div class="h"><span>📅</span><span>奶我助</span><span class="sp"></span>
          <span class="min" data-a="toggle">收起</span></div>
        <div class="b">
          <div class="s" data-r="status">准备中…</div>
          <div class="btns">
            <button class="p" data-a="sync">立即同步</button>
            <button data-a="dl">下载 JSON</button>
          </div>
        </div>
        <div class="f" data-r="foot"></div>
      </div>`;
    document.body.appendChild(host);

    const q = s => root.querySelector(s);
    const panel = {
      set(text, cls) {
        const n = q('[data-r="status"]');
        n.className = 's' + (cls ? ' ' + cls : '');
        n.textContent = text;
      },
      setFoot(text) { q('[data-r="foot"]').textContent = text; },
      setBusy(on) {
        root.querySelectorAll('.btns button').forEach(b => { b.disabled = on; });
      },
    };

    const last = Number(store.get(STATE_KEY, 0)) || 0;
    panel.setFoot(last ? `上次同步 ${hhmm(new Date(last))}` : '还没同步过');

    root.addEventListener('click', async e => {
      const b = e.target.closest('[data-a]');
      if (!b) return;

      if (b.dataset.a === 'toggle') {
        const hidden = q('.b').style.display === 'none';
        q('.b').style.display = hidden ? '' : 'none';
        q('[data-r="foot"]').style.display = hidden ? '' : 'none';
        b.textContent = hidden ? '收起' : '展开';
        return;
      }

      if (b.dataset.a === 'dl') {
        panel.setBusy(true);
        panel.set('正在读取…');
        try {
          download(await collect());
          panel.set('已下载 JSON，拖进「奶我助」网页即可', 'ok');
        } catch (err) {
          panel.set(hint(err), 'err');
        } finally {
          panel.setBusy(false);
        }
        return;
      }

      if (b.dataset.a === 'sync') {
        panel.setBusy(true);
        panel.set('正在读取并同步…');
        try {
          report(await sync());
        } catch (err) {
          panel.set(hint(err), 'err');
        } finally {
          panel.setBusy(false);
        }
      }
    });

    return panel;
  }

  /** 面板上统一的结果措辞。 */
  function report(r) {
    if (!ui) return;
    ui.setFoot(`上次同步 ${hhmm(new Date())}`);
    ui.set(r.stale ? `本地已有更新的数据，这次跳过（读到 ${r.count} 条）`
                   : `✅ ${r.message}\n共 ${r.count} 条日程`,
           r.stale ? '' : 'ok');
  }

  /** 本地服务没开是常态，措辞别吓人。 */
  function hint(err) {
    const m = String((err && err.message) || err);
    if (/连不上本地服务|超时|Failed to fetch|NetworkError/i.test(m)) {
      return '连不上本地服务。\n先把「奶我助」跑起来（bash app/run.sh），再点「立即同步」。';
    }
    return m;
  }

  // ============================== 自动同步 ==============================
  async function maybeAutoSync() {
    const last = Number(store.get(STATE_KEY, 0)) || 0;
    const left = MIN_INTERVAL_MS - (Date.now() - last);
    if (left > 0) {
      ui.set(`距上次同步不到 ${MIN_INTERVAL_MS / 60000} 分钟，这次跳过。\n`
           + `想立刻同步就点下面的按钮（约 ${Math.ceil(left / 60000)} 分钟后会自动再试）。`);
      return;
    }
    ui.set('正在读取并同步…');
    try {
      report(await sync());
    } catch (err) {
      ui.set(hint(err), 'err');
    }
  }

  // ============================== 入口 ==============================
  async function main() {
    if (!/(^|\.)sjtu\.edu\.cn$/i.test(location.hostname)) {
      console.warn('[奶我助] 这个脚本是给 calendar.sjtu.edu.cn 用的，'
                 + '当前页面是 ' + location.hostname + '，接口大概率读不到。');
    }

    if (!hasGM) {
      // ---------- 控制台模式：下载文件 ----------
      console.log('%c[奶我助] 开始读取交我办日历…', 'color:#1B3A6B;font-weight:bold');
      const out = await collect();
      download(out);
      const n = out.payload.events.length;
      console.log('%c[奶我助] 完成 ✓', 'color:#2E7D5B;font-weight:bold',
                  `共 ${n} 条事件，已下载 ${out.filename}`);
      if (out.payload.errors.length) {
        console.warn('[奶我助] 有抓取失败的区间：', out.payload.errors);
      }
      console.log('  tip：装一个用户脚本管理器（Tampermonkey）装上本脚本，'
                + '以后打开这个页面就会自动同步，不用再贴代码、也不用拖文件。');
      console.log('  如果没看到下载，数据已放在 window.__naiwozhu_export，'
                + '执行下面这行可复制到剪贴板：');
      console.log('  copy(JSON.stringify(window.__naiwozhu_export))');
      return;
    }

    // ---------- 用户脚本模式：自动同步 ----------
    ui = mountPanel();
    setTimeout(maybeAutoSync, 1500);
  }

  main().catch(err => {
    const msg = (err && err.message) || String(err);
    console.error('%c[奶我助] 出错：', 'color:#C0392B;font-weight:bold', msg);
    console.error('  排查顺序：① 页面还在登录状态吗 ② 地址栏是 '
                + ORIGIN + '/ui/calendar 吗 ③ 网络面板里 /api/event/list 返回了什么');
    if (ui) ui.set(hint(err), 'err');
  });
})();
