"""合成来源行为，可直接调用或通过本地 HTTP 访问。"""
import hashlib
import json
import threading
from pathlib import Path
from urllib.parse import parse_qs

DATA = Path(__file__).resolve().parents[1] / "sources"


def json_file(name):
    return json.loads((DATA / name).read_text(encoding="utf-8"))


class Source:
    def __init__(self):
        self.lock = threading.RLock()
        self.calls = []
        self.phase = "base"
        self.state = {}

    def request(self, method, path, query=None, headers=None, body=b""):
        query, headers = query or {}, headers or {}
        self.calls.append({"method": method, "path": path, "query": query})
        if path == "/__control" and method == "POST":
            control = json.loads(body or b"{}")
            self.phase = control.get("phase", self.phase)
            self.state.update(control.get("state", {}))
            return 200, {}, {"phase": self.phase}
        if path == "/__calls":
            return 200, {}, self.calls[:-1]
        return self.dispatch(method, path, query, headers, body)

    def dispatch(self, method, path, q, h, body):
        if path == "/session":
            revision = "r" + str(self.state.get("generation", 1))
            return 200, {}, {"revision": revision}
        if path != "/points":
            return 404, {}, {"error": "接口不存在"}
        if self.phase == "expired" or q.get("revision") != ["r" + str(self.state.get("generation", 1))]:
            return 410, {}, {"error": "来源会话已过期"}
        x0,y0,x1,y1 = map(int, q["bbox"][0].split(","))
        t0,t1 = map(int, q["time"][0].split(","))
        if x0>x1 or y0>y1 or t0>=t1:
            return 422, {}, {"error": "范围为空"}
        records = [r for r in json_file("points.json") if x0<=r["x"]<=x1 and y0<=r["y"]<=y1 and t0<=r["updated_at"]<t1]
        records.sort(key=lambda r:r["source_id"])
        return 200, {}, {"count": len(records), "truncated": len(records)>3, "revision": q["revision"][0], "records": records[:3]}
