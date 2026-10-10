"""small mcp — MCP-сервер: ИИ-агент (Claude Code, Cursor и другие) деплоит и расшаривает приложения сам."""

from .. import mcp as server
from ..i18n import tr


def register(sub):
    p = sub.add_parser(
        "mcp", help=tr("MCP server for AI agents: deploy and share apps from Claude Code, Cursor...",
                       "MCP-сервер для ИИ-агентов: деплой и доступ из Claude Code, Cursor..."),
        description=tr("Run the MCP server on stdin/stdout. An AI agent can then create apps, deploy them and share "
                       "them by email. Add it to Claude Code: claude mcp add small -- small mcp. Cursor and other "
                       "clients: command \"small\", args [\"mcp\"]. RAILWAY_API_TOKEN must be set in the environment "
                       "the agent starts from.",
                       "Запускает MCP-сервер на stdin/stdout. ИИ-агент сможет создавать приложения, деплоить их и "
                       "выдавать доступ по почте. Claude Code: claude mcp add small -- small mcp. Cursor и другие "
                       "клиенты: команда \"small\", аргументы [\"mcp\"]. RAILWAY_API_TOKEN должен быть в окружении, "
                       "из которого запускается агент."))
    p.set_defaults(func=cmd_mcp)


def cmd_mcp(args):
    return server.serve()
