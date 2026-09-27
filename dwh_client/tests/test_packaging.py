"""
Pruebas de la fase 5 (empaquetado, servicio, actualizaciones) sin red ni BD.

Las claves Ed25519 se generan EN LA PRUEBA (efímeras): no es una firma de producción.
Las partes exclusivas de Windows (SCM, DPAPI real, WinVerifyTrust) se ejecutan solo en
Windows (job de CI windows-latest); en el resto se prueba la lógica independiente.
"""

import base64
import json
import os
import shutil
import sys
import threading

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from nexus_agent import AGENT_VERSION, cli, selftest, winservice
from nexus_agent.agent import Agent, FatalAgentError
from nexus_agent.api import ApiRejected
from nexus_agent.credstore import CredentialStore
from nexus_agent.settings import Settings, read_enrollment_file
from nexus_agent.updates import (
    MANIFEST_NAME, SIGNATURE_NAME, UpdateVerificationError, build_manifest, key_id_for, manifest_bytes,
    parse_version, sign_manifest, verify_release,
)

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "packaging"))
import verify_package  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Utilidades: paquete falso y claves efímeras
# ─────────────────────────────────────────────────────────────────────────────
def _key():
    k = Ed25519PrivateKey.generate()
    raw = k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return k, {key_id_for(raw): base64.b64encode(raw).decode()}


def _package(tmp_path, version="9.0.0", key=None, *, authenticode=None, min_from="", exe="NexusAgent.exe"):
    d = tmp_path / f"pkg-{version}"
    (d / "sub").mkdir(parents=True)
    (d / exe).write_bytes(b"MZ binario compilado " + os.urandom(64))
    (d / "python312.dll").write_bytes(b"dll " + os.urandom(32))
    (d / "sub" / "_psycopg.pyd").write_bytes(b"pyd " + os.urandom(32))
    (d / "config.example.ini").write_text("[nexus]\napi_url = https://nexus.example\ntoken =\n", encoding="utf-8")
    m = build_manifest(str(d), version, platform="windows-x64", build={"t": 1}, authenticode=authenticode,
                       min_from_version=min_from, executables=[exe])
    data = manifest_bytes(m)
    (d / MANIFEST_NAME).write_bytes(data)
    if key is not None:
        (d / SIGNATURE_NAME).write_text(json.dumps(sign_manifest(data, key)), encoding="utf-8")
    return d


def _err(fn, code):
    with pytest.raises(UpdateVerificationError) as ei:
        fn()
    assert ei.value.code == code, ei.value
    return ei.value


# ─────────────────────────────────────────────────────────────────────────────
# Validación de actualizaciones
# ─────────────────────────────────────────────────────────────────────────────
def test_version_parse():
    assert parse_version("5.2") == (5, 2, 0, 0) < parse_version("5.10.0")
    _err(lambda: parse_version("5.2.0-beta"), "bad_version")
    _err(lambda: parse_version(""), "bad_version")


def test_paquete_firmado_valido(tmp_path):
    k, keys = _key()
    d = _package(tmp_path, "5.3.0", k)
    res = verify_release(str(d), installed_version="5.2.0", trusted_keys=keys, expected_platform="windows-x64")
    assert res.signed_manifest and res.key_id in keys and res.version == "5.3.0" and res.files == 4
    assert any("SIN FIRMA Authenticode" in w for w in res.warnings)


def test_archivo_alterado_rechazado(tmp_path):
    k, keys = _key()
    d = _package(tmp_path, "5.3.0", k)
    (d / "python312.dll").write_bytes(b"dll alterada")
    _err(lambda: verify_release(str(d), installed_version="5.2.0", trusted_keys=keys), "size_mismatch")
    # Mismo tamaño, contenido distinto → hash.
    data = (d / "sub" / "_psycopg.pyd").read_bytes()
    d2 = _package(tmp_path / "b", "5.3.0", k)
    (d2 / "sub" / "_psycopg.pyd").write_bytes(bytes(len(data)))
    _err(lambda: verify_release(str(d2), installed_version="5.2.0", trusted_keys=keys), "hash_mismatch")


