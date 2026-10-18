# Manufacturing Quality

English | [Tiếng Việt](README.vi.md)

Analyze January–June 2024 manufacturing defects, compare production lines and product families, and inspect the source records behind every metric. This RFID Data Analyst case-study application pairs an interactive **Dashboard** with an **Agent** workspace powered by the same deterministic Python analytics engine.

The Python backend fetches defect records over HTTP, cleans and reconciles them against the product master, and computes canonical KPIs, statistical tests, and sensitivity scenarios. The React and TypeScript frontend renders these results across both tabs, while [DATA_QUALITY.xlsx](DATA_QUALITY.xlsx) and the generated report below cover the full Part 1–Part 4 submission.

## Setup and run

Use **Python 3.12** and **Node.js 24 with npm**. Clone the repository and run the setup script from its root:

```sh
git clone https://github.com/delatran/manufacturing-quality.git
cd manufacturing-quality
```

Setup creates `.venv`, installs the hashed Python lockfile with `pip --require-hashes`, runs `npm ci`, and builds the frontend. It copies `.env.example` to `.env` if `.env` does not exist. Pass `-RuntimeOnly` on Windows or `--runtime-only` on macOS/Linux to install only Python runtime dependencies.

Windows PowerShell:

```powershell
.\scripts\setup.ps1
.\scripts\start.ps1
```

macOS or Linux:

