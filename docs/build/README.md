# How the figures and deliverables were made

These scripts are not needed to run the project. They record how the submission files were produced.

| Script | Produces | Needs |
|---|---|---|
| `architecture.html` + `shoot.py` | `docs/figures/architecture_{dark,light}.png` | `pip install playwright`, Microsoft Edge |
| `app_shots.py` | `docs/figures/app_*.png`: real screenshots of the running app | the above, plus Ollama with `qwen2.5:3b` |
| `build_deck.py` | `submission/Rig_Sight_CA3_Presentation.pptx` | `python-pptx`, `pymupdf`, the team's Canva PDF export |
| `build_report.py` | `submission/Rig_Sight_CA3_Report.docx` | `python-docx` |
| `to_pdf.ps1` | PDF exports for checking (and the Word table of contents) | Microsoft PowerPoint / Word |

```bash
python docs/build/shoot.py docs/figures
python docs/build/app_shots.py . docs/figures
python docs/build/build_deck.py "<Canva deck>.pdf" . submission/Rig_Sight_CA3_Presentation.pptx 48
python docs/build/build_report.py . submission/Rig_Sight_CA3_Report.docx 48
```

The deck keeps the team's original slides as high-resolution renders where their content is still accurate,
replaces outdated text on slides 10, 11, 15 and 17 in place, and adds the architecture, live-app and results slides.
