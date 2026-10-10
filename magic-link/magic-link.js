'use strict';
/**
 * Protect a Node.js app: sign in with a work account, a work email or an invitation link. One file, no dependencies,
 * Node 18+ (Node 22.13+ to keep sign-in state in SQLite).
 *
 *   const magicLink = require('./magic-link');
 *   app.use(magicLink());                                               // Express / Connect
 *
 *   const guard = magicLink();                                          // plain http
 *   http.createServer((req, res) => guard(req, res, () => handler(req, res)));
 *
 * Three ways to let someone in. The owner lists who may come in ACCESS_EMAILS, like sharing a Google Doc; an unknown
 * address becomes an access request.
 *   - Single sign-on with Google or Microsoft (OpenID Connect): "Sign in with Google" / "Sign in with Microsoft".
 *     The address comes from an ID token that the app gets straight from the provider; see identity() for which
 *     accounts count as work accounts.
 *   - Email sign-in: a person enters their email and gets a sign-in link (valid for 15 minutes, once).
 *   - Invitation link https://<app>/?token=<token> (ACCESS_TOKEN): sets a cookie and redirects without the token.
 * The browser remembers a sign-in for 30 days. Without access: a 403 page with the sign-in options (if ACCESS_EMAILS
 * is set) or a "Request access" button. Sign-ins, refusals and access requests are logged as single-line JSON
 * (Railway shows them as structured logs, `small access log` reads them). The app learns who is signed in from
 * req.magicLinkUser and how from req.magicLinkMethod ("email", "google", "microsoft" or "link").
 *
 * Behind the small gateway (small gateway on) the gateway does all of the above, and this file in the app only checks
 * that a request came through it: SMALL_GATEWAY_KEY set means "trust the gateway's signed X-Small-User,
 * X-Small-Method and X-Small-Auth headers, and nothing else".
 *
 * Environment variables:
 *   ACCESS_EMAILS       comma-separated addresses; "@company.com" means the whole domain. Remove an address and
 *                       sign-in stops working immediately.
 *   SESSION_SECRET      key that signs links and sessions (small generates it). If unset, a random one per start.
 *   GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET   "Sign in with Google" (small sso google)
 *   MICROSOFT_CLIENT_ID, MICROSOFT_CLIENT_SECRET, MICROSOFT_TENANT_ID
 *                       "Sign in with Microsoft" for the members of one Microsoft Entra tenant (small sso microsoft)
 *   RESEND_API_KEY, MAIL_FROM   email through Resend (HTTPS). The Node version has no SMTP: use Resend.
 *   LOGIN_LINKS_TO_LOG  1: don't send email, write the sign-in link to the log instead. For testing only.
 *   PUBLIC_URL          app address used in email links and sign-on redirects; otherwise RAILWAY_PUBLIC_DOMAIN;
 *                       locally, localhost.
 *   ACCESS_DB           SQLite file for sign-in state: used sign-in links, rate limits, "sign out on all devices".
 *                       Default: .magic-link.db on the Railway volume (RAILWAY_VOLUME_MOUNT_PATH, see small volume);
 *                       otherwise, or before Node 22.13, the state is kept in memory and a restart forgets it.
 *   SESSIONS_REVOKED    "ivan@company.com=1760000000, @company.com=..., *=...": sessions of that address, domain or
 *                       everyone issued before that Unix time no longer work (small access signout keeps it)
 *   ACCESS_TOKEN        comma-separated invitation-link tokens, optionally named: "alice:token1,bob:token2".
 *   ACCESS_REQUEST_URL  where the "Request access" button leads when there is no email sign-in
 *                       (mailto:..., https://t.me/...).
 *   TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID  access requests reach the owner in Telegram (small notify telegram).
 *   ACCESS_LANG         language of pages and emails: ru, en or auto (default: from the Accept-Language header).
 *   OWNER_LANG          language of messages for the owner (log lines and Telegram notifications): en (default)
 *                       or ru. small deploy sets it to match small's language.
 *   SMALL_GATEWAY_KEY   the app is behind the small gateway: let in only requests it signed with this key (small
 *                       gateway on sets it); the variables above then belong to the gateway
 */

const crypto = require('node:crypto');
const fs = require('node:fs');
const nodePath = require('node:path');

const COOKIE_NAME = 'small_access';
const COOKIE_MAX_AGE = 30 * 24 * 3600;
const LINK_TTL = 15 * 60;
const REQUEST_PATH = '/_access/request';
const LOGIN_PATH = '/_access/login';
const VERIFY_PATH = '/_access/verify';
const LOGOUT_PATH = '/_access/logout';
const SSO_PATH = '/_access/sso/'; // /_access/sso/google starts single sign-on with Google
const SSO_CALLBACK = '/_access/sso/callback'; // the redirect URI to register with Google and Microsoft
const SSO_TTL = 10 * 60; // how long a started single sign-on may take
const CLOCK_SKEW = 5 * 60; // leeway for the provider's clock when checking ID token times
const GATEWAY_TTL = 24 * 3600; // how long the gateway's signature on a request stays valid
const GATEWAY_HEADERS = ['x-small-user', 'x-small-method', 'x-small-auth']; // what the small gateway adds for the app
// How many sign-in emails can be requested within 15 minutes: per address and per IP.
const LIMIT_PER_EMAIL = 3;
const LIMIT_PER_IP = 10;
const LIMIT_WINDOW = 15 * 60;

