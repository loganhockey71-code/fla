import Tabs from "@/components/Tabs";
import NowView from "@/components/views/NowView";
import PredictionsView from "@/components/views/PredictionsView";
import PerformanceView from "@/components/views/PerformanceView";
import LearningView from "@/components/views/LearningView";

export const dynamic = "force-dynamic";
const TABS = [{ key: "now", label: "Live signals" }, { key: "predictions", label: "Prediction history" }, { key: "accuracy", label: "Accuracy" }, { key: "learning", label: "Learning" }];

export default async function SignalsPage({ searchParams }: { searchParams: Record<string, string | undefined> }) {
  const tab = TABS.some((t) => t.key === searchParams.tab) ? searchParams.tab! : "now";
  return (
    <>
      <h1>Signals</h1>
      <p className="sub">What the AI says now, how its past calls turned out, and how it learns from mistakes. Paper trading only.</p>
      <Tabs base="/signals" current={tab} tabs={TABS} />
      {tab === "now" && <NowView searchParams={searchParams} />}
      {tab === "predictions" && <PredictionsView searchParams={searchParams} />}
      {tab === "accuracy" && <PerformanceView />}
      {tab === "learning" && <LearningView />}
    </>
  );
}
