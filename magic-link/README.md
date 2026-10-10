# magic-link: доступ к приложению по ссылке-приглашению

Простейшая защита микросервиса общим токеном. Один файл без зависимостей для каждого языка:

| Файл | Для чего |
|---|---|
| [magic_link.py](magic_link.py) | WSGI-middleware: Flask, Django, Bottle, `wsgiref`; Python 3.8+ |
| [magic-link.js](magic-link.js) | middleware `(req, res, next)`: Express, Connect, чистый `http`; Node 18+ |

Скопируйте нужный файл в папку приложения. `small deploy` загружает только эту папку. Проще всего начать с
`small init`: он кладёт эти файлы в новую заготовку сам. Их копии лежат в `small-cli/small_cli/templates/`. После
правки файлов здесь скопируйте их туда: тест `small-cli` упадёт, если копии разойдутся.

## Как это работает

1. Владелец раздаёт ссылку `https://<app>.up.railway.app/?token=<ACCESS_TOKEN>`.
2. По ней middleware ставит куку `small_access` (HttpOnly, SameSite=Lax, Secure за HTTPS, 30 дней)
   и делает редирект на тот же адрес **без** `token`, чтобы токен не оставался в истории браузера.
3. Дальше доступ даёт кука. Без неё или с неверным токеном отдаётся **403** с кнопкой «Запросить доступ».
4. Кнопка ведёт на `ACCESS_REQUEST_URL`, если он задан (`mailto:`, Telegram, Google Form). Если не задан, кнопка
   открывает форму «Email или Telegram для ответа». Запрос печатается в лог приложения строкой `[access-request] ...`:
   в Railway её видно в Deploy Logs, поиск по `access-request`.

| Переменная | Значение |
|---|---|
| `ACCESS_TOKEN` | токен; несколько через запятую, например по одному на человека. **Не задан — доступ закрыт для всех.** |
| `ACCESS_REQUEST_URL` | необязательно: куда ведёт кнопка «Запросить доступ» |

Сгенерировать токен:

```bash
python -c "import secrets; print(secrets.token_urlsafe(24))"
```

В Railway: сервис → **Variables** → `ACCESS_TOKEN`.

## Подключение

**Python**

```python
from magic_link import MagicLink

application = MagicLink(application)        # любой WSGI
app.wsgi_app = MagicLink(app.wsgi_app)      # Flask
```

Для Django: в `wsgi.py` `application = MagicLink(get_wsgi_application())`. Параметры конструктора
(`tokens=`, `request_url=`, `cookie_name=`, `max_age=`) переопределяют переменные окружения.
Пример с `wsgiref`: [../test-app/app.py](../test-app/app.py).

**Node.js**

```js
const magicLink = require('./magic-link');

app.use(magicLink());                                   // Express: всё приложение
app.use('/admin', magicLink());                         // или только раздел

const guard = magicLink();                              // чистый http
http.createServer((req, res) => guard(req, res, () => handler(req, res)));
```

Опции: `{ tokens, requestUrl, cookieName, maxAge }`.

## Чего эта защита не делает

- **Токен общий.** Нет пользователей и нет отзыва доступа у одного человека. Чтобы отозвать доступ, смените
  `ACCESS_TOKEN` (все выданные куки перестанут работать) или выдавайте по токену на человека и удаляйте нужный.
- **Первый запрос `/?token=...` попадает в access-логи** сервера и прокси Railway. В
  [test-app](../test-app/app.py) токен в логах маскируется (`token=***`). В своём сервере (gunicorn, morgan)
  сделайте так же или отключите логирование query-строк.
- ASGI (FastAPI, Starlette) не поддерживается.
- Это защита «от случайных глаз» для демо и внутренних инструментов. Для чувствительных данных нужна полноценная авторизация.

## Тесты

```bash
python -m unittest test_magic_link
node --test
```

Обе версии проверяют одни и те же 10 сценариев:
- 403 с кнопкой «Запросить доступ»;
- редирект без токена и флаги куки, Secure за прокси;
- проход с кукой;
- неверный токен и поддельная кука;
- открытый редирект;
- несколько токенов, пустой `ACCESS_TOKEN`;
- журнал запросов доступа без инъекции строк;
- режим `ACCESS_REQUEST_URL`.
