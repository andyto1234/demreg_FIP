import numpy as np

def calc_chi2(mcmc_intensity, mcmc_int_error, dem0, mcmc_emis_sorted, logt_interp):
    # Calculate predicted intensities
    int_pred = np.sum(dem0[:, np.newaxis] * mcmc_emis_sorted, axis=0)
    
    # Calculate chi-square
    chi2 = np.sum(((int_pred - mcmc_intensity) / mcmc_int_error) ** 2)
    
    return chi2
