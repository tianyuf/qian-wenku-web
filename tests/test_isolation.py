from pathlib import Path


def test_package_has_no_old_pipeline_dependency():
    package = Path(__file__).parents[1] / "src" / "qian_wenku_web"
    source = "\n".join(
        path.read_text(encoding="utf-8") for path in package.rglob("*.py")
    )
    assert "src.pipeline" not in source
    assert "from pipeline" not in source
    assert "import pipeline" not in source
    assert not (package / "parse_dates.py").exists()
    assert not (package / "templates" / "network.html").exists()
