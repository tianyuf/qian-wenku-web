import json


def test_search_bounds_and_results(client):
    low = client.get("/api/search/?q=合成&limit=-20&offset=-9").get_json()
    assert low["limit"] == 1
    assert low["offset"] == 0
    assert len(low["results"]) == 1

    high = client.get("/api/search/?q=合成&limit=999999&offset=999999999").get_json()
    assert high["limit"] == 500
    assert high["offset"] == 1_000_000
    assert high["results"] == []

    invalid = client.get("/api/search/?q=合成&limit=nope&offset=nope").get_json()
    assert invalid["limit"] == 100
    assert invalid["offset"] == 0


def test_public_json_omits_internal_pdf_paths(client):
    routes = [
        "/api/search/?q=合成",
        "/api/browse/year/1911?limit=-1",
        "/api/browse/entry/1",
        "/api/meta/persons",
        "/api/meta/source/1",
        "/api/meta/stats",
    ]
    for route in routes:
        response = client.get(route)
        assert response.status_code == 200, route
        payload = response.get_json()
        assert "pdf_path" not in json.dumps(payload), route
        assert "private/" not in json.dumps(payload), route

    browse = client.get("/api/browse/year/1911?limit=-1").get_json()
    assert browse["count"] == 1
    assert browse["entries"][0]["source"]["pdf_available"] is True


def test_pdf_and_metadata_api_routes(client):
    url_response = client.get("/api/pdf/1/url/1")
    assert url_response.status_code == 200
    assert url_response.get_json()["url"].endswith(".jpg")
    assert client.get("/api/pdf/1/page/1").status_code == 302
    assert client.get("/api/pdf/99/url/1").status_code == 404
    assert client.get("/api/browse/years").get_json()["years"]
