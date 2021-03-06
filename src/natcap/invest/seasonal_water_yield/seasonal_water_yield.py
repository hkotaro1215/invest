"""InVEST Seasonal Water Yield Model."""
from __future__ import absolute_import

import os
import logging
import re
import fractions
import uuid
import warnings

import scipy.special
import numpy
from osgeo import gdal
from osgeo import ogr
import natcap.invest.pygeoprocessing_0_3_3.routing
import natcap.invest.pygeoprocessing_0_3_3.routing.routing_core
import pygeoprocessing

from .. import utils
from .. import validation

import seasonal_water_yield_core  #pylint: disable=import-error

LOGGER = logging.getLogger(
    'natcap.invest.seasonal_water_yield.seasonal_water_yield')

TARGET_NODATA = -1
N_MONTHS = 12
MONTH_ID_TO_LABEL = [
    'jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct',
    'nov', 'dec']

_OUTPUT_BASE_FILES = {
    'aggregate_vector_path': 'aggregated_results.shp',
    'annual_precip_path': 'P.tif',
    'cn_path': 'CN.tif',
    'l_avail_path': 'L_avail.tif',
    'l_path': 'L.tif',
    'l_sum_path': 'L_sum.tif',
    'l_sum_avail_path': 'L_sum_avail.tif',
    'qf_path': 'QF.tif',
    'b_sum_path': 'B_sum.tif',
    'b_path': 'B.tif',
    'vri_path': 'Vri.tif',
    }

_INTERMEDIATE_BASE_FILES = {
    'aet_path': 'aet.tif',
    'aetm_path_list': ['aetm_%d.tif' % (x+1) for x in xrange(N_MONTHS)],
    'flow_dir_path': 'flow_dir.tif',
    'qfm_path_list': ['qf_%d.tif' % (x+1) for x in xrange(N_MONTHS)],
    'stream_path': 'stream.tif',
}

_TMP_BASE_FILES = {
    'outflow_direction_path': 'outflow_direction.tif',
    'outflow_weights_path': 'outflow_weights.tif',
    'kc_path': 'kc.tif',
    'si_path': 'Si.tif',
    'lulc_aligned_path': 'lulc_aligned.tif',
    'dem_aligned_path': 'dem_aligned.tif',
    'loss_path': 'loss.tif',
    'zero_absorption_source_path': 'zero_absorption.tif',
    'soil_group_aligned_path': 'soil_group_aligned.tif',
    'flow_accum_path': 'flow_accum.tif',
    'precip_path_aligned_list': ['prcp_a%d.tif' % x for x in xrange(N_MONTHS)],
    'n_events_path_list': ['n_events%d.tif' % x for x in xrange(N_MONTHS)],
    'et0_path_aligned_list': ['et0_a%d.tif' % x for x in xrange(N_MONTHS)],
    'kc_path_list': ['kc_%d.tif' % x for x in xrange(N_MONTHS)],
    'l_aligned_path': 'l_aligned.tif',
    'cz_aligned_raster_path': 'cz_aligned.tif',
    'l_sum_pre_clamp': 'l_sum_pre_clamp.tif'
    }


