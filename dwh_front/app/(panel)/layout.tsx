import { Suspense } from "react";
import { Shell } from "@/components/sidebar";
import { FeedbackProvider } from "@/components/ui/feedback";
import { SessionProvider } from "@/components/session";

export default function PanelLayout({ children }: { children: React.ReactNode }) {
  return (
    <FeedbackProvider>
      <SessionProvider>
        <Shell>
          {/* Suspense: las páginas leen sus filtros de la URL (useSearchParams). */}
          <Suspense>{children}</Suspense>
        </Shell>
      </SessionProvider>
    </FeedbackProvider>
  );
}
