# qian-wenku-web

Standalone, read-only Flask application for searching and browsing prepared Qian Xuesen corpus artifacts. The interface includes nianpu, wenji, shuxin, recipient, entity, date, duplicate, citation, and page-image views without an asset build step.

## Data boundary

This repository contains application code and a tiny synthetic test fixture only. Production corpus databases, OCR text, PDFs, and page images are not included and are separately licensed. The MIT license in this repository applies to the code, not to corpus data or source documents.

The web process accepts one prepared artifact directory containing exactly this contract:

```text
artifacts/
├── corpus.db
├── manifest.json
└── page_images.json
```

`manifest.json` uses schema version `1` and contains:

```json
{
  "schema_version": 1,
  "generated_at": "2026-01-01T00:00:00Z",
  "counts": {
    "sources": 4,
    "entries": 5,
    "recipients": 2,
    "entities": 2,
    "page_images": 7
  },
  "files": {
    "corpus.db": {
      "sha256": "SHA-256 hex digest",
      "bytes": 12345
    },
    "page_images.json": {
      "sha256": "SHA-256 hex digest",
      "bytes": 678
    }
  }
}
```

`page_images.json` maps source IDs and positive page numbers to 10-character lowercase hexadecimal page-image IDs generated from truncated SHA-256 digests. The database must identify schema version `1` through both `PRAGMA user_version` and `schema_info`, contain the tables and columns checked by `qian_wenku_web.artifacts`, have non-empty sources and entries, and provide a populated permalink for every entry. The app opens SQLite with URI `mode=ro` and `PRAGMA query_only=ON`; it has no import, migration, schema creation, or ingestion code.

Validate an artifact before starting the service:

```bash
qian-wenku-preflight --artifact-dir /opt/qian-wenku-web/artifacts
```

## Development

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
pytest
python tests/create_fixture.py artifacts
flask --app qian_wenku_web.wsgi run
```

Configuration is through `WENKU_ARTIFACT_DIR`, `R2_CDN_BASE`, and `R2_PREFIX`; see `.env.example`. Environment files are not loaded by the package itself.

For a private beta, set `BETA_PASSPHRASE`, a long random `SECRET_KEY`, and `SESSION_COOKIE_SECURE=true` in the deployment environment. The passphrase is never stored in this repository. When `BETA_PASSPHRASE` is empty, login protection is disabled.

## Production

The checked-in examples deploy at the root domain `https://wenku.qianxuesen.org/`:

- `deploy/systemd/qian-wenku-web.service`
- `deploy/nginx/wenku.qianxuesen.org.conf`
- `deploy/nginx/www.qianxuesen.org.conf`
- `deploy/www/` static landing page for `https://www.qianxuesen.org/`

Install the package and virtual environment under `/opt/qian-wenku-web`, place the prepared artifact at `/opt/qian-wenku-web/artifacts`, and install only the web service and nginx site. There are no backup or ingestion units in this repository.

## Routes

- `/` and `/search/results`: search UI and HTMX results
- `/browse`, `/browse/year/<year>`, `/date/`: corpus browsing
- `/e/<permalink>`: canonical entry page
- `/browse/recipients`, `/browse/entities`: relationship directories
- `/api/search/`, `/api/browse/`, `/api/meta/`, `/api/pdf/`: JSON APIs
- `/health`: lightweight process and database health check
