'use strict';
/**
 * Protect a Node.js app: sign in with a work email or an invitation link. One file, no dependencies, Node 18+.
 *
 *   const magicLink = require('./magic-link');
 *   app.use(magicLink());                                               // Express / Connect
 *
 *   const guard = magicLink();                                          // plain http
 *   http.createServer((req, res) => guard(req, res, () => handler(req, res)));
 *
 * Two ways to let someone in:
 *   - Email sign-in, like sharing a Google Doc. The owner lists addresses in ACCESS_EMAILS. A person enters their
 *     email and gets a message with a sign-in link (valid for 15 minutes, once); the browser remembers the sign-in
 *     for 30 days. An unknown address is an access request.
 *   - Invitation link https://<app>/?token=<token> (ACCESS_TOKEN): sets a cookie and redirects without the token.
 * Without access: a 403 page with an email sign-in form (if ACCESS_EMAILS is set) or a "Request access" button.
 *
 * Environment variables:
 *   ACCESS_EMAILS       comma-separated addresses; "@company.com" means the whole domain. Remove an address and
 *                       sign-in stops working immediately.
 *   SESSION_SECRET      key that signs links and sessions (small generates it). If unset, a random one per start.
 *   RESEND_API_KEY, MAIL_FROM   email through Resend (HTTPS). The Node version has no SMTP: use Resend.
 *   LOGIN_LINKS_TO_LOG  1: don't send email, write the sign-in link to the log instead. For testing only.
 *   PUBLIC_URL          app address used in email links; otherwise RAILWAY_PUBLIC_DOMAIN; locally, localhost.
 *   ACCESS_TOKEN        comma-separated invitation-link tokens, optionally named: "alice:token1,bob:token2".
 *   ACCESS_REQUEST_URL  where the "Request access" button leads when there is no email sign-in
 *                       (mailto:..., https://t.me/...).
 *   TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID  access requests reach the owner in Telegram (small notify telegram).
 *   ACCESS_LANG         language of pages and emails: ru, en or auto (default: from the Accept-Language header).
 *   OWNER_LANG          language of messages for the owner (log lines and Telegram notifications): en (default)
 *                       or ru. small deploy sets it to match small's language.
 */

const crypto = require('node:crypto');

const COOKIE_NAME = 'small_access';
const COOKIE_MAX_AGE = 30 * 24 * 3600;
const LINK_TTL = 15 * 60;
const REQUEST_PATH = '/_access/request';
const LOGIN_PATH = '/_access/login';
const VERIFY_PATH = '/_access/verify';
const LOGOUT_PATH = '/_access/logout';
// How many sign-in emails can be requested within 15 minutes: per address and per IP.
const LIMIT_PER_EMAIL = 3;
const LIMIT_PER_IP = 10;
const LIMIT_WINDOW = 15 * 60 * 1000;