const EMAIL_RE = /^[^@\s,;<>"']+@[^@\s,;<>"']+\.[^@\s,;<>"']+$/;
const DOMAIN_RE = /^@[^@\s,;<>"']+\.[^@\s,;<>"']+$/;
const GUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
// Our pages have no scripts, can't be framed and submit forms only to the app itself.
const PAGE_CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; form-action 'self'; "
  + "frame-ancestors 'none'; base-uri 'none'";

const GOOGLE_AUTH_URL = 'https://accounts.google.com/o/oauth2/v2/auth';
const GOOGLE_TOKEN_URL = 'https://oauth2.googleapis.com/token';
const GOOGLE_ISSUERS = ['https://accounts.google.com', 'accounts.google.com'];
const GMAIL_DOMAINS = ['gmail.com', 'googlemail.com'];
const MICROSOFT_LOGIN_URL = 'https://login.microsoftonline.com';

const LANGS = ['ru', 'en'];
const DEFAULT_LANG = 'ru'; // when the browser doesn't say which language it prefers

const MESSAGES = {
  ru: {
    forbidden: ['&#128274;', 'Доступ по приглашению', 'Эта страница открывается только по ссылке-приглашению от владельца.'],
    invalid: ['&#128274;', 'Ссылка недействительна', 'Ссылка устарела или скопирована не полностью. Запросите новую.'],
    sent: ['&#9989;', 'Запрос отправлен', 'Владелец увидит запрос и пришлёт вам ссылку-приглашение.'],
    login: ['&#128274;', 'Вход по рабочей почте',
      'Приложение открывается только для приглашённых. Введите рабочую почту — пришлём ссылку для входа.'],
    sso: ['&#128274;', 'Вход рабочим аккаунтом', 'Приложение открывается только для приглашённых.'],
    check: ['&#9993;&#65039;', 'Проверьте почту',
      'Если у адреса есть доступ, ссылка для входа уже в почте: она действует 15 минут. '
      + 'Если доступа нет, владелец получит ваш запрос.'],
    bad_email: ['&#9888;&#65039;', 'Проверьте адрес', 'Похоже, в адресе почты ошибка. Введите его ещё раз.'],
    slow: ['&#8987;', 'Слишком много попыток', 'Подождите 15 минут и попробуйте снова.'],
    expired: ['&#8987;', 'Ссылка для входа устарела', 'Ссылка действует 15 минут и только один раз. Введите почту — пришлём новую.'],
    confirm: ['&#128274;', 'Вход в приложение', 'Нажмите кнопку, чтобы войти как %s.'],
    logout: ['&#128075;', 'Вы вышли', 'Вы вышли из приложения в этом браузере.'],
    logout_all: ['&#128075;', 'Вы вышли на всех устройствах', 'Все входы с адресом %s завершены. Чтобы вернуться, войдите снова.'],
    denied: ['&#128683;', 'Нет доступа', 'У %s нет доступа к этому приложению. Владелец получил ваш запрос.'],
    unverified: ['&#9888;&#65039;', 'Нужен рабочий аккаунт',
      'Этот аккаунт не подтверждает, что адрес %s принадлежит вашей организации. Войдите рабочим аккаунтом.'],
    sso_failed: ['&#9888;&#65039;', 'Не удалось войти', 'Вход не завершился. Попробуйте ещё раз.'],
    gateway: ['&#128274;', 'Откройте приложение по его адресу', 'Это приложение открывается только через свой адрес, где проверяется вход.'],
  },
  en: {
    forbidden: ['&#128274;', 'Invitation only', 'This page opens only with an invitation link from its owner.'],
    invalid: ['&#128274;', 'Invalid link', 'The link has expired or was copied incompletely. Ask for a new one.'],
    sent: ['&#9989;', 'Request sent', 'The owner will see your request and send you an invitation link.'],
    login: ['&#128274;', 'Sign in with your work email',
      "This app is for invited people only. Enter your work email and we'll send you a sign-in link."],
    sso: ['&#128274;', 'Sign in with your work account', 'This app is for invited people only.'],
    check: ['&#9993;&#65039;', 'Check your email',
      'If this address has access, a sign-in link is already in your inbox. It works for 15 minutes. '
      + "If it doesn't, the owner will get your request."],
    bad_email: ['&#9888;&#65039;', 'Check the address', 'This email address looks wrong. Please enter it again.'],
    slow: ['&#8987;', 'Too many attempts', 'Please wait 15 minutes and try again.'],
    expired: ['&#8987;', 'Sign-in link expired', 'The link works for 15 minutes and only once. Enter your email to get a new one.'],
    confirm: ['&#128274;', 'Sign in', 'Click the button to sign in as %s.'],
    logout: ['&#128075;', 'Signed out', 'You are no longer signed in to the app in this browser.'],
    logout_all: ['&#128075;', 'Signed out everywhere', 'Every sign-in as %s has ended, on all devices. Sign in again to come back.'],
    denied: ['&#128683;', 'No access', '%s has no access to this app. The owner has received your request.'],
    unverified: ['&#9888;&#65039;', 'Use your work account',
      "This account doesn't confirm that %s belongs to your organization. Sign in with your work account."],
    sso_failed: ['&#9888;&#65039;', 'Sign-in failed', "Sign-in didn't complete. Please try again."],
    gateway: ['&#128274;', 'Open the app at its address', 'This app opens only through its own address, where sign-in is checked.'],
  },
};

const UI = {
  ru: {
    request: 'Запросить доступ', contact: 'Email или Telegram для ответа', getLink: 'Получить ссылку для входа',
    signIn: 'Войти', other: 'Ввести другой адрес', google: 'Войти через Google', microsoft: 'Войти через Microsoft',
    or: 'или по ссылке на почту', everywhere: 'Выйти на всех устройствах', otherAccount: 'Войти другим аккаунтом',
    subject: (app) => `Вход в ${app}`,
    mailText: (app, link) => `Ссылка для входа в ${app}:\n${link}\n\nОна действует 15 минут и только один раз. `
      + 'Если вы не запрашивали вход, просто удалите это письмо.\n',
    mailIntro: (app) => `Ссылка для входа в <b>${app}</b>:`,
    mailNote: 'Она действует 15 минут и только один раз. Если вы не запрашивали вход, просто удалите это письмо.',
  },
  en: {
    request: 'Request access', contact: 'Email or Telegram to reach you', getLink: 'Email me a sign-in link',
    signIn: 'Sign in', other: 'Use a different address', google: 'Sign in with Google', microsoft: 'Sign in with Microsoft',
    or: 'or get a link by email', everywhere: 'Sign out on all devices', otherAccount: 'Use a different account',
    subject: (app) => `Sign in to ${app}`,
    mailText: (app, link) => `Sign-in link for ${app}:\n${link}\n\nIt works for 15 minutes and only once. `
      + "If you didn't ask to sign in, just delete this email.\n",
    mailIntro: (app) => `Sign-in link for <b>${app}</b>:`,
    mailNote: "It works for 15 minutes and only once. If you didn't ask to sign in, just delete this email.",
  },
};

// What the owner reads in the log for each event: [English, Russian], see audit().
const AUDIT = {
  sign_in: { email: ['signed in by email', 'вход по почте'], link: ['signed in by link', 'вход по ссылке'],
    google: ['signed in with Google', 'вход через Google'], microsoft: ['signed in with Microsoft', 'вход через Microsoft'] },
  denied: { google: ['no access, Google sign-in refused', 'нет доступа, вход через Google отклонён'],
    microsoft: ['no access, Microsoft sign-in refused', 'нет доступа, вход через Microsoft отклонён'] },
  sign_out_all: { email: ['signed out on all devices', 'выход на всех устройствах'] },
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
  const gatewayKey = String(options.gatewayKey ?? process.env.SMALL_GATEWAY_KEY ?? '').trim();
  let secret = String(options.secret ?? process.env.SESSION_SECRET ?? '').trim();
  if (emails.size && !secret && !gatewayKey) {
    console.log(ownerText('[magic-link] SESSION_SECRET is not set: everyone will have to sign in again after a restart',
      '[magic-link] SESSION_SECRET не задан: после перезапуска приложения всем придётся войти заново'));
  }
  secret = secret || crypto.randomBytes(32).toString('hex');
  const requestUrl = options.requestUrl ?? process.env.ACCESS_REQUEST_URL ?? '';
  const langSetting = String(options.lang ?? process.env.ACCESS_LANG ?? 'auto').trim().toLowerCase();
  const langOf = (req) => pickLang(req.headers['accept-language'], langSetting);
  const cookieName = options.cookieName ?? COOKIE_NAME;
  const stateCookieName = cookieName + '_sso';
  const maxAge = options.maxAge ?? COOKIE_MAX_AGE;
  const telegramToken = String(process.env.TELEGRAM_BOT_TOKEN || '').trim();
  const telegramChat = String(process.env.TELEGRAM_CHAT_ID || '').trim();
  const sso = emails.size ? ssoProviders() : {};
  const ssoIds = Object.keys(sso);
  const revoked = parseRevoked(process.env.SESSIONS_REVOKED || '');
  const store = openStore();
  if (gatewayKey) {
    console.log(ownerText('[magic-link] behind the small gateway: only requests it signed get in',
      '[magic-link] за шлюзом small: пускаются только запросы, подписанные шлюзом'));
  } else if (!entries.length && !emails.size) {
    console.log(ownerText('[magic-link] neither ACCESS_TOKEN nor ACCESS_EMAILS is set: nobody can get in',
      '[magic-link] ACCESS_TOKEN и ACCESS_EMAILS не заданы: доступ закрыт для всех'));
  }
  if (String(process.env.MICROSOFT_CLIENT_ID || '').trim() && !ssoProviders().microsoft && !gatewayKey) {
    console.log(ownerText('[magic-link] sign-in with Microsoft is off: it needs MICROSOFT_CLIENT_SECRET and '
      + 'MICROSOFT_TENANT_ID (a GUID). Set it up: small sso microsoft',
    '[magic-link] вход через Microsoft выключен: нужны MICROSOFT_CLIENT_SECRET и MICROSOFT_TENANT_ID (GUID). '
      + 'Настроить: small sso microsoft'));
  }
  if (emails.size && !mailMode() && !ssoIds.length && !gatewayKey) {
    console.log(ownerText('[magic-link] email is not set up (RESEND_API_KEY): sign-in emails will not be sent. Set it up: small mail',
      '[magic-link] почта не настроена (RESEND_API_KEY): письма для входа не уйдут. Подключить: small mail'));
  }
  if (emails.size && store.persistent) {
    console.log(ownerText('[magic-link] sign-in state is kept in ', '[magic-link] состояние входа хранится в ') + store.path);
  }

  const base = (req) => req.baseUrl || '';
  const clientIp = (req) => String(req.headers['x-forwarded-for'] || req.socket.remoteAddress || '').split(',')[0].trim();
  const userAgent = (req) => String(req.headers['user-agent'] || '');
  const isHttps = (req) => String(req.headers['x-forwarded-proto'] || '').split(',')[0].trim() === 'https'
    || Boolean(req.socket.encrypted);
  const cookie = (value, https, name = cookieName, age = maxAge) => `${name}=${value}; Path=/; Max-Age=${age}; HttpOnly; SameSite=Lax${https ? '; Secure' : ''}`;
  // Sign-in by email is offered unless single sign-on is set up and email is not.
  const emailForm = () => emails.size > 0 && (Boolean(mailMode()) || !ssoIds.length);

  function signInOptions(req, ui) {
    const buttons = ssoIds.map((id) => `<a class="btn sso" href="${escapeHtml(base(req) + SSO_PATH + id)}">${LOGOS[id]}<span>${ui[id]}</span></a>`).join('');
    if (!emailForm()) return buttons;
    return buttons + (buttons ? `<div class="or">${ui.or}</div>` : '') + loginForm(escapeHtml(base(req) + LOGIN_PATH), ui.getLink);
  }

  function page(req, res, status, kind, { email = '', token = '', headers = {} } = {}) {
    if (kind === 'forbidden' && emails.size) kind = ssoIds.length ? 'sso' : 'login';
    const lang = langOf(req);
    const ui = UI[lang];
    const [icon, title, rawText] = MESSAGES[lang][kind];
    const text = rawText.replace('%s', email);
    let action = '';
    if (kind === 'check') action = `<a class="link" href="${escapeHtml(base(req))}/">${ui.other}</a>`;
    else if (kind === 'confirm') action = confirmForm(escapeHtml(base(req) + VERIFY_PATH), escapeHtml(token), ui.signIn);
    else if (kind === 'sent') action = '';
    else if (kind === 'denied' || kind === 'unverified') action = `<a class="link" href="${escapeHtml(base(req))}/">${ui.otherAccount}</a>`;
    else if (emails.size) {
      action = signInOptions(req, ui);
      if (kind === 'logout' && token) action = everywhereForm(escapeHtml(base(req) + LOGOUT_PATH), escapeHtml(token), ui.everywhere) + action;
    } else if (requestUrl) action = `<a class="btn" href="${escapeHtml(requestUrl)}">${ui.request}</a>`;
    else action = requestForm(escapeHtml(base(req) + REQUEST_PATH), ui.contact, ui.request);
    const body = renderPage(lang, icon, escapeHtml(title), escapeHtml(text), action);
    res.writeHead(status, {
      'Content-Type': 'text/html; charset=utf-8',
      'Content-Length': Buffer.byteLength(body),
      'Cache-Control': 'no-store',
      'Referrer-Policy': 'no-referrer',
      'Content-Language': lang,
      'X-Frame-Options': 'DENY',
      'Content-Security-Policy': PAGE_CSP,
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
    contact = String(contact || '').slice(0, 200);
    const ip = clientIp(req);
    const ua = userAgent(req).slice(0, 200);
    // JSON keeps the entry on one log line, whatever the contact holds.
    logEvent(`[access-request] contact=${JSON.stringify(contact)} ip=${ip} ua=${JSON.stringify(ua)}`, 'warn',
      { magic_link: 'request', user: contact, ip, ua });
    if (!(telegramToken && telegramChat)) return;
    // No await: the user doesn't wait for Telegram. No parse_mode, so form text can't add markup.
    const api = String(process.env.TELEGRAM_API_URL || 'https://api.telegram.org').replace(/\/+$/, '');
    const hint = 'small access add ' + (EMAIL_RE.test(contact) ? contact : ownerText('NAME', 'ИМЯ'));
    const host = req.headers.host || ownerText('the app', 'приложению');
    const text = ownerText(
      `🔑 Access request to ${host}\nContact: ${contact || '—'}\nIP: ${ip || '—'}\n\nGrant access: ${hint}`,
      `🔑 Запрос доступа к ${host}\nКонтакт: ${contact || '—'}\nIP: ${ip || '—'}\n\nВыдать доступ: ${hint}`);
    fetch(`${api}/bot${telegramToken}/sendMessage`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ chat_id: telegramChat, text }),
    }).catch((e) => console.log(ownerText('[magic-link] could not send the access request to Telegram: ',
      '[magic-link] не удалось отправить запрос доступа в Telegram: ') + e.name));
  }

  // One JSON line per sign-in, refusal or sign-out: Railway turns the fields into log attributes, and
  // `small access log` collects the lines with @magic_link:* across deploys.
  function audit(event, method, who, req) {
    const ip = clientIp(req);
    const [en, ru] = AUDIT[event][method];
    logEvent(`[magic-link] ${ownerText(en, ru)}${who ? ': ' + who : ''}, ip ${ip || '—'}`, event === 'denied' ? 'warn' : 'info',
      { magic_link: event, method, user: who, ip, ua: userAgent(req).slice(0, 200) });
  }

  function emailAllowed(email) {
    email = String(email).toLowerCase();
    return emails.has(email) || emails.has('@' + email.slice(email.lastIndexOf('@') + 1));
  }

  // purpose keeps sign-in links, sessions and the rest apart: one can't be passed off as another.
  const mac = (purpose, payload) => crypto.createHmac('sha256', secret).update(`${purpose}|${payload}`).digest('base64url');
  const derive = mac; // the sign-on nonce and PKCE verifier, derived from the state ("sso-nonce", "sso-pkce")
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

  // Sessions of this address issued at or before this Unix time are signed out (SESSIONS_REVOKED, the
  // "Sign out on all devices" button). Issue times have milliseconds: signing in right after signing out works.
  function signedOutAt(email) {
    email = email.toLowerCase();
    return Math.max(revoked['=' + email] || 0, revoked['=@' + email.slice(email.lastIndexOf('@') + 1)] || 0,
      revoked['=*'] || 0, store.signedOut(email));
  }

  // The address of a valid session "e.<signed>" (not expired, not signed out), whether or not it still has access.
  function sessionEmail(value) {
    const session = sessionOf(value);
    return session ? session[0] : null;
  }

  // [address, sign-in method] of a valid session, whether or not it still has access; or null.
  function sessionOf(value) {
    const data = value && value.startsWith('e.') ? unsign('session', value.slice(2)) : null;
    if (!data || typeof data.e !== 'string' || !data.e) return null;
    // Sessions made before 0.8 don't say when they were issued: their expiry minus the lifetime does.
    const issued = typeof data.i === 'number' ? data.i : data.x - maxAge;
    if (issued <= signedOutAt(data.e)) return null;
    return [data.e, ['email', 'google', 'microsoft'].includes(data.m) ? data.m : 'email'];
  }

  function newSession(email, method) {
    const now = Date.now() / 1000;
    // Milliseconds, rounded down: a sign-out a moment later is always later than the issue time.
    return 'e.' + sign('session', { e: email, i: Math.floor(now * 1000) / 1000, x: Math.floor(now) + maxAge, m: method });
  }

  // Who is signed in and how: [address or ACCESS_TOKEN name, "email" / "google" / "microsoft" / "link"], or null.
  function whoIs(req) {
    const value = readCookie(req.headers.cookie, cookieName);
    if (value && value.startsWith('e.')) {
      const session = sessionOf(value);
      return session && emailAllowed(session[0]) ? session : null;
    }
    const name = owner(value, cookies);
    return name === null ? null : [name, 'link'];
  }

  // App address for the link in the email and for sign-on redirects. The Host header is trusted only locally:
  // otherwise an attacker could substitute their own domain, and the sign-in link from a genuine email would go to them.
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
      if (!(store.allow('ip:' + clientIp(req), LIMIT_PER_IP, LIMIT_WINDOW)
        && store.allow('mail:' + email, LIMIT_PER_EMAIL, LIMIT_WINDOW))) return page(req, res, 429, 'slow');
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
      const fresh = data && emailAllowed(data.e) && typeof data.n === 'string' && !store.isUsed('link:' + data.n);
      if (!fresh) return page(req, res, 403, 'expired');
      // Mail scanners open emailed links on their own. Sign in only by button, or a scanner would use up the link.
      if (req.method !== 'POST') return page(req, res, 200, 'confirm', { email: data.e, token });
      if (!store.useOnce('link:' + data.n, data.x)) return page(req, res, 403, 'expired');
      audit('sign_in', 'email', data.e, req);
      res.writeHead(302, { Location: base(req) + '/', 'Set-Cookie': cookie(newSession(data.e, 'email'), isHttps(req)), 'Cache-Control': 'no-store' });
      return res.end();
    };
    if (req.method === 'POST') return readForm(req, (form) => finish(form.get('t')));
    return finish(url.searchParams.get('t'));
  }

  // --- single sign-on: Google and Microsoft (OpenID Connect, authorization code flow with PKCE) ---

  // The state that comes back in the redirect is "<provider>.<random>"; the random part must match the signed state
  // cookie, which ties the answer to the browser that started the sign-in (no login CSRF). The nonce and the PKCE
  // verifier are derived from the state with SESSION_SECRET, so the cookie doesn't carry them.
  function ssoLinks(redirectUri, ids = ssoIds) {
    const nonce = crypto.randomBytes(24).toString('base64url');
    const links = {};
    for (const id of ids) {
      const provider = sso[id];
      const state = `${id}.${nonce}`;
      const params = new URLSearchParams({
        client_id: provider.clientId, redirect_uri: redirectUri, response_type: 'code', scope: provider.scope, state,
        nonce: derive('sso-nonce', state),
        code_challenge: crypto.createHash('sha256').update(derive('sso-pkce', state)).digest('base64url'),
        code_challenge_method: 'S256', prompt: 'select_account',
      });
      if (id === 'microsoft') params.set('response_mode', 'query');
      links[id] = `${provider.authUrl}?${params}`;
    }
    return { links, cookie: sign('sso', { s: nonce, r: redirectUri, x: Math.floor(Date.now() / 1000) + SSO_TTL }) };
  }

  function ssoFailed(reason) {
    console.log(ownerText('[magic-link] single sign-on failed: ', '[magic-link] вход через провайдера не удался: ') + reason);
    return { kind: 'sso_failed', session: null, email: '' };
  }

  // Finish single sign-on: {kind: "session", session, email} on success; otherwise session is null and kind is the
  // page to show: "denied" (no access; the owner gets a request), "unverified" or "sso_failed" (the reason is logged).
  async function ssoFinish(params, stateCookie, req) {
    const state = String(params.state || '');
    const dot = state.indexOf('.');
    const id = dot > 0 ? state.slice(0, dot) : '';
    const nonce = dot > 0 ? state.slice(dot + 1) : '';
    const data = unsign('sso', stateCookie || '');
    if (!(data && Object.hasOwn(sso, id) && nonce && safeEqual(String(data.s || ''), nonce))) {
      return ssoFailed('the sign-in was started in another browser or has expired');
    }
    if (params.error) return ssoFailed('the provider answered: ' + String(params.error).slice(0, 100));
    if (!params.code) return ssoFailed('the provider sent no code');
    let email;
    try {
      const claims = await exchangeCode(sso[id], String(params.code), String(data.r || ''), derive('sso-pkce', state));
      email = identity(sso[id], claims, derive('sso-nonce', state));
    } catch (e) {
      if (!(e instanceof SsoError)) throw e;
      if (!e.unverified) return ssoFailed(e.message);
      console.log(ownerText(`[magic-link] ${id} sign-in refused: `, `[magic-link] вход через ${id} отклонён: `) + e.message);
      return { kind: 'unverified', session: null, email: e.email };
    }
    if (!emailAllowed(email)) {
      audit('denied', id, email, req);
      if (store.allow('ip:' + clientIp(req), LIMIT_PER_IP, LIMIT_WINDOW)) notifyRequest(req, email);
      return { kind: 'denied', session: null, email };
    }
    if (!store.useOnce('sso:' + nonce, data.x)) return ssoFailed('this sign-in has already been used'); // a replay
    audit('sign_in', id, email, req);
    return { kind: 'session', session: newSession(email, id), email };
  }

  function ssoRedirect(req, res, id) {
    if (!Object.hasOwn(sso, id)) return page(req, res, 400, 'sso_failed');
    const appUrl = publicUrl(req);
    if (!appUrl) {
      console.log(ownerText("[magic-link] single sign-on can't start: the app's address is unknown. Set PUBLIC_URL",
        '[magic-link] вход через провайдера не начат: неизвестен адрес приложения. Задайте PUBLIC_URL'));
      return page(req, res, 500, 'sso_failed');
    }
    const { links, cookie: state } = ssoLinks(appUrl + SSO_CALLBACK, [id]);
    res.writeHead(302, { Location: links[id], 'Set-Cookie': cookie(state, isHttps(req), stateCookieName, SSO_TTL),
      'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer' });
    return res.end();
  }

  function ssoCallback(req, res, url) {
    const clear = cookie('', isHttps(req), stateCookieName, 0);
    ssoFinish(Object.fromEntries(url.searchParams), readCookie(req.headers.cookie, stateCookieName), req)
      .catch((e) => ssoFailed(e.message))
      .then(({ kind, session, email }) => {
        if (!session) {
          return page(req, res, kind === 'denied' || kind === 'unverified' ? 403 : 400, kind, { email, headers: { 'Set-Cookie': clear } });
        }
        res.writeHead(302, { Location: base(req) + '/', 'Set-Cookie': [clear, cookie(session, isHttps(req))], 'Cache-Control': 'no-store' });
        return res.end();
      });
  }

  // --- signing out ---

  // For a valid session: a short-lived token for the "Sign out on all devices" button. Only when the state is
  // persistent: kept in memory, a sign-out would be forgotten at the next restart.
  function everywhereToken(value) {
    const email = sessionEmail(value);
    return email && store.persistent ? sign('everywhere', { e: email, x: Math.floor(Date.now() / 1000) + SSO_TTL }) : null;
  }

  function logout(req, res) {
    const clear = { 'Set-Cookie': `${cookieName}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax` };
    if (!emails.size) return page(req, res, 200, 'forbidden', { headers: clear });
    const show = () => page(req, res, 200, 'logout',
      { token: everywhereToken(readCookie(req.headers.cookie, cookieName)) || '', headers: clear });
    if (req.method !== 'POST') return show();
    return readForm(req, (form) => {
      const data = unsign('everywhere', form.get('t'));
      if (!data || typeof data.e !== 'string' || !data.e) return show();
      store.signOut(data.e, Date.now() / 1000);
      audit('sign_out_all', 'email', data.e, req);
      return page(req, res, 200, 'logout_all', { email: data.e, headers: clear });
    });
  }

  // The request goes on to the app: who is signed in, and without the gateway's headers unless they were checked.
  function letIn(req, next, [user, method]) {
    if (!gatewayKey) for (const name of GATEWAY_HEADERS) delete req.headers[name];
    req.magicLinkUser = user;
    req.magicLinkMethod = method;
    return next();
  }

  return function magicLinkMiddleware(req, res, next) {
    if (gatewayKey) { // behind the gateway it has checked everything: only its signature counts here
      const who = gatewayIdentity(gatewayKey, ...GATEWAY_HEADERS.map((name) => String(req.headers[name] || '')));
      return who ? letIn(req, next, who) : page(req, res, 403, 'gateway');
    }
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
    if (ssoIds.length && path === SSO_CALLBACK) return ssoCallback(req, res, url);
    if (ssoIds.length && path.startsWith(SSO_PATH) && ['GET', 'HEAD'].includes(req.method)) {
      return ssoRedirect(req, res, path.slice(SSO_PATH.length));
    }
    if (path === LOGOUT_PATH) return logout(req, res);

    const token = url.searchParams.get('token');
    if (token !== null) {
      const name = owner(token, tokens);
      if (name === null) return page(req, res, 403, 'invalid');
      audit('sign_in', 'link', name, req);
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

    const who = whoIs(req);
    if (who !== null) return letIn(req, next, who);
    return page(req, res, 403, 'forbidden');
  };
}

