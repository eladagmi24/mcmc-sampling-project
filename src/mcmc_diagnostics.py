"""Convergence and effective-sample-size diagnostics following Vehtari et al. (2021).

Notation used throughout, and in the report:
    M   number of chains after splitting (each of the C sampled chains is split in half, M = 2C)
    N   number of draws in each split chain
    S   total number of draws, S = M * N
    W   mean within-split-chain variance
    B   between-split-chain variance
    rho_t   autocorrelation at lag t, estimated jointly across chains
    tau     integrated autocorrelation time

Every estimator here is written from scratch. `cross_check_against_arviz` compares the results
with ArviZ, the reference implementation of the same paper, so the implementation is verified
rather than merely asserted.
"""
import numpy as np
from scipy import stats

MINIMUM_VARIANCE = 1e-300


def split_chains(chains):
    """Split every chain in half, so that within-chain drift also inflates the statistics."""
    split = []
    for chain in chains:
        half_length = len(chain) // 2
        split.append(np.asarray(chain[:half_length], dtype=np.float64))
        split.append(np.asarray(chain[half_length:2 * half_length], dtype=np.float64))
    return np.array(split)


def rank_normalize(draw_matrix):
    """Rank-normalise draws to a standard normal scale using the Blom transformation.

    Ranks are computed over the pooled draws from every chain, averaging ties, and then mapped
    through the inverse normal cdf. This makes the diagnostics invariant to monotone
    transformations and robust to heavy-tailed or infinite-variance targets.
    """
    flat = draw_matrix.reshape(-1)
    ranks = stats.rankdata(flat, method='average')
    total_draws = len(flat)
    normal_scores = stats.norm.ppf((ranks - 3.0 / 8.0) / (total_draws - 1.0 / 4.0))
    return normal_scores.reshape(draw_matrix.shape)


def classical_potential_scale_reduction(split_draws):
    """R-hat = sqrt(var_plus / W) on already-split chains."""
    chain_count, chain_length = split_draws.shape
    chain_means = split_draws.mean(axis=1)
    within_variance = split_draws.var(axis=1, ddof=1).mean()
    if within_variance <= MINIMUM_VARIANCE:
        return 1.0
    between_variance = chain_length * chain_means.var(ddof=1)
    variance_plus = (chain_length - 1) / chain_length * within_variance \
        + between_variance / chain_length
    return float(np.sqrt(variance_plus / within_variance))


def rank_normalized_rhat(chains):
    """Rank-normalised split R-hat."""
    return classical_potential_scale_reduction(rank_normalize(split_chains(chains)))


def folded_rank_normalized_rhat(chains):
    """Folded rank-normalised split R-hat, which is sensitive to differences in scale."""
    split = split_chains(chains)
    folded = np.abs(split - np.median(split))
    return classical_potential_scale_reduction(rank_normalize(folded))


def maximum_rhat(chains):
    """The reported R-hat: the larger of the rank-normalised and folded rank-normalised values."""
    return float(max(rank_normalized_rhat(chains), folded_rank_normalized_rhat(chains)))


def _autocovariance_by_fft(sequence):
    length = len(sequence)
    centered = sequence - sequence.mean()
    padded_length = 1
    while padded_length < 2 * length:
        padded_length *= 2
    spectrum = np.fft.rfft(centered, padded_length)
    autocovariance = np.fft.irfft(spectrum * np.conjugate(spectrum), padded_length)[:length]
    return autocovariance / length


