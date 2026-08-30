# Contributing

Contributions should preserve the strict separation between this public web package and private corpus preparation systems.

1. Create a focused branch and keep changes minimal.
2. Do not add real corpus excerpts, databases, PDFs, page images, secrets, or machine-specific paths.
3. Do not add imports or dependencies on another repository or an ingestion pipeline.
4. Update the deterministic synthetic fixture when a schema-contract change is necessary.
5. Install development dependencies with `python -m pip install -e '.[dev]'` and run `pytest`.
6. Run `python tests/create_fixture.py /tmp/qian-wenku-fixture` and `qian-wenku-preflight --artifact-dir /tmp/qian-wenku-fixture` for artifact changes.

By contributing code, you agree that it may be distributed under the MIT license. Corpus data remains separately licensed and must not be submitted here.
