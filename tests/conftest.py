import os

# Los tests comprueban textos en español; el idioma se fija antes de importar la app.
os.environ["IROPT_LANG"] = "es"

import pytest


@pytest.fixture(autouse=True)
def _unlock_ini_writes(monkeypatch):
    """The app ships with untested ini writes locked; the older tests exercise the write path itself."""
    from iroptimizer import apply
    monkeypatch.setattr(apply, "WRITES_LOCKED", False)