def effective_sample_size_from_split_draws(split_draws):
    """Joint across-chain ESS using the Stan / Vehtari estimator.

    The autocorrelation is pooled across chains through
        rho_t = 1 - (W - mean_m[s_m^2 rho_{t,m}]) / var_plus,
    which accounts for between-chain variation, and the sum is truncated by Geyer's initial
    monotone positive sequence rule. The result is a single number for all M*N draws jointly,
    not a per-chain value that is then averaged.
    """
    chain_count, chain_length = split_draws.shape
    if chain_length < 4:
        return float(chain_count * chain_length)
    chain_variances = split_draws.var(axis=1, ddof=1)
    within_variance = chain_variances.mean()
    if within_variance <= MINIMUM_VARIANCE:
        return float(chain_count * chain_length)
    chain_means = split_draws.mean(axis=1)
    between_variance = chain_length * chain_means.var(ddof=1)
    variance_plus = (chain_length - 1) / chain_length * within_variance \
        + between_variance / chain_length
    weighted_autocovariance = np.zeros(chain_length)
    for chain_index in range(chain_count):
        autocovariance = _autocovariance_by_fft(split_draws[chain_index])
        if autocovariance[0] <= 0:
            continue
        autocorrelation = autocovariance / autocovariance[0]
        weighted_autocovariance += chain_variances[chain_index] * autocorrelation
    weighted_autocovariance /= chain_count
    pooled_autocorrelation = 1.0 - (within_variance - weighted_autocovariance) / variance_plus
    total_draws = chain_count * chain_length
    maximum_pairs = (chain_length - 1) // 2
    if maximum_pairs < 1:
        return float(total_draws)
    pair_sums = pooled_autocorrelation[0:2 * maximum_pairs:2] \
        + pooled_autocorrelation[1:2 * maximum_pairs + 1:2]
    non_positive = np.where(pair_sums <= 0)[0]
    retained_count = non_positive[0] if len(non_positive) else len(pair_sums)
    if retained_count < 1:
        retained_count = 1
    retained = np.minimum.accumulate(pair_sums[:retained_count])
    integrated_time = max(-1.0 + 2.0 * retained.sum(), 1.0 / np.log10(total_draws))
    return float(total_draws / integrated_time)


def bulk_effective_sample_size(chains):
    """Bulk-ESS: ESS of the rank-normalised draws, describing the centre of the distribution."""
    return effective_sample_size_from_split_draws(rank_normalize(split_chains(chains)))


def tail_effective_sample_size(chains):
    """Tail-ESS: the smaller of the ESS values for the 5% and 95% quantile indicator series.

    The indicator is used directly rather than rank-normalised: it takes only two values, so
    ranking it would be degenerate and would destroy the autocorrelation structure being measured.
    """
    split = split_chains(chains)
    pooled = split.reshape(-1)
    tail_sizes = []
    for quantile_level in (0.05, 0.95):
        threshold = np.quantile(pooled, quantile_level)
        indicator = (split <= threshold).astype(np.float64)
        if indicator.std() == 0:
            tail_sizes.append(float(split.size))
        else:
            tail_sizes.append(effective_sample_size_from_split_draws(indicator))
    return float(min(tail_sizes))


def monte_carlo_standard_error(chains):
    """MCSE of the posterior mean, using the joint bulk-ESS rather than any per-chain value."""
    pooled = np.concatenate([np.asarray(chain) for chain in chains])
    bulk = bulk_effective_sample_size(chains)
    if bulk <= 0:
        return float('nan')
    return float(np.std(pooled, ddof=1) / np.sqrt(bulk))


def summarise_parameter(chains):
    return {'rhat': maximum_rhat(chains),
            'rhat_rank_normalized': float(rank_normalized_rhat(chains)),
            'rhat_folded': float(folded_rank_normalized_rhat(chains)),
            'bulk_ess': bulk_effective_sample_size(chains),
            'tail_ess': tail_effective_sample_size(chains),
            'mcse': monte_carlo_standard_error(chains)}


def cross_check_against_arviz(chains, parameter_name='theta'):
    """Compare our estimators with ArviZ. Returns None when ArviZ is unavailable."""
    try:
        import arviz
    except ImportError:
        return None
    stacked = np.array([np.asarray(chain, dtype=np.float64) for chain in chains])
    dataset = arviz.convert_to_dataset({parameter_name: stacked})
    return {'ours_rhat': maximum_rhat(chains),
            'arviz_rhat': float(arviz.rhat(dataset, method='rank')[parameter_name].values),
            'ours_bulk_ess': bulk_effective_sample_size(chains),
            'arviz_bulk_ess': float(arviz.ess(dataset, method='bulk')[parameter_name].values),
            'ours_tail_ess': tail_effective_sample_size(chains),
            'arviz_tail_ess': float(arviz.ess(dataset, method='tail')[parameter_name].values)}
