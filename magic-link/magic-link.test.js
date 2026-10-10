'use strict';
// Tests for magic-link.js. Run: node --test
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const http = require('node:http');
const { test, before, after, mock } = require('node:test');

const magicLink = require('./magic-link');

// The checks read Russian log lines; the English ones are in the OWNER_LANG test.
process.env.OWNER_LANG = 'ru';

const TOKEN = 's3cret-token';
const COOKIE = 'small_access=' + crypto.createHmac('sha256', TOKEN).update('magic-link-cookie').digest('hex');

function serve(options) {
  const guard = magicLink(options);
  const server = http.createServer((req, res) => guard(req, res, () => res.end('secret page')));
  return new Promise((resolve) => server.listen(0, '127.0.0.1', () => resolve(server)));
}

let server;
let base;
before(async () => {
  server = await serve({ tokens: TOKEN, requestUrl: '' });
  base = `http://127.0.0.1:${server.address().port}`;
});
after(() => server.close());

const get = (path, headers = {}) => fetch(base + path, { headers, redirect: 'manual' });

test('no token: 403 with a "Request access" button', async () => {
  const res = await get('/');
  const body = await res.text();
  assert.equal(res.status, 403);
  assert.equal(res.headers.get('content-type'), 'text/html; charset=utf-8');
  assert.match(body, /Доступ по приглашению/);
  assert.match(body, /Запросить доступ/);
  assert.match(body, /action="\/_access\/request"/);
  assert.doesNotMatch(body, /secret page/);
});

test('a valid token sets the cookie and strips the token from the URL', async () => {
  const res = await get(`/reports?token=${TOKEN}&page=2`);
  assert.equal(res.status, 302);
  assert.equal(res.headers.get('location'), '/reports?page=2');
  const cookie = res.headers.get('set-cookie');
  assert.ok(cookie.startsWith(COOKIE + ';'), cookie);
  assert.ok(!cookie.includes(TOKEN));
  for (const flag of ['HttpOnly', 'SameSite=Lax', 'Path=/', 'Max-Age=2592000']) assert.ok(cookie.includes(flag), flag);
  assert.ok(!cookie.includes('Secure'));
});

test('behind an https proxy the cookie gets Secure', async () => {
  const res = await get(`/?token=${TOKEN}`, { 'x-forwarded-proto': 'https' });
  assert.ok(res.headers.get('set-cookie').includes('; Secure'));
});

test('a valid cookie lets the request through to the app', async () => {
  const res = await get('/', { cookie: 'other=1; ' + COOKIE });
  assert.equal(res.status, 200);
  assert.equal(await res.text(), 'secret page');
});

test('a wrong token and a forged cookie get 403', async () => {
  const res = await get('/?token=wrong');
  assert.equal(res.status, 403);
  assert.match(await res.text(), /Ссылка недействительна/);
  for (const cookie of [`small_access=${TOKEN}`, 'small_access=deadbeef', 'garbage;;==']) {
    assert.equal((await get('/', { cookie })).status, 403, cookie);
  }
});

test('no open redirect', async () => {
  const res = await new Promise((resolve) => {
    http.get({ host: '127.0.0.1', port: server.address().port, path: `//evil.example/x?token=${TOKEN}` }, resolve);
  });
  res.resume();
  assert.equal(res.statusCode, 302);
  assert.ok(!res.headers.location.startsWith('//'), res.headers.location);
});

test('several comma-separated tokens in ACCESS_TOKEN', async (t) => {
  t.after(() => { delete process.env.ACCESS_TOKEN; });
  process.env.ACCESS_TOKEN = 'alice-token, bob-token';
  const s = await serve();
  t.after(() => s.close());
  const url = (q) => fetch(`http://127.0.0.1:${s.address().port}/?token=${q}`, { redirect: 'manual' });
  assert.equal((await url('bob-token')).status, 302);
  assert.equal((await url('alice-token')).status, 302);
  assert.equal((await url('carol')).status, 403);
});