def execute(args):
    """Seasonal Water Yield.

    This function invokes the InVEST seasonal water yield model described in
    "Spatial attribution of baseflow generation at the parcel level for
    ecosystem-service valuation", Guswa, et. al (under review in "Water
    Resources Research")

    Parameters:
        args['workspace_dir'] (string): output directory for intermediate,
        temporary, and final files
        args['results_suffix'] (string): (optional) string to append to any
            output files
        args['threshold_flow_accumulation'] (number): used when classifying
            stream pixels from the DEM by thresholding the number of upstream
            cells that must flow into a cell before it's considered
            part of a stream.
        args['et0_dir'] (string): required if
            args['user_defined_local_recharge'] is False.  Path to a directory
            that contains rasters of monthly reference evapotranspiration;
            units in mm.
        args['precip_dir'] (string): required if
            args['user_defined_local_recharge'] is False. A path to a directory
            that contains rasters of monthly precipitation; units in mm.
        args['dem_raster_path'] (string): a path to a digital elevation raster
        args['lulc_raster_path'] (string): a path to a land cover raster used
            to classify biophysical properties of pixels.
        args['soil_group_path'] (string): required if
            args['user_defined_local_recharge'] is  False. A path to a raster
            indicating SCS soil groups where integer values are mapped to soil
            types::

                1: A
                2: B
                3: C
                4: D

        args['aoi_path'] (string): path to a vector that indicates the area
            over which the model should be run, as well as the area in which to
            aggregate over when calculating the output Qb.
        args['biophysical_table_path'] (string): path to a CSV table that maps
            landcover codes paired with soil group types to curve numbers as
            well as Kc values.  Headers must include 'lucode', 'CN_A', 'CN_B',
            'CN_C', 'CN_D', 'Kc_1', 'Kc_2', 'Kc_3', 'Kc_4', 'Kc_5', 'Kc_6',
            'Kc_7', 'Kc_8', 'Kc_9', 'Kc_10', 'Kc_11', 'Kc_12'.
        args['rain_events_table_path'] (string): Not required if
            args['user_defined_local_recharge'] is True or
            args['user_defined_climate_zones'] is True.  Path to a CSV table
            that has headers 'month' (1-12) and 'events' (int >= 0) that
            indicates the number of rain events per month
        args['alpha_m'] (float or string): required if args['monthly_alpha'] is
            false.  Is the proportion of upslope annual available local
            recharge that is available in month m.
        args['beta_i'] (float or string): is the fraction of the upgradient
            subsidy that is available for downgradient evapotranspiration.
        args['gamma'] (float or string): is the fraction of pixel local
            recharge that is available to downgradient pixels.
        args['user_defined_local_recharge'] (boolean): if True, indicates user
            will provide pre-defined local recharge raster layer
        args['l_path'] (string): required if
            args['user_defined_local_recharge'] is True.  If provided pixels
            indicate the amount of local recharge; units in mm.
        args['user_defined_climate_zones'] (boolean): if True, user provides
            a climate zone rain events table and a climate zone raster map in
            lieu of a global rain events table.
        args['climate_zone_table_path'] (string): required if
            args['user_defined_climate_zones'] is True. Contains monthly
            precipitation events per climate zone.  Fields must be:
            "cz_id", "jan", "feb", "mar", "apr", "may", "jun", "jul",
            "aug", "sep", "oct", "nov", "dec".
        args['climate_zone_raster_path'] (string): required if
            args['user_defined_climate_zones'] is True, pixel values correspond
            to the "cz_id" values defined in args['climate_zone_table_path']
        args['monthly_alpha'] (boolean): if True, use the alpha
        args['monthly_alpha_path'] (string): required if args['monthly_alpha']
            is True.

    Returns:
        ``None``
    """
    # This upgrades warnings to exceptions across this model.
    # I found this useful to catch all kinds of weird inputs to the model
    # during debugging and think it makes sense to have in production of this
    # model too.
    try:
        warnings.filterwarnings('error')
        _execute(args)
    finally:
        warnings.resetwarnings()


