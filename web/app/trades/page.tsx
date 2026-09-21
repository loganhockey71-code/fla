import Tabs from "@/components/Tabs";
import MyTradingView from "@/components/views/MyTradingView";
import AiTradesView from "@/components/views/AiTradesView";

export const dynamic = "force-dynamic";
const TABS = [{ key: "mine", label: "My trading" }, { key: "ai", label: "AI test account" }];

export default async function TradesPage({ searchParams }: { searchParams: Record<string, string | undefined> }) {
  const tab = TABS.some((t) => t.key === searchParams.tab) ? searchParams.tab! : "mine";
  return (
    <>
      <h1>Trades</h1>
      <Tabs base="/trades" current={tab} tabs={TABS} />
      {tab === "mine" ? <MyTradingView /> : <AiTradesView />}
    </>
  );
}