test('without ACCESS_TOKEN nobody gets in', async (t) => {
  const log = mock.method(console, 'log', () => {});
  t.after(() => log.mock.restore());
  const s = await serve({ tokens: '', emails: '' });
  t.after(() => s.close());
  assert.match(log.mock.calls[0].arguments[0], /доступ закрыт для всех/);
  const port = s.address().port;
  assert.equal((await fetch(`http://127.0.0.1:${port}/`)).status, 403);
  assert.equal((await fetch(`http://127.0.0.1:${port}/?token=`)).status, 403);
});

test('an access request is logged as a single line', async (t) => {
  const log = mock.method(console, 'log', () => {});
  t.after(() => log.mock.restore());
  const res = await fetch(base + '/_access/request', {
    method: 'POST',
    headers: { 'content-type': 'application/x-www-form-urlencoded', 'x-forwarded-for': '203.0.113.7, 10.0.0.1' },
    body: 'contact=%40ivan%0A[access-request] fake',
  });
  const body = await res.text();
  assert.equal(res.status, 200);
  assert.match(body, /Запрос отправлен/);
  assert.doesNotMatch(body, /<form/);
  const line = log.mock.calls[0].arguments[0];
  assert.ok(!line.includes('\n'), 'a line break from the form must not create a forged log line');
  const entry = JSON.parse(line.replace('[access-request] ', ''));
  assert.equal(entry.contact, '@ivan\n[access-request] fake');
  assert.equal(entry.ip, '203.0.113.7');
});

test('parsing named tokens', () => {
  assert.deepEqual(magicLink.parseTokens(' alice:tok-a, tok-b ,, bob : tok-c '),
    [{ name: 'alice', token: 'tok-a' }, { name: '', token: 'tok-b' }, { name: 'bob', token: 'tok-c' }]);
  assert.deepEqual(magicLink.parseTokens(''), []);
});

test('one token per person: revoking one does not affect the others', async (t) => {
  const log = mock.method(console, 'log', () => {});
  t.after(() => log.mock.restore());
  const full = await serve({ tokens: 'alice:tok-a,bob:tok-b' });
  t.after(() => full.close());
  const res = await fetch(`http://127.0.0.1:${full.address().port}/?token=tok-b`, { redirect: 'manual' });
  assert.equal(res.status, 302);
  assert.ok(log.mock.calls.some((c) => /вход по ссылке \(bob\)/.test(c.arguments[0])));
  assert.ok(!log.mock.calls.some((c) => String(c.arguments[0]).includes('tok-b')));
  const bobCookie = res.headers.get('set-cookie').split(';')[0];
  const aliceCookie = 'small_access=' + crypto.createHmac('sha256', 'tok-a').update('magic-link-cookie').digest('hex');

  const revoked = await serve({ tokens: 'alice:tok-a' }); // bob removed from ACCESS_TOKEN
  t.after(() => revoked.close());
  const url = `http://127.0.0.1:${revoked.address().port}/`;
  assert.equal((await fetch(url, { headers: { cookie: bobCookie } })).status, 403);
  assert.equal((await fetch(url + '?token=tok-b', { redirect: 'manual' })).status, 403);
  assert.equal((await fetch(url, { headers: { cookie: aliceCookie } })).status, 200);
});