def _execute(args):
    """Execute the seasonal water yield model.

    Parameters:
        See the parameters for
        `natcap.invest.seasonal_water_yield.seasonal_wateryield.execute`.

    Returns:
        None
    """
    LOGGER.info('prepare and test inputs for common errors')

    # fail early on a missing required rain events table
    if (not args['user_defined_local_recharge'] and
            not args['user_defined_climate_zones']):
        rain_events_lookup = (
            utils.build_lookup_from_csv(
                args['rain_events_table_path'], 'month'))

    biophysical_table = utils.build_lookup_from_csv(
        args['biophysical_table_path'], 'lucode')

    if args['monthly_alpha']:
        # parse out the alpha lookup table of the form (month_id: alpha_val)
        alpha_month = dict(
            (key, val['alpha']) for key, val in
            utils.build_lookup_from_csv(
                args['monthly_alpha_path'], 'month').iteritems())
    else:
        # make all 12 entries equal to args['alpha_m']
        alpha_m = float(fractions.Fraction(args['alpha_m']))
        alpha_month = dict(
            (month_index+1, alpha_m) for month_index in xrange(12))

    beta_i = float(fractions.Fraction(args['beta_i']))
    gamma = float(fractions.Fraction(args['gamma']))
    threshold_flow_accumulation = float(args['threshold_flow_accumulation'])
    pixel_size = pygeoprocessing.get_raster_info(
        args['dem_raster_path'])['pixel_size']
    file_suffix = utils.make_suffix_string(args, 'results_suffix')
    intermediate_output_dir = os.path.join(
        args['workspace_dir'], 'intermediate_outputs')
    output_dir = args['workspace_dir']
    utils.make_directories([intermediate_output_dir, output_dir])

    LOGGER.info('Building file registry')
    file_registry = utils.build_file_registry(
        [(_OUTPUT_BASE_FILES, output_dir),
         (_INTERMEDIATE_BASE_FILES, intermediate_output_dir),
         (_TMP_BASE_FILES, output_dir)], file_suffix)

    LOGGER.info('Checking that the AOI is not the output aggregate vector')
    if (os.path.normpath(args['aoi_path']) ==
            os.path.normpath(file_registry['aggregate_vector_path'])):
        raise ValueError(
            "The input AOI is the same as the output aggregate vector, "
            "please choose a different workspace or move the AOI file "
            "out of the current workspace %s" %
            file_registry['aggregate_vector_path'])

    LOGGER.info('Aligning and clipping dataset list')
    input_align_list = [args['lulc_raster_path'], args['dem_raster_path']]
    output_align_list = [
        file_registry['lulc_aligned_path'], file_registry['dem_aligned_path']]
    if not args['user_defined_local_recharge']:
        precip_path_list = []
        et0_path_list = []

        et0_dir_list = [
            os.path.join(args['et0_dir'], f) for f in os.listdir(
                args['et0_dir'])]
        precip_dir_list = [
            os.path.join(args['precip_dir'], f) for f in os.listdir(
                args['precip_dir'])]

        for month_index in range(1, N_MONTHS + 1):
            month_file_match = re.compile(r'.*[^\d]%d\.[^.]+$' % month_index)

            for data_type, dir_list, path_list in [
                    ('et0', et0_dir_list, et0_path_list),
                    ('Precip', precip_dir_list, precip_path_list)]:
                file_list = [
                    month_file_path for month_file_path in dir_list
                    if month_file_match.match(month_file_path)]
                if len(file_list) == 0:
                    raise ValueError(
                        "No %s found for month %d" % (data_type, month_index))
                if len(file_list) > 1:
                    raise ValueError(
                        "Ambiguous set of files found for month %d: %s" %
                        (month_index, file_list))
                path_list.append(file_list[0])

        input_align_list = (
            precip_path_list + [args['soil_group_path']] + et0_path_list +
            input_align_list)
        output_align_list = (
            file_registry['precip_path_aligned_list'] +
            [file_registry['soil_group_aligned_path']] +
            file_registry['et0_path_aligned_list'] + output_align_list)

    align_index = len(input_align_list) - 1  # this aligns with the DEM
    if args['user_defined_local_recharge']:
        input_align_list.append(args['l_path'])
        output_align_list.append(file_registry['l_aligned_path'])
    elif args['user_defined_climate_zones']:
        input_align_list.append(args['climate_zone_raster_path'])
        output_align_list.append(
            file_registry['cz_aligned_raster_path'])
    interpolate_list = ['nearest'] * len(input_align_list)

    pygeoprocessing.align_and_resize_raster_stack(
        input_align_list, output_align_list, interpolate_list,
        pixel_size, 'intersection', base_vector_path_list=[args['aoi_path']],
        raster_align_index=align_index)

    LOGGER.info('flow direction')
    natcap.invest.pygeoprocessing_0_3_3.routing.flow_direction_d_inf(
        file_registry['dem_aligned_path'],
        file_registry['flow_dir_path'])

    LOGGER.info('flow weights')
    natcap.invest.pygeoprocessing_0_3_3.routing.routing_core.calculate_flow_weights(
        file_registry['flow_dir_path'],
        file_registry['outflow_weights_path'],
        file_registry['outflow_direction_path'])

    LOGGER.info('flow accumulation')
    natcap.invest.pygeoprocessing_0_3_3.routing.flow_accumulation(
        file_registry['flow_dir_path'],
        file_registry['dem_aligned_path'],
        file_registry['flow_accum_path'])

    LOGGER.info('stream thresholding')
    natcap.invest.pygeoprocessing_0_3_3.routing.stream_threshold(
        file_registry['flow_accum_path'],
        threshold_flow_accumulation,
        file_registry['stream_path'])

    LOGGER.info('quick flow')
    if args['user_defined_local_recharge']:
        file_registry['l_path'] = file_registry['l_aligned_path']
        li_nodata = pygeoprocessing.get_raster_info(
            file_registry['l_path'])['nodata'][0]

        def l_avail_op(l_array):
            """Calculate equation [8] L_avail = min(gamma*L, L)"""
            result = numpy.empty(l_array.shape)
            result[:] = li_nodata
            valid_mask = (l_array != li_nodata)
            result[valid_mask] = numpy.min(numpy.stack(
                (gamma*l_array[valid_mask], l_array[valid_mask])), axis=0)
            return result
        pygeoprocessing.raster_calculator(
            [(file_registry['l_path'], 1)], l_avail_op,
            file_registry['l_avail_path'], gdal.GDT_Float32, li_nodata)
    else:
        # user didn't predefine local recharge so calculate it
        LOGGER.info('loading number of monthly events')
        for month_id in xrange(N_MONTHS):
            if args['user_defined_climate_zones']:
                cz_rain_events_lookup = (
                    utils.build_lookup_from_csv(
                        args['climate_zone_table_path'], 'cz_id'))
                month_label = MONTH_ID_TO_LABEL[month_id]
                climate_zone_rain_events_month = dict([
                    (cz_id, cz_rain_events_lookup[cz_id][month_label]) for
                    cz_id in cz_rain_events_lookup])
                n_events_nodata = -1
                pygeoprocessing.reclassify_raster(
                    (file_registry['cz_aligned_raster_path'], 1),
                    climate_zone_rain_events_month,
                    file_registry['n_events_path_list'][month_id],
                    gdal.GDT_Float32, n_events_nodata, values_required=True)
            else:
                # rain_events_lookup defined near entry point of execute
                n_events = rain_events_lookup[month_id+1]['events']
                pygeoprocessing.new_raster_from_base(
                    file_registry['dem_aligned_path'],
                    file_registry['n_events_path_list'][month_id],
                    gdal.GDT_Float32, [TARGET_NODATA],
                    fill_value_list=[n_events])

        LOGGER.info('calculate curve number')
        _calculate_curve_number_raster(
            file_registry['lulc_aligned_path'],
            file_registry['soil_group_aligned_path'],
            biophysical_table, file_registry['cn_path'])

        LOGGER.info('calculate Si raster')
        _calculate_si_raster(
            file_registry['cn_path'], file_registry['stream_path'],
            file_registry['si_path'])

        for month_index in xrange(N_MONTHS):
            LOGGER.info('calculate quick flow for month %d', month_index+1)
            _calculate_monthly_quick_flow(
                file_registry['precip_path_aligned_list'][month_index],
                file_registry['lulc_aligned_path'], file_registry['cn_path'],
                file_registry['n_events_path_list'][month_index],
                file_registry['stream_path'],
                file_registry['qfm_path_list'][month_index],
                file_registry['si_path'])

        qf_nodata = -1
        LOGGER.info('calculate QFi')

        # TODO: lose this loop
        def qfi_sum_op(*qf_values):
            """Sum the monthly qfis."""
            qf_sum = numpy.zeros(qf_values[0].shape)
            valid_mask = qf_values[0] != qf_nodata
            valid_qf_sum = qf_sum[valid_mask]
            for index in range(len(qf_values)):
                valid_qf_sum += qf_values[index][valid_mask]
            qf_sum[:] = qf_nodata
            qf_sum[valid_mask] = valid_qf_sum
            return qf_sum

        pygeoprocessing.raster_calculator(
            [(path, 1) for path in file_registry['qfm_path_list']],
            qfi_sum_op, file_registry['qf_path'], gdal.GDT_Float32, qf_nodata)

        LOGGER.info('calculate local recharge')
        kc_lookup = {}
        LOGGER.info('classify kc')
        for month_index in xrange(12):
            kc_lookup = dict([
                (lucode, biophysical_table[lucode]['kc_%d' % (month_index+1)])
                for lucode in biophysical_table])
            kc_nodata = -1  # a reasonable nodata value
            pygeoprocessing.reclassify_raster(
                (file_registry['lulc_aligned_path'], 1), kc_lookup,
                file_registry['kc_path_list'][month_index], gdal.GDT_Float32,
                kc_nodata)

        # call through to a cython function that does the necessary routing
        # between AET and L.sum.avail in equation [7], [4], and [3]
        seasonal_water_yield_core.calculate_local_recharge(
            file_registry['precip_path_aligned_list'],
            file_registry['et0_path_aligned_list'],
            file_registry['qfm_path_list'],
            file_registry['flow_dir_path'],
            file_registry['outflow_weights_path'],
            file_registry['outflow_direction_path'],
            file_registry['dem_aligned_path'],
            file_registry['lulc_aligned_path'], alpha_month,
            beta_i, gamma, file_registry['stream_path'],
            file_registry['l_path'],
            file_registry['l_avail_path'],
            file_registry['l_sum_avail_path'],
            file_registry['aet_path'], file_registry['kc_path_list'])

    #calculate Qb as the sum of local_recharge_avail over the AOI, Eq [9]
    qb_sum, qb_valid_count = _sum_valid(file_registry['l_path'])
    qb_result = 0.0
    if qb_valid_count > 0:
        qb_result = qb_sum / qb_valid_count

    li_nodata = pygeoprocessing.get_raster_info(
        file_registry['l_path'])['nodata'][0]

    def vri_op(li_array):
        """Calculate vri index [Eq 10]."""
        result = numpy.empty_like(li_array)
        result[:] = li_nodata
        if qb_sum > 0:
            valid_mask = li_array != li_nodata
            result[valid_mask] = li_array[valid_mask] / qb_sum
        return result
    pygeoprocessing.raster_calculator(
        [(file_registry['l_path'], 1)], vri_op, file_registry['vri_path'],
        gdal.GDT_Float32, li_nodata)

    _aggregate_recharge(
        args['aoi_path'], file_registry['l_path'],
        file_registry['vri_path'],
        file_registry['aggregate_vector_path'])

    LOGGER.info('calculate L_sum')  # Eq. [12]
    pygeoprocessing.new_raster_from_base(
        file_registry['dem_aligned_path'],
        file_registry['zero_absorption_source_path'],
        gdal.GDT_Float32, [TARGET_NODATA], fill_value_list=[0.0])
    natcap.invest.pygeoprocessing_0_3_3.routing.route_flux(
        file_registry['flow_dir_path'],
        file_registry['dem_aligned_path'],
        file_registry['l_path'],
        file_registry['zero_absorption_source_path'],
        file_registry['loss_path'],
        file_registry['l_sum_pre_clamp'], 'flux_only',
        stream_uri=file_registry['stream_path'])

    # The result of route_flux can be slightly negative due to roundoff error
    # (on the order of 1e-4.  It is acceptable to clamp those values to 0.0
    l_sum_pre_clamp_nodata = pygeoprocessing.get_raster_info(
        file_registry['l_sum_pre_clamp'])['nodata'][0]

    def clamp_l_sum(l_sum_pre_clamp):
        """Clamp any negative values to 0.0."""
        result = l_sum_pre_clamp.copy()
        result[
            (l_sum_pre_clamp != l_sum_pre_clamp_nodata) &
            (l_sum_pre_clamp < 0.0)] = 0.0
        return result

    pygeoprocessing.raster_calculator(
        [(file_registry['l_sum_pre_clamp'], 1)], clamp_l_sum,
        file_registry['l_sum_path'], gdal.GDT_Float32, l_sum_pre_clamp_nodata)

    LOGGER.info('calculate B_sum')
    seasonal_water_yield_core.route_baseflow_sum(
        file_registry['dem_aligned_path'],
        file_registry['l_path'],
        file_registry['l_avail_path'],
        file_registry['l_sum_path'],
        file_registry['outflow_direction_path'],
        file_registry['outflow_weights_path'],
        file_registry['stream_path'],
        file_registry['b_sum_path'])

    LOGGER.info('calculate B')

    b_sum_nodata = li_nodata

    def op_b(b_sum, l_avail, l_sum):
        """Calculate B=max(B_sum*Lavail/L_sum, 0)."""
        valid_mask = (
            (b_sum != b_sum_nodata) & (l_avail != li_nodata) & (l_sum > 0) &
            (l_sum != l_sum_pre_clamp_nodata))
        result = numpy.empty(b_sum.shape)
        result[:] = b_sum_nodata
        result[valid_mask] = (
            b_sum[valid_mask] * l_avail[valid_mask] / l_sum[valid_mask])
        # if l_sum is zero, it's okay to make B zero says Perrine in an email
        result[l_sum == 0] = 0.0
        result[(result < 0) & valid_mask] = 0
        return result

    pygeoprocessing.raster_calculator(
        [(file_registry['b_sum_path'], 1),
         (file_registry['l_path'], 1),
         (file_registry['l_sum_path'], 1)], op_b, file_registry['b_path'],
        gdal.GDT_Float32, b_sum_nodata)

    LOGGER.info('deleting temporary files')
    for file_id in _TMP_BASE_FILES:
        try:
            if isinstance(file_registry[file_id], basestring):
                os.remove(file_registry[file_id])
            elif isinstance(file_registry[file_id], list):
                for index in xrange(len(file_registry[file_id])):
                    os.remove(file_registry[file_id][index])
        except OSError:
            # Let it go.
            pass

    LOGGER.info('  (\\w/)  SWY Complete!')
    LOGGER.info('  (..  \\ ')
    LOGGER.info(' _/  )  \\______')
    LOGGER.info('(oo /\'\\        )`,')
    LOGGER.info(' `--\' (v  __( / ||')
    LOGGER.info('       |||  ||| ||')
    LOGGER.info('      //_| //_|')


