import type { ReactNode } from 'react';
import { Navigate } from 'react-router-dom';
import { useMe } from '../lib/auth';

/**
 * Gate authenticated routes on GET /ops/auth/me. While the bootstrap query is
 * loading we show nothing; on error (401 etc.) we redirect to /login.
 */
export function RequireAuth({ children }: { children: ReactNode }) {
  const me = useMe();

  if (me.isLoading) {
    return (
      <div className="py-16 text-center text-sm text-gray-500">Loading…</div>
    );
  }

  if (me.isError || !me.data) {
    return <Navigate to="/login" replace />;
  }

  return <>{children}</>;
}