def test_firma_invalida_y_clave_desconocida(tmp_path):
    k, keys = _key()
    d = _package(tmp_path, "5.3.0", k)
    # Manifiesto modificado DESPUÉS de firmar (p. ej. para "legitimar" un archivo cambiado).
    m = json.loads((d / MANIFEST_NAME).read_text(encoding="utf-8"))
    m["version"] = "5.4.0"
    (d / MANIFEST_NAME).write_bytes(manifest_bytes(m))
    _err(lambda: verify_release(str(d), installed_version="5.2.0", trusted_keys=keys), "bad_signature")
    # Firmado con otra clave (no confiable).
    other, _ = _key()
    d2 = _package(tmp_path / "o", "5.3.0", other)
    _err(lambda: verify_release(str(d2), installed_version="5.2.0", trusted_keys=keys), "unknown_key")
    # Sin claves confiables compiladas (estado actual del repo): toda firma es "desconocida".
    _err(lambda: verify_release(str(d2), installed_version="5.2.0", trusted_keys={}), "unknown_key")


def test_downgrade_y_misma_version(tmp_path):
    k, keys = _key()
    d = _package(tmp_path, "5.1.0", k)
    _err(lambda: verify_release(str(d), installed_version="5.2.0", trusted_keys=keys), "downgrade")
    same = _package(tmp_path / "s", "5.2.0", k)
    _err(lambda: verify_release(str(same), installed_version="5.2.0", trusted_keys=keys), "same_version")
    assert verify_release(str(same), installed_version="5.2.0", trusted_keys=keys, allow_same_version=True)
    mf = _package(tmp_path / "m", "6.0.0", k, min_from="5.5.0")
    _err(lambda: verify_release(str(mf), installed_version="5.2.0", trusted_keys=keys), "min_from_version")


def test_manifiesto_sin_firma_solo_en_modo_explicito(tmp_path):
    d = _package(tmp_path, "5.3.0", None)
    _err(lambda: verify_release(str(d), installed_version="5.2.0", trusted_keys={}), "unsigned_manifest")
    res = verify_release(str(d), installed_version="5.2.0", trusted_keys={}, allow_unsigned=True)
    assert not res.signed_manifest and any("SIN FIRMA" in w for w in res.warnings)
    # Aun sin firma, la integridad se verifica.
    (d / "NexusAgent.exe").write_bytes(b"x")
    _err(lambda: verify_release(str(d), installed_version="5.2.0", trusted_keys={}, allow_unsigned=True),
         "size_mismatch")


def test_archivo_extra_y_rutas_peligrosas(tmp_path):
    k, keys = _key()
    d = _package(tmp_path, "5.3.0", k)
    (d / "version.dll").write_bytes(b"dll plantada")  # DLL no declarada (secuestro de carga)
    e = _err(lambda: verify_release(str(d), installed_version="5.2.0", trusted_keys=keys), "unexpected_files")
    assert "version.dll" in e.message
    d2 = _package(tmp_path / "p", "5.3.0", None)
    m = json.loads((d2 / MANIFEST_NAME).read_text(encoding="utf-8"))
    m["files"].append({"path": "../fuera.dll", "size": 1, "sha256": "0" * 64})
    (d2 / MANIFEST_NAME).write_bytes(manifest_bytes(m))
    _err(lambda: verify_release(str(d2), installed_version="5.2.0", trusted_keys={}, allow_unsigned=True),
         "unsafe_path")
    _err(lambda: verify_release(str(tmp_path / "no-existe"), installed_version="5.2.0", trusted_keys={}),
         "no_manifest")