def _calculate_monthly_quick_flow(
        precip_path, lulc_raster_path, cn_path, n_events_raster_path,
        stream_path, qf_monthly_path, si_path):
    """Calculate quick flow for a month.

    Parameters:
        precip_path (string): path to file that correspond to monthly
            precipitation
        lulc_raster_path (string): path to landcover raster
        cn_path (string): path to curve number raster
        n_events_raster_path (string): a path to a raster where each pixel
            indicates the number of rain events.
        stream_path (string): path to stream mask raster where 1 indicates a
            stream pixel, 0 is a non-stream but otherwise valid area from the
            original DEM, and nodata indicates areas outside the valid DEM.
        qf_monthly_path_list (list of string): list of paths to output monthly
            rasters.
        si_path (string): list to output raster for potential maximum retention

    Returns:
        None
    """
    si_nodata = -1
    cn_nodata = pygeoprocessing.get_raster_info(cn_path)['nodata'][0]

    def si_op(ci_array, stream_array):
        """Potential maximum retention."""
        result = numpy.empty_like(ci_array)
        result[:] = si_nodata
        valid_mask = (ci_array != cn_nodata) & (ci_array != 0)
        result[valid_mask] = 1000.0 / ci_array[valid_mask] - 10
        result[(stream_array == 1) & valid_mask] = 0
        return result

    pygeoprocessing.raster_calculator(
         [(cn_path, 1), (stream_path, 1)], si_op, si_path, gdal.GDT_Float32,
         si_nodata)

    qf_nodata = -1
    p_nodata = pygeoprocessing.get_raster_info(precip_path)['nodata'][0]
    n_events_nodata = pygeoprocessing.get_raster_info(
        n_events_raster_path)['nodata'][0]
    stream_nodata = pygeoprocessing.get_raster_info(stream_path)['nodata'][0]

    def qf_op(p_im, s_i, n_events, stream_array):
        """Calculate quick flow as in Eq [1] in user's guide.

        Parameters:
            p_im (numpy.array): precipitation at pixel i on month m
            s_i (numpy.array): factor that is 1000/CN_i - 10
                (Equation 1b from user's guide)
            n_events (numpy.array): number of rain events on the pixel
            stream_mask (numpy.array): 1 if stream, otherwise not a stream
                pixel.

        Returns:
            quick flow (numpy.array)
        """
        valid_mask = (
            (p_im != p_nodata) & (s_i != si_nodata) & (p_im != 0.0) &
            (stream_array != 1) &
            (n_events != n_events_nodata) & (n_events > 0))
        valid_n_events = n_events[valid_mask]
        valid_si = s_i[valid_mask]

        # a_im is the mean rain depth on a rainy day at pixel i on month m
        # the 25.4 converts inches to mm since Si is in inches
        a_im = numpy.empty(valid_n_events.shape)
        a_im = p_im[valid_mask] / (valid_n_events * 25.4)
        qf_im = numpy.empty(p_im.shape)
        qf_im[:] = qf_nodata

        # Precompute the last two terms in quickflow so we can handle a
        # numerical instability when s_i is large and/or a_im is small
        # on large valid_si/a_im this number will be zero and the latter
        # exponent will also be zero because of a divide by zero. rather than
        # raise that numerical warning, just handle it manually
        E1 = scipy.special.expn(1, valid_si / a_im)  #pylint: disable=invalid-name,no-member
        E1[valid_si == 0] = 0
        nonzero_e1_mask = E1 != 0
        exp_result = numpy.zeros(valid_si.shape)
        exp_result[nonzero_e1_mask] = numpy.exp(
            (0.8 * valid_si[nonzero_e1_mask]) / a_im[nonzero_e1_mask] +
            numpy.log(E1[nonzero_e1_mask]))

        # qf_im is the quickflow at pixel i on month m Eq. [1]
        qf_im[valid_mask] = (25.4 * valid_n_events * (
            (a_im - valid_si) * numpy.exp(-0.2 * valid_si / a_im) +
            valid_si ** 2 / a_im * exp_result))

        # if precip is 0, then QF should be zero
        qf_im[(p_im == 0) | (n_events == 0)] = 0.0
        # if we're on a stream, set quickflow to the precipitation
        valid_stream_precip_mask = (stream_array == 1) & (p_im != p_nodata)
        qf_im[valid_stream_precip_mask] = p_im[valid_stream_precip_mask]

        # this handles some user cases where they don't have data defined on
        # their landcover raster. It otherwise crashes later with some NaNs
        qf_im[(qf_im == qf_nodata) & (stream_array != stream_nodata)] = 0.0
        return qf_im

    pygeoprocessing.raster_calculator(
        [(path, 1) for path in [
            precip_path, si_path, n_events_raster_path, stream_path]], qf_op,
        qf_monthly_path, gdal.GDT_Float32, qf_nodata)


