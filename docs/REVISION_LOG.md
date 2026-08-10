# Revision log

All experiments were rerun from scratch and the report regenerated from
`results/experiment_results_v2.json`. No number in the document is hand-copied.

Reproduce with:

```
python src/run_experiment_v2.py --skip-nuts     # ~18 minutes
python src/create_report_v2.py
```

---

## 1. Data and evaluation

| Requirement | Status |
|---|---|
| Chronological train/validation/test split | Done — 60/20/20, giving 3,000 / 688 / 680 rows |
| Scalers fitted on training data only | Done |
| Validation used to select likelihood and ν | Done — by mean log pointwise predictive density |
| Test set used only for final numbers | Done |
| Forecasting horizon stated | Done — one step, **5 minutes**, stated in abstract and §3.2 |
| Contemporaneous features lagged | Done — memory, disk and network are now used at t−1, making this genuine forecasting rather than nowcasting. **This changed the design matrix, so all results differ from the previous version.** |
| Justify 5,000 of ~430,000 observations | Done — §3.4, computational, with the consequence for test-set precision stated |
| No leakage across splits | Done — embargo drops any row whose 6-step feature window crosses a boundary (632 rows) |
| Residual autocorrelation diagnostic | Done — ACF plus Ljung-Box, §6.4 and Figure 8 |
| Discussion of pooling 50 VMs | Done — §3.5, per-VM residual sd ranges 0.000–1.575 (median 0.049) across 48 machines against a pooled 0.349 |

Note on VM leakage: the same machine appears in several splits at different times. That is the
intended design for time-series forecasting, and §3.3 states explicitly that results describe
forecasting for already-observed machines, not generalisation to unseen machines.

## 2. MCMC diagnostics

| Requirement | Status |
|---|---|
| Four independently initialised chains | Done, from overdispersed starts |
| Rank-normalised and folded R-hat | Done — reported value is the max of the two |
| Bulk and tail ESS, computed jointly | Done — joint across-chain estimator, not per-chain averages |
| Define exactly how ESS is aggregated | Done — §2.7: worst case (max R-hat, min ESS) over monitored parameters |
| ESS/second from the same draws and runtime | Done |
| Do not call Preconditioned MH converged at R-hat 1.0128 | Done — a longer run was added (40,000 draws/chain → R-hat 1.0024), and the shorter run is described as borderline |
| MCSE from joint ESS | Done, and reported only for converged chains |
| Dispersed starting values | Done — β ~ N(0, 2²), log σ² ~ N(0, 1.5²) |

The diagnostics are implemented from scratch in `src/mcmc_diagnostics.py` and **verified against
ArviZ** across six stress cases (independent, AR(0.95), offset chains, heavy-tailed, smoothed,
near-constant): R-hat agrees to 2×10⁻⁵ and ESS to 0.4%.

Two bugs were found and fixed during that verification: the Geyer pairing started at lag 1 instead
of lag 0, dropping the ρ₀ term (an off-by-two in τ), and tail-ESS rank-normalised a binary
indicator, which is degenerate. Both affected the previously reported ESS values.

## 3. External validation

| Requirement | Status |
|---|---|
| Clarify what emcee does and does not validate | Done — §5.5 uses the suggested wording |
| Replace the "proves the model is correct" claim | Done |
| Add an independent reference for likelihood and priors | Done — **a deterministic analytical reference**, not another sampler |
| Report emcee convergence diagnostics | Done — R-hat 1.0224, min bulk ESS 1,885, plus a caveat that this is above our own threshold |

The analytical reference marginalises β in closed form (`y | σ² ~ N(0, σ²I + τ²XX')`, via
Sylvester and Woodbury) and integrates the remaining 1-D density over σ² on a 6,000-point grid.
It involves no sampling at all, so it validates the likelihood and prior algebra independently of
any sampler. Gibbs reproduces it to within 1.4×10⁻⁴.

## 4. Mathematical corrections

- Preconditioner formula corrected in **both the text and the code**. The code previously computed
  σ̂²(XᵀX + I/τ²)⁻¹, which is wrong. It now computes σ̂²(XᵀX + σ̂²I/τ²)⁻¹ = (XᵀX/σ̂² + I/τ²)⁻¹.
- Priors for β and σ² stated separately; both Gamma and Inverse-Gamma declared in **shape-rate**
  form, with a note that NumPy takes a scale argument so rates are passed as reciprocals.
- HMC transformation ψ = log σ² and its Jacobian term documented in §2.5, with a statement of
  which samplers use it and which do not.
- All symbols in the R-hat and ESS formulas defined in §2.7 (M, N, S, W, B, ρ_t, τ).

## 5. NUTS and sampler consistency

- Every claim that emcee is a NUTS reference removed; the report states plainly that NUTS was not
  used and that no NUTS results are reported.