test('an access request reaches the owner in Telegram', async (t) => {
  const messages = [];
  const inbox = http.createServer((req, res) => {
    let body = '';
    req.on('data', (c) => { body += c; });
    req.on('end', () => { messages.push({ path: req.url, body: JSON.parse(body) }); res.end('{"ok":true,"result":{}}'); });
  });
  await new Promise((resolve) => inbox.listen(0, '127.0.0.1', resolve));
  t.after(() => inbox.close());
  const saved = { ...process.env };
  t.after(() => { for (const k of ['TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID', 'TELEGRAM_API_URL']) { if (saved[k] === undefined) delete process.env[k]; else process.env[k] = saved[k]; } });
  Object.assign(process.env, { TELEGRAM_BOT_TOKEN: '123:abc', TELEGRAM_CHAT_ID: '42', TELEGRAM_API_URL: `http://127.0.0.1:${inbox.address().port}` });
  const log = mock.method(console, 'log', () => {});
  t.after(() => log.mock.restore());

  const s = await serve({ tokens: TOKEN, requestUrl: '' });
  t.after(() => s.close());
  const res = await fetch(`http://127.0.0.1:${s.address().port}/_access/request`, {
    method: 'POST',
    headers: { 'content-type': 'application/x-www-form-urlencoded', 'x-forwarded-for': '203.0.113.7' },
    body: 'contact=bob%40example.com',
  });
  assert.equal(res.status, 200);
  for (let i = 0; i < 50 && !messages.length; i++) await new Promise((r) => setTimeout(r, 50));
  assert.equal(messages.length, 1);
  assert.equal(messages[0].path, '/bot123:abc/sendMessage');
  assert.equal(messages[0].body.chat_id, '42');
  assert.equal(messages[0].body.parse_mode, undefined);
  for (const part of ['127.0.0.1', 'bob@example.com', '203.0.113.7', 'small access add']) {
    assert.ok(messages[0].body.text.includes(part), part);
  }
});

test('the owner reads the log and Telegram in their language (OWNER_LANG)', async (t) => {
  const messages = [];
  const inbox = http.createServer((req, res) => {
    let body = '';
    req.on('data', (c) => { body += c; });
    req.on('end', () => { messages.push(JSON.parse(body)); res.end('{"ok":true,"result":{}}'); });
  });
  await new Promise((resolve) => inbox.listen(0, '127.0.0.1', resolve));
  t.after(() => inbox.close());
  const log = mock.method(console, 'log', () => {});
  t.after(() => log.mock.restore());
  setEnv(t, { OWNER_LANG: 'en', TELEGRAM_BOT_TOKEN: '123:abc', TELEGRAM_CHAT_ID: '42',
    TELEGRAM_API_URL: `http://127.0.0.1:${inbox.address().port}` });
  for (const [lang, words, login] of [
    ['en', ['Access request to', 'Grant access: small access add'], '[magic-link] signed in by link (bob)'],
    ['ru', ['Запрос доступа к', 'Выдать доступ: small access add'], '[magic-link] вход по ссылке (bob)'],
  ]) {
    process.env.OWNER_LANG = lang;
    messages.length = 0;
    const s = await serve({ tokens: 'bob:' + TOKEN, requestUrl: '' });
    const url = `http://127.0.0.1:${s.address().port}`;
    await fetch(`${url}/_access/request`, { method: 'POST', headers: { 'content-type': 'application/x-www-form-urlencoded' }, body: 'contact=x' });
    await fetch(`${url}/?token=${TOKEN}`, { redirect: 'manual' });
    s.close();
    for (let i = 0; i < 50 && !messages.length; i++) await new Promise((r) => setTimeout(r, 50));
    for (const word of words) assert.ok(messages[0].text.includes(word), word);
    assert.ok(log.mock.calls.some((c) => String(c.arguments[0]).startsWith(login)), login);
  }
});

test('ACCESS_REQUEST_URL replaces the form with a link', async (t) => {
  const s = await serve({ tokens: TOKEN, requestUrl: 'mailto:owner@example.com?subject=Доступ"' });
  t.after(() => s.close());
  const port = s.address().port;
  const body = await (await fetch(`http://127.0.0.1:${port}/`)).text();
  assert.match(body, /<a class="btn" href="mailto:owner@example\.com\?subject=Доступ&quot;">/);
  assert.doesNotMatch(body, /<form/);
  const post = await fetch(`http://127.0.0.1:${port}/_access/request`, { method: 'POST' });
  assert.equal(post.status, 403);
});

// --- Email sign-in ---

