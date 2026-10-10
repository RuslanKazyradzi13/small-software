'use strict';
/**
 * Защита Node.js-приложения ссылкой-приглашением (magic link). Один файл, без зависимостей, Node 18+.
 *
 *   const magicLink = require('./magic-link');
 *   app.use(magicLink());                                               // Express / Connect
 *
 *   const guard = magicLink();                                          // чистый http
 *   http.createServer((req, res) => guard(req, res, () => handler(req, res)));
 *
 * Владелец раздаёт ссылку https://<app>/?token=<токен>. По ней middleware ставит куку и перенаправляет
 * на тот же адрес без токена. Без верного токена или куки — страница 403 с кнопкой «Запросить доступ».
 *
 * Переменные окружения:
 *   ACCESS_TOKEN        токены через запятую; у токена может быть имя: "alice:токен1,bob:токен2".
 *                       Убрали чей-то токен — его ссылка и выданные по ней куки перестают работать,
 *                       у остальных всё работает. Не задан — доступ закрыт для всех.
 *   ACCESS_REQUEST_URL  куда ведёт кнопка (mailto:..., https://t.me/...). Не задан — кнопка отправляет
 *                       форму в само приложение, и запрос печатается в лог строкой [access-request]
 *   TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
 *                       если заданы, запрос из формы ещё и приходит владельцу в Telegram
 *                       (подключить: small notify telegram)
 */

const crypto = require('node:crypto');

const COOKIE_NAME = 'small_access';
const COOKIE_MAX_AGE = 30 * 24 * 3600;
const REQUEST_PATH = '/_access/request';

const MESSAGES = {
  forbidden: ['&#128274;', 'Доступ по приглашению', 'Эта страница открывается только по ссылке-приглашению от владельца.'],
  invalid: ['&#128274;', 'Ссылка недействительна', 'Ссылка устарела или скопирована не полностью. Запросите новую.'],
  sent: ['&#9989;', 'Запрос отправлен', 'Владелец увидит запрос и пришлёт вам ссылку-приглашение.'],
};

