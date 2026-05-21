import { useState } from 'react';
import { Link } from 'react-router-dom';
import { useQuery, keepPreviousData } from '@tanstack/react-query';
import { apiFetch } from '../lib/apiClient';
import { QueryBoundary } from '../components/QueryBoundary';
import { AppHeader } from '../components/AppHeader';
import type { CustomerListResponse } from '../types';

const LIMIT = 25;
const STATUS_OPTIONS = ['', 'active', 'suspended', 'closed'];

export function CustomersPage() {
  const [qInput, setQInput] = useState('');
  const [q, setQ] = useState('');
  const [status, setStatus] = useState('');
  const [page, setPage] = useState(1);

  const query = useQuery({
    queryKey: ['customers', { q, status, page }],
    queryFn: () => {
      const params = new URLSearchParams();
      if (q) params.set('q', q);
      if (status) params.set('status', status);
      params.set('page', String(page));
      params.set('limit', String(LIMIT));
      return apiFetch<CustomerListResponse>(`/ops/customers?${params}`);
    },
    placeholderData: keepPreviousData,
  });

  function applySearch(e: React.FormEvent) {
    e.preventDefault();
    setPage(1);
    setQ(qInput.trim());
  }

  return (
    <div className="min-h-screen bg-gray-50">
      <AppHeader />
      <main className="mx-auto max-w-5xl px-4 py-6">
        <h1 className="mb-4 text-2xl font-semibold text-gray-900">Customers</h1>

        <form onSubmit={applySearch} className="mb-4 flex flex-wrap gap-2">
          <input
            type="text"
            value={qInput}
            onChange={(e) => setQInput(e.target.value)}
            placeholder="Search name or email…"
            className="flex-1 rounded border border-gray-300 px-3 py-2 text-sm outline-none focus:border-blue-500"
          />
          <select
            value={status}
            onChange={(e) => {
              setStatus(e.target.value);
              setPage(1);
            }}
            className="rounded border border-gray-300 px-3 py-2 text-sm"
          >
            {STATUS_OPTIONS.map((s) => (
              <option key={s || 'all'} value={s}>
                {s === '' ? 'All statuses' : s}
              </option>
            ))}
          </select>
          <button
            type="submit"
            className="rounded bg-blue-700 px-4 py-2 text-sm font-semibold text-white hover:bg-blue-800"
          >
            Search
          </button>
        </form>

        <QueryBoundary query={query} loadingLabel="Loading customers…">
          {(data) => {
            if (data.data.length === 0) {
              return (
                <div className="rounded border border-gray-200 bg-white p-8 text-center text-sm text-gray-500">
                  No customers.
                </div>
              );
            }
            const totalPages = Math.max(1, Math.ceil(data.total / data.limit));
            return (
              <>
                <div className="overflow-x-auto rounded border border-gray-200 bg-white">
                  <table className="w-full text-left text-sm">
                    <thead className="border-b bg-gray-50 text-xs uppercase text-gray-500">
                      <tr>
                        <th className="px-4 py-2">Name</th>
                        <th className="px-4 py-2">Email</th>
                        <th className="px-4 py-2">Status</th>
                        <th className="px-4 py-2">Plan</th>
                        <th className="px-4 py-2"></th>
                      </tr>
                    </thead>
                    <tbody>
                      {data.data.map((c) => (
                        <tr key={c.id} className="border-b last:border-0">
                          <td className="px-4 py-2 font-medium text-gray-900">
                            {c.name}
                          </td>
                          <td className="px-4 py-2 text-gray-600">
                            {c.billing_email}
                          </td>
                          <td className="px-4 py-2">
                            <StatusBadge status={c.status} />
                          </td>
                          <td className="px-4 py-2 text-gray-600">
                            {c.plan_name}
                          </td>
                          <td className="px-4 py-2 text-right">
                            <Link
                              to={`/customers/${c.id}`}
                              className="text-blue-700 hover:underline"
                            >
                              Open
                            </Link>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>

                <div className="mt-4 flex items-center justify-between text-sm text-gray-600">
                  <span>
                    {data.total} customer{data.total === 1 ? '' : 's'} · page{' '}
                    {data.page} of {totalPages}
                  </span>
                  <div className="flex gap-2">
                    <button
                      type="button"
                      onClick={() => setPage((p) => Math.max(1, p - 1))}
                      disabled={page <= 1}
                      className="rounded border border-gray-300 bg-white px-3 py-1 disabled:opacity-50"
                    >
                      Previous
                    </button>
                    <button
                      type="button"
                      onClick={() => setPage((p) => p + 1)}
                      disabled={page >= totalPages}
                      className="rounded border border-gray-300 bg-white px-3 py-1 disabled:opacity-50"
                    >
                      Next
                    </button>
                  </div>
                </div>
              </>
            );
          }}
        </QueryBoundary>
      </main>
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
