/* WDTT Mini App — SPA на Telegram WebApp API */
(function () {
  'use strict';
  // Telegram WebApp SDK может появиться/наполниться с задержкой (особенно при
  // открытии из меню чатов), поэтому tg — переменная, а не константа.
  let tg = window.Telegram && window.Telegram.WebApp;
  if (tg) { tg.ready(); tg.expand(); }

  const app = document.getElementById('app');
  const toastEl = document.getElementById('toast');
  let token = sessionStorage.getItem('wdtt_token') || '';
  let me = null;
  let plansData = null;
  let view = 'home';

  function toast(msg) {
    toastEl.textContent = msg;
    toastEl.classList.add('show');
    clearTimeout(toast._t);
    toast._t = setTimeout(() => toastEl.classList.remove('show'), 2500);
  }

  async function api(path, opts = {}) {
    const headers = { 'Content-Type': 'application/json' };
    if (token) headers['Authorization'] = 'Bearer ' + token;
    const r = await fetch(path, { ...opts, headers: { ...headers, ...(opts.headers || {}) } });
    if (r.status === 401 && path !== '/api/auth') {
      token = '';
      sessionStorage.removeItem('wdtt_token');
      await auth();
      throw new Error('session expired');
    }
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || ('HTTP ' + r.status));
    return data;
  }

  // Ждём, пока Telegram инициализирует WebApp и отдаст initData.
  // При открытии из меню чатов (Web App shortcut) данные могут прийти
  // на 0.5–2 секунды позже, чем появится сам SDK.
  function waitForInitData(timeoutMs) {
    return new Promise((resolve) => {
      const started = Date.now();
      (function check() {
        // SDK мог подгрузиться позже — обновляем ссылку и просим Telegram
        // отдать данные о пользователе.
        if (!tg && window.Telegram && window.Telegram.WebApp) {
          tg = window.Telegram.WebApp;
          try { tg.ready(); tg.expand(); } catch (_) { /* ignore */ }
        }
        if (tg && tg.initData) return resolve(tg.initData);
        if (Date.now() - started >= timeoutMs) return resolve('');
        setTimeout(check, 150);
      })();
    });
  }

  async function auth(attempt) {
    attempt = attempt || 0;
    const initData = tg ? tg.initData : '';
    const data = await api('/api/auth', { method: 'POST', body: JSON.stringify({ initData }) });
    token = data.token;
    sessionStorage.setItem('wdtt_token', token);
    window.__starsEnabled = false;
    window.__apps = (data.ecosystem && data.ecosystem.apps) || {};
    return data.user;
  }

  // ВАЖНО: все данные, приходящие с сервера, вставляются через esc() —
  // innerHTML используется только для собственной разметки приложения.
  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&' + 'amp;')
      .replace(/</g, '&' + 'lt;')
      .replace(/>/g, '&' + 'gt;')
      .replace(/"/g, '&' + 'quot;')
      .replace(/'/g, '&' + '#39;');
  }

  function fmtDate(ts) {
    if (!ts) return '—';
    const d = new Date(ts * 1000);
    return d.toLocaleDateString('ru-RU', { day: 'numeric', month: 'long', year: 'numeric' });
  }

  function fmtBytes(n) {
    if (n >= 1024 ** 3) return (n / 1024 ** 3).toFixed(1) + ' ГБ';
    if (n >= 1024 ** 2) return (n / 1024 ** 2).toFixed(1) + ' МБ';
    if (n >= 1024) return (n / 1024).toFixed(1) + ' КБ';
    return n + ' Б';
  }

  function daysLeft(exp) {
    const d = Math.ceil((exp - Date.now() / 1000) / 86400);
    if (d <= 0) return 'истекла';
    if (d === 1) return '1 день';
    if (d < 5) return d + ' дня';
    return d + ' дней';
  }

  function copy(text) {
    if (navigator.clipboard) navigator.clipboard.writeText(text).then(() => toast('Скопировано ✅'));
    else toast(text);
    if (tg) tg.HapticFeedback.notificationOccurred('success');
  }

  function linkBlock(label, url) {
    return `<div class="card"><h3>${esc(label)}</h3>
      <div class="link-box"><span class="link-text">${esc(url)}</span><span class="copy" data-copy="${esc(url)}">копировать</span></div></div>`;
  }
  window.copyLink = copy;

  // Данные с сервера НЕ подставляем внутрь inline-JS: кавычка в URL/plan_id
  // ломала JS-строку и давала XSS. Значения лежат в data-атрибутах,
  // а клики обрабатываются делегированием.
  document.addEventListener('click', (e) => {
    const copyEl = e.target.closest('[data-copy]');
    if (copyEl) {
      e.preventDefault();
      copy(copyEl.dataset.copy);
      return;
    }
    const payEl = e.target.closest('[data-pay]');
    if (payEl) {
      e.preventDefault();
      window.pay(payEl.dataset.pay, payEl.dataset.method || 'yookassa', payEl);
      return;
    }
    const devEl = e.target.closest('[data-dev]');
    if (devEl) {
      go('help:' + devEl.dataset.dev);  // открываем «вкладку» инструкции устройства
      return;
    }
    const backEl = e.target.closest('[data-back]');
    if (backEl) {
      go('help');  // возврат к выбору устройства (промах)
      return;
    }
    const admEl = e.target.closest('[data-adm-remove]');
    if (admEl) {
      e.preventDefault();
      window.adminRemoveAdmin(admEl.dataset.admRemove);
      return;
    }
    const invEl = e.target.closest('[data-inv-revoke]');
    if (invEl) {
      e.preventDefault();
      window.adminRevokeInvite(invEl.dataset.invRevoke);
      return;
    }
  });

  // ---------- views ----------

  function siteLinkHtml() {
    const url = window.__siteUrl || '';
    if (!url) return '';
    // Автовход на сайт: initData добавляем в фрагмент (#init=...) — она не уходит
    // в заголовках/логи; сайт валидирует её токеном бота и ставит сессию сам.
    let href = url;
    try {
      if (tg && tg.initData) {
        href = url.replace(/\/+$/, '') + '/tg-login#init=' + encodeURIComponent(tg.initData);
      }
    } catch (e) { /* без initData — обычная ссылка на сайт */ }
    return `<a class="site-link" href="${esc(href)}" target="_blank" rel="noopener">🌐 Наш сайт</a>`;
  }

  // Приложения для подключения — ссылки приходят из /api/auth (ecosystem.apps)
  function appsHtml() {
    const a = window.__apps || {};
    const items = [
      ['🤖', 'Android', 'скачать APK', a.android],
      ['🍎', 'iOS', 'TestFlight', a.ios_testflight],
      ['🪟', 'Windows', 'скачать PWDTT', a.windows],
    ].filter((it) => it[3]);
    if (!items.length) return '';
    return `<div class="card"><h3>📱 Приложения для подключения</h3><div class="apps">` +
      items.map(([ico, name, sub, url]) =>
        `<a class="app-link" href="${esc(url)}" target="_blank" rel="noopener">` +
        `<span class="app-ico">${ico}</span><span>${esc(name)}<br><small>${esc(sub)}</small></span></a>`).join('') +
      `</div></div>`;
  }

  function navHtml() {
    const items = [
      ['home', '🏠', 'Главная'], ['plans', '🛒', 'Тарифы'], ['ref', '👥', 'Рефералы'],
      ['help', '📖', 'Инструкция'],
    ];
    if (me && me.is_admin) items.push(['admin', '🛠', 'Админ']);
    return items.map(([k, ico, label]) =>
      `<button class="${view === k || (k === 'help' && view.startsWith('help:')) ? 'active' : ''}" onclick="go('${k}')"><span class="ico">${ico}</span><span>${label}</span></button>`).join('');
  }

  // Навигация живёт вне #app (отдельный flex-item), поэтому всегда остаётся
  // внизу экрана, а прокручивается только содержимое.
  // innerHTML безопасен: navHtml() собирает только собственную разметку из констант.
  function renderNav() {
    const bar = document.getElementById('navbar');
    if (bar) bar.querySelector('.nav-inner').innerHTML = navHtml();
  }

  function scrollTop() {
    app.scrollTo({ top: 0, behavior: 'smooth' });
  }

  function headHtml(title, sub) {
    const brand = window.__brand || 'K.O. Gateway';
    const first = (brand.trim().split(/\s+/)[0] || brand);
    // «K.O. Gateway» -> «K.O.», «Gateway» -> «GW»
    const mark = /[.·:—-]/.test(first)
      ? first.slice(0, 4).toUpperCase()
      : brand.split(/\s+/).map(w => w[0]).join('').slice(0, 2).toUpperCase();
    return `<div class="header"><div class="brand-mark">${esc(mark)}</div>
      <div><div>${esc(title)}</div><small>${esc(sub || brand)}</small></div></div>`;
  }

  function skeletonHtml() {
    return `<div class="view-enter">${headHtml('Загрузка…', '')}
      <div class="card"><div class="skeleton sk-title"></div>
        <div class="skeleton sk-line"></div><div class="skeleton sk-line" style="width:70%"></div>
        <div class="skeleton sk-line" style="width:85%"></div></div>
      <div class="card"><div class="skeleton sk-line"></div><div class="skeleton sk-line" style="width:60%"></div></div></div>`;
  }

  function homeHtml() {
    let statusCard;
    if (me && me.has_subscription && me.active) {
      const t = me.traffic;
      const isTrial = me.is_trial || (me.plan && me.plan.id === 'trial');
      let trafficHtml = '';
      if (t) {
        if (t.unlimited) trafficHtml = `<div class="row"><span class="k">Трафик</span><span class="v">♾ безлимит · ${fmtBytes(t.used_bytes)}</span></div>`;
        else if (isTrial) {
          // пробный период лимитирован (5 ГБ) — показываем прогресс-бар
          const pct = Math.min(100, Math.round(t.used_bytes / t.limit_bytes * 100));
          trafficHtml = `<div class="row"><span class="k">Трафик (пробный)</span><span class="v">${fmtBytes(t.used_bytes)} / ${fmtBytes(t.limit_bytes)}</span></div>
            <div class="bar"><div style="width:${pct}%"></div></div>`;
        } else {
          trafficHtml = `<div class="row"><span class="k">Трафик</span><span class="v">${fmtBytes(t.used_bytes)}</span></div>`;
        }
      }
      statusCard = `<div class="card hero-card">
        <div class="hero-title">${esc(me.plan.name || 'Подписка')}${isTrial ? ' 🎁' : ''}</div>
        <div class="hero-sub">Активна до ${fmtDate(me.expires_at)} · ${daysLeft(me.expires_at)}</div>
        <span class="badge ok">● Активна</span>
        <div style="margin-top:12px">
          <div class="row"><span class="k">Тариф</span><span class="v">${esc(me.plan.name || '—')}</span></div>
          ${me.plan.max_devices ? `<div class="row"><span class="k">Устройства</span><span class="v">${me.devices_online != null ? me.devices_online + ' подключено · ' : ''}до ${me.max_devices || me.plan.max_devices}</span></div>` : ''}
          ${trafficHtml}
        </div></div>`;
    } else if (me && me.has_subscription) {
      statusCard = `<div class="card">
        <div class="hero-title">Подписка истекла</div>
        <div class="hero-sub" style="color:var(--hint)">Продлите подписку, чтобы вернуть доступ</div>
        <span class="badge err">● Не активна</span>
        <button class="btn" onclick="go('plans')">Продлить 🛒</button></div>`;
    } else {
      statusCard = `<div class="card">
        <div class="hero-title">Нет активной подписки</div>
        <div class="hero-sub" style="color:var(--hint)">Оформите тариф или начните с пробного периода</div>
        ${me && !me.trial_used ? `<button class="btn" onclick="claimTrial(this)">🎁 Пробный период — бесплатно</button>` : ''}
        <button class="btn secondary" onclick="go('plans')">🛒 Выбрать тариф</button></div>`;
    }

    let linksHtml = '';
    if (me && me.links && Object.keys(me.links).length) {
      const labels = { unified: '🔗 Единая ссылка подписки', ios: '🍏 iOS', android: '🤖 Android', windows: '🪟 Windows — PWDTT', universal: '🔗 Универсальная' };
      linksHtml = `<h3 style="font-size:12px;text-transform:uppercase;letter-spacing:.6px;color:var(--hint);margin:16px 16px 0;font-weight:700">Ссылки подключения</h3>` +
        Object.entries(me.links).map(([k, v]) => linkBlock(labels[k] || k, v)).join('');
    }
    return headHtml(window.__brand || 'K.O. Gateway', 'Управление подпиской') +
      statusCard + linksHtml + siteLinkHtml();
  }

  function planMeta(p) {
    const parts = [];
    if (p.days) parts.push(`${p.days} дн.`);
    parts.push(p.traffic_gb ? `${p.traffic_gb} ГБ` : 'безлимит');
    if (p.max_devices) parts.push(`до ${p.max_devices} устр.`);
    return parts.join(' · ');
  }

  async function plansHtml() {
    if (!plansData) plansData = await api('/api/plans');
    const planActions = (p) => `
      <div class="actions">
        <button class="btn sm" data-pay="${esc(p.id)}" data-method="yookassa">Оплатить</button>
      </div>`;
    const plans = plansData.plans || [];
    const rows = plans.map((p) => `
      <div class="plan">
        <div><div class="name">${esc(p.name)}</div><div class="desc">${esc(planMeta(p))}</div></div>
        <div class="price">
          <div class="rub">${p.price} ₽</div>
          ${planActions(p)}
        </div>
      </div>`).join('');
    const addons = (plansData.addons || []).map(p => `
      <div class="plan">
        <div><div class="name">➕ ${esc(p.name)}</div><div class="desc">${esc(p.description || `до ${p.max_devices} устройств, срок не меняется`)}</div></div>
        <div class="price">
          <div class="rub">+${p.price} ₽</div>
          ${planActions(p)}
        </div>
      </div>`).join('');
    return headHtml('Тарифы', 'Выберите подходящий план') +
      `<div class="card flush">${rows}</div>` +
      (addons ? `<h3 style="font-size:12px;text-transform:uppercase;letter-spacing:.6px;color:var(--hint);margin:16px 16px 0;font-weight:700">Больше устройств</h3><div class="card flush">${addons}</div>` : '') +
      appsHtml() +
      `<div class="card"><p class="note">Оплата: ЮKassa (карта/СБП). После оплаты доступ выдаётся автоматически. Лимит трафика есть только у пробного периода.</p></div>` +
      '';
  }

  async function refHtml() {
    const r = await api('/api/referrals');
    lastRefLink = r.link;
    return headHtml('Реферальная программа', `+${r.reward_days} дн. за приглашённого`) +
      `<div class="card">
        <h3>Ваша ссылка</h3>
        <div class="link-box"><span class="link-text">${esc(r.link)}</span><span class="copy" data-copy="${esc(r.link)}">копировать</span></div>
        <div class="row"><span class="k">Приглашено</span><span class="v">${r.count}</span></div>
        <button class="btn" style="margin-top:12px" onclick="shareRef()">📤 Отправить приглашение</button>
        <button class="btn secondary" onclick="copyLink(lastRefLink)">📋 Скопировать ссылку</button>
      </div>
      <div class="card"><p class="note">«Отправить» — выбор чатов внутри Telegram. «Скопировать» — ссылка в буфере обмена: вставьте в письмо или другой мессенджер.</p></div>`;
  }

  let lastRefLink = '';
  window.shareRef = function () {
    const tg = window.Telegram && window.Telegram.WebApp;
    const text = 'Присоединяйся к ' + (window.__brand || 'K.O. Gateway') + ' — стабильный доступ без VPN! Моя ссылка: ' + lastRefLink;
    const shareUrl = 'https://t.me/share/url?url=' + encodeURIComponent(lastRefLink) + '&text=' + encodeURIComponent(text);
    if (tg && tg.openTelegramLink) {
      tg.openTelegramLink(shareUrl);  // системный «Поделиться»: выбор пользователей и чатов
      if (tg.HapticFeedback) tg.HapticFeedback.impactOccurred('light');
    } else if (navigator.share) {
      navigator.share({ title: window.__brand || 'K.O. Gateway', text, url: lastRefLink }).catch(() => {});
    } else {
      copy(lastRefLink);
      toast('Ссылка скопирована — отправьте её вручную');
    }
  };

  let instrData = null;
  async function getInstr() {
    if (!instrData) instrData = await api('/api/v1/instructions');
    return instrData;
  }

  function platformLinksHtml(p) {
    let links = '';
    if (p.app_url) {
      links += `<p><a class="btn secondary" href="${esc(p.app_url)}" target="_blank" rel="noopener">${esc(p.app_label || 'Скачать приложение')}</a></p>`;
    }
    (p.links || []).forEach((l) => {
      links += `<p><a href="${esc(l.url)}" target="_blank" rel="noopener">${esc(l.label)}</a></p>`;
    });
    return links;
  }

  function platformStepsHtml(p) {
    return (p.steps || []).map((s, i) => s.img
      ? `<figure class="instr-step">
          <img src="${esc(s.img)}" alt="${esc(s.caption)}" loading="lazy">
          <figcaption><b>${i + 1}.</b> ${esc(s.caption)}</figcaption>
        </figure>`
      : `<p class="instr-text"><b>${i + 1}.</b> ${esc(s.caption)}</p>`).join('');
  }

  function fallbackPlatforms() {
    const a = window.__apps || {};
    return [
      { key: 'android', title: '🤖 Android', app_url: a.android, app_label: 'Скачать WDTT (APK)' },
      { key: 'ios', title: '🍎 iOS', app_url: a.ios_testflight, app_label: 'TestFlight' },
      { key: 'windows', title: '🪟 Windows', app_url: a.windows, app_label: 'Скачать PWDTT (EXE)' },
    ];
  }

  // Экран «Инструкция»: сначала выбор устройства. По тапу на устройство
  // открывается отдельная «вкладка» (view help:<device>) с его инструкцией —
  // оттуда есть кнопка «Назад» на случай промаха.
  async function helpHtml() {
    let data = null;
    try { data = await getInstr(); } catch (e) { /* фолбэк ниже */ }
    const platforms = (data && data.platforms && data.platforms.length) ? data.platforms : fallbackPlatforms();
    const cards = platforms.map((p) => `
      <button class="card dev-card" data-dev="${esc(p.key)}">
        <h3>${esc(p.title)}</h3>
        <span class="note">${esc(p.app_label || 'Пошаговая инструкция')}</span>
        <span class="btn secondary sm" style="margin-top:10px">Открыть инструкцию →</span>
      </button>`).join('');
    return headHtml('Инструкция', 'Выберите ваше устройство') +
      `<div class="card"><p class="note" style="margin:0">Выберите устройство — откроется отдельная вкладка с пошаговой инструкцией для него. Если выбрали не то устройство — нажмите «Назад» и выберите другое.</p></div>` +
      cards;
  }

  async function helpDeviceHtml(key) {
    let data = null;
    try { data = await getInstr(); } catch (e) { data = null; }
    const p = ((data && data.platforms) || []).find((x) => x.key === key);
    if (!p) return helpHtml();
    return `<button class="btn secondary" data-back>← Назад к выбору устройства</button>` +
      headHtml(p.title, (data && data.intro) || `Подключение к ${window.__brand || 'K.O. Gateway'}`) +
      `<div class="card"><h3>${esc(p.title)}</h3>
        <p>${p.intro}</p>${platformLinksHtml(p)}${platformStepsHtml(p)}</div>`;
  }

  // ---------- админ-панель (только для ADMIN_IDS) ----------

  let adminPollTimer = null;
  function adminPoll() {
    clearInterval(adminPollTimer);
    adminPollTimer = setInterval(async () => {
      if (view !== 'admin') { clearInterval(adminPollTimer); return; }
      try {
        const d = await api('/api/v1/admin/installs');
        if (!d.install_running) clearInterval(adminPollTimer);
        render();
      } catch (e) { /* молча */ }
    }, 10000);
  }

  async function adminHtml() {
    let d, adm, st;
    try {
      d = await api('/api/v1/admin');
      adm = await api('/api/v1/admin/admins');
      st = await api('/api/v1/admin/settings');
    } catch (e) {
      return headHtml('Админ-панель', '') +
        `<div class="card"><p class="note">${esc(e.message)}</p></div>`;
    }
    const s = d.stats || {};
    const servers = (d.servers || []).map((srv) => `
      <div class="row"><span class="k">#${srv.id} ${esc(srv.name)}</span>
        <span class="v">${esc(srv.base_url)} · 👤 ${srv.active_users}/${d.server_max_users}</span></div>`).join('')
      || '<p class="note">Серверов пока нет — добавьте первый ниже.</p>';
    const ico = (st) => st === 'running' ? '⏳' : (st === 'done' ? '✅' : '⚠️');
    const installs = (d.installs || []).map((i) => `
      <div class="row"><span class="k">${ico(i.status)} #${i.id} ${esc(i.name)} (${esc(i.host)})</span>
        <span class="v">${esc(i.detail || i.status)}</span></div>`).join('')
      || '<p class="note">Установок пока нет.</p>';
    const srcLabel = { env: 'конфиг', manual: 'по ID', invite: 'по ссылке' };
    const myId = me ? (me.id || me.telegram_id || 0) : 0;
    const admins = (adm.admins || []).map((a) => `
      <div class="row"><span class="k">${a.telegram_id} <small>(${esc(srcLabel[a.source] || a.source)})</small></span>
        <span class="v">${(a.source !== 'env' && a.telegram_id !== myId)
          ? `<button class="btn" style="padding:4px 10px;font-size:12px" data-adm-remove="${a.telegram_id}">снять</button>`
          : ''}</span></div>`).join('')
      || '<p class="note">Админов пока нет.</p>';
    const invites = (adm.invites || []).map((i) => `
      <div class="row"><span class="k" style="word-break:break-all;font-size:12px">${esc(i.url)}</span>
        <span class="v">${i.used ? '✅ использовано'
          : `<button class="btn" style="padding:4px 10px;font-size:12px" data-inv-revoke="${esc(i.code)}">отозвать</button>`}</span></div>`).join('')
      || '<p class="note">Ссылок-приглашений нет.</p>';
    return headHtml('Админ-панель', 'Серверы и статистика') +
      `<div class="card"><h3>📊 Статистика</h3>
        <div class="row"><span class="k">Клиентов</span><span class="v">${s.customers ?? 0} (активных ${s.active ?? 0})</span></div>
        <div class="row"><span class="k">Оплачено платежей</span><span class="v">${s.paid ?? 0}</span></div>
        <div class="row"><span class="k">Выручка</span><span class="v">${s.revenue ?? 0} ₽</span></div></div>
       <div class="card"><h3>🖥 Серверы (лимит ${d.server_max_users} на панель)</h3>${servers}
        <p class="note">Новые клиенты распределяются на наименее загруженный сервер.</p></div>
       <div class="card"><h3>➕ Добавить сервер (автоустановка панели по SSH)</h3>
        <input id="srv_name" class="inp" placeholder="Имя сервера (например, de2)" autocomplete="off">
        <input id="srv_host" class="inp" placeholder="IP-адрес сервера" autocomplete="off">
        <input id="srv_port" class="inp" placeholder="Порт SSH" value="22" inputmode="numeric" autocomplete="off">
        <input id="srv_user" class="inp" placeholder="Логин SSH (обычно root)" autocomplete="off">
        <input id="srv_pass" class="inp" type="password" placeholder="Пароль SSH" autocomplete="off">
        <button class="btn" style="margin-top:10px" onclick="adminAddServer(this)">🚀 Установить панель на сервер</button>
        <p class="note" style="margin-top:8px">Установка занимает 2–10 минут: панель скачается, поднимется, сменятся дефолтные логин/пароль, выставится лимит. Логин и пароль панели покажутся в списке установок.</p></div>
       <div class="card"><h3>Установки серверов</h3>${installs}</div>
       <div class="card"><h3>📞 VK-звонки (хеши)</h3>
        <p class="note">Новым клиентам сразу выдаётся по 4 VK-вызова (vk_hash) с ротацией VK-аккаунтов; панель ограничивает максимум в 4 на профиль. Кнопка ниже добивает хеши до 4 всем старым активным клиентам (работает фоном, результат — в логе бота).</p>
        <button class="btn" style="margin-top:8px" onclick="adminBackfillVk(this)">🔄 Добить VK-хеши до 4 у всех клиентов</button>
        <p class="note" id="vk_backfill_note" style="margin-top:8px"></p></div>
       <div class="card"><h3>👮 Администраторы</h3>${admins}
        <input id="adm_id" class="inp" placeholder="Telegram ID нового админа" inputmode="numeric" autocomplete="off" style="margin-top:8px">
        <button class="btn" style="margin-top:8px" onclick="adminAddAdmin(this)">➕ Назначить администратора по ID</button>
        <button class="btn" style="margin-top:8px" onclick="adminCreateInvite(this)">🔗 Создать ссылку-приглашение</button>
        <p class="note" style="margin-top:8px">Ссылка вида t.me/бот?start=admin_… — открывший её в боте становится администратором (одноразовая).</p>
        <h4 style="margin-top:10px">Ссылки-приглашения</h4>${invites}</div>
       <div class="card"><h3>⚙️ Настройки</h3>
        <p class="note">🪟 Ссылка на приложение <b>PWDTT</b> (Windows) — попадает в подписку, инструкции и на сайт. Пустое поле — сброс на дефолт.</p>
        <input id="win_url" class="inp" value="${esc(st.app_windows_url || '')}" placeholder="https://…/pwdtt-client-windows-amd64.exe" autocomplete="off" style="margin-top:8px">
        <button class="btn" style="margin-top:8px" onclick="adminSaveWindows(this)">💾 Сохранить ссылку PWDTT</button></div>`;
  }

  window.adminAddServer = async function (btn) {
    btn.disabled = true; btn.textContent = '⏳ Запускаем…';
    const body = {
      name: document.getElementById('srv_name').value.trim(),
      host: document.getElementById('srv_host').value.trim(),
      ssh_port: parseInt(document.getElementById('srv_port').value, 10) || 22,
      ssh_user: document.getElementById('srv_user').value.trim(),
      ssh_password: document.getElementById('srv_pass').value,
    };
    try {
      await api('/api/v1/admin/servers', { method: 'POST', body: JSON.stringify(body) });
      toast('🚀 Установка запущена (2–10 минут)');
      if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred('success');
      render();
    } catch (e) {
      toast(e.message);
      btn.disabled = false; btn.textContent = '🚀 Установить панель на сервер';
    }
  };

  window.adminAddAdmin = async function (btn) {
    btn.disabled = true;
    const id = parseInt((document.getElementById('adm_id').value || '').trim(), 10);
    try {
      if (!Number.isInteger(id) || id <= 0) throw new Error('Укажите положительный Telegram ID');
      await api('/api/v1/admin/admins', { method: 'POST', body: JSON.stringify({ telegram_id: id }) });
      toast('👮 Администратор назначен');
      render();
    } catch (e) {
      toast(e.message);
      btn.disabled = false;
    }
  };

  window.adminCreateInvite = async function (btn) {
    btn.disabled = true;
    try {
      const r = await api('/api/v1/admin/invites', { method: 'POST', body: '{}' });
      copy(r.url);
      toast('🔗 Ссылка-приглашение создана и скопирована');
      render();
    } catch (e) {
      toast(e.message);
      btn.disabled = false;
    }
  };

  window.adminRemoveAdmin = async function (id) {
    try {
      await api('/api/v1/admin/admins/remove', { method: 'POST', body: JSON.stringify({ telegram_id: parseInt(id, 10) }) });
      toast('Администратор снят');
      render();
    } catch (e) { toast(e.message); }
  };

  window.adminRevokeInvite = async function (code) {
    try {
      await api('/api/v1/admin/invites/revoke', { method: 'POST', body: JSON.stringify({ code: String(code) }) });
      toast('Приглашение отозвано');
      render();
    } catch (e) { toast(e.message); }
  };

  window.adminSaveWindows = async function (btn) {
    btn.disabled = true;
    const url = (document.getElementById('win_url').value || '').trim();
    try {
      const r = await api('/api/v1/admin/settings', { method: 'POST', body: JSON.stringify({ app_windows_url: url }) });
      toast(r.reset ? '↩️ Сброшено на ссылку по умолчанию' : '💾 Ссылка PWDTT сохранена');
      render();
    } catch (e) {
      toast(e.message);
      btn.disabled = false;
    }
  };

  window.adminBackfillVk = async function (btn) {
    btn.disabled = true; btn.textContent = '⏳ Запущено…';
    const note = document.getElementById('vk_backfill_note');
    try {
      const r = await api('/api/v1/admin/backfill-vk', { method: 'POST', body: '{}' });
      if (note) note.textContent = '✅ Задача запущена: всем активным клиентам добьются хеши до ' + (r.target || 4) + '. Результат — в логе бота.';
      toast('🔄 Бэкфилл VK-хешей запущен');
    } catch (e) {
      btn.disabled = false; btn.textContent = '🔄 Добить VK-хеши до 4 у всех клиентов';
      if (note) note.textContent = '⚠️ ' + e.message;
      toast(e.message);
    }
  };

  async function render() {
    app.classList.remove('view-enter');
    try {
      let html;
      if (view === 'home') html = homeHtml();
      else if (view === 'plans') html = await plansHtml();
      else if (view === 'ref') html = await refHtml();
      else if (view === 'help') html = await helpHtml();
      else if (view.startsWith('help:')) html = await helpDeviceHtml(view.slice(5));
      else if (view === 'admin') { html = await adminHtml(); adminPoll(); }
      app.innerHTML = `<div class="view-enter">${html}</div>`;
      renderNav();
    } catch (e) {
      app.innerHTML = `<div class="view-enter"><div class="card">
        <div class="hero-title">Что-то пошло не так</div>
        <div class="hero-sub" style="color:var(--hint)">${esc(e.message)}</div>
        <button class="btn" onclick="render()">🔄 Повторить</button></div></div>`;
      renderNav();
    }
  }

  window.go = function (v) {
    if (v === view) { scrollTop(); return; }
    view = v;
    if (tg && tg.HapticFeedback) tg.HapticFeedback.impactOccurred('light');
    scrollTop();
    render();
  };

  window.claimTrial = async function (btn) {
    btn.disabled = true; btn.textContent = '⏳ Активируем…';
    try {
      const r = await api('/api/trial', { method: 'POST', body: '{}' });
      toast('🎁 Пробный период активирован!');
      me = await api('/api/me');
      render();
    } catch (e) {
      toast(e.message);
      btn.disabled = false; btn.textContent = '🎁 Пробный период';
    }
  };

  window.pay = async function (planId, method, btn) {
    btn.disabled = true;
    const orig = btn.textContent;
    btn.textContent = '⏳';
    try {
      const r = await api('/api/pay/' + method, { method: 'POST', body: JSON.stringify({ plan_id: planId }) });
      if (tg) tg.openLink(r.url);
      else window.open(r.url, '_blank');
      toast('Завершите оплату и вернитесь в приложение');
      // после возврата из оплаты — проверим статус
      if (method === 'yookassa') pollPayment();
    } catch (e) {
      toast(e.message);
      btn.disabled = false; btn.textContent = orig;
    }
  };

  async function pollPayment() {
    for (let i = 0; i < 20; i++) {
      await new Promise(r => setTimeout(r, 3000));
      try {
        await api('/api/check_payment', { method: 'POST', body: '{}' });
        me = await api('/api/me');
        if (me.has_subscription && me.active) {
          toast('✅ Оплата прошла! Подписка активна');
          view = 'home';
          render();
          return;
        }
      } catch (e) { /* retry */ }
    }
  }

  // ---------- boot ----------
  (async function boot() {
    // Скелетоны на время загрузки данных
    app.innerHTML = skeletonHtml();
    renderNav();
    // Ждём initData: при открытии из меню чатов Telegram отдаёт его
    // с небольшой задержкой — авторизация должна пройти автоматически.
    const initData = await waitForInitData(4000);
    if (!initData) {
      const bot = window.__botUrl || 'https://t.me/KO_Gateway_bot';
      const site = window.__siteUrl || '';
      app.innerHTML = `<div class="view-enter"><div class="card">
        <div class="hero-title">Откройте приложение в Telegram</div>
        <div class="hero-sub" style="color:var(--hint)">Это приложение работает только внутри Telegram:
          нажмите кнопку «Открыть приложение» в боте или в меню чатов — здесь появятся подписка,
          ссылки для подключения и настройки.</div>
        <p style="margin-top:1.2rem"><a class="btn" href="${esc(bot)}" target="_blank" rel="noopener">Открыть бота в Telegram</a></p>
        ${site ? `<p><a class="btn secondary" href="${esc(site)}" target="_blank" rel="noopener">🌐 Сайт K.O. Gateway</a></p>` : ''}
        <button class="btn secondary" onclick="location.reload()">🔄 Повторить</button></div></div>`;
      renderNav();
      return;
    }
    try {
      await auth();
      me = await api('/api/me');
      render();
    } catch (e) {
      // Временная ошибка (сеть/5xx) — пробуем ещё пару раз автоматически.
      const transient = /HTTP 5|network|timeout|Failed to fetch/i.test(String(e && e.message));
      if (transient) {
        for (let i = 0; i < 2; i++) {
          await new Promise((r) => setTimeout(r, 800 * (i + 1)));
          try {
            await auth();
            me = await api('/api/me');
            render();
            return;
          } catch (_) { /* повторяем */ }
        }
      }
      const bot = window.__botUrl || 'https://t.me/KO_Gateway_bot';
      app.innerHTML = `<div class="view-enter"><div class="card">
        <div class="hero-title">Не удалось авторизоваться</div>
        <div class="hero-sub" style="color:var(--hint)">${esc(e.message)}</div>
        <p class="note">Откройте приложение через кнопку в боте или через меню чатов.</p>
        <p><a class="btn" href="${esc(bot)}" target="_blank" rel="noopener">Открыть бота в Telegram</a></p>
        <button class="btn secondary" onclick="location.reload()">🔄 Повторить</button></div></div>`;
      renderNav();
    }
  })();
})();
