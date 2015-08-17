"""InVEST Carbon Edge Effect Model"""

import os
import logging
import math
import time

import uuid
import numpy
from osgeo import gdal
from osgeo import ogr
from osgeo import osr
import shapely
import shapely.geometry
import shapely.wkb
import pygeoprocessing
import scipy.spatial

logging.basicConfig(format='%(asctime)s %(name)-18s %(levelname)-8s \
    %(message)s', level=logging.DEBUG, datefmt='%m/%d/%Y %H:%M:%S ')

LOGGER = logging.getLogger('natcap.invest.carbon_edge_effect')

#grid cells are 100km so 1000km this is a good upper bound to search
DISTANCE_UPPER_BOUND = 100e4

#The number of nearest global edge carbon points to search for
N_NEAREST_POINTS = 3

def execute(args):
    """InVEST Carbon Edge Model calculates the carbon due to edge effects in
    forest pixels.

    Args:
        args['workspace_dir'] (string): a uri to the directory that will write
            output and other temporary files during calculation. (required)
        args['results_suffix'] (string): a string to append to any output file
            name (optional)
        args['serviceshed_uri'] (string): (optional) if present, a path to a
            shapefile that will be used to aggregate carbon stock results at the
            end of the run.
        args['biophysical_table_uri'] (string): a path to a CSV table that has
            at least a header for an 'lucode', 'is_forest', and 'c_above'.
                'lucode': an integer that corresponds to landcover codes in
                    the raster args['lulc_uri']

                'is_forest': either 0 or 1 indicating whether the landcover type
                    is forest (1) or not (0)

                'c_above': floating point number indicating tons of carbon per
                    hectare for that landcover type

                Example:
                    lucode, is_forest, c_above
                    0,0,32.8
                    1,1,n/a
                    2,1,n/a
                    16,0,28.1

        args['lulc_uri'] (string): path to a integer landcover code raster
        args['carbon_model_shape_uri'] (string): path to a shapefile that
            has points defining carbon edge models.  Has at least the fields
            'method', 'theta1', 'theta2', 'theta3'.

    returns None"""

    pygeoprocessing.create_directories([args['workspace_dir']])
    try:
        file_suffix = args['results_suffix']
        if file_suffix != "" and not file_suffix.startswith('_'):
            file_suffix = '_' + file_suffix
    except KeyError:
        file_suffix = ''

    #TASK: (optional) clip dataset to AOI if it exists

    #classify forest pixels from lulc
    biophysical_table = pygeoprocessing.get_lookup_from_table(
        args['biophysical_table_uri'], 'lucode')

    lucode_to_per_pixel_carbon = {}
    forest_codes = []
    cell_area_ha = pygeoprocessing.geoprocessing.get_cell_size_from_uri(
        args['lulc_uri']) ** 2 / 10000.0

    for lucode in biophysical_table:
        try:
            is_forest = int(biophysical_table[int(lucode)]['is_forest'])
            if is_forest == 1:
                forest_codes.append(lucode)
            lucode_to_per_pixel_carbon[int(lucode)] = float(
                biophysical_table[lucode]['c_above']) * cell_area_ha
        except ValueError:
            #this might be because the c_above parameter is n/a or undefined
            #because of forest
            lucode_to_per_pixel_carbon[int(lucode)] = 0.0

    #map aboveground carbon from table to lulc that is not forest
    carbon_map_nodata = -1
    non_forest_carbon_map_uri = os.path.join(
        args['workspace_dir'],
        'non_forest_carbon_map%s.tif' % file_suffix)

    pygeoprocessing.reclassify_dataset_uri(
        args['lulc_uri'], lucode_to_per_pixel_carbon,
        non_forest_carbon_map_uri, gdal.GDT_Float32, carbon_map_nodata)

    #map distance to edge
    non_forest_mask_uri = os.path.join(
        args['workspace_dir'], 'non_forest_mask%s.tif' % file_suffix)
    forest_mask_nodata = 255
    lulc_nodata = pygeoprocessing.get_nodata_from_uri(args['lulc_uri'])
    out_pixel_size = pygeoprocessing.get_cell_size_from_uri(args['lulc_uri'])
    def mask_non_forest_op(lulc_array):
        """converts forest lulc codes to 1"""
        forest_mask = ~numpy.in1d(lulc_array.flatten(), forest_codes).reshape(
            lulc_array.shape)
        nodata_mask = lulc_array == lulc_nodata
        return numpy.where(nodata_mask, forest_mask_nodata, forest_mask)
    pygeoprocessing.vectorize_datasets(
        [args['lulc_uri']], mask_non_forest_op, non_forest_mask_uri,
        gdal.GDT_Byte, forest_mask_nodata, out_pixel_size, "intersection",
        vectorize_op=False)

    edge_distance_uri = os.path.join(
        args['workspace_dir'], 'edge_distance%s.tif' % file_suffix)
    pygeoprocessing.distance_transform_edt(
        non_forest_mask_uri, edge_distance_uri)

    non_forest_carbon_stocks_uri = os.path.join(
        args['workspace_dir'],
        'non_forest_carbon_stocks%s.tif' % file_suffix)
    #calculate easy to read surface carbon map
    def non_forest_carbon_op(carbon_reclass, non_forest_mask):
        """Adds carbon values everywhere that's not forest"""
        return numpy.where(
            non_forest_mask == 1, carbon_reclass, carbon_map_nodata)

    pygeoprocessing.vectorize_datasets(
        [non_forest_carbon_map_uri, non_forest_mask_uri],
        non_forest_carbon_op, non_forest_carbon_stocks_uri, gdal.GDT_Float32,
        carbon_map_nodata, out_pixel_size, "intersection", vectorize_op=False)

    #Build spatial index for model for closest 3 points
    bounding_box = pygeoprocessing.get_bounding_box(args['lulc_uri'])
    model_bounding_box = shapely.geometry.box(*bounding_box)

    lulc_ref = osr.SpatialReference()
    lulc_projection_wkt = pygeoprocessing.get_dataset_projection_wkt_uri(
        args['lulc_uri'])
    lulc_ref.ImportFromWkt(lulc_projection_wkt)

    carbon_model_reproject_uri = os.path.join(
        args['workspace_dir'], 'local_carbon_shape.shp')

    pygeoprocessing.reproject_datasource_uri(
        args['carbon_model_shape_uri'], lulc_projection_wkt,
        carbon_model_reproject_uri)

    model_shape_ds = ogr.Open(carbon_model_reproject_uri)
    model_shape_layer = model_shape_ds.GetLayer()

    # coordinate transformation to model points to lulc projection
    kd_points = []
    theta_model_parameters = []
    method_model_parameter = []
    parameters_of_interest = ['theta1', 'theta2', 'theta3']
    for poly_feature in model_shape_layer:
        poly_geom = poly_feature.GetGeometryRef()

        #project point_feature to lulc_uri projection
        shapely_poly = shapely.wkb.loads(poly_geom.ExportToWkb())

        # test if point in bounding box and add to kd-tree if so
        if model_bounding_box.intersects(shapely_poly):
            poly_centroid = poly_geom.Centroid()
            #put in row/col order since rasters are row/col indexed
            kd_points.append([poly_centroid.GetY(), poly_centroid.GetX()])

            theta_model_parameters.append([
                poly_feature.GetField(feature_id) for feature_id in
                parameters_of_interest])
            method_model_parameter.append(poly_feature.GetField('method'))

    method_model_parameter = numpy.array(
        method_model_parameter, dtype=numpy.int32)
    theta_model_parameters = numpy.array(
        theta_model_parameters, dtype=numpy.float32)

    #if kd-tree is empty, raise exception
    if len(kd_points) == 0:
        raise ValueError("The input raster is outside any carbon edge model")

    kd_tree = scipy.spatial.cKDTree(kd_points)

    edge_carbon_map_uri = os.path.join(
        args['workspace_dir'], 'edge_carbon_map%s.tif' % file_suffix)
    carbon_edge_nodata = -9999.0
    pygeoprocessing.new_raster_from_base_uri(
        edge_distance_uri, edge_carbon_map_uri, 'GTiff', carbon_edge_nodata,
        gdal.GDT_Float32)

    edge_carbon_dataset = gdal.Open(edge_carbon_map_uri, gdal.GA_Update)
    edge_carbon_band = edge_carbon_dataset.GetRasterBand(1)
    edge_carbon_geotransform = edge_carbon_dataset.GetGeoTransform()

    edge_distance_dataset = gdal.Open(edge_distance_uri)
    edge_distance_band = edge_distance_dataset.GetRasterBand(1)
    block_size = edge_distance_band.GetBlockSize()

    n_rows = edge_carbon_dataset.RasterYSize
    n_cols = edge_carbon_dataset.RasterXSize

    cols_per_block, rows_per_block = block_size[0], block_size[1]
    n_col_blocks = int(math.ceil(n_cols / float(cols_per_block)))
    n_row_blocks = int(math.ceil(n_rows / float(rows_per_block)))

    last_time = time.time()

    last_row_block_width = None
    last_col_block_width = None

    for row_block_index in xrange(n_row_blocks):
        row_offset = row_block_index * rows_per_block
        row_block_width = n_rows - row_offset
        if row_block_width > rows_per_block:
            row_block_width = rows_per_block

        for col_block_index in xrange(n_col_blocks):
            col_offset = col_block_index * cols_per_block
            col_block_width = n_cols - col_offset
            if col_block_width > cols_per_block:
                col_block_width = cols_per_block

            current_time = time.time()
            if current_time - last_time > 5.0:
                LOGGER.info(
                    'carbon edge calculation approx. %.2f%% complete',
                    ((row_block_index * n_col_blocks + col_block_index) /
                     float(n_row_blocks * n_col_blocks) * 100.0))
                last_time = current_time

            #This is true at least once since last_* initialized with None
            if (last_row_block_width != row_block_width or
                    last_col_block_width != col_block_width):
                edge_distance_block = numpy.zeros(
                    (row_block_width, col_block_width), dtype=numpy.float32)

                last_row_block_width = row_block_width
                last_col_block_width = col_block_width

            edge_distance_band.ReadAsArray(
                xoff=col_offset, yoff=row_offset,
                win_xsize=col_block_width,
                win_ysize=row_block_width,
                buf_obj=edge_distance_block)

            #gdal treats the first axis as the row
            row_coords, col_coords = (
                numpy.mgrid[0:row_block_width, 0:col_block_width].astype(
                    numpy.float64))

            col_coords *= edge_carbon_geotransform[1] # x width
            row_coords *= edge_carbon_geotransform[5] # y width
            col_coords += (
                edge_carbon_geotransform[0] +
                edge_carbon_geotransform[1] * col_offset) # starting x coord
            row_coords += (
                edge_carbon_geotransform[3] +
                edge_carbon_geotransform[5] * row_offset) # starting y coord

            coord_points = zip(row_coords.ravel(), col_coords.ravel())

            distances, indexes = kd_tree.query(
                coord_points, k=N_NEAREST_POINTS,
                distance_upper_bound=DISTANCE_UPPER_BOUND, n_jobs=-1)
            distances = distances.reshape(
                edge_distance_block.shape[0], edge_distance_block.shape[1],
                N_NEAREST_POINTS)
            indexes = indexes.reshape(
                edge_distance_block.shape[0], edge_distance_block.shape[1],
                N_NEAREST_POINTS)

            thetas = numpy.zeros((
                edge_distance_block.shape[0], edge_distance_block.shape[1], 3))

            biomass = numpy.empty(
                (edge_distance_block.shape[0],
                 edge_distance_block.shape[1], N_NEAREST_POINTS),
                dtype=numpy.float32)

            for point_index in xrange(N_NEAREST_POINTS):
                valid_index_mask = (
                    (indexes[:, :, point_index] != len(kd_points)) &
                    (edge_distance_block > 0))
                for theta_index in xrange(3):
                    thetas[valid_index_mask, theta_index] = (
                        theta_model_parameters[
                            indexes[valid_index_mask, point_index],
                            theta_index])

                # the 3 is for the 3 models
                biomass_model = numpy.zeros((
                    edge_distance_block.shape[0],
                    edge_distance_block.shape[1], 3))

                #asymtotic model
                #biomass_1 = t1 - t2 * exp(-t3 * edge_dist_km)
                biomass_model[valid_index_mask, 0] = (
                    thetas[valid_index_mask, 0] -
                    thetas[valid_index_mask, 1] * numpy.exp(
                        -thetas[valid_index_mask, 2] *
                        edge_distance_block[valid_index_mask])) * cell_area_ha

                #logarithmic model
                #biomass_2 = t1 + t2 * numpy.log(edge_dist_km)
                biomass_model[valid_index_mask, 1] = (
                    thetas[valid_index_mask, 0] +
                    thetas[valid_index_mask, 1] * numpy.log(
                        edge_distance_block[valid_index_mask])) * cell_area_ha

                #linear regression
                #biomass_3 = t1 + t2 * edge_dist_km
                biomass_model[valid_index_mask, 2] = (
                    thetas[valid_index_mask, 0] +
                    thetas[valid_index_mask, 1] *
                    edge_distance_block[valid_index_mask]) * cell_area_ha

                biomass[valid_index_mask, point_index] = biomass_model[
                    valid_index_mask, method_model_parameter[indexes[
                        valid_index_mask, point_index]]-1]

                biomass[~valid_index_mask, point_index] = carbon_edge_nodata

            valid_distances = distances >= 0.0
            weights = numpy.zeros(distances.shape)
            weights[valid_distances] = (
                N_NEAREST_POINTS / distances[valid_distances])
            denom = numpy.sum(weights, axis=2)
            average_biomass = numpy.sum(weights * biomass, axis=2) / denom

            result = numpy.where(
                edge_distance_block > 0, average_biomass, carbon_edge_nodata)
            edge_carbon_band.WriteArray(
                result, xoff=col_offset, yoff=row_offset)

    cell_size_in_meters = pygeoprocessing.get_cell_size_from_uri(
        args['lulc_uri'])

    #combine maps into output
    carbon_map_uri = os.path.join(
        args['workspace_dir'], 'carbon_map%s.tif' % file_suffix)

    def combine_carbon_maps(non_forest_carbon, forest_carbon):
        """This combines the forest and non forest maps into one"""
        return numpy.where(
            forest_carbon == carbon_edge_nodata, non_forest_carbon,
            forest_carbon)
    pygeoprocessing.vectorize_datasets(
        [non_forest_carbon_stocks_uri, edge_carbon_map_uri],
        combine_carbon_maps, carbon_map_uri, gdal.GDT_Float32,
        carbon_map_nodata, cell_size_in_meters, 'intersection',
        vectorize_op=False, datasets_are_pre_aligned=True)

    #TASK: generate report (optional) by serviceshed if they exist
    if 'serviceshed_uri' in args:
        _aggregate_carbon_map(
            args['serviceshed_uri'], args['workspace_dir'], carbon_map_uri)


