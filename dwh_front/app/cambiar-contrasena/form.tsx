"use client";

import { useEffect, useState, type FormEvent } from "react";
import { KeyRound } from "lucide-react";
import { api } from "@/lib/api";
import { logout } from "@/components/session";
import { Button, Input } from "@/components/ui/primitives";

function PasswordInput(props: { id: string; autoComplete: string; value: string; onChange: (e: React.ChangeEvent<HTMLInputElement>) => void }) {
  return <Input type="password" maxLength={256} {...props} />;
}

interface MeLite {
  user: { username: string; display_name: string };
  must_change_password: boolean;
  password_min_length?: number;
}

/** Cambio de contraseña (obligatorio si el administrador la reinició o es el primer ingreso). */
export default function ChangePasswordForm() {
  const [me, setMe] = useState<MeLite | null>(null);
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [repeat, setRepeat] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [done, setDone] = useState(false);

  useEffect(() => {
    api<MeLite>("admin/auth/me").then(setMe).catch(() => undefined);
  }, []);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    if (next !== repeat) {
      setError("Las contraseñas nuevas no coinciden.");
      return;
    }
    setLoading(true);
    try {
      await api("admin/auth/change-password", { method: "POST", body: { current_password: current, new_password: next } });
      setDone(true);
      setCurrent("");
      setNext("");
      setRepeat("");
      setTimeout(() => (window.location.href = "/"), 900);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="w-full max-w-sm">
      <form onSubmit={onSubmit} className="rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
        <div className="mb-4 flex items-center gap-2">
          <KeyRound className="h-5 w-5 text-brand-600" />
          <h1 className="text-lg font-semibold text-slate-900">Cambiar contraseña</h1>
        </div>
        {me?.must_change_password && (
          <p className="mb-4 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
            Debe definir una contraseña nueva antes de continuar{me ? `, ${me.user.display_name}` : ""}.
          </p>
        )}
        <label className="mb-1 block text-sm font-medium text-slate-700" htmlFor="cur">
          Contraseña actual
        </label>
        <PasswordInput id="cur" autoComplete="current-password" value={current} onChange={(e) => setCurrent(e.target.value)} />
        <label className="mb-1 mt-4 block text-sm font-medium text-slate-700" htmlFor="new">
          Contraseña nueva
        </label>
        <PasswordInput id="new" autoComplete="new-password" value={next} onChange={(e) => setNext(e.target.value)} />
        <label className="mb-1 mt-4 block text-sm font-medium text-slate-700" htmlFor="rep">
          Repita la contraseña nueva
        </label>
        <PasswordInput id="rep" autoComplete="new-password" value={repeat} onChange={(e) => setRepeat(e.target.value)} />
        <p className="mt-2 text-[11px] text-slate-500">
          Mínimo {me?.password_min_length ?? 8} caracteres; no puede contener su usuario ni ser una contraseña común. Al cambiarla se cierran sus demás sesiones.
        </p>
        {error && (
          <div className="mt-4 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700" role="alert">
            {error}
          </div>
        )}
        {done && (
          <div className="mt-4 rounded-md border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-700" role="status">
            Contraseña actualizada.
          </div>
        )}
        <Button type="submit" className="mt-5 w-full" loading={loading} disabled={!current || !next || !repeat}>
          Guardar contraseña
        </Button>
        <button type="button" onClick={() => void logout()} className="mt-3 w-full text-center text-xs text-slate-500 hover:text-slate-700">
          Cerrar sesión
        </button>
      </form>
    </div>
  );
}
