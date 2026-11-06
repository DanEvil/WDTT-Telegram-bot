// WDTT сайт — общий JS
(function () {
  'use strict';

  // Кнопка «Скопировать» рядом с .mono
  function initCopyButtons() {
    document.querySelectorAll('.mono').forEach(function (el) {
      if (el.querySelector('.copy-btn')) return;
      var code = el.querySelector('code');
      if (!code) return;
      var btn = document.createElement('button');
      btn.className = 'copy-btn';
      btn.type = 'button';
      btn.textContent = '📋 Копировать';
      btn.addEventListener('click', function () {
        var text = code.textContent || '';
        navigator.clipboard.writeText(text).then(function () {
          btn.textContent = '✅ Скопировано';
          setTimeout(function () { btn.textContent = '📋 Копировать'; }, 1500);
        }).catch(function () {
          // fallback
          var ta = document.createElement('textarea');
          ta.value = text;
          document.body.appendChild(ta);
          ta.select();
          try { document.execCommand('copy'); btn.textContent = '✅ Скопировано'; }
          catch (e) { btn.textContent = '❌'; }
          document.body.removeChild(ta);
          setTimeout(function () { btn.textContent = '📋 Копировать'; }, 1500);
        });
      });
      el.appendChild(btn);
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initCopyButtons);
  } else {
    initCopyButtons();
  }
})();
