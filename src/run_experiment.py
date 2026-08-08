"""
Run all MCMC experiments, compute metrics, and save figures + results.
"""
import sys, os, glob, time, json
sys.stdout.reconfigure(encoding='utf-8')

PROJECT_ROOT_DIRECTORY = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS_DIRECTORY = os.path.join(PROJECT_ROOT_DIRECTORY, 'results')
FIGURES_DIRECTORY = os.path.join(RESULTS_DIRECTORY, 'figures')
RESULTS_FILE_PATH = os.path.join(RESULTS_DIRECTORY, 'experiment_results.json')
os.makedirs(FIGURES_DIRECTORY, exist_ok=True)


def figure_path(figure_filename):
    """Absolute path of a figure inside results/figures, so the script runs from any directory."""
    return os.path.join(FIGURES_DIRECTORY, figure_filename)


import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from scipy.special import gammaln

np.random.seed(42)
plt.rcParams.update({'figure.figsize': (12, 6), 'font.size': 12, 'axes.grid': True, 'grid.alpha': 0.3})

# ============================================================
# 1. DATA LOADING
# ============================================================
print('Loading data...')
DATA_DIR = os.path.join(PROJECT_ROOT_DIRECTORY, 'data', 'fastStorage', '2013-8')
COLUMNS = ['Timestamp', 'CPU_Cores', 'CPU_Capacity_MHz', 'CPU_Usage_MHz', 'CPU_Usage_Pct',
           'Mem_Provisioned_KB', 'Mem_Usage_KB', 'Disk_Read_KBps', 'Disk_Write_KBps',
           'Net_Recv_KBps', 'Net_Trans_KBps']
csv_files = sorted(glob.glob(os.path.join(DATA_DIR, '*.csv')))
NUM_VMS = 50
dfs = []
for f in csv_files[:NUM_VMS]:
    vm_df = pd.read_csv(f, sep=';\t', header=0, engine='python')
    vm_df.columns = COLUMNS
    vm_df['VM_ID'] = os.path.basename(f).replace('.csv', '')
    dfs.append(vm_df)
data = pd.concat(dfs, ignore_index=True)
data['Datetime'] = pd.to_datetime(data['Timestamp'], unit='s')
print('  %d rows from %d VMs' % (len(data), NUM_VMS))

# ============================================================
# 2. EDA FIGURES
# ============================================================
print('Generating EDA figures...')
fig, axes = plt.subplots(2, 3, figsize=(16, 10))
fig.suptitle('Exploratory Data Analysis', fontsize=16, fontweight='bold')
plot_cols = ['CPU_Usage_Pct', 'Mem_Usage_KB', 'Disk_Read_KBps',
             'Disk_Write_KBps', 'Net_Recv_KBps', 'Net_Trans_KBps']
plot_names = ['CPU Usage (%)', 'Memory Usage (KB)', 'Disk Read (KB/s)',
              'Disk Write (KB/s)', 'Net Received (KB/s)', 'Net Transmitted (KB/s)']
for ax, col, name in zip(axes.flat, plot_cols, plot_names):
    ax.hist(data[col].dropna(), bins=50, alpha=0.7, edgecolor='black', linewidth=0.5)
    ax.set_title(name)
    ax.set_ylabel('Frequency')
plt.tight_layout()
plt.savefig(figure_path('eda_histograms.png'), dpi=150, bbox_inches='tight')
plt.close()

sample_vm = data[data['VM_ID'] == data['VM_ID'].unique()[0]].sort_values('Datetime').reset_index(drop=True)
fig, axes = plt.subplots(3, 1, figsize=(14, 10), sharex=True)
fig.suptitle('Time Series for VM: %s' % sample_vm['VM_ID'].iloc[0], fontsize=14, fontweight='bold')
axes[0].plot(sample_vm['Datetime'], sample_vm['CPU_Usage_Pct'], linewidth=0.5, color='steelblue')
axes[0].set_ylabel('CPU Usage (%)')
axes[1].plot(sample_vm['Datetime'], sample_vm['Mem_Usage_KB'] / 1024, linewidth=0.5, color='coral')
axes[1].set_ylabel('Memory Usage (MB)')
axes[2].plot(sample_vm['Datetime'], sample_vm['Disk_Read_KBps'], linewidth=0.5, color='seagreen')
axes[2].set_ylabel('Disk Read (KB/s)')
axes[2].set_xlabel('Time')
plt.tight_layout()
plt.savefig(figure_path('eda_timeseries.png'), dpi=150, bbox_inches='tight')
plt.close()

