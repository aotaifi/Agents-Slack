import re
from pathlib import Path

from fastapi.testclient import TestClient

from agent_commons.main import create_app

KATEX = Path(__file__).parent.parent / "src/agent_commons/static/vendor/katex"


def test_katex_assets_are_vendored_and_served_locally(tmp_path):
    with TestClient(create_app(f"sqlite:///{tmp_path}/t.db")) as client:
        page = client.get("/").text
        assert "/static/vendor/katex/katex.min.js" in page
        assert "/static/vendor/katex/katex.min.css" in page
        assert not re.search(r"""(?:src|href)=["']https?://""", page)
        assert client.get("/static/vendor/katex/katex.min.js").status_code == 200
        css = client.get("/static/vendor/katex/katex.min.css")
        assert css.status_code == 200
        fonts = re.findall(r"url\((fonts/[^)]+)\)", css.text)
        assert fonts and all(f.endswith(".woff2") for f in fonts)
        for font in fonts:
            response = client.get(f"/static/vendor/katex/{font}")
            assert response.status_code == 200 and response.content[:4] == b"wOF2"
        assert (KATEX / "LICENSE").read_text().startswith("The MIT License")
