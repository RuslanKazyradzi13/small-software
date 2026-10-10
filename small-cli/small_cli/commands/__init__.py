"""Команды small. Каждый модуль добавляет свои подкоманды функцией register(subparsers).

Порядок здесь — порядок в `small --help`: от создания приложения к его удалению.
"""

from . import access, dashboard, deploy, destroy, env, init, link, logs

ALL = (init, deploy, link, access, env, logs, dashboard, destroy)
