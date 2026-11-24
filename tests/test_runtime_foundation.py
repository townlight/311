from fastapi.testclient import TestClient

import townlight_311
from townlight_311.main import app

client = TestClient(app)


def test_package_version_is_011() -> None:
    assert townlight_311.__version__ == "0.2.0"


def test_root_endpoint_states_runtime_boundary() -> None:
    response = client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "Townlight 311"
    assert data["version"] == townlight_311.__version__
    assert "developer-only prototype" in data["status"]
    assert "is not released or tested-installed" in data["status"]
    assert "Windows" in data["message"]
    assert "candidate" in data["message"]
    assert "is not installed" in data["message"]
    assert "implemented Windows routes" in data["next_step"]
    assert "integration candidate" in data["next_step"]
    assert "not been released or tested as an installed Windows build" in data["next_step"]


def test_health_endpoint_reports_versions() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["service"] == "townlight_311"
    assert data["version"] == "0.2.0"
    assert data["townlight_core_version"] == "1.3.0.dev2"
