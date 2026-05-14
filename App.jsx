import { useState, useEffect } from "react"
import StocksPage from "./components/StocksPage"
import "./index.css"

export default function App() {
  const [stats, setStats] = useState(null)
  useEffect(() => { fetch("/api/stats").then(r=>r.json()).then(setStats).catch(()=>{}) }, [])
  return (
    <div className="app">
      <header className="header">
        <div className="header-inner">
          <div className="logo">
            <span className="logo-mark">●</span>
            <span className="logo-text">CERNIM<span className="logo-accent">CHERNO</span></span>
          </div>
          {stats && stats.total_dates > 0 && (
            <div className="header-stats">
              <span>{stats.total_skus} SKU</span>
              <span className="sep">·</span>
              <span>{stats.total_dates} дней</span>
              <span className="sep">·</span>
              <span>{stats.date_from} — {stats.date_to}</span>
            </div>
          )}
        </div>
      </header>
      <main className="main">
        <StocksPage onStatsUpdate={setStats} />
      </main>
    </div>
  )
}
