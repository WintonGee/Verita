import { useEffect, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { apiFetch } from '../lib/apiClient';
import { QueryBoundary } from '../components/QueryBoundary';
import { AppHeader } from '../components/AppHeader';
import { IssueCreditModal } from '../components/IssueCreditModal';
import { OverrideLineItemModal } from '../components/OverrideLineItemModal';
import { formatUSD } from '../lib/money';
import type {
  ApiKey,
  CustomerDetail,
  Invoice,
  InvoiceLineItem,
} from '../types';

type OverrideTarget = { invoice: Invoice; lineItem: InvoiceLineItem };

export function CustomerDetailPage() {
  const { id = '' } = useParams();
  const [creditOpen, setCreditOpen] = useState(false);
  const [overrideTarget, setOverrideTarget] = useState<OverrideTarget | null>(
    null,
  );
  const [toast, setToast] = useState<string | null>(null);

  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(null), 5000);
    return () => clearTimeout(t);
  }, [toast]);

  const query = useQuery({
    queryKey: ['customer', id],
    queryFn: () => apiFetch<CustomerDetail>(`/ops/customers/${id}`),
  });

  return (
    <div className="min-h-screen bg-gray-50">
      <AppHeader />
      <main className="mx-auto max-w-5xl px-4 py-6">
        <Link
          to="/customers"
          className="text-sm text-blue-700 hover:underline"
        >
          ← Back to customers
        </Link>

        {toast && (
          <div
            className="mt-4 rounded border border-green-300 bg-green-50 p-3 text-sm text-green-800"
            role="status"
          >
            {toast}
          </div>
        )}

        <QueryBoundary query={query} loadingLabel="Loading customer…">
          {(c) => (
            <div className="mt-4 space-y-6">
              {/* Header */}
              <section className="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <h1 className="text-2xl font-semibold text-gray-900">
                    {c.name}
                  </h1>
                  <div className="mt-1 text-sm text-gray-600">
                    {c.billing_email}
                  </div>
                  <div className="mt-2 flex items-center gap-2 text-sm">
                    <StatusBadge status={c.status} />
                    <span className="text-gray-500">·</span>
                    <span className="text-gray-700">{c.price_plan.name}</span>
                  </div>
                </div>
                <button
                  type="button"
                  onClick={() => setCreditOpen(true)}
                  className="rounded bg-green-700 px-4 py-2 text-sm font-semibold text-white hover:bg-green-800"
                >
                  Issue credit
                </button>
              </section>

              {/* Current period card */}
              <CurrentPeriodCard period={c.current_period} />

              {/* Invoices */}
              <section>
                <h2 className="mb-2 text-lg font-semibold text-gray-900">
                  Invoices
                </h2>
                <InvoicesTable
                  invoices={c.invoices}
                  onOverride={(invoice, lineItem) =>
                    setOverrideTarget({ invoice, lineItem })
                  }
                />
              </section>

              {/* API keys */}
              <section>
                <h2 className="mb-2 text-lg font-semibold text-gray-900">
                  API keys
                </h2>
                <ApiKeysTable keys={c.api_keys} />
              </section>

              {creditOpen && (
                <IssueCreditModal
                  customerId={c.id}
                  customerName={c.name}
                  onClose={() => setCreditOpen(false)}
                  onSuccess={setToast}
                />
              )}

              {overrideTarget && (
                <OverrideLineItemModal
                  customerId={c.id}
                  invoiceId={overrideTarget.invoice.id}
                  lineItem={overrideTarget.lineItem}
                  onClose={() => setOverrideTarget(null)}
                  onSuccess={setToast}
                />
              )}
            </div>
          )}
        </QueryBoundary>
      </main>
    </div>
  );
}

function CurrentPeriodCard({
  period,
}: {
  period: CustomerDetail['current_period'];
}) {
  return (
    <section className="rounded border border-gray-200 bg-white p-4">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-lg font-semibold text-gray-900">Current period</h2>
        {period.anomaly && (
          <span className="rounded bg-red-100 px-2 py-1 text-sm font-semibold text-red-800">
            ⚠ Anomaly — today &gt; {period.multiplier_threshold}× baseline
          </span>
        )}
      </div>
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-3">
        <Stat label="Today (units)" value={period.today_units.toLocaleString()} />
        <Stat
          label="30-day daily avg"
          value={period.thirty_day_daily_avg.toLocaleString()}
        />
        <Stat
          label="Anomaly threshold"
          value={`${period.multiplier_threshold}×`}
        />
      </div>
      {/* Simple visual ratio bar (no chart lib) */}
      <UsageBar
        today={period.today_units}
        avg={period.thirty_day_daily_avg}
        anomaly={period.anomaly}
      />
    </section>
  );
}

function UsageBar({
  today,
  avg,
  anomaly,
}: {
  today: number;
  avg: number;
  anomaly: boolean;
}) {
  const max = Math.max(today, avg, 1);
  const todayPct = Math.round((today / max) * 100);
  const avgPct = Math.round((avg / max) * 100);
  return (
    <div className="mt-4 space-y-2 text-xs text-gray-600">
      <div>
        <div className="mb-0.5">Today</div>
        <div className="h-3 w-full rounded bg-gray-100">
          <div
            className={`h-3 rounded ${anomaly ? 'bg-red-500' : 'bg-blue-500'}`}
            style={{ width: `${todayPct}%` }}
          />
        </div>
      </div>
      <div>
        <div className="mb-0.5">30-day avg</div>
        <div className="h-3 w-full rounded bg-gray-100">
          <div
            className="h-3 rounded bg-gray-400"
            style={{ width: `${avgPct}%` }}
          />
        </div>
      </div>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-xs uppercase text-gray-500">{label}</div>
      <div className="text-xl font-semibold text-gray-900">{value}</div>
    </div>
  );
}

