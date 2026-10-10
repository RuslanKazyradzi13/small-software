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
  const entry = JSON.parse(line);
  assert.equal(entry.message, '[access-request] contact="@ivan\\n[access-request] fake" ip=203.0.113.7 ua="node"');
  assert.deepEqual([entry.level, entry.magic_link, entry.user, entry.ip], ['warn', 'request', '@ivan\n[access-request] fake', '203.0.113.7']);
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
  assert.deepEqual(events(log).map((e) => [e.message.replace(/ip .*/, 'ip'), e.magic_link, e.method, e.user]),
    [['[magic-link] вход по ссылке: bob, ip', 'sign_in', 'link', 'bob']]);
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
    ['en', ['Access request to', 'Grant access: small access add'], '[magic-link] signed in by link: bob'],
    ['ru', ['Запрос доступа к', 'Выдать доступ: small access add'], '[magic-link] вход по ссылке: bob'],
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
    assert.ok(events(log).some((e) => e.message.startsWith(login)), login);
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
  assert.ok(events(log).some((e) => e.message.startsWith('[magic-link] вход по почте: ivan@company.com, ip ')
    && e.method === 'email' && e.user === 'ivan@company.com'));
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
  assert.ok(events(log).some((e) => e.magic_link === 'request' && e.user === 'stranger@evil.example'));
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

// --- Log lines as JSON ---

// The JSON lines (sign-ins, refusals, access requests) among console.log calls.
function events(log) {
  return log.mock.calls.map((c) => String(c.arguments[0])).filter((l) => l.startsWith('{')).map((l) => JSON.parse(l));
}

// --- Single sign-on: Google and Microsoft against a token endpoint mock ---

const TENANT = '72f988bf-86f1-41af-91ab-2d7cd011db47';
const b64json = (value) => Buffer.from(JSON.stringify(value)).toString('base64url');

