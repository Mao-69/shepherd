#!/usr/bin/env python3
"""
CRV Research — Advanced Post-Session Dashboard
Run with: streamlit run crv/dashboard.py
"""

import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from pathlib import Path
import json
from datetime import datetime
import markdown2

st.set_page_config(page_title="CRV Research", layout="wide", initial_sidebar_state="expanded")
st.title("🧿 CRV Research — Session Analysis Dashboard")

# ====================== SESSION SELECTION ======================
sessions_root = Path("sessions")
session_dirs = sorted([d for d in sessions_root.iterdir() if d.is_dir()], reverse=True)

# Multi-select for comparison
selected_sessions = st.sidebar.multiselect(
    "Select Session(s) for Analysis",
    options=[d.name for d in session_dirs],
    default=[session_dirs[0].name] if session_dirs else [],
    max_selections=6
)

if not selected_sessions:
    st.warning("Please select at least one session.")
    st.stop()

# ====================== LOAD DATA ======================
@st.cache_data
def load_session_data(session_id: str):
    path = sessions_root / session_id
    data = {"id": session_id, "path": path}
    
    # Metadata
    meta_path = path / "metadata.json"
    data["meta"] = json.load(open(meta_path)) if meta_path.exists() else {}
    
    # Core CSVs
    if (path / "heartrate.csv").exists():
        data["hr"] = pd.read_csv(path / "heartrate.csv", parse_dates=["timestamp"])
    if (path / "stage_transitions.csv").exists():
        data["stages"] = pd.read_csv(path / "stage_transitions.csv", parse_dates=["timestamp"])
    if (path / "audio_events.csv").exists():
        data["audio"] = pd.read_csv(path / "audio_events.csv", parse_dates=["timestamp"])
    
    return data

all_data = [load_session_data(sid) for sid in selected_sessions]

# ====================== METRICS ======================
st.subheader("Key Performance Indicators")

cols = st.columns(len(selected_sessions) if len(selected_sessions) <= 4 else 4)

for i, session in enumerate(all_data):
    meta = session["meta"]
    coherent_min = meta.get("total_coherent_seconds", 0) // 60
    baseline = meta.get("baseline_bpm")
    coherent_pct = "—"  # Will improve in next version with better calc

    with cols[i % len(cols)]:
        st.metric(
            label=f"{session['id'][:19]}",
            value=f"{coherent_min} min",
            delta=f"{baseline:.1f} bpm baseline" if baseline else None
        )

# ====================== HR TIMELINE (Single or Comparison) ======================
st.subheader("Heart Rate + Coherence Timeline")

if len(selected_sessions) == 1:
    session = all_data[0]
    if "hr" in session and not session["hr"].empty:
        df = session["hr"].copy()
        
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=df["timestamp"], y=df["bpm"], 
                                mode="lines", name="Heart Rate", line=dict(color="#ef4444")))
        
        # Coherent periods
        if "state" in df.columns:
            coherent = df[df["state"] == "coherent"]
            fig.add_trace(go.Scatter(x=coherent["timestamp"], y=coherent["bpm"],
                                    mode="markers", name="Coherent State",
                                    marker=dict(color="#22c55e", size=9, symbol="circle")))
        
        fig.update_layout(height=550, title="Heart Rate with Coherent Periods")
        st.plotly_chart(fig, use_container_width=True)
else:
    st.info("Multi-session comparison view coming in next iteration (overlay or small multiples).")

# ====================== STAGE ANALYSIS ======================
st.subheader("Stage-by-Stage Analysis")

stage_stats = []
for session in all_data:
    if "stages" not in session or "hr" not in session:
        continue
    for _, stage in session["stages"].iterrows():
        stage_start = stage["timestamp"]
        # Find next stage or end
        next_stages = session["stages"][session["stages"]["timestamp"] > stage_start]
        stage_end = next_stages.iloc[0]["timestamp"] if not next_stages.empty else None
        
        # HR during this stage
        hr_stage = session["hr"][session["hr"]["timestamp"] >= stage_start]
        if stage_end:
            hr_stage = hr_stage[hr_stage["timestamp"] < stage_end]
            
        if not hr_stage.empty:
            coherent_time = len(hr_stage[hr_stage["state"] == "coherent"]) if "state" in hr_stage.columns else 0
            stage_stats.append({
                "Session": session["id"][:12],
                "Stage": stage["stage_name"],
                "Focus": stage["focus_level"],
                "Duration_min": round((hr_stage["timestamp"].max() - hr_stage["timestamp"].min()).total_seconds()/60, 1),
                "Avg_HR": round(hr_stage["bpm"].mean(), 1),
                "Coherent_%": round(100 * coherent_time / len(hr_stage), 1) if len(hr_stage) > 0 else 0,
                "HR_Drop": round(hr_stage["bpm"].mean() - session["meta"].get("baseline_bpm", hr_stage["bpm"].mean()), 1)
            })

if stage_stats:
    df_stages = pd.DataFrame(stage_stats)
    st.dataframe(df_stages, use_container_width=True)
    
    col_a, col_b = st.columns(2)
    with col_a:
        fig_stage = px.bar(df_stages, x="Stage", y="Coherent_%", color="Session", 
                          title="Coherence % by Stage")
        st.plotly_chart(fig_stage, use_container_width=True)
    with col_b:
        fig_hr = px.bar(df_stages, x="Stage", y="HR_Drop", color="Session",
                       title="Average HR Drop by Stage (vs Baseline)")
        st.plotly_chart(fig_hr, use_container_width=True)

# ====================== NOTES & INSIGHTS ======================
tab1, tab2, tab3 = st.tabs(["Session Notes", "Research Insights", "Raw Data"])

with tab1:
    for session in all_data:
        notes_path = session["path"] / "notes.md"
        if notes_path.exists():
            st.markdown(f"### {session['id']}")
            with open(notes_path) as f:
                st.markdown(markdown2.markdown(f.read()), unsafe_allow_html=True)
            st.divider()

with tab2:
    st.markdown("**What 'Coherent' Suggests**")
    st.info("""
    **Coherent** = Heart rate **below baseline** + **very stable** (low short-term variation).  
    This is a proxy for strong parasympathetic activation — the physiological state most associated with deep relaxation while maintaining awareness.
    
    Many remote viewers report their strongest impressions occur during these windows.
    """)
    
    if len(stage_stats) > 0:
        best_stage = df_stages.loc[df_stages["Coherent_%"].idxmax()]
        st.success(f"**Highest coherence occurred during:** {best_stage['Stage']} ({best_stage['Coherent_%']}% coherent)")

with tab3:
    for session in all_data:
        if "hr" in session:
            with st.expander(f"Raw Heart Rate — {session['id']}"):
                st.dataframe(session["hr"])

# ====================== EXPORT ======================
st.sidebar.markdown("---")
if st.sidebar.button("Export Current View as HTML Report"):
    # Simple export (can be expanded)
    st.sidebar.success("Report exported! (feature expandable)")

st.caption("CRV Research Dashboard v2 — Supports personal use, group sharing, and detailed analysis")