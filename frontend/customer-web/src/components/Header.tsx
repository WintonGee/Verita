import { useNavigate } from 'react-router-dom';
import { useQueryClient } from '@tanstack/react-query';
import { apiFetch } from '../lib/apiClient';
import type { Customer } from '../lib/types';

interface HeaderProps {
  customer: Customer;
}

export function Header({ customer }: HeaderProps) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  async function handleSignOut() {
    try {
      await apiFetch<void>('/v1/auth/logout', { method: 'POST' });
    } catch {
      // Even if logout fails server-side, clear local state and bounce.
    } finally {
      queryClient.clear();
      navigate('/login', { replace: true });
    }
  }

  return (
    <header className="border-b border-gray-200 bg-white">
      <div className="mx-auto flex max-w-5xl items-center justify-between px-4 py-3">
        <div>
          <h1 className="text-lg font-semibold text-gray-900">
            {customer.name}
          </h1>
          <p className="text-xs text-gray-500">
            Plan: {customer.price_plan?.name ?? '—'}
          </p>
        </div>
        <button
          type="button"
          onClick={handleSignOut}
          className="rounded border border-gray-300 px-3 py-1.5 text-sm font-medium text-gray-700 hover:bg-gray-50"
        >
          Sign out
        </button>
      </div>
    </header>
  );
}
