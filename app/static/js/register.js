/* MEMBER - register page helper.
 *
 * 1. Capture utm_* / fbclid from the URL into localStorage FIRST-TOUCH
 *    (written only once, an existing first touch is never overwritten).
 * 2. Fill the hidden attribution inputs from that first touch when the URL has none.
 * 3. Read the _fbp / _fbc cookies and derive _fbc from fbclid when the cookie is missing.
 * 4. Fill landing_url (first touch) and referrer when empty.
 * 5. Light client-side required-field check - UX only, the server is authoritative.
 *
 * Vanilla JS, no framework, no inline handlers, never throws when elements are absent.
 */
(function () {
  'use strict';

  var STORAGE_KEY = 'member_attribution';
  var UTM_KEYS = ['utm_source', 'utm_medium', 'utm_campaign', 'utm_content', 'utm_term'];
  var HIDDEN_NAMES = UTM_KEYS.concat(['landing_url', 'referrer', 'fbp', 'fbc']);
  var FBP_COOKIE = '_fbp';
  var FBC_COOKIE = '_fbc';

  /* ------------------------------------------------------------------ storage */

  function readStoredFirstTouch() {
    try {
      var raw = window.localStorage.getItem(STORAGE_KEY);
      if (!raw) return null;
      var parsed = JSON.parse(raw);
      return parsed && typeof parsed === 'object' ? parsed : null;
    } catch (err) {
      return null;
    }
  }

  function writeStoredFirstTouch(value) {
    try {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(value));
      return true;
    } catch (err) {
      return false; /* private mode / quota: degrade silently */
    }
  }

  /* ---------------------------------------------------------------------- url */

  function readUrlParams() {
    var out = {};
    try {
      var params = new URLSearchParams(window.location.search || '');
      UTM_KEYS.forEach(function (key) {
        var value = params.get(key);
        if (value) out[key] = value;
      });
      var fbclid = params.get('fbclid');
      if (fbclid) out.fbclid = fbclid;
    } catch (err) {
      /* URLSearchParams unavailable: no URL attribution */
    }
    return out;
  }

  function currentUrl() {
    try {
      return window.location.href || '';
    } catch (err) {
      return '';
    }
  }

  function readCookie(name) {
    try {
      var parts = (document.cookie || '').split(';');
      var prefix = name + '=';
      for (var i = 0; i < parts.length; i++) {
        var item = parts[i].trim();
        if (item.indexOf(prefix) === 0) {
          var raw = item.slice(prefix.length);
          try {
            return decodeURIComponent(raw);
          } catch (err) {
            return raw;
          }
        }
      }
    } catch (err) {
      /* cookies blocked */
    }
    return '';
  }

  /* ---------------------------------------------------------------- first touch */

  function firstTouch() {
    var stored = readStoredFirstTouch();
    if (stored) return stored; /* never overwrite an existing first touch */

    var fromUrl = readUrlParams();
    var first = {};
    UTM_KEYS.forEach(function (key) {
      if (fromUrl[key]) first[key] = fromUrl[key];
    });
    if (fromUrl.fbclid) first.fbclid = fromUrl.fbclid;
    first.landing_url = currentUrl();
    first.referrer = (document.referrer || '');
    writeStoredFirstTouch(first);
    return first;
  }

  /* ------------------------------------------------------------------ form fill */

  function buildValues(first) {
    var fromUrl = readUrlParams();
    var values = {};

    UTM_KEYS.forEach(function (key) {
      values[key] = first[key] || fromUrl[key] || '';
    });
    values.landing_url = first.landing_url || currentUrl();
    values.referrer = first.referrer || document.referrer || '';

    values.fbp = readCookie(FBP_COOKIE);

    var fbc = readCookie(FBC_COOKIE);
    if (!fbc) {
      var fbclid = fromUrl.fbclid || first.fbclid || '';
      if (fbclid) fbc = 'fb.1.' + Date.now() + '.' + fbclid;
    }
    values.fbc = fbc;

    return values;
  }

  function fillHiddenInputs(values) {
    HIDDEN_NAMES.forEach(function (name) {
      var input = document.querySelector('input[type="hidden"][name="' + name + '"]');
      if (!input || input.value) return; /* server-provided values win */
      var value = values[name];
      if (value) input.value = value;
    });
  }

  /* ------------------------------------------------------------------ validation */

  function emailLooksValid(value) {
    return /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/.test(value);
  }

  function initValidation() {
    var form = document.querySelector('[data-register-form]');
    if (!form) return;

    var box = form.querySelector('[data-register-error]');
    var nameInput = form.querySelector('input[name="full_name"]');
    var emailInput = form.querySelector('input[name="email"]');
    var consentInput = form.querySelector('input[name="consent_marketing"]');

    function showError(text) {
      if (!box) return;
      box.textContent = text;
      box.hidden = false;
    }

    function clearError() {
      if (!box) return;
      box.textContent = '';
      box.hidden = true;
    }

    function mark(input, invalid) {
      if (!input) return;
      if (invalid) input.setAttribute('aria-invalid', 'true');
      else input.removeAttribute('aria-invalid');
    }

    form.addEventListener('submit', function (event) {
      var problems = [];

      if (nameInput && !String(nameInput.value || '').trim()) {
        problems.push('Vui lòng nhập họ và tên.');
        mark(nameInput, true);
      } else {
        mark(nameInput, false);
      }

      if (emailInput && !emailLooksValid(String(emailInput.value || '').trim())) {
        problems.push('Vui lòng nhập địa chỉ email hợp lệ.');
        mark(emailInput, true);
      } else {
        mark(emailInput, false);
      }

      /* the checkbox is only mandatory when the server marked it as such */
      if (consentInput && consentInput.hasAttribute && consentInput.hasAttribute('required') && !consentInput.checked) {
        problems.push('Vui lòng đồng ý nhận thông tin để tiếp tục.');
        mark(consentInput, true);
      } else {
        mark(consentInput, false);
      }

      if (problems.length) {
        event.preventDefault();
        showError(problems.join(' '));
        var firstInvalid = form.querySelector('[aria-invalid="true"]');
        if (firstInvalid && typeof firstInvalid.focus === 'function') firstInvalid.focus();
      } else {
        clearError();
      }
    });

    form.addEventListener('input', function (event) {
      if (box && !box.hidden) clearError();
      var target = event.target;
      if (target && target.getAttribute && target.getAttribute('aria-invalid') === 'true') {
        mark(target, false);
      }
    });
  }

  /* ----------------------------------------------------------------------- init */

  function init() {
    var first = firstTouch();
    fillHiddenInputs(buildValues(first));
    initValidation();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
