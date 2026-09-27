"use client";

import { useState } from "react";
import { Eye, EyeOff } from "lucide-react";
import { Input } from "@/components/ui/primitives";

/** Campo de contraseña de solo escritura: el valor actual nunca se muestra. */
export function PasswordInput({
  id,
  value,
  onChange,
  hasPassword,
  isEdit,
}: {
  id?: string;
  value: string;
  onChange: (v: string) => void;
  hasPassword: boolean;
  isEdit: boolean;
}) {
  const [show, setShow] = useState(false);
  return (
    <div className="relative">
      <Input
        id={id}
        type={show ? "text" : "password"}
        autoComplete="new-password"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={isEdit && hasPassword ? "•••••••• (sin cambios)" : isEdit ? "Sin contraseña guardada" : ""}
        className="pr-10"
      />
      <button
        type="button"
        onClick={() => setShow((s) => !s)}
        className="absolute inset-y-0 right-0 flex w-9 items-center justify-center text-slate-400 hover:text-slate-600"
        aria-label={show ? "Ocultar" : "Mostrar"}
        tabIndex={-1}
      >
        {show ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
      </button>
    </div>
  );
}

export function SecretNotice({ encrypted, errors }: { encrypted: string[]; errors: string[] }) {
  if (errors.length > 0) {
    return (
      <div className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
        No se pudieron descifrar: <b>{errors.join(", ")}</b>. Déjalos vacíos para conservar el valor actual o escribe uno nuevo.
      </div>
    );
  }
  if (encrypted.length > 0) {
    return (
      <p className="text-xs text-slate-500">
        Credenciales guardadas cifradas (ENC:) en la BD de configuración.
      </p>
    );
  }
  return null;
}
