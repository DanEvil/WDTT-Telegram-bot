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
  let settingsData = null;
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
    }
  });

  // ---------- views ----------

  function siteLinkHtml() {
    const url = window.__siteUrl || '';
    return url ? `<a class="site-link" href="${esc(url)}" target="_blank" rel="noopener">🌐 Открыть сайт ${esc(url.replace(/^https?:\/\//, ''))}</a>` : '';
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
      ['help', '📖', 'Инструкция'], ['settings', '⚙️', 'Настройки'],
    ];
    return items.map(([k, ico, label]) =>
      `<button class="${view === k ? 'active' : ''}" onclick="go('${k}')"><span class="ico">${ico}</span><span>${label}</span></button>`).join('');
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

  async function settingsHtml() {
    if (!settingsData) settingsData = await api('/api/v1/settings');
    const s = settingsData;
    const opts = (s.notify_hours_options || [0, 1, 3, 6, 12, 24, 48]).map(h =>
      `<option value="${h}"${h === s.notify_hours ? ' selected' : ''}>${h === 0 ? 'Не уведомлять' : `За ${h} ч.`}</option>`).join('');
    // рекурренты выключены в магазине ЮKassa -> карта не сохранится, автопродление невозможно
    const noRecurring = s.recurring_supported === false;
    const renewNote = noRecurring
      ? `<div class="note" style="margin-top:8px;color:var(--err)">
           ⚠️ Автопродление сейчас недоступно: платёжный шлюз не поддерживает повторные списания
           для этого магазина, карта не сохраняется. Оплата и продление вручную работают.
         </div>`
      : (s.auto_renew && !s.has_payment_method
          ? '<div class="note" style="margin-top:8px;color:var(--warn)">⚠️ Сначала оплатите любой тариф картой через ЮKassa — карта сохранится для автопродления.</div>'
          : '');
    return headHtml('Настройки', 'Автопродление подписки') +
      `<div class="card">
        <h3>🔄 Автопродление</h3>
        <label class="check"><input type="checkbox" id="auto_renew"${s.auto_renew ? ' checked' : ''}${noRecurring ? ' disabled' : ''}>
          <span class="check-text">Автоматически продлевать подписку на <b>1 месяц</b> (30 дней) — списываем ${s.renew_price} ₽ с сохранённой карты</span></label>
        ${renewNote}
        <h3 style="margin-top:16px">🔔 Уведомление о списании</h3>
        <label class="check"><input type="checkbox" id="notify_on"${s.notify_hours > 0 ? ' checked' : ''}>
          <span class="check-text">Уведомлять меня о списании средств</span></label>
        <select id="notify_hours" ${s.notify_hours > 0 ? '' : 'disabled'}>${opts}</select>
        <button class="btn" style="margin-top:16px" onclick="saveSettings(this)">Сохранить</button>
      </div>
      <div class="card"><p class="note">Автопродление всегда списывает стоимость только месячного тарифа и добавляет 30 дней к текущей дате окончания. Отключить можно в любой момент.</p></div>`;
  }

  window.saveSettings = async function (btn) {
    btn.disabled = true; btn.textContent = '⏳ Сохраняем…';
    const autoRenew = document.getElementById('auto_renew').checked;
    const notifyOn = document.getElementById('notify_on').checked;
    const notifyHours = notifyOn ? parseInt(document.getElementById('notify_hours').value, 10) : 0;
    try {
      settingsData = await api('/api/v1/settings', {
        method: 'POST',
        body: JSON.stringify({ auto_renew: autoRenew, notify_hours: notifyHours }),
      });
      toast('✅ Настройки сохранены');
      render();
    } catch (e) {
      toast(e.message);
      btn.disabled = false; btn.textContent = 'Сохранить';
    }
  };

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
          ${me.plan.max_devices ? `<div class="row"><span class="k">Устройств</span><span class="v">до ${me.plan.max_devices}</span></div>` : ''}
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
      const labels = { unified: '🔗 Единая ссылка подписки', ios: '🍏 iOS', android: '🤖 Android', universal: '🔗 Универсальная' };
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

  async function helpHtml() {
    // Пошаговые инструкции со скриншотами — из общего API (/api/v1/instructions).
    let data = null;
    try {
      data = await api('/api/v1/instructions');
    } catch (e) { /* offline-фолбэк ниже */ }
    if (!data || !data.platforms || !data.platforms.length) {
      return headHtml('Инструкция', `Подключение к ${window.__brand || 'K.O. Gateway'}`) +
        `<div class="card"><h3>🤖 Android</h3>
          <p>Установите приложение WDTT (APK), вставьте ссылку <code>wdtt://</code> из профиля и нажмите «Подключить».</p>
        </div>
        <div class="card"><h3>🍎 iOS</h3>
          <p>Установите TestFlight, откройте приглашение, затем в приложении: Settings → Backup & Restore → Import from Connection Link…</p>
        </div>`;
    }
    const cards = data.platforms.map((p) => {
      let links = '';
      if (p.app_url) {
        links += `<p><a class="btn secondary" href="${esc(p.app_url)}" target="_blank" rel="noopener">${esc(p.app_label || 'Скачать')}</a></p>`;
      }
      (p.links || []).forEach((l) => {
        links += `<p><a href="${esc(l.url)}" target="_blank" rel="noopener">${esc(l.label)}</a></p>`;
      });
      const steps = (p.steps || []).map((s, i) => `
        <figure class="instr-step">
          <img src="${esc(s.img)}" alt="${esc(s.caption)}" loading="lazy">
          <figcaption><b>${i + 1}.</b> ${esc(s.caption)}</figcaption>
        </figure>`).join('');
      return `<div class="card"><h3>${esc(p.title)}</h3>
        <p>${p.intro}</p>${links}${steps}</div>`;
    }).join('');
    return headHtml('Инструкция', data.intro || `Подключение к ${window.__brand || 'K.O. Gateway'}`) + cards;
  }

  async function render() {
    app.classList.remove('view-enter');
    try {
      let html;
      if (view === 'home') html = homeHtml();
      else if (view === 'plans') html = await plansHtml();
      else if (view === 'ref') html = await refHtml();
      else if (view === 'help') html = await helpHtml();
      else if (view === 'settings') html = await settingsHtml();
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