// --- the small gateway: it signs who is signed in, the app behind it checks the signature ---

// X-Small-Auth for a request the gateway lets through: "<unix time>.<HMAC-SHA256 of user, method and time>", the same
// as magic_link.py. The app behind the gateway checks it with the same SMALL_GATEWAY_KEY, so a request that didn't
// come through the gateway (say, the app got a public address by mistake) can't claim to be someone.
function gatewayAuth(key, user, method, now = Date.now() / 1000) {
  const moment = Math.floor(now);
  return `${moment}.${crypto.createHmac('sha256', key).update(`small-gateway|${user}|${method}|${moment}`).digest('base64url')}`;
}

// [user, method] if X-Small-Auth is the gateway's signature of them, made within GATEWAY_TTL; otherwise null.
function gatewayIdentity(key, user, method, auth, now = Date.now() / 1000) {
  const moment = String(auth || '').split('.')[0];
  if (!key || !/^\d+$/.test(moment)) return null;
  if (!(now - GATEWAY_TTL <= Number(moment) && Number(moment) <= now + CLOCK_SKEW)) return null;
  return safeEqual(gatewayAuth(key, user, method, Number(moment)), String(auth)) ? [user, method] : null;
}

// --- single sign-on (OpenID Connect) ---

// Single sign-on didn't produce an address we trust. unverified: the account is real, but the provider doesn't vouch
// that the address belongs to an organization (a personal Google account with a company address, a guest).
class SsoError extends Error {
  constructor(reason, email = '', unverified = false) {
    super(reason);
    this.email = email;
    this.unverified = unverified;
  }
}