def _aggregate_carbon_map(serviceshed_uri, workspace_dir, carbon_map_uri):
    """Helper function to aggregate carbon values for the given serviceshed."""

    esri_driver = ogr.GetDriverByName('ESRI Shapefile')
    original_serviceshed_datasource = ogr.Open(serviceshed_uri)
    serviceshed_datasource_filename = os.path.join(
        workspace_dir, os.path.basename(serviceshed_uri))
    if os.path.exists(serviceshed_datasource_filename):
        os.remove(serviceshed_datasource_filename)
    serviceshed_result = esri_driver.CopyDataSource(
        original_serviceshed_datasource, serviceshed_datasource_filename)
    original_serviceshed_datasource = None
    serviceshed_layer = serviceshed_result.GetLayer()

    #make an identifying id per polygon that can be used for aggregation
    while True:
        serviceshed_defn = serviceshed_layer.GetLayerDefn()
        poly_id_field = str(uuid.uuid4())[-8:]
        if serviceshed_defn.GetFieldIndex(poly_id_field) == -1:
            break
    layer_id_field = ogr.FieldDefn(poly_id_field, ogr.OFTInteger)
    serviceshed_layer.CreateField(layer_id_field)
    for poly_index, poly_feat in enumerate(serviceshed_layer):
        poly_feat.SetField(poly_id_field, poly_index)
        serviceshed_layer.SetFeature(poly_feat)
    serviceshed_layer.SyncToDisk()

    #aggregate carbon stocks by the new ID field
    serviceshed_stats = pygeoprocessing.aggregate_raster_values_uri(
        carbon_map_uri, serviceshed_datasource_filename,
        shapefile_field=poly_id_field, ignore_nodata=True,
        threshold_amount_lookup=None, ignore_value_list=[],
        process_pool=None, all_touched=False)

    # don't need a random poly id anymore
    serviceshed_layer.DeleteField(
        serviceshed_defn.GetFieldIndex(poly_id_field))

    carbon_sum_field = ogr.FieldDefn('c_sum', ogr.OFTReal)
    carbon_mean_field = ogr.FieldDefn('c_ha_mean', ogr.OFTReal)
    serviceshed_layer.CreateField(carbon_sum_field)
    serviceshed_layer.CreateField(carbon_mean_field)

    serviceshed_layer.ResetReading()
    for poly_index, poly_feat in enumerate(serviceshed_layer):
        poly_feat.SetField(
            'c_sum', serviceshed_stats.total[poly_index])
        poly_feat.SetField(
            'c_ha_mean', serviceshed_stats.hectare_mean[poly_index])
        serviceshed_layer.SetFeature(poly_feat)
