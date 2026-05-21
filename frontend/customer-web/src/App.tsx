import { BrowserRouter, Routes, Route } from 'react-router-dom';
import { RequireAuth } from './components/RequireAuth';
import { Login } from './pages/Login';
import { Dashboard } from './pages/Dashboard';
import { InvoiceDetail } from './pages/InvoiceDetail';

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route
          path="/"
          element={
            <RequireAuth>
              <Dashboard />
            </RequireAuth>
          }
        />
        <Route
          path="/invoices/:id"
          element={
            <RequireAuth>
              <InvoiceDetail />
            </RequireAuth>
          }
        />
      </Routes>
    </BrowserRouter>
  );
}