numeric_cols = ['CPU_Usage_Pct', 'CPU_Cores', 'CPU_Capacity_MHz', 'CPU_Usage_MHz',
                'Mem_Provisioned_KB', 'Mem_Usage_KB', 'Disk_Read_KBps', 'Disk_Write_KBps',
                'Net_Recv_KBps', 'Net_Trans_KBps']
corr_matrix = data[numeric_cols].corr()
fig, ax = plt.subplots(figsize=(10, 8))
short_names = ['CPU%', 'Cores', 'CPUCap', 'CPUMHz', 'MemProv', 'MemUse', 'DskR', 'DskW', 'NetR', 'NetT']
im = ax.imshow(corr_matrix, cmap='RdBu_r', vmin=-1, vmax=1)
ax.set_xticks(range(len(numeric_cols)))
ax.set_yticks(range(len(numeric_cols)))
ax.set_xticklabels(short_names, rotation=45, ha='right')
ax.set_yticklabels(short_names)
for i in range(len(numeric_cols)):
    for j in range(len(numeric_cols)):
        ax.text(j, i, '%.2f' % corr_matrix.iloc[i, j], ha='center', va='center', fontsize=8)
fig.colorbar(im, ax=ax, shrink=0.8)
ax.set_title('Feature Correlation Matrix (Before Feature Selection)', fontsize=14, fontweight='bold')
plt.tight_layout()
plt.savefig(figure_path('eda_correlation.png'), dpi=150, bbox_inches='tight')
plt.close()

# ============================================================
# 3. FEATURE ENGINEERING
# ============================================================
print('Engineering features...')
def engineer_features(vm_data):
    df = vm_data.sort_values('Datetime').copy()
    target_col = 'CPU_Usage_Pct'
    feature_cols = ['Mem_Usage_KB', 'Disk_Read_KBps',
                    'Disk_Write_KBps', 'Net_Recv_KBps', 'Net_Trans_KBps']
    for lag in [1, 2, 3]:
        df['CPU_lag_%d' % lag] = df[target_col].shift(lag)
    df['CPU_rolling_mean'] = df[target_col].shift(1).rolling(window=6).mean()
    df['CPU_rolling_std'] = df[target_col].shift(1).rolling(window=6).std()
    df = df.dropna().reset_index(drop=True)
    predictors = feature_cols + ['CPU_lag_%d' % i for i in [1, 2, 3]] + ['CPU_rolling_mean', 'CPU_rolling_std']
    return df, predictors, target_col

all_features = []
for vm_id in data['VM_ID'].unique():
    vm_data = data[data['VM_ID'] == vm_id]
    if len(vm_data) < 50:
        continue
    feat_df, predictor_names, target_name = engineer_features(vm_data)
    all_features.append(feat_df)
features_df = pd.concat(all_features, ignore_index=True)

MAX_SAMPLES = 5000
features_df = features_df.sort_values('Datetime').reset_index(drop=True)
if len(features_df) > MAX_SAMPLES:
    features_df = features_df.iloc[:MAX_SAMPLES].reset_index(drop=True)

X_raw = features_df[predictor_names].values
y_raw = features_df[target_name].values
X_mean, X_std = X_raw.mean(axis=0), X_raw.std(axis=0)
X_std[X_std == 0] = 1.0
X_scaled = (X_raw - X_mean) / X_std
y_mean, y_std = y_raw.mean(), y_raw.std()
if y_std == 0:
    y_std = 1.0
y_scaled = (y_raw - y_mean) / y_std
X = np.column_stack([np.ones(len(X_scaled)), X_scaled])
y = y_scaled
n, p = X.shape
split = int(0.7 * n)
X_train, X_test = X[:split], X[split:]
y_train, y_test = y[:split], y[split:]
print('  Train: %d, Test: %d, Features: %d' % (len(y_train), len(y_test), p))

