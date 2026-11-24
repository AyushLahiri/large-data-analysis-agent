"""Internal transport for the analysis backend. One session object per live app session."""
import json
import os
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen
from uuid import uuid4


class SearchSession:
    def __init__(self, session_id=None):
        self.session_id = session_id or uuid4().hex
        self._closed = False
        self._scope = None

    def _request(self, method, path, project_id, chat_id, body=None, timeout=135):
        if self._closed:
            raise RuntimeError("This search session has ended.")
        token = os.environ.get("V5_ANALYSIS_SEARCH_TOKEN")
        if not token:
            token_file = Path(os.environ["LOCALAPPDATA"]) / "AtlasV5" / "Search" / "analysis.token"
            token = token_file.read_text(encoding="utf-8").strip()
        self._scope = (project_id, chat_id)
        request = Request("http://127.0.0.1:8767" + path, method=method,
                          data=None if body is None else json.dumps(body).encode(),
                          headers={"Authorization": "Bearer " + token, "Content-Type": "application/json",
                                   "X-Session-Id": self.session_id, "X-Project-Id": project_id, "X-Chat-Id": chat_id})
        try:
            with urlopen(request, timeout=timeout) as response:
                return None if response.status == 204 else json.load(response)
        except HTTPError as error:
            raise RuntimeError(f"Search service {error.code}: {error.read().decode(errors='replace')}") from error

    def search(self, project_id, chat_id, question, assets=()):
        return self._request("POST", "/v1/search", project_id, chat_id, {"question": question, "assets": list(assets)})

    def reply(self, project_id, chat_id, search_id, answer="", assets=()):
        return self._request("POST", "/v1/search/" + quote(search_id, safe=""), project_id, chat_id,
                             {"answer": answer, "assets": list(assets)})

    def status(self, project_id, chat_id, search_id):
        return self._request("GET", "/v1/search/" + quote(search_id, safe=""), project_id, chat_id)

    def acknowledge_delivery(self, project_id, chat_id, search_id):
        return self._request("POST", "/v1/search/" + quote(search_id, safe="") + "/delivered", project_id,
                             chat_id, {"recipient": "analysis_agent"}, timeout=600)

    def close(self):
        if self._scope and not self._closed:
            self._request("DELETE", "/v1/session", *self._scope)
        self._closed = True

