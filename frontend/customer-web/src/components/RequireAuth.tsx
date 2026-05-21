import type { ReactNode } from 'react';
import { Navigate } from 'react-router-dom';
import { useMe } from '../hooks/useMe';

/**
 * Route guard. Auth state is derived from the /v1/me query.
 * - while the initial /v1/me fetch is pending → show a spinner
 * - on error (e.g. 401, no session) → redirect to /login
 * - otherwise → render the protected route
 */
export function RequireAuth({ children }: { children: ReactNode }) {
  const { isPending, isError, data } = useMe();

  if (isPending) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-gray-50">
        <div
          role="status"
          aria-label="Loading"
          className="h-6 w-6 rounded-full border-2 border-gray-300 border-t-gray-700 animate-spin"
        />
      </div>
    );
  }

  if (isError || !data) {
    return <Navigate to="/login" replace />;
  }

  return <>{children}</>;
}