# ============================================================
# 4. MODEL DEFINITION
# ============================================================
TAU2 = 10.0
A0 = 2.0
B0 = 1.0

def log_likelihood(beta, sigma2, X, y):
    n = len(y)
    residuals = y - X @ beta
    return -0.5 * n * np.log(2 * np.pi * sigma2) - 0.5 * np.sum(residuals**2) / sigma2

def log_prior_beta(beta, tau2=TAU2):
    p = len(beta)
    return -0.5 * p * np.log(2 * np.pi * tau2) - 0.5 * np.sum(beta**2) / tau2

def log_prior_sigma2(sigma2, a0=A0, b0=B0):
    if sigma2 <= 0:
        return -np.inf
    return a0 * np.log(b0) - gammaln(a0) - (a0 + 1) * np.log(sigma2) - b0 / sigma2

def log_posterior(beta, sigma2, X, y):
    lp_sigma = log_prior_sigma2(sigma2)
    if np.isinf(lp_sigma):
        return -np.inf
    return log_likelihood(beta, sigma2, X, y) + log_prior_beta(beta) + lp_sigma

# ============================================================
# 5. SAMPLERS
# ============================================================
def metropolis_hastings(X, y, n_samples, burn_in, step_beta=0.01, step_sigma2=0.05):
    n, p = X.shape
    beta = np.zeros(p)
    sigma2 = 1.0
    log_sigma2 = np.log(sigma2)
    current_lp = log_posterior(beta, sigma2, X, y)
    total = n_samples + burn_in
    beta_samples = np.zeros((n_samples, p))
    sigma2_samples = np.zeros(n_samples)
    accepted = 0
    for i in range(total):
        beta_prop = beta + np.random.normal(0, step_beta, size=p)
        log_sigma2_prop = log_sigma2 + np.random.normal(0, step_sigma2)
        sigma2_prop = np.exp(log_sigma2_prop)
        proposed_lp = log_posterior(beta_prop, sigma2_prop, X, y)
        log_jacobian = log_sigma2_prop - log_sigma2
        log_alpha = proposed_lp - current_lp + log_jacobian
        if np.log(np.random.uniform()) < log_alpha:
            beta = beta_prop
            sigma2 = sigma2_prop
            log_sigma2 = log_sigma2_prop
            current_lp = proposed_lp
            if i >= burn_in:
                accepted += 1
        if i >= burn_in:
            beta_samples[i - burn_in] = beta
            sigma2_samples[i - burn_in] = sigma2
    return beta_samples, sigma2_samples, accepted / n_samples

def gibbs_sampling(X, y, n_samples, burn_in, tau2=TAU2, a0=A0, b0=B0):
    n, p = X.shape
    XtX = X.T @ X
    Xty = X.T @ y
    beta = np.zeros(p)
    sigma2 = 1.0
    total = n_samples + burn_in
    beta_samples = np.zeros((n_samples, p))
    sigma2_samples = np.zeros(n_samples)
    for i in range(total):
        precision_beta = XtX / sigma2 + np.eye(p) / tau2
        cov_beta = np.linalg.inv(precision_beta)
        mean_beta = cov_beta @ (Xty / sigma2)
        beta = np.random.multivariate_normal(mean_beta, cov_beta)
        residuals = y - X @ beta
        a_n = a0 + n / 2.0
        b_n = b0 + 0.5 * np.sum(residuals**2)
        sigma2 = 1.0 / np.random.gamma(a_n, 1.0 / b_n)
        if i >= burn_in:
            beta_samples[i - burn_in] = beta
            sigma2_samples[i - burn_in] = sigma2
    return beta_samples, sigma2_samples, 1.0

def grad_log_posterior(theta, X, y, tau2=TAU2, a0=A0, b0=B0):
    p_features = X.shape[1]
    beta = theta[:p_features]
    log_sigma2 = theta[p_features]
    sigma2 = np.exp(log_sigma2)
    n = len(y)
    residuals = y - X @ beta
    grad_beta = (X.T @ residuals) / sigma2 - beta / tau2
    grad_log_sigma2 = -0.5 * n + 0.5 * np.sum(residuals**2) / sigma2 - (a0 + 1) + b0 / sigma2 + 1
    return np.concatenate([grad_beta, [grad_log_sigma2]])

