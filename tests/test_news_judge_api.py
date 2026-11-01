"""Gemini 호출 시 API 키가 URL 에 들어가지 않는지 확인(오류 메시지·로그로 새지 않게)."""
from news_judge import judge


def test_api_key_sent_in_header_not_url(monkeypatch):
    seen = {}

    class Resp:
        status_code = 200

        def json(self):
            return {"candidates": [{"content": {"parts": [{"text": "[]"}]}}], "usageMetadata": {}}

    def fake_post(url, headers=None, json=None, timeout=None):
        seen["url"], seen["headers"] = url, headers
        return Resp()

    monkeypatch.setattr(judge.requests, "post", fake_post)
    judge.call_gemini_api("p", "gemini-3.8-flash", "SECRET_KEY")
    assert "SECRET_KEY" not in seen["url"]
    assert seen["headers"]["x-goog-api-key"] == "SECRET_KEY"
