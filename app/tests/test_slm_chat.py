import json

import app.api.model as model_api


def test_slm_chat_maps_reply_and_needs_an_endpoint(client, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "LLM_SLM_ENDPOINT", "")
    assert client.post("/api/model/slm-chat", json={"message": "x"}).status_code == 503

    monkeypatch.setattr(get_settings(), "LLM_SLM_ENDPOINT", "http://slm/v1/chat/completions")
    seen = {}

    async def fake(endpoint, body, key):
        seen["user"] = body["messages"][1]["content"]
        reply = {"news_summary": "h", "event_type": "ORDER_WIN", "reason": "r", "sentiment": "POSITIVE",
                 "sentiment_score": 0.9, "facts": [], "confidence": 0.9, "recommendation": "BUY"}
        return {"choices": [{"message": {"content": json.dumps(reply)}}]}

    monkeypatch.setattr(model_api, "_slm_post", fake)
    r = client.post("/api/model/slm-chat", json={"symbol": "abc", "message": "Order win\nRs 845 crore LoA"})
    assert r.status_code == 200 and r.json()["mapped"]["recommendation"] == "BUY"
    assert seen["user"].startswith("SYMBOL: ABC\nFILED: ") and "HEADLINE: Order win\n" in seen["user"]
    assert seen["user"].endswith("FILING:\nRs 845 crore LoA")
