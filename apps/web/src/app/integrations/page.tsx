import type { Metadata } from "next";

import { IntegrationsScreen } from "@/features/integrations/integrations";

export const metadata: Metadata = { title: "Integrations" };

export default function IntegrationsPage() {
  return <IntegrationsScreen />;
}
