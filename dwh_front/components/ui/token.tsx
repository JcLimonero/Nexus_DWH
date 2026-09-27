"use client";

import { useState } from "react";
import { Copy, Eye, EyeOff, KeyRound, RefreshCw, Ban } from "lucide-react";
import { IconButton } from "./primitives";
import { useToast } from "./feedback";

export async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

/** Muestra un token enmascarado con acciones mostrar / copiar / regenerar / revocar. */
export function TokenField({
  token,
  onRegenerate,
  onRevoke,
  emptyLabel = "Sin token",
}: {
  token: string | null;
  onRegenerate?: () => void;
  onRevoke?: () => void;
  emptyLabel?: string;
}) {
  const [visible, setVisible] = useState(false);
  const toast = useToast();

  if (!token) {
    return (
      <div className="flex items-center gap-1">
        <span className="text-xs italic text-slate-400">{emptyLabel}</span>
        {onRegenerate && (
          <IconButton label="Generar token" onClick={onRegenerate}>
            <KeyRound className="h-4 w-4" />
          </IconButton>
        )}
      </div>
    );
  }

  return (
    <div className="flex items-center gap-0.5">
      <code className="max-w-[11rem] truncate rounded bg-slate-100 px-1.5 py-0.5 font-mono text-xs text-slate-700 sm:max-w-[16rem]" title={visible ? token : undefined}>
        {visible ? token : `${token.slice(0, 6)}••••••••••`}
      </code>
      <IconButton label={visible ? "Ocultar token" : "Mostrar token"} onClick={() => setVisible((v) => !v)}>
        {visible ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
      </IconButton>
      <IconButton
        label="Copiar token"
        onClick={async () => ((await copyText(token)) ? toast.success("Token copiado al portapapeles.") : toast.error("No se pudo copiar."))}
      >
        <Copy className="h-4 w-4" />
      </IconButton>
      {onRegenerate && (
        <IconButton label="Regenerar token" onClick={onRegenerate}>
          <RefreshCw className="h-4 w-4" />
        </IconButton>
      )}
      {onRevoke && (
        <IconButton label="Revocar token" tone="danger" onClick={onRevoke}>
          <Ban className="h-4 w-4" />
        </IconButton>
      )}
    </div>
  );
}
