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


# A valid 1x1 progressive (SOF2) grayscale JPEG, used as a stand-in page scan.
TINY_JPEG = __import__("base64").b64decode(
    "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAP//////////////////////////////////////"
    "////////////////////////////////////////////////////////////wgALCAABAAEB"
    "AREA/8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPxA="
)


def test_jpeg_info_and_pdf_builder():
    from qian_wenku_web.pdfbuild import InvalidJpegError, PdfStreamBuilder, jpeg_info
    import pytest

    assert jpeg_info(TINY_JPEG) == (1, 1, 1)
    with pytest.raises(InvalidJpegError):
        jpeg_info(b"not a jpeg")

    builder = PdfStreamBuilder()
    chunks = list(builder.begin())
    chunks += list(builder.add_jpeg_page(TINY_JPEG))
    chunks += list(builder.add_notice_page("Page 2 could not be retrieved."))
    chunks += list(builder.finish(title="测试文档", author="qianxuesen.org"))
    pdf = b"".join(chunks)

    assert pdf.startswith(b"%PDF-1.4\n")
    assert pdf.rstrip().endswith(b"%%EOF")
    assert pdf.count(b"/Type /Page ") == 2
    assert b"/Count 2" in pdf
    assert b"/DCTDecode" in pdf and TINY_JPEG in pdf
    # startxref must point at the xref table, and the xref must index every object.
    startxref = int(pdf[pdf.rindex(b"startxref") + len(b"startxref"):].split()[0])
    assert pdf[startxref:startxref + 4] == b"xref"
    size = int(pdf[pdf.rindex(b"/Size") + 5:].split()[0])
    for obj_id in range(1, size):
        marker = b"%d 0 obj" % obj_id
        offset = pdf.index(marker)
        line = pdf[pdf.index(b"xref"):].split(b"\n")[2 + obj_id]
        assert int(line.split()[0]) == offset, obj_id
    # CJK title survives as UTF-16BE hex.
    assert ("﻿测试文档".encode("utf-16-be")).hex().encode() in pdf


def test_entry_pdf_download(client, monkeypatch):
    from qian_wenku_web.api import pdf as pdf_api

    fetched = []

    def fake_fetch(url):
        fetched.append(url)
        return TINY_JPEG

    monkeypatch.setattr(pdf_api, "fetch_image", fake_fetch)

    response = client.get("/api/pdf/entry/1.pdf")
    assert response.status_code == 200
    assert response.content_type == "application/pdf"
    disposition = response.headers["Content-Disposition"]
    assert disposition.startswith('attachment; filename="entry-1.pdf"')
    assert "filename*=UTF-8''" in disposition
    assert response.headers["Cache-Control"] == "private, max-age=86400"
    body = response.get_data()
    assert body.startswith(b"%PDF-1.4")
    assert body.count(b"/Type /Page ") == 1
    assert len(fetched) == 1 and fetched[0].endswith(".jpg")

    # Unknown entry, and an entry whose source has no scans.
    assert client.get("/api/pdf/entry/999.pdf").status_code == 404
    assert client.get("/api/pdf/entry/5.pdf").status_code == 404

    # CDN outage on the first page is a clean error, not an empty file.
    def failing_fetch(url):
        raise pdf_api.PageImageError("boom")

    monkeypatch.setattr(pdf_api, "fetch_image", failing_fetch)
    outage = client.get("/api/pdf/entry/1.pdf")
    assert outage.status_code == 502
    assert outage.get_json()["error"]


def test_entry_pdf_substitutes_notice_for_failed_later_pages(client, monkeypatch):
    from qian_wenku_web.api import pdf as pdf_api

    # Pretend entry 1 spans pages 1-3 of source 1 (fixture maps 3 pages).
    monkeypatch.setattr(pdf_api, "_entry_pages", lambda row: [1, 2, 3])
    calls = {"n": 0}

    def flaky_fetch(url):
        calls["n"] += 1
        if calls["n"] == 2:
            raise pdf_api.PageImageError("transient")
        return TINY_JPEG

    monkeypatch.setattr(pdf_api, "fetch_image", flaky_fetch)
    body = client.get("/api/pdf/entry/1.pdf").get_data()
    assert body.count(b"/Type /Page ") == 3
    assert b"Page 2 could not be retrieved" in body
    assert b"/Count 3" in body

    # The page cap returns 413 instead of attempting a huge download.
    monkeypatch.setattr(pdf_api, "_entry_pages", lambda row: list(range(1, 300)))
    assert client.get("/api/pdf/entry/1.pdf").status_code == 413
