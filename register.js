// ==UserScript==
// @name         Onyx Auto Register (CF Temp Mail)
// @namespace    https://tampermonkey.net/
// @version      2026-02-25
// @description  自动注册 + 邮箱验证 + 创建 Agent，并通过 GM_cookie 读取 fastapiusersauth 后上报到本地后端
// @author       onxy2api
// @match        https://cloud.onyx.app/auth/signup*
// @match        https://cloud.onyx.app/auth/login*
// @match        https://cloud.onyx.app/auth/waiting-on-verification*
// @match        https://cloud.onyx.app/app*
// @match        https://cloud.onyx.app/admin/api-key*
// @icon         https://www.google.com/s2/favicons?sz=64&domain=onyx.app
// @grant        GM_xmlhttpRequest
// @grant        GM_cookie
// @connect      *
// @run-at       document-idle
// ==/UserScript==

(function () {
  'use strict';

  /**
   * ================= 配置区（必须改） =================
   */
  const CONFIG = {
    // 对应 register.py: CF_WORKER_DOMAIN（不带 https://）
    CF_WORKER_DOMAIN: '',
    // 对应 register.py: CF_EMAIL_DOMAIN
    CF_EMAIL_DOMAIN: '',
    // 对应 register.py: CF_ADMIN_PASSWORD
    CF_ADMIN_PASSWORD: '',

    // 行为配置
    AUTO_START: true,
    MAX_REGISTER_RETRY: 9,
    REGISTER_TIMEOUT_MS: 120000,
    MAIL_TIMEOUT_MS: 120000,
    MAIL_POLL_INTERVAL_MS: 3000,
    AGENT_TIMEOUT_MS: 120000,
    COOKIE_WAIT_TIMEOUT_MS: 9000,
    PASSWORD_LENGTH: 16,
    AGENT_NAME_PREFIX: 'Test Agent',
    STATE_MAX_AGE_MS: 30 * 60 * 1000,
    FULL_AUTO_RESTART_DELAY_MS: 1200,

    // 自动上报 fastapiusersauth Cookie 到 onyx2api（app.py）
    APPEND_COOKIE_ENABLED: true,
    APPEND_COOKIE_URL: 'http://127.0.0.1:19898/api/onyx-cookies/append',
    APPEND_COOKIE_ADMIN_PASSWORD: '',
    APPEND_COOKIE_TIMEOUT_MS: 15000,
    APPEND_COOKIE_MAX_RETRY: 3,
  };

  const TAG = '[Onyx-AutoRegister]';
  const STORAGE_KEY = 'onyx_auto_register_state_v1';
  const LAST_ACCOUNT_KEY = 'onyx_last_registered_account';
  const FULL_AUTO_ENABLED_KEY = 'onyx_full_auto_enabled_v1';

  function loadState() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      return raw ? JSON.parse(raw) : null;
    } catch {
      return null;
    }
  }

  function saveState(patch) {
    const prev = loadState() || {};
    const next = {
      ...prev,
      ...patch,
      updatedAt: Date.now(),
    };
    localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
    return next;
  }

  function clearState() {
    localStorage.removeItem(STORAGE_KEY);
  }

  function loadLastRegisteredAccount() {
    try {
      const raw = window.localStorage.getItem(LAST_ACCOUNT_KEY);
      return raw ? JSON.parse(raw) : null;
    } catch {
      return null;
    }
  }

  function saveLastRegisteredAccount(patch) {
    const prev = loadLastRegisteredAccount() || {};

    const next = {
      ...prev,
      ...patch,
      ts: Date.now(),
    };

    window.localStorage.setItem(LAST_ACCOUNT_KEY, JSON.stringify(next));
    return next;
  }

  function loadFullAutoEnabled() {
    try {
      const raw = window.localStorage.getItem(FULL_AUTO_ENABLED_KEY);
      return raw === '1' || String(raw).toLowerCase() === 'true';
    } catch {
      return false;
    }
  }

  function saveFullAutoEnabled(enabled) {
    window.localStorage.setItem(FULL_AUTO_ENABLED_KEY, enabled ? '1' : '0');
    return enabled;
  }

  function isFullAutoEnabled() {
    return loadFullAutoEnabled();
  }

  function maskEmail(email) {
    const s = String(email || '');
    const i = s.indexOf('@');
    if (i <= 1) return s;
    return `${s.slice(0, 2)}***${s.slice(i)}`;
  }

  function log(...args) {
    console.log(TAG, ...args);
  }

  function warn(...args) {
    console.warn(TAG, ...args);
  }

  function err(...args) {
    console.error(TAG, ...args);
  }

  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  function randInt(min, max) {
    return Math.floor(Math.random() * (max - min + 1)) + min;
  }

  function randomChoice(str) {
    return str[Math.floor(Math.random() * str.length)];
  }

  function randomString(chars, len) {
    let s = '';
    for (let i = 0; i < len; i += 1) s += randomChoice(chars);
    return s;
  }

  function createRandomName() {
    const letters = 'abcdefghijklmnopqrstuvwxyz';
    const digits = '0123456789';
    const letters1 = randomString(letters, randInt(4, 6));
    const numbers = randomString(digits, randInt(1, 3));
    const letters2 = randomString(letters, randInt(0, 5));
    return `${letters1}${numbers}${letters2}`;
  }

  function generateRandomPassword(length = 16) {
    const uppers = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ';
    const lowers = 'abcdefghijklmnopqrstuvwxyz';
    const digits = '0123456789';
    const specials = '!@#$%';
    const all = uppers + lowers + digits + specials;

    const body = randomString(all, length);
    return randomChoice(uppers)
      + randomChoice(lowers)
      + randomChoice(digits)
      + randomChoice(specials)
      + body.slice(4);
  }

  function fetchFallback({ method, url, headers, data, timeout = 30000 }) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeout);

    return fetch(url, {
      method,
      headers,
      body: data,
      signal: controller.signal,
      credentials: 'omit',
    }).then(async (resp) => {
      const responseText = await resp.text();
      return {
        status: resp.status,
        responseText,
        statusText: resp.statusText,
      };
    }).finally(() => {
      clearTimeout(timer);
    });
  }

  function gmRequest({ method, url, headers, data, timeout = 30000 }) {
    return new Promise((resolve, reject) => {
      GM_xmlhttpRequest({
        method,
        url,
        headers,
        data,
        timeout,
        onload: (resp) => resolve(resp),
        onerror: (e) => {
          const message = String((e && (e.error || e.statusText)) || '');
          if (message.toLowerCase().includes('blacklisted')) {
            warn('GM_xmlhttpRequest 被黑名单拦截，尝试使用 fetch 回退请求:', url);
            fetchFallback({ method, url, headers, data, timeout })
              .then(resolve)
              .catch(reject);
            return;
          }
          reject(e);
        },
        ontimeout: () => reject(new Error(`请求超时: ${url}`)),
      });
    });
  }

  function getAppendCookieConfig() {
    const enabled = Boolean(CONFIG.APPEND_COOKIE_ENABLED);
    const url = String(CONFIG.APPEND_COOKIE_URL || '').trim();
    const adminPassword = String(CONFIG.APPEND_COOKIE_ADMIN_PASSWORD || '').trim();
    const timeout = Number(CONFIG.APPEND_COOKIE_TIMEOUT_MS || 15000);
    const maxRetry = Math.max(1, Number(CONFIG.APPEND_COOKIE_MAX_RETRY || 1));

    return {
      enabled,
      url,
      adminPassword,
      timeout,
      maxRetry,
    };
  }

  function gmCookieList(details) {
    return new Promise((resolve, reject) => {
      if (typeof GM_cookie === 'undefined' || !GM_cookie || typeof GM_cookie.list !== 'function') {
        reject(new Error('GM_cookie 不可用：请确认脚本管理器支持并已授权 @grant GM_cookie'));
        return;
      }

      try {
        GM_cookie.list(details, (cookies, error) => {
          if (error) {
            reject(new Error(`GM_cookie.list 失败: ${String(error)}`));
            return;
          }
          resolve(Array.isArray(cookies) ? cookies : []);
        });
      } catch (e) {
        reject(e);
      }
    });
  }

  function gmCookieDelete(details) {
    return new Promise((resolve, reject) => {
      if (typeof GM_cookie === 'undefined' || !GM_cookie || typeof GM_cookie.delete !== 'function') {
        reject(new Error('GM_cookie.delete 不可用：请确认脚本管理器支持并已授权 @grant GM_cookie'));
        return;
      }

      try {
        GM_cookie.delete(details, (error) => {
          if (error) {
            reject(new Error(`GM_cookie.delete 失败: ${String(error)}`));
            return;
          }
          resolve(true);
        });
      } catch (e) {
        reject(e);
      }
    });
  }

  function getDomainVariants(hostname) {
    const host = String(hostname || '').trim().toLowerCase();
    if (!host) return [];

    const segments = host.split('.').filter(Boolean);
    const out = new Set([host, `.${host}`]);

    for (let i = 1; i < segments.length - 1; i += 1) {
      const part = segments.slice(i).join('.');
      out.add(part);
      out.add(`.${part}`);
    }

    return Array.from(out);
  }

  function getPathVariants(pathname) {
    const path = String(pathname || '/');
    const out = new Set(['/']);

    const chunks = path.split('/').filter(Boolean);
    let cur = '';
    for (const chunk of chunks) {
      cur += `/${chunk}`;
      out.add(cur);
      out.add(`${cur}/`);
    }

    return Array.from(out);
  }

  function expireDocumentCookie(name, domain, path) {
    const cookieName = encodeURIComponent(String(name || '').trim());
    if (!cookieName) return;

    const domainPart = domain ? `; domain=${domain}` : '';
    const pathPart = path ? `; path=${path}` : '; path=/';
    const expiresPart = '; expires=Thu, 01 Jan 1970 00:00:00 GMT; Max-Age=0';

    document.cookie = `${cookieName}=${expiresPart}${pathPart}${domainPart}`;
    document.cookie = `${cookieName}=;${expiresPart}${pathPart}${domainPart}; Secure`;
  }

  async function clearAllCookiesForCurrentSite() {
    const host = String(window.location.hostname || '').trim().toLowerCase();
    const domainVariants = getDomainVariants(host);
    const pathVariants = getPathVariants(window.location.pathname || '/');

    // 先尝试 document.cookie 级别删除
    const docCookies = String(document.cookie || '')
      .split(';')
      .map((part) => String(part || '').trim())
      .filter(Boolean);

    const docCookieNames = docCookies
      .map((item) => item.split('=')[0])
      .map((name) => decodeURIComponent(String(name || '').trim()))
      .filter(Boolean);

    for (const name of docCookieNames) {
      for (const path of pathVariants) {
        expireDocumentCookie(name, '', path);
        for (const domain of domainVariants) {
          expireDocumentCookie(name, domain, path);
        }
      }
    }

    // 再尝试 GM_cookie 级别删除（覆盖 HttpOnly 等）
    let gmCookies = [];
    try {
      gmCookies = await gmCookieList({});
    } catch (e) {
      warn('清理 Cookie 时，GM_cookie.list 获取失败，将仅依赖 document.cookie 删除:', e);
      gmCookies = [];
    }

    let gmDeleteSuccess = 0;
    let gmDeleteFailed = 0;

    const isDomainInCurrentSite = (domainLike) => {
      const d = String(domainLike || '').trim().toLowerCase().replace(/^\./, '');
      if (!d || !host) return false;
      return d === host || host.endsWith(`.${d}`) || d.endsWith(`.${host}`);
    };

    for (const item of gmCookies) {
      if (!item || typeof item !== 'object') continue;

      const name = String(item.name || '').trim();
      if (!name) continue;

      const domain = String(item.domain || '').trim();
      const path = String(item.path || '/').trim() || '/';
      const urlHost = domain.replace(/^\./, '') || host;

      if (!isDomainInCurrentSite(urlHost)) continue;

      const candidates = [
        { name, domain, path },
        { name, url: `https://${urlHost}${path}` },
        { name, url: `https://${urlHost}/` },
      ];

      let deleted = false;
      for (const details of candidates) {
        try {
          await gmCookieDelete(details);
          deleted = true;
          break;
        } catch {
          // 尝试下一个候选
        }
      }

      if (deleted) gmDeleteSuccess += 1;
      else gmDeleteFailed += 1;
    }

    log(`Cookie 清理完成: document=${docCookieNames.length}, gm_deleted=${gmDeleteSuccess}, gm_failed=${gmDeleteFailed}`);
    return { documentDeleted: docCookieNames.length, gmDeleteSuccess, gmDeleteFailed };
  }

  function scheduleFullAutoRestartToSignup() {
    const target = 'https://cloud.onyx.app/auth/signup';
    const delay = Math.max(200, Number(CONFIG.FULL_AUTO_RESTART_DELAY_MS || 1200));
    log(`全自动模式：${delay}ms 后跳回注册页重新开始`);
    setTimeout(() => {
      window.location.href = target;
    }, delay);
  }

  async function getCookieValueOnce(cookieName) {
    const name = String(cookieName || '').trim();
    if (!name) return '';

    const queries = [
      { url: 'https://cloud.onyx.app', name },
      { domain: 'cloud.onyx.app', name },
      { domain: '.onyx.app', name },
      { domain: 'onyx.app', name },
      { name },
      {},
    ];

    for (const q of queries) {
      let items = [];
      try {
        items = await gmCookieList(q);
      } catch {
        continue;
      }

      const match = items.find((item) => {
        if (!item || typeof item !== 'object') return false;
        if (String(item.name || '') !== name) return false;

        const value = String(item.value || '').trim();
        if (!value) return false;

        const expiryMs = Number(item.expirationDate || 0) * 1000;
        if (expiryMs > 0 && expiryMs <= Date.now()) return false;

        return true;
      });

      if (match) {
        return String(match.value || '').trim();
      }
    }

    return '';
  }

  async function waitForOnyxAuthCookies(timeoutMs = 120000) {
    const start = Date.now();
    let authValue = '';
    let csrfValue = '';

    while (Date.now() - start < timeoutMs) {
      if (!authValue) authValue = await getCookieValueOnce('fastapiusersauth');
      if (!csrfValue) csrfValue = await getCookieValueOnce('fastapiusersoauthcsrf');

      if (authValue && csrfValue) {
        return {
          fastapiusersauth: authValue,
          fastapiusersoauthcsrf: csrfValue,
          complete: true,
        };
      }

      if (authValue && !csrfValue) {
        // 已拿到 auth，继续短暂等待 csrf，超时后仍可回退为旧格式
        await sleep(600);
        continue;
      }

      await sleep(600);
    }

    return {
      fastapiusersauth: authValue,
      fastapiusersoauthcsrf: csrfValue,
      complete: Boolean(authValue && csrfValue),
    };
  }

  function buildOnyxCookieString(authValue, csrfValue) {
    const auth = String(authValue || '').trim();
    const csrf = String(csrfValue || '').trim();
    if (!auth) return '';
    if (csrf) return `fastapiusersauth=${auth}; fastapiusersoauthcsrf=${csrf}`;
    return `fastapiusersauth=${auth}`;
  }

  async function appendCookieToServer(cookieValue) {
    const appendCfg = getAppendCookieConfig();
    if (!appendCfg.enabled) {
      log('已禁用 Cookie 自动上报，跳过 append');
      return { ok: false, skipped: true, reason: 'disabled' };
    }

    const cookie = String(cookieValue || '').trim();
    if (!cookie) {
      throw new Error('append 失败：cookie 为空');
    }

    if (!appendCfg.url || !appendCfg.adminPassword) {
      warn('未配置 APPEND_COOKIE_URL 或 APPEND_COOKIE_ADMIN_PASSWORD，跳过 append');
      return { ok: false, skipped: true, reason: 'missing_config' };
    }

    let lastError = null;

    for (let attempt = 1; attempt <= appendCfg.maxRetry; attempt += 1) {
      try {
        const resp = await gmRequest({
          method: 'POST',
          url: appendCfg.url,
          headers: {
            'Content-Type': 'application/json',
            'x-admin-password': appendCfg.adminPassword,
          },
          data: JSON.stringify({ cookie }),
          timeout: appendCfg.timeout,
        });

        const bodyText = String(resp.responseText || '');
        const body = safeJsonParse(bodyText) || {};

        if (resp.status >= 200 && resp.status < 300 && body.ok === true) {
          log(`Cookie 已自动上报到服务器: inserted=${String(body.inserted)} total=${String(body.total)}`);
          return {
            ok: true,
            inserted: Boolean(body.inserted),
            total: Number(body.total || 0),
          };
        }

        throw new Error(`HTTP ${resp.status}: ${bodyText}`);
      } catch (e) {
        lastError = e;
        warn(`Cookie 上报失败 ${attempt}/${appendCfg.maxRetry}:`, e);
        if (attempt < appendCfg.maxRetry) {
          await sleep(800 + randInt(0, 700));
        }
      }
    }

    throw new Error(`Cookie 上报失败（重试耗尽）: ${String(lastError)}`);
  }

  async function createTempEmail() {
    log('创建临时邮箱...');

    const url = `https://${CONFIG.CF_WORKER_DOMAIN}/admin/new_address`;
    const name = createRandomName();

    const resp = await gmRequest({
      method: 'POST',
      url,
      headers: {
        'x-admin-auth': CONFIG.CF_ADMIN_PASSWORD,
        'Content-Type': 'application/json',
      },
      data: JSON.stringify({
        enablePrefix: true,
        name,
        domain: CONFIG.CF_EMAIL_DOMAIN,
      }),
      timeout: 30000,
    });

    if (resp.status !== 200) {
      throw new Error(`创建邮箱失败: HTTP ${resp.status} ${resp.responseText || ''}`);
    }

    let data;
    try {
      data = JSON.parse(resp.responseText || '{}');
    } catch (e) {
      throw new Error(`创建邮箱返回非 JSON: ${String(e)}`);
    }

    const email = data.address;
    const cfToken = data.jwt;
    if (!email || !cfToken) {
      throw new Error(`创建邮箱返回缺少字段: ${resp.responseText}`);
    }

    log(`邮箱创建成功: ${email}`);
    return { email, cfToken };
  }

  async function fetchMails(cfToken) {
    const url = `https://${CONFIG.CF_WORKER_DOMAIN}/api/mails?limit=10&offset=0`;

    const resp = await gmRequest({
      method: 'GET',
      url,
      headers: {
        Authorization: `Bearer ${cfToken}`,
        'Content-Type': 'application/json',
      },
      timeout: 30000,
    });

    if (resp.status !== 200) {
      warn(`拉取邮件失败: HTTP ${resp.status}`);
      return [];
    }

    let data;
    try {
      data = JSON.parse(resp.responseText || '{}');
    } catch {
      return [];
    }

    return Array.isArray(data.results) ? data.results : [];
  }

  function decodeQuotedPrintable(text) {
    return String(text || '')
      // quoted-printable 软换行：=\r\n 或 =\n
      .replace(/=\r?\n/g, '')
      // =3D => =，以及其它十六进制编码字节
      .replace(/=([A-Fa-f0-9]{2})/g, (_, hex) => String.fromCharCode(parseInt(hex, 16)));
  }

  function extractVerificationLink(raw) {
    if (!raw) return null;

    const pattern = /https:\/\/cloud\.onyx\.app\/auth\/verify-email\?token=[A-Za-z0-9._~-]+(?:%[0-9A-Fa-f]{2})*(?:&|&amp;)first_user=true/i;
    const candidates = [String(raw), decodeQuotedPrintable(raw)];

    for (const candidate of candidates) {
      const normalized = String(candidate || '').replace(/\s+/g, '');
      const match = normalized.match(pattern);
      if (match) {
        return match[0].replace(/&amp;/gi, '&');
      }
    }

    return null;
  }

  async function waitForVerificationEmail(cfToken, timeoutMs = 120000) {
    log(`等待验证邮件，最长 ${(timeoutMs / 1000).toFixed(0)} 秒...`);
    const start = Date.now();

    while (Date.now() - start < timeoutMs) {
      const mails = await fetchMails(cfToken);

      for (const item of mails) {
        if (!item || typeof item !== 'object') continue;
        const sender = String(item.source || '').toLowerCase();
        if (!sender.includes('onyx')) continue;

        const raw = String(item.raw || '');
        const link = extractVerificationLink(raw);
        if (link) return link;
      }

      await sleep(CONFIG.MAIL_POLL_INTERVAL_MS);
    }

    return null;
  }

  async function waitForElement(selector, timeoutMs = 30000) {
    const start = Date.now();
    while (Date.now() - start < timeoutMs) {
      const el = document.querySelector(selector);
      if (el) return el;
      await sleep(200);
    }
    return null;
  }

  async function waitForCondition(checker, timeoutMs = 30000, intervalMs = 220) {
    const start = Date.now();
    while (Date.now() - start < timeoutMs) {
      const value = checker();
      if (value) return value;
      await sleep(intervalMs);
    }
    return null;
  }

  function normalizeText(s) {
    return String(s || '').replace(/\s+/g, ' ').trim().toLowerCase();
  }

  function isVisible(el) {
    if (!(el instanceof HTMLElement)) return false;
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== 'none'
      && style.visibility !== 'hidden'
      && Number(style.opacity || '1') > 0
      && rect.width > 2
      && rect.height > 2;
  }

  function findButtonByText(texts, { exact = false, scope = document } = {}) {
    const expected = Array.isArray(texts) ? texts.map((t) => normalizeText(t)) : [normalizeText(texts)];
    const nodes = Array.from(scope.querySelectorAll('button, [role="button"]'));

    for (const node of nodes) {
      if (!isVisible(node)) continue;
      if (node.disabled || node.getAttribute('aria-disabled') === 'true') continue;
      const text = normalizeText(node.textContent || node.getAttribute('aria-label') || '');
      if (!text) continue;

      const matched = expected.some((exp) => (exact ? text === exp : text.includes(exp)));
      if (matched) return node;
    }
    return null;
  }

  function setNativeInputValue(input, value) {
    const prototype = Object.getPrototypeOf(input);
    const descriptor = Object.getOwnPropertyDescriptor(prototype, 'value')
      || Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')
      || Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value');

    if (descriptor && typeof descriptor.set === 'function') {
      descriptor.set.call(input, value);
    } else {
      input.value = value;
    }
  }

  function dispatchInputEvent(input, ch = '') {
    try {
      input.dispatchEvent(new InputEvent('input', {
        bubbles: true,
        data: ch,
        inputType: ch ? 'insertText' : 'insertReplacementText',
      }));
    } catch {
      input.dispatchEvent(new Event('input', { bubbles: true }));
    }
  }

  async function typeLikeHuman(input, text, minDelay = 50, maxDelay = 140) {
    input.focus();
    setNativeInputValue(input, '');
    dispatchInputEvent(input, '');

    for (const ch of text) {
      const next = String(input.value || '') + ch;
      setNativeInputValue(input, next);
      dispatchInputEvent(input, ch);
      await sleep(randInt(minDelay, maxDelay));
    }

    // 某些 React 表单在 blur 时会用内部 state 回写，这里先主动同步一次
    if (String(input.value || '') !== String(text)) {
      setNativeInputValue(input, text);
      dispatchInputEvent(input, '');
    }

    input.dispatchEvent(new Event('change', { bubbles: true }));
    input.dispatchEvent(new Event('blur', { bubbles: true }));
  }

  function fireKey(el, key) {
    el.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true }));
    el.dispatchEvent(new KeyboardEvent('keyup', { key, bubbles: true }));
  }

  async function selectSignupOption(maxAttempts = 8) {
    const trigger = await waitForElement("button[role='combobox'], [aria-haspopup='listbox']", 60000);
    if (!trigger) throw new Error('未找到注册来源下拉框');

    const isPlaceholder = (text) => {
      const t = String(text || '').trim().toLowerCase();
      return !t
        || t.includes('select an option')
        || t.includes('choose')
        || t.includes('请选择')
        || t.includes('选择');
    };

    const isVisible = (el) => {
      if (!(el instanceof HTMLElement)) return false;
      const style = window.getComputedStyle(el);
      const rect = el.getBoundingClientRect();
      return style.display !== 'none'
        && style.visibility !== 'hidden'
        && Number(style.opacity || '1') > 0
        && rect.width > 2
        && rect.height > 2;
    };

    const getSelectedText = () => String(trigger.textContent || '').trim();

    const current = getSelectedText();
    if (!isPlaceholder(current)) {
      log(`来源已选择: ${current}`);
      return current;
    }

    for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
      trigger.focus();
      trigger.click();
      await sleep(randInt(220, 420));

      const optionSelectors = [
        "[role='listbox'] [role='option']",
        "[data-radix-popper-content-wrapper] [role='option']",
        "[role='option']",
        "li[role='option']",
        "[data-state][role='option']",
      ];

      let options = [];
      for (const selector of optionSelectors) {
        options = Array.from(document.querySelectorAll(selector));
        if (options.length > 0) break;
      }

      options = options.filter((el) => {
        if (!isVisible(el)) return false;
        if (String(el.getAttribute('aria-disabled') || '').toLowerCase() === 'true') return false;
        const txt = String(el.textContent || '').trim();
        if (!txt) return false;
        return !isPlaceholder(txt);
      });

      if (options.length > 0) {
        const pick = options[randInt(0, options.length - 1)];
        pick.scrollIntoView({ block: 'nearest', inline: 'nearest' });
        await sleep(randInt(60, 140));
        pick.click();
        await sleep(randInt(260, 450));
      } else {
        // 兜底：键盘导航
        fireKey(trigger, 'ArrowDown');
        await sleep(randInt(80, 150));

        const extraSteps = randInt(0, 4);
        for (let i = 0; i < extraSteps; i += 1) {
          fireKey(trigger, 'ArrowDown');
          await sleep(randInt(60, 120));
        }

        fireKey(trigger, 'Enter');
        await sleep(randInt(260, 450));
      }

      const selectedText = getSelectedText();
      if (!isPlaceholder(selectedText)) {
        log(`来源选择成功: ${selectedText}`);
        return selectedText;
      }

      warn(`来源选择失败，重试 ${attempt}/${maxAttempts}`);
      fireKey(trigger, 'Escape');
      await sleep(randInt(140, 260));
    }

    throw new Error('选择注册来源失败');
  }

  function findCreateAccountButton() {
    const button = document.querySelector("button[type='submit']");
    if (button) return button;
    const buttons = Array.from(document.querySelectorAll('button'));
    return buttons.find((b) => String(b.textContent || '').trim().toLowerCase() === 'create account') || null;
  }

  function safeJsonParse(text) {
    try {
      return JSON.parse(text);
    } catch {
      return null;
    }
  }

  function getRegisterErrorMessage() {
    const el = document.querySelector('.font-secondary-body.text-text-03.ml-0\\.5');
    if (!el) return '';
    return String(el.textContent || el.innerText || '').trim();
  }

  function isNonRetryableRegisterError(message) {
    const text = normalizeText(message);
    return text.includes('account already exists');
  }

  function interceptNextRegisterResponse(timeoutMs = 120000) {
    return new Promise((resolve) => {
      const originalFetch = window.fetch;
      const originalXhrOpen = XMLHttpRequest.prototype.open;
      const originalXhrSend = XMLHttpRequest.prototype.send;
      let settled = false;

      function cleanup() {
        window.fetch = originalFetch;
        XMLHttpRequest.prototype.open = originalXhrOpen;
        XMLHttpRequest.prototype.send = originalXhrSend;
      }

      function settle(value) {
        if (settled) return;
        settled = true;
        cleanup();
        resolve(value);
      }

      window.fetch = async function (...args) {
        const req = args[0];
        const url = typeof req === 'string' ? req : (req && req.url ? req.url : '');
        const res = await originalFetch.apply(this, args);

        if (url.includes('/api/auth/register')) {
          try {
            const text = await res.clone().text();
            settle({ status: res.status, text });
          } catch {
            settle({ status: res.status, text: '' });
          }
        }

        return res;
      };

      XMLHttpRequest.prototype.open = function (method, url, ...rest) {
        this.__onyxRegisterUrl = typeof url === 'string' ? url : String(url);
        return originalXhrOpen.call(this, method, url, ...rest);
      };

      XMLHttpRequest.prototype.send = function (...args) {
        this.addEventListener('load', () => {
          const url = this.__onyxRegisterUrl || '';
          if (url.includes('/api/auth/register')) {
            settle({ status: this.status, text: this.responseText || '' });
          }
        });
        return originalXhrSend.apply(this, args);
      };

      setTimeout(() => settle(null), timeoutMs);
    });
  }

  async function submitRegisterWithRetry(expectedEmail, onSuccess) {
    const waitAttemptByUi = async (beforeHref, timeoutMs) => {
      const start = Date.now();
      let loaderSeen = false;

      while (Date.now() - start < timeoutMs) {
        const curHref = getCurrentHref();
        const path = getCurrentPath();

        const redirectedToLogin = path.startsWith('/auth/login');
        if (redirectedToLogin) {
          return { ok: false, reason: 'redirected_to_login' };
        }

        const navigated = curHref !== beforeHref
          || path.startsWith('/auth/waiting-on-verification')
          || path.startsWith('/app')
          || path.startsWith('/auth/verify-email');
        if (navigated) {
          return { ok: true, reason: 'navigated' };
        }

        const hasLoader = Boolean(document.querySelector('.loader'));
        if (hasLoader) {
          loaderSeen = true;
        }

        // 关键逻辑：出现过 loader，随后 loader 消失且页面未跳转 => 本次注册失败
        if (loaderSeen && !hasLoader) {
          await sleep(220);
          const stillSamePage = getCurrentHref() === beforeHref && getCurrentPath().startsWith('/auth/signup');
          if (stillSamePage) {
            return { ok: false, reason: 'loader_disappeared_without_navigation' };
          }
        }

        await sleep(120);
      }

      return { ok: false, reason: 'timeout' };
    };

    for (let i = 0; i < CONFIG.MAX_REGISTER_RETRY; i += 1) {
      log(`提交注册 ${i + 1}/${CONFIG.MAX_REGISTER_RETRY}...`);

      const createBtn = findCreateAccountButton();
      if (!createBtn) throw new Error('未找到 Create Account 按钮');

      const beforeHref = getCurrentHref();
      createBtn.click();

      const result = await waitAttemptByUi(beforeHref, CONFIG.REGISTER_TIMEOUT_MS);
      if (result.ok) {
        log(`注册成功（${result.reason}）`);
        if (typeof onSuccess === 'function') {
          onSuccess({ is_active: true, email: expectedEmail, detectedBy: result.reason });
        }
        return true;
      }

      if (result.reason === 'redirected_to_login') {
        const fullAutoEnabled = isFullAutoEnabled();
        warn('注册失败：提交后跳转到 /auth/login');

        if (fullAutoEnabled) {
          clearState();
          log('已启用全自动循环：将自动跳回注册页重新开始');
          scheduleFullAutoRestartToSignup();
        }

        throw new Error('注册失败：提交后跳转到登录页');
      }

      const registerErrorMessage = getRegisterErrorMessage();
      if (registerErrorMessage) {
        warn(`注册失败提示: ${registerErrorMessage}`);
      }

      if (isNonRetryableRegisterError(registerErrorMessage)) {
        const fullAutoEnabled = isFullAutoEnabled();
        warn(`注册失败：命中不可重试错误（account already exists）: ${registerErrorMessage}`);

        if (fullAutoEnabled) {
          clearState();
          log('已启用全自动循环：检测到不可重试错误，刷新页面重新开始注册');
          setTimeout(() => window.location.reload(), 120);
        }

        throw new Error(`注册失败（不可重试）: ${registerErrorMessage}`);
      }

      warn(`注册失败，准备重试：${result.reason}`);
      await sleep(1000 + randInt(0, 800));
    }

    throw new Error('注册失败：超过最大重试次数');
  }

  function inferPostVerifyPhaseByPath(path) {
    const p = String(path || '');
    if (p.startsWith('/admin/api-key')) return 'post_verify_reporting_cookie';
    return 'post_verify_open_agent_page';
  }

  async function loginFromLoginPage(email, password) {
    log('检测到跳转到登录页，尝试自动登录...');

    const emailInput = await waitForElement("input[name='email']", 30000);
    const passwordInput = await waitForElement("input[name='password']", 30000);
    const submitBtn = await waitForCondition(() => {
      const btn = document.querySelector("button[type='submit']");
      return (btn && !btn.disabled) ? btn : null;
    }, 30000, 180);

    if (!emailInput || !passwordInput || !submitBtn) {
      throw new Error('登录页自动登录失败：未找到 email/password/submit 元素');
    }

    await typeLikeHuman(emailInput, email, 40, 120);
    await sleep(randInt(120, 320));
    await typeLikeHuman(passwordInput, password, 50, 130);
    await sleep(randInt(120, 320));

    submitBtn.click();

    const landingPath = await waitForCondition(() => {
      const path = getCurrentPath();
      if (
        path.startsWith('/app')
        || path.startsWith('/admin/api-key')
        || path.startsWith('/auth/waiting-on-verification')
        || path.startsWith('/auth/verify-email')
      ) {
        return path;
      }
      return null;
    }, CONFIG.REGISTER_TIMEOUT_MS, 220);

    if (!landingPath) {
      throw new Error('登录页自动登录失败：提交后未跳转到预期页面');
    }

    if (landingPath.startsWith('/auth/waiting-on-verification') || landingPath.startsWith('/auth/verify-email')) {
      log('登录后仍需邮箱验证，继续等待验证邮件流程');
      return { requiresEmailVerification: true, landingPath };
    }

    const phase = inferPostVerifyPhaseByPath(landingPath);
    saveState({ phase, email, password });
    log(`登录成功并进入已激活路径: ${landingPath}`);
    return { requiresEmailVerification: false, landingPath };
  }

  function getCurrentPath() {
    return String(window.location.pathname || '');
  }

  function getCurrentHref() {
    return String(window.location.href || '');
  }

  function makeAgentName() {
    const suffix = Date.now().toString().slice(-6);
    return `${CONFIG.AGENT_NAME_PREFIX} ${suffix}`;
  }

  function extractAssistantIdFromHref(href) {
    try {
      const url = new URL(String(href || ''), window.location.origin);
      return url.searchParams.get('assistantId') || null;
    } catch {
      return null;
    }
  }

  async function createAgentOnCurrentPage(state) {
    log('开始创建 Agent...');

    const agentName = state.agentName || makeAgentName();
    saveState({ phase: 'post_verify_creating_agent', agentName });

    const nameInput = await waitForElement("input[name='name'], input[placeholder*='Agent'], input[aria-label*='Agent']", CONFIG.AGENT_TIMEOUT_MS);
    if (!nameInput) {
      throw new Error('创建 Agent 失败：未找到名称输入框');
    }

    await typeLikeHuman(nameInput, agentName, 40, 120);
    await sleep(randInt(140, 340));

    let submitBtn = document.querySelector("button[type='submit']");
    if (!submitBtn || submitBtn.disabled) {
      submitBtn = findButtonByText(['create', '创建']);
    }
    if (!submitBtn) {
      throw new Error('创建 Agent 失败：未找到提交按钮');
    }

    submitBtn.click();
    log(`已提交 Agent 创建: ${agentName}`);

    const successHref = await waitForCondition(() => {
      const href = getCurrentHref();
      if (href.includes('/app?assistantId=')) return href;
      return null;
    }, CONFIG.AGENT_TIMEOUT_MS, 250);

    if (!successHref) {
      throw new Error('创建 Agent 后未跳转到 /app?assistantId=...');
    }

    const assistantId = extractAssistantIdFromHref(successHref);
    saveLastRegisteredAccount({ agentName, assistantId: assistantId || null });
    saveState({
      phase: 'post_verify_reporting_cookie',
      agentName,
      assistantId: assistantId || null,
    });

    log(`Agent 创建成功: name=${agentName}, assistantId=${assistantId || 'unknown'}`);
    await reportCookieOnCurrentPage({
      ...state,
      agentName,
      assistantId: assistantId || null,
    });
    return true;
  }

  async function reportCookieOnCurrentPage(state) {
    log('开始读取并上报 Onyx Cookie（fastapiusersauth + fastapiusersoauthcsrf）...');
    saveState({ phase: 'post_verify_reporting_cookie' });

    const cookieInfo = await waitForOnyxAuthCookies(CONFIG.COOKIE_WAIT_TIMEOUT_MS);
    const authValue = String(cookieInfo.fastapiusersauth || '').trim();
    const csrfValue = String(cookieInfo.fastapiusersoauthcsrf || '').trim();

    if (!authValue) {
      throw new Error('未在超时时间内读取到 fastapiusersauth Cookie');
    }

    const cookieString = buildOnyxCookieString(authValue, csrfValue);

    let appendResult = null;
    try {
      appendResult = await appendCookieToServer(cookieString);
    } catch (e) {
      warn('Cookie 自动上报失败（本地已读取到 cookie）:', e);
    }

    saveLastRegisteredAccount({
      fastapiusersauth: authValue,
      fastapiusersoauthcsrf: csrfValue || null,
      cookie: cookieString,
      cookie_format: csrfValue ? 'dual' : 'legacy_single',
      agentName: state.agentName || null,
      assistantId: state.assistantId || null,
      appendResult,
      postVerifiedAt: Date.now(),
    });

    const appendSuccess = Boolean(appendResult && appendResult.ok === true);
    const fullAutoEnabled = isFullAutoEnabled();

    saveState({
      phase: 'post_verify_done',
      fastapiusersauth: authValue,
      fastapiusersoauthcsrf: csrfValue || null,
      cookie: cookieString,
      appendResult,
      completedAt: Date.now(),
      fullAutoEnabled,
    });

    if (appendSuccess && fullAutoEnabled) {
      try {
        await clearAllCookiesForCurrentSite();
      } catch (e) {
        warn('全自动模式：Cookie 清理失败，将继续跳回注册页:', e);
      }

      clearState();
      log(`Cookie 上报成功，已进入全自动重启流程: ${authValue.slice(0, 12)}...`);
      scheduleFullAutoRestartToSignup();
      return true;
    }

    clearState();
    log(`Cookie 上报流程完成: ${authValue.slice(0, 12)}...`);
    return true;
  }

  function isPostVerifyPhase(phase) {
    return [
      'verified_link_found',
      'post_verify_pending',
      'post_verify_open_agent_page',
      'post_verify_creating_agent',
      'post_verify_reporting_cookie',
      // 兼容旧阶段名
      'post_verify_open_api_key_page',
      'post_verify_creating_api_key',
    ].includes(String(phase || ''));
  }

  async function resumePostVerificationFromState(from = 'auto', options = {}) {
    const requireState = options.requireState !== undefined
      ? Boolean(options.requireState)
      : (from === 'auto');

    const path = getCurrentPath();
    const rawState = loadState();
    const stateFromStorage = (rawState && isStateFresh(rawState)) ? rawState : null;
    if (rawState && !stateFromStorage) {
      warn('验证后状态已过期，自动清理');
      clearState();
    }

    if (requireState && !stateFromStorage) {
      log('无可用注册状态，跳过验证后自动启动');
      return false;
    }

    const last = loadLastRegisteredAccount() || {};
    let state = {
      ...last,
      ...(stateFromStorage || {}),
    };

    if (!isPostVerifyPhase(state.phase)) {
      if (path.startsWith('/admin/api-key')) {
        state = { ...state, phase: 'post_verify_reporting_cookie' };
      } else if (path.startsWith('/app/agents/create')) {
        state = { ...state, phase: 'post_verify_creating_agent' };
      } else if (path.startsWith('/app')) {
        state = { ...state, phase: 'post_verify_open_agent_page' };
      } else {
        return false;
      }
    }

    if (running) return true;
    running = true;

    try {
      log(`继续执行验证后流程（来源=${from}，阶段=${state.phase}，路径=${path}）`);

      if (state.phase === 'verified_link_found' || state.phase === 'post_verify_pending' || state.phase === 'post_verify_open_agent_page' || state.phase === 'post_verify_creating_agent') {
        if (!path.startsWith('/app/agents/create')) {
          saveState({ phase: 'post_verify_open_agent_page' });
          window.location.href = 'https://cloud.onyx.app/app/agents/create';
          return true;
        }
        await createAgentOnCurrentPage(state);
        return true;
      }

      if (
        state.phase === 'post_verify_reporting_cookie'
        || state.phase === 'post_verify_open_api_key_page'
        || state.phase === 'post_verify_creating_api_key'
      ) {
        await reportCookieOnCurrentPage(state);
        return true;
      }

      if (path.startsWith('/admin/api-key')) {
        await reportCookieOnCurrentPage(state);
        return true;
      }

      if (path.startsWith('/app')) {
        saveState({ phase: 'post_verify_open_agent_page' });
        window.location.href = 'https://cloud.onyx.app/app/agents/create';
        return true;
      }

      return false;
    } catch (e) {
      err('验证后自动化流程失败:', e);
      saveState({
        phase: state.phase || 'post_verify_pending',
        postVerifyError: String(e),
        postVerifyFailedAt: Date.now(),
      });
      running = false;
      return false;
    }
  }

  async function run() {
    if (!CONFIG.CF_WORKER_DOMAIN || !CONFIG.CF_EMAIL_DOMAIN || !CONFIG.CF_ADMIN_PASSWORD) {
      throw new Error('请先在 CONFIG 中填写 CF_WORKER_DOMAIN / CF_EMAIL_DOMAIN / CF_ADMIN_PASSWORD');
    }

    log('开始自动注册流程');

    // 1) 创建临时邮箱
    const { email, cfToken } = await createTempEmail();
    const password = generateRandomPassword(CONFIG.PASSWORD_LENGTH);

    // 保存初始状态，便于跳转后继续
    saveState({
      phase: 'form_filling',
      email,
      password,
      cfToken,
      createdAt: Date.now(),
    });

    // 2) 选择注册来源
    await selectSignupOption(6);

    // 3) 填写邮箱与密码
    const emailInput = await waitForElement("input[name='email']", 30000);
    const passwordInput = await waitForElement("input[name='password']", 30000);
    if (!emailInput || !passwordInput) {
      throw new Error('未找到邮箱/密码输入框');
    }

    await typeLikeHuman(emailInput, email, 60, 220);
    await sleep(randInt(300, 900));
    await typeLikeHuman(passwordInput, password, 80, 180);

    log(`已填入邮箱: ${email}`);
    log(`已填入密码(前5位): ${password.slice(0, 5)}*****`);

    // 4) 提交注册（含重试）
    await submitRegisterWithRetry(email, (data) => {
      saveState({
        phase: 'register_submitted',
        registerResponse: data,
      });
    });

    // 5) 处理提交后分支：
    // - 正常：等待验证邮件并跳转
    // - 异常但可利用：被直接跳到 /auth/login，则自动登录并继续后续流程
    const currentPathAfterSubmit = getCurrentPath();
    if (currentPathAfterSubmit.startsWith('/auth/login')) {
      const loginResult = await loginFromLoginPage(email, password);
      if (!loginResult.requiresEmailVerification) {
        saveLastRegisteredAccount({
          email,
          password,
          bypassedEmailVerification: true,
          loginLandingPath: loginResult.landingPath,
        });
        await resumePostVerificationFromState('auto', { requireState: false });
        return;
      }
    }

    saveState({ phase: 'waiting_email' });
    const authLink = await waitForVerificationEmail(cfToken, CONFIG.MAIL_TIMEOUT_MS);
    if (!authLink) {
      throw new Error('未在超时时间内收到验证邮件');
    }

    log(`验证链接: ${authLink}`);

    // 便于你后续留档
    saveLastRegisteredAccount({
      email,
      password,
      authLink,
      bypassedEmailVerification: false,
    });

    // 保留状态，验证成功后在 app 页面继续创建 Agent 并上报 Cookie
    saveState({
      phase: 'verified_link_found',
      authLink,
      email,
      password,
    });
    window.location.href = authLink;
  }

  let running = false;

  function isStateFresh(state) {
    if (!state || typeof state !== 'object') return false;
    const baseTs = Number(state.updatedAt || state.createdAt || 0);
    if (!baseTs) return false;
    return (Date.now() - baseTs) <= CONFIG.STATE_MAX_AGE_MS;
  }

  async function resumeWaitingVerificationFromState(from = 'auto') {
    const state = loadState();
    if (!state) {
      warn('未找到可恢复状态，无法在等待页继续轮询邮箱');
      return false;
    }

    if (!isStateFresh(state)) {
      warn('恢复状态已过期，自动清理');
      clearState();
      return false;
    }

    if (!state.cfToken) {
      warn('恢复状态缺少 cfToken，无法继续轮询');
      return false;
    }

    if (running) return true;
    running = true;

    try {
      log(`从等待页继续轮询验证邮件（来源=${from}，邮箱=${maskEmail(state.email)}）`);
      saveState({ phase: 'waiting_email' });

      const authLink = await waitForVerificationEmail(state.cfToken, CONFIG.MAIL_TIMEOUT_MS);
      if (!authLink) {
        throw new Error('等待页：未在超时时间内收到验证邮件');
      }

      saveLastRegisteredAccount({
        email: state.email,
        password: state.password,
        authLink,
      });

      saveState({
        phase: 'verified_link_found',
        authLink,
        email: state.email,
        password: state.password,
      });
      window.location.href = authLink;
      return true;
    } catch (e) {
      err('等待页继续轮询失败:', e);
      saveState({ phase: 'waiting_email_failed', error: String(e) });
      running = false;
      return false;
    }
  }

  async function startRegisterFlow(source = 'manual') {
    if (running) return false;

    const btn = document.getElementById('onyx-auto-register-btn');
    running = true;

    if (btn) {
      btn.disabled = true;
      btn.style.opacity = '0.8';
      btn.style.cursor = 'not-allowed';
      btn.textContent = source === 'full_auto' ? '全自动执行中...' : '注册中...';
    }

    try {
      await run();
      if (btn) btn.textContent = '已提交，跳转验证中...';
      return true;
    } catch (e) {
      err('自动注册失败:', e);
      running = false;
      if (btn) {
        btn.disabled = false;
        btn.style.opacity = '1';
        btn.style.cursor = 'pointer';
        btn.textContent = '开始注册';
      }
      return false;
    }
  }

  function createFullAutoToggleButton() {
    if (document.getElementById('onyx-full-auto-toggle-btn')) return;

    const btn = document.createElement('button');
    btn.id = 'onyx-full-auto-toggle-btn';
    btn.type = 'button';
    btn.style.position = 'fixed';
    btn.style.top = '16px';
    btn.style.left = '16px';
    btn.style.zIndex = '999999';
    btn.style.padding = '10px 14px';
    btn.style.border = 'none';
    btn.style.borderRadius = '8px';
    btn.style.color = '#fff';
    btn.style.fontSize = '14px';
    btn.style.fontWeight = '600';
    btn.style.cursor = 'pointer';
    btn.style.boxShadow = '0 4px 12px rgba(0,0,0,.25)';

    const render = () => {
      const enabled = isFullAutoEnabled();
      btn.textContent = enabled ? '全自动循环：开' : '全自动循环：关';
      btn.style.background = enabled ? '#0f766e' : '#6b7280';
    };

    btn.addEventListener('click', () => {
      const next = !isFullAutoEnabled();
      saveFullAutoEnabled(next);
      render();
      log(`全自动循环已${next ? '启用' : '关闭'}`);
    });

    render();
    document.body.appendChild(btn);
    log('已注入“全自动循环”按钮');
  }

  function createStartButton() {
    if (document.getElementById('onyx-auto-register-btn')) return;

    const btn = document.createElement('button');
    btn.id = 'onyx-auto-register-btn';
    btn.textContent = '开始注册';
    btn.type = 'button';
    btn.style.position = 'fixed';
    btn.style.top = '16px';
    btn.style.right = '16px';
    btn.style.zIndex = '999999';
    btn.style.padding = '10px 14px';
    btn.style.border = 'none';
    btn.style.borderRadius = '8px';
    btn.style.background = '#16a34a';
    btn.style.color = '#fff';
    btn.style.fontSize = '14px';
    btn.style.fontWeight = '600';
    btn.style.cursor = 'pointer';
    btn.style.boxShadow = '0 4px 12px rgba(0,0,0,.25)';

    btn.addEventListener('click', async () => {
      await startRegisterFlow('manual');
    });

    document.body.appendChild(btn);
    log('已注入“开始注册”按钮');
  }

  function createResumeButton() {
    if (document.getElementById('onyx-auto-resume-btn')) return;

    const btn = document.createElement('button');
    btn.id = 'onyx-auto-resume-btn';
    btn.textContent = '继续验证邮箱';
    btn.type = 'button';
    btn.style.position = 'fixed';
    btn.style.top = '16px';
    btn.style.right = '16px';
    btn.style.zIndex = '999999';
    btn.style.padding = '10px 14px';
    btn.style.border = 'none';
    btn.style.borderRadius = '8px';
    btn.style.background = '#2563eb';
    btn.style.color = '#fff';
    btn.style.fontSize = '14px';
    btn.style.fontWeight = '600';
    btn.style.cursor = 'pointer';
    btn.style.boxShadow = '0 4px 12px rgba(0,0,0,.25)';

    btn.addEventListener('click', async () => {
      if (running) return;
      btn.disabled = true;
      btn.style.opacity = '0.8';
      btn.style.cursor = 'not-allowed';
      btn.textContent = '轮询中...';

      const ok = await resumeWaitingVerificationFromState('manual');
      if (!ok) {
        btn.disabled = false;
        btn.style.opacity = '1';
        btn.style.cursor = 'pointer';
        btn.textContent = '继续验证邮箱';
      }
    });

    document.body.appendChild(btn);
    log('已注入“继续验证邮箱”按钮');
  }

  function createPostVerifyResumeButton() {
    if (document.getElementById('onyx-auto-postverify-btn')) return;

    const btn = document.createElement('button');
    btn.id = 'onyx-auto-postverify-btn';
    btn.textContent = '继续创建 Agent/上报Cookie';
    btn.type = 'button';
    btn.style.position = 'fixed';
    btn.style.top = '16px';
    btn.style.right = '16px';
    btn.style.zIndex = '999999';
    btn.style.padding = '10px 14px';
    btn.style.border = 'none';
    btn.style.borderRadius = '8px';
    btn.style.background = '#7c3aed';
    btn.style.color = '#fff';
    btn.style.fontSize = '14px';
    btn.style.fontWeight = '600';
    btn.style.cursor = 'pointer';
    btn.style.boxShadow = '0 4px 12px rgba(0,0,0,.25)';

    btn.addEventListener('click', async () => {
      if (running) return;
      btn.disabled = true;
      btn.style.opacity = '0.8';
      btn.style.cursor = 'not-allowed';
      btn.textContent = '执行中...';

      const ok = await resumePostVerificationFromState('manual', { requireState: false });
      if (!ok) {
        btn.disabled = false;
        btn.style.opacity = '1';
        btn.style.cursor = 'pointer';
        btn.textContent = '继续创建 Agent/上报Cookie';
      }
    });

    document.body.appendChild(btn);
    log('已注入“继续创建 Agent/上报Cookie”按钮');
  }

  function bootstrap() {
    const start = () => {
      const path = window.location.pathname;
      createFullAutoToggleButton();

      const isWaitingPage = path.startsWith('/auth/waiting-on-verification');
      if (isWaitingPage) {
        createResumeButton();
        resumeWaitingVerificationFromState('auto').catch((e) => err('等待页自动恢复失败:', e));
        return;
      }

      const isPostVerifyPage = path.startsWith('/app') || path.startsWith('/admin/api-key');
      if (isPostVerifyPage) {
        createPostVerifyResumeButton();

        const state = loadState();
        const shouldAutoResume = Boolean(state && isStateFresh(state) && isPostVerifyPhase(state.phase));
        if (shouldAutoResume) {
          resumePostVerificationFromState('auto', { requireState: true }).catch((e) => err('验证后流程自动恢复失败:', e));
        } else {
          log('未检测到可恢复注册状态：仅提供手动继续按钮');
        }
        return;
      }

      createStartButton();

      const isSignupPage = path.startsWith('/auth/signup');
      if (isSignupPage && isFullAutoEnabled()) {
        setTimeout(() => {
          startRegisterFlow('full_auto').catch((e) => err('全自动启动失败:', e));
        }, 260);
      }
    };

    const runStart = () => {
      try {
        start();
      } catch (e) {
        err('路由切换后执行 start 失败:', e);
      }
    };

    let lastHref = getCurrentHref();
    const onRouteMaybeChanged = (reason = 'unknown') => {
      const href = getCurrentHref();
      if (href === lastHref) return;
      lastHref = href;
      log(`检测到 SPA 路由变化(${reason}): ${href}`);
      runStart();
      setTimeout(runStart, 300);
      setTimeout(runStart, 900);
    };

    const wrapHistoryMethod = (name) => {
      const fn = history[name];
      if (typeof fn !== 'function') return;
      history[name] = function patchedHistory(...args) {
        const result = fn.apply(this, args);
        onRouteMaybeChanged(name);
        return result;
      };
    };

    wrapHistoryMethod('pushState');
    wrapHistoryMethod('replaceState');
    window.addEventListener('popstate', () => onRouteMaybeChanged('popstate'));
    window.addEventListener('hashchange', () => onRouteMaybeChanged('hashchange'));

    const ensureButtonsObserver = new MutationObserver(() => {
      const path = getCurrentPath();
      if (path.startsWith('/auth/signup') && !document.getElementById('onyx-auto-register-btn')) {
        createStartButton();
      }
    });

    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', () => {
        runStart();
        ensureButtonsObserver.observe(document.documentElement, { childList: true, subtree: true });
      }, { once: true });
    } else {
      runStart();
      ensureButtonsObserver.observe(document.documentElement, { childList: true, subtree: true });
    }
  }

  bootstrap();
})();
