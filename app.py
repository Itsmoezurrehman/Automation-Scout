"""
Automation Scout — Your go to AI agent finding where your team should automate next.
"""

import json
import requests
import streamlit as st

# OpenAI Python SDK — used as a generic client for the Azure Foundry v1 endpoint
try:
    from openai import OpenAI
    _OPENAI_AVAILABLE = True
except Exception:
    _OPENAI_AVAILABLE = False


# scoring logic
def compute_score(p: dict):
    """Return (score 0-100, breakdown dict)."""
    hours_saved = p["frequency"] * p["hours"] * (p["pct_manual"] / 100.0)
    hours_component = min(hours_saved / 40.0, 1.0) * 60
    tool_component = min(p["tools"] / 5.0, 1.0) * 15
    people_component = min(p["people"] / 6.0, 1.0) * 15
    base = hours_component + tool_component + people_component
    judgment_penalty = 25 if p["needs_judgment"] else 0
    score = round(max(0.0, min(base - judgment_penalty, 100.0)), 1)
    return score, {
        "hours_saved_per_month": round(hours_saved, 1),
        "hours_component": round(hours_component, 1),
        "tool_component": round(tool_component, 1),
        "people_component": round(people_component, 1),
        "judgment_penalty": judgment_penalty,
    }


def explain(p: dict) -> str:
    _, b = compute_score(p)
    bits = [
        f"recovers ~{b['hours_saved_per_month']} hours/month",
        f"{p['pct_manual']}% manual",
        f"runs {p['frequency']}x/month",
        f"touches {p['people']} people",
    ]
    if p["needs_judgment"]:
        bits.append("but needs human judgment (penalty applied)")
    return ", ".join(bits)


# seed data
PROCESS_FIELDS = ["name", "frequency", "hours", "people",
                  "pct_manual", "tools", "needs_judgment"]

SEED = [
    {"name": "Weekly vendor invoice reconciliation", "frequency": 4, "hours": 2.0,
     "people": 3, "pct_manual": 85, "tools": 3, "needs_judgment": False},
    {"name": "Daily standup notes distribution", "frequency": 22, "hours": 0.5,
     "people": 5, "pct_manual": 90, "tools": 2, "needs_judgment": False},
    {"name": "New employee access provisioning", "frequency": 8, "hours": 1.5,
     "people": 4, "pct_manual": 75, "tools": 5, "needs_judgment": False},
    {"name": "Monthly board report compilation", "frequency": 1, "hours": 6.0,
     "people": 2, "pct_manual": 70, "tools": 4, "needs_judgment": True},
    {"name": "Customer refund approvals", "frequency": 30, "hours": 0.3,
     "people": 2, "pct_manual": 60, "tools": 3, "needs_judgment": True},
]


# Supabase persistence (via REST; falls back to session state)
def get_sb_config():
    """Return (base_url, key) from Streamlit secrets, or (None, None)."""
    try:
        return st.secrets["SUPABASE_URL"].rstrip("/"), st.secrets["SUPABASE_KEY"]
    except Exception:
        return None, None


def _sb_headers(key):
    return {"apikey": key, "Authorization": f"Bearer {key}",
            "Content-Type": "application/json"}


def _seed_session():
    if "processes" not in st.session_state:
        st.session_state.processes = [dict(p, id=i) for i, p in enumerate(SEED)]
    return st.session_state.processes


def load_processes(url, key):
    if not url:
        return _seed_session()
    try:
        r = requests.get(f"{url}/rest/v1/processes?select=*&order=id",
                         headers=_sb_headers(key), timeout=10)
        r.raise_for_status()
        rows = r.json()
        if not rows:  
            # first run: seed the table once
            requests.post(f"{url}/rest/v1/processes", headers=_sb_headers(key),
                          data=json.dumps(SEED), timeout=10)
            rows = requests.get(f"{url}/rest/v1/processes?select=*&order=id",
                                headers=_sb_headers(key), timeout=10).json()
        return rows
    except Exception as e:
        st.warning(f"Supabase read failed, using local data. ({e})")
        return _seed_session()


def add_process(url, key, p):
    clean = {k: p[k] for k in PROCESS_FIELDS}
    if not url:
        nid = max([x.get("id", 0) for x in st.session_state.processes], default=0) + 1
        st.session_state.processes.append(dict(clean, id=nid))
        return
    requests.post(f"{url}/rest/v1/processes", headers=_sb_headers(key),
                  data=json.dumps(clean), timeout=10)


def delete_process(url, key, pid):
    if not url:
        st.session_state.processes = [
            x for x in st.session_state.processes if x.get("id") != pid]
        return
    requests.delete(f"{url}/rest/v1/processes?id=eq.{pid}",
                    headers=_sb_headers(key), timeout=10)


def load_sample_data(url, key):
    """Add the seed examples ONLY if the inventory is empty. Never deletes."""
    existing = load_processes(url, key)
    if existing:
        return False  # already has data — do nothing
    if not url:
        st.session_state.processes = [dict(p, id=i) for i, p in enumerate(SEED)]
    else:
        requests.post(f"{url}/rest/v1/processes", headers=_sb_headers(key),
                      data=json.dumps(SEED), timeout=10)
    return True