- Section renamed "Hamiltonian Monte Carlo".
- One consistent classification: five samplers target the Gaussian posterior; Student-t Gibbs
  targets a different model and is discussed separately in §6.
- "Adaptive (preconditioned) MH" eliminated — the two are now distinct samplers throughout.

## 6. Predictive intervals and calibration

- "Posterior predictive interval" used throughout. "Credible interval" survives in exactly one
  place, §6.1, where the two are deliberately contrasted.
- Construction explained: parameter uncertainty and observation noise, empirical quantiles of
  replicates.
- Coverage reported with **moving-block bootstrap** 95% intervals (block 50, 2,000 replicates).
- ν selected on validation data over a grid; sensitivity shown in Figure 6.
- Described as a **partial improvement**: Gaussian 94.7% → Student-t 74.1% against a nominal 50%
  is still about 1.5× too wide, and the report says so.

## 7. Sensitivity analyses

- HMC now varies ε **and** L jointly (4 × 3 grid) and reports **bulk ESS per gradient evaluation**.
- Grid runs are warm-started with long burn-in, and each cell reports R-hat and a converged flag.
  Only 2 of 12 cells converged; the comparison is restricted to those and the rest are marked.
- The best converged cell (ε=0.002, L=5) has **96.8%** acceptance while the highest acceptance in
  the grid (99.2%) is less efficient — the report uses this to argue explicitly that high
  acceptance is not evidence of good tuning, and that the step size is conservative.
- Stationarity rule defined precisely: within 1% of the median log posterior of the final 500
  iterations, sustained for 50 consecutive iterations.
- §5.7 corrected: the dispersed (+3) start **is** harder than the extreme-variance start for both
  local samplers, and the report now says so with the numbers.
- Adaptive MH failure described as specific to the tested burn-in and adaptation configuration.

## 8. Results and reporting

- OLS median absolute error added.
- All errors reported in **CPU percentage points** alongside standardised units.
- Runtimes reported as mean ± sd over 3 independent repeats; ESS error bars in Figure 3.
- Non-converged samplers excluded from every ranking, with an explicit paragraph explaining why.
- The "seconds per sample" phrasing is gone; runtime is stated as the total for 4 chains × 10,000
  draws.
- The 94.3 / 94.5 discrepancy is resolved — there is now a single generated source for coverage.

## 9. Abstract and academic writing

- Abstract is **242 words** (was ~500), covering objective, data, methods, findings, conclusion.
- "Extended Edition" and the first-round narrative removed; the report presents the final study.
- Three research questions stated in §1 and answered in §7.
- Overclaiming words removed.
- **Numbered citations only.** A citation registry assigns numbers on first use and emits only
  cited entries, so bibliography and in-text citations cannot diverge — verified 16 cited / 16
  listed.
- emcee cited (Foreman-Mackey et al. 2013) plus Goodman & Weare 2010; Bitbrains cited with DOI and
  dataset URL.

## 10. Figures and layout

- Blank page removed (3 deliberate page breaks, none consecutive).
- Page numbers added via a PAGE field in the footer.
- Automatic TOC field inserted (`TOC \o "1-3" \h \z \u`) — see open items below.
- All 12 tables have a repeating header row (`tblHeader`) and `cantSplit`.
- Headings and figure images use keep-with-next so captions stay with their figure.
- **All 10 figures have alt text.**
- Figure 1 redrawn with logarithmic count axes and log x-axes for the five skewed channels.
- Table column widths set explicitly to stop "Preconditioned MH" wrapping.

---

## Follow-up round: Student-t diagnostics, title page, README sync

- `robust_student_t_gibbs` now accepts `initial_parameters` and starts from an overdispersed point
  by default, matching the Gaussian samplers. Mixture weights still start at one.
- The selected Student-t model is fitted with 4 dispersed chains and diagnosed with the same
  `summarise_sampler` helper, thresholds and worst-case aggregation as Section 5: R-hat 1.0012,
  minimum bulk ESS 2272, converged True. Section 6 now reports those chains **pooled**, so its
  numbers moved in the third or fourth decimal.
- Section 6.3 states the Student-t diagnostics explicitly, so the model behind the headline
  predictive numbers is held to the project's own standard.
- Section 5.6 now discloses that the HMC grid is warm started rather than dispersed, and that it
  ranks configurations rather than certifying them.
- Title page: course and lecturer filled in; institution and student IDs left as blanks to be
  completed by hand.
- The table of contents field is marked dirty, so Word rebuilds it on open with no keypress.
- README numbers regenerated from the current results file; the "fixes the calibration" phrasing
  softened to match the report, which is careful that the intervals remain miscalibrated.

## Reviewer feedback round (August 2026)

