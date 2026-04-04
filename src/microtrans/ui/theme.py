"""Tema visual da interface Streamlit (dark dashboard)."""

MT_CSS = """
<style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
    :root {
      --mt-bg: #0f0f13;
      --mt-panel: #1c1c1e;
      --mt-panel-2: #222225;
      --mt-line: #2a2a2d;
      --mt-text: #ffffff;
      --mt-muted: #a0a0a5;
      --mt-tertiary: #4a4a4a;
      --mt-accent: #84ff4a;
      --mt-accent-alt: #ff8c00;
      --mt-accent-soft: rgba(132, 255, 74, 0.15);
      --mt-up: #00c853;
      --mt-down: #ff3b30;
    }
    [data-testid="stAppViewContainer"],
    .stApp {
      background:
        radial-gradient(circle at 10% 0%, rgba(132,255,74,0.08), transparent 38%),
        radial-gradient(circle at 90% 10%, rgba(0,200,255,0.06), transparent 35%),
        var(--mt-bg) !important;
      color: var(--mt-text) !important;
      font-family: "Inter", "Segoe UI", Roboto, sans-serif !important;
    }
    [data-testid="stHeader"] { background-color: var(--mt-panel) !important; }
    section[data-testid="stSidebar"] {
      background-color: var(--mt-panel) !important;
      border-right: 1px solid var(--mt-line) !important;
    }
    section[data-testid="stSidebar"] [data-testid="stVerticalBlock"] {
      gap: 0.4rem !important;
    }
    .mt-side-brand {
      display: flex;
      align-items: center;
      gap: 10px;
      margin-bottom: 10px;
      margin-top: 2px;
    }
    .mt-side-brand-icon {
      width: 30px;
      height: 30px;
      border-radius: 8px;
      background: linear-gradient(135deg, rgba(132,255,74,0.35), rgba(255,140,0,0.2));
      border: 1px solid var(--mt-line);
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 0.72rem;
      font-weight: 700;
      color: var(--mt-text);
    }
    .mt-wolf-icon {
      width: 34px;
      height: 34px;
      padding: 0;
      background: radial-gradient(circle at 50% 35%, rgba(132,255,74,0.18), rgba(0,0,0,0.0)), #0f1217;
    }
    .mt-wolf-icon svg {
      width: 100%;
      height: 100%;
      display: block;
    }
    .mt-side-brand-title { font-weight: 700; font-size: 0.96rem; color: var(--mt-text); line-height: 1.1; }
    .mt-side-brand-sub { font-size: 0.72rem; color: var(--mt-muted); line-height: 1.1; }
    .mt-side-divider {
      border-top: 1px solid var(--mt-line);
      margin-top: 0.35rem;
      margin-bottom: 0.35rem;
      opacity: 0.9;
    }
    section[data-testid="stSidebar"] * { color: var(--mt-text) !important; }
    [data-testid="stSidebarNav"] { display: none !important; }
    div[data-testid="stHorizontalBlock"] { gap: 0.8rem !important; }
    .mt-bar {
      background: var(--mt-panel);
      border: 1px solid var(--mt-line);
      border-radius: 16px;
      padding: 14px 18px;
      margin-bottom: 12px;
      display: flex;
      flex-wrap: wrap;
      gap: 20px;
      align-items: baseline;
    }
    .mt-topbar {
      background: var(--mt-panel);
      border: 1px solid var(--mt-line);
      border-radius: 16px;
      padding: 14px 18px;
      margin-bottom: 12px;
      box-shadow: 0 12px 26px rgba(0, 0, 0, 0.24);
    }
    .mt-topbar-title { font-size: 1.08rem; font-weight: 700; color: var(--mt-text); }
    .mt-topbar-sub { font-size: 0.86rem; color: var(--mt-muted); margin-top: 2px; }
    .mt-pair { font-size: 1.35rem; font-weight: 700; color: var(--mt-accent); }
    .mt-price { font-size: 1.5rem; font-weight: 600; }
    .mt-up { color: var(--mt-up) !important; }
    .mt-down { color: var(--mt-down) !important; }
    .mt-muted { color: var(--mt-muted); font-size: 0.85rem; }
    .mt-stepper {
      display: flex; gap: 8px; flex-wrap: wrap; margin: 12px 0 20px 0;
    }
    .mt-step {
      flex: 1; min-width: 120px; text-align: center;
      padding: 10px 8px; border-radius: 12px; border: 1px solid var(--mt-line);
      background: var(--mt-panel-2); font-size: 0.85rem;
    }
    .mt-step.ok { border-color: var(--mt-up); color: var(--mt-up); }
    .mt-step.wait { color: var(--mt-muted); }
    .mt-step.run { border-color: var(--mt-accent); color: var(--mt-accent); box-shadow: 0 0 10px var(--mt-accent-soft); }
    h1, h2, h3, h4 { color: var(--mt-text) !important; letter-spacing: 0.01em; }
    h1 { font-size: 1.85rem !important; font-weight: 700 !important; }
    h2 { font-size: 1.45rem !important; font-weight: 700 !important; }
    h3, h4 { font-weight: 600 !important; }
    .mt-kpi-grid [data-testid="stMetric"] {
      background: var(--mt-panel);
      border: 1px solid var(--mt-line);
      border-radius: 16px;
      padding: 12px 16px;
      min-height: 108px;
      display: flex;
      flex-direction: column;
      justify-content: center;
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.02);
    }
    .mt-kpi-grid [data-testid="stMetricLabel"] {
      color: var(--mt-muted) !important;
      font-size: 0.78rem !important;
      font-weight: 500 !important;
      letter-spacing: 0.02em;
      margin-bottom: 0.2rem;
    }
    .mt-kpi-grid [data-testid="stMetricValue"] {
      font-size: 1.65rem !important;
      font-weight: 700 !important;
      line-height: 1.12 !important;
      margin-bottom: 0.1rem;
    }
    .mt-kpi-grid [data-testid="stMetricDelta"] {
      font-size: 0.78rem !important;
      font-weight: 600 !important;
    }
    .mt-panel-title { font-size: 1rem; font-weight: 600; margin-bottom: 0.35rem; }
    div[data-testid="stVerticalBlockBorderWrapper"] {
      border: 1px solid var(--mt-line) !important;
      border-radius: 16px !important;
      background: var(--mt-panel) !important;
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.015) !important;
    }
    div[data-testid="stVerticalBlockBorderWrapper"] > div {
      padding-top: 0.35rem !important;
      padding-bottom: 0.2rem !important;
    }
    div[data-testid="stVerticalBlockBorderWrapper"] .stDataFrame {
      border-radius: 12px !important;
      overflow: hidden !important;
    }
    .stCaption {
      color: var(--mt-muted) !important;
      font-size: 0.79rem !important;
      line-height: 1.35 !important;
    }
    .stTabs [data-baseweb="tab-list"] { background: var(--mt-panel); gap: 4px; }
    div[data-testid="stMetricValue"] { color: var(--mt-text) !important; }
    .stButton button {
      border-radius: 12px !important;
      border: 1px solid rgba(255,255,255,0.09) !important;
      background:
        linear-gradient(180deg, rgba(255,255,255,0.04), rgba(255,255,255,0.01)),
        var(--mt-panel-2) !important;
      color: var(--mt-text) !important;
      min-height: 44px !important;
      white-space: nowrap !important;
      font-weight: 700 !important;
      letter-spacing: 0.01em !important;
      box-shadow: 0 8px 16px rgba(0,0,0,0.25);
      transition: all 0.18s ease !important;
    }
    .stButton button[kind="primary"] {
      background:
        linear-gradient(90deg, rgba(132,255,74,0.22), rgba(132,255,74,0.08)),
        var(--mt-panel-2) !important;
      color: #f3ffe9 !important;
      border-color: rgba(132,255,74,0.45) !important;
    }
    section[data-testid="stSidebar"] .stButton button {
      justify-content: flex-start !important;
      font-weight: 600 !important;
      padding: 0.48rem 0.7rem !important;
      min-height: 2.15rem !important;
      transition: all 0.18s ease !important;
    }
    section[data-testid="stSidebar"] .stButton button:hover {
      border-color: rgba(132, 255, 74, 0.25) !important;
      background: rgba(255,255,255,0.02) !important;
    }
    section[data-testid="stSidebar"] .stButton button[kind="primary"] {
      border-color: rgba(132, 255, 74, 0.5) !important;
      box-shadow: 0 0 12px rgba(132, 255, 74, 0.12) !important;
      background: linear-gradient(90deg, rgba(132,255,74,0.14), rgba(132,255,74,0.04)) !important;
      color: #f3ffe9 !important;
    }
    .stTextInput>div>div>input,
    .stNumberInput input,
    .stSelectbox div[data-baseweb="select"] > div {
      border-radius: 10px !important;
      background: var(--mt-panel-2) !important;
      border-color: var(--mt-line) !important;
      color: var(--mt-text) !important;
    }
    .mt-card {
      background: var(--mt-panel);
      border: 1px solid var(--mt-line);
      border-radius: 14px;
      padding: 14px 16px;
      margin-bottom: 10px;
    }
    .mt-card.soft {
      background: linear-gradient(180deg, rgba(255,255,255,0.01), rgba(255,255,255,0.0)), var(--mt-panel);
    }
    .mt-nav-title {
      text-transform: uppercase;
      letter-spacing: 0.06em;
      font-size: 0.72rem;
      color: var(--mt-tertiary);
      margin-top: 0.45rem;
      margin-bottom: 0.2rem;
      font-weight: 700;
    }
    .mt-badge {
      display: inline-block;
      font-size: 0.78rem;
      border-radius: 999px;
      padding: 2px 10px;
      border: 1px solid var(--mt-line);
      color: var(--mt-muted);
      margin-right: 6px;
    }
    .mt-badge.ok { color: var(--mt-up); border-color: rgba(0, 200, 83, 0.35); }
    .mt-badge.warn { color: #ff8c00; border-color: rgba(255, 140, 0, 0.35); }
    .mt-badge.live { color: var(--mt-accent); border-color: rgba(132, 255, 74, 0.35); }
    .mt-badge.analyt { color: #ff6b6b; border-color: rgba(255, 107, 107, 0.35); }
    .mt-wallet-box{
      background: var(--mt-panel);
      border: 1px solid var(--mt-line);
      border-radius: 14px;
      padding: 12px 14px;
      margin-bottom: 12px;
      box-sizing: border-box;
      box-shadow: 0 10px 24px rgba(0, 0, 0, 0.22);
    }
    .mt-wallet-title{
      font-weight: 800;
      color: var(--mt-accent);
      margin-bottom: 6px;
    }
    .mt-wallet-row{
      display:flex;
      justify-content:space-between;
      gap: 12px;
      font-size: 0.9rem;
      color: var(--mt-text);
      padding: 4px 0;
    }
    .mt-wallet-muted{
      color: var(--mt-muted);
      font-size: 0.82rem;
    }
    .mt-wallet-pnl-up{ color: var(--mt-up) !important; font-weight: 800; }
    .mt-wallet-pnl-down{ color: var(--mt-down) !important; font-weight: 800; }
    .mt-login-wrap{
      max-width: 520px;
      margin: 24px auto 0 auto;
      background: linear-gradient(180deg, rgba(255,255,255,0.015), rgba(255,255,255,0.005)), var(--mt-panel);
      border: 1px solid var(--mt-line);
      border-radius: 18px;
      padding: 18px 18px 14px 18px;
      box-shadow: 0 18px 34px rgba(0, 0, 0, 0.28);
    }
    .mt-login-title{
      font-weight: 800;
      font-size: 1.06rem;
      margin-bottom: 4px;
    }
    .mt-login-sub{
      color: var(--mt-muted);
      font-size: 0.86rem;
      margin-bottom: 8px;
    }
</style>
"""

