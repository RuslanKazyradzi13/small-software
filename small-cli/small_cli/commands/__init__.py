"""Команды small. Каждый модуль добавляет свои подкоманды функцией register(subparsers).

Порядок здесь — порядок в `small --help`: от создания приложения к его удалению.
"""

from . import access, dashboard, db, deploy, destroy, domain, env, init, link, logs, mail, notify, sleep

ALL = (init, deploy, link, access, mail, notify, env, db, domain, sleep, logs, dashboard, destroy)
