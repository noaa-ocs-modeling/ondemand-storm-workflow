from argparse import ArgumentParser
from pathlib import Path
import pickle

import chaospy
import dask
from matplotlib import pyplot
import numpy
from sklearn.linear_model import LassoCV, ElasticNetCV, LinearRegression
from sklearn.model_selection import ShuffleSplit, LeaveOneOut
import xarray

from ensembleperturbation.parsing.adcirc import subset_dataset
from ensembleperturbation.perturbation.atcf import VortexPerturbedVariable
from ensembleperturbation.plotting.perturbation import plot_perturbations
from ensembleperturbation.plotting.surrogate import (
    plot_kl_surrogate_fit,
    plot_selected_percentiles,
    plot_selected_validations,
    plot_sensitivities,
    plot_validations,
)
from ensembleperturbation.uncertainty_quantification.karhunen_loeve_expansion import (
    karhunen_loeve_expansion,
    karhunen_loeve_prediction,
)
from ensembleperturbation.uncertainty_quantification.surrogate import (
    percentiles_from_surrogate,
    sensitivities_from_surrogate,
    surrogate_from_karhunen_loeve,
    surrogate_from_training_set,
    validations_from_surrogate,
)
from ensembleperturbation.utilities import get_logger

EFS_MOUNT_POINT = Path('~').expanduser() / 'app/io'
LOGGER = get_logger('klpc_wetonly')



def main(args):

    tracks_dir = EFS_MOUNT_POINT / args.tracks_dir
    ensemble_dir = EFS_MOUNT_POINT / args.ensemble_dir

    analyze(tracks_dir, ensemble_dir/'analyze')