const EMAIL_RE = /^[^@\s,;<>"']+@[^@\s,;<>"']+\.[^@\s,;<>"']+$/;
const DOMAIN_RE = /^@[^@\s,;<>"']+\.[^@\s,;<>"']+$/;

const LANGS = ['ru', 'en'];
const DEFAULT_LANG = 'ru'; // when the browser doesn't say which language it prefers

const MESSAGES = {
  ru: {
    forbidden: ['&#128274;', 'Доступ по приглашению', 'Эта страница открывается только по ссылке-приглашению от владельца.'],
    invalid: ['&#128274;', 'Ссылка недействительна', 'Ссылка устарела или скопирована не полностью. Запросите новую.'],
    sent: ['&#9989;', 'Запрос отправлен', 'Владелец увидит запрос и пришлёт вам ссылку-приглашение.'],
    login: ['&#128274;', 'Вход по рабочей почте',
      'Приложение открывается только для приглашённых. Введите рабочую почту — пришлём ссылку для входа.'],
    check: ['&#9993;&#65039;', 'Проверьте почту',
      'Если у адреса есть доступ, ссылка для входа уже в почте: она действует 15 минут. '
      + 'Если доступа нет, владелец получит ваш запрос.'],
    bad_email: ['&#9888;&#65039;', 'Проверьте адрес', 'Похоже, в адресе почты ошибка. Введите его ещё раз.'],
    slow: ['&#8987;', 'Слишком много попыток', 'Подождите 15 минут и попробуйте снова.'],
    expired: ['&#8987;', 'Ссылка для входа устарела', 'Ссылка действует 15 минут и только один раз. Введите почту — пришлём новую.'],
    confirm: ['&#128274;', 'Вход в приложение', 'Нажмите кнопку, чтобы войти как %s.'],
    logout: ['&#128075;', 'Вы вышли', 'Чтобы войти снова, введите рабочую почту.'],
  },
  en: {
    forbidden: ['&#128274;', 'Invitation only', 'This page opens only with an invitation link from its owner.'],
    invalid: ['&#128274;', 'Invalid link', 'The link has expired or was copied incompletely. Ask for a new one.'],
    sent: ['&#9989;', 'Request sent', 'The owner will see your request and send you an invitation link.'],
    login: ['&#128274;', 'Sign in with your work email',
      "This app is for invited people only. Enter your work email and we'll send you a sign-in link."],
    check: ['&#9993;&#65039;', 'Check your email',
      'If this address has access, a sign-in link is already in your inbox. It works for 15 minutes. '
      + "If it doesn't, the owner will get your request."],
    bad_email: ['&#9888;&#65039;', 'Check the address', 'This email address looks wrong. Please enter it again.'],
    slow: ['&#8987;', 'Too many attempts', 'Please wait 15 minutes and try again.'],
    expired: ['&#8987;', 'Sign-in link expired', 'The link works for 15 minutes and only once. Enter your email to get a new one.'],
    confirm: ['&#128274;', 'Sign in', 'Click the button to sign in as %s.'],
    logout: ['&#128075;', 'Signed out', 'To sign in again, enter your work email.'],
  },
};

const UI = {
  ru: {
    request: 'Запросить доступ', contact: 'Email или Telegram для ответа', getLink: 'Получить ссылку для входа',
    signIn: 'Войти', other: 'Ввести другой адрес', subject: (app) => `Вход в ${app}`,
    mailText: (app, link) => `Ссылка для входа в ${app}:\n${link}\n\nОна действует 15 минут и только один раз. `
      + 'Если вы не запрашивали вход, просто удалите это письмо.\n',
    mailIntro: (app) => `Ссылка для входа в <b>${app}</b>:`,
    mailNote: 'Она действует 15 минут и только один раз. Если вы не запрашивали вход, просто удалите это письмо.',
  },
  en: {
    request: 'Request access', contact: 'Email or Telegram to reach you', getLink: 'Email me a sign-in link',
    signIn: 'Sign in', other: 'Use a different address', subject: (app) => `Sign in to ${app}`,
    mailText: (app, link) => `Sign-in link for ${app}:\n${link}\n\nIt works for 15 minutes and only once. `
      + "If you didn't ask to sign in, just delete this email.\n",
    mailIntro: (app) => `Sign-in link for <b>${app}</b>:`,
    mailNote: "It works for 15 minutes and only once. If you didn't ask to sign in, just delete this email.",
  },
};

// Page language: ACCESS_LANG (ru/en), otherwise whichever of ru/en Accept-Language prefers most.
function pickLang(acceptLanguage, setting = 'auto') {
  if (LANGS.includes(setting)) return setting;
  if (!String(acceptLanguage || '').trim()) return DEFAULT_LANG;
  let best = 'en'; // browser wants some other language (de, fr...): English is a safer bet than Russian
  let bestQ = -1;
  for (const part of String(acceptLanguage).split(',')) {
    const [tag, ...params] = part.trim().split(';');
    let base = tag.trim().toLowerCase().split('-')[0];
    if (base === '*') base = DEFAULT_LANG; // "any language" (what Node's fetch sends by default)
    const qParam = params.map((p) => p.trim()).find((p) => p.startsWith('q='));
    const q = qParam ? Number(qParam.slice(2)) || 0 : 1;
    if (LANGS.includes(base) && q > bestQ) { best = base; bestQ = q; }
  }
  return best;
}

function magicLink(options = {}) {
  const entries = parseTokens(options.tokens ?? process.env.ACCESS_TOKEN ?? '');
  const tokens = entries.map((e) => [e.name, e.token]);
  const cookies = entries.map((e) => [e.name, cookieValue(e.token)]);
  const emails = parseEmails(options.emails ?? process.env.ACCESS_EMAILS ?? '');
  let secret = String(options.secret ?? process.env.SESSION_SECRET ?? '').trim();
  if (emails.size && !secret) {
    console.log(ownerText('[magic-link] SESSION_SECRET is not set: everyone will have to sign in again after a restart',
      '[magic-link] SESSION_SECRET не задан: после перезапуска приложения всем придётся войти заново'));
  }
  secret = secret || crypto.randomBytes(32).toString('hex');
  const requestUrl = options.requestUrl ?? process.env.ACCESS_REQUEST_URL ?? '';
  const langSetting = String(options.lang ?? process.env.ACCESS_LANG ?? 'auto').trim().toLowerCase();
  const langOf = (req) => pickLang(req.headers['accept-language'], langSetting);
  const cookieName = options.cookieName ?? COOKIE_NAME;
  const maxAge = options.maxAge ?? COOKIE_MAX_AGE;
  const telegramToken = String(process.env.TELEGRAM_BOT_TOKEN || '').trim();
  const telegramChat = String(process.env.TELEGRAM_CHAT_ID || '').trim();
  const usedLinks = new Map(); // one-time sign-in links: nonce -> expiry (ms)
  const attempts = new Map(); // email rate limiting: key -> [request times]
  if (!entries.length && !emails.size) {
    console.log(ownerText('[magic-link] neither ACCESS_TOKEN nor ACCESS_EMAILS is set: nobody can get in',
      '[magic-link] ACCESS_TOKEN и ACCESS_EMAILS не заданы: доступ закрыт для всех'));
  }
  if (emails.size && !mailMode()) {
    console.log(ownerText('[magic-link] email is not set up (RESEND_API_KEY): sign-in emails will not be sent. Set it up: small mail',
      '[magic-link] почта не настроена (RESEND_API_KEY): письма для входа не уйдут. Подключить: small mail'));
  }

  const base = (req) => req.baseUrl || '';
  const clientIp = (req) => String(req.headers['x-forwarded-for'] || req.socket.remoteAddress || '').split(',')[0].trim();
  const isHttps = (req) => String(req.headers['x-forwarded-proto'] || '').split(',')[0].trim() === 'https'
    || Boolean(req.socket.encrypted);
  const cookie = (value, https) => `${cookieName}=${value}; Path=/; Max-Age=${maxAge}; HttpOnly; SameSite=Lax${https ? '; Secure' : ''}`;

  function page(req, res, status, kind, { email = '', token = '', headers = {} } = {}) {
    if (kind === 'forbidden' && emails.size) kind = 'login';
    const lang = langOf(req);
    const ui = UI[lang];
    const [icon, title, rawText] = MESSAGES[lang][kind];
    const text = kind === 'confirm' ? rawText.replace('%s', email) : rawText;
    let action = '';
    if (kind === 'check') action = `<a class="link" href="${escapeHtml(base(req))}/">${ui.other}</a>`;
    else if (kind === 'confirm') action = confirmForm(escapeHtml(base(req) + VERIFY_PATH), escapeHtml(token), ui.signIn);
    else if (kind === 'sent') action = '';
    else if (emails.size) action = loginForm(escapeHtml(base(req) + LOGIN_PATH), ui.getLink);
    else if (requestUrl) action = `<a class="btn" href="${escapeHtml(requestUrl)}">${ui.request}</a>`;
    else action = requestForm(escapeHtml(base(req) + REQUEST_PATH), ui.contact, ui.request);
    const body = renderPage(lang, icon, escapeHtml(title), escapeHtml(text), action);
    res.writeHead(status, {
      'Content-Type': 'text/html; charset=utf-8',
      'Content-Length': Buffer.byteLength(body),
      'Cache-Control': 'no-store',
      'Referrer-Policy': 'no-referrer',
      'Content-Language': lang,
      ...headers,
    });
    res.end(body);
  }

  // Form fields: from req.body (express.urlencoded()) or by reading the stream ourselves.
  function readForm(req, callback) {
    if (req.body !== undefined || req.readableEnded) return callback(new URLSearchParams(req.body || {}));
    let body = '';
    req.setEncoding('utf8');
    req.on('data', (chunk) => { if (body.length < 4096) body += chunk; });
    req.on('end', () => callback(new URLSearchParams(body.slice(0, 4096))));
  }

  function notifyRequest(req, contact) {
    const entry = { contact: String(contact || '').slice(0, 200), ip: clientIp(req), ua: String(req.headers['user-agent'] || '').slice(0, 200) };
    // JSON.stringify escapes newlines, so a user can't forge log lines.
    console.log('[access-request] ' + JSON.stringify(entry));
    if (!(telegramToken && telegramChat)) return;
    // No await: the user doesn't wait for Telegram. No parse_mode, so form text can't add markup.
    const api = String(process.env.TELEGRAM_API_URL || 'https://api.telegram.org').replace(/\/+$/, '');
    const hint = 'small access add ' + (EMAIL_RE.test(entry.contact) ? entry.contact : ownerText('NAME', 'ИМЯ'));
    const host = req.headers.host || ownerText('the app', 'приложению');
    const text = ownerText(
      `🔑 Access request to ${host}\nContact: ${entry.contact || '—'}\nIP: ${entry.ip || '—'}\n\nGrant access: ${hint}`,
      `🔑 Запрос доступа к ${host}\nКонтакт: ${entry.contact || '—'}\nIP: ${entry.ip || '—'}\n\nВыдать доступ: ${hint}`);
    fetch(`${api}/bot${telegramToken}/sendMessage`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ chat_id: telegramChat, text }),
    }).catch((e) => console.log(ownerText('[magic-link] could not send the access request to Telegram: ',
      '[magic-link] не удалось отправить запрос доступа в Telegram: ') + e.name));
  }

  function emailAllowed(email) {
    email = String(email).toLowerCase();
    return emails.has(email) || emails.has('@' + email.slice(email.lastIndexOf('@') + 1));
  }

  // purpose keeps sign-in links and sessions apart: one can't be passed off as the other.
  const mac = (purpose, payload) => crypto.createHmac('sha256', secret).update(`${purpose}|${payload}`).digest('base64url');
  const sign = (purpose, data) => {
    const payload = Buffer.from(JSON.stringify(data)).toString('base64url');
    return `${payload}.${mac(purpose, payload)}`;
  };
  function unsign(purpose, value) {
    const [payload, signature = ''] = String(value || '').split('.');
    if (!payload || !safeEqual(signature, mac(purpose, payload))) return null;
    try {
      const data = JSON.parse(Buffer.from(payload, 'base64url').toString('utf8'));
      return data && Number.isInteger(data.x) && data.x >= Date.now() / 1000 ? data : null;
    } catch {
      return null;
    }
  }

  function cookieOwner(req) {
    const value = readCookie(req.headers.cookie, cookieName);
    if (value && value.startsWith('e.')) {
      const data = unsign('session', value.slice(2));
      return data && emailAllowed(data.e) ? data.e : null;
    }
    return owner(value, cookies);
  }

  function allow(key, limit) {
    const now = Date.now();
    const recent = (attempts.get(key) || []).filter((t) => t > now - LIMIT_WINDOW);
    if (attempts.size > 10000) for (const [k, v] of attempts) if (v[v.length - 1] <= now - LIMIT_WINDOW) attempts.delete(k);
    if (recent.length >= limit) {
      attempts.set(key, recent);
      return false;
    }
    attempts.set(key, [...recent, now]);
    return true;
  }

  // App address for the link in the email. The Host header is trusted only locally: otherwise an attacker could
  // substitute their own domain, and the sign-in link from a genuine email would go to them.
  function publicUrl(req) {
    const explicit = String(process.env.PUBLIC_URL || '').trim().replace(/\/+$/, '');
    if (explicit) return explicit;
    const domain = String(process.env.RAILWAY_PUBLIC_DOMAIN || '').trim();
    if (domain) return `https://${domain}${base(req)}`;
    const host = String(req.headers.host || '');
    const hostname = host.replace(/:\d+$/, '').replace(/^\[|\]$/g, '').toLowerCase();
    if (['localhost', '127.0.0.1', '::1'].includes(hostname) || hostname.endsWith('.localhost')) {
      return `${isHttps(req) ? 'https' : 'http'}://${host}${base(req)}`;
    }
    return null;
  }

  function loginRequest(req, res) {
    readForm(req, (form) => {
      const email = String(form.get('email') || '').trim().toLowerCase().slice(0, 254);
      if (!EMAIL_RE.test(email)) return page(req, res, 400, 'bad_email');
      if (!(allow('ip:' + clientIp(req), LIMIT_PER_IP) && allow('mail:' + email, LIMIT_PER_EMAIL))) return page(req, res, 429, 'slow');
      if (emailAllowed(email)) {
        const appUrl = publicUrl(req);
        if (!appUrl) {
          console.log(ownerText("[magic-link] sign-in email not sent: the app's address is unknown. Set PUBLIC_URL",
            '[magic-link] письмо для входа не отправлено: неизвестен адрес приложения. Задайте PUBLIC_URL'));
        } else {
          const token = sign('login', { e: email, x: Math.floor(Date.now() / 1000) + LINK_TTL, n: crypto.randomBytes(9).toString('base64url') });
          // No await, so the response doesn't wait for mail and looks the same for unknown addresses.
          magicLink.sendLoginMail(email, `${appUrl}${VERIFY_PATH}?t=${encodeURIComponent(token)}`, new URL(appUrl).host,
            langOf(req));
        }
      } else {
        notifyRequest(req, email);
      }
      return page(req, res, 200, 'check');
    });
  }

  function verify(req, res, url) {
    const finish = (token) => {
      const data = unsign('login', token);
      const fresh = data && emailAllowed(data.e) && !usedLinks.has(data.n);
      if (!fresh) return page(req, res, 403, 'expired');
      // Mail scanners open emailed links on their own. Sign in only by button, or a scanner would use up the link.
      if (req.method !== 'POST') return page(req, res, 200, 'confirm', { email: data.e, token });
      const now = Date.now();
      for (const [nonce, expires] of usedLinks) if (expires < now) usedLinks.delete(nonce);
      usedLinks.set(data.n, data.x * 1000);
      console.log(ownerText(`[magic-link] signed in by email (${data.e}), ip=${clientIp(req)}`,
        `[magic-link] вход по почте (${data.e}), ip=${clientIp(req)}`));
      const session = 'e.' + sign('session', { e: data.e, x: Math.floor(now / 1000) + maxAge });
      res.writeHead(302, { Location: base(req) + '/', 'Set-Cookie': cookie(session, isHttps(req)), 'Cache-Control': 'no-store' });
      return res.end();
    };
    if (req.method === 'POST') return readForm(req, (form) => finish(form.get('t')));
    return finish(url.searchParams.get('t'));
  }

  return function magicLinkMiddleware(req, res, next) {
    const url = new URL(req.url, 'http://localhost');
    const path = url.pathname;
    if (req.method === 'POST' && path === REQUEST_PATH && !requestUrl) {
      return readForm(req, (form) => {
        notifyRequest(req, form.get('contact'));
        page(req, res, 200, 'sent');
      });
    }
    if (emails.size && path === LOGIN_PATH && req.method === 'POST') return loginRequest(req, res);
    if (emails.size && path === VERIFY_PATH) return verify(req, res, url);
    if (path === LOGOUT_PATH) {
      return page(req, res, 200, emails.size ? 'logout' : 'forbidden',
        { headers: { 'Set-Cookie': `${cookieName}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax` } });
    }

    const token = url.searchParams.get('token');
    if (token !== null) {
      const name = owner(token, tokens);
      if (name === null) return page(req, res, 403, 'invalid');
      console.log(ownerText('[magic-link] signed in by link', '[magic-link] вход по ссылке')
        + (name ? ` (${name})` : '') + ', ip=' + clientIp(req));
      // Strip the token from the URL so it doesn't stay in browser history or leak through Referer.
      // Collapse leading slashes: a Location of "//host" would be an open redirect.
      const full = new URL(req.originalUrl || req.url, 'http://localhost');
      full.searchParams.delete('token');
      res.writeHead(302, {
        Location: '/' + full.pathname.replace(/^\/+/, '') + full.search,
        'Set-Cookie': cookie(cookieValue(token), isHttps(req)),
        'Cache-Control': 'no-store',
      });
      return res.end();
    }

    if (cookieOwner(req) !== null) return next();
    return page(req, res, 403, 'forbidden');
  };
}

