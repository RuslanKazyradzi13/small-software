# magic-link: доступ к приложению по ссылке-приглашению

Простейшая защита микросервиса токенами — общим или личным на каждого человека. Один файл без зависимостей для
каждого языка:

| Файл | Для чего |
|---|---|
| [magic_link.py](magic_link.py) | `MagicLink` — WSGI (Flask, Django, Bottle, `wsgiref`); `MagicLinkASGI` — ASGI (FastAPI, Starlette); Python 3.8+ |
| [magic-link.js](magic-link.js) | middleware `(req, res, next)`: Express, Connect, чистый `http`; Node 18+ |

Скопируйте нужный файл в папку приложения. `small deploy` загружает только эту папку. Проще всего начать с
`small init`: он кладёт эти файлы в новую заготовку сам. Их копии лежат в `small-cli/small_cli/templates/`. После
правки файлов здесь скопируйте их туда: тест `small-cli` упадёт, если копии разойдутся.

## Как это работает

1. Владелец раздаёт ссылку `https://<app>.up.railway.app/?token=<токен>`.
2. По ней middleware ставит куку `small_access` (HttpOnly, SameSite=Lax, Secure за HTTPS, 30 дней)
   и делает редирект на тот же адрес **без** `token`, чтобы токен не оставался в истории браузера.
   В лог пишется строка `[magic-link] вход по ссылке (имя), ip=…`; самого токена в ней нет.
3. Дальше доступ даёт кука. Без неё или с неверным токеном отдаётся **403** с кнопкой «Запросить доступ».
4. Кнопка ведёт на `ACCESS_REQUEST_URL`, если он задан (`mailto:`, Telegram, Google Form). Если не задан, кнопка
   открывает форму «Email или Telegram для ответа». Запрос печатается в лог приложения строкой `[access-request] ...`:
   в Railway её видно в Deploy Logs, поиск по `access-request`.

| Переменная | Значение |
|---|---|
| `ACCESS_TOKEN` | токены через запятую; у токена может быть имя: `alice:токен1,bob:токен2`. **Не задан — доступ закрыт для всех.** |
| `ACCESS_REQUEST_URL` | необязательно: куда ведёт кнопка «Запросить доступ» |

### Токен на каждого человека

В куке хранится не сам токен, а производное от него. Поэтому, если убрать из `ACCESS_TOKEN` токен одного человека
(после перезапуска приложения), перестанут работать и его ссылка, и его уже выданная кука. У остальных всё останется
как было, менять общую ссылку не нужно.

С `small` это делается командами:

```
small access add bob       # выдать bob личную ссылку
small access revoke bob    # отозвать
```

Вручную тоже можно: имя — до двоеточия, в самом токене двоеточия быть не должно. Сгенерировать токен:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

В Railway: сервис → **Variables** → `ACCESS_TOKEN`.

## Подключение

**Python, WSGI**

```python
from magic_link import MagicLink

application = MagicLink(application)        # любой WSGI
app.wsgi_app = MagicLink(app.wsgi_app)      # Flask
```

Для Django: в `wsgi.py` `application = MagicLink(get_wsgi_application())`.
Пример: шаблон [python-http/app.py](../small-cli/small_cli/templates/python-http/app.py).

**Python, ASGI**

```python
from magic_link import MagicLinkASGI

app.add_middleware(MagicLinkASGI)           # FastAPI / Starlette
app = MagicLinkASGI(app)                    # любой ASGI
```

Закрыты все HTTP-запросы, в том числе `/docs`. Websocket без куки закрывается до установки соединения, сервер
отвечает 403. Lifespan проходит без проверки. Пример: шаблон
[fastapi/main.py](../small-cli/small_cli/templates/fastapi/main.py); в нём же фильтр, который прячет `?token=`
из access-лога `uvicorn`.

У обеих версий параметры конструктора `tokens=`, `request_url=`, `cookie_name=` и `max_age=` переопределяют
переменные окружения.

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

- **Нет учётных записей.** Токен — это просто ссылка: кто её получил, тот и войдёт. Имена нужны только для
  отзыва и для записей в логе.
- **Первый запрос `/?token=...` попадает в access-логи** сервера и прокси Railway. В шаблонах `python-http` и
  `fastapi` токен в логах сервера маскируется (`token=***`). В своём сервере (gunicorn, morgan) сделайте так же
  или отключите логирование query-строк.
- **Отзыв срабатывает после перезапуска** приложения: токены читаются при старте. `small access revoke`
  перезапускает приложение сам.
- Это защита «от случайных глаз» для демо и внутренних инструментов. Для чувствительных данных нужна полноценная
  авторизация.

## Тесты

```bash
python -m unittest test_magic_link     # 20: WSGI, ASGI, личные токены
node --test                            # 12
```

Проверяются:
- 403 с кнопкой «Запросить доступ»;
- редирект без токена и флаги куки, Secure за прокси;
- проход с кукой;
- неверный токен и поддельная кука;
- открытый редирект;
- несколько токенов и пустой `ACCESS_TOKEN`;
- журнал запросов доступа без инъекции строк;
- режим `ACCESS_REQUEST_URL`;
- личные токены: отзыв одного не задевает остальных;
- для ASGI ещё websocket и lifespan.
