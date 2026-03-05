import React from 'react';
import { createRoot } from 'react-dom/client';
import App from './App';
import WorkshopPage from './WorkshopPage';
import StrategyDetailPage from './StrategyDetailPage';
import StrategyLifecyclePage from './StrategyLifecyclePage';
import HistoryPage from './HistoryPage';
import './index.css';

function CurrentPage() {
  const path = window.location.pathname;
  if (path.startsWith('/history')) {
    return <HistoryPage />;
  }
  if (path.startsWith('/settings')) {
    return <App initialSettingsOpen />;
  }
  if (path.startsWith('/strategies')) {
    return <StrategyLifecyclePage />;
  }
  if (path.startsWith('/workshop')) {
    return <WorkshopPage />;
  }
  if (path.startsWith('/strategy/')) {
    return <StrategyDetailPage />;
  }
  return <App />;
}

createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <CurrentPage />
  </React.StrictMode>,
);