def log_posterior_hmc(theta, X, y):
    p_features = X.shape[1]
    beta = theta[:p_features]
    log_sigma2 = theta[p_features]
    sigma2 = np.exp(log_sigma2)
    lp = log_posterior(beta, sigma2, X, y)
    lp += log_sigma2
    return lp

def hmc_sampler(X, y, n_samples, burn_in, step_size=0.001, n_leapfrog=20):
    n, p_features = X.shape
    d = p_features + 1
    theta = np.zeros(d)
    current_lp = log_posterior_hmc(theta, X, y)
    total = n_samples + burn_in
    beta_samples = np.zeros((n_samples, p_features))
    sigma2_samples = np.zeros(n_samples)
    accepted = 0
    for i in range(total):
        momentum = np.random.normal(0, 1, size=d)
        theta_prop = theta.copy()
        momentum_prop = momentum.copy()
        grad = grad_log_posterior(theta_prop, X, y)
        momentum_prop += 0.5 * step_size * grad
        for _ in range(n_leapfrog):
            theta_prop += step_size * momentum_prop
            grad = grad_log_posterior(theta_prop, X, y)
            momentum_prop += step_size * grad
        momentum_prop -= 0.5 * step_size * grad
        momentum_prop = -momentum_prop
        proposed_lp = log_posterior_hmc(theta_prop, X, y)
        current_ke = 0.5 * np.sum(momentum**2)
        proposed_ke = 0.5 * np.sum(momentum_prop**2)
        log_alpha = proposed_lp - current_lp - proposed_ke + current_ke
        if np.log(np.random.uniform()) < log_alpha:
            theta = theta_prop
            current_lp = proposed_lp
            if i >= burn_in:
                accepted += 1
        if i >= burn_in:
            beta_samples[i - burn_in] = theta[:p_features]
            sigma2_samples[i - burn_in] = np.exp(theta[p_features])
    return beta_samples, sigma2_samples, accepted / n_samples

