from multiprocessing import Pool
from functools import partial
from time import sleep
from tqdm import tqdm
import numpy as np
import astropy.units as u
from ashmcmc import ashmcmc, interp_emis_temp
import argparse
import platform
from demcmc import (
    EmissionLine,
    TempBins,
    load_cont_funcs,
    plot_emission_loci,
    predict_dem_emcee,
    ContFuncDiscrete,
)
from demcmc.units import u_temp, u_dem
from mcmc.mcmc_utils import calc_chi2, mcmc_process
from demregpy import dn2dem
import demregpy


def check_dem_exists(filename: str) -> bool:
    # Check if the DEM file exists
    from os.path import exists
    return exists(filename)    

def process_pixel(args: tuple[int, np.ndarray, np.ndarray, list[str], np.ndarray, ashmcmc]) -> None:
    from pathlib import Path
    # Process a single pixel with the given arguments
    xpix, Intensity, Int_error, Lines, ldens, a = args
    output_file = f'{a.outdir}/dem_columns/dem_{xpix}.npz'
    # Extract the directory path from the output_file
    output_dir = Path(output_file).parent

    # Check if the directory exists, and create it if it doesn't
    output_dir.mkdir(parents=True, exist_ok=True)

    ycoords_out = []
    dem_results = []
    chi2_results = []
    linenames_list = []

    if not check_dem_exists(output_file):
        for ypix in tqdm(range(Intensity.shape[0])):

            logt, emis, linenames = a.read_emissivity(ldens[ypix, xpix])
            logt_interp = interp_emis_temp(logt.value)
            temp_bins = TempBins(logt_interp * u.K)
            # loc = np.where((np.log10(logt_interp) >= 4) & (np.log10(logt_interp) <= 8))
            emis_sorted = a.emis_filter(emis, linenames, Lines)
            mcmc_lines = []

            mcmc_intensity = []
            mcmc_int_error = []
            mcmc_emis_sorted = []
            dlogt = logt_interp[1]-logt_interp[0]
            temps=10**np.arange(np.min(logt_interp),np.max(logt_interp)+dlogt,dlogt)


            for ind, line in enumerate(Lines):
                if (line[:2] == 'fe') and (Intensity[ypix, xpix, ind] > 10):
                    mcmc_intensity.append(Intensity[ypix, xpix, ind])
                    mcmc_int_error.append(Int_error[ypix, xpix,ind]+Intensity[ypix, xpix,ind]*0.23)
                    mcmc_emis_sorted.append(emis_sorted[ind, :])
                    mcmc_lines.append(line)


            # doing DEM calculation
            dem0,edem0,elogt0,chisq0,dn_reg0=dn2dem(mcmc_intensity,mcmc_int_error,mcmc_emis_sorted,logt_interp,temps)

            chi2 = calc_chi2(mcmc_intensity, mcmc_int_error, dem0, mcmc_emis_sorted, logt_interp)
            dem_results.append(dem0)
            chi2_results.append(chi2)
            ycoords_out.append(ypix)
            linenames_list.append(mcmc_lines)

        dem_results = np.array(dem_results)
        chi2_results = np.array(chi2_results)
        linenames_list = np.array(linenames_list, dtype=object)

        np.savez(output_file, dem_results=dem_results, chi2=chi2_results, ycoords_out=ycoords_out, lines_used=linenames_list, logt = np.array(logt_interp))

def download_data(filename: str) -> None:
    from eispac.download import download_hdf5_data
    download_hdf5_data(filename.split('/')[-1], local_top='SO_EIS_data', overwrite=False)

def combine_dem_files(xdim:int, ydim:int, dir: str) -> np.array:
    from glob import glob
    from re import search

    dem_files = sorted(glob(f'{dir}/dem_columns/dem*.npz'))
    # print(dem_files)
    ref = np.load(dem_files[0])['dem_results'].shape
    logt = np.load(dem_files[0])['logt']
    dem_combined = np.zeros((ydim,xdim,ref[1]))
    chi2_combined = np.zeros((ydim,xdim))
    lines_used = np.zeros((ydim,xdim))

    for dem_file in tqdm(dem_files):
        # print(dem_file)
        xpix_loc = search(r'dem_(\d+)\.npz$', dem_file).group(1)
        # print(xpix_loc)
        dem_combined[:,int(xpix_loc), :] = np.load(dem_file)['dem_results'] 
        chi2_combined[:,int(xpix_loc)] = np.load(dem_file)['chi2'] 
        lines_used[:,int(xpix_loc)] = np.array([len(line) for line in np.load(dem_file, allow_pickle=True)['lines_used']])
    return dem_combined, chi2_combined, lines_used, logt