### Code changes (run_experiment_v2.py, mcmc_diagnostics.py)

- **All 12 parameters monitored.** `REPORTED_PARAMETERS` expanded from 4 (indices 1, 3, 6, 10) to
  all 11 regression coefficients plus sigma^2. This exposed a coordinate (Disk_Read_KBps_lag1,
  R-hat 1.0109) that pushed Preconditioned MH above the 1.01 threshold at the standard 10,000
  draws — the convergence flag **flipped from True to False**. The long run (40,000 draws) remains
  converged at R-hat 1.0033.
- **L=15 added to HMC sensitivity grid**, making it 4×4 = 16 cells. Only 1 of 16 converged
  (ε=0.002, L=15) — down from 2 of 12 before, because monitoring all 12 parameters is stricter.
- **Degenerate-chain R-hat returns None** instead of astronomically large numbers
  (`DEGENERATE_VARIANCE_THRESHOLD = 1e-10` in `mcmc_diagnostics.py`).
- **Stationarity rule replaced.** The "within 1% of stationary level for 50 consecutive iterations"
  criterion was replaced with a dispersion-based rule: median ± 3·MAD of the final 500 iterations,
  sustained for the remainder of the chain. Key renamed `iterations_to_stable_region`.
- **Panel-aware residual diagnostics.** ACF and Ljung-Box are now computed per machine, never across
  machine boundaries. Reported values: median per-machine lag-1 ACF, per-machine Ljung-Box
  rejection count and fraction, adaptive lag count (`min(max_lag, n//3)` per machine).
- **Cluster bootstrap** replaces block bootstrap for coverage intervals: resamples machines with
  replacement, keeping each machine's observations intact.
- **Gibbs acceptance rate → None** instead of 1.0 (structural property, not a tuning diagnostic).
- **Inverse-Gamma terminology fixed.** Docstring now reads: "1/σ² ~ Gamma(a0, rate=b0),
  equivalently σ² ~ Inverse-Gamma(a0, scale=b0)."

### Report text changes (create_report_v2.py)

- §2.7 "Aggregation across parameters" now says all 12 parameters, not 4.
- §2.1 Inverse-Gamma formula shows both Gamma (shape-rate) and Inverse-Gamma (shape-scale) forms.
- §5.2 Convergence narrative updated: "Two samplers meet the criterion at the standard run length
  and three do not." Preconditioned MH described as not converged at 10k, converging at 40k.
- §5.4 Efficiency table handles None acceptance (Gibbs → N/A). Ranking paragraph no longer claims
  HMC is the most efficient per draw; Gibbs leads both per second and per draw.
- §5.7 Stationarity rule description updated to MAD-based. Key `iterations_to_stable_region`.
  Initialisation narrative revised to match new data.
- §6.4 Residual paragraph uses panel-aware keys (median per-machine ACF, Ljung-Box rejection count).
- §6.5 Calibration table caption: "cluster-bootstrap" replaces "moving-block bootstrap".
- §6.5 Coverage interpretation: "about 1.5 times wider" replaced with "covering roughly X times the
  intended fraction of observations" to avoid conflating coverage ratio with width ratio.
- §7 RQ1: "Two of the five" instead of "Three of the five". RQ2: Gibbs most efficient per draw,
  not HMC.
- §8 Conclusions updated to reflect the Preconditioned MH flip.
- HMC grid table handles None R-hat (displays "degenerate").
- Abstract updated: "Gibbs and HMC converge; preconditioned Metropolis converges only with a
  longer run."

## Open items

1. **Title page** — course and lecturer are filled in. Institution and student ID numbers remain
   underscore blanks, since I do not know them; fill them in directly or edit
   `TITLE_PAGE_PLACEHOLDERS` in `src/create_report_v2.py`.
2. **The TOC is built by Word on open.** The field is marked dirty, so Word rebuilds it with page
   numbers and links when the document is opened; no user action is needed.
3. **No visual page-by-page inspection.** The document was verified structurally by parsing its
   XML (page breaks, fields, header rows, alt text, no stray format specifiers). Rendering to PDF
   requires Word or LibreOffice, neither of which is available here, so orphaned headers and
   awkward pagination could not be confirmed visually.
4. **PyMC/Stan not run.** The independent-specification check is satisfied by the analytical
   quadrature reference instead, which is arguably stronger for validating the likelihood and
   priors. PyMC remains unusable here without a C++ compiler.
5. **ν sits at the edge of the grid** (ν = 2, infinite variance). The report flags this and
   recommends a prior on ν rather than accepting an edge solution.
6. **Stale artefacts.** `results/figures/` still contains figures from the previous round that the
   current report does not reference. The flat course folder outside the repository also still
   holds superseded copies of some scripts and the old report. Neither was deleted, since the
   repository reorganisation was not mine to undo.
