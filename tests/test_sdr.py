"""InVEST SDR model tests."""

import unittest
import tempfile
import shutil
import os

import numpy
from osgeo import ogr
from osgeo import osr
from natcap.invest.pygeoprocessing_0_3_3.testing import scm

SAMPLE_DATA = os.path.join(
    os.path.dirname(__file__), '..', 'data', 'invest-data',
    'Base_Data', 'Freshwater')
REGRESSION_DATA = os.path.join(
    os.path.dirname(__file__), '..', 'data', 'invest-test-data',
    'sdr')


class SDRTests(unittest.TestCase):
    """Regression tests for InVEST SDR model."""

    def setUp(self):
        """Initalize SDRRegression tests."""
        self.workspace_dir = tempfile.mkdtemp()

    def tearDown(self):
        """Clean up remaining files."""
        shutil.rmtree(self.workspace_dir)

    @staticmethod
    def generate_base_args(workspace_dir):
        """Generate a base sample args dict for SDR."""
        args = {
            'biophysical_table_path': os.path.join(
                SAMPLE_DATA, 'biophysical_table.csv'),
            'dem_path': os.path.join(SAMPLE_DATA, 'dem'),
            'erodibility_path': os.path.join(
                SAMPLE_DATA, 'erodibility_SI_clip.tif'),
            'erosivity_path': os.path.join(SAMPLE_DATA, 'erosivity'),
            'ic_0_param': '0.5',
            'k_param': '2',
            'lulc_path': os.path.join(SAMPLE_DATA, 'landuse_90'),
            'sdr_max': '0.8',
            'threshold_flow_accumulation': '1000',
            'watersheds_path': os.path.join(SAMPLE_DATA, 'watersheds.shp'),
            'workspace_dir': workspace_dir,
        }
        return args

    @scm.skip_if_data_missing(SAMPLE_DATA)
    @scm.skip_if_data_missing(REGRESSION_DATA)
    def test_sdr_validation(self):
        """SDR test regular validation."""
        from natcap.invest import sdr

        # use predefined directory so test can clean up files during teardown
        args = SDRTests.generate_base_args(
            self.workspace_dir)
        args['drainage_path'] = os.path.join(
            REGRESSION_DATA, 'sample_drainage.tif')
        validate_result = sdr.validate(args, limit_to=None)
        self.assertFalse(
            validate_result,
            "expected no failed validations instead got %s" % str(
                validate_result))

    @scm.skip_if_data_missing(SAMPLE_DATA)
    @scm.skip_if_data_missing(REGRESSION_DATA)
    def test_sdr_validation_wrong_types(self):
        """SDR test validation for wrong GIS types."""
        from natcap.invest import sdr

        # use predefined directory so test can clean up files during teardown
        args = SDRTests.generate_base_args(
            self.workspace_dir)
        # swap watershed and dem for different types
        args['dem_path'], args['watersheds_path'] = (
            args['watersheds_path'], args['dem_path'])
        validate_result = sdr.validate(args, limit_to=None)
        self.assertTrue(
            validate_result,
            "expected failed validations instead didn't get any")
        self.assertTrue(all(
            [x[1] in ['not a raster', 'not a vector']
            for x in validate_result]))

    @scm.skip_if_data_missing(SAMPLE_DATA)
    @scm.skip_if_data_missing(REGRESSION_DATA)
    def test_sdr_validation_missing_key(self):
        """SDR test validation that's missing keys."""
        from natcap.invest import sdr

        # use predefined directory so test can clean up files during teardown
        args = {}
        with self.assertRaises(KeyError) as context:
            validate_result = sdr.validate(args, limit_to=None)
        self.assertTrue(
            'The following keys were expected' in str(context.exception))

    @scm.skip_if_data_missing(SAMPLE_DATA)
    @scm.skip_if_data_missing(REGRESSION_DATA)
    def test_sdr_validation_key_no_value(self):
        """SDR test validation that's missing a value on a key."""
        from natcap.invest import sdr

        # use predefined directory so test can clean up files during teardown
        args = SDRTests.generate_base_args(
            self.workspace_dir)
        args['dem_path'] = ''
        validate_result = sdr.validate(args, limit_to=None)
        self.assertTrue(
            validate_result,
            'expected a validation error but didn\'t get one')

    @scm.skip_if_data_missing(SAMPLE_DATA)
    @scm.skip_if_data_missing(REGRESSION_DATA)
    def test_sdr_validation_watershed_missing_ws_id(self):
        """SDR test validation notices missing `ws_id` field on watershed."""
        from natcap.invest import sdr

        vector_driver = ogr.GetDriverByName("ESRI Shapefile")
        test_watershed_path = os.path.join(
            self.workspace_dir, 'watershed.shp')
        vector = vector_driver.CreateDataSource(test_watershed_path)
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(4326)
        layer = vector.CreateLayer("watershed", srs, ogr.wkbPoint)
        # forget to add a 'ws_id' field
        layer.CreateField(ogr.FieldDefn("id", ogr.OFTInteger))
        feature = ogr.Feature(layer.GetLayerDefn())
        feature.SetField("id", 0)
        feature.SetGeometry(ogr.CreateGeometryFromWkt("POINT(-112.2 42.5)"))
        layer.CreateFeature(feature)
        feature = None
        layer = None
        vector = None

        # use predefined directory so test can clean up files during teardown
        args = SDRTests.generate_base_args(
            self.workspace_dir)
        args['watersheds_path'] = test_watershed_path
        validate_result = sdr.validate(args, limit_to=None)
        self.assertTrue(
            validate_result,
            'expected a validation error but didn\'t get one')
        self.assertTrue(
            'does not have a `ws_id` field defined' in validate_result[0][1],
            'expected a `ws_id` validation error, but got %s' % (
                validate_result))


    @scm.skip_if_data_missing(SAMPLE_DATA)
    @scm.skip_if_data_missing(REGRESSION_DATA)
    def test_sdr_validation_watershed_missing_ws_id_value(self):
        """SDR test validation notices bad value in `ws_id` watershed."""
        from natcap.invest import sdr

        vector_driver = ogr.GetDriverByName("ESRI Shapefile")
        test_watershed_path = os.path.join(
            self.workspace_dir, 'watershed.shp')
        vector = vector_driver.CreateDataSource(test_watershed_path)
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(4326)
        layer = vector.CreateLayer("watershed", srs, ogr.wkbPoint)
        # forget to add a 'ws_id' field
        layer.CreateField(ogr.FieldDefn("ws_id", ogr.OFTInteger))
        feature = ogr.Feature(layer.GetLayerDefn())
        # intentionally not setting ws_id
        feature.SetGeometry(ogr.CreateGeometryFromWkt("POINT(-112.2 42.5)"))
        layer.CreateFeature(feature)
        feature = None
        layer = None
        vector = None

        # use predefined directory so test can clean up files during teardown
        args = SDRTests.generate_base_args(
            self.workspace_dir)
        args['watersheds_path'] = test_watershed_path

        validate_result = sdr.validate(args, limit_to=None)
        self.assertTrue(
            validate_result,
            'expected a validation error but didn\'t get one')
        self.assertTrue(
            'feature 0 has an invalid value of' in validate_result[0][1],
            'expected an invalid `ws_id` value but got %s' % (
                validate_result))


    @scm.skip_if_data_missing(SAMPLE_DATA)
    @scm.skip_if_data_missing(REGRESSION_DATA)
    def test_base_regression(self):
        """SDR base regression test on sample data.

        Execute SDR with sample data and checks that the output files are
        generated and that the aggregate shapefile fields are the same as the
        regression case.
        """
        from natcap.invest import sdr

        # use predefined directory so test can clean up files during teardown
        args = SDRTests.generate_base_args(
            self.workspace_dir)
        # make args explicit that this is a base run of SWY
        sdr.execute(args)

        SDRTests._assert_regression_results_equal(
            args['workspace_dir'],
            os.path.join(REGRESSION_DATA, 'file_list_base.txt'),
            os.path.join(args['workspace_dir'], 'watershed_results_sdr.shp'),
            os.path.join(REGRESSION_DATA, 'agg_results_base.csv'))

    @scm.skip_if_data_missing(SAMPLE_DATA)
    @scm.skip_if_data_missing(REGRESSION_DATA)
    def test_output_exists_regression(self):
        """SDR test case where an output shapefile already exists.

        Execute SDR with sample data but workspace already contains
        "watershed_results_sdr.shp".  Model should delete file and proceed
        with report.
        """
        from natcap.invest import sdr

        # use predefined directory so test can clean up files during teardown
        args = SDRTests.generate_base_args(
            self.workspace_dir)

        # copy AOI on top of where the output shapefile should reside
        shutil.copy(
            args['watersheds_path'], os.path.join(
                self.workspace_dir, 'watershed_results_sdr.shp'))

        sdr.execute(args)

        SDRTests._assert_regression_results_equal(
            args['workspace_dir'],
            os.path.join(REGRESSION_DATA, 'file_list_base.txt'),
            os.path.join(args['workspace_dir'], 'watershed_results_sdr.shp'),
            os.path.join(REGRESSION_DATA, 'agg_results_base.csv'))

    @scm.skip_if_data_missing(SAMPLE_DATA)
    @scm.skip_if_data_missing(REGRESSION_DATA)
    def test_drainage_regression(self):
        """SDR drainage layer regression test on sample data.

        Execute SDR with sample data and a drainage layer and checks that the
        output files are generated and that the aggregate shapefile fields
        are the same as the regression case.
        """
        from natcap.invest import sdr

        # use predefined directory so test can clean up files during teardown
        args = SDRTests.generate_base_args(
            self.workspace_dir)
        args['drainage_path'] = os.path.join(
            REGRESSION_DATA, 'sample_drainage.tif')
        sdr.execute(args)

        SDRTests._assert_regression_results_equal(
            args['workspace_dir'],
            os.path.join(REGRESSION_DATA, 'file_list_drainage.txt'),
            os.path.join(args['workspace_dir'], 'watershed_results_sdr.shp'),
            os.path.join(REGRESSION_DATA, 'agg_results_drainage.csv'))

    @scm.skip_if_data_missing(SAMPLE_DATA)
    @scm.skip_if_data_missing(REGRESSION_DATA)
    def test_base_usle_c_too_large(self):
        """SDR test exepected exception for USLE_C > 1.0."""
        from natcap.invest import sdr

        # use predefined directory so test can clean up files during teardown
        args = SDRTests.generate_base_args(
            self.workspace_dir)
        args['biophysical_table_path'] = os.path.join(
            REGRESSION_DATA, 'biophysical_table_too_large.csv')

        with self.assertRaises(ValueError):
            sdr.execute(args)

    @scm.skip_if_data_missing(SAMPLE_DATA)
    @scm.skip_if_data_missing(REGRESSION_DATA)
    def test_base_usle_p_nan(self):
        """SDR test expected exception for USLE_P not a number."""
        from natcap.invest import sdr

        # use predefined directory so test can clean up files during teardown
        args = SDRTests.generate_base_args(
            self.workspace_dir)
        args['biophysical_table_path'] = os.path.join(
            REGRESSION_DATA, 'biophysical_table_invalid_value.csv')

        with self.assertRaises(ValueError):
            sdr.execute(args)

    @staticmethod
    def _assert_regression_results_equal(
            workspace_dir, file_list_path, result_vector_path,
            agg_results_path):
        """Test workspace state against expected aggregate results.

        Parameters:
            workspace_dir (string): path to the completed model workspace
            file_list_path (string): path to a file that has a list of all
                the expected files relative to the workspace base
            result_vector_path (string): path to the summary shapefile
                produced by the SWY model.
            agg_results_path (string): path to a csv file that has the
                expected aggregated_results.shp table in the form of
                fid,vri_sum,qb_val per line

        Returns:
            None

        Raises:
            AssertionError if any files are missing or results are out of
            range by `tolerance_places`
        """
        # test that the workspace has the same files as we expect
        SDRTests._test_same_files(
            file_list_path, workspace_dir)

        # we expect a file called 'aggregated_results.shp'
        result_vector = ogr.Open(result_vector_path)
        result_layer = result_vector.GetLayer()

        # The relative tolerance 1e-6 was determined by
        # experimentation on the application with the given range of numbers.
        # This is an apparently reasonable approach as described by ChrisF:
        # http://stackoverflow.com/a/3281371/42897
        # and even more reading about picking numerical tolerance (it's hard):
        # https://randomascii.wordpress.com/2012/02/25/comparing-floating-point-numbers-2012-edition/
        rel_tol = 1e-6

        with open(agg_results_path, 'rb') as agg_result_file:
            error_list = []
            for line in agg_result_file:
                fid, sed_retent, sed_export, usle_tot = [
                    float(x) for x in line.split(',')]
                feature = result_layer.GetFeature(int(fid))
                for field, value in [
                        ('sed_retent', sed_retent),
                        ('sed_export', sed_export),
                        ('usle_tot', usle_tot)]:
                    if not numpy.isclose(
                            feature.GetField(field), value, rtol=rel_tol):
                        error_list.append(
                            "FID %d %s expected %f, got %f" % (
                                fid, field, value, feature.GetField(field)))
                ogr.Feature.__swig_destroy__(feature)
                feature = None

        result_layer = None
        ogr.DataSource.__swig_destroy__(result_vector)
        result_vector = None

        if error_list:
            raise AssertionError('\n'.join(error_list))

    @staticmethod
    def _test_same_files(base_list_path, directory_path):
        """Assert files in `base_list_path` are in `directory_path`.

        Parameters:
            base_list_path (string): a path to a file that has one relative
                file path per line.
            directory_path (string): a path to a directory whose contents will
                be checked against the files listed in `base_list_file`

        Returns:
            None

        Raises:
            AssertionError when there are files listed in `base_list_file`
                that don't exist in the directory indicated by `path`
        """
        missing_files = []
        with open(base_list_path, 'r') as file_list:
            for file_path in file_list:
                full_path = os.path.join(directory_path, file_path.rstrip())
                if full_path == '':
                    continue
                if not os.path.isfile(full_path):
                    missing_files.append(full_path)
        if len(missing_files) > 0:
            raise AssertionError(
                "The following files were expected but not found: " +
                '\n'.join(missing_files))