def process_data(filename: str, num_processes: int) -> None:
    # Create an ashmcmc object with the specified filename
    download_data(filename)
    a = ashmcmc(filename)

    # Retrieve necessary data from ashmcmc object
    Lines, Intensity, Int_error = a.fit_data(plot=False)
    ldens = a.read_density()

    # Generate a list of arguments for process_pixel function
    args_list = [(xpix, Intensity, Int_error, Lines, ldens, a) for xpix in range(Intensity.shape[1])]

    # Create a Pool of processes for parallel execution
    with Pool(processes=num_processes) as pool:
        results = list(tqdm(pool.imap(process_pixel, args_list), total=len(args_list), desc="Processing Pixels"))

    # Combine the DEM files into a single array
    print('------------------------------Combining DEM files------------------------------')
    dem_combined, chi2_combined, lines_used, logt = combine_dem_files(Intensity.shape[1], Intensity.shape[0], a.outdir)
    np.savez(f'{a.outdir}/{a.outdir.split("/")[-1]}_dem_combined.npz', dem_combined=dem_combined, chi2_combined=chi2_combined, lines_used=lines_used, logt=logt)
    
    return f'{a.outdir}/{a.outdir.split("/")[-1]}_dem_combined.npz'

def pred_intensity_compact(emis: np.array, logt: np.array, dem: np.array) -> float:
    """
    Calculate the predicted intensity for a given emissivity, temperature, and DEM.
    
    Parameters:
    emis (np.array): Emissivity array
    logt (np.array): Log temperature array
    dem (np.array): Differential Emission Measure array
    
    Returns:
    float: Predicted intensity
    """
    # Ensure all inputs are numpy arrays
    emis = np.array(emis)
    logt = np.array(logt)
    dem = np.array(dem)
    
    # Calculate the temperature array
    temp = 10**logt
    
    # Calculate the integrand
    integrand = emis * dem
    
    # Perform the integration using the trapezoidal rule
    intensity = np.trapz(integrand, temp)
    
    return intensity
    
def correct_metadata(map, ratio_name):
    # Correct the metadata of the map
    map.meta['measrmnt'] = 'FIP Bias'
    map.meta.pop('bunit', None)
    map.meta['line_id'] = ratio_name
    return map

def calc_composition_parallel(args):
    ypix, xpix, ldens, dem_median, intensities, line_databases, comp_ratio, a = args
    logt, emis, linenames = a.read_emissivity(ldens[ypix, xpix])
    logt_interp = interp_emis_temp(logt.value)
    emis_sorted = a.emis_filter(emis, linenames, line_databases[comp_ratio][:2])
    int_lf = pred_intensity_compact(emis_sorted[0], logt_interp, dem_median[ypix, xpix])
    dem_scaled = dem_median[ypix, xpix] * (intensities[ypix, xpix, 0] / int_lf)
    int_hf = pred_intensity_compact(emis_sorted[1], logt_interp, dem_scaled)
    fip_ratio = int_hf / intensities[ypix, xpix, 1]
    return ypix, xpix, fip_ratio

def calc_composition(filename, np_file, line_databases, num_processes):
    from sunpy.map import Map
    from multiprocessing import Pool

    a = ashmcmc(filename)
    ldens = a.read_density()
    dem_data = np.load(np_file)
    dem_median = dem_data['dem_combined']

    for comp_ratio in line_databases:
        intensities = np.zeros((ldens.shape[0], ldens.shape[1], 2))
        composition = np.zeros_like(ldens)

        # Read the intensity maps for the composition lines
        for num, fip_line in enumerate(line_databases[comp_ratio][:2]):
            print('getting intensity \n')
            map = a.ash.get_intensity(fip_line, outdir=a.outdir, plot=False, calib=True)
            intensities[:, :, num] = map.data

        # Create argument list for parallel processing
        args_list = [(ypix, xpix, ldens, dem_median, intensities, line_databases, comp_ratio, a)
                     for ypix, xpix in np.ndindex(ldens.shape)]

        # Create a pool of worker processes
        with Pool(processes=num_processes) as pool:
            results = pool.map(calc_composition_parallel, args_list)

        # Update composition array with the results
        for ypix, xpix, fip_ratio in results:
            composition[ypix, xpix] = fip_ratio

        np.savez(f'{a.outdir}/{a.outdir.split("/")[-1]}_composition_{comp_ratio}.npz',
                 composition=composition, chi2=dem_data['chi2_combined'], no_lines=dem_data['lines_used'])

        map_fip = Map(composition, map.meta)
        map_fip = correct_metadata(map_fip, comp_ratio)
        map_fip.save(f'{a.outdir}/{a.outdir.split("/")[-1]}_{comp_ratio}.fits', overwrite=True)
