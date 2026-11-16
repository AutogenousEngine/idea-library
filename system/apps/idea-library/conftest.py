"""Every Idea Library test reads a scratch copy of the synthetic library in testdata/, never the user's own."""

import shutil
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from idea_library import runner
from werkzeug.serving import make_server

TESTDATA = Path(__file__).parent / "testdata"


@pytest.fixture
def library_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The app's data folder, holding a copy of the synthetic library."""
    library = tmp_path / "library"
    shutil.copytree(TESTDATA, library)
    monkeypatch.setattr(runner, "DATA_DIR", library)
    return library


@pytest.fixture
def library_url(library_dir: Path) -> Iterator[str]:
    """A throwaway server of the app over the synthetic library, on a free port."""
    server = make_server("127.0.0.1", 0, runner.app, threaded=True)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    thread.join()
