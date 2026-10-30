"""Remote memory client for Agents; final answers remain with the calling Agent."""
import json
import urllib.request


class MemoryClient:
    def __init__(self, base_url, token, user, timeout_s=60):
        self.base_url, self.token, self.user, self.timeout_s = base_url.rstrip("/"), token, user, timeout_s

    def _post(self, path, **body):
        body["user"] = self.user
        req = urllib.request.Request(self.base_url + "/memsys/" + path,
                json.dumps(body, ensure_ascii=False).encode(),
                {"Content-Type": "application/json", "X-Memsys-Token": self.token})
        with urllib.request.urlopen(req, timeout=self.timeout_s) as response:
            return json.load(response)

    def open_session(self, external_id=None):
        return self._post("sessions", external_id=external_id)["session_id"]

    def add_turn(self, session_id, role, content, idempotency_key):
        """Supply a stable event ID and reuse it on network retries."""
        return self._post("turns", session_id=session_id,
                          turns=[dict(role=role, content=content, idempotency_key=idempotency_key)])

    def recall(self, query, budget=None, coverage_budget=None):
        return self._post("recall", query=query, budget=budget, coverage_budget=coverage_budget)

    def recall_pages(self, query):
        return self._post("recall-pages", query=query)

    def end_session(self, session_id):
        return self._post("end", session_id=session_id)

    def history(self, key):
        return self._post("history", key=key)

    def sources(self, keys):
        return self._post("sources", keys=list(keys))

    def forget(self, key):
        return self._post("forget", key=key)

    def retry_job(self, job_key):
        """Retry a failed write explicitly after resolving its reported error."""
        return self._post("retry", job_key=job_key)
