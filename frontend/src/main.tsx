import {createRoot} from 'react-dom/client'
import './index.css'
import App, {loadData} from './App'
import {fetchBootstrap} from './api'
fetchBootstrap().then(d => { if (d) loadData(d) }).finally(() => createRoot(document.getElementById('root')!).render(<App/>))
