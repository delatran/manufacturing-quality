# Rebuild Agent evaluation

This page records historical development evaluations and their frozen execution
profiles. Those profiles and approvals do not describe or authorize the current
runtime. See the current [repository contract](../AGENTS.md) and
[runtime configuration](../README.md#setup-and-run). The recorded results below
do not establish live model quality for the current managed Agents API runtime.

Bộ đánh giá này khóa tám tác vụ và mười hai lượt trước khi gọi API. Số học được tính bằng bộ đọc nguồn độc lập. Kết quả live chỉ được ghi nhận từ đường chạy ứng dụng, Agents SDK, công cụ backend và artifact đã đọc lại. Các phép thử lỗi dùng dữ liệu giả được báo riêng. Baseline live chưa chạy vì ngân sách cũ đã hết; không dùng kết quả cũ để nhận là cải thiện.

Lần `post-v9` đã chạy đủ tám ca và mười hai lượt với Luna thật trên source V10, deadline ứng dụng 360 giây và giới hạn chờ 480 giây. Đánh giá độc lập chốt 6/8 tác vụ Passed: R02/R04/R05/R06/R07/R08; R01/R03 Failed. R01 câu đầu đúng nhưng follow-up timeout; R03 so sánh đầu đúng và Undo khôi phục giao diện, nhưng follow-up chưa được xác minh và không giao báo cáo cuối. Cả 39 trạng thái nội dung giữ nguyên bằng chứng, gồm các phần đúng nhưng chưa hoàn tất trong hai ca Failed; không dùng chúng để nhận là đạt 8/8. Ba mươi lỗi tự động do yêu cầu KPI ngoài phần tổng hợp chi phí được giữ nguyên, tách khỏi bốn lỗi thật về hoàn tất và giao tệp. Runtime kết thúc với exit 0, nguồn 45 tệp không đổi và tiến trình/cổng đã đóng; hai thông báo console vẫn được lưu, không nhận là dọn dẹp không lỗi. Phí token lần này là 0,150532575 USD; tổng chín lần thử là 0,750842455 USD qua 433 response, không còn usage chưa rõ hay khoản giữ ngân sách. Đây là các lần thử phát triển không mù, không phải bằng chứng cải thiện có kiểm soát. VH01 bàn giao tiếng Việt vẫn Not run và chưa được cấp lượt chạy.

The frozen protocol is [rebuild_protocol_v1.json](rebuild_protocol_v1.json).
Its adjacent SHA-256 file protects the task wording, requirements, execution
limits, scoring rule and exclusions. The two pre-dispatch amendments document a
Windows newline seal correction and a more accurate label for the in-progress
working-tree manifest. Neither amendment changed a task or success criterion.

The baseline observation recorded the application's configuration at that time:
`gpt-6-luna`, medium effort, no hosted tools, and zero remaining historical
development allowance. The coordinator observed 559 passing tests, one skipped
test and a successful frontend build. Live baseline tasks are `Not run`.
The protocol-freeze manifest includes in-progress rebuild edits and does not
identify the original baseline source. This evaluation makes no quantified live
before/after improvement claim.

## Observed live attempts

| Attempt | Status | Observed outcome | Token-price estimate |
|---|---|---|---|
| `post-v1`, R01 turn 1 | Failed | End-to-end turn 245.891 seconds; runtime timeout at 241.672 seconds; no report, delivered business artifact or saved finding | USD 0.104070205 after terminal-response reconciliation |
| `post-v1`, R01 turn 2 and R02 to R08 | Not run | No generation dispatched after the timeout; coordinator required locale and evidence-navigation repairs | Not applicable |
| `post-v2`, R01 turn 1 | Failed | End-to-end turn 246.937 seconds; the application reached its 240-second deadline; no report, delivered business artifact or saved finding | USD 0.008940695 after terminal-response reconciliation |
| `post-v2`, remaining 11 turns | Not run | The runner stopped on unresolved final-response usage. Later reconciliation does not automatically replay or resume paid requests. | Not applicable |
| `post-v3`, all eight cases | Failed | Strict substantive completion 5/8, or 62.5%; 11/12 turns observed. R01/R02/R06/R07/R08 Passed; R03/R04/R05 Failed. R03 undo Not run. | USD 0.115152665 for the eight-case attempt |
| `post-v4`, R01 turn 1 on frozen v5 | Failed | Application boundary 246.547 seconds; total 277.375 seconds including the 30-second accounting window. Four unverified findings remained at version 2, with no final report or delivery. | USD 0.015874515 after later terminal-response reconciliation |
| `post-v4`, remaining 11 turns | Not run | Final usage was unresolved when the suite stopped. Later accounting did not replay or resume generation. No R02/R03 observation gate was reached. | Not applicable |
| `post-v5`, all eight cases on frozen v6 | Failed | Strict substantive completion 5/8 (62.5%); 11/12 turns observed. R01/R02/R05/R07/R08 Passed; R03/R04 timeout, R06 Unverified; dependent R03 Undo Not run. | USD 0.093018885 across 62 responses; cumulative task USD 0.337056965 across 163 responses |
| `post-v6`, all eight cases on frozen v7 | Failed | Strict substantive completion 5/8 (62.5%); 11/12 turns observed. R02/R04/R05/R07/R08 Passed; R01 timeout, R03 false ranking exclusivity, R06 duplicated unnatural English Failed. R01 follow-up Not run. Actual model scenario apply and Undo observed. Cleanup Failed separately. | USD 0.08314474 across 60 responses; cumulative task USD 0.420201705 across 223 responses |
| `post-v7`, all eight cases on frozen V8 | Failed | Strict substantive completion 7/8 (87.5%); 11/12 turns observed. R02 to R08 Passed; R01 timeout and wrong saved Category1 metric Failed. Dependent R01 follow-up Not run. Actual scenario/model Undo observed and normal cleanup Passed. | USD 0.100946350 across 63 responses; cumulative task USD 0.521148055 across 286 responses |
| `post-v8`, all eight cases on frozen V9/application240 | Failed | Strict completion 4/8 (50%); 10/12 turns observed. R04/R06/R07/R08 Passed; R01/R03/R05 timeout and R02 Surface-as-whole-Line3 error Failed. R01 alternative/R03 Undo Not run. Five actual UI boundaries; normal cleanup Passed. | USD 0.079161825 across 61 responses; cumulative task USD 0.600309880 across 347 responses |
| `post-v9`, all eight cases on executed V10/application360 | Failed | Strict task completion 6/8 (75%); all 12 turns observed. R02/R04/R05/R06/R07/R08 Passed; R01 follow-up Timeout and R03 follow-up Unverified. Ten turns completed, with both first-turn reports and all six real UI boundaries preserved. | USD 0.150532575 across 86 responses; cumulative task USD 0.750842455 across 433 responses |

## post-v9 final actual outcome

Original result (local-only archive: `evals/rebuild_results_post-v9.json`; excluded from the public checkout),
source/lifecycle seal (local-only archive: `evals/rebuild_post_v9_seal.json`; excluded from the public checkout), and
final independent outcomes (local-only archive: `evals/rebuild_post_v9_outcomes.json`; excluded from the public checkout) bind the executed
V10/application360 and original outer480 condition. This is a disclosed nonblind
rerun of the same eight cases, twelve prompts and 39 content criteria. Historical
240-second outcomes are unchanged. The strict result is **Failed: six cases
Passed and R01/R03 Failed**, with ten completed, one timeout and one unverified
turn. All twelve turns were actually attempted.

| Case | Strict outcome | Actual evidence and remaining limit |
|---|---|---|
| R01 | Failed | First-turn report, decompositions and artifacts are correct. Follow-up removes the first hypothesis and retains corrected alternative evidence in saved version 7, but reaches the 360-second deadline with no accepted report or delivery. The late response is analyst repair JSON, not final QA. |
| R02 | Passed | Whole Line-3 subtotal 189,934.78 / coverage 98.227% and Critical narrowing 95 events / 93 known / 2 unknown / 65,297.27 are correct; actual model UI adoption and fresh EN/VI/model CSV bytes were checked. |
| R03 | Failed | First-turn scenario comparison and both full-ranking swaps are correct. Model Undo restores canonical UI and prior report availability. Follow-up tries a reserved current-run HTML selector and ends Unverified, with no accepted new report/delivery. |
| R04 | Passed | Descriptive method comparisons and limits are correct; no supported basis for a causal or efficacy ranking is invented, and the measurement plan is concrete. |
| R05 | Passed | Three priorities, Evidence/Action/proposed-role Owner/Metric/limits, editable saved draft and actual JSON/CSV/HTML delivery were read back. No external sending occurred. |
| R06 | Passed | Natural English, canonical quantities, linked numerator/static reference denominator and limits are correct. |
| R07 | Passed | Both fresh Vietnamese paraphrases preserve actual unrounded quantities, scope and meaning with readable prose. |
| R08 | Passed | Empty cohort and unavailable reference/coverage/intensity are explicit, with no perfect-quality conclusion. Two structured cohort-product unit fields remain null as a supplemental metadata failure. |

All 39 content statuses remain exactly as independently authored, including
partial-only passes in failed follow-ups. They do not upgrade the six-of-eight
task result. The final manual (local-only archive: `.runtime/rebuild-20261016/independent-review/post-v9-manual-review-v1.json`; excluded from the public checkout)
SHA-256 is `bb71825ff13897ab447a24c4a39b55f146dd74bccd4fd0c09783708c4f24b1a5`.
Original raw fields and checks remain immutable. Thirty automatic Failed flags
concern absent nonfinancial metrics in actual cost-only projections, not explicit
null observations. Four separate application-completion/artifact failures remain
real. No active scorer change or paid replay was used to adjudicate either group.

The wording review (local-only archive: `.runtime/rebuild-20261016/independent-review/post-v9-wording-review.json`; excluded from the public checkout)
reads ten complete delivered reports/HTML, five EN/five VI, and binds six actual
root CUA receipts. It verifies EN/VI source parity and 92/94 declared reference
units. The two actual null paths are cohort_products_with_events and
cohort_products_without_events in R08; human product labels and values remain
correct. Undelivered follow-up wording is Not run. No narrow/mobile or
console-cleanliness claim is transferred from historical checks.

The independent terminal audit (local-only archive: `.runtime/rebuild-20261016/independent-review/post-v9-seal-review-v1.json`; excluded from the public checkout)
checks 45 exact executed sources, 86 new created/accounted response IDs, all 433
unique task prices and zero unresolved usage/reservation/cache detail. Tokens
for this attempt are input 2,980,510 / output 84,057 / cached 2,296,115 / write 684,137 /
reasoning 49,825. Cached/write counts are subsets of input; reasoning is a subset
of output. The attempt estimate is USD 0.150532575, task USD 0.750842455, remaining
USD 19.249157545 under the respective USD 5 / USD 20 caps. These are frozen token-price
estimates, not invoices. Thirteen transport elapsed fields remain null; no
provider timestamps or retrieval time substitute for missing generation latency.
No all-eight-task successful attempt has been observed, so its cost remains
Not run/null.

Actual finished_at is 2026-10-17T01:28:17.304315+00:00 and exec exit 0. All three
owned processes and observer port 49117 are gone from actual OS records.
Console observations (local-only archive: `.runtime/rebuild-20261016/suites/post-v9/console-observations.json`; excluded from the public checkout)
preserve WinError10054 and the Uvicorn graceful-timeout warning that logged zero
running ASGI tasks. No evaluator interrupt or force-kill occurred; internal helper
abort/cancel counts were not instrumented. Actual normal process/socket closure
Passed does not mean cleanup was fault-free.

The preparation V3 (local-only archive: `evals/rebuild_post_v9_preparation_v3.json`; excluded from the public checkout) binds actual current at
dispatch 209 and full clean 1,292 Passed gates, zero skips, plus real CLI/scope/export/
source-error checks. These overlap and are not added together or substituted for
live task quality. Protocol/oracle bytes, Luna medium / output 12,000 / SDK 30 / tools 32 /
QA 2 and dollar caps stayed fixed. Application 360 was an explicit new latency
condition. No controlled improvement, unseen holdout or general reliability is
claimed. Future application 450 / post-v10 is a separate unexecuted decision; no
further main or Vietnamese VH01 generation is authorized here.

## Historical post-v8 final actual outcome

This section retains the executed V9/application240 observation and prospective
planning as recorded before post-v9. It does not replace the post-v9 result above.

Original result (local-only archive: `evals/rebuild_results_post-v8.json`; excluded from the public checkout),
normal source/lifecycle seal (local-only archive: `evals/rebuild_post_v8_seal.json`; excluded from the public checkout), and
final independent outcomes (local-only archive: `evals/rebuild_post_v8_outcomes.json`; excluded from the public checkout) preserve execution,
accounting and the strict manual verdict separately. Four tasks Passed and four
Failed. Of 39 unchanged requirements, 35 Passed, two Failed and two Not run,
zero Blocked. Qualified partial passes in failed cases are retained with their
notes; they do not mean the requested workflow completed. Seven observed turns
completed and three timed out. Two dependent turns were never dispatched.

| Case | Strict outcome | Actual evidence and limitation |
|---|---|---|
| R01 | Failed | Correct January/June premise, reconciled line/product rows and selective saved-state repairs; current version 4 unverified, no delivered report, alternative follow-up Not run. QA2's ambiguous phrase was in report 2, not the already-caveated saved summary. |
| R02 | Failed | First report mislabeled Surface 69,676.27/97.09% as whole Line-3. Actual whole view 189,934.78/98.22695035460993%; later Critical 95/93 known/2 unknown/65,297.27 and CSV are correct. |
| R03 | Failed | Both full rankings and all swap positions correct; real scenario adoption. Policy-purpose grammar falsely rejected source-bound “treats ... as financially unavailable”. Timeout left version 3 unverified/no report or delivery; Undo Not run. |
| R04 | Passed | Correct method counts/shares/coverage, no unsupported efficacy ranking or causal invention, concrete exposure and ground-truth measurement design. |
| R05 | Failed | Three correct priorities and editable version 4/registered partial ZIP are real. No delivered working export/report before deadline; late final QA verified:true was retained for usage only. |
| R06 | Passed | Natural concise English, canonical quantities, linked numerator/reference denominator and limitations correct. |
| R07 | Passed | Both Vietnamese paraphrases preserve unrounded canonical values, scope and caveats, with natural wording. |
| R08 | Passed | Actual empty cohort, unavailable denominator/intensity/coverage and explicit no-perfect-quality interpretation. |

Root observed five actual R02/R03 CUA boundaries. The Dashboard scopes/exports
were correct even when R02's report was semantically wrong. No sixth Undo gate
is fabricated. The wording review v2 (local-only archive: `.runtime/rebuild-20261016/independent-review/post-v8-wording-review-v2.json`; excluded from the public checkout)
read all seven delivered reports and complete HTML text, 3 EN / 4 VI, retaining the
Surface error. It clarifies that R05 had registered partial files but no
**delivered** working export. Original v1 remains unchanged. Annotation coverage
is 36/37; R08 month_count 6 lacks structured units while human prose says selected
months. Linked/eligible metadata still uses a generic including-unmatched
fallback. No future V10 repair receives credit on this executed V9 evidence.

All 45 archived source/evaluator identities match before/after execution. The
real finish time is 2026-10-16T23:42:48.353107+00:00; observer/Workspace cleanup
and exit 0 were normal, both owned processes gone and port absent from actual OS
socket tables. Independent terminal review checks all 61 new model-start/created
IDs and 347 task IDs, 57 matched reservations, exact Decimal prices and zero
unresolved holds/usage/cache details. The attempt estimate is USD 0.079161825 and
the task estimate USD 0.600309880, below USD5/USD20. Successful complete twelve-turn
suite cost remains Not run, not zero. The current accounting aliases and task
balance are synchronized; the prior mixed-alias snapshot is preserved.

The sealed R05 timing analysis (local-only archive: `evals/rebuild_post_v8_R05_timing_diagnostics.json`; excluded from the public checkout)
uses local events only. Its 240.643154-second runtime span contains 206.416449
seconds of host model-call windows, 13.900245 seconds of tool windows and 20.326460
seconds of unpartitioned gaps. Known transport elapsed totals 189.628 seconds;
the last response elapsed remains null. The final provider QA completed 1.699141
seconds after the nominal deadline with verified:true and was never applied.
Maximum observed output 1770 tokens and input 48,039 do not establish output-limit
pressure. Queue/network causes are unknown.

The V9 final preparation (local-only archive: `evals/rebuild_post_v8_preparation_v5.json`; excluded from the public checkout) binds actual
209-Passed independent and 1,219-Passed full-clean gates, zero skips, with no
provider calls in those gates. The [prospective profile feasibility](rebuild_timeout_profile_feasibility_v1.json)
and post-v9 skeleton (local-only archive: `evals/rebuild_post_v9_preparation.json`; excluded from the public checkout) disclose a separate
360-second application latency condition, still outer 480 / Luna medium / output 12,000,
QA retries 2 / SDK turns 30 / tool calls 32 and unchanged dollar caps. Fresh final-source bindings, gates,
blank manual and explicit paid release are required. Historical 240-second failures,
protocol/oracle bytes, all 8/12/39 tasks and independent evidence remain unchanged.
No controlled improvement, blind holdout or general reliability is claimed.
VH01 stays a separate Not run task.

## post-v7 final actual outcome

This section retains the historical V8/application240 observation. It does not
replace the later post-v9 verdict above.

Original result (local-only archive: `evals/rebuild_results_post-v7.json`; excluded from the public checkout),
normal source/lifecycle seal (local-only archive: `evals/rebuild_post_v7_seal.json`; excluded from the public checkout), and
separate final independent outcomes (local-only archive: `evals/rebuild_post_v7_outcomes.json`; excluded from the public checkout) preserve the
application execution and independent task judgments separately. The original
harness fields are unchanged; its mechanical `Not run` values are not silently
rewritten into manual passes. Final strict review is seven cases Passed and R01
Failed. Of 39 unchanged requirements, 37 Passed, one Failed and one Not run;
Blocked is zero. Ten observed turns completed; R01 timed out and its dependent
follow-up was not dispatched.

R01 retained an unverified finding that labels Category1 total event change as 9
using a Critical-count-change reference. Independent event counts are 44 to 67,
so the total change is 23. It delivered no final report or business artifact.
Partial premise checking, comparisons and persistence do not upgrade the task.
The retained diagnostic (local-only archive: `evals/rebuild_post_v7_R01_diagnostics.json`; excluded from the public checkout) binds authored
tool calls, exact QA input, saved state and same-ID cancellation accounting.

R02's actual model-issued scope and Critical adoption, R03's scenario adoption
and model Undo were observed at six root CUA boundaries. Both full product-cost
rankings matched the independent source order. The delivered report truthfully
distinguishes stable leaders from lower-ranking changes. Undo restores the UI
while retained comparison evidence keeps its earlier scenario context. R05
created a real editable English handover with three priorities, proposed roles,
actions, metrics and source-matching exports. R06 and both R07 paraphrases retain
canonical values and reference-output limitations. R08 preserves an empty cohort
with unavailable denominators, without inferring perfect quality.

The separately authored wording receipt (local-only archive: `.runtime/rebuild-20261016/independent-review/post-v7-wording-review.json`; excluded from the public checkout)
read ten complete reports and HTML text. R06 English is concise and natural,
without adjacent value echoes. Four per-reference structured unit annotations
remain missing: `reference_output` in R06/R07 turn 1,
`known_monthly_output_subtotal` and `output_known_product_count` in R08. Coverage
is 83 of 87 references, with explicit human-facing meanings. Inline percent
omissions with unit notes, the real older v8 artifact named in R03 and R08's
awkward double negative remain recorded. No complete structured-unit or current
mobile-rendering claim is made. VH01 remains a separate Not run task.

The preparation v3 binding (local-only archive: `evals/rebuild_post_v7_preparation_v3.json`; excluded from the public checkout) links the exact
source, helper and unchanged protocol to the current V8 independent 209-Passed
gate and clean 1,168-Passed full gate, both zero skips. Their original failed
attempts and the reviewed test-only representation corrections are retained.
Forty-five source/evaluator files, all 63 created provider response IDs and all
286 task response IDs passed independent terminal/archive/Decimal accounting
review. Normal observer/Workspace cleanup and process exit 0 were observed;
owned processes and listening port are gone. The genuine finish time is
2026-10-16T22:37:46.724926+00:00.

Exact frozen-price estimates are USD 0.100946350 for this eight-case attempt and
USD 0.521148055 for the task. There are no unresolved reservations, unknown
usage receipts or missing cache details. One cancelled R01 response has unknown
transport elapsed time, retained as null. Its original 11-response lifecycle
usage and later read-only receipt of the same ID remain separate. No new
response was generated by reconciliation. These figures are token-price
estimates, not invoice reconciliation, and do not prove controlled performance
improvement over earlier nonblind attempts.

`post-v1` completed zero of the eight frozen tasks. Its actual application
manifest did not change during the run. The Agent produced 28 ledger receipts,
including eight failed tool reads: six oversized or unpaged collection reads and
two unavailable receipt lookups. The largest single model input had 230,854
tokens. Across all 15 responses, including the last cancelled response, usage
was 1,601,843 input tokens, 10,061 output tokens, 782,863 cached tokens, 372,523
cache-write tokens, and 7,117 reasoning tokens. These counts are nested as
described in the accounting section and must not be added together.

The runtime initially recorded 14 terminal usages and retained a USD 0.30
reservation. A read-only provider retrieve confirmed that the remaining
response was cancelled and returned its actual token usage. The evaluator
appended this receipt and settled only that old reservation. No new generation
was created by reconciliation. The raw lifecycle accounting remains preserved
in rebuild_results_post-v1.json (local-only archive: `evals/rebuild_results_post-v1.json`; excluded from the public checkout); the later proof
is rebuild_post_v1_reconciliation.json (local-only archive: `evals/rebuild_post_v1_reconciliation.json`; excluded from the public checkout).

The coordinator briefly requested a pause for a suspected manual-export defect,
then withdrew it after inspecting the already implemented binding. A runtime
next-prompt gate was inserted and removed while R01 was still running, before
any prompt reached that gate. It changed no source, model input, tool result or
active response. The control history is preserved in
rebuild_post_v1_pause.json (local-only archive: `evals/rebuild_post_v1_pause.json`; excluded from the public checkout). The actual timeout and
failed task were not removed or reclassified.

The independent deterministic checks are separate from live tasks. The historical v7
pre-dispatch gate, receipt v10, passed 209 cases in 291.53 seconds, with zero
provider requests and identical before/after manifests. Its receipt SHA-256 is
`955730a8d670d500d7017e68479f71b21b987bf3931c168d8c7204f7d412c44c`.
It independently ran 195 reviewer-authored and 14 coordinator-authored cases.
It preceded the failed post-v6 live attempt and does not upgrade its task verdicts.
A subsequent source repair needs a fresh gate. Historical v9 passed 138 cases in
131.01 seconds on frozen v6; v8 passed 91 in 63.80 seconds on frozen v5; v7 passed
54 in 37.00 seconds on frozen v4. Earlier receipts remain preserved at their
observed revisions. The receipt integration and exact category mapping are
rebuild_deterministic_results.json (local-only archive: `evals/rebuild_deterministic_results.json`; excluded from the public checkout) and
`docs/AGENT_REVIEW.md`. All eight required failure categories are mapped to exact
test nodes, with synthetic/fake-provider evidence explicitly labeled. These
results do not count toward live task completion or rendered browser adoption.

`post-v2` is an explicitly nonblind development rerun of the same eight frozen
cases and twelve turns after evidence-navigation, explicit scope, locale export
and cancellation-accounting repairs. It began on the frozen v3 application;
no task wording, independent expected value or scoring requirement changed.
The actual per-turn outcomes are retained without replacing `post-v1`. The
application source did not change during either attempt.

`post-v2` recorded ten tool receipts, including two rejected investigation
saves. The first attempted save contained product identifier labels that the
validator treated as uncited measurements. The second attempted save used
January evidence for a statement about June and was correctly rejected. This
second error is substantive and cannot be resolved by relaxing numerical
verification. The actual saved work state remained empty at version one.

Across eight terminal responses, usage was 182,901 input tokens, 6,194 output
tokens, 147,572 cached tokens, 33,403 cache-write tokens and 2,053 reasoning
tokens. The maximum single input was 35,308 tokens. Observed response elapsed
times were 72.640, 49.141, 9.906, 12.422, 17.282, 20.406 and 23.485 seconds.
The last response elapsed time is unavailable. Its provider timestamps show
creation and completion, but no queue or network timing was supplied.

The eighth response became terminal after the application returned timeout. A
read-only retrieve returned `cancelled` and actual usage, adding USD 0.00131353
to the original seven-response subtotal. No new generation was created. Total
rebuild spending at that boundary was USD 0.1130109 across 23 responses, with no unresolved
reservation. See rebuild_post_v2_diagnostics.json (local-only archive: `evals/rebuild_post_v2_diagnostics.json`; excluded from the public checkout)
and rebuild_post_v2_reconciliation.json (local-only archive: `evals/rebuild_post_v2_reconciliation.json`; excluded from the public checkout).
That attempt was under its spending cap. The subsequent eight-case attempt is
reported below; a successfully completed twelve-turn suite has not been observed.

The `post-v3` application source manifest was unchanged throughout execution.
It produced 71 terminal provider responses: 2,034,948 input tokens, 60,025 output
tokens, 1,465,249 cached tokens, 540,711 cache-write tokens and 33,968 reasoning
tokens. Actual terminal usage for the R03, R04 and R05 timeout responses was read
without creating generation. Five final-response transport elapsed times across
all three attempts remain unavailable. The total task estimate is USD
0.228163565 across 94 unique response IDs, with no unresolved reservation or
missing generation usage. The [accounting snapshot](rebuild_task_accounting.json)
retains the token totals and unavailable elapsed identities.

The original post-v3 result SHA-256 is
`22cc2db190d293dc9465b59eab0680e8ef7f375374e54927ef9b68804788e096`.
It remains unchanged. The checker originally ignored explicit parent
`result.context.filters` and assumed canonical scope for nested KPIs. The
separate [scope adjudication](rebuild_scope_adjudication_post-v3.json) replayed
605 checks from retained sealed live receipts, corrected exactly 78 false scope
failures, and preserved all six application/artifact failures. Eleven offline
counterexamples require explicit nearest-scope metadata, reject missing/null/
partial/conflicting scopes and prevent separate comparison branches from sharing
an ambiguous parent scope. Observed values, oracle arithmetic and task rubric
did not change. The adjudicated result (local-only archive: `evals/rebuild_results_post-v3_adjudicated.json`; excluded from the public checkout)
retains every original check and initial case verdict beside the corrected ones.
An independent reviewer verified the replay and preservation without a model
judge or API request.

The reviewer checked actual report text, numerical reference targets, saved
investigation transitions and downloaded artifact bytes for all eight cases.
R01 correctly replaced its first hypothesis and tested alternatives. R02 kept
Line-3 when switching to Critical; its CSV contained exactly 95 matching IDs,
93 known costs totaling 65,297.27 and two unknown cost blanks. R06/R07 preserved
unrounded canonical values across English and Vietnamese paraphrases. R08 kept
the empty cohort's denominator, intensity and coverage unavailable. These five
cases met their frozen substantive requirements. Browser adoption was not
rendered during that suite, and HTML presentation/context limitations remain
recorded separately in `docs/AGENT_REVIEW.md`.

R03 timed out with unverified partial findings after real scenario comparison
and view application, with no delivered report or artifact. R04 collected
correct method-filtered data but failed to deliver its response. R05 left an
empty investigation and no handover brief or priorities; its complete data CSV
does not satisfy that request. R05's last cancelled response had 49,184 input,
1,841 output and 434 reasoning tokens. It did not approach the 12,000-output
limit. Actual timings and output types are in
rebuild_post_v3_R05_diagnostics.json (local-only archive: `evals/rebuild_post_v3_R05_diagnostics.json`; excluded from the public checkout);
queue and network timing were not supplied. Acceptance remains incomplete.

The historical `post-v4` source seal confirms that all 42 backend, configuration and
dependency files and both evaluator files were unchanged from pre-dispatch to
suite termination. The original result SHA-256 is
`a6f37112e14b531b8ae9b4b624ad2caf57ae6bbd29944143a7bc2d53edd8f97f`.
It remains unchanged at rebuild_results_post-v4.json (local-only archive: `evals/rebuild_results_post-v4.json`; excluded from the public checkout),
including its original six-response accounting, null exact total, USD 0.30
reservation and unresolved-usage stop reason. The final response was still in
progress for all six reads inside the 30-second window. A later single read of
that exact existing ID returned `cancelled`, 39,631 input tokens, 428 output
tokens, all 428 reasoning tokens, 37,376 cached tokens and zero cache-write
tokens. The separate reconciliation (local-only archive: `evals/rebuild_post_v4_reconciliation.json`; excluded from the public checkout) adds
USD 0.00081326 and settles only that old reservation. No generation was created
and no failed or unrun task was reclassified. At that boundary, task accounting
was USD 0.24403808 across 101 terminal responses, with six unavailable response
elapsed times.

The post-v4 diagnostic (local-only archive: `evals/rebuild_post_v4_diagnostics.json`; excluded from the public checkout) binds the actual tool
arguments, saved draft, semantic QA verdict, exact QA input bytes, local events
and response identities. The initial save used a correct reference-intensity
change citation with the phrase describing events per 1,000 reference units.
Validation rejected the literal `1.000`. The successful resave removed that
unit phrase. Semantic QA later requested explicit units and correctly rejected
the separate ambiguous phrase `giảm -5`. That conflict supports a narrow
unit-scale repair; it does not justify ignoring uncited measurements. The draft
was never promoted to a verified result and no report or business artifact was
delivered.

The exact semantic QA Runner input was 115,409 bytes. Its qualitative-support
subtree used 66,253 bytes across five entries, numeric support 12,702 bytes,
investigation draft 11,815 bytes, delivery 5,241 bytes and ancestor contexts
5,353 bytes under the exact same serializer. The first five analyst response
elapsed times were 10.844, 11.234, 23.047, 21.016 and 21.594 seconds; semantic QA
took 36.219 seconds. The final repair response's transport elapsed time is
unavailable. Its provider creation/completion timestamps are 127 seconds apart,
which does not establish service-only, queue or network timing. Coordinator
whole-project and clean-copy checks ran concurrently at suite start, so these
measurements do not establish a controlled latency improvement.

## Frozen v6 live attempt: post-v5

Original result (local-only archive: `evals/rebuild_results_post-v5.json`; excluded from the public checkout) SHA-256
`e91a8bacbfa939586663ca39eac0b732d45f1b6ca4f4ff3bc99b4af176e54322`
is preserved byte-for-byte, with a ledger snapshot and all 42 consumed production
files plus two evaluator files archived under the suite. The
source seal (local-only archive: `evals/rebuild_post_v5_seal.json`; excluded from the public checkout) records no source/evaluator drift. The
independent outcomes (local-only archive: `evals/rebuild_post_v5_outcomes.json`; excluded from the public checkout) retain the separate final
manual review and each frozen requirement. Five tasks Passed, three Failed;
application timeouts and Unverified returns are not reclassified as successful
delivery when underlying numbers are correct.

R01 passed its two-turn hypothesis/decomposition work. R02 retained Line-3,
then narrowed Critical, with actual root CUA observing both model-issued view
changes before manual interaction. Its 95-row Vietnamese Agent CSV matched the
English UI CSV in every value except export locale. R05 delivered three usable
priorities, proposed role owners, metrics, a reviewed editable JSON brief, English
HTML and a working evidence ZIP; its 14 ZIP member hashes and bilingual source
rows were independently checked. R07 passed its Vietnamese paraphrase and exact
source-scope checks. R08 correctly explained zero events, zero eligible products,
unavailable denominator/intensity/coverage and the limits of zero logged events.

R03 timed out at 242.438 application-boundary seconds, 249.172 including read-only
reconciliation. It applied the exclude-cost-10 view and saved unverified version
4, but delivered no report; the dependent frozen Undo remained Not run. Two
atomic save rejections, the scope-qualification semantic QA issue, exact
71,902/72,899-byte QA inputs and final cancelled response are retained in
its diagnostic (local-only archive: `evals/rebuild_post_v5_R03_diagnostics.json`; excluded from the public checkout). Canonical and scenario
arithmetic and both complete product-cost rankings matched independent source
calculations; that partial evidence does not satisfy completed-task delivery.

R04 timed out at 241.641 application-boundary seconds, 242.734 including
read-only reconciliation. A real ambiguous adjacent-number save failed; QA also
caught a saved event-count marker attached to efficacy prose. Date tokens were
falsely treated as measurements, and a later QA requested more experiment-design
detail. The final completed provider response contained an investigation-write
function call, which was not executed after the application deadline. Exact
42,586/42,775-byte QA inputs and all authored arguments remain in
the R04 diagnostic (local-only archive: `evals/rebuild_post_v5_R04_diagnostics.json`; excluded from the public checkout).

R06 exhausted two repair retries and returned Unverified after 89.844 seconds.
Its final intensity phrase cited the exact metadata scale, role `unit_scale`,
value 1,000, on the same line, but the numeric audit still rejected `1,000`.
The R06 diagnostic (local-only archive: `evals/rebuild_post_v5_R06_diagnostics.json`; excluded from the public checkout) retains all three
reports, audits and source-bound numeric-support entries. R06 delivered no
English report, so R07 proves numerical/source parity only. No completed bilingual
summary pair is claimed.

Some R05/R08 machine per-reference unit annotations are null for count,
within-row percentage, day-of-month or cohort fields. The separate mechanical
checks remain Failed even though human labels and saved unit notes are correct.
The task rubric and these broader artifact requirements remain distinct; neither
is silently removed. Acceptance remains incomplete.

The 62 unique responses used 1,413,026 input tokens, 54,486 output tokens,
959,541 cached tokens, 433,279 cache-write tokens and 28,190 reasoning tokens.
Their exact frozen-price estimate is USD 0.093018885. Cumulative task accounting
is USD 0.337056965 across 163 responses, with no unresolved usage/reservation.
All 163 prices were independently recomputed; eight response transport elapsed
times remain unavailable across the five attempts. Provider timestamps do not
replace those elapsed measurements. Concurrent whole-project checks and source
repairs between attempts prevent a controlled performance comparison.

A separate [Vietnamese handover addendum](rebuild_bilingual_handover_protocol_v1.json)
was frozen before any outcome. Its actual task is **Not run**, excluded from the
main 5/8 rate. The [entrypoint](rebuild_bilingual_handover.py) reuses the unchanged
shared actual app/SDK runner with a separate protocol pointer and its own source
hash. [Offline preparation](rebuild_bilingual_handover_preparation.json) checked
validation, the non-live guard, normal mypy and Ruff without importing the app or
OpenAI, creating a suite or changing the ledger. Fresh source gates and explicit
coordinator release are required before a paid invocation. An earlier Vietnamese
investigation is not substituted for a Vietnamese handover brief.

## Frozen v7 live attempt: post-v6

The original result (local-only archive: `evals/rebuild_results_post-v6.json`; excluded from the public checkout), SHA-256
`e2649a64b4399bf3c2c2a88287dabec79882a4223b5a5271b2665cee602e39ff`,
contains all observed turn boundaries but no final suite summary. It is preserved
byte for byte. All eight cases were attempted; application execution produced
one timeout and ten completed turns. The R01 follow-up is Not run. The
independent task outcomes (local-only archive: `evals/rebuild_post_v6_outcomes.json`; excluded from the public checkout) are five Passed and
three Failed under the unchanged frozen rubric. The R02 scoped cost/Critical
flow, R04 efficacy limitation, R05 editable handover, both R07 paraphrases and
R08 empty-cohort treatment Passed. A working tool path or application QA verdict
was not substituted for substantive task review.

R01 timed out after 246.5 seconds at the application boundary. The bounded
read-only reconciliation used 11.796 seconds and the full measured boundary was
258.937 seconds. All ten response IDs became terminal and were accounted. The
investigation retained six unverified findings at version 3 but delivered no
report. Its correct backend decomposition and intended repair do not satisfy the
whole investigation task or its unrun alternative-hypothesis follow-up.

R03's backend returns correct complete canonical/scenario product rankings. Its
human report falsely says only one lower-ranked pair switches order. Independent
source arithmetic shows two swaps: positions 61/62 (76/27 becomes 27/76) and
92/93 (11/55 becomes 55/11). Application QA verified the false exclusivity claim;
independent comparison review Failed it. Root CUA actually observed scenario
application and then model-issued Undo. The canonical view and costs were
restored, while prior scenario evidence remained as stale historical evidence.
Successful Undo remains separately Passed and does not repair the false answer.

R06 delivered the correct canonical counts, observed cost subtotal, availability,
linked-event numerator and reference denominator. Its answer nevertheless repeats
each main figure as an authored bold literal followed by an inserted cited value.
For example, it reads `**1,071 total defect events** 1,071 [claim:...]`. This fails
the existing natural-English criterion. Exact R05/R06 payloads also preserve
recovered false positives: structural `Priority 1/2/3` headings were classified
as measurements, and an inline `1,000` bound to a current `unit_scale` reference
was rejected before a separate-sentence repair. These are actual intermediate
failures, not synthetic observations or reasons to waive the final rubric.

Normal observer finalization did not finish after all model work was terminal.
The helper's unbounded Uvicorn shutdown can await unfinished ASGI tasks after
closing sockets; the exact live Python await stack was not observed. Root moved
the observer tab to `about:blank`; the listening port and connections were gone,
but the owned harness still waited. The pre-action snapshot (local-only archive: `evals/rebuild_post_v6_cleanup_pre_action.json`; excluded from the public checkout)
proves all eleven database runs and all sixty created response IDs were terminal,
all reservations settled, and all forty-four source/evaluator files exact. Root
then authorized exactly one Ctrl+C through the owned exec session. It returned
exit 1 immediately with no output; both owned PIDs disappeared. No force kill or
new model request was used. Cleanup is Failed. The original raw result was not
backfilled with a synthetic `finished_at_utc`, status or accounting summary.

The separate postmortem seal (local-only archive: `evals/rebuild_post_v6_seal.json`; excluded from the public checkout), SHA-256
`543491734a1b9344f9c86eae105127014023b8520baad6f7ccbe17718a01c838`,
archives the original result, task ledger and exact executed source bytes. Its
before and independently captured after manifests match; it explicitly labels
the latter as a postmortem observation rather than a harness-finalizer receipt.
The preserved suite workspace can be served without a key for read-only review.

The sixty unique responses used 1,146,153 input tokens, 46,548 output tokens,
724,909 cached tokens, 419,890 cache-write tokens and 23,884 reasoning tokens.
The exact frozen-price estimate is USD 0.08314474. Task accounting is
USD 0.420201705 across 223 unique response IDs, with zero unresolved reservation,
unknown usage or unavailable cache detail. Every price was independently
recomputed from observed token usage. Nine transport elapsed fields across the
six attempts remain null. No provider queue/network timing or controlled latency
improvement is claimed. A fully successful twelve-turn suite remains Not run.
VH01 remains separately Not run and unreleased.

## Execution and independent verification

The recorded attempts sent requests through the real FastAPI HTTP handlers using
an ASGI transport. The production `Workspace` invoked the installed official
Agents SDK runner and real OpenAI Responses API, with real backend tools. These
observations verify the server path used by those attempts; separate browser
verification covered the rendered user interface. No model transport, tool result
or report was replaced with a fixture in a live run. Hosted tracing, hosted
search, hosted file storage, MCP, and arbitrary model code execution were
disabled for the evaluated application. Local durable events retained the
response identity, tools, arguments, results, errors and usage.

The standard-library oracle fetches the defect source through HTTP, parses the
master CSV, and independently applies the declared duplicate, date, conflict,
cost and scope rules. It imports no production preparation or analysis code.
Expected values stay in evaluator files and are never supplied to the model.
The seven-scope no-cost preflight is recorded in
rebuild_preflight.json (local-only archive: `evals/rebuild_preflight.json`; excluded from the public checkout). This preflight is backend
arithmetic evidence and does not count as live Agent task completion.

The live runner records each tool receipt and quantitative claim reference,
checks receipt/code seals, compares scoped KPIs with independent calculations,
and downloads every delivered artifact through its owning session/run endpoint.
A successful download and matching hash establish artifact availability and
identity. Task-specific content, hypothesis handling, natural language and
usable priorities require a separate observable evidence review. Application QA
is recorded but is not the evaluator's oracle or task verdict.

`post-v3` was dispatched on the frozen v4 implementation as a further nonblind
development rerun. It retains the same eight cases, twelve planned turns,
independent oracle and rubric. The effective application deadline remains 240
seconds, and the separate outer harness wait remains 480 seconds. Actual
outcomes are retained separately from both earlier failures.

Before `post-v3`, evaluator-only instrumentation added a bounded read of existing
response IDs at the completed application boundary. It reads for at most 30
seconds within the remaining outer allowance and never creates or cancels a
generation. Fully accounted ordinary failures remain failed; dependent follow-ups
remain unrun, while independent later cases may proceed on fresh sessions.
Unknown usage, provider stops, source drift and budget holds still stop further
dispatch. Both `error` and `incomplete_details` provider-stop metadata are checked.
The offline proof ran 18 checks with no network or model generation; its scope is
accounting instrumentation, not live Agent quality. See
[rebuild_boundary_reconciliation_amendment.json](rebuild_boundary_reconciliation_amendment.json).

## Reproduction

Run from the application root with the existing server-side key. The first four
commands do not dispatch a model request. The final line is the recorded paid
post-v6 invocation, not authorization to overwrite or repeat it:

```powershell
.\.venv\Scripts\python.exe scripts\run_rebuild_eval.py validate
.\.venv\Scripts\python.exe scripts\run_rebuild_eval.py preflight
.\.venv\Scripts\python.exe scripts\run_rebuild_eval.py summarize
.\.venv\Scripts\python.exe evals\rebuild_scope_adjudication.py --suite-id post-v3 --verify-only
.\.venv\Scripts\python.exe scripts\run_rebuild_eval.py run --live --suite-id post-v6 --observe-ui
```

Use a fresh suite identifier for an explicitly labeled rerun. The runner refuses
to overwrite a suite, does not resume a partially submitted request by replaying
it, and never automatically substitutes another model. A limited development
rerun can select frozen cases with repeated `--case R01` arguments. Such runs
remain visible in the same task spending ledger and are not blind holdouts.

Each suite saves the actual application source hashes, plus executable harness
and observer helper hashes and copies, before dispatch and checks them before
each turn and after the suite. Source changes stop further dispatch and keep
the affected result incomplete. The source HTTP hash must equal the frozen
independent source. Rebuild code and data should remain stable for a complete
suite. The application uses one local-history continuation strategy.

`--observe-ui` exposes the same app and Workspace through a loopback-only
ephemeral server with its lifecycle disabled. R02/R03 wait before first dispatch
and after terminal, accounted turns for root CUA observation. All waits are
recorded separately from turn/model latency. The helper publishes
`browser-observer/observer-current.json` with the exact URL, session, full
context, required latest command and root marker template. It requires the exact
marker, an actual session WebSocket listener, the matching UI context and
unchanged product assets. Bootstrap chooses the already existing session and
clears its old draft; it never sends a prompt. The root aligns Dashboard scope
before first mounting Agent and keeps the same mounted page through both turns.
The offline simulated-client smoke passed two flows and seven checks, while the
root CUA bootstrap smoke checked storage/redirect/listener setup. In post-v5,
root observed actual Luna-issued R02 Line-3 Evidence and Critical adoption, plus
partial R03 scenario application. The R03 parent timeout remains Failed and its
frozen Undo is Not run. Root markers, DOM/screenshots, context IDs and separately
measured waits remain in the observer records and docs evidence.
In post-v6, all six actual R02/R03 browser barriers were observed, including
Critical adoption, scenario apply and model Undo. The actual UI and Agent
Critical CSVs were byte identical; canonical/scenario CSVs and visible scope
were read back. The separate cleanup failure described above remains Failed.
See [rebuild_browser_observer_amendment.json](rebuild_browser_observer_amendment.json).

## Evidence and accounting

All new state is isolated under `.runtime/rebuild-20261016`. Each suite has its
own workspace, sessions, run results, source manifest, local traces and artifact
downloads. Per-response spending is appended to `task_spend.jsonl` at that root.
The runner does not read historical `.runtime` costs or old task results as
current evaluation evidence.

The frozen limits are USD 5 for a complete suite and USD 20 for this rebuild
task. The pre-dispatch estimate was USD 0.20 to USD 1.00 for the suite; it is an
estimate, not a measured result. Each turn reserves USD 0.30, and the application
enforces its own token-cost bound before model dispatch. Unknown unresolved
generation usage retains the conservative reservation and stops subsequent
dispatch.

Token-price estimates use the [official Luna model prices](https://developers.openai.com/api/docs/models/gpt-6-luna),
verified on 2026-10-16: per million tokens, USD 0.10 input, USD 0.01 cached input,
USD 0.125 cache writes, and USD 0.50 output. Cached tokens are included in input
tokens; reasoning tokens are included in output tokens. Run aggregates are not
added again to per-response totals. Missing token details or latency are
unavailable, never invented as zero. These estimates are not provider invoice
reconciliation. No hosted tool fee is authorized or expected for this suite.

When total input/output usage is observed but cache details are unavailable,
the exact token-price total remains null. The transport's conservative cost
upper bound counts toward the budget instead. A generation with missing total
usage keeps its reservation and stops subsequent dispatch. This pre-dispatch
accounting correction is documented in `rebuild_accounting_amendment.json`.

Every check uses exactly `Passed`, `Failed`, `Blocked`, or `Not run`.
The primary result is strict completed tasks divided by the eight frozen tasks.
Partial cases, unverified exports, unsupported claims, scope drift and absent
behavior evidence cannot count as completed tasks. This small case suite
establishes only the behaviors it covers.
