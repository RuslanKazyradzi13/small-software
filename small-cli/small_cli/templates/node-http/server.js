'use strict';
// Микро-приложение, созданное `small init`. Деплой: `small deploy`.
//
// Доступ по ссылке-приглашению https://<домен>/?token=<ACCESS_TOKEN>: токен лежит в small.json,
// проверку делает magic-link.js. Свой код пишите в handler().

const http = require('node:http');
const magicLink = require('./magic-link');

const PAGE = `<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>small app</title>
</head>
<body style="font-family: system-ui, sans-serif; text-align: center; padding: 48px 16px;">
  <h1>🚀 Работает!</h1>
  <p>Приложение создано командой <code>small init</code>. Измените <code>server.js</code> и выполните <code>small deploy</code>.</p>
</body>
</html>
`;

function handler(req, res) {
  res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
  res.end(PAGE);
}

const guard = magicLink();
const host = process.env.HOST || '0.0.0.0';
const port = Number(process.env.PORT) || 8080;
http.createServer((req, res) => guard(req, res, () => handler(req, res)))
  .listen(port, host, () => console.log(`Listening on ${host}:${port}`));
