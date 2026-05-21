import { useMe } from '../hooks/useMe';
import { Header } from '../components/Header';
import { UsageChart } from '../components/UsageChart';
import { CurrentPeriodTile } from '../components/CurrentPeriodTile';
import { InvoiceTable } from '../components/InvoiceTable';

export function Dashboard() {
  // The route guard already ensures we're authenticated; this read is cached.
  const { data: me } = useMe();

  return (
    <div className="min-h-screen bg-gray-50">
      {me && <Header customer={me.customer} />}
      <main className="mx-auto max-w-5xl space-y-6 px-4 py-6">
        <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
          <div className="lg:col-span-2">
            <UsageChart />
          </div>
          <div className="lg:col-span-1">
            <CurrentPeriodTile />
          </div>
        </div>
        <InvoiceTable />
      </main>
    </div>
  );
}