function setEnv(t, vars) {
  const saved = {};
  for (const [k, v] of Object.entries(vars)) {
    saved[k] = process.env[k];
    if (v === undefined) delete process.env[k]; else process.env[k] = v;
  }
  t.after(() => { for (const [k, v] of Object.entries(saved)) { if (v === undefined) delete process.env[k]; else process.env[k] = v; } });
}

// http.request rather than fetch, so we can set our own Host header.
function request(port, { method = 'GET', path = '/', headers = {}, body = '' } = {}) {
  return new Promise((resolve, reject) => {
    const req = http.request({ host: '127.0.0.1', port, method, path,
      headers: { 'content-type': 'application/x-www-form-urlencoded', ...headers } }, (res) => {
      let data = '';
      res.setEncoding('utf8');
      res.on('data', (c) => { data += c; });
      res.on('end', () => resolve({ status: res.statusCode, headers: res.headers, body: data }));
    });
    req.on('error', reject);
    req.end(body);
  });
}

async function emailServer(t, options = {}) {
  setEnv(t, { RAILWAY_PUBLIC_DOMAIN: 'reports.up.railway.app', PUBLIC_URL: undefined, LOGIN_LINKS_TO_LOG: '1', RESEND_API_KEY: undefined });
  const log = mock.method(console, 'log', () => {});
  t.after(() => log.mock.restore());
  const sent = [];
  const original = magicLink.sendLoginMail;
  magicLink.sendLoginMail = (...args) => { sent.push(args); };
  t.after(() => { magicLink.sendLoginMail = original; });
  const s = await serve({ tokens: '', requestUrl: '', emails: 'ivan@company.com, @team.com', secret: 'test-secret', ...options });
  t.after(() => s.close());
  const port = s.address().port;
  const ask = (email, ip = '203.0.113.7', host = 'reports.up.railway.app') => request(port, {
    method: 'POST', path: '/_access/login', body: new URLSearchParams({ email }).toString(),
    headers: { 'x-forwarded-for': ip, host } });
  const tokenOf = (link) => new URL(link).searchParams.get('t');
  const confirm = (token) => request(port, { method: 'POST', path: '/_access/verify', body: new URLSearchParams({ t: token }).toString(),
    headers: { 'x-forwarded-proto': 'https' } });
  return { port, log, sent, ask, tokenOf, confirm };
}

test('email sign-in: email, "Sign in" button, cookie, one-time link', async (t) => {
  const { port, log, sent, ask, tokenOf, confirm } = await emailServer(t);
  const start = await request(port);
  assert.equal(start.status, 403);
  assert.match(start.body, /Вход по рабочей почте/);
  assert.match(start.body, /action="\/_access\/login"/);
  assert.doesNotMatch(start.body, /Запросить доступ/);

  const answer = await ask(' Ivan@Company.com ');
  assert.equal(answer.status, 200);
  assert.match(answer.body, /Проверьте почту/);
  assert.equal(sent.length, 1);
  const [email, link, appName] = sent[0];
  assert.equal(email, 'ivan@company.com');
  assert.equal(appName, 'reports.up.railway.app');
  assert.ok(link.startsWith('https://reports.up.railway.app/_access/verify?t='), link);

  const token = tokenOf(link);
  for (let i = 0; i < 2; i++) { // a mail scanner opens the link: no sign-in happens yet
    const opened = await request(port, { path: '/_access/verify?t=' + encodeURIComponent(token) });
    assert.equal(opened.status, 200);
    assert.match(opened.body, /войти как ivan@company\.com/);
    assert.equal(opened.headers['set-cookie'], undefined);
    assert.equal(opened.headers['referrer-policy'], 'no-referrer');
  }
  const done = await confirm(token);
  assert.equal(done.status, 302);
  assert.equal(done.headers.location, '/');
  const cookie = done.headers['set-cookie'][0];
  assert.ok(cookie.startsWith('small_access=e.'), cookie);
  for (const flag of ['HttpOnly', 'SameSite=Lax', 'Secure', 'Max-Age=2592000']) assert.ok(cookie.includes(flag), flag);
  assert.ok(log.mock.calls.some((c) => /вход по почте \(ivan@company\.com\)/.test(c.arguments[0])));
  const inside = await request(port, { headers: { cookie: cookie.split(';')[0] } });
  assert.equal(inside.status, 200);
  assert.equal(inside.body, 'secret page');
  const again = await confirm(token);
  assert.equal(again.status, 403);
  assert.match(again.body, /Ссылка для входа устарела/);
});