// "alice:tok1, tok2" -> [{name: "alice", token: "tok1"}, {name: "", token: "tok2"}]. A token must not contain ":".
function parseTokens(raw) {
  const entries = [];
  for (const item of Array.isArray(raw) ? raw : String(raw).split(',')) {
    const text = String(item).trim();
    const colon = text.lastIndexOf(':');
    const token = text.slice(colon + 1).trim();
    if (token) entries.push({ name: colon >= 0 ? text.slice(0, colon).trim() : '', token });
  }
  return entries;
}

// "Ivan@Company.com, @company.com, junk" -> Set {"ivan@company.com", "@company.com"}: addresses and domains,
// lowercased.
function parseEmails(raw) {
  const found = new Set();
  for (const item of Array.isArray(raw) ? raw : String(raw).split(',')) {
    const text = String(item).trim().toLowerCase();
    if (EMAIL_RE.test(text) || DOMAIN_RE.test(text)) found.add(text);
  }
  return found;
}

// How to send sign-in emails: "log", "resend" or "" (not set up).
function mailMode() {
  if (['1', 'true', 'yes'].includes(String(process.env.LOGIN_LINKS_TO_LOG || '').trim().toLowerCase())) return 'log';
  if (String(process.env.RESEND_API_KEY || '').trim()) return 'resend';
  return '';
}