// Single sign-on providers set up in the environment, in button order. A provider needs its client ID and secret;
// Microsoft also needs the tenant ID (a GUID): only that organization's accounts can sign in.
// *_AUTH_URL, *_TOKEN_URL and MICROSOFT_LOGIN_URL exist for tests.
function ssoProviders(env = process.env) {
  const get = (name) => String(env[name] || '').trim();
  const found = {};
  if (get('GOOGLE_CLIENT_ID') && get('GOOGLE_CLIENT_SECRET')) {
    found.google = { id: 'google', clientId: get('GOOGLE_CLIENT_ID'), clientSecret: get('GOOGLE_CLIENT_SECRET'),
      scope: 'openid email', authUrl: get('GOOGLE_AUTH_URL') || GOOGLE_AUTH_URL,
      tokenUrl: get('GOOGLE_TOKEN_URL') || GOOGLE_TOKEN_URL };
  }
  const tenant = get('MICROSOFT_TENANT_ID').toLowerCase();
  if (get('MICROSOFT_CLIENT_ID') && get('MICROSOFT_CLIENT_SECRET') && GUID_RE.test(tenant)) {
    const login = (get('MICROSOFT_LOGIN_URL') || MICROSOFT_LOGIN_URL).replace(/\/+$/, '');
    found.microsoft = { id: 'microsoft', clientId: get('MICROSOFT_CLIENT_ID'), clientSecret: get('MICROSOFT_CLIENT_SECRET'),
      scope: 'openid profile email', tenant, authUrl: `${login}/${tenant}/oauth2/v2.0/authorize`,
      tokenUrl: `${login}/${tenant}/oauth2/v2.0/token` };
  }
  return found;
}