# ============================================================
# 6. DIAGNOSTICS
# ============================================================
def effective_sample_size(chain):
    n = len(chain)
    chain_centered = chain - np.mean(chain)
    variance = np.var(chain_centered)
    if variance == 0:
        return 0.0
    acf = np.correlate(chain_centered, chain_centered, mode='full')[n-1:]
    acf = acf / (variance * n)
    tau = 1.0
    for k in range(1, n // 2):
        if acf[k] < 0.05:
            break
        tau += 2.0 * acf[k]
    return n / tau

def gelman_rubin(chains):
    m = len(chains)
    n = len(chains[0])
    chain_means = np.array([np.mean(c) for c in chains])
    overall_mean = np.mean(chain_means)
    between_chain_var = n / (m - 1) * np.sum((chain_means - overall_mean)**2)
    within_chain_var = np.mean([np.var(c, ddof=1) for c in chains])
    var_hat = (1 - 1/n) * within_chain_var + (1/n) * between_chain_var
    return np.sqrt(var_hat / within_chain_var) if within_chain_var > 0 else float('inf')

def compute_predictions(beta_samples, sigma2_samples, X_test, y_test):
    n_post = len(beta_samples)
    y_pred_samples = beta_samples @ X_test.T
    y_pred_mean = np.mean(y_pred_samples, axis=0)
    rmse = np.sqrt(np.mean((y_test - y_pred_mean)**2))
    y_pred_std = np.std(y_pred_samples, axis=0)
    avg_sigma = np.sqrt(np.mean(sigma2_samples))
    total_std = np.sqrt(y_pred_std**2 + avg_sigma**2)
    lower_95 = y_pred_mean - 1.96 * total_std
    upper_95 = y_pred_mean + 1.96 * total_std
    coverage_95 = np.mean((y_test >= lower_95) & (y_test <= upper_95))
    lower_50 = y_pred_mean - 0.6745 * total_std
    upper_50 = y_pred_mean + 0.6745 * total_std
    coverage_50 = np.mean((y_test >= lower_50) & (y_test <= upper_50))
    return {
        'rmse': float(rmse), 'coverage_95': float(coverage_95), 'coverage_50': float(coverage_50),
        'y_pred_mean': y_pred_mean, 'lower_95': lower_95, 'upper_95': upper_95
    }

# ============================================================
# 7. RUN SAMPLERS
# ============================================================
N_SAMPLES = 10000
BURN_IN = 2000
N_CHAINS = 3
results = {}
method_names = ['MH', 'Gibbs', 'HMC']
bar_colors = ['steelblue', 'coral', 'seagreen']

print('Running Metropolis-Hastings (3 chains x %d samples)...' % N_SAMPLES)
mh_chains_beta, mh_chains_sigma2, mh_ars = [], [], []
start = time.time()
for chain in range(N_CHAINS):
    np.random.seed(42 + chain)
    b, s, ar = metropolis_hastings(X_train, y_train, N_SAMPLES, BURN_IN, step_beta=0.001, step_sigma2=0.05)
    mh_chains_beta.append(b)
    mh_chains_sigma2.append(s)
    mh_ars.append(ar)
    print('  Chain %d: acceptance=%.3f' % (chain + 1, ar))
mh_time = time.time() - start
results['MH'] = {'beta_chains': mh_chains_beta, 'sigma2_chains': mh_chains_sigma2,
                  'acceptance_rate': float(np.mean(mh_ars)), 'time': mh_time}

print('Running Gibbs Sampling (3 chains x %d samples)...' % N_SAMPLES)
gibbs_chains_beta, gibbs_chains_sigma2 = [], []
start = time.time()
for chain in range(N_CHAINS):
    np.random.seed(42 + chain)
    b, s, ar = gibbs_sampling(X_train, y_train, N_SAMPLES, BURN_IN)
    gibbs_chains_beta.append(b)
    gibbs_chains_sigma2.append(s)
    print('  Chain %d: acceptance=%.3f' % (chain + 1, ar))
gibbs_time = time.time() - start
results['Gibbs'] = {'beta_chains': gibbs_chains_beta, 'sigma2_chains': gibbs_chains_sigma2,
                     'acceptance_rate': 1.0, 'time': gibbs_time}

print('Running Hamiltonian Monte Carlo (3 chains x %d samples)...' % N_SAMPLES)
hmc_chains_beta, hmc_chains_sigma2, hmc_ars = [], [], []
start = time.time()
for chain in range(N_CHAINS):
    np.random.seed(42 + chain)
    b, s, ar = hmc_sampler(X_train, y_train, N_SAMPLES, BURN_IN, step_size=0.002, n_leapfrog=15)
    hmc_chains_beta.append(b)
    hmc_chains_sigma2.append(s)
    hmc_ars.append(ar)
    print('  Chain %d: acceptance=%.3f' % (chain + 1, ar))
hmc_time = time.time() - start
results['HMC'] = {'beta_chains': hmc_chains_beta, 'sigma2_chains': hmc_chains_sigma2,
                   'acceptance_rate': float(np.mean(hmc_ars)), 'time': hmc_time}

# ============================================================
# 8. COMPUTE METRICS
# ============================================================
print('Computing metrics...')
for method in method_names:
    beta_combined = np.vstack(results[method]['beta_chains'])
    sigma2_combined = np.concatenate(results[method]['sigma2_chains'])
    preds = compute_predictions(beta_combined, sigma2_combined, X_test, y_test)
    results[method]['predictions'] = preds
    ess_list = []
    for pidx in range(p):
        for c in results[method]['beta_chains']:
            ess_list.append(effective_sample_size(c[:, pidx]))
    for c in results[method]['sigma2_chains']:
        ess_list.append(effective_sample_size(c))
    results[method]['avg_ess'] = float(np.mean(ess_list))
    results[method]['ess_per_sec'] = results[method]['avg_ess'] / results[method]['time']

# Gelman-Rubin for key params
param_indices = [0, 1, min(3, p - 1)]
param_labels = ['Intercept', 'beta_1', 'beta_%d' % param_indices[2]]
rhat_results = {}
for method in method_names:
    rhat_results[method] = {}
    for pidx, plabel in zip(param_indices, param_labels):
        chains = [c[:, pidx] for c in results[method]['beta_chains']]
        rhat_results[method][plabel] = float(gelman_rubin(chains))
    rhat_results[method]['sigma2'] = float(gelman_rubin(results[method]['sigma2_chains']))

# ============================================================
# 9. GENERATE ALL FIGURES
# ============================================================
print('Generating figures...')

# Trace plots
fig, axes = plt.subplots(3, 3, figsize=(18, 12))
fig.suptitle('Trace Plots (3 chains per method)', fontsize=16, fontweight='bold')
for col_idx, method in enumerate(method_names):
    chains = results[method]['beta_chains']
    for row_idx, (pidx, plabel) in enumerate(zip(param_indices, param_labels)):
        ax = axes[row_idx, col_idx]
        for ci in range(N_CHAINS):
            ax.plot(chains[ci][:, pidx], alpha=0.6, linewidth=0.3)
        if row_idx == 0:
            ax.set_title(method, fontsize=14, fontweight='bold')
        if col_idx == 0:
            ax.set_ylabel(plabel)
        if row_idx == 2:
            ax.set_xlabel('Iteration')
plt.tight_layout()
plt.savefig(figure_path('trace_plots.png'), dpi=150, bbox_inches='tight')
plt.close()

# Sigma2 trace
fig, axes = plt.subplots(1, 3, figsize=(18, 4))
fig.suptitle('Trace Plots for sigma^2', fontsize=14, fontweight='bold')
for col_idx, method in enumerate(method_names):
    ax = axes[col_idx]
    for ci in range(N_CHAINS):
        ax.plot(results[method]['sigma2_chains'][ci], alpha=0.6, linewidth=0.3)
    ax.set_title(method)
    ax.set_xlabel('Iteration')
    if col_idx == 0:
        ax.set_ylabel('sigma^2')
plt.tight_layout()
plt.savefig(figure_path('trace_sigma2.png'), dpi=150, bbox_inches='tight')
plt.close()

# Posterior histograms
fig, axes = plt.subplots(len(param_indices) + 1, 3, figsize=(16, 14))
fig.suptitle('Posterior Distributions', fontsize=16, fontweight='bold')
for col_idx, method in enumerate(method_names):
    beta_combined = np.vstack(results[method]['beta_chains'])
    sigma2_combined = np.concatenate(results[method]['sigma2_chains'])
    for row_idx, (pidx, plabel) in enumerate(zip(param_indices, param_labels)):
        ax = axes[row_idx, col_idx]
        ax.hist(beta_combined[:, pidx], bins=60, density=True, alpha=0.7, color=bar_colors[col_idx])
        ax.axvline(np.mean(beta_combined[:, pidx]), color='red', linestyle='--', linewidth=1.5)
        if row_idx == 0:
            ax.set_title(method, fontsize=13, fontweight='bold')
        if col_idx == 0:
            ax.set_ylabel(plabel)
    ax = axes[-1, col_idx]
    ax.hist(sigma2_combined, bins=60, density=True, alpha=0.7, color=bar_colors[col_idx])
    ax.axvline(np.mean(sigma2_combined), color='red', linestyle='--', linewidth=1.5)
    ax.set_xlabel('Value')
    if col_idx == 0:
        ax.set_ylabel('sigma^2')
plt.tight_layout()
plt.savefig(figure_path('posterior_distributions.png'), dpi=150, bbox_inches='tight')
plt.close()

# Autocorrelation
fig, axes = plt.subplots(1, 3, figsize=(18, 5))
fig.suptitle('Autocorrelation of Intercept (Chain 1)', fontsize=14, fontweight='bold')
max_lag = 100
for ax, method, color in zip(axes, method_names, bar_colors):
    chain = results[method]['beta_chains'][0][:, 0]
    cc = chain - np.mean(chain)
    acf_full = np.correlate(cc, cc, mode='full')[len(cc)-1:]
    acf_full = acf_full / acf_full[0]
    ax.bar(range(max_lag), acf_full[:max_lag], color=color, alpha=0.7)
    ax.axhline(y=0.05, color='red', linestyle='--', linewidth=1)
    ax.axhline(y=-0.05, color='red', linestyle='--', linewidth=1)
    ax.set_title(method)
    ax.set_xlabel('Lag')
    if ax == axes[0]:
        ax.set_ylabel('ACF')
plt.tight_layout()
plt.savefig(figure_path('autocorrelation.png'), dpi=150, bbox_inches='tight')
plt.close()

# R-hat convergence
fig, axes = plt.subplots(1, 3, figsize=(18, 5))
fig.suptitle('Running R-hat Convergence (Intercept)', fontsize=14, fontweight='bold')
checkpoints = np.arange(200, N_SAMPLES + 1, 200)
for ax, method, color in zip(axes, method_names, bar_colors):
    rhats = []
    for cp in checkpoints:
        chains_cp = [c[:cp, 0] for c in results[method]['beta_chains']]
        rhats.append(gelman_rubin(chains_cp))
    ax.plot(checkpoints, rhats, color=color, linewidth=2)
    ax.axhline(y=1.0, color='black', linestyle='--', linewidth=1)
    ax.axhline(y=1.1, color='red', linestyle=':', linewidth=1, label='R-hat=1.1')
    ax.set_title(method)
    ax.set_xlabel('Iteration')
    if ax == axes[0]:
        ax.set_ylabel('R-hat')
    ax.legend()
    ax.set_ylim(0.95, max(1.5, max(rhats) + 0.1))
plt.tight_layout()
plt.savefig(figure_path('rhat_convergence.png'), dpi=150, bbox_inches='tight')
plt.close()

# Comparison bar charts
fig, axes = plt.subplots(2, 3, figsize=(18, 10))
fig.suptitle('Method Comparison', fontsize=16, fontweight='bold')
metrics = [
    ('Acceptance Rate', [results[m]['acceptance_rate'] for m in method_names]),
    ('Average ESS', [results[m]['avg_ess'] for m in method_names]),
    ('Runtime (s)', [results[m]['time'] for m in method_names]),
    ('ESS / second', [results[m]['ess_per_sec'] for m in method_names]),
    ('RMSE', [results[m]['predictions']['rmse'] for m in method_names]),
    ('95% Coverage', [results[m]['predictions']['coverage_95'] for m in method_names]),
]
for ax, (metric_name, values) in zip(axes.flat, metrics):
    bars = ax.bar(method_names, values, color=bar_colors, edgecolor='black', linewidth=0.5)
    ax.set_title(metric_name, fontsize=13)
    ax.set_ylabel(metric_name)
    for bar, val in zip(bars, values):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height(),
                '%.3f' % val, ha='center', va='bottom', fontsize=10)
