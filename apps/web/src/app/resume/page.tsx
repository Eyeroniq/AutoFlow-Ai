import type { Metadata } from "next";

import { ResumeRefinerScreen } from "@/features/resume/resume-refiner";

export const metadata: Metadata = { title: "Resume refiner" };

export default function ResumePage() {
  return <ResumeRefinerScreen />;
}