/**
 * The address from ID token claims, checked; otherwise SsoError.
 *
 * The token came straight from the provider's token endpoint over HTTPS with our client secret, so, as OpenID Connect
 * Core 3.1.3.7 allows, TLS stands in for checking its signature; everything else is checked: audience, expiry, nonce,
 * issuer. On top of that, the address must be one the provider vouches for:
 *   - Google: the address is verified, and it is a Gmail address or belongs to a Google Workspace organization (the hd
 *     claim). A personal Google account registered with a company address is refused: whoever made it may long have
 *     lost that mailbox.
 *   - Microsoft: a member of the configured tenant (issuer and tid match, not a guest from elsewhere). The address is
 *     the sign-in name (preferred_username, the UPN), whose domain the tenant has verified. Tokens of any other tenant
 *     are refused, so another organization can't claim your addresses.
 */
function identity(provider, claims, nonce, now = Date.now() / 1000) {
  const audience = claims.aud;
  if (audience !== provider.clientId && !(Array.isArray(audience) && audience.includes(provider.clientId))) {
    throw new SsoError('the ID token is for another app (aud)');
  }
  if (Array.isArray(audience) && audience.length > 1 && claims.azp !== provider.clientId) {
    throw new SsoError('the ID token is for another app (azp)');
  }
  if (typeof claims.exp !== 'number' || claims.exp < now - CLOCK_SKEW) throw new SsoError('the ID token has expired');
  if (typeof claims.iat === 'number' && claims.iat > now + CLOCK_SKEW) throw new SsoError('the ID token is issued in the future');
  if (!safeEqual(String(claims.nonce ?? ''), nonce)) throw new SsoError('the ID token belongs to another sign-in (nonce)');

  if (provider.id === 'google') {
    if (!GOOGLE_ISSUERS.includes(claims.iss)) throw new SsoError('the ID token is not from Google (iss)');
    const email = typeof claims.email === 'string' ? claims.email.trim().toLowerCase() : '';
    if (!EMAIL_RE.test(email)) throw new SsoError('Google sent no email address');
    if (claims.email_verified !== true && claims.email_verified !== 'true') {
      throw new SsoError(`Google has not verified ${email}`, email, true);
    }
    if (!claims.hd && !GMAIL_DOMAINS.includes(email.slice(email.lastIndexOf('@') + 1))) {
      throw new SsoError(`${email} is a personal Google account, not a Google Workspace one`, email, true);
    }
    return email;
  }

  const issuer = `https://login.microsoftonline.com/${provider.tenant}/v2.0`;
  if (claims.iss !== issuer || String(claims.tid ?? '').toLowerCase() !== provider.tenant) {
    throw new SsoError('the ID token is from another Microsoft tenant');
  }
  const email = typeof claims.preferred_username === 'string' ? claims.preferred_username.trim().toLowerCase() : '';
  if (claims.idp !== undefined && claims.idp !== null && !String(claims.idp).toLowerCase().includes(provider.tenant)) {
    throw new SsoError(`${email || 'the account'} is a guest in the tenant, not a member`, email, true);
  }
  if (email.includes('#ext#') || !EMAIL_RE.test(email)) throw new SsoError('Microsoft sent no work address', email, true);
  return email;
}