test('email sign-in: an unknown address gets the same response and an access request; whole domain', async (t) => {
  const { log, sent, ask } = await emailServer(t);
  const known = await ask('ivan@company.com');
  const unknown = await ask('stranger@evil.example', '198.51.100.1');
  assert.equal(unknown.status, known.status);
  assert.equal(unknown.body, known.body);
  assert.ok(log.mock.calls.some((c) => String(c.arguments[0]).includes('"contact":"stranger@evil.example"')));
  await ask('petr@team.com', '198.51.100.2');
  await ask('petr@team.com.evil.example', '198.51.100.3');
  assert.deepEqual(sent.map((m) => m[0]), ['ivan@company.com', 'petr@team.com']);
});

test('email sign-in: revoked address, forged cookie, different secret', async (t) => {
  const { sent, ask, tokenOf, confirm } = await emailServer(t);
  await ask('ivan@company.com');
  const cookie = (await confirm(tokenOf(sent[0][1]))).headers['set-cookie'][0].split(';')[0];

  const revoked = await serve({ tokens: '', emails: '@team.com', secret: 'test-secret' });
  t.after(() => revoked.close());
  assert.equal((await request(revoked.address().port, { headers: { cookie } })).status, 403);
  const otherSecret = await serve({ tokens: '', emails: 'ivan@company.com', secret: 'another' });
  t.after(() => otherSecret.close());
  assert.equal((await request(otherSecret.address().port, { headers: { cookie } })).status, 403);

  const mac = cookie.split('.').pop();
  const forged = Buffer.from(JSON.stringify({ e: 'boss@company.com', x: 9999999999 })).toString('base64url');
  const s = await serve({ tokens: '', emails: 'ivan@company.com, boss@company.com', secret: 'test-secret' });
  t.after(() => s.close());
  assert.equal((await request(s.address().port, { headers: { cookie: `small_access=e.${forged}.${mac}` } })).status, 403);
  // A sign-in link can't be passed off as a session.
  await ask('ivan@company.com', '198.51.100.9');
  assert.equal((await request(s.address().port, { headers: { cookie: 'small_access=e.' + tokenOf(sent[1][1]) } })).status, 403);
});

test('email sign-in: email limit, invalid address, Host does not change the link, sign-out', async (t) => {
  const { port, sent, ask } = await emailServer(t);
  for (let i = 0; i < 3; i++) assert.equal((await ask('ivan@company.com', `198.51.100.${i}`)).status, 200);
  const slow = await ask('ivan@company.com', '198.51.100.99');
  assert.equal(slow.status, 429);
  assert.match(slow.body, /Слишком много попыток/);
  assert.equal((await ask('not-an-email', '198.51.100.50')).status, 400);

  delete process.env.RAILWAY_PUBLIC_DOMAIN;
  const before = sent.length;
  await ask('olga@team.com', '198.51.100.60', 'evil.example');
  assert.equal(sent.length, before);
  await ask('olga@team.com', '198.51.100.61', 'localhost:3000');
  assert.ok(sent[sent.length - 1][1].startsWith('http://localhost:3000/_access/verify?t='));
  process.env.PUBLIC_URL = 'https://reports.company.com/';
  await ask('olga@team.com', '198.51.100.62', 'evil.example');
  assert.ok(sent[sent.length - 1][1].startsWith('https://reports.company.com/_access/verify?t='));

  const out = await request(port, { path: '/_access/logout' });
  assert.equal(out.status, 200);
  assert.ok(out.headers['set-cookie'][0].includes('Max-Age=0'));
});

