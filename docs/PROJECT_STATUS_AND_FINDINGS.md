# Sampling Project — Status and Findings

Date: 2026-08-08

Everything below was measured by running the code. Section 5 lists the one piece that was not run
and what it costs.

Deliverable: **`docs/Sampling_Project_Report.docx`** — the original filename, with all corrections
merged in. 540 paragraphs, 13 figures, generated from `results/experiment_results_v2.json` by
`src/create_report_v2.py`. No number in it is hand-copied.

**File layout:**

| Path | Role |
|---|---|
| `docs/Sampling_Project_Report.docx` | **The report.** Corrected and extended. |
| `docs/archive/Sampling_Project_Report_round1_backup.docx` | Byte-copy of the report as it stood before the merge. |
| `src/run_experiment_v2.py`, `src/create_report_v2.py` | Current generators. |
| `src/run_experiment.py`, `src/create_report.py` | First-round scripts, kept for provenance. `create_report.py` writes `docs/archive/Sampling_Project_Report_round1.docx` so it cannot overwrite the current report. |
| `results/`, `results/figures/` | Numerical results and every figure. |
| `data/` | Input traces, not committed. See `data/README.md`. |

Every script resolves paths relative to the repository root, so all of them can be run from any
working directory.

Five figures that the extended round had dropped were regenerated from the corrected chains rather
than reused from round one, because three of them (`comparison_bars`, `rhat_convergence`,
`autocorrelation`) displayed superseded numbers and would have contradicted the text. One new
figure, `v2_rhat_comparison.png`, plots plain against split R-hat as the chains lengthen and shows
directly why the first round's diagnostic missed the problem.

---

## 1. Headline finding: plain MH never converged

The first round concluded that "all three methods approximate the same posterior distribution,
providing evidence for implementation correctness." **That claim is not supported.** It rested on
the plain Gelman-Rubin statistic, which returned 1.0267 or below for everything.

Split R-hat, which halves each chain so that within-chain drift also inflates the statistic, tells
a different story:

| Sampler | Worst split R-hat | Largest deviation from Gibbs posterior mean |
|---|---|---|
| MH | 1.47 | **0.315** |
| Adaptive MH (naive) | 9.66 | **0.489** |
| Preconditioned MH | 1.01 | 0.00061 |
| Gibbs | 1.00 | — |
| HMC | 1.00 | 0.00053 |

For scale, the widest direction of the posterior has a standard deviation of 0.044. MH's error is
**seven times larger than the entire posterior spread**. It is not inefficient; it is wrong.

The positive side of the same table: Gibbs, HMC and preconditioned MH — exact conditionals,
gradients, and a random walk respectively — agree to within 0.0006. That mutual agreement is the
strongest correctness evidence in the project.

## 2. Why MH fails, and what fixes it

The posterior covariance `(XᵀX/σ² + I/τ²)⁻¹` has **condition number 273** and a maximum
coefficient correlation of **0.772**, because `CPU_lag_1/2/3` and `CPU_rolling_mean` measure nearly
the same quantity. One isotropic step size must fit a direction of sd 0.0027 while crossing one of
sd 0.044.

Two repairs were tried:

- **Textbook adaptive Metropolis (learning the covariance from the chain) failed outright** —
  acceptance collapsed to 1.2%, split R-hat 9.66. The reason is a bootstrap problem: the
  unpreconditioned chain has an autocorrelation time of ~1,000, so 2,000 burn-in iterations supply
  only a couple of effectively independent points, nowhere near enough for a 12-dimensional
  covariance. What it actually learns is the transient drift toward the mode, whose spread is much
  larger than the posterior; scaling that by 2.38²/d makes proposals so big that everything is
  rejected. This failure is reported in the document rather than hidden — it is a genuine result.
- **Preconditioning with the observed Fisher information worked.** ESS 9.3 → 217.2 (×23),
  acceptance 27.5% against the theoretical optimum of 23.4%, and posterior means that match Gibbs.
  Still an order of magnitude behind Gibbs and HMC, because preconditioning removes the anisotropy
  penalty but not the random walk.

## 3. The calibration explanation was wrong, and the fix improves accuracy

