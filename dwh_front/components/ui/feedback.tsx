"use client";

import { createContext, useCallback, useContext, useRef, useState, type ReactNode } from "react";
import { AlertTriangle, CheckCircle2, Info, X, XCircle } from "lucide-react";
import { Button } from "./primitives";
import { Modal } from "./modal";
import { cx } from "@/lib/format";

// ── Toasts ────────────────────────────────────────────────────────────────
type ToastKind = "success" | "error" | "info";
interface ToastItem {
  id: number;
  kind: ToastKind;
  message: string;
}
interface ToastApi {
  success: (m: string) => void;
  error: (m: string) => void;
  info: (m: string) => void;
}

const ToastCtx = createContext<ToastApi | null>(null);

// ── Confirmación ──────────────────────────────────────────────────────────
interface ConfirmOptions {
  title: string;
  message: ReactNode;
  confirmLabel?: string;
  danger?: boolean;
}
const ConfirmCtx = createContext<((o: ConfirmOptions) => Promise<boolean>) | null>(null);

export function FeedbackProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<ToastItem[]>([]);
  const nextId = useRef(1);

  const push = useCallback((kind: ToastKind, message: string) => {
    const id = nextId.current++;
    setToasts((t) => [...t, { id, kind, message }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), kind === "error" ? 7000 : 4000);
  }, []);

  const toastApi = useRef<ToastApi>({
    success: (m) => push("success", m),
    error: (m) => push("error", m),
    info: (m) => push("info", m),
  });

  const [confirmState, setConfirmState] = useState<(ConfirmOptions & { resolve: (v: boolean) => void }) | null>(null);
  const confirm = useCallback(
    (o: ConfirmOptions) => new Promise<boolean>((resolve) => setConfirmState({ ...o, resolve })),
    [],
  );
  const close = (v: boolean) => {
    confirmState?.resolve(v);
    setConfirmState(null);
  };

  const icons = {
    success: <CheckCircle2 className="h-5 w-5 text-emerald-500" />,
    error: <XCircle className="h-5 w-5 text-red-500" />,
    info: <Info className="h-5 w-5 text-brand-500" />,
  };

  return (
    <ToastCtx.Provider value={toastApi.current}>
      <ConfirmCtx.Provider value={confirm}>
        {children}
        <div className="pointer-events-none fixed inset-x-0 top-3 z-[60] flex flex-col items-center gap-2 px-4 sm:items-end sm:right-4 sm:left-auto" aria-live="polite">
          {toasts.map((t) => (
            <div
              key={t.id}
              className={cx(
                "pointer-events-auto flex w-full max-w-sm items-start gap-3 rounded-lg border bg-white p-3 shadow-lg",
                t.kind === "error" ? "border-red-200" : "border-slate-200",
              )}
            >
              {icons[t.kind]}
              <p className="flex-1 text-sm text-slate-700">{t.message}</p>
              <button onClick={() => setToasts((x) => x.filter((y) => y.id !== t.id))} className="text-slate-400 hover:text-slate-600" aria-label="Cerrar">
                <X className="h-4 w-4" />
              </button>
            </div>
          ))}
        </div>
        <Modal
          open={Boolean(confirmState)}
          onClose={() => close(false)}
          title={confirmState?.title || ""}
          size="sm"
          footer={
            <>
              <Button variant="secondary" onClick={() => close(false)}>
                Cancelar
              </Button>
              <Button variant={confirmState?.danger ? "danger" : "primary"} onClick={() => close(true)} data-autofocus>
                {confirmState?.confirmLabel || "Confirmar"}
              </Button>
            </>
          }
        >
          <div className="flex gap-3">
            {confirmState?.danger && <AlertTriangle className="h-5 w-5 shrink-0 text-red-500" />}
            <div className="text-sm text-slate-600">{confirmState?.message}</div>
          </div>
        </Modal>
      </ConfirmCtx.Provider>
    </ToastCtx.Provider>
  );
}

export function useToast(): ToastApi {
  const ctx = useContext(ToastCtx);
  if (!ctx) throw new Error("useToast fuera de FeedbackProvider");
  return ctx;
}

export function useConfirm() {
  const ctx = useContext(ConfirmCtx);
  if (!ctx) throw new Error("useConfirm fuera de FeedbackProvider");
  return ctx;
}
