import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import App from './App';
import { I18nProvider } from './i18n';
import './fonts.css';
import './styles.css';
import './dashboard.css';
import './i18n.css';
import './agent-experience.css';
import './agent-flow.css';
import './investigation.css';

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <I18nProvider>
      <App />
    </I18nProvider>
  </StrictMode>,
);