Round one attributed 95% intervals covering 98.7% to "weakly informative priors." A plain OLS fit,
with no priors and no MCMC, reproduces the same over-coverage — so the priors cannot be the cause.

The real cause is the residual distribution: **excess kurtosis 108.9**, sd/(1.4826×MAD) = 4.24.
CPU traces idle then spike, and a Gaussian likelihood has one parameter to describe both regimes,
so σ² inflates to cover the spikes and every interval is sized for an event that rarely happens.

A Student-t likelihood (ν = 4), sampled as a normal scale mixture so all conditionals stay
conjugate:

| Metric | Gaussian | Student-t | Nominal |
|---|---|---|---|
| 50% interval coverage | 94.5% | **73.7%** | 50% |
| 95% interval coverage | 98.7% | **96.3%** | 95% |
| Mean 50% interval width | 0.534 | **0.068** | — |
| Median absolute error | 0.0462 | **0.0186** | — |

Calibration improves substantially and the typical-case error drops 60%. It is not perfect: 73.7%
against a nominal 50% still over-covers, because real CPU traces are not exactly Student-t either.

## 4. A diagnostic correction that partly mattered

The old ESS estimator truncates the autocorrelation sum at the first lag below 0.05. Run on 10,000
genuinely independent draws it returns exactly 10,000, so it cannot distinguish an excellent
sampler from a perfect one.

Correcting it to Geyer's initial monotone positive sequence estimator **changed HMC (6,183 →
7,997) but not Gibbs (10,000 → 10,000)**. In this two-block conjugate model the Gibbs draws really
are near-independent, so the old number was right by luck. Worth stating honestly rather than
claiming the correction overturned it.

## 5. External validation — done, with a substitution

The template requires a comparison against an existing tool
("השוונו את המכונה שלנו למכונה אחרת הקיימת בשוק"). This is Section 5.4 and it is complete.

The original plan was PyMC / NUTS. PyMC compiles its log-posterior through PyTensor, no C++
compiler is installed on this machine, and the pure-Python fallback had not finished three chains
after ~25 minutes of CPU. **emcee** was used instead: an established third-party library
implementing the Goodman-Weare affine-invariant ensemble sampler, an algorithm unrelated to any of
ours, and pure Python so it needs no toolchain.

It produced 120,000 draws in a few seconds and reproduces the Gibbs posterior means to within
**0.0008**. That last figure is the one number in the project that moves between runs, by roughly
0.0002: emcee draws from the global NumPy random state rather than from a seeded generator, unlike
every sampler written here. Because it was written by someone else and uses a different algorithm, it tests the
model transcription itself, not just our sampling of it — the one thing internal agreement between
our own three samplers could not establish.

It also independently confirms the failure diagnosis: plain MH and the naive adaptive variant
deviate from the external reference by roughly the same amounts they deviate from Gibbs, ruling out
the possibility that Gibbs and HMC were the ones in error.

The PyMC path remains in the code and runs by default; `--skip-nuts` omits only that.

## 6. Remaining tasks

1. **Push to GitHub — blocked on authentication.** The repository URL
   (`https://github.com/eladagmi24/mcmc-sampling-project`) is set in `create_report_v2.py` and
   appears in the report. The project is committed locally on `main`, one commit ahead of
   `origin/main`, 9.4 MB across 35 files. The push returns HTTP 403: Git Credential Manager on
   this machine holds credentials for the GitHub account **EladDagmi**, while the repository
   belongs to **eladagmi24**, which has no write access for that identity. Either sign in as
   `eladagmi24`, or add `EladDagmi` as a collaborator on the repository. Nothing is lost — the
   commit is made and `git push origin main` will complete once the identity is right.
2. Optional: also run the PyMC / NUTS reference, which would add a second external
   implementation. Not required — Section 5.4 is already satisfied by emcee.
3. Optional extension: put a prior on ν and sample it, instead of fixing ν = 4.

## 7. How to regenerate everything

From the repository root:

```
python src/run_experiment_v2.py --skip-nuts   # ~1 minute -> results/ JSON + figures
python src/create_report_v2.py                # ~5 seconds -> docs/Sampling_Project_Report.docx
```