def test_authenticode_declarado(tmp_path):
    k, keys = _key()
    d = _package(tmp_path, "5.3.0", k, authenticode={"signed": True, "subject": "CN=X", "thumbprint": "AB"})
    ok = verify_release(str(d), installed_version="5.2.0", trusted_keys=keys, authenticode_check=lambda p: "valid")
    assert ok.authenticode == "valid"
    for bad in ("invalid", "unsigned"):
        _err(lambda: verify_release(str(d), installed_version="5.2.0", trusted_keys=keys,
                                    authenticode_check=lambda p: bad), "authenticode_invalid")
    nc = verify_release(str(d), installed_version="5.2.0", trusted_keys=keys,
                        authenticode_check=lambda p: "unsupported")
    assert nc.authenticode == "not_checked" and nc.warnings
    d2 = _package(tmp_path / "w", "5.3.0", k)
    _err(lambda: verify_release(str(d2), installed_version="5.2.0", trusted_keys=keys,
                                expected_platform="linux-x64"), "wrong_platform")


def test_cli_verify_update(tmp_path, capsys):
    d = _package(tmp_path, "99.0.0", None)
    assert cli.main(["--verify-update", str(d)]) == 4
    assert "RECHAZADO [unsigned_manifest]" in capsys.readouterr().out
    assert cli.main(["--verify-update", str(d), "--allow-unsigned-manifest"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("OK versión 99.0.0") and "SIN FIRMA" in out
    old = _package(tmp_path / "old", "1.0.0", None)
    assert cli.main(["--verify-update", str(old), "--allow-unsigned-manifest"]) == 4
    assert "downgrade" in capsys.readouterr().out


def test_herramientas_de_manifiesto(tmp_path, monkeypatch):
    """make_manifest regenera hashes tras 'firmar' y elimina la firma Ed25519 vieja."""
    import make_manifest

    k, keys = _key()
    d = _package(tmp_path, "5.3.0", k)
    (d / "NexusAgent.exe").write_bytes(b"MZ ahora con firma authenticode simulada en la prueba")
    assert make_manifest.main(["--package", str(d), "--authenticode-signed", "--signer-subject", "CN=Prueba",
                               "--signer-thumbprint", "ab12"]) == 0
    assert not (d / SIGNATURE_NAME).exists()
    m = json.loads((d / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert m["authenticode"] == {"signed": True, "subject": "CN=Prueba", "thumbprint": "AB12"}
    (d / SIGNATURE_NAME).write_text(json.dumps(sign_manifest((d / MANIFEST_NAME).read_bytes(), k)))
    res = verify_release(str(d), installed_version="5.2.0", trusted_keys=keys, authenticode_check=lambda p: "valid")
    assert res.signed_manifest and res.authenticode == "valid"


# ─────────────────────────────────────────────────────────────────────────────
# verify_package.py (contenido del paquete)
# ─────────────────────────────────────────────────────────────────────────────
def _fake_dist(tmp_path):
    d = tmp_path / "NexusAgent"
    (d / "certifi").mkdir(parents=True)
    (d / "NexusAgent.exe").write_bytes(b"MZ\x00 constantes compiladas: Instalacion enrolada")
    (d / "_ssl.pyd").write_bytes(b"\x00pyd")
    (d / "certifi" / "cacert.pem").write_text("-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n")
    shutil.copyfile(os.path.join(os.path.dirname(HERE), "config_postgres.ini.example"), d / "config.example.ini")
    (d / "LEEME.txt").write_text("leeme")
    return d


def test_verify_package_limpio_y_con_manifiesto(tmp_path):
    d = _fake_dist(tmp_path)
    res = verify_package.scan(str(d))
    assert res["ok"], res["errors"]
    (d / MANIFEST_NAME).write_bytes(manifest_bytes(build_manifest(str(d), "5.2.0", platform="windows-x64")))
    assert verify_package.scan(str(d))["ok"]
    (d / "LEEME.txt").write_text("cambiado")
    (d / "extra.dll").write_bytes(b"x")
    errs = verify_package.scan(str(d))["errors"]
    assert any("SHA-256 distinto" in e and "LEEME.txt" in e for e in errs)
    assert any("no declarado" in e and "extra.dll" in e for e in errs)


@pytest.mark.parametrize("rel,content,needle", [
    ("nexus_agent/agent.py", b"x", "PROPIO"),
    ("client_postgres.pyc", b"x", "PROPIO"),
    ("tercero/modulo.pyc", b"x", "Fuente/bytecode"),
    ("seed_dev_postgres.sql", b"select 1", "*.sql"),
    ("config.ini", b"[nexus]\ntoken = abc\n", "config.ini"),
    ("otro.ini", b"[x]\n", "solo config.example.ini"),
    ("agent_data/agent_credential.dpapi", b"x", "agent_data"),
    ("agent_state.db", b"x", "agent_state.db"),
    ("logs/nexus_agent.log", b"x", "logs"),
    ("tests/test_units.py", b"x", "tests"),
    ("NexusAgent.exe.dmp", b"x", "*.dmp"),
    ("clave.pem", b"-----BEGIN PRIVATE KEY-----\nAAA\n-----END PRIVATE KEY-----\n", "llave privada"),
    ("firma.pfx", b"x", "*.pfx"),
    ("lib.dll", b"\x00\x00def ensure_credential(self, force_enroll: bool = False)", "código fuente propio"),
])
def test_verify_package_detecta_prohibidos(tmp_path, rel, content, needle):
    d = _fake_dist(tmp_path)
    p = d / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    res = verify_package.scan(str(d))
    assert not res["ok"]
    assert any(needle in e for e in res["errors"]), res["errors"]


def test_verify_package_plantilla_con_valores(tmp_path):
    d = _fake_dist(tmp_path)
    (d / "config.example.ini").write_text("[nexus]\napi_url = https://x\ngroup_token = secreto123\n")
    assert any("group_token" in e for e in verify_package.scan(str(d))["errors"])
    # Terceros .pyc tolerados solo con el modo explícito; los propios nunca.
    d2 = _fake_dist(tmp_path / "b")
    (d2 / "tercero.pyc").write_bytes(b"x")
    assert verify_package.scan(str(d2), allow_third_party_py=True)["ok"]
    (d2 / "nexus_agent").mkdir()
    (d2 / "nexus_agent" / "cli.pyc").write_bytes(b"x")
    assert not verify_package.scan(str(d2), allow_third_party_py=True)["ok"]


def test_plantilla_del_repo_sin_valores_sensibles():
    errors = []
    verify_package.check_config_template(os.path.join(os.path.dirname(HERE), "config_postgres.ini.example"), errors)
    assert errors == []


# ─────────────────────────────────────────────────────────────────────────────
# Servicio de Windows: lógica independiente del SCM
# ─────────────────────────────────────────────────────────────────────────────
def test_wait_hint_cubre_la_gracia():
    assert winservice.stop_wait_hint_ms(60) == (60 + 50) * 1000
    assert winservice.stop_wait_hint_ms(-5) == 50 * 1000


def test_disposicion_de_salida():
    assert winservice.exit_disposition(0) == ("stopped", 0, 0)
    assert winservice.exit_disposition(2) == ("stopped", 1066, 2)
    assert winservice.exit_disposition(3) == ("stopped", 1066, 3)
    assert winservice.exit_disposition(1)[0] == "crash"


class _FakeAgent:
    def __init__(self):
        self.stopped = threading.Event()
        self.settings = Settings(shutdown_grace_seconds=120)

    def stop(self):
        self.stopped.set()


def test_parada_antes_y_despues_de_crear_el_agente():
    c = winservice.StopCoordinator()
    assert c.grace_seconds() == 60
    c.request_stop()                      # el SCM pide parar mientras se carga la config
    a = _FakeAgent()
    c.attach(a)
    assert a.stopped.is_set()
    c2 = winservice.StopCoordinator()
    b = _FakeAgent()
    c2.attach(b)
    assert not b.stopped.is_set() and c2.grace_seconds() == 120
    c2.request_stop()
    assert b.stopped.is_set()


@pytest.mark.skipif(sys.platform == "win32", reason="en Windows --service requiere el SCM")
def test_service_fuera_de_windows(capsys):
    assert cli.main(["--service"]) == 2
    assert "solo está disponible en Windows" in capsys.readouterr().err


def test_run_agent_en_modo_servicio_sin_senales(tmp_path):
    """El modo servicio corre en un hilo que no es el principal: no debe instalar señales ni usar consola."""
    ini = tmp_path / "config.ini"
    ini.write_text("[nexus]\napi_url = http://127.0.0.1:9\n[agent]\n"
                   f"data_dir = {tmp_path / 'data'}\nlog_dir = {tmp_path / 'logs'}\n", encoding="utf-8")
    seen = {}
    args = cli.build_parser().parse_args(["--config", str(ini)])
    out = {}

    def run():
        out["code"] = cli.run_agent(args, console=False, install_signals=False, on_agent=lambda a: seen.setdefault("a", a))

    th = threading.Thread(target=run)
    th.start()
    th.join(30)
    assert out["code"] == 2 and "a" in seen          # sin credencial ni token → configuración
    log = (tmp_path / "logs" / "nexus_agent.log").read_text(encoding="utf-8")
    assert "· servicio ===" in log and "No hay credencial de instalación" in log


# ─────────────────────────────────────────────────────────────────────────────
# Token de enrolamiento de un solo uso
# ─────────────────────────────────────────────────────────────────────────────
def test_archivo_de_token_prioridad_y_bom(tmp_path):
    p = tmp_path / "enrollment_token.ini"
    assert read_enrollment_file(str(p)) == ("", "")
    p.write_bytes("﻿[nexus]\ntoken = emp\nagency_token = ag\n".encode("utf-8"))  # BOM (Notepad)
    assert read_enrollment_file(str(p)) == ("agency", "ag")


class _EnrollApi:
    def __init__(self, settings, holder):
        self.reject = False
        self.enrolled = []

    def enroll(self, kind, token, info):
        if self.reject:
            raise ApiRejected(401, "invalid_token", "token inválido")
        self.enrolled.append((kind, token))
        return {"installation_id": "i-1", "secret": "s" * 43, "scope": {"type": kind}}

    def close(self):
        pass


def _enroll_agent(tmp_path):
    s = Settings(api_url="http://127.0.0.1:9", data_dir=str(tmp_path))
    return Agent(s, api_factory=_EnrollApi, store=CredentialStore(str(tmp_path), force_backend="file"))


def test_enrolamiento_con_archivo_de_un_solo_uso(tmp_path):
    a = _enroll_agent(tmp_path)
    (tmp_path / "enrollment_token.ini").write_text("[nexus]\ngroup_token = tok-grupo\n", encoding="utf-8")
    cred = a.ensure_credential()
    assert cred.installation_id == "i-1" and a.api.enrolled == [("group", "tok-grupo")]
    assert not (tmp_path / "enrollment_token.ini").exists()      # borrado tras guardar la credencial
    assert a.store.exists()


def test_enrolamiento_rechazado_aparta_el_archivo(tmp_path):
    a = _enroll_agent(tmp_path)
    a.api.reject = True
    (tmp_path / "enrollment_token.ini").write_text("[nexus]\nagency_token = malo\n", encoding="utf-8")
    with pytest.raises(FatalAgentError) as ei:
        a.ensure_credential()
    assert ei.value.exit_code == 3
    assert not (tmp_path / "enrollment_token.ini").exists()
    marker = tmp_path / "enrollment_token.ini.rechazado"
    assert marker.exists() and "malo" not in marker.read_text(encoding="utf-8")  # sin el token en claro
    assert "invalid_token" in marker.read_text(encoding="utf-8")


def test_token_sobrante_se_borra_si_ya_hay_credencial(tmp_path):
    a = _enroll_agent(tmp_path)
    (tmp_path / "enrollment_token.ini").write_text("[nexus]\ngroup_token = uno\n", encoding="utf-8")
    a.ensure_credential()
    (tmp_path / "enrollment_token.ini").write_text("[nexus]\ngroup_token = sobrante\n", encoding="utf-8")
    b = _enroll_agent(tmp_path)
    b.ensure_credential()                       # carga la credencial existente
    assert b.api.enrolled == []                 # no re-enrola
    assert not (tmp_path / "enrollment_token.ini").exists()


def test_self_check_de_credencial_en_archivo(tmp_path):
    ok, detail = CredentialStore(str(tmp_path), force_backend="file").self_check()
    assert ok and "0600" in detail
    fake = CredentialStore(str(tmp_path), force_backend="dpapi", protect=lambda d, m: d[::-1],
                           unprotect=lambda d, m: d[::-1])
    assert fake.self_check()[0]
    broken = CredentialStore(str(tmp_path), force_backend="dpapi", protect=lambda d, m: d,
                             unprotect=lambda d, m: (_ for _ in ()).throw(OSError("perfil no cargado")))
    ok, detail = broken.self_check()
    assert not ok and "OSError" in detail


# ─────────────────────────────────────────────────────────────────────────────
# CLI: --version / --selftest
# ─────────────────────────────────────────────────────────────────────────────
def test_version_y_selftest(capsys):
    assert cli.main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == AGENT_VERSION
    assert cli.main(["--selftest"]) == 0
    out = capsys.readouterr().out
    for name in ("psycopg2", "pymysql", "pyodbc", "fdb", "cryptography", "sqlite3", "ssl", "requests"):
        assert f"OK     {name}" in out, out
    assert "Resultado: OK" in out
    assert "ERROR" not in json.dumps(selftest.summary())


# ─────────────────────────────────────────────────────────────────────────────
# Solo Windows (CI windows-latest): DPAPI real y WinVerifyTrust
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI solo existe en Windows")
@pytest.mark.parametrize("scope", ["user", "machine"])
def test_dpapi_real(tmp_path, scope):
    from nexus_agent.credstore import InstallationCredential

    st = CredentialStore(str(tmp_path), scope)
    assert st.backend == "dpapi" and st.self_check()[0]
    st.save(InstallationCredential("id-1", "secreto-dpapi", "https://x"))
    raw = (tmp_path / "agent_credential.dpapi").read_bytes()
    assert b"secreto-dpapi" not in raw and b"secreto-dpapi" not in base64.b64decode(raw)
    assert st.load().secret == "secreto-dpapi"


@pytest.mark.skipif(sys.platform != "win32", reason="WinVerifyTrust solo existe en Windows")
def test_authenticode_real(tmp_path):
    from nexus_agent.authenticode import authenticode_status

    f = tmp_path / "sin_firma.exe"
    shutil.copyfile(sys.executable, f)
    with open(f, "ab") as fh:           # romper cualquier firma que tuviera la copia
        fh.write(b"\x00" * 16)
    assert authenticode_status(str(f)) in ("unsigned", "invalid")
    plain = tmp_path / "texto.exe"
    plain.write_bytes(b"no es un PE")
    assert authenticode_status(str(plain)) in ("unsigned", "invalid")


# ─────────────────────────────────────────────────────────────────────────────
# Correcciones de validación (fase 5, ronda 2)
# ─────────────────────────────────────────────────────────────────────────────
def test_con_claves_confiables_no_hay_modo_sin_firma(tmp_path, capsys, monkeypatch):
    _, keys = _key()
    d = _package(tmp_path, "5.3.0", None)
    e = _err(lambda: verify_release(str(d), installed_version="5.2.0", trusted_keys=keys, allow_unsigned=True),
             "unsigned_manifest")
    assert "exige manifiestos firmados" in e.message
    # Por CLI con las claves "compiladas" (release_keys parcheado en la prueba).
    from nexus_agent import release_keys

    monkeypatch.setattr(release_keys, "TRUSTED_RELEASE_KEYS", keys)
    d2 = _package(tmp_path / "c", "99.0.0", None)
    assert cli.main(["--verify-update", str(d2), "--allow-unsigned-manifest"]) == 4
    assert "unsigned_manifest" in capsys.readouterr().out


@pytest.mark.parametrize("bad", [
    "a:b.dll", "NexusAgent.exe:flujo", "nul", "NUL.txt", "sub/con.dll", "COM1", "lpt9.log", "aux.",
    "archivo.dll.", "archivo.dll ", "sub./x.dll", "C:/x.dll", "/abs.dll", "\\\\srv\\x.dll", "a/../b", "a//b",
    "tab\tname", "",
])
def test_rutas_no_permitidas_en_windows(tmp_path, bad):
    d = _package(tmp_path, "5.3.0", None)
    m = json.loads((d / MANIFEST_NAME).read_text(encoding="utf-8"))
    m["files"].append({"path": bad, "size": 1, "sha256": "0" * 64})
    (d / MANIFEST_NAME).write_bytes(manifest_bytes(m))
    _err(lambda: verify_release(str(d), installed_version="5.2.0", trusted_keys={}, allow_unsigned=True),
         "unsafe_path")


def test_nombres_permitidos():
    from nexus_agent.updates import _is_safe_relpath

    for ok in ("NexusAgent.exe", "sub/_psycopg.pyd", "console.dll", "com10.dll", "auxiliar.pyd", "a.b.c"):
        assert _is_safe_relpath(ok), ok


def test_duplicado_por_mayusculas_y_extra_con_otra_capitalizacion(tmp_path):
    d = _package(tmp_path, "5.3.0", None)
    m = json.loads((d / MANIFEST_NAME).read_text(encoding="utf-8"))
    dup = dict(m["files"][0])
    dup["path"] = dup["path"].upper()
    m["files"].append(dup)
    (d / MANIFEST_NAME).write_bytes(manifest_bytes(m))
    e = _err(lambda: verify_release(str(d), installed_version="5.2.0", trusted_keys={}, allow_unsigned=True),
             "bad_manifest")
    assert "repetido" in e.message


@pytest.mark.parametrize("entry", [
    "no-es-un-objeto", {"path": "LEEME.txt"}, {"path": "x", "size": "10", "sha256": "0" * 64},
    {"path": "x", "size": True, "sha256": "0" * 64}, {"path": "x", "size": -1, "sha256": "0" * 64},
    {"path": "x", "size": 1, "sha256": "zz"}, {"path": 7, "size": 1, "sha256": "0" * 64}, None,
])
def test_entradas_mal_formadas_rechazo_controlado(tmp_path, entry, capsys):
    d = _package(tmp_path, "99.0.0", None)
    m = json.loads((d / MANIFEST_NAME).read_text(encoding="utf-8"))
    m["files"].insert(0, entry)
    (d / MANIFEST_NAME).write_bytes(manifest_bytes(m))
    with pytest.raises(UpdateVerificationError) as ei:
        verify_release(str(d), installed_version="5.2.0", trusted_keys={}, allow_unsigned=True)
    assert ei.value.code in ("bad_manifest", "unsafe_path")
    assert cli.main(["--verify-update", str(d), "--allow-unsigned-manifest"]) == 4
    out = capsys.readouterr()
    assert "RECHAZADO" in out.out and "Traceback" not in out.out + out.err


@pytest.mark.parametrize("manifest_patch", [
    {"files": "texto"}, {"files": []}, {"executables": "NexusAgent.exe", "authenticode": {"signed": True}},
    {"version": 5}, {"authenticode": "x"},
])
def test_manifiestos_hostiles_no_rompen_la_cli(tmp_path, manifest_patch, capsys):
    d = _package(tmp_path, "99.0.0", None)
    m = json.loads((d / MANIFEST_NAME).read_text(encoding="utf-8"))
    m.update(manifest_patch)
    (d / MANIFEST_NAME).write_bytes(manifest_bytes(m))
    assert cli.main(["--verify-update", str(d), "--allow-unsigned-manifest"]) == 4
    out = capsys.readouterr()
    assert "RECHAZADO" in out.out and "Traceback" not in out.out + out.err


def _symlink(src, dst, is_dir=False):
    try:
        os.symlink(src, dst, target_is_directory=is_dir)
    except (OSError, NotImplementedError) as exc:  # Windows sin privilegio de enlaces
        pytest.skip(f"no se pueden crear enlaces simbólicos aquí: {exc}")


def test_enlaces_simbolicos_rechazados(tmp_path):
    # Archivo enlace (aunque apunte a un archivo declarado del mismo paquete).
    d = _package(tmp_path, "5.3.0", None)
    _symlink(str(d / "python312.dll"), str(d / "enlace.dll"))
    e = _err(lambda: verify_release(str(d), installed_version="5.2.0", trusted_keys={}, allow_unsigned=True),
             "link_in_package")
    assert "enlace.dll" in e.message
    # Carpeta enlace hacia fuera del paquete: se rechaza aunque os.walk no la recorra.
    fuera = tmp_path / "fuera"
    fuera.mkdir()
    (fuera / "plantada.dll").write_bytes(b"x")
    d2 = _package(tmp_path / "b", "5.3.0", None)
    _symlink(str(fuera), str(d2 / "sub2"), is_dir=True)
    _err(lambda: verify_release(str(d2), installed_version="5.2.0", trusted_keys={}, allow_unsigned=True),
         "link_in_package")
    # La carpeta raíz del paquete como enlace.
    d3 = _package(tmp_path / "c", "5.3.0", None)
    link_root = tmp_path / "raiz_enlace"
    _symlink(str(d3), str(link_root), is_dir=True)
    _err(lambda: verify_release(str(link_root), installed_version="5.2.0", trusted_keys={}, allow_unsigned=True),
         "link_in_package")


def test_verify_package_detecta_docstrings(tmp_path):
    d = _fake_dist(tmp_path)
    (d / "NexusAgent.exe").write_bytes(b"MZ\x00Coordina la parada pedida por el SCM con el agente")
    res = verify_package.scan(str(d))
    assert any("Docstring propio" in e for e in res["errors"])


def test_error_de_configuracion_en_modo_servicio_queda_en_log(tmp_path):
    """config.ini ausente/ilegible en modo servicio: aún no hay log_dir → se escribe en <data_dir>/../logs."""
    data = tmp_path / "NexusAgent" / "data"
    args = cli.build_parser().parse_args(["--config", str(tmp_path / "no-existe.ini"), "--data-dir", str(data)])
    assert cli.run_agent(args, console=False, install_signals=False) == 2
    log = (tmp_path / "NexusAgent" / "logs" / "nexus_agent.log").read_text(encoding="utf-8")
    assert "Error de configuración, el servicio se detiene (código 2)" in log
    bad = tmp_path / "malo.ini"
    bad.write_text("[nexus]\napi_url = http://127.0.0.1:9\n[agent]\nheartbeat_seconds = abc\n", encoding="utf-8")
    args = cli.build_parser().parse_args(["--config", str(bad), "--data-dir", str(data)])
    assert cli.run_agent(args, console=False, install_signals=False) == 2
    log = (tmp_path / "NexusAgent" / "logs" / "nexus_agent.log").read_text(encoding="utf-8")
    assert "heartbeat_seconds debe ser un entero" in log


@pytest.mark.parametrize("extra", [
    "\n[agent]\nheartbeat_seconds = 30\n",          # sección duplicada (caso 1b del CI)
    "\n[agent2]\nx = 1\nx = 2\n",                    # opción duplicada
])
def test_config_ini_malformado_es_error_de_configuracion(tmp_path, extra):
    """Un INI mal formado debe ser ConfigError (salida 2), no una caída con reinicios."""
    from nexus_agent.sanitize import ConfigError
    from nexus_agent.settings import load_settings
    cfg = tmp_path / "config.ini"
    cfg.write_text("[nexus]\napi_url = https://nexus.invalid\n[agent]\ntick_seconds = 5\n" + extra, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_settings(str(cfg), data_dir=str(tmp_path / "data"))
    sin_encabezado = tmp_path / "sin.ini"
    sin_encabezado.write_text("api_url = x\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_settings(str(sin_encabezado), data_dir=str(tmp_path / "data"))