# def calc_composition(filename, np_file, line_database):
#     # I am tired and am probably very dumb in calculating this
#     from sunpy.map import Map
#     a = ashmcmc(filename)

#     ldens = a.read_density()
#     dem_data = np.load(np_file)
#     dem_median = dem_data['dem_combined']

#     # Retrieve necessary data from ashmcmc object
#     for comp_ratio in line_databases:
#         intensities = np.zeros((ldens.shape[0], ldens.shape[1], 2))
#         composition = np.zeros_like(ldens)  # Initialize composition array

#         for num, fip_line in enumerate(line_databases[comp_ratio][:2]):  # Iterate only over the first 2 lines
#             map = a.ash.get_intensity(fip_line, outdir=a.outdir, plot=False)
#             intensities[:, :, num] = map.data

#         print(f'------------------------------Calculating {line_databases[comp_ratio][2]} FIP Bias------------------------------')
#         for ypix, xpix in tqdm(np.ndindex(ldens.shape)):  # Iterate over each pixel
#             logt, emis, linenames = a.read_emissivity(ldens[ypix, xpix]) # Read emissivity from .sav files
#             logt_interp = interp_emis_temp(logt.value) # Interpolate the temperature
#             temp_bins = TempBins(logt_interp * u.K) # Create temp_bin structure for intensity prediction
#             emis_sorted = a.emis_filter(emis, linenames, line_databases[comp_ratio][:2]) # Filter emissivity based on specified lines
#             dem_pixel = dem_median[ypix, xpix,:] # Extract DEM for the pixel
#             int_lf = pred_intensity_compact(emis_sorted[0], logt_interp, line_databases[comp_ratio][0], dem_pixel)
#             dem_scaled = dem_pixel * (intensities[ypix, xpix, 0] / int_lf)
#             int_hf = pred_intensity_compact(emis_sorted[1], logt_interp, line_databases[comp_ratio][1], dem_scaled)
#             fip_ratio = int_hf/intensities[ypix, xpix, 1]
#             composition[ypix, xpix] = fip_ratio  # Update composition matrix

#         np.savez(f'{a.outdir}/{a.outdir.split("/")[-1]}_composition_{comp_ratio}.npz', composition=composition, chi2 =  dem_data['chi2_combined'], no_lines = dem_data['lines_used'])

#         # Create SunPy Map with appropriate metadata
#         map_fip = Map(composition, map.meta)
#         map_fip = correct_metadata(map_fip, comp_ratio)
#         map_fip.save(f'{a.outdir}/{a.outdir.split("/")[-1]}_{comp_ratio}.fits')

# def calc_composition(filename, np_file, line_database):
#     from sunpy.map import Map
#     from multiprocessing import Pool
#     import platform

#     a = ashmcmc(filename)

#     ldens = a.read_density()
#     dem_data = np.load(np_file)
#     dem_median = dem_data['dem_combined']

#     # Retrieve necessary data from ashmcmc object
#     for comp_ratio in line_databases:
#         intensities = np.zeros((ldens.shape[0], ldens.shape[1], 2))
#         composition = np.zeros_like(ldens)  # Initialize composition array

#         for num, fip_line in enumerate(line_databases[comp_ratio][:2]):  # Iterate only over the first 2 lines
#             map = a.ash.get_intensity(fip_line, outdir=a.outdir, plot=False)
#             intensities[:, :, num] = map.data

#         def calc_composition_pixel(ypix, xpix):
#             logt, emis, linenames = a.read_emissivity(ldens[ypix, xpix]) # Read emissivity from .sav files
#             logt_interp = interp_emis_temp(logt.value) # Interpolate the temperature
#             temp_bins = TempBins(logt_interp * u.K) # Create temp_bin structure for intensity prediction
#             emis_sorted = a.emis_filter(emis, linenames, line_databases[comp_ratio][:2]) # Filter emissivity based on specified lines

