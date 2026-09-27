"""
Configuración 12-factor (nexus_config.py, DWH_README.md §23): variables NEXUS__<SECCION>__<CLAVE>
sobre config.ini (opcional), sin registrar valores; trusted_proxies con redes CIDR.
Pruebas unitarias: no requieren Docker ni BD.
"""

import os
import subprocess
import sys

import pytest

import nexus_config
from panel_auth import AuthSettings

BACK_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _write_ini(tmp_path, text: str) -> str:
    p = tmp_path / "config.ini"
    p.write_text(text, encoding="utf-8")
    return str(p)


def test_sin_archivo_solo_variables(tmp_path):
    env = {
        "NEXUS__DATABASE__HOST": "host.docker.internal",
        "NEXUS__DATABASE__PORT": "5432",
        "NEXUS__DATABASE__DB": "NexusDWH",
        "NEXUS__DATABASE__USER": "nexus",
        "NEXUS__DATABASE__PASSWORD": "s3cr%t;#pw",
        "NEXUS__DATABASE__POOL_MAX": "30",
        "NEXUS__CONNECTION_TEST__ENABLED": "false",
        "OTRA": "x",
    }
    ini, info = nexus_config.load_config(str(tmp_path / "no-existe.ini"), env)
    assert info["file_loaded"] is False
    assert ini.get("database", "host") == "host.docker.internal"
    assert ini.getint("database", "port") == 5432
    assert ini.getint("database", "pool_max") == 30
    # '%' y caracteres especiales llegan literales (interpolación de ConfigParser escapada).
    assert ini.get("database", "password") == "s3cr%t;#pw"
    assert ini.getboolean("connection_test", "enabled") is False
    assert "database.password" in info["applied"]
    assert "connection_test.enabled" in info["applied"]
    params = nexus_config.db_connect_params(ini)
    assert params["dbname"] == "NexusDWH" and params["password"] == "s3cr%t;#pw"
    assert "sslmode" not in params


def test_variables_sobrescriben_el_archivo(tmp_path):
    path = _write_ini(tmp_path, "[database]\nhost = 127.0.0.1\npassword = del_archivo\npool_min = 3\n"
                                "[auth]\ntrusted_proxies = 127.0.0.1\n")
    env = {"NEXUS__DATABASE__PASSWORD": "del_entorno", "NEXUS__AUTH__TRUSTED_PROXIES": "10.0.0.0/8,172.16.0.0/12",
           "NEXUS__DATABASE__SSLMODE": "require"}
    ini, info = nexus_config.load_config(path, env)
    assert info["file_loaded"] is True
    assert ini.get("database", "password") == "del_entorno"
    assert ini.get("database", "host") == "127.0.0.1"          # sin variable: queda el archivo
    assert ini.getint("database", "pool_min") == 3
    assert nexus_config.db_connect_params(ini)["sslmode"] == "require"
    s = AuthSettings.from_ini(ini)
    assert s.trusted_proxies == ("10.0.0.0/8", "172.16.0.0/12")


def test_vacias_y_mal_formadas_se_ignoran(tmp_path):
    path = _write_ini(tmp_path, "[database]\nport = 5546\n")
    env = {"NEXUS__DATABASE__PORT": "", "NEXUS__DATABASE__USER": "   ",
           "NEXUS__SOLO_SECCION": "x", "NEXUS__A__B__C": "x", "NEXUS____X": "x", "NEXUS__DB-X__Y": "x"}
    ini, info = nexus_config.load_config(path, env)
    assert ini.getint("database", "port") == 5546
    assert ini.get("database", "user", fallback="postgres") == "postgres"
    assert info["applied"] == []
    assert sorted(info["invalid"]) == sorted(["NEXUS__SOLO_SECCION", "NEXUS__A__B__C", "NEXUS____X", "NEXUS__DB-X__Y"])


