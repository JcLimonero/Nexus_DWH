# ARCHIVO SINCRONIZADO: copia exacta de dwh_back/redact.py (una prueba verifica que coincidan).
"""
redact.py — saneamiento de textos en el backend.

Se usa para:
  * detalle de eventos legados (POST /client-event), que puede traer trazas;
  * mensajes de error de ejecuciones del agente (defensa en profundidad: el
    agente ya los sanea);
  * error_detail de activity_log y logs del servidor.

Diseño contra ReDoS: la entrada se RECORTA antes de sanear (``MAX_INPUT``),
todas las expresiones usan cuantificadores acotados sobre clases negadas (sin
``.*?`` abiertos) y el SQL se detecta con búsquedas lineales (str.find).

Qué quita (enfoque conservador; ver DWH_README §17.7):
  * TODOS los literales entre comillas simples ('...'; un apóstrofo dentro de
    una palabra, como en "Can't", no abre literal) y los literales entre
    comillas dobles ("...") salvo cuando van tras una palabra de identificador
    (relation/column/table/constraint/index/schema/type/function/sequence/view):
    así se conservan nombres de tablas/columnas y se ocultan valores, usuarios,
    hosts, bases, etc.
  * listas de valores entre paréntesis: ``Key (...)=(...)``, ``Failing row
    contains (...)``, ``value is (...)``, ``VALUES (...)`` y cualquier
    paréntesis con comas o @;
  * líneas DETAIL/LINE/HINT/QUERY/CONTEXT/WHERE de PostgreSQL;
  * SQL (también multilínea): desde select…from, insert…into, update…set,
    delete…from, with…as, merge…into, create/alter…, exec… hasta el final;
  * pares clave=valor de cadenas de conexión/DSN, URLs con credenciales, IPs,
    puertos y todo valor conocido pasado en ``secrets``;
  * correos electrónicos y secuencias de ≥ 9 dígitos (tarjetas, teléfonos,
    cuentas) en cualquier parte, y valores entre corchetes [..] salvo la cadena
    de drivers ODBC y SQLSTATE ([Microsoft][ODBC Driver 17…][42S02]).
  Se conserva el código numérico inicial de MySQL: "(1062, ***)".
  Límite residual: valores de negocio SIN comillas ni formato reconocible
  (p. ej. "rfc=XAXX…" sin clave de conexión conocida, fragmentos de fila CSV)
  no se detectan.
"""

import re
from typing import Iterable, List, Optional, Tuple

MAX_INPUT = 16_384

_CONN_KEYS = (
    r"pwd|password|passwd|uid|user id|user|username|server|data source|dsn|database|"
    r"initial catalog|host|hostaddr|dbname|port|address|addr|token|secret|api[_-]?key"
)
_RE_KV = re.compile(rf"(?i)\b({_CONN_KEYS})\s*=\s*(\"[^\"\n]{{0,500}}\"|'[^'\n]{{0,500}}'|\{{[^}}\n]{{0,500}}\}}|[^;\s,]{{1,500}})")
_RE_URL_CRED = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]{0,20}://)[^/\s:@]{1,200}(?::[^/\s@]{0,200})?@[^/\s]{1,200}")
_RE_KEY_VALUES = re.compile(r"Key \(([^()\n]{0,300})\)=\([^\n]{0,4000}")
_RE_FAILING_ROW = re.compile(r"(?i)failing row contains \([^\n]{0,4000}")
_RE_VALUE_LIST = re.compile(r"(?i)\b(value is|values|contains|entry|row)\s*\([^()\n]{0,2000}\)")
_RE_PAREN_LIST = re.compile(r"\((?=[^()\n]{0,2000}[,@])[^()\n]{0,2000}\)")
_RE_PG_EXTRA_LINES = re.compile(r"(?im)^[ \t]{0,20}(DETAIL|LINE \d{1,6}|HINT|QUERY|CONTEXT|WHERE)\b[^\n]{0,20000}$")
_RE_CARET = re.compile(r"(?m)^[ \t]{0,2000}\^[ \t]{0,20}$")
_RE_IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
_RE_IPV6_PAREN = re.compile(r"\((?:[0-9a-fA-F]{0,4}:){2,7}[0-9a-fA-F]{0,4}\)")
_RE_PORT = re.compile(r"(?i)\bport\s{1,5}\d{1,6}\b")
_IDENT_WORDS = r"relation|column|table|constraint|index|schema|type|function|sequence|view|trigger|operator|extension"
_RE_DQ = re.compile(rf'(?i)(\b(?:{_IDENT_WORDS})\s{{1,5}})?"([^"\n]{{0,500}})"')
# Literal entre comillas simples. Solo una contracción inglesa ("Can't", "it's",
# "we're", "I'll"…: letra + ' + t/s/d/m/re/ve/ll + fin de palabra) NO abre literal;
# cualquier otra comilla sí, incluidos los prefijos de literal N'…', E'…', X'….
# Dentro de un literal se admite el apóstrofo ('d'Artagnan'): el valor queda oculto.
_RE_SQ = re.compile(
    r"(?!(?<=[^\W\d_])'(?i:[tsdm]|re|ve|ll)\b)'(?:[^'\n]|(?<=[^\W\d_])'(?=[^\W\d_])){0,500}'")