async function sendLoginMail(email, link, appName, lang = DEFAULT_LANG) {
  if (mailMode() === 'log') {
    console.log(ownerText(`[magic-link] sign-in link (${email}): ${link}`, `[magic-link] ссылка для входа (${email}): ${link}`));
    return;
  }
  const ui = UI[LANGS.includes(lang) ? lang : DEFAULT_LANG];
  const key = String(process.env.RESEND_API_KEY || '').trim();
  try {
    if (!key) throw new Error(ownerText('email is not set up: set RESEND_API_KEY', 'почта не настроена: задайте RESEND_API_KEY'));
    const api = String(process.env.RESEND_API_URL || 'https://api.resend.com').replace(/\/+$/, '');
    const response = await fetch(`${api}/emails`, {
      method: 'POST',
      headers: { Authorization: `Bearer ${key}`, 'Content-Type': 'application/json', 'User-Agent': 'small-magic-link/1.0' },
      body: JSON.stringify({
        from: String(process.env.MAIL_FROM || '').trim() || 'small <onboarding@resend.dev>',
        to: [email],
        subject: ui.subject(appName),
        text: ui.mailText(appName, link),
        html: `<p>${ui.mailIntro(escapeHtml(appName))}</p><p><a href="${escapeHtml(link)}">${ui.signIn}</a></p>`
          + `<p style="color:#666">${ui.mailNote}</p>`,
      }),
    });
    if (!response.ok) throw new Error(`HTTP ${response.status} ${(await response.text()).slice(0, 200)}`);
    console.log(ownerText(`[magic-link] sign-in email sent: ${email}`, `[magic-link] письмо для входа отправлено: ${email}`));
  } catch (e) {
    // A failed email is no reason to crash the app; the key stays out of the log.
    console.log(ownerText(`[magic-link] could not send the sign-in email to ${email}: `,
      `[magic-link] не удалось отправить письмо для входа на ${email}: `) + e.message);
  }
}

