'use strict';
// A small app created by `small init`. Deploy it with `small deploy`.
//
// Who gets in is decided by magic-link.js: people you invite by work email (`small access add ivan@company.com`)
// or with a personal invite link (`small access add bob`). Write your own code in handler().

const http = require('node:http');
const magicLink = require('./magic-link');

const PAGE = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>small app</title>
</head>
<body style="font-family: system-ui, sans-serif; text-align: center; padding: 48px 16px;">
  <h1>🚀 It works!</h1>
  <p>This app was created by <code>small init</code>. Edit <code>server.js</code> and run <code>small deploy</code>.</p>
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
