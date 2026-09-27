import type { Metadata } from "next";
import ChangePasswordForm from "./form";

export const metadata: Metadata = { title: "Cambiar contraseña" };

export default function ChangePasswordPage() {
  return (
    <main className="flex min-h-screen items-center justify-center bg-gradient-to-br from-slate-50 to-brand-50 px-4">
      <ChangePasswordForm />
    </main>
  );
}