def _calculate_curve_number_raster(
        lulc_raster_path, soil_group_path, biophysical_table, cn_path):
    """Calculate the CN raster from the landcover and soil group rasters.

    Parameters:
        lulc_raster_path (string): path to landcover raster
        soil_group_path (string): path to raster indicating soil group where
            pixel values are in [1,2,3,4]
        biophysical_table (dict): maps landcover IDs to dictionaries that
            contain at least the keys 'cn_a', 'cn_b', 'cn_c', 'cn_d', that
            map to the curve numbers for that landcover and soil type.
        cn_path (string): path to output curve number raster to be output
            which will be the dimensions of the intersection of
            `lulc_raster_path` and `soil_group_path` the cell size of
            `lulc_raster_path`.

    Returns:
        None
    """
    soil_nodata = pygeoprocessing.get_raster_info(
        soil_group_path)['nodata'][0]
    map_soil_type_to_header = {
        1: 'cn_a',
        2: 'cn_b',
        3: 'cn_c',
        4: 'cn_d',
    }
    # curve numbers are always positive so -1 a good nodata choice
    cn_nodata = -1
    lulc_to_soil = {}
    lulc_nodata = pygeoprocessing.get_raster_info(
        lulc_raster_path)['nodata'][0]
    for soil_id, soil_column in map_soil_type_to_header.iteritems():
        lulc_to_soil[soil_id] = {
            'lulc_values': [],
            'cn_values': []
        }
        for lucode in sorted(biophysical_table.keys() + [lulc_nodata]):
            if lucode != lulc_nodata:
                lulc_to_soil[soil_id]['cn_values'].append(
                    biophysical_table[lucode][soil_column])
                lulc_to_soil[soil_id]['lulc_values'].append(lucode)
            else:
                # handle the lulc nodata with cn nodata
                lulc_to_soil[soil_id]['lulc_values'].append(lulc_nodata)
                lulc_to_soil[soil_id]['cn_values'].append(cn_nodata)

        # Making the landcover array a float32 in case the user provides a
        # float landcover map like Kate did.
        lulc_to_soil[soil_id]['lulc_values'] = (
            numpy.array(lulc_to_soil[soil_id]['lulc_values'],
                        dtype=numpy.float32))
        lulc_to_soil[soil_id]['cn_values'] = (
            numpy.array(lulc_to_soil[soil_id]['cn_values'],
                        dtype=numpy.float32))

    def cn_op(lulc_array, soil_group_array):
        """Map lulc code and soil to a curve number."""
        cn_result = numpy.empty(lulc_array.shape)
        cn_result[:] = cn_nodata
        for soil_group_id in numpy.unique(soil_group_array):
            if soil_group_id == soil_nodata:
                continue
            current_soil_mask = (soil_group_array == soil_group_id)
            index = numpy.digitize(
                lulc_array.ravel(),
                lulc_to_soil[soil_group_id]['lulc_values'], right=True)
            cn_values = (
                lulc_to_soil[soil_group_id]['cn_values'][index]).reshape(
                    lulc_array.shape)
            cn_result[current_soil_mask] = cn_values[current_soil_mask]
        return cn_result

    cn_nodata = -1
    pygeoprocessing.raster_calculator(
        [(lulc_raster_path, 1), (soil_group_path, 1)], cn_op, cn_path,
        gdal.GDT_Float32, cn_nodata)


