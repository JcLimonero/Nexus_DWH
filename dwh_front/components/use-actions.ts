"use client";

import { useCallback, useState } from "react";
import { api } from "@/lib/api";
import { useConfirm, useToast } from "@/components/ui/feedback";

/** Ejecuta mutaciones con toasts y confirmación opcional. */
export function useActions(onDone?: () => void) {
  const toast = useToast();
  const confirm = useConfirm();
  const [busy, setBusy] = useState<string | null>(null);

  const run = useCallback(
    async <T,>(
      key: string,
      path: string,
      opts: { method?: string; body?: unknown; success?: string; confirm?: { title: string; message: React.ReactNode; confirmLabel?: string; danger?: boolean } } = {},
    ): Promise<T | null> => {
      if (opts.confirm && !(await confirm(opts.confirm))) return null;
      setBusy(key);
      try {
        const res = await api<T>(path, { method: opts.method || "POST", body: opts.body });
        if (opts.success) toast.success(opts.success);
        onDone?.();
        return res;
      } catch (e) {
        toast.error((e as Error).message);
        return null;
      } finally {
        setBusy(null);
      }
    },
    [confirm, toast, onDone],
  );

  return { run, busy };
}