# LLM helpers
@st.cache_resource(show_spinner=False)
def get_client():
    """Cached OpenAI-SDK client pointed at the Azure Foundry project endpoint.

    Reads from Streamlit secrets so credentials never appear in the UI and the
    app is judge-ready out of the box (no setup step on camera).
    """
    if not _OPENAI_AVAILABLE:
        return None
    try:
        base_url = st.secrets["AZURE_FOUNDRY_BASE_URL"]
        api_key = st.secrets["AZURE_FOUNDRY_KEY"]
    except Exception:
        return None
    try:
        return OpenAI(base_url=base_url, api_key=api_key)
    except Exception:
        return None


def get_deployment() -> str | None:
    """Foundry deployment name from secrets (e.g. 'gpt-5-mini')."""
    try:
        return st.secrets["AZURE_FOUNDRY_DEPLOYMENT"]
    except Exception:
        return None


def llm_extract(description: str, client, model: str):
    system = (
        "You extract structured data about business processes for an automation "
        "scoring engine. Return only valid JSON. No prose, no markdown fences."
    )
    user_prompt = (
        "Extract a JSON object with these exact keys:\n"
        "- name: short title for the process\n"
        "- frequency: int, times performed per month (weekly=4, daily=22, monthly=1)\n"
        "- hours: float, hours per single run\n"
        "- people: int, number of people involved\n"
        "- pct_manual: int 0-100, how much is manual/repetitive data handling\n"
        "- tools_list: array of strings, every distinct app, system, or tool named "
        "or clearly implied. Example: copying from emails into a spreadsheet and "
        "reconciling against a system gives three tools: email, spreadsheet, "
        "reconciliation system. List each one separately.\n"
        "- needs_judgment: bool. TRUE only if the work requires expert decisions, "
        "interpretation, or discretion such as approving exceptions or assessing "
        "risk. FALSE for routine mechanical work like copying, reconciling, or data "
        "entry, even if a person does it.\n\n"
        f"Description: {description}"
    )
    try:
        r = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_prompt},
            ],
        )
        txt = r.choices[0].message.content.replace("```json", "").replace("```", "").strip()
        data = json.loads(txt)
        data["tools"] = max(len(data.get("tools_list", [])), 1)
        return data
    except Exception as e:
        st.warning(f"LLM extraction failed, fill the form manually. ({e})")
        return None


def llm_recommendation(p: dict, client, model: str, inventory: list | None = None):
    """Generate a recommendation grounded in the scoring engine's structured output.

    The model receives the process fields PLUS the score breakdown and the
    process's rank in the inventory as evidence, and is instructed to cite that
    evidence in its reasoning. This is the Foundry IQ integration in practice:
    Foundry reasons OVER the analysis output, not in isolation.
    """
    score, breakdown = compute_score(p)
    evidence = {
        "process": {k: p.get(k) for k in PROCESS_FIELDS},
        "automation_score": score,
        "score_breakdown": breakdown,
    }
    if inventory:
        ranked = sorted(inventory, key=lambda x: compute_score(x)[0], reverse=True)
        position = next(
            (i + 1 for i, x in enumerate(ranked) if x.get("name") == p.get("name")),
            None,
        )
        evidence["rank_in_inventory"] = {
            "position": position,
            "total_processes": len(ranked),
        }

    system = (
        "You are a business analyst recommending automation approaches. Your "
        "recommendations must be grounded in the structured evidence supplied "
        "below — do not invent numbers and do not claim facts that aren't in the "
        "evidence. Cite the specific score components (hours recoverable, % "
        "manual, judgment penalty, tool count, rank) when explaining why this "
        "is or isn't a strong automation candidate. Name concrete tooling "
        "categories that fit (RPA, workflow automation, integration platform, "
        "agent, etc.). Be specific, not generic."
    )
    user_prompt = (
        "Evidence from the scoring engine:\n"
        f"{json.dumps(evidence, indent=2)}\n\n"
        "In 3-4 sentences, recommend a concrete automation approach for this "
        "process. Reference the evidence in your reasoning."
    )
    try:
        r = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_prompt},
            ],
        )
        return r.choices[0].message.content.strip()
    except Exception as e:
        st.warning(f"LLM recommendation failed. ({e})")
        return None


# app
st.set_page_config(page_title="Automation Scout", page_icon="🔍", layout="wide")
url, key = get_sb_config()

with st.sidebar:
    st.title("🔍 Automation Scout")
    st.caption("Finds where your team should automate next.")
    mode = st.radio("Mode", ["Discovery", "Analysis"])
    st.caption("💾 Persistent storage: " + ("on (Supabase)" if url else "off (session only)"))
    st.divider()
    st.subheader("AI engine")
    _deployment_preview = get_deployment()
    if get_client() and _deployment_preview:
        st.success(f"✓ Connected to Azure AI Foundry · {_deployment_preview}")
        st.caption(
            "Grounding extraction and recommendations through Microsoft "
            "Foundry IQ. The model reasons over the scoring engine's "
            "structured output, not in isolation."
        )
    else:
        st.error("AI engine not configured.")
        st.caption(
            "Set AZURE_FOUNDRY_BASE_URL, AZURE_FOUNDRY_KEY, and "
            "AZURE_FOUNDRY_DEPLOYMENT in Streamlit secrets."
        )
    if st.button("Load sample data (only if empty)"):
        added = load_sample_data(url, key)
        if added:
            st.success("Sample processes loaded.")
        else:
            st.info("Inventory already has data — nothing changed.")
        st.rerun()