def _calculate_si_raster(cn_path, stream_path, si_path):
    """Calculate the S factor of the quickflow equation [1].

    Parameters:
        cn_path (string): path to curve number raster
        stream_path (string): path to a stream raster (0, 1)
        si_path (string): path to output s_i raster

    Returns:
        None
    """
    si_nodata = -1
    cn_nodata = pygeoprocessing.get_raster_info(cn_path)['nodata'][0]

    def si_op(ci_factor, stream_mask):
        """Calculate si factor."""
        valid_mask = (ci_factor != cn_nodata) & (ci_factor > 0)
        si_array = numpy.empty(ci_factor.shape)
        si_array[:] = si_nodata
        # multiply by the stream mask != 1 so we get 0s on the stream and
        # unaffected results everywhere else
        si_array[valid_mask] = (
            (1000.0 / ci_factor[valid_mask] - 10) * (
                stream_mask[valid_mask] != 1))
        return si_array

    pygeoprocessing.raster_calculator(
        [(cn_path, 1), (stream_path, 1)], si_op, si_path, gdal.GDT_Float32,
        si_nodata)


def _aggregate_recharge(
        aoi_path, l_path, vri_path, aggregate_vector_path):
    """Aggregate recharge values for the provided watersheds/AOIs.

    Generates a new shapefile that's a copy of 'aoi_path' in sum values from L
    and Vri.

    Parameters:
        aoi_path (string): path to shapefile that will be used to
            aggregate rasters
        l_path (string): path to (L) local recharge raster
        vri_path (string): path to Vri raster
        aggregate_vector_path (string): path to shapefile that will be created
            by this function as the aggregating output.  will contain fields
            'l_sum' and 'vri_sum' per original feature in `aoi_path`.  If this
            file exists on disk prior to the call it is overwritten with
            the result of this call.

    Returns:
        None
    """
    if os.path.exists(aggregate_vector_path):
        LOGGER.warn(
            '%s exists, deleting and writing new output',
            aggregate_vector_path)
        os.remove(aggregate_vector_path)

    original_aoi_vector = gdal.OpenEx(aoi_path, gdal.OF_VECTOR)

    driver = gdal.GetDriverByName('ESRI Shapefile')
    driver.CreateCopy(aggregate_vector_path, original_aoi_vector)
    gdal.Dataset.__swig_destroy__(original_aoi_vector)
    original_aoi_vector = None
    aggregate_vector = gdal.OpenEx(aggregate_vector_path, 1)
    aggregate_layer = aggregate_vector.GetLayer()

    # make an identifying id per polygon that can be used for aggregation
    while True:
        serviceshed_defn = aggregate_layer.GetLayerDefn()
        poly_id_field = str(uuid.uuid4())[-8:]
        if serviceshed_defn.GetFieldIndex(poly_id_field) == -1:
            break
    layer_id_field = ogr.FieldDefn(poly_id_field, ogr.OFTInteger)
    aggregate_layer.CreateField(layer_id_field)
    for poly_index, poly_feat in enumerate(aggregate_layer):
        poly_feat.SetField(poly_id_field, poly_index)
        aggregate_layer.SetFeature(poly_feat)
    aggregate_layer.SyncToDisk()

    for raster_path, aggregate_field_id, op_type in [
            (l_path, 'qb', 'mean'), (vri_path, 'vri_sum', 'sum')]:

        # aggregate carbon stocks by the new ID field
        aggregate_stats = pygeoprocessing.zonal_statistics(
            (raster_path, 1), aggregate_vector_path, poly_id_field)

        aggregate_field = ogr.FieldDefn(aggregate_field_id, ogr.OFTReal)
        aggregate_field.SetWidth(24)
        aggregate_field.SetPrecision(11)
        aggregate_layer.CreateField(aggregate_field)

        aggregate_layer.ResetReading()
        for poly_index, poly_feat in enumerate(aggregate_layer):
            if op_type == 'mean':
                pixel_count = aggregate_stats[poly_index]['count']
                if pixel_count != 0:
                    value = (aggregate_stats[poly_index]['sum'] / pixel_count)
                else:
                    LOGGER.warn(
                        "no coverage for polygon %s", ', '.join(
                            [str(poly_feat.GetField(_)) for _ in xrange(
                                poly_feat.GetFieldCount())]))
                    value = 0.0
            elif op_type == 'sum':
                value = aggregate_stats[poly_index]['sum']
            poly_feat.SetField(aggregate_field_id, value)
            aggregate_layer.SetFeature(poly_feat)

    # don't need a random poly id anymore
    aggregate_layer.DeleteField(
        serviceshed_defn.GetFieldIndex(poly_id_field))
    aggregate_layer.SyncToDisk()
    aggregate_layer = None
    gdal.Dataset.__swig_destroy__(aggregate_vector)
    aggregate_vector = None


