import { Shell } from "@/components/sidebar";
import { FeedbackProvider } from "@/components/ui/feedback";

export default function PanelLayout({ children }: { children: React.ReactNode }) {
  return (
    <FeedbackProvider>
      <Shell>{children}</Shell>
    </FeedbackProvider>
  );
}