#             int_lf = pred_intensity_compact(emis_sorted[0], logt_interp, line_databases[comp_ratio][0], dem_median[ypix, xpix])
#             dem_scaled = dem_median[ypix, xpix] * (intensities[ypix, xpix, 0] / int_lf)
#             int_hf = pred_intensity_compact(emis_sorted[1], logt_interp, line_databases[comp_ratio][1], dem_scaled)
#             fip_ratio = int_hf/intensities[ypix, xpix, 1]
#             return fip_ratio

#         # Determine the operating system type (Linux or macOS)
#         # Set the number of processes based on the operating system
#         if platform.system() == "Linux": process_num = 60 # above 64 seems to break the MSSL machine
#         elif platform.system() == "Darwin": process_num = 10
#         else: process_num = 10 

#         with Pool(processes=process_num) as pool:
#             results = list(tqdm(pool.starmap(calc_composition_pixel, [(ypix, xpix) for ypix, xpix in np.ndindex(ldens.shape)]), total=ldens.size, desc="Processing Pixels"))
#             composition = np.array(results).reshape(ldens.shape)

#         np.savez(f'{a.outdir}/{a.outdir}_composition_{comp_ratio}.npz', composition=composition, chi2 =  dem_data['chi2_combined'], no_lines = dem_data['lines_used'])

#         # Create SunPy Map with appropriate metadata
#         map_fip = Map(composition, map.meta)
#         map_fip = correct_metadata(map_fip, comp_ratio)
#         map_fip.save(f'{a.outdir}/{a.outdir}_{comp_ratio}.fits')

import os

def update_filenames_txt(old_filename, new_filename):
    with open("config.txt", "r") as file:
        lines = file.readlines()

    with open("config.txt", "w") as file:
        for line in lines:
            if line.strip() == old_filename:
                file.write(new_filename + "\n")
            else:
                file.write(line)

if __name__ == "__main__":
    # Determine the operating system type (Linux or macOS)
    # Set the default number of cores based on the operating system
    if platform.system() == "Linux":
        default_cores = 60  # above 64 seems to break the MSSL machine - probably due to no. cores = 64?
    elif platform.system() == "Darwin":
        default_cores = 10
    else:
        default_cores = 10

    # Create an argument parser
    parser = argparse.ArgumentParser(description='Process data using multiprocessing.')
    parser.add_argument('-c', '--cores', type=int, default=default_cores,
                        help='Number of cores to use (default: {})'.format(default_cores))
    args = parser.parse_args()

    # Read filenames from a text file
    with open("config.txt", "r") as file:
        filenames = [line.strip() for line in file]

    for file_num, filename_full in enumerate(filenames):
        filename = filename_full.replace(" [processing]", '')
        # Check if the file has already been processed

        # Re-read the config.txt file to get the latest information
        with open("config.txt", "r") as file:
            current_filenames = [line.strip() for line in file]

        filename_full = current_filenames[file_num]
        if not filename_full.endswith("[processed]") and not filename_full.endswith("[processing]"):
            # try:
            # Add "[processing]" to the end of the filename in filenames.txt
            processing_filename = filename + " [processing]"
            update_filenames_txt(filename_full, processing_filename)
            print(f"Processing: {filename}")
            np_file = process_data(filename, args.cores)
            print(f"Processed: {filename}")
            line_databases = {
                "sis": ['si_10_258.37', 's_10_264.23', 'SiX_SX'],
                # "CaAr": ['ca_14_193.87', 'ar_14_194.40', 'CaXIV_ArXIV'],
            }
            calc_composition(filename, np_file, line_databases, args.cores)

            # Change "[processing]" to "[processed]" in filenames.txt after processing is finished
            processed_filename = filename + " [processed]"
            update_filenames_txt(processing_filename, processed_filename)

            # except Exception as e:
            #     print(f"Failed: {e}")


# how to call
# need to create a config.txt file with the filenames of the data files
# e.g.
# SO_EIS_data/eis_20230327_061218.data.h5
# Then run the following command in terminal:
# python mcmc_para.py –-cores 50
#
# Custom setting locations:
# ashmcmc.py - read_emissivity - abund_file = 'emissivities_sun_photospheric_2015_scott'
# ashmcmc.py - find_matching_file - change abundance file directory
# asheis.py - class asheis (line 71) - input the directory where the IDL density interpolation files are stored
# asheis.py - line 137 - 2023 calibration hard coded