// Claims of the ID token for an authorization code (with the client secret and the PKCE verifier).
async function exchangeCode(provider, code, redirectUri, verifier) {
  let response;
  let payload;
  try {
    response = await fetch(provider.tokenUrl, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded', Accept: 'application/json', 'User-Agent': 'small-magic-link/1.0' },
      body: new URLSearchParams({ grant_type: 'authorization_code', code, redirect_uri: redirectUri,
        client_id: provider.clientId, client_secret: provider.clientSecret, code_verifier: verifier }).toString(),
      signal: AbortSignal.timeout(15000),
    });
    payload = await response.json().catch(() => ({}));
  } catch (e) {
    throw new SsoError('no answer from the provider: ' + e.name);
  }
  if (!response.ok) {
    throw new SsoError(`the provider refused the code: HTTP ${response.status} ${String((payload && payload.error) || '').slice(0, 100)}`);
  }
  const parts = String((payload && payload.id_token) || '').split('.');
  let claims = null;
  try {
    claims = parts.length === 3 ? JSON.parse(Buffer.from(parts[1], 'base64url').toString('utf8')) : null;
  } catch {
    claims = null;
  }
  if (!claims || typeof claims !== 'object' || Array.isArray(claims)) throw new SsoError('the provider sent no ID token');
  return claims;
}

