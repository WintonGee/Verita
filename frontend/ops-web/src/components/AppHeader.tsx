import { Link, useNavigate } from 'react-router-dom';
import { useQueryClient } from '@tanstack/react-query';
import { logout, useMe } from '../lib/auth';

/** Top bar shown on authenticated pages: branding + current user + sign out. */
export function AppHeader() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const me = useMe();

  async function handleLogout() {
    try {
      await logout();
    } catch {
      // ignore — clear local state regardless
    }
    queryClient.clear();
    navigate('/login', { replace: true });
  }

  return (
    <header className="border-b bg-white">
      <div className="mx-auto flex max-w-5xl items-center justify-between px-4 py-3">
        <Link to="/customers" className="font-semibold text-gray-900">
          Verita Ops Console
        </Link>
        <div className="flex items-center gap-3 text-sm text-gray-600">
          {me.data && <span>{me.data.user.username}</span>}
          <button
            type="button"
            onClick={handleLogout}
            className="rounded border border-gray-300 px-3 py-1 hover:bg-gray-50"
          >
            Sign out
          </button>
        </div>
      </div>
    </header>
  );
}
