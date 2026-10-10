"""Тонкий клиент Railway: GraphQL API v2 и загрузка исходников (тот же эндпоинт, что у `railway up`)."""

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from . import __version__
from .i18n import tr

DEFAULT_API_URL = "https://backboard.railway.com"
TOKEN_ENV_VARS = ("RAILWAY_API_TOKEN", "RAILWAY_TOKEN")
CUSTOM_DOMAIN_STATUS = ("dnsRecords { recordType hostlabel fqdn requiredValue currentValue status purpose } "
                        "verified verificationDnsHost verificationToken certificateStatus")


class RailwayError(Exception):
    pass


def token_from_env():
    """Токен Railway из окружения. Пробелы, кавычки и угловые скобки по краям отбрасываются: в токене их не бывает,
    а из заготовки вида "<токен>" их легко скопировать вместе со значением."""
    raw = next((os.environ[v] for v in TOKEN_ENV_VARS if os.environ.get(v)), "")
    return raw.strip().strip("\"'<>").strip() or None


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
            raise RailwayError(tr("cannot reach %s: %s", "нет связи с %s: %s")
                               % (self.base_url, getattr(e, "reason", e))) from None
        try:
            return json.loads(raw or b"{}")
        except ValueError:
            raise RailwayError(tr("unexpected server response: %s", "неожиданный ответ сервера: %s")
                               % _error_text(raw)) from None

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
            raise RailwayError(tr("the new project has no environments", "у созданного проекта нет окружений"))
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
            raise RailwayError(tr("Railway did not return a deploymentId: %s",
                                  "Railway не вернул deploymentId: %s") % res)
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

    def instance_status(self, service_id, environment_id, timeout=60):
        """{"sleep": включён ли режим сна, "deployment": последний деплой или None}."""
        data = self.graphql(
            """query($serviceId: String!, $environmentId: String!) {
                 serviceInstance(serviceId: $serviceId, environmentId: $environmentId) {
                   sleepApplication
                   latestDeployment { id status createdAt }
                 }
               }""",
            {"serviceId": service_id, "environmentId": environment_id},
            timeout=timeout,
        )
        instance = data["serviceInstance"]
        return {"sleep": bool(instance.get("sleepApplication")), "deployment": instance["latestDeployment"]}

    def update_instance(self, service_id, environment_id, **settings):
        """Настройки сервиса в окружении (sleepApplication, startCommand...). Вступают в силу со следующим деплоем."""
        self.graphql(
            "mutation($s: String!, $e: String, $input: ServiceInstanceUpdateInput!) "
            "{ serviceInstanceUpdate(serviceId: $s, environmentId: $e, input: $input) }",
            {"s": service_id, "e": environment_id, "input": settings},
        )

    def average_load(self, project_id, start, end, step_seconds=3600, timeout=60):
        """Средняя нагрузка проекта (все его сервисы, включая базы) за период: {"CPU_USAGE": ядра,
        "MEMORY_USAGE_GB": ГБ, "DISK_USAGE_GB": ГБ}.

        Берём metrics, а не usage: у metrics известные единицы (средние ядра и ГБ за интервал), а usage
        накапливается с задержкой. Для каждого сервиса среднее считается с его первого замера до конца периода,
        пропуски (сервис спал) — как ноль; затем средние сервисов складываются.
        """
        data = self.graphql(
            """query($p: String!, $start: DateTime!, $end: DateTime!, $step: Int!) {
                 metrics(projectId: $p, startDate: $start, endDate: $end, sampleRateSeconds: $step,
                         averagingWindowSeconds: $step, groupBy: [SERVICE_ID],
                         measurements: [CPU_USAGE, MEMORY_USAGE_GB, DISK_USAGE_GB]) {
                   measurement values { ts value }
                 }
               }""",
            {"p": project_id, "start": start, "end": end, "step": step_seconds},
            timeout=timeout,
        )
        end_ts = int(_parse_time(end))
        load = {}
        for series in data["metrics"]:
            values = series["values"]
            if not values:
                continue
            expected = max(1, (end_ts - min(v["ts"] for v in values)) // step_seconds + 1)
            average = sum(float(v["value"]) for v in values) / max(expected, len(values))
            load[series["measurement"]] = load.get(series["measurement"], 0.0) + average
        return load

    def template(self, code):
        data = self.graphql("query($code: String!) { template(code: $code) { id name serializedConfig } }",
                            {"code": code})
        return data["template"]

    def deploy_template(self, project_id, environment_id, template_id, config):
        data = self.graphql(
            """mutation($input: TemplateDeployV2Input!) { templateDeployV2(input: $input) { projectId workflowId } }""",
            {"input": {"projectId": project_id, "environmentId": environment_id, "templateId": template_id,
                       "serializedConfig": config}},
        )
        return data["templateDeployV2"].get("workflowId")

    def workflow_status(self, workflow_id):
        data = self.graphql("query($id: String!) { workflowStatus(workflowId: $id) { status error } }",
                            {"id": workflow_id})
        return data["workflowStatus"]

    def custom_domains(self, project_id, environment_id, service_id):
        data = self.graphql(
            """query($p: String!, $e: String!, $s: String!) {
                 domains(projectId: $p, environmentId: $e, serviceId: $s) {
                   customDomains { id domain targetPort status { %s } }
                 }
               }""" % CUSTOM_DOMAIN_STATUS,
            {"p": project_id, "e": environment_id, "s": service_id},
        )
        return data["domains"]["customDomains"]

    def create_custom_domain(self, project_id, environment_id, service_id, domain, port=None):
        inp = {"projectId": project_id, "environmentId": environment_id, "serviceId": service_id, "domain": domain}
        if port:
            inp["targetPort"] = port
        data = self.graphql(
            "mutation($input: CustomDomainCreateInput!) "
            "{ customDomainCreate(input: $input) { id domain targetPort status { %s } } }" % CUSTOM_DOMAIN_STATUS,
            {"input": inp},
        )
        return data["customDomainCreate"]

    def delete_custom_domain(self, domain_id):
        self.graphql("mutation($id: String!) { customDomainDelete(id: $id) }", {"id": domain_id})

    def deployment_status(self, deployment_id):
        data = self.graphql("query($id: String!) { deployment(id: $id) { status } }", {"id": deployment_id})
        return data["deployment"]["status"]

    def log_entries(self, deployment_id, build=False, limit=100, start_date=None):
        """Строки лога: [{"timestamp", "message", "severity"}]. start_date (ISO) — только строки начиная с него."""
        field = "buildLogs" if build else "deploymentLogs"
        data = self.graphql(
            "query($id: String!, $limit: Int, $startDate: DateTime) "
            "{ %s(deploymentId: $id, limit: $limit, startDate: $startDate) { timestamp message severity } }" % field,
            {"id": deployment_id, "limit": limit, "startDate": start_date},
        )
        return data[field]

    def logs(self, deployment_id, build=True, limit=40):
        return [entry["message"] for entry in self.log_entries(deployment_id, build=build, limit=limit)]

    def service_variables(self, project_id, environment_id, service_id, unrendered=False):
        """Переменные сервиса. unrendered=True — как записаны, без подстановки ссылок: `${{Postgres.DATABASE_URL}}`,
        а не готовый адрес с паролем."""
        ids = {"projectId": project_id, "environmentId": environment_id, "serviceId": service_id}
        if not unrendered:
            data = self.graphql(
                "query($projectId: String!, $environmentId: String!, $serviceId: String) "
                "{ variables(projectId: $projectId, environmentId: $environmentId, serviceId: $serviceId) }", ids)
        else:
            data = self.graphql(
                "query($projectId: String!, $environmentId: String!, $serviceId: String, $unrendered: Boolean) "
                "{ variables(projectId: $projectId, environmentId: $environmentId, serviceId: $serviceId, "
                "unrendered: $unrendered) }", dict(ids, unrendered=True))
        return data["variables"]

    def project_services(self, project_id):
        """(имя проекта, [{"id", "name"}] его сервисов)."""
        data = self.graphql(
            "query($id: String!) { project(id: $id) { name services { edges { node { id name } } } } }",
            {"id": project_id},
        )
        project = data["project"]
        return project["name"], [edge["node"] for edge in project["services"]["edges"]]

    def delete_variable(self, project_id, environment_id, service_id, name):
        self.graphql(
            "mutation($input: VariableDeleteInput!) { variableDelete(input: $input) }",
            {"input": {"projectId": project_id, "environmentId": environment_id, "serviceId": service_id,
                       "name": name}},
        )

    def redeploy(self, service_id, environment_id):
        """Перезапустить сервис с текущими переменными (последний образ пересобирать не нужно)."""
        self.graphql(
            "mutation($s: String!, $e: String!) { serviceInstanceRedeploy(serviceId: $s, environmentId: $e) }",
            {"s": service_id, "e": environment_id},
        )

    def delete_project(self, project_id):
        self.graphql("mutation($id: String!) { projectDelete(id: $id) }", {"id": project_id})

    def delete_service(self, service_id):
        self.graphql("mutation($id: String!) { serviceDelete(id: $id) }", {"id": service_id})


def _parse_time(iso):
    """'2026-10-10T12:00:00Z' -> unix-время."""
    return datetime.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()


def _error_text(raw):
    text = raw.decode("utf-8", "replace").strip() if raw else ""
    try:
        payload = json.loads(text)
    except ValueError:
        return text[:300] or tr("(empty response)", "(пустой ответ)")
    if isinstance(payload, dict):
        if payload.get("message"):
            return payload["message"]
        if payload.get("errors"):
            return "; ".join(e.get("message", "?") for e in payload["errors"])
    return text[:300]