// --- sign-in state: used links, rate limits, "sign out on all devices" (times in seconds, as in magic_link.py) ---

// State in this process's memory: a restart forgets it.
function memoryStore() {
  let used = new Map();
  const hits = new Map();
  const signouts = new Map();
  return {
    persistent: false,
    isUsed: (key) => used.has(key),
    useOnce(key, expires) { // true the first time for a key (until expires), false afterwards
      const now = Date.now() / 1000;
      used = new Map([...used].filter(([, x]) => x > now));
      if (used.has(key)) return false;
      used.set(key, expires);
      return true;
    },
    allow(key, limit, window) { // count an attempt; false when there were already `limit` of them within `window`
      const now = Date.now() / 1000;
      if (hits.size > 10000) for (const [k, v] of hits) if (v[v.length - 1] <= now - window) hits.delete(k);
      const recent = (hits.get(key) || []).filter((t) => t > now - window);
      if (recent.length >= limit) {
        hits.set(key, recent);
        return false;
      }
      hits.set(key, [...recent, now]);
      return true;
    },
    signedOut: (email) => signouts.get(email) || 0,
    signOut(email, moment) { signouts.set(email, Math.max(signouts.get(email) || 0, moment)); },
  };
}

// State in a SQLite file (on a Railway volume): survives restarts. The same tables as magic_link.py.
function sqliteStore(path) {
  const { DatabaseSync } = require('node:sqlite'); // here: before Node 22.13 there is none, and memory is used
  fs.mkdirSync(nodePath.dirname(nodePath.resolve(path)), { recursive: true });
  const db = new DatabaseSync(path);
  db.exec(`PRAGMA busy_timeout = 10000; PRAGMA journal_mode = WAL;
    CREATE TABLE IF NOT EXISTS used (key TEXT PRIMARY KEY, expires INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS hits (key TEXT NOT NULL, at REAL NOT NULL);
    CREATE INDEX IF NOT EXISTS hits_key ON hits (key, at);
    CREATE TABLE IF NOT EXISTS signouts (email TEXT PRIMARY KEY, at REAL NOT NULL);`);
  const q = (sql) => db.prepare(sql);
  const statements = {
    isUsed: q('SELECT 1 AS found FROM used WHERE key = ?'),
    purge: q('DELETE FROM used WHERE expires < ?'),
    use: q('INSERT OR IGNORE INTO used (key, expires) VALUES (?, ?)'),
    forget: q('DELETE FROM hits WHERE at <= ?'),
    count: q('SELECT COUNT(*) AS n FROM hits WHERE key = ?'),
    hit: q('INSERT INTO hits (key, at) VALUES (?, ?)'),
    signedOut: q('SELECT at FROM signouts WHERE email = ?'),
    signOut: q('INSERT OR REPLACE INTO signouts (email, at) VALUES (?, MAX(?, COALESCE((SELECT at FROM signouts WHERE email = ?), 0)))'),
  };
  return {
    persistent: true,
    path,
    db,
    isUsed: (key) => statements.isUsed.get(key) !== undefined,
    useOnce(key, expires) {
      statements.purge.run(Math.floor(Date.now() / 1000));
      return statements.use.run(key, Math.floor(expires)).changes === 1; // the primary key makes this atomic
    },
    allow(key, limit, window) {
      const now = Date.now() / 1000;
      db.exec('BEGIN IMMEDIATE');
      try {
        statements.forget.run(now - window);
        const count = statements.count.get(key).n;
        if (count < limit) statements.hit.run(key, now);
        db.exec('COMMIT');
        return count < limit;
      } catch (e) {
        db.exec('ROLLBACK');
        throw e;
      }
    },
    signedOut: (email) => (statements.signedOut.get(email) || { at: 0 }).at,
    signOut(email, moment) { statements.signOut.run(email, moment, email); },
  };
}

