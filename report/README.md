# Implementation reports

**Start here if you just want to understand the project: [`SUMMARY.md`](SUMMARY.md)** — a short,
plain-language version written for presenting and discussion, not for technical review.

The two files below are the full technical reports. Both revisions are kept. The older one is
**not** superseded in the sense of being wrong — it accurately describes the build at the time it
was written, and Revision 2 refers back to it.

| File | Covers | PDF |
|---|---|---|
| `OpenEdu_Orchestrator_Report.tex` | **Revision 1** — the 4-agent LangGraph build running against dummy data and a mock OpenEduCat client. 57 tests. | `OpenEdu_Orchestrator_Report.pdf` (committed) |
| `OpenEdu_Orchestrator_Report_v2.tex` | **Revision 2** — adds real Odoo 19.0 + OpenEduCat 19.0 and real MySQL source, the `SourceAdapter` multi-source contract, the LLM mapping-authoring tool, the `reconcile` drift audit, and production hardening (retry/backoff, structured logging, env-var secrets, CI). 84 tests. | not yet built — see below |

## Building the PDF

Revision 2's PDF has not been committed because the authoring machine had no LaTeX toolchain
installed. To build it:

```bash
cd report
pdflatex OpenEdu_Orchestrator_Report_v2.tex
pdflatex OpenEdu_Orchestrator_Report_v2.tex
```

Run `pdflatex` **twice** — the first pass writes the `.aux` file the table of contents and the
`\ref` cross-references are resolved from on the second.

Requires a TeX distribution (MiKTeX or TeX Live on Windows) with `tikz`, `pgfplots`, `booktabs`,
`listings`, `titlesec`, and `fancyhdr`. Alternatively, upload the `.tex` to Overleaf, which has
all of these preinstalled.
