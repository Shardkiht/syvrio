import { createRoot } from 'react-dom/client';
import App from './App';
import { applyTheme, readTheme } from './state/theme';
import './styles.css';

// Apply before the first paint so a stored preference never flashes the other
// scheme on reload. Restoring must not persist anything by itself.
applyTheme(readTheme(), false);

const root = document.getElementById('root');
if (root) createRoot(root).render(<App />);
