"""
ratelimit.py — Límites de tasa en memoria (por proceso)
───────────────────────────────────────────────────────
Ventana deslizante por clave (IP, prefijo de token, usuario...). Se usa para:
  * inicio de sesión del panel (por IP y por usuario inexistente; el bloqueo de
    usuarios existentes se guarda en BD: panel_user.failed_attempts/locked_until);
  * POST /agent/enroll (por IP y por prefijo del token de enrolamiento);
  * ráfagas de credenciales de instalación inválidas (por IP).

Es memoria del proceso: con varias réplicas el límite es por réplica (documentado).
El número de claves está acotado (se descartan las más antiguas).
"""

import threading
import time
from collections import OrderedDict, deque
from typing import Deque, Optional


class SlidingWindowLimiter:
    def __init__(self, limit: int, window_seconds: float, max_keys: int = 20_000) -> None:
        self.limit = max(1, int(limit))
        self.window = float(window_seconds)
        self.max_keys = max_keys
        self._hits: "OrderedDict[str, Deque[float]]" = OrderedDict()
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> Deque[float]:
        q = self._hits.get(key)
        if q is None:
            q = deque()
            self._hits[key] = q
            while len(self._hits) > self.max_keys:
                self._hits.popitem(last=False)
        else:
            self._hits.move_to_end(key)
        cutoff = now - self.window
        while q and q[0] <= cutoff:
            q.popleft()
        return q

    def blocked(self, key: str) -> Optional[float]:
        """Segundos hasta que se libere un cupo si la clave está en el límite; None si no."""
        if not key:
            return None
        now = time.monotonic()
        with self._lock:
            q = self._prune(key, now)
            if len(q) >= self.limit:
                return max(1.0, q[0] + self.window - now)
            return None

    def hit(self, key: str) -> None:
        if not key:
            return
        now = time.monotonic()
        with self._lock:
            self._prune(key, now).append(now)

    def hit_and_check(self, key: str) -> Optional[float]:
        """Registra un intento y devuelve la espera si con él se superó el límite."""
        if not key:
            return None
        now = time.monotonic()
        with self._lock:
            q = self._prune(key, now)
            if len(q) >= self.limit:
                return max(1.0, q[0] + self.window - now)
            q.append(now)
            return None

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._hits.clear()


class BackoffLock:
    """
    Bloqueo con backoff exponencial por clave (para usuarios INEXISTENTES, con las
    mismas reglas que los existentes en BD: así un 429 no revela si el usuario existe).
    """

    def __init__(self, max_failures: int, base_seconds: float, max_seconds: float, max_keys: int = 20_000) -> None:
        self.max_failures = max(1, int(max_failures))
        self.base = float(base_seconds)
        self.max = float(max_seconds)
        self.max_keys = max_keys
        self._state: "OrderedDict[str, list]" = OrderedDict()  # key -> [failures, locked_until]
        self._lock = threading.Lock()

    def locked_for(self, key: str) -> Optional[float]:
        now = time.monotonic()
        with self._lock:
            st = self._state.get(key)
            if st and st[1] > now:
                return st[1] - now
            return None

    def fail(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            st = self._state.get(key)
            if st is None:
                st = [0, 0.0]
                self._state[key] = st
                while len(self._state) > self.max_keys:
                    self._state.popitem(last=False)
            st[0] += 1
            if st[0] >= self.max_failures:
                st[1] = now + lock_seconds(st[0], self.max_failures, self.base, self.max)

    def reset(self, key: str) -> None:
        with self._lock:
            self._state.pop(key, None)


def lock_seconds(failures: int, max_failures: int, base: float, maximum: float) -> float:
    """Duración del bloqueo tras `failures` fallos: base × 2^(fallos − máximo), con tope."""
    if failures < max_failures:
        return 0.0
    return float(min(maximum, base * (2 ** min(30, failures - max_failures))))
