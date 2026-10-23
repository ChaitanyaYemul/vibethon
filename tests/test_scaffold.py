from fastapi.testclient import TestClient
from app.main import app


def test_static_entrypoint():
    response = TestClient(app).get("/")
    assert response.status_code == 200
    assert "Last-Mile Dispatch" in response.text