```sh
sh scripts/setup.sh
sh scripts/start.sh
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000). A single process serves both the built frontend and the FastAPI backend. Interactive API documentation is at [/docs](http://127.0.0.1:8000/docs) and health status is at [/health](http://127.0.0.1:8000/health). Stop the server with Ctrl+C.

The Dashboard runs without an OpenAI key (internet access is required to fetch the assignment's defect records over HTTP). To enable the Agent, set `OPENAI_API_KEY` and `OPENAI_MODEL` in `.env` or in the process environment:

```dotenv
OPENAI_API_KEY=your_server_side_key
OPENAI_MODEL=gpt-6-luna
```

The application uses `gpt-6-luna` via the managed OpenAI Agents API and Codex harness (`analyst/config.py`), connecting the model to local Python analysis tools and automated report verification. Managed Agents is the sole provider backend for local and Cloudflare execution; configuration rejects alternate transports. Without `OPENAI_API_KEY`, the Dashboard remains fully functional while the Agent stays disabled.

Configuration reads `OPENAI_API_KEY` and `OPENAI_MODEL`, with process environment variables taking precedence over `.env`. Runtime policy in `Settings` (`analyst/config.py`) enables the managed Agents API with maximum reasoning effort and `unbounded_runs=True` locally and on Cloudflare. Application-side turn, output-token, tool-call, cost and run-duration limits are absent; source inputs remain read-only. Provider and infrastructure limits still apply. Bounded settings remain available for test fixtures.

The Cloudflare bridge routes managed sessions, streaming events, function results, native steering and cancellation through a private connection. Each provider session belongs to one visitor, and repeated logical messages retain their idempotency keys. See the [cloud configuration](cloudflare/README.md) for deployment and recovery verification.

Keep `OPENAI_API_KEY` in the local backend environment or the Cloudflare Worker secret—never in the frontend bundle. In the [Cloudflare deployment](cloudflare/README.md) ([tranthiennhan.com/manufacturing-quality](https://tranthiennhan.com/manufacturing-quality)), the Worker holds the provider credential and proxies requests from per-visitor Containers so the key never enters a Container.

For frontend development, run `npm --prefix frontend run dev` while the backend listens on `127.0.0.1:8000`.

## Explore the Dashboard

Start with the overview, pick a priority, and open its supporting evidence or filter the charts. Five sections organize the core analytical questions:

| Section | What you can check |
|---|---|
| **Overview and priorities** | Four headline metrics, monthly sparklines, half-period comparisons, and three ranked priorities with evidence and recommended actions. |
| **Trends and unusual days** | Monthly defect and Critical counts, top drivers of change, and daily counts against a January-based screening threshold. Click any flagged day to inspect its records. |
| **Location and severity** | Severity breakdown across defect locations, the contingency table and chi-square / Cramér's V test, and volume by inspection method. |
| **Lines and products** | Production lines and products ranked by event count or events per 1,000 reference units, plus within-line category comparisons that describe product mix and line differences. |
| **Repair costs** | Known repair cost by line, defect type, and severity, the repair cost distribution, and the products contributing most to recorded cost. |

Filter by month, line, category, product, severity, defect type, location, and inspection method. **View evidence** jumps directly to the supporting chart or table, and **Inspect individual defect records** opens the cleaned event log with full traceability to raw HTTP rows and cleaning rules.

The **Export data & DATA_QUALITY.xlsx** menu downloads either the filtered CSV or the full-study [DATA_QUALITY.xlsx](DATA_QUALITY.xlsx) workbook.

**Key metric definitions:**
- **Events per 1,000 reference units** uses the master table's static `monthly_output` multiplied by the number of selected months. Treat it as reference intensity rather than a true defect rate over actual production or inspection volume.
- **Known repair cost** sums valid non-negative costs and reports coverage explicitly; missing costs are never imputed as zero.

## Investigate with the Agent

The **Agent** tab inherits the Dashboard's active scope and displays the selected period, filters, four headline metrics, and three starter questions (`Q1`–`Q3`). Each run snapshots the scope when submitted so follow-up turns stay consistent even if Dashboard filters change later. View actions inside Agent responses can jump to Dashboard evidence or update Dashboard filters with one-click undo.

The Agent streams live tool execution and reasoning summaries, verifies every numeric claim against tool outputs, and renders its final report inline with charts. For deeper questions, it combines canonical Dashboard sections with read-only SQL over the frozen scope's event and reference-product tables—supporting custom joins, window functions, anomaly-day drill-downs, and raw-row traceability while preserving canonical denominators.

Starter questions you can ask once `OPENAI_API_KEY` is configured:

- “Compare the first and second halves of this period. Which defect types and lines contribute most to the increase?”
- “Does the highest-intensity category remain higher within each production line?”
- “Which line has the highest average known repair cost, and how complete are its cost records?”

Flow is visible throughout each run. Add details while the Agent works to steer that same run; its frozen data scope stays attached. Final answers stay in chat with registered inline charts. Agent result downloads are unavailable; the Dashboard retains its filtered CSV and full-study workbook exports.

The managed adapter is in [analyst/managed_agents.py](analyst/managed_agents.py), with typed tools in [analyst/product_tools.py](analyst/product_tools.py) and report verification in [analyst/runtime.py](analyst/runtime.py). [Evaluation protocols](evals/README.md) and the [historical audit](AUDIT.md) distinguish automated checks from dated live model outcomes.

Both tabs support English and Vietnamese, use the `Be Vietnam Pro` typeface and Warm Forest palette from `tranthiennhan.com`, and persist your language choice.

## Reproduce and verify the submission

The submission generator fetches the HTTP defect source, verifies source/master/template SHA-256 digests against the assignment commit, computes the canonical snapshot, and updates both README analysis sections and [DATA_QUALITY.xlsx](DATA_QUALITY.xlsx):

```powershell
.\.venv\Scripts\python.exe -m scripts.generate_submission
.\.venv\Scripts\python.exe -m scripts.generate_submission --check
```

On macOS/Linux, use `.venv/bin/python`. Pass `--output-dir artifacts/submission-preview` to write to a separate directory. `--check` validates the saved snapshot, calculation code hashes, local source digests, generated bilingual prose, and workbook cells without making network calls.

To run the standalone case-study pipeline and export all evidence artifacts:

```powershell
.\.venv\Scripts\python.exe -m analyst.case_study --output .runtime/manual-case/artifacts
```

Run the full verification suite from the repository root:

```powershell
.\scripts\check.ps1
```

On macOS/Linux, run `sh scripts/check.sh`. This executes dependency lock checks, Ruff formatting/lint, Mypy, pytest, submission contract verification, and frontend Prettier, ESLint, TypeScript, UI verification suites, and production build.

<!-- BEGIN GENERATED CANONICAL ANALYSIS -->

## Case study analysis: Part 3

Scope: January–June 2024 under canonical policy `canonical-v2`, dataset `dataset-4586b9cf07494f8c8481a2f8f8a578359fbab7c4c7435b06ccb7defef31fa515` and report snapshot `5fb4c047808c71233216b53a`. The defect source was fetched over HTTP at `2026-10-18T21:11:49.395606+00:00`. Amounts are in **local currency (unspecified)**. Unrounded values, definitions and source receipts are in the [canonical snapshot](reports/canonical_snapshot.json); the dashboard shows the same numbers.

### Key findings

1. **Defects are rising: a negative sign for the plant.** Logged events grew from 147 in January to 252 in June (+71.43%). April–June logged 657 events against 414 in January–March (+58.70%); Critical events rose 52.90% and observed repair cost 72.10% between the two halves.
2. **Surface has the most defects; Component has the most severe ones.** Surface logs 384 events. Component has 148 Critical events out of 346 (42.77%), against 27.72% at the other locations combined.
3. **Category 4 has the highest observed reference intensity.** Its products log 4.592 events per 1,000 reference units against 1.473 for all other products. Within each of the 4 lines that make them, they exceed the rest of the line by 1.56–7.19×. The 4 highest-intensity products (81, 83, 82, 84) are all Category 4.
4. **Line-3 repairs cost 1.66× more.** Line-3 averages 685.69 per known-cost repair against 413.35 on the other lines, so it carries 37.43% of known repair cost with 26.51% of the events.
5. **The increase is concentrated.** Of the +105 events between January and June, Cosmetic defects contribute +58 (55.2%) and Line-3 +43 (41.0%).

### Data quality and counting units

- 1,090 raw rows were received over HTTP. Cleaning removed 14 surplus copies (12 exact, 1 identical after label normalisation, 1 missing-cost duplicate) and quarantined 2 rows that share an ID but contradict each other, leaving 1,074 events. 3 valid events fall outside January–June 2024, so **1,071 events are analysed**.
- 15 rows were corrected (13 label case/whitespace fixes, 2 unambiguous day-first dates). 426 slash dates could be read either way; they follow the dominant M/D/YYYY convention and a scenario tests the reverse. 436 retained events carry at least one flag; flags and corrections overlap and are not additive.
- 4 events reference products missing from the master; they stay in plant totals but not in output-normalised rates. 22 events have no valid cost (coverage 97.95%); missing and negative costs are never imputed. 12 raw rows carry a cost of exactly 10, the lowest value observed; they stay valid and a scenario withholds them.
- No month has logged events after day 28, and 16 of 182 calendar dates have none. Reporting completeness is unverified, so month-end gaps must not be read as zero defects.
- Every issue, its detection rule, the action taken and its impact are in [DATA_QUALITY.xlsx](DATA_QUALITY.xlsx).

### Q1. Trend, January–June 2024

| Month | Defect events | Critical | Observed repair cost | Known cost / all events | Avg cost per known-cost event |
|---|---|---|---|---|---|
| 2024-01 | 147 | 50 | 65,488.29 | 142/147 | 461.19 |
| 2024-02 | 119 | 38 | 54,376.13 | 117/119 | 464.75 |
| 2024-03 | 148 | 50 | 67,147.84 | 144/148 | 466.30 |
| 2024-04 | 195 | 57 | 94,773.56 | 192/195 | 493.61 |
| 2024-05 | 210 | 80 | 103,148.47 | 209/210 | 493.53 |
| 2024-06 | 252 | 74 | 123,930.10 | 245/252 | 505.84 |

**Answer: a negative sign.** Logged events rose 71.43% from January (147) to June (252). The later three months have a higher observed count: April–June averaged 219 events a month against 138 in January–March (+58.70%). No month logs events after day 28, so reporting completeness must be checked before explaining February's dip or interpreting the rise as a change in the underlying process. Critical events (+52.90%) and observed repair cost (+72.10%) rose with the count, and the average cost per known-cost repair climbed from 464.05 to 498.22.

Every line increased. Line-3 grew fastest (31 → 74 events) and contributes 41.0% of the January–June increase; by defect type, Cosmetic contributes 55.2% (39 → 97). A plant-wide rise points first to common causes such as volume, materials or inspection changes. Reference output is static, so output-normalised intensity rises in the same proportion; actual monthly output and inspected units are needed to confirm that the defect rate, not just production or inspection volume, went up.

### Q2. Defect location and severity

| Location | Events | Share of events | Minor | Moderate | Critical | Critical share at location |
|---|---|---|---|---|---|---|
| Surface | 384 | 35.85% | 157 | 127 | 100 | 26.04% |
| Component | 346 | 32.31% | 87 | 111 | 148 | 42.77% |
| Internal | 341 | 31.84% | 117 | 123 | 101 | 29.62% |

**Answer:** Surface is the most frequent location (384 events). Severity depends on location: Component's Critical share is 42.77%, against 27.72% elsewhere. The association is statistically clear (chi-square p = 0.000003; expected-cell checks pass) but small in size (Cramér's V = 0.121).

**Where to focus:** start failure analysis with Component Critical defects, because Component holds 42.41% of all Critical events. Surface is the volume issue with the lowest Critical share, so standard containment and inspection fit it better than deep failure analysis. The location label does not identify a mechanism; teardown of representative failures and process data are needed to test causes.

### Q3. Other notable patterns

| Pattern | Evidence | Why it matters to the Production team |
|---|---|---|
| Category 4 has the highest defect intensity on every line | 4.592 vs 1.473 events per 1,000 reference units; 1.56–7.19× the rest of each line. Without the output denominator: 14.1 events per product vs 9.4–10.2 for other categories. These are low-volume products (512 units a month on average vs 991 plant-wide). | The category has elevated reference intensity across the lines that make it. Shared design, materials and changeover practices are hypotheses to investigate; these descriptive comparisons do not separate product and line causes. Verify actual exposure and differences among products before choosing a corrective action. |
| Line-3 repairs cost much more | 685.69 vs 413.35 per known-cost repair (1.66×); for Category 4 alone, 701.74 on Line-3 vs 445.58 on other lines. | This line accounts for the largest share of observed repair cost (37.43%). Investigate repair routes and cost recording alongside product mix: comparison within a category still leaves differences in products, failure types and severity unresolved. |
| Line rankings depend on the denominator | Line-4 has the most events (340); Line-3 has the highest intensity (2.2907 per 1,000 reference units). Category 2 is made only on Line-3; Category 3 is made only on Line-4. | Raw counts describe logged workload. Where a category exists on only one line, these data cannot separate line and product effects. Use shared-category comparisons where available, check remaining product-mix differences and collect actual production and inspection exposure before assessing line performance. |
| Repair cost does not track severity | Average cost per known-cost event: Minor 476.13, Moderate 490.25, Critical 489.14. By defect type, Functional averages 503.85 against 474.43–475.65. Known-cost median 488.45, p90 770.35. | Cost cannot stand in for severity: prioritise safety and function by Critical counts and use cost to target repair-process work. A flat cost profile also suggests checking with Finance how repair cost is recorded. |
| A historical daily screen highlights unusual counts | 7 days exceed the January-based limit of 11.27 events a day (January mean 4.74): 2024-04-09, 2024-05-25, 2024-06-03, 2024-06-10, 2024-06-20, 2024-06-21, 2024-06-27. | The first historical breach is in April. This retrospective screen shows which dates to investigate; it is not a real-time alert or a confirmed process change. Operational alerts need a rolling baseline per line and category, reliable reporting and validation against confirmed incidents. |
| Data capture has gaps | No events after day 28 in any month; 16/182 days empty; 22 unknown costs; 12 costs of exactly 10. | Fix reporting before automating alerts; otherwise month-end gaps look like improvement and cost totals stay incomplete. |

**Drill-down: products 81–85.** These are the first five Category 4 product IDs and include the four highest-intensity products. Together they log 124 events, 47 Critical and 66,309.78 observed repair cost over 15,438 reference units. Monthly events: 2024-01: 16; 2024-02: 8; 2024-03: 22; 2024-04: 23; 2024-05: 29; 2024-06: 26.

| ID / Product | Events | Critical | Events / 1,000 reference units | Observed cost | Known cost / all events | Intensity rank (of 100) |
|---|---|---|---|---|---|---|
| 81 / Product CC | 34 | 11 | 11.980 | 16,756.54 | 34/34 | 1 |
| 83 / Product CE | 26 | 9 | 11.807 | 14,180.07 | 25/26 | 2 |
| 82 / Product CD | 23 | 10 | 10.222 | 11,863.32 | 23/23 | 3 |
| 84 / Product CF | 22 | 8 | 6.236 | 11,340.20 | 21/22 | 4 |
| 85 / Product CG | 19 | 9 | 4.113 | 12,169.65 | 19/19 | 15 |

### Sensitivity to assumptions

| Scenario | Events | Observed cost | Known cost / all events | Products 81–85 events | Products 81–85 Critical | Products 81–85 cost |
|---|---|---|---|---|---|---|
| Canonical | 1,071 | 508,864.39 | 1,049/1,071 | 124 | 47 | 66,309.78 |
| exclude_cost_10 | 1,071 | 508,754.39 | 1,038/1,071 | 124 | 47 | 66,309.78 |
| ambiguous_dm | 837 | 398,151.06 | 822/837 | 96 | 36 | 50,470.98 |
| year_2024 | 1,074 | 511,041.96 | 1,052/1,074 | 125 | 47 | 66,974.77 |

`exclude_cost_10` withholds costs equal to 10 without changing event counts; `ambiguous_dm` reads the order-ambiguous dates as D/M; `year_2024` moves out-of-period years into 2024. Each scenario is calculated separately and never modifies the canonical result. The leading signals (most severe location, highest-intensity line and category, costliest line per repair, top cost type and top cost product) and therefore the three priorities do not change in any tested scenario. Differences show sensitivity to assumptions, not confidence intervals or savings.

## Part 4: Recommendations

### Top three priorities

These are the same priorities the dashboard shows under “Top priorities for this view” in its Overview; the backend derives them from the analysis above. Targets are provisional until actual output and inspection volumes are available.

#### Priority 1: Investigate Critical defects at Component

- **Evidence:** 148 of 346 Component events are Critical (42.77%), against 27.72% elsewhere; Component holds 42.41% of all Critical events.
- **Action:** Pull the 148 Critical records, tear down representative failures per product family and check incoming component lots and assembly settings; add a containment check at Component inspection until the cause is confirmed.
- **Owner:** Quality Engineer (lead) with the Production Team Leaders of the affected lines.
- **Success measure:** Monthly Critical count and share at Component. Provisional target: bring the share down to the other locations' 27.72% within two months of the corrective action.

#### Priority 2: Investigate high-intensity Category 4 products

- **Evidence:** 282 events over 61,410 reference units (4.592 per 1,000) against 1.473 for all other products; higher than the rest of the line on 4 of 4 lines (1.56–7.19×); products 81, 83, 82, 84 lead product intensity.
- **Action:** Compare Category 4 design, materials and process steps (including changeovers on these low-volume products) with the other categories; start with the leading products and pilot a fix on one line before rolling it out.
- **Owner:** Process/Product Engineering (lead) with the Quality Engineer and the team leaders of every line that makes the category.
- **Success measure:** Category 4 events per 1,000 reference units on each line and its ratio to the rest of the line. Provisional target: halve the 3.119-per-1,000 gap within one quarter.

#### Priority 3: Investigate repair cost per event on Line-3

- **Evidence:** 685.69 per known-cost repair against 413.35 on the other lines (1.66×); 37.43% of known cost from 26.51% of events; the gap persists within Category 4 (701.74 vs 445.58).
- **Action:** Break Line-3 repair cost into labour, parts, rework and external service with Maintenance; compare repair routes for comparable products in the same category on other lines and check remaining product-mix differences; confirm with Finance that every line records cost the same way.
- **Owner:** Line-3 Team Leader (lead) with the Maintenance Lead and Finance.
- **Success measure:** Line-3 average known repair cost per event against other lines, with cost coverage. Provisional target: close half of the 272.33-per-event gap within one quarter.

### Next steps for the tool

Used every day, a static dashboard still leaves the Production team to notice changes, explain them and follow up by hand. The table maps each limitation to what this tool already does and the next capability to add.

| Limitation of a static dashboard | What this tool already does | Next capability |
|---|---|---|
| It waits to be opened; nobody is told when something changes. | The Trends and unusual days section marks historical days above the January-based limit (7 in this full-case period); filters recalculate the displayed scope. It does not send real-time alerts. | Scheduled checks per shift, line and category that push an alert with its evidence to the owner (Teams, Zalo or e-mail). |
| The weekly report is still assembled by hand. | The Dashboard export menu downloads CSV for the active filters and the separate full-case DATA_QUALITY.xlsx workbook. Markdown brief and evidence ZIP endpoints are backend capabilities, not current Dashboard controls. | Send the brief on a schedule to each department, with an approval step and delivery receipts. |
| Follow-up questions need an analyst. | The Agent tab investigates questions with the same backend metrics and checks every number against tool receipts. It is a prototype: it needs an OpenAI key and its live acceptance suite has not fully passed ([AUDIT.md](AUDIT.md)). | Make it fast and reliable enough for daily use, then let it draft the investigation behind each alert automatically for a person to review. |
| An insight does not become an action. | Recommendations describe proposed actions and owners. The current interface has no follow-up task manager; backend records alone do not establish that an action was assigned or completed. | Shared corrective-action tracking with due dates, escalation and before/after measurement on the same definitions. |
| Data arrives late and incomplete. | HTTP fetch with manual refresh; stale or failed refreshes are labelled. | An MES/ERP feed with freshness monitoring, enforced month-end logging and cost capture. |
| Rates rest on assumptions. | Reference intensity uses static monthly_output. Backend scenarios and this report test date and cost assumptions. The Dashboard has no dedicated manual what-if panel; WebMCP can apply its existing date and cost scenarios through the Dashboard filters, with a visible scenario badge. | Actual output and inspected units by product, shift and lot, plus machine and process step, to turn intensities into defect rates and test causes. |

Owners are proposed roles and targets are provisional; no assignment has been sent and no plant outcome has been measured.

<!-- END GENERATED CANONICAL ANALYSIS -->

## Sources, architecture, and documentation

The [assignment repository](https://github.com/phongtdt/RFID_Data-Analyst_Case-Study-Oct-2026) supplies the [master CSV](https://github.com/phongtdt/RFID_Data-Analyst_Case-Study-Oct-2026/blob/main/data/master_data.csv), [quality template](https://github.com/phongtdt/RFID_Data-Analyst_Case-Study-Oct-2026/blob/main/DATA_QUALITY_TEMPLATE.xlsx), and [HTTP defect endpoint](https://raw.githubusercontent.com/phongtdt/RFID_Data-Analyst_Case-Study-Oct-2026/main/mock-api/defect_records.json). SHA-256 digests, reference commit, fetch timestamps, and schemas are recorded in [reports/canonical_snapshot.json](reports/canonical_snapshot.json). Original inputs remain read-only.

`analyst/case_study` owns ingestion, cleaning, scopes, metrics, scenarios, the workbook, and the quality brief. `analyst/dashboard.py` serves shared analysis contexts and evidence payloads. Agent runtime, typed tools, SQLite persistence, and report verification live under `analyst/`. `frontend/src` implements the bilingual React + TypeScript UI.

| Document | Scope |
|---|---|
| [DATA_QUALITY.xlsx](DATA_QUALITY.xlsx) | Completed Part 1 data-quality workbook (10 sheets: Summary, Issues Found, Assumptions, Impact on Results, and reconciliation sheets). |
| [analyst/app.py](analyst/app.py) and [frontend/src](frontend/src) | HTTP/WebSocket contracts and bilingual Dashboard/Agent implementation. |
| [evals/README.md](evals/README.md) and [AUDIT.md](AUDIT.md) | Evaluation protocols and dated verification outcomes. |
| [knowledge/kpi_registry.yaml](knowledge/kpi_registry.yaml) | Formal KPI formulas, denominators, units, and data-quality handling policies. |

Detailed documentation and raw verification receipts are maintained locally under `docs/`, which is excluded from Git. A fresh clone needs only the tracked setup instructions, source, contracts and tests.

The local server listens on loopback (`127.0.0.1:8000`) and stores session state, SQLite records, and investigation artifacts under the git-ignored `.runtime/` directory. The [Cloudflare deployment](cloudflare/README.md) isolates each visitor in a dedicated Container backed by Durable Object state while keeping `OPENAI_API_KEY` in the Worker.
