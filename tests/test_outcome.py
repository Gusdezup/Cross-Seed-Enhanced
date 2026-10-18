from app.worker import outcome


def test_outcomes():
    assert outcome({"status": "pending"}) == "live"
    assert outcome({"status": "timeout"}) == "bad"
    assert outcome({"status": "done", "result": {"routed": ["C411"], "injected": ["C411"], "failed": ["X"]}}) == "hit"
    assert outcome({"status": "done", "result": {"routed": ["C411"], "matched": ["C411"]}}) == "found"
    assert outcome({"status": "done", "result": {"routed": ["C411"], "candidates": 3}}) == "none"
    assert outcome({"status": "done", "result": {"found": 2, "injected": [], "failed": []}}) == "found"
