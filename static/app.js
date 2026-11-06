/* WDTT Mini App — SPA на Telegram WebApp API */
(function () {
  'use strict';
  const tg = window.Telegram && window.Telegram.WebApp;
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

  async function auth() {
    const initData = tg ? tg.initData : '';
    const data = await api('/api/auth', { method: 'POST', body: JSON.stringify({ initData }) });
    token = data.token;
    sessionStorage.setItem('wdtt_token', token);
    window.__starsEnabled = data.stars_enabled;
    window.__starsRate = data.stars_rate;
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
      <div class="link-box"><span class="link-text">${esc(url)}</span><span class="copy" onclick="copyLink('${esc(url).replace(/'/g, "\\'")}')">копировать</span></div></div>`;
  }
  window.copyLink = copy;

  // ---------- views ----------

  function siteLinkHtml() {
    const url = window.__siteUrl || '';
    return url ? `<a class="site-link" href="${esc(url)}" target="_blank" rel="noopener">🌐 Открыть сайт ${esc(url.replace(/^https?:\/\//, ''))}</a>` : '';
  }

  function navHtml() {
    const items = [
      ['home', '🏠', 'Главная'], ['plans', '🛒', 'Тарифы'], ['ref', '👥', 'Рефералы'],
      ['help', '📖', 'Инструкция'], ['settings', '⚙️', 'Настройки'],
    ];
    return `<nav class="nav">${items.map(([k, ico, label]) =>
      `<button class="${view === k ? 'active' : ''}" onclick="go('${k}')"><span class="ico">${ico}</span><span>${label}</span></button>`).join('')}</nav>`;
  }

  function headHtml(title, sub) {
    const brand = window.__brand || 'K.O. Gateway';
    const mark = brand.split(/\s+/).map(w => w[0]).join('').slice(0, 2).toUpperCase();
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
    return headHtml('Настройки', 'Автопродление подписки') +
      `<div class="card">
        <h3>🔄 Автопродление</h3>
        <label class="check"><input type="checkbox" id="auto_renew"${s.auto_renew ? ' checked' : ''}>
          <span class="check-text">Автоматически продлевать подписку на <b>1 месяц</b> (30 дней) — списываем ${s.renew_price} ₽ с сохранённой карты</span></label>
        ${s.auto_renew && !s.has_payment_method ? '<div class="note" style="margin-top:8px;color:var(--warn)">⚠️ Сначала оплатите любой тариф картой через ЮKassa — карта сохранится для автопродления.</div>' : ''}
        <h3 style="margin-top:16px">🔔 Уведомление о списании</h3>
        <label class="check"><input type="checkbox" id="notify_on"${s.notify_hours > 0 ? ' checked' : ''}>
          <span class="check-text">Уведомлять о списании средств</span></label>
        <select id="notify_hours" ${s.notify_hours > 0 ? '' : 'disabled'}>${opts}</select>
        <button class="btn" style="margin-top:16px" onclick="saveSettings(this)">Сохранить</button>
      </div>
      <div class="card"><p class="note">Автопродление всегда списывает стоимость только месячного тарифа и добавляет 30 дней к текущей дате окончания. Отключить можно в любой момент.</p></div>` + navHtml();
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
      statusCard + linksHtml + siteLinkHtml() + navHtml();
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
        ${window.__starsEnabled ? `<div class="stars">или ${p.stars} ⭐</div>` : ''}
        <button class="btn secondary sm" onclick="pay('${p.id}','yookassa',this)">Оплатить</button>
        ${window.__starsEnabled ? `<button class="btn sm" onclick="pay('${p.id}','stars',this)">⭐ ${p.stars}</button>` : ''}
      </div>`;
    const plans = plansData.plans || [];
    // «популярный» — средний по цене тариф (визуально выделяем)
    let topIdx = -1;
    if (plans.length > 1) {
      let best = -1, bestPrice = -1;
      plans.forEach((p, i) => { const pr = Number(p.price) || 0; if (pr > bestPrice) { bestPrice = pr; best = i; } });
      topIdx = best;
    }
    const rows = plans.map((p, i) => `
      <div class="plan${i === topIdx ? ' is-top' : ''}">
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
      `<div class="card"><p class="note">Оплата: ЮKassa (карта/СБП) или Telegram Stars ⭐. После оплаты доступ выдаётся автоматически. Лимит трафика есть только у пробного периода.</p></div>` +
      navHtml();
  }

  async function refHtml() {
    const r = await api('/api/referrals');
    lastRefLink = r.link;
    return headHtml('Реферальная программа', `+${r.reward_days} дн. за приглашённого`) +
      `<div class="card">
        <h3>Ваша ссылка</h3>
        <div class="link-box"><span class="link-text">${esc(r.link)}</span><span class="copy" onclick="copyLink('${esc(r.link).replace(/'/g, "\\'")}')">копировать</span></div>
        <div class="row"><span class="k">Приглашено</span><span class="v">${r.count}</span></div>
        <button class="btn" style="margin-top:12px" onclick="shareRef()">📤 Отправить приглашение</button>
        <button class="btn secondary" onclick="copyLink(lastRefLink)">📋 Скопировать ссылку</button>
      </div>
      <div class="card"><p class="note">«Отправить» — выбор чатов внутри Telegram. «Скопировать» — ссылка в буфере обмена: вставьте в письмо или другой мессенджер.</p></div>` + navHtml();
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

  function helpHtml() {
    const steps = (arr) => arr.map((s, i) => `<div class="help-step"><span class="n">${i + 1}</span><span>${s}</span></div>`).join('');
    return headHtml('Инструкция', `Подключение к ${window.__brand || 'K.O. Gateway'}`) +
      `<div class="card"><h3>🤖 Android</h3>
        ${steps(['Установите приложение <b>WDTT</b> из Google Play.',
                 'Нажмите «Добавить» и вставьте ссылку подписки (или отсканируйте QR).',
                 'Включите VPN — готово.'])}
      </div>
      <div class="card"><h3>🍎 iOS</h3>
        ${steps(['Установите приложение из App Store.',
                 'Вставьте iOS-ссылку из главного экрана приложения.',
                 'Разрешите VPN в настройках iOS и подключитесь.'])}
      </div>` + navHtml();
  }

  async function render() {
    app.classList.remove('view-enter');
    try {
      let html;
      if (view === 'home') html = homeHtml();
      else if (view === 'plans') html = await plansHtml();
      else if (view === 'ref') html = await refHtml();
      else if (view === 'help') html = helpHtml();
      else if (view === 'settings') html = await settingsHtml();
      app.innerHTML = `<div class="view-enter">${html}</div>`;
    } catch (e) {
      app.innerHTML = `<div class="view-enter"><div class="card">
        <div class="hero-title">Что-то пошло не так</div>
        <div class="hero-sub" style="color:var(--hint)">${esc(e.message)}</div>
        <button class="btn" onclick="render()">🔄 Повторить</button></div></div>`;
    }
  }

  window.go = function (v) {
    if (v === view) { window.scrollTo({ top: 0, behavior: 'smooth' }); return; }
    view = v;
    if (tg && tg.HapticFeedback) tg.HapticFeedback.impactOccurred('light');
    window.scrollTo({ top: 0, behavior: 'smooth' });
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
    try {
      await auth();
      me = await api('/api/me');
      render();
    } catch (e) {
      app.innerHTML = `<div class="view-enter"><div class="card">
        <div class="hero-title">Не удалось авторизоваться</div>
        <div class="hero-sub" style="color:var(--hint)">${esc(e.message)}</div>
        <p class="note">Откройте приложение через кнопку в боте.</p>
        <button class="btn" onclick="location.reload()">🔄 Повторить</button></div></div>`;
    }
  })();
})();