test('email goes through Resend; errors are logged without the key; log mode', async (t) => {
  const requests = [];
  let status = 200;
  const api = http.createServer((req, res) => {
    let body = '';
    req.on('data', (c) => { body += c; });
    req.on('end', () => {
      requests.push({ path: req.url, headers: req.headers, body: JSON.parse(body) });
      res.writeHead(status);
      res.end(status === 200 ? '{"id":"msg_1"}' : '{"message":"The domain is not verified"}');
    });
  });
  await new Promise((resolve) => api.listen(0, '127.0.0.1', resolve));
  t.after(() => api.close());
  setEnv(t, { LOGIN_LINKS_TO_LOG: undefined, RESEND_API_KEY: 're_test_key', MAIL_FROM: 'Отчёты <noreply@company.com>',
    RESEND_API_URL: `http://127.0.0.1:${api.address().port}` });
  const log = mock.method(console, 'log', () => {});
  t.after(() => log.mock.restore());
  const link = 'https://reports.up.railway.app/_access/verify?t=abc.def';

  await magicLink.sendLoginMail('ivan@company.com', link, 'reports.up.railway.app');
  const { path, headers, body } = requests[0];
  assert.equal(path, '/emails');
  assert.equal(headers.authorization, 'Bearer re_test_key');
  assert.deepEqual(body.to, ['ivan@company.com']);
  assert.equal(body.from, 'Отчёты <noreply@company.com>');
  assert.ok(body.text.includes(link));
  assert.ok(log.mock.calls.some((c) => /письмо для входа отправлено: ivan@company\.com/.test(c.arguments[0])));

  status = 403;
  await magicLink.sendLoginMail('ivan@company.com', link, 'reports.up.railway.app');
  const failure = log.mock.calls.map((c) => String(c.arguments[0])).find((l) => l.includes('не удалось'));
  assert.match(failure, /HTTP 403 .*not verified/);
  assert.ok(!failure.includes('re_test_key'));

  process.env.LOGIN_LINKS_TO_LOG = '1';
  await magicLink.sendLoginMail('ivan@company.com', link, 'reports.up.railway.app');
  assert.ok(log.mock.calls.some((c) => String(c.arguments[0]) === `[magic-link] ссылка для входа (ivan@company.com): ${link}`));
});

test('language: Russian for Russian speakers, English for others, ACCESS_LANG beats the browser', async (t) => {
  const pick = magicLink.pickLang;
  assert.equal(pick(''), 'ru');
  assert.equal(pick('en-US,en;q=0.9,ru;q=0.8'), 'en');
  assert.equal(pick('ru-RU,ru;q=0.9,en-US;q=0.8'), 'ru');
  assert.equal(pick('fr-FR'), 'en');
  assert.equal(pick('*'), 'ru'); // what Node's fetch sends by default: "any language"
  assert.equal(pick('en;q=0.3, ru;q=0.6'), 'ru');
  assert.equal(pick('ru', 'en'), 'en');

  const { port, sent, ask } = await emailServer(t);
  const english = { 'accept-language': 'en-US,en;q=0.9' };
  const page = await request(port, { headers: english });
  assert.equal(page.headers['content-language'], 'en');
  assert.match(page.body, /<html lang="en">/);
  assert.match(page.body, /Sign in with your work email/);
  assert.match(page.body, /Email me a sign-in link/);
  await request(port, { method: 'POST', path: '/_access/login', body: 'email=ivan%40company.com',
    headers: { ...english, 'x-forwarded-for': '198.51.100.70', host: 'reports.up.railway.app' } });
  assert.equal(sent[0][3], 'en'); // the email is in the language of whoever asked to sign in
  const forced = await serve({ tokens: TOKEN, requestUrl: '', emails: '', lang: 'en' });
  t.after(() => forced.close());
  const body = (await request(forced.address().port)).body;
  assert.match(body, /Invitation only/);
  assert.match(body, />Request access<\/button>/);
});
