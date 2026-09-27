import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import support  # noqa: E402

DBNAME = "nexus_test_cfg_back"


@pytest.fixture(scope="session")
def env():
    if not support.docker_available():
        pytest.skip("Docker no disponible")
    support.recreate_config_db(DBNAME)
    # latest_version: la API de instalaciones marca "outdated" a las versiones menores (fase 5).
    b = support.Backend(DBNAME, extra_ini={"agent": {"latest_version": "5.2.0"}})
    b.start()
    try:
        ids = support.seed_config(b)
        yield {"backend": b, "ids": ids, "db": DBNAME}
    finally:
        b.stop()
        support.drop_config_db(DBNAME)


@pytest.fixture()
def db(env):
    conn = support.cfg_conn(env["db"])
    yield conn
    conn.close()