// Text for the owner (log, Telegram) in their language, OWNER_LANG. Visitors see their browser's language.
function ownerText(en, ru) {
  return String(process.env.OWNER_LANG || '').trim().toLowerCase().startsWith('ru') ? ru : en;
}

// The cookie holds a value derived from the token, not the token itself: remove the token from ACCESS_TOKEN
// and its cookies stop working.
function cookieValue(token) {
  return crypto.createHmac('sha256', token).update('magic-link-cookie').digest('hex');
}

// Name of the secret's owner ("" if unnamed), or null if the secret doesn't match. pairs: [[name, secret], ...].
function owner(value, pairs) {
  if (value == null) return null;
  for (const [name, secret] of pairs) if (safeEqual(value, secret)) return name;
  return null;
}

function safeEqual(a, b) {
  const digest = (s) => crypto.createHash('sha256').update(String(s)).digest();
  return crypto.timingSafeEqual(digest(a), digest(b));
}

function readCookie(header, name) {
  for (const part of String(header || '').split(';')) {
    const eq = part.indexOf('=');
    if (eq > 0 && part.slice(0, eq).trim() === name) return part.slice(eq + 1).trim();
  }
  return null;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
}

function requestForm(action, placeholder, button) {
  return `<form method="post" action="${action}">`
    + `<input name="contact" required maxlength="200" placeholder="${placeholder}">`
    + `<button class="btn" type="submit">${button}</button></form>`;
}

