"""
authenticode.py — estado de la firma Authenticode de un archivo (solo Windows).

Usa ``WinVerifyTrust`` (wintrust.dll) vía ctypes con la acción
``WINTRUST_ACTION_GENERIC_VERIFY_V2``, sin interfaz de usuario. Devuelve:

  valid        firma embebida válida y cadena de confianza correcta
  unsigned     el archivo no tiene firma embebida (TRUST_E_NOSIGNATURE y afines)
  invalid      hay firma pero no es válida (hash alterado, certificado no
               confiable, caducado sin sello de tiempo, revocado…)
  unsupported  no es Windows: no se puede comprobar aquí

Notas:
  * Solo firmas EMBEBIDAS (las de catálogo del sistema no cuentan; no aplica al
    agente, que se firma con signtool).
  * La comprobación de revocación va desactivada por defecto
    (``WTD_REVOKE_NONE``) para no depender de red en sedes sin salida a
    Internet; el script ``update_agent.ps1`` complementa con
    ``Get-AuthenticodeSignature`` y compara el emisor/huella esperados.
  * Esto NO decide quién es el firmante: solo si la firma es válida. La
    identidad esperada (sujeto/huella) se compara en el script de actualización.
"""

import sys

TRUST_E_NOSIGNATURE = 0x800B0100
TRUST_E_SUBJECT_FORM_UNKNOWN = 0x800B0003
TRUST_E_PROVIDER_UNKNOWN = 0x800B0001
_UNSIGNED_CODES = {TRUST_E_NOSIGNATURE, TRUST_E_SUBJECT_FORM_UNKNOWN, TRUST_E_PROVIDER_UNKNOWN}


def _win_verify_trust(path: str) -> int:  # pragma: no cover - solo Windows
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                    ("Data4", ctypes.c_ubyte * 8)]

    class WINTRUST_FILE_INFO(ctypes.Structure):
        _fields_ = [("cbStruct", wintypes.DWORD), ("pcwszFilePath", wintypes.LPCWSTR),
                    ("hFile", wintypes.HANDLE), ("pgKnownSubject", ctypes.POINTER(GUID))]

    class WINTRUST_DATA(ctypes.Structure):
        _fields_ = [("cbStruct", wintypes.DWORD), ("pPolicyCallbackData", ctypes.c_void_p),
                    ("pSIPClientData", ctypes.c_void_p), ("dwUIChoice", wintypes.DWORD),
                    ("fdwRevocationChecks", wintypes.DWORD), ("dwUnionChoice", wintypes.DWORD),
                    ("pFile", ctypes.POINTER(WINTRUST_FILE_INFO)), ("dwStateAction", wintypes.DWORD),
                    ("hWVTStateData", wintypes.HANDLE), ("pwszURLReference", wintypes.LPCWSTR),
                    ("dwProvFlags", wintypes.DWORD), ("dwUIContext", wintypes.DWORD),
                    ("pSignatureSettings", ctypes.c_void_p)]

    # {00AAC56B-CD44-11d0-8CC2-00C04FC295EE}
    action = GUID(0x00AAC56B, 0xCD44, 0x11D0, (ctypes.c_ubyte * 8)(0x8C, 0xC2, 0x00, 0xC0, 0x4F, 0xC2, 0x95, 0xEE))
    WTD_UI_NONE, WTD_REVOKE_NONE, WTD_CHOICE_FILE = 2, 0, 1
    WTD_STATEACTION_VERIFY, WTD_STATEACTION_CLOSE = 1, 2

    file_info = WINTRUST_FILE_INFO(ctypes.sizeof(WINTRUST_FILE_INFO), path, None, None)
    data = WINTRUST_DATA()
    data.cbStruct = ctypes.sizeof(WINTRUST_DATA)
    data.dwUIChoice = WTD_UI_NONE
    data.fdwRevocationChecks = WTD_REVOKE_NONE
    data.dwUnionChoice = WTD_CHOICE_FILE
    data.pFile = ctypes.pointer(file_info)
    data.dwStateAction = WTD_STATEACTION_VERIFY

    wintrust = ctypes.WinDLL("wintrust")
    wintrust.WinVerifyTrust.argtypes = [wintypes.HWND, ctypes.POINTER(GUID), ctypes.c_void_p]
    wintrust.WinVerifyTrust.restype = ctypes.c_long
    try:
        result = wintrust.WinVerifyTrust(None, ctypes.byref(action), ctypes.byref(data))
    finally:
        data.dwStateAction = WTD_STATEACTION_CLOSE
        wintrust.WinVerifyTrust(None, ctypes.byref(action), ctypes.byref(data))
    return result & 0xFFFFFFFF


def classify(code: int) -> str:
    """Traduce el código de WinVerifyTrust a valid / unsigned / invalid."""
    code &= 0xFFFFFFFF
    if code == 0:
        return "valid"
    if code in _UNSIGNED_CODES:
        return "unsigned"
    return "invalid"


def authenticode_status(path: str) -> str:
    if sys.platform != "win32":
        return "unsupported"
    return classify(_win_verify_trust(path))  # pragma: no cover - solo Windows