def analyze(tracks_dir, analyze_dir):
    # KL parameters
    variance_explained = 0.9999
    # subsetting parameters
    isotach = 34  # -kt wind swath of the cyclone
    depth_bounds = 25.0
    point_spacing = None
    node_status_mask = 'always_wet'
    # analysis type
    variable_name = 'zeta_max'
    use_depth = True  # for depths
    # use_depth = False   # for elevations
    log_space = False  # normal linear space
    # log_space = True  # use log-scale to force surrogate to positive values only
    training_runs = 'korobov'
    validation_runs = 'random'
    # PC parameters
    polynomial_order = 3
    # cross_validator = ShuffleSplit(n_splits=10, test_size=12, random_state=666)
    # cross_validator = ShuffleSplit(random_state=666)
    cross_validator = LeaveOneOut()
    # regression_model = LassoCV(
    #    fit_intercept=False, cv=cross_validator, selection='random', random_state=666
    # )
    regression_model = ElasticNetCV(
        fit_intercept=False,
        cv=cross_validator,
        l1_ratio=0.5,
        selection='random',
        random_state=666,
    )
    # regression_model = LinearRegression(fit_intercept=False)
    regression_name = 'ElasticNet_LOO'
    if training_runs == 'quadrature':
        use_quadrature = True
    else:
        use_quadrature = False

    make_perturbations_plot = True
    make_klprediction_plot = True
    make_klsurrogate_plot = True
    make_sensitivities_plot = True
    make_validation_plot = True
    make_percentile_plot = True

    save_plots = True

    storm_name = None

    if log_space:
        output_directory = analyze_dir / f'outputs_log_{regression_name}'
    else:
        output_directory = analyze_dir / f'outputs_linear_{regression_name}'
    if not output_directory.exists():
        output_directory.mkdir(parents=True, exist_ok=True)

    subset_filename = output_directory / 'subset.nc'
    kl_filename = output_directory / 'karhunen_loeve.pkl'
    kl_surrogate_filename = output_directory / 'kl_surrogate.npy'
    surrogate_filename = output_directory / 'surrogate.npy'
    kl_validation_filename = output_directory / 'kl_surrogate_fit.nc'
    sensitivities_filename = output_directory / 'sensitivities.nc'
    validation_filename = output_directory / 'validation.nc'
    percentile_filename = output_directory / 'percentiles.nc'

    filenames = ['perturbations.nc', 'maxele.63.nc']
    if storm_name is None:
        storm_name = tracks_dir / 'original.22'

    datasets = {}
    existing_filenames = []
    for filename in filenames:
        filename = analyze_dir / filename
        if filename.exists():
            datasets[filename.name] = xarray.open_dataset(filename, chunks='auto')
        else:
            raise FileNotFoundError(filename.name)

    perturbations = datasets[filenames[0]]
    max_elevations = datasets[filenames[1]]
    min_depth = 0.8 * max_elevations.h0  # the minimum allowable depth

    perturbations = perturbations.assign_coords(
        type=(
            'run',
            (
                numpy.where(
                    perturbations['run'].str.contains(training_runs),
                    'training',
                    numpy.where(
                        perturbations['run'].str.contains(validation_runs),
                        'validation',
                        'none',
                    ),
                )
            ),
        )
    )

    if len(numpy.unique(perturbations['type'][:])) == 1:
        perturbations['type'][:] = numpy.random.choice(
            ['training', 'validation'], size=len(perturbations.run), p=[0.7, 0.3]
        )
        LOGGER.info('dividing 70/30% for training/testing the model')

    training_perturbations = perturbations.sel(run=perturbations['type'] == 'training')
    validation_perturbations = perturbations.sel(run=perturbations['type'] == 'validation')

    if make_perturbations_plot:
        plot_perturbations(
            training_perturbations=training_perturbations,
            validation_perturbations=validation_perturbations,
            runs=perturbations['run'].values,
            perturbation_types=perturbations['type'].values,
            track_directory=tracks_dir,
            output_directory=output_directory if save_plots else None,
        )

    variables = {
        variable_class.name: variable_class()
        for variable_class in VortexPerturbedVariable.__subclasses__()
    }

    distribution = chaospy.J(
        *(
            variables[variable_name].chaospy_distribution()
            for variable_name in perturbations['variable'].values
        )
    )

    # sample based on subset and excluding points that are never wet during training run
    if not subset_filename.exists():
        LOGGER.info('subsetting nodes')
        subset = subset_dataset(
            ds=max_elevations,
            variable=variable_name,
            maximum_depth=depth_bounds,
            wind_swath=[storm_name, isotach],
            node_status_selection={
                'mask': node_status_mask,
                'runs': training_perturbations['run'],
            },
            point_spacing=point_spacing,
            output_filename=subset_filename,
        )

    # subset chunking can be disturbed by point_spacing so load from saved filename always
    LOGGER.info(f'loading subset from "{subset_filename}"')
    subset = xarray.open_dataset(subset_filename)
    if 'element' in subset:
        elements = subset['element']
    subset = subset[variable_name]

    # divide subset into training/validation runs
    with dask.config.set(**{'array.slicing.split_large_chunks': True}):
        training_set = subset.sel(run=training_perturbations['run'])
        validation_set = subset.sel(run=validation_perturbations['run'])

    LOGGER.info(f'total {training_set.shape} training samples')
    LOGGER.info(f'total {validation_set.shape} validation samples')

    training_set_adjusted = training_set.copy(deep=True)

    if use_depth:
        training_set_adjusted += training_set_adjusted['depth']  # + adjusted_min_depth

    if log_space:
        training_set_adjusted = numpy.log(training_set_adjusted)

    # Evaluating the Karhunen-Loeve expansion
    nens, ngrid = training_set.shape
    if not kl_filename.exists():
        LOGGER.info(
            f'Evaluating Karhunen-Loeve expansion from {ngrid} grid nodes and {nens} ensemble members'
        )
        kl_expansion = karhunen_loeve_expansion(
            training_set_adjusted.values,
            neig=variance_explained,
            method='PCA',
            output_directory=output_directory,
        )
    else:
        LOGGER.info(f'loading Karhunen-Loeve expansion from "{kl_filename}"')
        with open(kl_filename, 'rb') as kl_handle:
            kl_expansion = pickle.load(kl_handle)

    LOGGER.info(f'found {kl_expansion["neig"]} Karhunen-Loeve modes')
    LOGGER.info(f'Karhunen-Loeve expansion: {list(kl_expansion)}')

    # plot prediction versus actual simulated
    if make_klprediction_plot:
        kl_predicted = karhunen_loeve_prediction(
            kl_dict=kl_expansion,
            actual_values=training_set_adjusted,
            ensembles_to_plot=[0, int(nens / 2), nens - 1],
            element_table=elements if point_spacing is None else None,
            plot_directory=output_directory,
        )

    # evaluate the surrogate for each KL sample
    kl_training_set = xarray.DataArray(data=kl_expansion['samples'], dims=['run', 'mode'])
    kl_surrogate_model = surrogate_from_training_set(
        training_set=kl_training_set,
        training_perturbations=training_perturbations,
        distribution=distribution,
        filename=kl_surrogate_filename,
        use_quadrature=use_quadrature,
        polynomial_order=polynomial_order,
        regression_model=regression_model,
    )

    # plot kl surrogate model versus training set
    if make_klsurrogate_plot:
        kl_fit = validations_from_surrogate(
            surrogate_model=kl_surrogate_model,
            training_set=kl_training_set,
            training_perturbations=training_perturbations,
            filename=kl_validation_filename,
        )

        plot_kl_surrogate_fit(
            kl_fit=kl_fit,
            output_filename=output_directory / 'kl_surrogate_fit.png' if save_plots else None,
        )

    # convert the KL surrogate model to the overall surrogate at each node
    surrogate_model = surrogate_from_karhunen_loeve(
        mean_vector=kl_expansion['mean_vector'],
        eigenvalues=kl_expansion['eigenvalues'],
        modes=kl_expansion['modes'],
        kl_surrogate_model=kl_surrogate_model,
        filename=surrogate_filename,
    )

    if make_sensitivities_plot:
        sensitivities = sensitivities_from_surrogate(
            surrogate_model=surrogate_model,
            distribution=distribution,
            variables=perturbations['variable'],
            nodes=subset,
            element_table=elements if point_spacing is None else None,
            filename=sensitivities_filename,
        )
        plot_sensitivities(
            sensitivities=sensitivities,
            storm=storm_name,
            output_filename=output_directory / 'sensitivities.png' if save_plots else None,
        )

    if make_validation_plot:
        node_validation = validations_from_surrogate(
            surrogate_model=surrogate_model,
            training_set=training_set,
            training_perturbations=training_perturbations,
            validation_set=validation_set,
            validation_perturbations=validation_perturbations,
            convert_from_log_scale=log_space,
            convert_from_depths=use_depth,
            minimum_allowable_value=min_depth if use_depth else None,
            element_table=elements if point_spacing is None else None,
            filename=validation_filename,
        )

        plot_validations(
            validation=node_validation,
            output_directory=output_directory if save_plots else None,
        )

        plot_selected_validations(
            validation=node_validation,
            run_list=validation_set['run'][
                numpy.linspace(0, validation_set.shape[0], 6, endpoint=False).astype(int)
            ].values,
            output_directory=output_directory if save_plots else None,
        )

    if make_percentile_plot:
        percentiles = [10, 50, 90]
        node_percentiles = percentiles_from_surrogate(
            surrogate_model=surrogate_model,
            distribution=distribution,
            training_set=validation_set,
            percentiles=percentiles,
            convert_from_log_scale=log_space,
            convert_from_depths=use_depth,
            minimum_allowable_value=min_depth if use_depth else None,
            element_table=elements if point_spacing is None else None,
            filename=percentile_filename,
        )

        plot_selected_percentiles(
            node_percentiles=node_percentiles,
            perc_list=percentiles,
            output_directory=output_directory if save_plots else None,
        )


if __name__ == '__main__':

    parser = ArgumentParser()
    parser.add_argument('-d', '--ensemble-dir')
    parser.add_argument('-t', '--tracks-dir')
    parser.add_argument('-s', '--sequential', action='store_true')

    main(parser.parse_args())