function loginForm(action, button) {
  return `<form method="post" action="${action}">`
    + '<input name="email" type="email" required maxlength="254" autocomplete="email" placeholder="name@company.com">'
    + `<button class="btn" type="submit">${button}</button></form>`;
}

function confirmForm(action, token, button) {
  return `<form method="post" action="${action}"><input type="hidden" name="t" value="${token}">`
    + `<button class="btn" type="submit">${button}</button></form>`;
}

function renderPage(lang, icon, title, text, action) {
  return `<!doctype html>
<html lang="${lang}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>${title}</title>
<style>
  :root { color-scheme: light dark; --bg: #f5f5f4; --card: #fff; --text: #1c1917; --muted: #57534e;
          --border: #e7e5e4; --accent: #2563eb; }
  @media (prefers-color-scheme: dark) {
    :root { --bg: #0c0a09; --card: #1c1917; --text: #f5f5f4; --muted: #a8a29e; --border: #292524; --accent: #3b82f6; }
  }
  * { box-sizing: border-box; }
  body { margin: 0; min-height: 100vh; display: grid; place-items: center; padding: 16px;
         background: var(--bg); color: var(--text); font: 16px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
  main { width: 100%; max-width: 400px; padding: 32px; text-align: center;
         background: var(--card); border: 1px solid var(--border); border-radius: 12px; }
  .icon { font-size: 32px; line-height: 1; margin-bottom: 12px; }
  h1 { margin: 0 0 8px; font-size: 20px; }
  p { margin: 0 0 24px; color: var(--muted); overflow-wrap: anywhere; }
  input { width: 100%; margin-bottom: 12px; padding: 10px 12px; font: inherit; color: inherit;
          background: transparent; border: 1px solid var(--border); border-radius: 8px; }
  .btn { display: block; width: 100%; padding: 10px 16px; font: inherit; font-weight: 600; color: #fff;
         background: var(--accent); border: 0; border-radius: 8px; text-decoration: none; cursor: pointer; }
  .btn:hover { filter: brightness(1.1); }
  .link { color: var(--accent); }
</style>
</head>
<body>
<main>
  <div class="icon" aria-hidden="true">${icon}</div>
  <h1>${title}</h1>
  <p>${text}</p>
  ${action}
</main>
</body>
</html>
`;
}

module.exports = magicLink;
module.exports.magicLink = magicLink;
module.exports.parseTokens = parseTokens;
module.exports.parseEmails = parseEmails;
module.exports.pickLang = pickLang;
// The middleware calls magicLink.sendLoginMail, so tests can intercept sending the email.
module.exports.sendLoginMail = sendLoginMail;
