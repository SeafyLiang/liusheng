import importlib.util
from pathlib import Path

import pytest


def test_initialization_preserves_existing_configuration(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('init_deploy', Path(__file__).resolve().parents[1] / 'scripts/init-deploy.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.os, 'getuid', lambda: 1000, raising=False)
    monkeypatch.setattr(module.os, 'getgid', lambda: 1000, raising=False)
    module.initialize(tmp_path)
    before = (tmp_path / '.env').read_bytes()
    assert (tmp_path / '.env').stat().st_mode & 0o777 == 0o600
    assert all((tmp_path / name).is_dir() for name in ('data', 'music', 'inbox'))
    with pytest.raises(SystemExit, match='已存在'):
        module.initialize(tmp_path)
    assert (tmp_path / '.env').read_bytes() == before
