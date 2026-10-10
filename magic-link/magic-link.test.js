'use strict';
// Тесты magic-link.js. Запуск: node --test
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const http = require('node:http');
const { test, before, after, mock } = require('node:test');

const magicLink = require('./magic-link');

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

test('без токена — 403 с кнопкой «Запросить доступ»', async () => {
  const res = await get('/');
  const body = await res.text();
  assert.equal(res.status, 403);
  assert.equal(res.headers.get('content-type'), 'text/html; charset=utf-8');
  assert.match(body, /Доступ по приглашению/);
  assert.match(body, /Запросить доступ/);
  assert.match(body, /action="\/_access\/request"/);
  assert.doesNotMatch(body, /secret page/);
});

test('верный токен ставит куку и убирает токен из адреса', async () => {
  const res = await get(`/reports?token=${TOKEN}&page=2`);
  assert.equal(res.status, 302);
  assert.equal(res.headers.get('location'), '/reports?page=2');
  const cookie = res.headers.get('set-cookie');
  assert.ok(cookie.startsWith(COOKIE + ';'), cookie);
  assert.ok(!cookie.includes(TOKEN));
  for (const flag of ['HttpOnly', 'SameSite=Lax', 'Path=/', 'Max-Age=2592000']) assert.ok(cookie.includes(flag), flag);
  assert.ok(!cookie.includes('Secure'));
});

test('за https-прокси кука получает Secure', async () => {
  const res = await get(`/?token=${TOKEN}`, { 'x-forwarded-proto': 'https' });
  assert.ok(res.headers.get('set-cookie').includes('; Secure'));
});

test('с верной кукой пропускает в приложение', async () => {
  const res = await get('/', { cookie: 'other=1; ' + COOKIE });
  assert.equal(res.status, 200);
  assert.equal(await res.text(), 'secret page');
});

test('неверный токен и поддельная кука — 403', async () => {
  const res = await get('/?token=wrong');
  assert.equal(res.status, 403);
  assert.match(await res.text(), /Ссылка недействительна/);
  for (const cookie of [`small_access=${TOKEN}`, 'small_access=deadbeef', 'garbage;;==']) {
    assert.equal((await get('/', { cookie })).status, 403, cookie);
  }
});

test('нет открытого редиректа', async () => {
  const res = await new Promise((resolve) => {
    http.get({ host: '127.0.0.1', port: server.address().port, path: `//evil.example/x?token=${TOKEN}` }, resolve);
  });
  res.resume();
  assert.equal(res.statusCode, 302);
  assert.ok(!res.headers.location.startsWith('//'), res.headers.location);
});

test('несколько токенов через запятую в ACCESS_TOKEN', async (t) => {
  t.after(() => { delete process.env.ACCESS_TOKEN; });
  process.env.ACCESS_TOKEN = 'alice-token, bob-token';
  const s = await serve();
  t.after(() => s.close());
  const url = (q) => fetch(`http://127.0.0.1:${s.address().port}/?token=${q}`, { redirect: 'manual' });
  assert.equal((await url('bob-token')).status, 302);
  assert.equal((await url('alice-token')).status, 302);
  assert.equal((await url('carol')).status, 403);
});

test('без ACCESS_TOKEN доступ закрыт для всех', async (t) => {
  const log = mock.method(console, 'log', () => {});
  t.after(() => log.mock.restore());
  const s = await serve({ tokens: '' });
  t.after(() => s.close());
  assert.match(log.mock.calls[0].arguments[0], /ACCESS_TOKEN не задан/);
  const port = s.address().port;
  assert.equal((await fetch(`http://127.0.0.1:${port}/`)).status, 403);
  assert.equal((await fetch(`http://127.0.0.1:${port}/?token=`)).status, 403);
});

test('запрос доступа пишется в лог одной строкой', async (t) => {
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
  assert.ok(!line.includes('\n'), 'перевод строки из формы не должен порождать поддельную строку лога');
  const entry = JSON.parse(line.replace('[access-request] ', ''));
  assert.equal(entry.contact, '@ivan\n[access-request] fake');
  assert.equal(entry.ip, '203.0.113.7');
});

test('ACCESS_REQUEST_URL заменяет форму ссылкой', async (t) => {
  const s = await serve({ tokens: TOKEN, requestUrl: 'mailto:owner@example.com?subject=Доступ"' });
  t.after(() => s.close());
  const port = s.address().port;
  const body = await (await fetch(`http://127.0.0.1:${port}/`)).text();
  assert.match(body, /<a class="btn" href="mailto:owner@example\.com\?subject=Доступ&quot;">/);
  assert.doesNotMatch(body, /<form/);
  const post = await fetch(`http://127.0.0.1:${port}/_access/request`, { method: 'POST' });
  assert.equal(post.status, 403);
});
