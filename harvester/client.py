"""来源客户端：进程内直连与本地 HTTP 两种实现，接口一致。"""
import json
import urllib.error
import urllib.parse
import urllib.request


class SourceError(RuntimeError):
    """来源返回了协议之外的错误（非 200/410/422 的 4xx/5xx 等）。"""


class SessionExpired(RuntimeError):
    """来源以 410 提示 revision 失效，当前轮必须停止。"""

    def __init__(self, revision, message="来源会话已过期"):
        super().__init__(f"{message}: {revision}")
        self.revision = revision


class DirectClient:
    """直接持有 :class:`source_site.source.Source` 的进程内客户端。"""

    def __init__(self, source):
        self.source = source

    def open_session(self):
        status, _, payload = self.source.request("GET", "/session")
        if status != 200:
            raise SourceError(f"开启会话失败: HTTP {status} {payload}")
        return payload["revision"]

    def query(self, revision, cell):
        query = {
            "revision": [revision],
            "bbox": [cell.bbox_param()],
            "time": [cell.time_param()],
        }
        status, _, payload = self.source.request("GET", "/points", query)
        if status == 410:
            raise SessionExpired(revision, payload.get("error", "来源会话已过期"))
        if status != 200:
            raise SourceError(f"查询失败: HTTP {status} {payload}")
        if payload.get("revision") != revision:
            # 来源不应在 200 中偷换 revision；一旦发生按协议错误处理，拒绝混用。
            raise SourceError("来源返回的 revision 与会话不一致，拒绝混用")
        return payload


class HttpClient:
    """本地 HTTP 来源客户端，只访问 127.0.0.1 的合成服务。"""

    def __init__(self, base_url, timeout=10):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _get(self, path, params=None):
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                payload = {"error": str(exc)}
            return exc.code, payload

    def open_session(self):
        status, payload = self._get("/session")
        if status != 200:
            raise SourceError(f"开启会话失败: HTTP {status} {payload}")
        return payload["revision"]

    def query(self, revision, cell):
        status, payload = self._get("/points", {
            "revision": revision,
            "bbox": cell.bbox_param(),
            "time": cell.time_param(),
        })
        if status == 410:
            raise SessionExpired(revision, payload.get("error", "来源会话已过期"))
        if status != 200:
            raise SourceError(f"查询失败: HTTP {status} {payload}")
        if payload.get("revision") != revision:
            raise SourceError("来源返回的 revision 与会话不一致，拒绝混用")
        return payload