function magicLink(options = {}) {
  const entries = parseTokens(options.tokens ?? process.env.ACCESS_TOKEN ?? '');
  const tokens = entries.map((e) => [e.name, e.token]);
  const cookies = entries.map((e) => [e.name, cookieValue(e.token)]);
  const requestUrl = options.requestUrl ?? process.env.ACCESS_REQUEST_URL ?? '';
  const cookieName = options.cookieName ?? COOKIE_NAME;
  const maxAge = options.maxAge ?? COOKIE_MAX_AGE;
  const telegramToken = String(process.env.TELEGRAM_BOT_TOKEN || '').trim();
  const telegramChat = String(process.env.TELEGRAM_CHAT_ID || '').trim();
  if (!entries.length) console.log('[magic-link] ACCESS_TOKEN не задан: доступ закрыт для всех');

  function page(req, res, status, kind) {
    const [icon, title, text] = MESSAGES[kind];
    let action = '';
    if (kind !== 'sent') {
      action = requestUrl
        ? `<a class="btn" href="${escapeHtml(requestUrl)}">Запросить доступ</a>`
        : requestForm(escapeHtml((req.baseUrl || '') + REQUEST_PATH));
    }
    const body = renderPage(icon, escapeHtml(title), escapeHtml(text), action);
    res.writeHead(status, {
      'Content-Type': 'text/html; charset=utf-8',
      'Content-Length': Buffer.byteLength(body),
      'Cache-Control': 'no-store',
    });
    res.end(body);
  }

  function accessRequest(req, res) {
    const done = (contact) => {
      const forwarded = String(req.headers['x-forwarded-for'] || req.socket.remoteAddress || '');
      const entry = {
        contact: String(contact || '').slice(0, 200),
        ip: forwarded.split(',')[0].trim(),
        ua: String(req.headers['user-agent'] || '').slice(0, 200),
      };
      // JSON.stringify экранирует переводы строк: пользователь не сможет подделать строки лога.
      console.log('[access-request] ' + JSON.stringify(entry));
      if (telegramToken && telegramChat) notifyTelegram(req.headers.host, entry);
      page(req, res, 200, 'sent');
    };

    // Без await: пользователь не ждёт Telegram. Без parse_mode — текст из формы ничего не разметит.
    function notifyTelegram(host, entry) {
      const api = String(process.env.TELEGRAM_API_URL || 'https://api.telegram.org').replace(/\/+$/, '');
      const text = `🔑 Запрос доступа к ${host || 'приложению'}\nКонтакт: ${entry.contact || '—'}\nIP: ${entry.ip || '—'}`
        + '\n\nВыдать личную ссылку: small access add ИМЯ';
      fetch(`${api}/bot${telegramToken}/sendMessage`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ chat_id: telegramChat, text }),
      }).catch((e) => console.log('[magic-link] не удалось отправить запрос доступа в Telegram: ' + e.name));
    }
    // Если тело уже разобрал express.urlencoded(), поток прочитан — берём готовое значение.
    if (req.body !== undefined || req.readableEnded) return done(req.body && req.body.contact);
    let body = '';
    req.setEncoding('utf8');
    req.on('data', (chunk) => { if (body.length < 4096) body += chunk; });
    req.on('end', () => done(new URLSearchParams(body.slice(0, 4096)).get('contact')));
  }

  return function magicLinkMiddleware(req, res, next) {
    const url = new URL(req.url, 'http://localhost');
    if (req.method === 'POST' && url.pathname === REQUEST_PATH && !requestUrl) return accessRequest(req, res);

    const token = url.searchParams.get('token');
    if (token !== null) {
      const name = owner(token, tokens);
      if (name === null) return page(req, res, 403, 'invalid');
      const ip = String(req.headers['x-forwarded-for'] || req.socket.remoteAddress || '').split(',')[0].trim();
      console.log('[magic-link] вход по ссылке' + (name ? ` (${name})` : '') + ', ip=' + ip);
      // Убираем токен из адреса, чтобы он не оставался в истории браузера и не утекал через Referer.
      // Ведущие слэши схлопываем: Location "//host" был бы открытым редиректом.
      const full = new URL(req.originalUrl || req.url, 'http://localhost');
      full.searchParams.delete('token');
      const proto = String(req.headers['x-forwarded-proto'] || '').split(',')[0].trim();
      const secure = proto === 'https' || req.socket.encrypted ? '; Secure' : '';
      res.writeHead(302, {
        Location: '/' + full.pathname.replace(/^\/+/, '') + full.search,
        'Set-Cookie': `${cookieName}=${cookieValue(token)}; Path=/; Max-Age=${maxAge}; HttpOnly; SameSite=Lax${secure}`,
        'Cache-Control': 'no-store',
      });
      return res.end();
    }

    if (owner(readCookie(req.headers.cookie, cookieName), cookies) !== null) return next();
    return page(req, res, 403, 'forbidden');
  };
}

// "alice:tok1, tok2" -> [{name: "alice", token: "tok1"}, {name: "", token: "tok2"}]. В токене не должно быть ":".
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

// В куке лежит не сам токен, а производное от него: убрали токен из ACCESS_TOKEN — его куки больше не подходят.
function cookieValue(token) {
  return crypto.createHmac('sha256', token).update('magic-link-cookie').digest('hex');
}

// Имя владельца секрета ("" — без имени) или null, если секрет не подошёл. pairs: [[имя, секрет], ...].
function owner(value, pairs) {
  if (value == null) return null;
  const digest = crypto.createHash('sha256').update(value).digest();
  for (const [name, secret] of pairs) {
    if (crypto.timingSafeEqual(digest, crypto.createHash('sha256').update(secret).digest())) return name;
  }
  return null;
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

function requestForm(action) {
  return `<form method="post" action="${action}">`
    + '<input name="contact" required maxlength="200" placeholder="Email или Telegram для ответа">'
    + '<button class="btn" type="submit">Запросить доступ</button></form>';
}

function renderPage(icon, title, text, action) {
  return `<!doctype html>
<html lang="ru">
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
  p { margin: 0 0 24px; color: var(--muted); }
  input { width: 100%; margin-bottom: 12px; padding: 10px 12px; font: inherit; color: inherit;
          background: transparent; border: 1px solid var(--border); border-radius: 8px; }
  .btn { display: block; width: 100%; padding: 10px 16px; font: inherit; font-weight: 600; color: #fff;
         background: var(--accent); border: 0; border-radius: 8px; text-decoration: none; cursor: pointer; }
  .btn:hover { filter: brightness(1.1); }
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
