"""Run source tests with the same imports as `python app/story_tts_server.py`."""
from pathlib import Path
import sys
from urllib.parse import urlparse

import pytest
import requests

import os

# Never read a developer's app/.env (real, paid provider keys) during tests.
os.environ['PAPERBOOK_SKIP_DOTENV'] = '1'

APP = Path(__file__).resolve().parents[1] / 'app'
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))


@pytest.fixture(autouse=True)
def local_network_only(monkeypatch):
    """Tests may use controlled loopback services, never paid external providers."""
    original = requests.sessions.Session.request

    def request(session, method, url, *args, **kwargs):
        if urlparse(str(url)).hostname not in {'localhost', '127.0.0.1', '::1'}:
            raise AssertionError('External network is disabled during local validation')
        return original(session, method, url, *args, **kwargs)

    monkeypatch.setattr(requests.sessions.Session, 'request', request)