plt.tight_layout()
plt.savefig(figure_path('comparison_bars.png'), dpi=150, bbox_inches='tight')
plt.close()

# Predictions vs actual
fig, axes = plt.subplots(3, 1, figsize=(14, 12))
fig.suptitle('Predictions vs Actual (Test Set)', fontsize=16, fontweight='bold')
plot_n = min(200, len(y_test))
for ax, method, color in zip(axes, method_names, bar_colors):
    preds = results[method]['predictions']
    ax.plot(range(plot_n), y_test[:plot_n], 'k-', linewidth=1, label='Actual', alpha=0.8)
    ax.plot(range(plot_n), preds['y_pred_mean'][:plot_n], color=color, linewidth=1, label='Predicted', alpha=0.8)
    ax.fill_between(range(plot_n), preds['lower_95'][:plot_n], preds['upper_95'][:plot_n],
                    color=color, alpha=0.15, label='95% CI')
    ax.set_title('%s (RMSE=%.4f)' % (method, preds['rmse']), fontsize=13)
    ax.legend(loc='upper right')
    ax.set_ylabel('CPU Usage (scaled)')
axes[-1].set_xlabel('Test Sample Index')
plt.tight_layout()
plt.savefig(figure_path('predictions_vs_actual.png'), dpi=150, bbox_inches='tight')
plt.close()

