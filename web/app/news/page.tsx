import EventsView from "@/components/views/EventsView";

export const dynamic = "force-dynamic";

export default async function NewsPage({ searchParams }: { searchParams: Record<string, string | undefined> }) {
  return (
    <>
      <h1>News</h1>
      <EventsView searchParams={searchParams} />
    </>
  );
}
