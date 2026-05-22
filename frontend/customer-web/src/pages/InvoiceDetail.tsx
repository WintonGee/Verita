import { useQuery } from '@tanstack/react-query';
import { Link, useParams } from 'react-router-dom';
import { apiFetch, ApiError } from '../lib/apiClient';
import { formatDate, formatMoney, formatUnitPrice, formatUnits } from '../lib/format';
import type { InvoiceDetail as InvoiceDetailType } from '../lib/types';
import { useMe } from '../hooks/useMe';
import { Header } from '../components/Header';
import { QueryBoundary } from '../components/QueryBoundary';
import { StatusBadge } from '../components/StatusBadge';

export function InvoiceDetail() {
  const { id } = useParams<{ id: string }>();
  const { data: me } = useMe();

  const query = useQuery({
    queryKey: ['invoice', id],
    queryFn: () => apiFetch<InvoiceDetailType>(`/v1/invoices/${id}`),
    enabled: !!id,
  });

  const is404 = query.error instanceof ApiError && query.error.status === 404;

  return (
    <div className="min-h-screen bg-gray-50">
      {me && <Header customer={me.customer} />}
      <main className="mx-auto max-w-5xl space-y-6 px-4 py-6">
        <Link
          to="/"
          className="inline-block text-sm font-medium text-blue-600 hover:underline"
        >
          ← Back to dashboard
        </Link>

        {is404 ? (
          <div className="rounded-md border border-gray-300 bg-white p-6 text-sm text-gray-600">
            This invoice doesn&apos;t exist or you don&apos;t have access to it.
          </div>
        ) : (
          <QueryBoundary
            isPending={query.isPending}
            isError={query.isError}
            error={query.error}
            data={query.data}
            onRetry={() => query.refetch()}
          >
            {(invoice) => (
              <>
                <section className="rounded-lg border border-gray-200 bg-white p-4">
                  <div className="flex items-start justify-between">
                    <div>
                      <h1 className="text-lg font-semibold text-gray-900">
                        Invoice
                      </h1>
                      <p className="font-mono text-xs text-gray-500">
                        {invoice.id}
                      </p>
                      <p className="mt-2 text-sm text-gray-700">
                        {formatDate(invoice.period_start)} –{' '}
                        {formatDate(invoice.period_end)}
                      </p>
                    </div>
                    <div className="text-right">
                      <StatusBadge status={invoice.status} />
                      <p className="mt-2 font-mono text-xl font-bold text-gray-900">
                        {formatMoney(
                          invoice.total_micro_cents,
                          invoice.currency,
                        )}
                      </p>
                    </div>
                  </div>
                  <dl className="mt-4 grid grid-cols-2 gap-2 text-xs text-gray-500 sm:grid-cols-3">
                    <div>
                      <dt className="font-medium">Issued</dt>
                      <dd>{formatDate(invoice.issued_at)}</dd>
                    </div>
                    <div>
                      <dt className="font-medium">Paid</dt>
                      <dd>{formatDate(invoice.paid_at)}</dd>
                    </div>
                    <div>
                      <dt className="font-medium">Currency</dt>
                      <dd>{invoice.currency}</dd>
                    </div>
                  </dl>
                </section>

                <section className="rounded-lg border border-gray-200 bg-white p-4">
                  <h2 className="mb-4 text-sm font-semibold text-gray-900">
                    Line items
                  </h2>
                  {invoice.line_items.length === 0 ? (
                    <div className="rounded-md border border-dashed border-gray-300 bg-gray-50 p-6 text-center text-sm text-gray-500">
                      No line items on this invoice.
                    </div>
                  ) : (
                    <div className="overflow-x-auto">
                      <table className="w-full text-left text-sm">
                        <thead>
                          <tr className="border-b border-gray-200 text-xs uppercase tracking-wide text-gray-500">
                            <th className="py-2 pr-4 font-medium">Kind</th>
                            <th className="py-2 pr-4 font-medium">
                              Description
                            </th>
                            <th className="py-2 pr-4 text-right font-medium">
                              Units
                            </th>
                            <th className="py-2 pr-4 text-right font-medium">
                              Unit price
                            </th>
                            <th className="py-2 pr-0 text-right font-medium">
                              Amount
                            </th>
                          </tr>
                        </thead>
                        <tbody>
                          {invoice.line_items.map((li) => (
                            <tr
                              key={li.id}
                              className="border-b border-gray-100 last:border-0 align-top"
                            >
                              <td className="py-2 pr-4 capitalize text-gray-700">
                                {li.kind.replace(/_/g, ' ')}
                              </td>
                              <td className="py-2 pr-4 text-gray-900">
                                {li.description}
                                {li.overridden_at && (
                                  <span
                                    className="ml-2 rounded bg-amber-100 px-1.5 py-0.5 text-[10px] font-medium text-amber-800"
                                    title={li.override_reason ?? undefined}
                                  >
                                    overridden
                                  </span>
                                )}
                              </td>
                              <td className="py-2 pr-4 text-right font-mono text-gray-700">
                                {li.units != null ? formatUnits(li.units) : '—'}
                              </td>
                              <td className="py-2 pr-4 text-right font-mono text-gray-700">
                                {li.unit_price_micro_cents != null
                                  ? formatUnitPrice(li.unit_price_micro_cents)
                                  : '—'}
                              </td>
                              <td className="py-2 pr-0 text-right font-mono text-gray-900">
                                {formatMoney(
                                  li.amount_micro_cents,
                                  invoice.currency,
                                )}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                </section>
              </>
            )}
          </QueryBoundary>
        )}
      </main>
    </div>
  );
}