// Where sign-in state lives: ACCESS_DB, else .magic-link.db on the Railway volume, else memory.
function openStore() {
  const mount = String(process.env.RAILWAY_VOLUME_MOUNT_PATH || '').trim();
  let path = String(process.env.ACCESS_DB || '').trim();
  if (!path && mount) path = nodePath.join(mount, '.magic-link.db');
  if (!path || path === 'memory') return memoryStore();
  try {
    return sqliteStore(path);
  } catch (e) { // no node:sqlite or a broken volume should not take the app down: fall back to memory, and say so
    console.log(ownerText(`[magic-link] cannot open ${path} (${e.code || e.name}: ${String(e.message).slice(0, 200)}): sign-in state is kept in memory`,
      `[magic-link] не открывается ${path} (${e.code || e.name}: ${String(e.message).slice(0, 200)}): состояние входа хранится в памяти`));
    return memoryStore();
  }
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

// "ivan@company.com=1760000000, *=1760000100" -> {"=ivan@company.com": 1760000000, "=*": 1760000100}: address, @domain
// or * (everyone) -> Unix time before which their sessions are signed out. Keys start with "=", so no address can be
// mistaken for an Object property. Junk is skipped.
function parseRevoked(raw) {
  const found = {};
  for (const item of String(raw || '').split(',')) {
    const text = item.trim().toLowerCase();
    const eq = text.lastIndexOf('=');
    const who = text.slice(0, eq).trim();
    const moment = text.slice(eq + 1).trim();
    if (eq > 0 && (who === '*' || EMAIL_RE.test(who) || DOMAIN_RE.test(who)) && /^\d+$/.test(moment)) {
      found['=' + who] = Math.max(found['=' + who] || 0, Number(moment));
    }
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

// A log line as one JSON object: message and level for people, the rest as attributes for filters.
function logEvent(message, level, fields) {
  console.log(JSON.stringify({ message, level, ...fields }));
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

function everywhereForm(action, token, button) {
  return `<form method="post" action="${action}"><input type="hidden" name="t" value="${token}">`
    + `<button class="btn quiet" type="submit">${button}</button></form>`;
}

const LOGOS = {
  google: '<svg viewBox="0 0 48 48" aria-hidden="true">'
    + '<path fill="#EA4335" d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 '
    + '5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z"/>'
    + '<path fill="#4285F4" d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 '
    + '7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z"/>'
    + '<path fill="#FBBC05" d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 '
    + '0 20.12 0 24c0 3.88.92 7.54 2.56 10.78l7.97-6.19z"/>'
    + '<path fill="#34A853" d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 '
    + '0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z"/></svg>',
  microsoft: '<svg viewBox="0 0 21 21" aria-hidden="true"><path fill="#f25022" d="M1 1h9v9H1z"/>'
    + '<path fill="#7fba00" d="M11 1h9v9h-9z"/><path fill="#00a4ef" d="M1 11h9v9H1z"/>'
    + '<path fill="#ffb900" d="M11 11h9v9h-9z"/></svg>',
};

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
  .btn.sso, .btn.quiet { display: flex; align-items: center; justify-content: center; gap: 10px; margin-bottom: 12px;
                         color: var(--text); background: transparent; border: 1px solid var(--border); }
  .btn.sso svg { width: 18px; height: 18px; flex: none; }
  .or { display: flex; align-items: center; gap: 12px; margin: 8px 0 16px; color: var(--muted); font-size: 14px; }
  .or::before, .or::after { content: ""; flex: 1; border-top: 1px solid var(--border); }
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
module.exports.parseRevoked = parseRevoked;
module.exports.pickLang = pickLang;
module.exports.identity = identity;
module.exports.ssoProviders = ssoProviders;
module.exports.SsoError = SsoError;
module.exports.gatewayAuth = gatewayAuth;
module.exports.gatewayIdentity = gatewayIdentity;
// The middleware calls magicLink.sendLoginMail, so tests can intercept sending the email.
module.exports.sendLoginMail = sendLoginMail;