# Segunda pasada: una comilla de apóstrofo ("O'Brien", "users'") puede desplazar el
# emparejamiento y dejar el valor siguiente fuera de un literal ('***'secreto').
# Se oculta lo que quede pegado a un literal ya oculto y cerrado por otra comilla.
_RE_SQ_TAIL = re.compile(r"'\*\*\*'(?![\s.,;:)])[^'\n]{1,500}'(?!(?i:[tsdm]|re|ve|ll)\b)")
_RE_WS = re.compile(r"[ \t]{2,}")
_RE_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9\-]{1,63}(?:\.[A-Za-z0-9\-]{1,63}){1,8}")
_RE_LONG_DIGITS = re.compile(
    r"(?<![\w.])(?:\d{9,40}|\d{4}([ \-])\d{4}\1\d{4}\1\d{1,7}|\d{2,4}[ \-]\d{3,4}[ \-]\d{4})(?![\w.])")
_RE_BRACKET = re.compile(r"\[([^\[\]\n]{0,500})\]")
# Corchetes que SÍ se conservan: cadena de drivers ODBC y SQLSTATE (p. ej. [42S02]).
_RE_BRACKET_KEEP = re.compile(
    r"(?i)^(?:[0-9A-Z]{5}|microsoft|unixodbc|driver manager|sql server|odbc[^\]]{0,60}|"
    r"sql server native client[^\]]{0,20}|freetds|mysql|mariadb|postgresql|psqlodbc|firebird|pervasive|"
    r"ibm|oracle|informix|actian|zen|sql omitido|\*\*\*)$")

# Inicio de SQL: palabra clave + palabra que la confirma más adelante (búsqueda lineal).
_SQL_PAIRS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("select", ("from",)),
    ("insert", ("into",)),
    ("update", ("set",)),
    ("delete", ("from",)),
    ("with", ("as",)),
    ("merge", ("into", "using")),
    ("create", ("table", "index", "view", "schema", "function", "procedure", "trigger")),
    ("alter", ("table", "index", "view", "schema")),
    ("exec", ()),
    ("execute", ()),
)
_RE_WORD = re.compile(r"[A-Za-z_]+")


def _strip_sql(s: str) -> str:
    """Corta desde el primer inicio de SQL reconocible hasta el final (lineal)."""
    words = [(m.start(), m.group(0).lower()) for m in _RE_WORD.finditer(s)]
    for i, (pos, w) in enumerate(words):
        for kw, confirm in _SQL_PAIRS:
            if w != kw:
                continue
            if not confirm:
                # exec/execute seguido de un identificador
                if i + 1 < len(words) and words[i + 1][0] - (pos + len(w)) <= 3:
                    return s[:pos] + "[SQL omitido]"
                continue
            # la palabra de confirmación debe aparecer en las 60 palabras siguientes
            for _, w2 in words[i + 1:i + 61]:
                if w2 in confirm:
                    return s[:pos] + "[SQL omitido]"
    return s


def _bracket(m: "re.Match") -> str:
    return m.group(0) if _RE_BRACKET_KEEP.match(m.group(1).strip()) else "[***]"


def _paren_list(m: "re.Match") -> str:
    # Conserva el código numérico inicial (p. ej. MySQL "(1062, ...)").
    inner = m.group(0)[1:-1]
    code = re.match(r"\s{0,3}(\d{1,6})\s{0,3},", inner)
    return f"({code.group(1)}, ***)" if code else "(***)"


def _dq(m: "re.Match") -> str:
    if m.group(1):
        return m.group(0)  # identificador (tabla/columna/constraint...): se conserva
    return '"***"'


def redact_text(text: Optional[str], secrets: Optional[Iterable[str]] = None, max_len: int = 1000,
                strip_sql: bool = True, max_input: int = MAX_INPUT) -> str:
    if not text:
        return ""
    s = str(text)[:max_input]  # recorte ANTES de sanear (acota el costo)
    values: List[str] = sorted({str(v) for v in (secrets or []) if v and len(str(v)) >= 3}, key=len, reverse=True)
    for value in values:
        s = s.replace(value, "***")
    if strip_sql:
        s = _strip_sql(s)
    s = _RE_KEY_VALUES.sub(lambda m: f"Key ({m.group(1)})=(***)", s)
    s = _RE_FAILING_ROW.sub("Failing row contains (***)", s)
    s = _RE_PG_EXTRA_LINES.sub("", s)
    s = _RE_CARET.sub("", s)
    s = _RE_URL_CRED.sub(lambda m: f"{m.group(1)}***@***", s)
    s = _RE_KV.sub(lambda m: f"{m.group(1)}=***", s)
    s = _RE_EMAIL.sub("***@***", s)
    s = _RE_DQ.sub(_dq, s)
    s = _RE_SQ.sub("'***'", s)
    s = _RE_SQ_TAIL.sub("'***'***'", s)
    s = _RE_VALUE_LIST.sub(lambda m: f"{m.group(1)} (***)", s)
    s = _RE_PAREN_LIST.sub(_paren_list, s)
    s = _RE_BRACKET.sub(_bracket, s)
    s = _RE_IPV6_PAREN.sub("(***)", s)
    s = _RE_IPV4.sub("***", s)
    s = _RE_LONG_DIGITS.sub("***", s)
    s = _RE_PORT.sub("port ***", s)
    s = "\n".join(line.rstrip() for line in s.splitlines() if line.strip())
    s = _RE_WS.sub(" ", s)
    if len(s) > max_len:
        s = s[: max_len - 3] + "..."
    return s


def token_prefix(token: Optional[str]) -> str:
    """Prefijo corto para auditoría (nunca el token completo)."""
    return (token or "")[:8]