// Records the code exchange and answers with an unsigned ID token made of the claims the test sets (the app trusts
// TLS to the token endpoint, as OpenID Connect allows).
async function oidcProvider(t) {
  const provider = { requests: [], claims: {}, status: 200 };
  const server = http.createServer((req, res) => {
    let body = '';
    req.on('data', (c) => { body += c; });
    req.on('end', () => {
      const form = Object.fromEntries(new URLSearchParams(body));
      provider.requests.push({ path: req.url, form });
      const raw = provider.status === 200
        ? JSON.stringify({ access_token: 'at', id_token: `${b64json({ alg: 'RS256' })}.${b64json(provider.claims)}.c2ln` })
        : '{"error":"invalid_grant","error_description":"Bad code"}';
      res.writeHead(provider.status, { 'content-type': 'application/json' });
      res.end(raw);
    });
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  t.after(() => server.close());
  provider.url = `http://127.0.0.1:${server.address().port}`;
  return provider;
}

async function ssoServer(t, options = {}) {
  const provider = await oidcProvider(t);
  setEnv(t, { RAILWAY_PUBLIC_DOMAIN: 'reports.up.railway.app', PUBLIC_URL: undefined, LOGIN_LINKS_TO_LOG: undefined,
    RESEND_API_KEY: undefined, ACCESS_DB: undefined, RAILWAY_VOLUME_MOUNT_PATH: undefined, SESSIONS_REVOKED: undefined,
    GOOGLE_CLIENT_ID: '123-abc.apps.googleusercontent.com', GOOGLE_CLIENT_SECRET: 'GOCSPX-secret',
    GOOGLE_AUTH_URL: 'https://accounts.google.test/auth', GOOGLE_TOKEN_URL: provider.url + '/google',
    MICROSOFT_CLIENT_ID: 'ms-client', MICROSOFT_CLIENT_SECRET: 'ms-secret', MICROSOFT_TENANT_ID: TENANT.toUpperCase(),
    MICROSOFT_LOGIN_URL: provider.url, ...(options.env || {}) });
  const log = mock.method(console, 'log', () => {});
  t.after(() => log.mock.restore());
  const s = await serve({ tokens: '', requestUrl: '', emails: 'ivan@company.com, @team.com', secret: 'test-secret' });
  t.after(() => s.close());
  const port = s.address().port;
  const now = () => Math.floor(Date.now() / 1000);
  const claims = {
    google: (nonce, extra) => ({ iss: 'https://accounts.google.com', aud: '123-abc.apps.googleusercontent.com', sub: '1',
      exp: now() + 3600, iat: now(), nonce, email: 'ivan@company.com', email_verified: true, hd: 'company.com', ...extra }),
    microsoft: (nonce, extra) => ({ iss: `https://login.microsoftonline.com/${TENANT}/v2.0`, aud: 'ms-client', tid: TENANT,
      exp: now() + 3600, iat: now(), nonce, preferred_username: 'Ivan@Company.com', ...extra }),
  };
  const start = async (id = 'google') => {
    const res = await request(port, { path: '/_access/sso/' + id, headers: { 'x-forwarded-proto': 'https' } });
    assert.equal(res.status, 302, res.body);
    return { query: Object.fromEntries(new URL(res.headers.location).searchParams), cookie: res.headers['set-cookie'][0].split(';')[0], res };
  };
  const finish = (query, cookie, params = {}) => request(port, {
    path: '/_access/sso/callback?' + new URLSearchParams({ code: '4/0Abc', state: query.state, ...params }),
    headers: { 'x-forwarded-proto': 'https', 'x-forwarded-for': '203.0.113.9', 'user-agent': 'Firefox', ...(cookie ? { cookie } : {}) },
  });
  const signIn = async (id = 'google', extra = {}) => {
    const { query, cookie } = await start(id);
    provider.claims = claims[id](query.nonce, extra);
    for (const [k, v] of Object.entries(extra)) if (v === undefined) delete provider.claims[k];
    return finish(query, cookie);
  };
  return { port, log, provider, start, finish, signIn, claims };
}

test('single sign-on: the page offers Google and Microsoft, no email form without mail', async (t) => {
  const { port } = await ssoServer(t);
  const page = await request(port);
  assert.equal(page.status, 403);
  assert.equal(page.headers['x-frame-options'], 'DENY');
  assert.match(page.headers['content-security-policy'], /default-src 'none'.*form-action 'self'.*frame-ancestors 'none'/);
  assert.doesNotMatch(page.body, /<script/);
  assert.match(page.body, /Вход рабочим аккаунтом/);
  assert.match(page.body, /href="\/_access\/sso\/google"/);
  assert.match(page.body, /href="\/_access\/sso\/microsoft"/);
  assert.ok(page.body.indexOf('Войти через Google') < page.body.indexOf('Войти через Microsoft'));
  assert.doesNotMatch(page.body, /type="email"/);
  assert.match((await request(port, { headers: { 'accept-language': 'en' } })).body, /Sign in with Microsoft/);
  assert.equal((await request(port, { path: '/_access/sso/github' })).status, 400);
});

test('single sign-on with email set up shows both', async (t) => {
  const { port } = await ssoServer(t, { env: { LOGIN_LINKS_TO_LOG: '1' } });
  const body = (await request(port)).body;
  assert.match(body, /или по ссылке на почту/);
  assert.match(body, /type="email"/);
});

test('single sign-on: the authorization request and the code exchange (Google)', async (t) => {
  const { port, log, provider, start, finish, claims } = await ssoServer(t);
  const { query, cookie, res } = await start();
  assert.deepEqual({ client_id: query.client_id, redirect_uri: query.redirect_uri, response_type: query.response_type,
    scope: query.scope, code_challenge_method: query.code_challenge_method, prompt: query.prompt },
  { client_id: '123-abc.apps.googleusercontent.com', redirect_uri: 'https://reports.up.railway.app/_access/sso/callback',
    response_type: 'code', scope: 'openid email', code_challenge_method: 'S256', prompt: 'select_account' });
  assert.ok(query.state.startsWith('google.') && query.state.length > 30, query.state);
  for (const flag of ['small_access_sso=', 'HttpOnly', 'SameSite=Lax', 'Secure', 'Max-Age=600']) {
    assert.ok(res.headers['set-cookie'][0].includes(flag), flag);
  }
  assert.ok(!cookie.includes(query.nonce));

  provider.claims = claims.google(query.nonce);
  const done = await finish(query, cookie);
  assert.equal(done.status, 302);
  assert.equal(done.headers.location, '/');
  const [clear, session] = done.headers['set-cookie'];
  assert.equal(clear, 'small_access_sso=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax; Secure');
  assert.ok(session.startsWith('small_access=e.'), session);
  assert.equal((await request(port, { headers: { cookie: session.split(';')[0] } })).body, 'secret page');

  const { path, form } = provider.requests[0];
  assert.equal(path, '/google');
  assert.deepEqual({ grant_type: form.grant_type, code: form.code, redirect_uri: form.redirect_uri,
    client_id: form.client_id, client_secret: form.client_secret },
  { grant_type: 'authorization_code', code: '4/0Abc', redirect_uri: query.redirect_uri, client_id: query.client_id,
    client_secret: 'GOCSPX-secret' });
  assert.equal(crypto.createHash('sha256').update(form.code_verifier).digest('base64url'), query.code_challenge);
  const [event] = events(log);
  assert.deepEqual({ message: event.message, magic_link: event.magic_link, method: event.method, user: event.user, ip: event.ip, ua: event.ua },
    { message: '[magic-link] вход через Google: ivan@company.com, ip 203.0.113.9', magic_link: 'sign_in',
      method: 'google', user: 'ivan@company.com', ip: '203.0.113.9', ua: 'Firefox' });
  const all = log.mock.calls.map((c) => String(c.arguments[0])).join('\n');
  assert.ok(!all.includes('GOCSPX-secret') && !all.includes('4/0Abc'));
  // The same answer again (a stolen callback URL with the cookie): refused.
  assert.equal((await finish(query, cookie)).status, 400);
});

test('single sign-on: Microsoft members of the tenant only', async (t) => {
  const { provider, signIn } = await ssoServer(t);
  const done = await signIn('microsoft');
  assert.equal(done.status, 302);
  assert.equal(provider.requests[0].path, `/${TENANT}/oauth2/v2.0/token`);
  const other = '11111111-2222-3333-4444-555555555555';
  for (const extra of [{ idp: `https://sts.windows.net/${other}/` }, { idp: 'live.com' },
    { preferred_username: 'ivan_company.com#EXT#@corp.onmicrosoft.com' }]) {
    const res = await signIn('microsoft', extra);
    assert.equal(res.status, 403, JSON.stringify(extra));
    assert.match(res.body, /Нужен рабочий аккаунт/);
  }
  assert.equal((await signIn('microsoft', { idp: `https://sts.windows.net/${TENANT}/` })).status, 302);
  assert.equal((await signIn('microsoft', { tid: other })).status, 400);
  assert.equal((await signIn('microsoft', { iss: `https://login.microsoftonline.com/${other}/v2.0` })).status, 400);
});

test('single sign-on: accounts Google does not vouch for, tampered tokens', async (t) => {
  const { signIn, log } = await ssoServer(t);
  const personal = await signIn('google', { hd: undefined });
  assert.equal(personal.status, 403);
  assert.match(personal.body, /Нужен рабочий аккаунт/);
  assert.ok(log.mock.calls.some((c) => String(c.arguments[0]).includes('personal Google account')));
  assert.equal((await signIn('google', { email_verified: false })).status, 403);
  assert.equal((await signIn('google', { email: 'olga@team.com', hd: 'team.com' })).status, 302);
  const now = Math.floor(Date.now() / 1000);
  for (const extra of [{ aud: 'another-app' }, { aud: ['another-app', '123-abc.apps.googleusercontent.com'] },
    { exp: now - 3600 }, { exp: undefined }, { iat: now + 3600 }, { nonce: 'another' }, { nonce: undefined },
    { iss: 'https://evil.example' }, { email: undefined }, { email: 'not-an-email' }]) {
    const res = await signIn('google', extra);
    assert.equal(res.status, 400, JSON.stringify(extra));
    assert.match(res.body, /Не удалось войти/);
    assert.ok(!String(res.headers['set-cookie']).includes('small_access=e.'));
  }
});

test('single sign-on: the state ties the answer to the browser; provider errors', async (t) => {
  const { provider, start, finish, claims, log } = await ssoServer(t);
  const { query, cookie } = await start();
  provider.claims = claims.google(query.nonce);
  const other = await start();
  assert.equal((await finish(query, other.cookie)).status, 400); // login CSRF: another browser's state
  assert.equal((await finish(query, null)).status, 400);
  assert.equal((await finish({ state: 'google.forged' }, cookie)).status, 400);
  assert.equal(provider.requests.length, 0);
  assert.equal((await finish(query, cookie, { error: 'access_denied' })).status, 400);
  assert.ok(log.mock.calls.some((c) => String(c.arguments[0]).includes('access_denied')));
  provider.status = 400;
  assert.equal((await finish(query, cookie)).status, 400);
  assert.ok(log.mock.calls.some((c) => String(c.arguments[0]).includes('HTTP 400 invalid_grant')));
  provider.status = 200;
  assert.equal((await finish(query, cookie)).status, 302);
});

test('single sign-on: an address without access becomes an access request', async (t) => {
  const { signIn, log } = await ssoServer(t);
  const res = await signIn('google', { email: 'stranger@evil.example', hd: 'evil.example' });
  assert.equal(res.status, 403);
  assert.match(res.body, /Нет доступа/);
  assert.match(res.body, /stranger@evil\.example/);
  assert.deepEqual(events(log).map((e) => [e.magic_link, e.user]),
    [['denied', 'stranger@evil.example'], ['request', 'stranger@evil.example']]);
});

test('single sign-on: settings', (t) => {
  const env = { GOOGLE_CLIENT_ID: 'g', GOOGLE_CLIENT_SECRET: 's', MICROSOFT_CLIENT_ID: 'm', MICROSOFT_CLIENT_SECRET: 'x',
    MICROSOFT_TENANT_ID: TENANT };
  assert.deepEqual(Object.keys(magicLink.ssoProviders(env)), ['google', 'microsoft']);
  assert.deepEqual(Object.keys(magicLink.ssoProviders({ ...env, MICROSOFT_TENANT_ID: 'company.com' })), ['google']);
  assert.deepEqual(Object.keys(magicLink.ssoProviders({ ...env, GOOGLE_CLIENT_SECRET: '' })), ['microsoft']);
  const ms = magicLink.ssoProviders(env).microsoft;
  assert.equal(ms.tokenUrl, `https://login.microsoftonline.com/${TENANT}/oauth2/v2.0/token`);
  // The sign-in name (UPN), not the mutable email claim.
  assert.equal(magicLink.identity(ms, { aud: 'm', exp: 2000, nonce: 'n', iss: `https://login.microsoftonline.com/${TENANT}/v2.0`,
    tid: TENANT, preferred_username: 'Ivan@Company.com', email: 'boss@company.com' }, 'n', 1500), 'ivan@company.com');
  assert.throws(() => magicLink.identity({ id: 'google', clientId: 'g' }, { aud: 'g', exp: 2000, nonce: 'n',
    iss: 'accounts.google.com', email: 'a@company.com', email_verified: 'true' }, 'n', 1500),
  (e) => e instanceof magicLink.SsoError && e.unverified && e.email === 'a@company.com');
});

// --- Sign-in state in SQLite, sessions revoked by the owner, signing out everywhere ---

let sqliteAvailable = true;
try { require('node:sqlite'); } catch { sqliteAvailable = false; }

test('ACCESS_DB: a used link stays used after a restart; sign out on all devices', { skip: !sqliteAvailable && 'node:sqlite needs Node 22.13+' }, async (t) => {
  const os = require('node:os');
  const fs = require('node:fs');
  const path = require('node:path');
  const folder = fs.mkdtempSync(path.join(os.tmpdir(), 'magic-link-'));
  t.after(() => { try { fs.rmSync(folder, { recursive: true, force: true }); } catch { /* Windows: the database is still open */ } });
  setEnv(t, { ACCESS_DB: path.join(folder, 'state', 'access.db') });
  const { port, log, sent, ask, tokenOf, confirm } = await emailServer(t);
  assert.ok(log.mock.calls.some((c) => String(c.arguments[0]).includes('состояние входа хранится в')));
  await ask('ivan@company.com', '198.51.100.1');
  const token = tokenOf(sent[0][1]);
  const laptop = (await confirm(token)).headers['set-cookie'][0].split(';')[0];
  await ask('ivan@company.com', '198.51.100.2');
  const phone = (await confirm(tokenOf(sent[1][1]))).headers['set-cookie'][0].split(';')[0];

  const restarted = await serve({ tokens: '', requestUrl: '', emails: 'ivan@company.com, @team.com', secret: 'test-secret' });
  t.after(() => restarted.close());
  const again = await request(restarted.address().port, { method: 'POST', path: '/_access/verify',
    body: new URLSearchParams({ t: token }).toString() });
  assert.equal(again.status, 403);

  const out = await request(port, { path: '/_access/logout', headers: { cookie: laptop } });
  assert.match(out.body, /Выйти на всех устройствах/);
  assert.match((await request(port, { path: '/_access/logout' })).body, /Вы вышли/);
  assert.doesNotMatch((await request(port, { path: '/_access/logout' })).body, /Выйти на всех устройствах/);
  const everywhere = out.body.match(/name="t" value="([^"]+)"/)[1];
  assert.equal((await request(port, { headers: { cookie: phone } })).status, 200);
  const all = await request(port, { method: 'POST', path: '/_access/logout', body: new URLSearchParams({ t: everywhere }).toString() });
  assert.match(all.body, /Вы вышли на всех устройствах/);
  assert.equal(events(log).pop().magic_link, 'sign_out_all');
  assert.equal((await request(port, { headers: { cookie: phone } })).status, 403);
  assert.equal((await request(restarted.address().port, { headers: { cookie: phone } })).status, 403);
  // A forged token signs nobody out.
  await ask('ivan@company.com', '198.51.100.3');
  const fresh = (await confirm(tokenOf(sent[2][1]))).headers['set-cookie'][0].split(';')[0];
  await request(port, { method: 'POST', path: '/_access/logout', body: 't=forged' });
  assert.equal((await request(port, { headers: { cookie: fresh } })).status, 200);
});

test('without a store there is no "sign out on all devices"', async (t) => {
  const { port, sent, ask, tokenOf, confirm } = await emailServer(t);
  await ask('ivan@company.com');
  const cookie = (await confirm(tokenOf(sent[0][1]))).headers['set-cookie'][0].split(';')[0];
  assert.doesNotMatch((await request(port, { path: '/_access/logout', headers: { cookie } })).body, /Выйти на всех устройствах/);
});

test('SESSIONS_REVOKED: an address, a domain, everyone; new sign-ins work', async (t) => {
  assert.deepEqual(magicLink.parseRevoked(' Ivan@Company.com=100, @team.com=5, *=7, junk=1, x@y.com=abc, ivan@company.com=50, __proto__=1'),
    { '=ivan@company.com': 100, '=@team.com': 5, '=*': 7 });
  setEnv(t, { SESSIONS_REVOKED: undefined });
  const { sent, ask, tokenOf, confirm } = await emailServer(t);
  await ask('ivan@company.com', '198.51.100.1');
  const ivan = (await confirm(tokenOf(sent[0][1]))).headers['set-cookie'][0].split(';')[0];
  await ask('olga@team.com', '198.51.100.2');
  const olga = (await confirm(tokenOf(sent[1][1]))).headers['set-cookie'][0].split(';')[0];
  const later = Math.floor(Date.now() / 1000) + 5;
  for (const [value, ivanStatus, olgaStatus] of [[`ivan@company.com=${later}`, 403, 200], [`@team.com=${later}`, 200, 403],
    [`*=${later}`, 403, 403], [`ivan@company.com=${later - 3600}`, 200, 200]]) {
    process.env.SESSIONS_REVOKED = value;
    const s = await serve({ tokens: '', emails: 'ivan@company.com, @team.com', secret: 'test-secret' });
    const port = s.address().port;
    assert.deepEqual([(await request(port, { headers: { cookie: ivan } })).status, (await request(port, { headers: { cookie: olga } })).status],
      [ivanStatus, olgaStatus], value);
    s.close();
  }
});