def test_resumen_sin_valores(tmp_path):
    env = {"NEXUS__DATABASE__PASSWORD": "valor-secreto-123", "NEXUS__AUTH__PANEL_PROXY_KEY": "clave-secreta-456"}
    _ini, info = nexus_config.load_config(str(tmp_path / "x.ini"), env)
    text = repr(info)
    assert "valor-secreto-123" not in text and "clave-secreta-456" not in text
    assert set(info["applied"]) == {"database.password", "auth.panel_proxy_key"}


def test_allow_static_token_falso_por_defecto():
    ini, _ = nexus_config.load_config("/no/existe.ini", {})
    assert AuthSettings.from_ini(ini, "x" * 40).allow_static_token is False
    ini, _ = nexus_config.load_config("/no/existe.ini", {"NEXUS__ADMIN__ALLOW_STATIC_TOKEN": "true"})
    assert AuthSettings.from_ini(ini, "x" * 40).allow_static_token is True


def test_variable_especifica_sigue_funcionando(monkeypatch):
    monkeypatch.setenv("NEXUS_PANEL_PROXY_KEY", "clave-legada")
    ini, _ = nexus_config.load_config("/no/existe.ini", {})
    assert AuthSettings.from_ini(ini).panel_proxy_key == "clave-legada"
    ini, _ = nexus_config.load_config("/no/existe.ini", {"NEXUS__AUTH__PANEL_PROXY_KEY": "clave-nueva"})
    assert AuthSettings.from_ini(ini).panel_proxy_key == "clave-nueva"


@pytest.mark.parametrize("ip,esperado", [
    ("127.0.0.1", True), ("::1", True),
    ("10.0.1.7", True), ("172.18.0.5", True), ("172.32.0.1", False),
    ("192.168.1.10", False), ("8.8.8.8", False), ("", False), ("no-ip", False),
    ("fd00::1", False),
])
def test_trusted_proxies_cidr(ip, esperado):
    s = AuthSettings(trusted_proxies=("127.0.0.1", "::1", "10.0.0.0/8", "172.16.0.0/12"))
    assert s.is_trusted_proxy(ip) is esperado


def test_trusted_proxies_entrada_invalida_no_rompe():
    s = AuthSettings(trusted_proxies=("panel.local", "10.0.0.0/33", "192.168.0.10"))
    assert s.is_trusted_proxy("192.168.0.10") is True
    assert s.is_trusted_proxy("10.0.0.1") is False
    assert s.is_trusted_proxy("panel.local") is True   # coincidencia exacta de texto (compatibilidad)


def test_migrate_y_manage_users_leen_variables(tmp_path):
    """migrate.py y manage_users.py aceptan la BD solo por variables (sin config.ini)."""
    code = (
        "import migrate, manage_users, nexus_config;"
        "p = migrate.load_db_params();"
        "assert p['host'] == 'db.interno' and p['port'] == 6543 and p['dbname'] == 'NexusDWH';"
        "assert p['password'] == 'pw%x' and p['sslmode'] == 'verify-full' and p['connect_timeout'] == 10;"
        "print('ok')"
    )
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("NEXUS")}
    env.update({"NEXUS_CONFIG_FILE": str(tmp_path / "no-existe.ini"),
                "NEXUS__DATABASE__HOST": "db.interno", "NEXUS__DATABASE__PORT": "6543",
                "NEXUS__DATABASE__DB": "NexusDWH", "NEXUS__DATABASE__PASSWORD": "pw%x",
                "NEXUS__DATABASE__SSLMODE": "verify-full"})
    r = subprocess.run([sys.executable, "-c", code], cwd=BACK_DIR, env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "ok"


def test_manage_users_sin_config_ni_variables(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("NEXUS")}
    env["NEXUS_CONFIG_FILE"] = str(tmp_path / "no-existe.ini")
    r = subprocess.run([sys.executable, "manage_users.py", "list"], cwd=BACK_DIR, env=env,
                       capture_output=True, text=True)
    assert r.returncode != 0
    assert "NEXUS__DATABASE__" in r.stderr