# ============================================================
# 10. HYPERPARAMETER SENSITIVITY
# ============================================================
print('Running sensitivity analysis...')
mh_sensitivity = []
for step in [0.001, 0.005, 0.01, 0.02, 0.05]:
    np.random.seed(42)
    _, _, ar = metropolis_hastings(X_train, y_train, 3000, 500, step_beta=step, step_sigma2=0.05)
    mh_sensitivity.append({'step': step, 'acceptance': float(ar)})
    print('  MH step=%.3f -> acceptance=%.3f' % (step, ar))

hmc_sensitivity = []
for lf in [5, 10, 15, 20, 25]:
    np.random.seed(42)
    _, _, ar = hmc_sampler(X_train, y_train, 3000, 500, step_size=0.002, n_leapfrog=lf)
    hmc_sensitivity.append({'leapfrog': lf, 'acceptance': float(ar)})
    print('  HMC L=%d -> acceptance=%.3f' % (lf, ar))

fig, axes = plt.subplots(1, 2, figsize=(14, 5))
fig.suptitle('Hyperparameter Sensitivity', fontsize=14, fontweight='bold')
axes[0].plot([s['step'] for s in mh_sensitivity], [s['acceptance'] for s in mh_sensitivity],
             'o-', color='steelblue', linewidth=2, markersize=8)