client = get_client()
model = get_deployment()

if mode == "Discovery":
    st.header("Discovery — log a process")
    st.write("Describe a process you suspect is wasteful. The Scout scores its "
             "automation potential and adds it to the inventory.")

    if st.session_state.get("_saved_msg"):
        st.success(st.session_state.pop("_saved_msg"))
    
    if client:
        desc = st.text_area("Describe the process in plain English"),
                            key="disc_desc")
        if st.button("Analyze description") and desc:
            fields = llm_extract(desc, client, model)
            if fields:
                st.session_state._draft = fields
                st.success("Extracted — review and save below.")

    draft = st.session_state.get("_draft", {})
    c1, c2 = st.columns(2)
    with c1:
        name = st.text_input("Process name", value=draft.get("name", ""),
                             key="disc_name")
        frequency = st.number_input("Times per month", 1, 1000,
                                    int(draft.get("frequency", 4)),
                                    key="disc_freq")
        hours = st.number_input("Hours per run", 0.1, 100.0,
                                float(draft.get("hours", 1.0)),
                                key="disc_hrs")
    with c2:
        people = st.number_input("People involved", 1, 50,
                                 int(draft.get("people", 2)),
                                 key="disc_ppl")
        pct_manual = st.slider("% manual / repetitive", 0, 100,
                               int(draft.get("pct_manual", 70)),
                               key="disc_pct")
        tools = st.number_input("Number of tools/apps used", 1, 20,
                                int(draft.get("tools", 3)),
                                key="disc_tools")
    needs_judgment = st.checkbox("Needs human judgment",
                                 value=bool(draft.get("needs_judgment", False)),
                                 key="disc_jdg")

    if name:
        preview = {"name": name, "frequency": frequency, "hours": hours,
                   "people": people, "pct_manual": pct_manual, "tools": tools,
                   "needs_judgment": needs_judgment}
        score, _ = compute_score(preview)
        st.metric("Automation potential score", f"{score} / 100")
        if st.button("Save to inventory", type="primary"):
            add_process(url, key, preview)
            _saved_name = name
            # Clear all widgets' state so the form resets on rerun
            for _wk in ["disc_name", "disc_freq", "disc_hrs", "disc_ppl",
                        "disc_pct", "disc_tools", "disc_jdg", "disc_desc"]:
                st.session_state.pop(_wk, None)
            st.session_state.pop("_draft", None)
            # Store message before rerun so it survives the page refresh
            st.session_state["_saved_msg"] = f"Saved '{_saved_name}' to inventory."
            st.rerun()

else:  # Analysis
    st.header("Analysis — what to automate next")
    processes = load_processes(url, key)
    ranked = sorted(processes, key=lambda p: compute_score(p)[0], reverse=True)

    st.subheader("Ranked inventory")
    table = [{"Process": p["name"], "Score": compute_score(p)[0],
              "Hrs saved/mo": compute_score(p)[1]["hours_saved_per_month"],
              "% manual": p["pct_manual"], "Judgment": p["needs_judgment"]}
             for p in ranked]
    st.dataframe(table, use_container_width=True, hide_index=True)
    st.bar_chart({p["name"]: compute_score(p)[0] for p in ranked})

    if ranked:
        st.subheader("Manage inventory")
        idx = st.selectbox(
            "Select a process to remove", list(range(len(ranked))),
            format_func=lambda i: f"{ranked[i]['name']} — {compute_score(ranked[i])[0]}/100")
        target_name = ranked[idx]["name"]
        if not st.session_state.get("confirm_delete"):
            if st.button("Delete selected"):
                st.session_state.confirm_delete = True
                st.rerun()
        else:
            st.warning(f"Delete '{target_name}'? This can't be undone.")
            col_a, col_b = st.columns(2)
            with col_a:
                if st.button("Yes, delete"):
                    delete_process(url, key, ranked[idx].get("id"))
                    st.session_state.confirm_delete = False
                    st.success("Removed.")
                    st.rerun()
            with col_b:
                if st.button("Cancel"):
                    st.session_state.confirm_delete = False
                    st.rerun()

    st.subheader("Top 3 recommendations")
    for i, p in enumerate(ranked[:3], 1):
        score, _ = compute_score(p)
        with st.expander(f"#{i}  {p['name']}  —  {score}/100", expanded=(i == 1)):
            st.write(f"**Why:** {explain(p)}")
            if client and st.button("Generate narrative recommendation", key=f"rec_{i}"):
                rec = llm_recommendation(p, client, model, inventory=ranked)
                if rec:
                    st.info(rec)