def _sum_valid(raster_path):
    """Calculate the sum of the non-nodata pixels in the raster.

    Parameters:
        raster_path (string): path to raster on disk

    Returns:
        (sum, n_pixels) tuple where sum is the sum of the non-nodata pixels
        and n_pixels is the count of them
    """
    raster_sum = 0
    raster_count = 0
    raster_nodata = pygeoprocessing.get_raster_info(raster_path)['nodata'][0]

    for _, block in pygeoprocessing.iterblocks(
            raster_path, band_index_list=[1]):
        valid_mask = block != raster_nodata
        raster_sum += numpy.sum(block[valid_mask])
        raster_count += numpy.count_nonzero(valid_mask)
    return raster_sum, raster_count


@validation.invest_validator
def validate(args, limit_to=None):
    """Validate args to ensure they conform to `execute`'s contract.

    Parameters:
        args (dict): dictionary of key(str)/value pairs where keys and
            values are specified in `execute` docstring.
        limit_to (str): (optional) if not None indicates that validation
            should only occur on the args[limit_to] value. The intent that
            individual key validation could be significantly less expensive
            than validating the entire `args` dictionary.

    Returns:
        list of ([invalid key_a, invalid_keyb, ...], 'warning/error message')
            tuples. Where an entry indicates that the invalid keys caused
            the error message in the second part of the tuple. This should
            be an empty list if validation succeeds.
    """
    missing_key_list = []
    no_value_list = []
    validation_error_list = []

    required_keys = [
        'workspace_dir',
        'threshold_flow_accumulation',
        'dem_raster_path',
        'lulc_raster_path',
        'aoi_path',
        'biophysical_table_path',
        'beta_i',
        'gamma']

    if 'user_defined_local_recharge' in args:
        if args['user_defined_local_recharge']:
            required_keys.append('l_path')
        else:
            required_keys.extend([
                'et0_dir',
                'precip_dir',
                'soil_group_path'])

    if ('user_defined_climate_zones' in args and
            args['user_defined_climate_zones']):
        required_keys.extend([
            'climate_zone_table_path',
            'climate_zone_raster_path'])
    else:
        required_keys.extend(['rain_events_table_path'])

    if 'monthly_alpha_path' in args:
        if args['monthly_alpha_path']:
            required_keys.append('monthly_alpha_path')
        else:
            required_keys.append('alpha_m')

    for key in required_keys:
        if limit_to is None or limit_to == key:
            if key not in args:
                missing_key_list.append(key)
            elif args[key] in ['', None]:
                no_value_list.append(key)

    if len(missing_key_list) > 0:
        # if there are missing keys, we have raise KeyError to stop hard
        raise KeyError(
            "The following keys were expected in `args` but were missing " +
            ', '.join(missing_key_list))

    if len(no_value_list) > 0:
        validation_error_list.append(
            (no_value_list, 'parameter has no value'))

    file_type_list = [
        ('dem_raster_path', 'raster'),
        ('lulc_raster_path', 'raster'),
        ('aoi_path', 'vector'),
        ('biophysical_table_path', 'table'),
        ('climate_zone_table_path', 'table'),
        ('climate_zone_raster_path', 'raster'),
        ('monthly_alpha_path', 'table'),
        ('precip_dir', 'directory'),
        ('soil_group_path', 'raster'),
        ('rain_events_table_path', 'table'),
        ('l_path', 'raster')]

    # check that existing/optional files are the correct types
    with utils.capture_gdal_logging():
        for key, key_type in file_type_list:
            if (limit_to in [None, key]) and key in required_keys:
                if not os.path.exists(args[key]):
                    validation_error_list.append(
                        ([key], 'not found on disk'))
                    continue
                if key_type == 'raster':
                    raster = gdal.OpenEx(args[key])
                    if raster is None:
                        validation_error_list.append(
                            ([key], 'not a raster'))
                    del raster
                elif key_type == 'vector':
                    vector = gdal.OpenEx(args[key])
                    if vector is None:
                        validation_error_list.append(
                            ([key], 'not a vector'))
                    del vector

    return validation_error_list