function InvoicesTable({
  invoices,
  onOverride,
}: {
  invoices: Invoice[];
  onOverride: (invoice: Invoice, lineItem: InvoiceLineItem) => void;
}) {
  if (invoices.length === 0) {
    return (
      <div className="rounded border border-gray-200 bg-white p-6 text-center text-sm text-gray-500">
        No invoices yet.
      </div>
    );
  }
  return (
    <div className="space-y-3">
      {invoices.map((inv) => (
        <div
          key={inv.id}
          className="overflow-hidden rounded border border-gray-200 bg-white"
        >
          <div className="flex flex-wrap items-center justify-between gap-2 border-b bg-gray-50 px-4 py-2 text-sm">
            <div className="text-gray-700">
              {fmtDate(inv.period_start)} – {fmtDate(inv.period_end)}
            </div>
            <div className="flex items-center gap-3">
              <InvoiceStatusBadge status={inv.status} />
              <span className="font-semibold text-gray-900">
                {formatUSD(inv.total_micro_cents)} {inv.currency}
              </span>
            </div>
          </div>

          {inv.line_items && inv.line_items.length > 0 ? (
            <table className="w-full text-left text-sm">
              <thead className="text-xs uppercase text-gray-400">
                <tr>
                  <th className="px-4 py-1">Description</th>
                  <th className="px-4 py-1 text-right">Amount</th>
                  <th className="px-4 py-1"></th>
                </tr>
              </thead>
              <tbody>
                {inv.line_items.map((li) => (
                  <tr key={li.id} className="border-t">
                    <td className="px-4 py-2 text-gray-700">
                      {li.description}
                      {li.overridden_at && (
                        <span className="ml-2 rounded bg-amber-100 px-1.5 py-0.5 text-xs text-amber-800">
                          overridden
                        </span>
                      )}
                    </td>
                    <td className="px-4 py-2 text-right text-gray-900">
                      {formatUSD(li.amount_micro_cents)}
                    </td>
                    <td className="px-4 py-2 text-right">
                      {inv.status === 'paid' ? (
                        <span
                          className="cursor-not-allowed text-xs text-gray-400"
                          title="Cannot override a paid invoice"
                        >
                          Override
                        </span>
                      ) : (
                        <button
                          type="button"
                          onClick={() => onOverride(inv, li)}
                          className="text-xs text-amber-700 hover:underline"
                        >
                          Override
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <div className="px-4 py-3 text-xs text-gray-400">
              Line items not available for this invoice.
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

function ApiKeysTable({ keys }: { keys: ApiKey[] }) {
  if (keys.length === 0) {
    return (
      <div className="rounded border border-gray-200 bg-white p-6 text-center text-sm text-gray-500">
        No API keys.
      </div>
    );
  }
  return (
    <div className="overflow-x-auto rounded border border-gray-200 bg-white">
      <table className="w-full text-left text-sm">
        <thead className="border-b bg-gray-50 text-xs uppercase text-gray-500">
          <tr>
            <th className="px-4 py-2">Prefix</th>
            <th className="px-4 py-2">Name</th>
            <th className="px-4 py-2">Last used</th>
            <th className="px-4 py-2">Status</th>
          </tr>
        </thead>
        <tbody>
          {keys.map((k) => (
            <tr key={k.id} className="border-b last:border-0">
              <td className="px-4 py-2 font-mono text-gray-700">{k.prefix}</td>
              <td className="px-4 py-2 text-gray-700">{k.name}</td>
              <td className="px-4 py-2 text-gray-600">
                {k.last_used_at ? fmtDate(k.last_used_at) : '—'}
              </td>
              <td className="px-4 py-2">
                {k.revoked_at ? (
                  <span className="rounded bg-gray-200 px-2 py-0.5 text-xs text-gray-700">
                    revoked {fmtDate(k.revoked_at)}
                  </span>
                ) : (
                  <span className="rounded bg-green-100 px-2 py-0.5 text-xs text-green-800">
                    active
                  </span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function StatusBadge({ status }: { status: string }) {
  const color =
    status === 'active'
      ? 'bg-green-100 text-green-800'
      : status === 'suspended'
        ? 'bg-amber-100 text-amber-800'
        : status === 'closed'
          ? 'bg-gray-200 text-gray-700'
          : 'bg-gray-100 text-gray-700';
  return (
    <span className={`rounded px-2 py-0.5 text-xs font-medium ${color}`}>
      {status}
    </span>
  );
}

function InvoiceStatusBadge({ status }: { status: string }) {
  const color =
    status === 'paid'
      ? 'bg-green-100 text-green-800'
      : status === 'issued'
        ? 'bg-blue-100 text-blue-800'
        : status === 'void'
          ? 'bg-gray-200 text-gray-700'
          : 'bg-gray-100 text-gray-700';
  return (
    <span className={`rounded px-2 py-0.5 text-xs font-medium ${color}`}>
      {status}
    </span>
  );
}

function fmtDate(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString('en-US', {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
  });
}
