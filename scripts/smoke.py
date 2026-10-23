"""Small HTTP smoke check against an already running local server."""
import httpx
from app.config import settings


def main():
    with httpx.Client(base_url="http://127.0.0.1:8000", timeout=15) as client:
        page = client.get("/")
        page.raise_for_status()
        assert "relay" in page.text
        config = client.get("/api/config")
        config.raise_for_status()
        login = client.post("/api/auth/login", json={"email": "admin@demo.local", "password": settings.demo_password})
        login.raise_for_status()
        client.headers["Authorization"] = "Bearer " + login.json()["token"]
        profile = client.get("/api/me")
        profile.raise_for_status()
        assert profile.json()["role"] == "admin"
        overview = client.get("/api/admin/overview")
        overview.raise_for_status()
        assert "kpis" in overview.json()
        docs = client.get("/api/openapi.json")
        docs.raise_for_status()
    print("HTTP smoke passed: UI, config, login, me, overview, API schema.")


if __name__ == "__main__":
    main()
