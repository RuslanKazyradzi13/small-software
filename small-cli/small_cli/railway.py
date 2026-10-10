"""Тонкий клиент Railway: GraphQL API v2 и загрузка исходников (тот же эндпоинт, что у `railway up`)."""

import json
import os
import urllib.error
import urllib.parse
import urllib.request

from . import __version__

DEFAULT_API_URL = "https://backboard.railway.com"
TOKEN_ENV_VARS = ("RAILWAY_API_TOKEN", "RAILWAY_TOKEN")


class RailwayError(Exception):
    pass


def token_from_env():
    return next((os.environ[v] for v in TOKEN_ENV_VARS if os.environ.get(v)), None)


class Railway:
    def __init__(self, token, base_url=None):
        self.token = token
        self.base_url = (base_url or os.environ.get("SMALL_API_URL") or DEFAULT_API_URL).rstrip("/")

    # --- транспорт ---

    def _post(self, url, body, content_type, timeout=60):
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "Authorization": "Bearer " + self.token,
            "Content-Type": content_type,
            "Accept": "application/json",
            "User-Agent": "small-cli/" + __version__,
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            raise RailwayError("HTTP %d: %s" % (e.code, _error_text(e.read()))) from None
        except OSError as e:  # URLError, таймауты, разрыв соединения
            raise RailwayError("нет связи с %s: %s" % (self.base_url, getattr(e, "reason", e))) from None
        try:
            return json.loads(raw or b"{}")
        except ValueError:
            raise RailwayError("неожиданный ответ сервера: %s" % _error_text(raw)) from None

    def graphql(self, query, variables=None, timeout=60):
        body = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
        res = self._post(self.base_url + "/graphql/v2", body, "application/json", timeout=timeout)
        if res.get("errors"):
            raise RailwayError("; ".join(e.get("message", "?") for e in res["errors"]))
        return res.get("data") or {}

    # --- операции ---

    def workspaces(self):
        """Работает только с токеном аккаунта; токен workspace получит ошибку."""
        data = self.graphql("query { me { workspaces { id name } } }")
        return data["me"]["workspaces"]

    def create_project(self, name, workspace_id=None):
        inp = {"name": name}
        if workspace_id:
            inp["workspaceId"] = workspace_id
        data = self.graphql(
            """mutation($input: ProjectCreateInput!) {
                 projectCreate(input: $input) { id environments { edges { node { id name } } } }
               }""",
            {"input": inp},
        )
        project = data["projectCreate"]
        envs = [edge["node"] for edge in project["environments"]["edges"]]
        if not envs:
            raise RailwayError("у созданного проекта нет окружений")
        env = next((e for e in envs if e["name"] == "production"), envs[0])
        return project["id"], env["id"]

    def find_projects(self, name, workspace_id=None):
        """Неудалённые проекты с точно таким именем, вместе с окружениями и сервисами."""
        data = self.graphql(
            """query($workspaceId: String) {
                 projects(workspaceId: $workspaceId, first: 500) { edges { node {
                   id name deletedAt
                   environments { edges { node { id name } } }
                   services { edges { node { id name } } }
                 } } }
               }""",
            {"workspaceId": workspace_id},
        )
        found = []
        for edge in data["projects"]["edges"]:
            node = edge["node"]
            if node["name"] == name and not node.get("deletedAt"):
                found.append({
                    "id": node["id"],
                    "environments": [e["node"] for e in node["environments"]["edges"]],
                    "services": [s["node"] for s in node["services"]["edges"]],
                })
        return found

    def create_service(self, project_id, environment_id, name):
        data = self.graphql(
            "mutation($input: ServiceCreateInput!) { serviceCreate(input: $input) { id } }",
            {"input": {"projectId": project_id, "environmentId": environment_id, "name": name}},
        )
        return data["serviceCreate"]["id"]

    def upsert_variables(self, project_id, environment_id, service_id, variables):
        """Добавляет/обновляет переменные сервиса одним запросом. Остальные переменные не трогает,
        повторный деплой не запускает: их подхватит следующая сборка."""
        self.graphql(
            "mutation($input: VariableCollectionUpsertInput!) { variableCollectionUpsert(input: $input) }",
            {"input": {"projectId": project_id, "environmentId": environment_id, "serviceId": service_id,
                       "variables": variables, "replace": False, "skipDeploys": True}},
        )

    def upload(self, project_id, environment_id, service_id, tarball, message=None):
        """Загружает .tar.gz с кодом и запускает сборку. Возвращает {deploymentId, url, logsUrl, ...}."""
        params = {"serviceId": service_id}
        if message:
            params["message"] = message
        url = "%s/project/%s/environment/%s/up?%s" % (
            self.base_url, project_id, environment_id, urllib.parse.urlencode(params))
        res = self._post(url, tarball, "application/gzip", timeout=600)
        if not res.get("deploymentId"):
            raise RailwayError("Railway не вернул deploymentId: %s" % res)
        return res

    def create_domain(self, service_id, environment_id, port=None):
        inp = {"serviceId": service_id, "environmentId": environment_id}
        if port:
            inp["targetPort"] = port
        data = self.graphql(
            "mutation($input: ServiceDomainCreateInput!) { serviceDomainCreate(input: $input) { id domain } }",
            {"input": inp},
        )
        domain = data["serviceDomainCreate"]
        return domain["id"], domain["domain"]

    def domain_available(self, domain):
        data = self.graphql(
            "query($domain: String!) { serviceDomainAvailable(domain: $domain) { available } }",
            {"domain": domain},
        )
        return bool(data["serviceDomainAvailable"]["available"])

    def rename_domain(self, domain_id, service_id, environment_id, domain, port=None):
        inp = {"serviceDomainId": domain_id, "serviceId": service_id,
               "environmentId": environment_id, "domain": domain}
        if port:
            inp["targetPort"] = port
        self.graphql("mutation($input: ServiceDomainUpdateInput!) { serviceDomainUpdate(input: $input) }",
                     {"input": inp})

    def service_domains(self, project_id, environment_id, service_id):
        data = self.graphql(
            """query($p: String!, $e: String!, $s: String!) {
                 domains(projectId: $p, environmentId: $e, serviceId: $s) { serviceDomains { id domain } }
               }""",
            {"p": project_id, "e": environment_id, "s": service_id},
        )
        return data["domains"]["serviceDomains"]

    def service_domain(self, project_id, environment_id, service_id, domain_id):
        for d in self.service_domains(project_id, environment_id, service_id):
            if d["id"] == domain_id:
                return d["domain"]
        return None

    def latest_deployment(self, service_id, environment_id, timeout=60):
        """{"id", "status", "createdAt"} последнего деплоя сервиса или None, если деплоев не было."""
        data = self.graphql(
            """query($serviceId: String!, $environmentId: String!) {
                 serviceInstance(serviceId: $serviceId, environmentId: $environmentId) {
                   latestDeployment { id status createdAt }
                 }
               }""",
            {"serviceId": service_id, "environmentId": environment_id},
            timeout=timeout,
        )
        return data["serviceInstance"]["latestDeployment"]

    def deployment_status(self, deployment_id):
        data = self.graphql("query($id: String!) { deployment(id: $id) { status } }", {"id": deployment_id})
        return data["deployment"]["status"]

    def logs(self, deployment_id, build=True, limit=40):
        field = "buildLogs" if build else "deploymentLogs"
        data = self.graphql(
            "query($id: String!, $limit: Int) { %s(deploymentId: $id, limit: $limit) { message } }" % field,
            {"id": deployment_id, "limit": limit},
        )
        return [entry["message"] for entry in data[field]]


def _error_text(raw):
    text = raw.decode("utf-8", "replace").strip() if raw else ""
    try:
        payload = json.loads(text)
    except ValueError:
        return text[:300] or "(пустой ответ)"
    if isinstance(payload, dict):
        if payload.get("message"):
            return payload["message"]
        if payload.get("errors"):
            return "; ".join(e.get("message", "?") for e in payload["errors"])
    return text[:300]