axes[0].axhline(y=0.234, color='red', linestyle='--', linewidth=1, label='Optimal (~23.4%)')
axes[0].set_xlabel('Step Size (step_beta)')
axes[0].set_ylabel('Acceptance Rate')
axes[0].set_title('MH: Step Size vs Acceptance')
axes[0].legend()
axes[1].plot([s['leapfrog'] for s in hmc_sensitivity], [s['acceptance'] for s in hmc_sensitivity],
             'o-', color='seagreen', linewidth=2, markersize=8)
axes[1].axhline(y=0.65, color='red', linestyle='--', linewidth=1, label='Target range (65-80%)')
axes[1].axhline(y=0.80, color='red', linestyle='--', linewidth=1)
axes[1].set_xlabel('Leapfrog Steps (L)')
axes[1].set_ylabel('Acceptance Rate')
axes[1].set_title('HMC: Leapfrog Steps vs Acceptance')
axes[1].legend()
plt.tight_layout()
plt.savefig(figure_path('sensitivity.png'), dpi=150, bbox_inches='tight')
plt.close()

# ============================================================
# 11. OLS BASELINE
# ============================================================
print('Computing OLS baseline...')
beta_ols = np.linalg.lstsq(X_train, y_train, rcond=None)[0]
y_pred_ols = X_test @ beta_ols
ols_rmse = np.sqrt(np.mean((y_test - y_pred_ols) ** 2))
print('  OLS RMSE: %.4f' % ols_rmse)

# ============================================================
# 12. SAVE RESULTS JSON
# ============================================================
output = {
    'n_samples': N_SAMPLES, 'burn_in': BURN_IN, 'n_chains': N_CHAINS,
    'n_train': len(y_train), 'n_test': len(y_test), 'n_features': p,
}
for method in method_names:
    output[method] = {
        'acceptance_rate': results[method]['acceptance_rate'],
        'avg_ess': results[method]['avg_ess'],
        'time': results[method]['time'],
        'ess_per_sec': results[method]['ess_per_sec'],
        'rmse': results[method]['predictions']['rmse'],
        'coverage_95': results[method]['predictions']['coverage_95'],
        'coverage_50': results[method]['predictions']['coverage_50'],
    }
output['ols_rmse'] = ols_rmse
output['rhat'] = rhat_results
output['mh_sensitivity'] = mh_sensitivity
output['hmc_sensitivity'] = hmc_sensitivity

with open(RESULTS_FILE_PATH, 'w') as f:
    json.dump(output, f, indent=2)

print('\nAll done! Results saved to results/experiment_results.json')
print('Figures written to results/figures/:')
print('         eda_histograms.png, eda_timeseries.png, eda_correlation.png,')
print('         trace_plots.png, trace_sigma2.png, posterior_distributions.png,')
print('         autocorrelation.png, rhat_convergence.png, comparison_bars.png,')
print('         predictions_vs_actual.png, sensitivity.png')
