import { createRoot } from 'react-dom/client';
import App from './App';
import { applyTheme, readTheme } from './state/theme';
import './styles.css';

// 首次绘制前应用已保存的偏好，避免重载时闪现其他主题。恢复偏好时无需写入存储。
applyTheme(readTheme(), false);

const root = document.getElementById('root');
if (root) createRoot(root).render(<App />);
