# CA-3 submission · Reading the Rig

**Agentic AI for Daily Drilling Reports (DDR) and non-productive time (NPT) detection**
Foundation of Agentic AI · TY AI-ML B · Symbiosis Institute of Technology, Pune
Rahul Narkhede (24070126146) and Ranjan Verma (24070126149)

| What the brief asks for | File |
|---|---|
| PPT (problem, solution, architecture, demo) | [Rig_Sight_CA3_Presentation.pptx](Rig_Sight_CA3_Presentation.pptx) · [PDF](Rig_Sight_CA3_Presentation.pdf) |
| Report (DOCX): problem statement, domain, agents, architecture, validation, results, limitations, future scope | [Rig_Sight_CA3_Report.docx](Rig_Sight_CA3_Report.docx) · [PDF](Rig_Sight_CA3_Report.pdf) |
| Architecture diagram (agents, data flow, tools, decisions) | [Rig_Sight_CA3_Architecture.png](Rig_Sight_CA3_Architecture.png) |
| Codebase: working Streamlit PoC with synthetic data, tests and evaluation | the repository root, or [Rig_Sight_CA3_Codebase.zip](Rig_Sight_CA3_Codebase.zip) |

- **Repository:** https://github.com/ranjanvrma/rig-sight
- **Live demo:** https://rig-sight.streamlit.app/ (hosted on Streamlit Community Cloud with the mock classifier;
  the local model runs with Ollama)

## Run the code

```bash
pip install -r requirements-dev.txt
streamlit run app.py
python -m pytest
python evaluate.py
```

Full setup, Ollama and deployment notes: the [main README](../README.md).

## Results in one line

On 15 synthetic reports with the same local model (qwen2.5:3b), the agent graph's NPT error was 0.47 h per report
against 3.83 h for a single-prompt chatbot, and the Critic caught 2 of 2 planted log errors. 48 automated tests pass.
