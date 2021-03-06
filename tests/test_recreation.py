"""InVEST Recreation model tests."""
import datetime
import glob
import zipfile
import socket
import threading
import Queue
import unittest
import tempfile
import shutil
import os
import functools
import logging

import Pyro4
import natcap.invest.pygeoprocessing_0_3_3
from natcap.invest.pygeoprocessing_0_3_3.testing import scm
import numpy
from osgeo import ogr

Pyro4.config.SERIALIZER = 'marshal'  # allow null bytes in strings

SAMPLE_DATA = os.path.join(
    os.path.dirname(__file__), '..', 'data', 'invest-data',
    'recreation')
REGRESSION_DATA = os.path.join(
    os.path.dirname(__file__), '..', 'data', 'invest-test-data',
    'recreation_model')

LOGGER = logging.getLogger('test_recreation')


def _timeout(max_timeout):
    """Timeout decorator, parameter in seconds."""
    def timeout_decorator(target):
        """Wrap the original function."""
        work_queue = Queue.Queue()
        result_queue = Queue.Queue()

        def worker():
            """Read one func,args,kwargs tuple and execute."""
            func, args, kwargs = work_queue.get()
            result = func(*args, **kwargs)
            result_queue.put(result)

        work_thread = threading.Thread(target=worker)
        work_thread.daemon = True
        work_thread.start()

        @functools.wraps(target)
        def func_wrapper(*args, **kwargs):
            """Closure for function."""
            try:
                work_queue.put((target, args, kwargs))
                return result_queue.get(timeout=max_timeout)
            except Queue.Empty:
                raise RuntimeError("Timeout of %f exceeded" % max_timeout)
        return func_wrapper
    return timeout_decorator


class TestBufferedNumpyDiskMap(unittest.TestCase):
    """Tests for BufferedNumpyDiskMap."""

    def setUp(self):
        """Setup workspace."""
        self.workspace_dir = tempfile.mkdtemp()

    def test_basic_operation(self):
        """Recreation test buffered file manager basic ops w/ no buffer."""
        from natcap.invest.recreation import buffered_numpy_disk_map
        file_manager = buffered_numpy_disk_map.BufferedNumpyDiskMap(
            os.path.join(self.workspace_dir, 'test'), 0)

        file_manager.append(1234, numpy.array([1, 2, 3, 4]))
        file_manager.append(1234, numpy.array([1, 2, 3, 4]))
        file_manager.append(4321, numpy.array([-4, -1, -2, 4]))

        numpy.testing.assert_equal(
            file_manager.read(1234), numpy.array([1, 2, 3, 4, 1, 2, 3, 4]))

        numpy.testing.assert_equal(
            file_manager.read(4321), numpy.array([-4, -1, -2, 4]))

        file_manager.delete(1234)
        with self.assertRaises(IOError):
            file_manager.read(1234)

    def tearDown(self):
        """Delete workspace."""
        shutil.rmtree(self.workspace_dir)


class TestRecServer(unittest.TestCase):
    """Tests that set up local rec server on a port and call through."""

    def setUp(self):
        """Setup workspace."""
        self.workspace_dir = tempfile.mkdtemp()

    @scm.skip_if_data_missing(REGRESSION_DATA)
    def test_hashfile(self):
        """Recreation test for hash and fast hash of file."""
        from natcap.invest.recreation import recmodel_server
        file_path = os.path.join(REGRESSION_DATA, 'sample_data.csv')
        file_hash = recmodel_server._hashfile(
            file_path, blocksize=2**20, fast_hash=False)
        self.assertEqual(file_hash, 'b372f3f062afb3e8')

    @scm.skip_if_data_missing(REGRESSION_DATA)
    def test_hashfile_fast(self):
        """Recreation test for hash and fast hash of file."""
        from natcap.invest.recreation import recmodel_server
        file_path = os.path.join(REGRESSION_DATA, 'sample_data.csv')
        file_hash = recmodel_server._hashfile(
            file_path, blocksize=2**20, fast_hash=True)
        # we can't assert the full hash since it is dependant on the file
        # last access time and we can't reliably set that in Python.
        # instead we just check that at the very least it ends with _fast_hash
        self.assertTrue(file_hash.endswith('_fast_hash'))

    @scm.skip_if_data_missing(SAMPLE_DATA)
    @scm.skip_if_data_missing(REGRESSION_DATA)
    def test_year_order(self):
        """Recreation ensure that end year < start year raise ValueError."""
        from natcap.invest.recreation import recmodel_server

        with self.assertRaises(ValueError):
            # intentionally construct start year > end year
            _ = recmodel_server.RecModel(
                os.path.join(REGRESSION_DATA, 'sample_data.csv'),
                2014, 2005, os.path.join(self.workspace_dir, 'server_cache'))

    @scm.skip_if_data_missing(SAMPLE_DATA)
    @scm.skip_if_data_missing(REGRESSION_DATA)
    @_timeout(30.0)
    def test_workspace_fetcher(self):
        """Recreation test workspace fetcher on a local Pyro4 empty server."""
        from natcap.invest.recreation import recmodel_server
        from natcap.invest.recreation import recmodel_client
        from natcap.invest.recreation import recmodel_workspace_fetcher

        natcap.invest.pygeoprocessing_0_3_3.create_directories([self.workspace_dir])

        sample_point_data_path = os.path.join(
            REGRESSION_DATA, 'sample_data.csv')

        # Attempt a few connections, we've had this test be flaky on the
        # entire suite run which we suspect is because of a race condition
        server_launched = False
        for _ in range(3):
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.bind(('', 0))
                port = sock.getsockname()[1]
                sock.close()
                sock = None

                server_args = {
                    'hostname': 'localhost',
                    'port': port,
                    'raw_csv_point_data_path': sample_point_data_path,
                    'cache_workspace': self.workspace_dir,
                    'min_year': 2004,
                    'max_year': 2015,
                }

                server_thread = threading.Thread(
                    target=recmodel_server.execute, args=(server_args,))
                server_thread.daemon = True
                server_thread.start()
                server_launched = True
                break
            except:
                LOGGER.warn("Can't start server process on port %d", port)
        if not server_launched:
            self.fail("Server didn't start")

        path = "PYRO:natcap.invest.recreation@localhost:%s" % port
        LOGGER.info("Local server path %s", path)
        recreation_server = Pyro4.Proxy(path)
        aoi_path = os.path.join(
            REGRESSION_DATA, 'test_aoi_for_subset.shp')
        basename = os.path.splitext(aoi_path)[0]
        aoi_archive_path = os.path.join(
            self.workspace_dir, 'aoi_zipped.zip')
        with zipfile.ZipFile(aoi_archive_path, 'w') as myzip:
            for filename in glob.glob(basename + '.*'):
                myzip.write(filename, os.path.basename(filename))

        # convert shapefile to binary string for serialization
        zip_file_binary = open(aoi_archive_path, 'rb').read()
        date_range = (('2005-01-01'), ('2014-12-31'))
        out_vector_filename = 'test_aoi_for_subset_pud.shp'

        _, workspace_id = (
            recreation_server.calc_photo_user_days_in_aoi(
                zip_file_binary, date_range, out_vector_filename))
        fetcher_args = {
            'workspace_dir': self.workspace_dir,
            'hostname': 'localhost',
            'port': port,
            'workspace_id': workspace_id,
        }
        try:
            recmodel_workspace_fetcher.execute(fetcher_args)
        except:
            LOGGER.error(
                "Server process failed (%s) is_alive=%s",
                str(server_thread), server_thread.is_alive())
            raise

        out_workspace_dir = os.path.join(
            self.workspace_dir, 'workspace_zip')
        os.makedirs(out_workspace_dir)
        workspace_zip_path = os.path.join(
            self.workspace_dir, workspace_id + '.zip')
        zipfile.ZipFile(workspace_zip_path, 'r').extractall(
            out_workspace_dir)
        natcap.invest.pygeoprocessing_0_3_3.testing.assert_vectors_equal(
            aoi_path,
            os.path.join(out_workspace_dir, 'test_aoi_for_subset.shp'))

    @scm.skip_if_data_missing(REGRESSION_DATA)
    @_timeout(30.0)
    def test_empty_server(self):
        """Recreation test a client call to simple server."""
        from natcap.invest.recreation import recmodel_server
        from natcap.invest.recreation import recmodel_client

        natcap.invest.pygeoprocessing_0_3_3.create_directories([self.workspace_dir])
        empty_point_data_path = os.path.join(
            self.workspace_dir, 'empty_table.csv')
        open(empty_point_data_path, 'w').close()  # touch the file

        # attempt to get an open port; could result in race condition but
        # will be okay for a test. if this test ever fails because of port
        # in use, that's probably why
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(('', 0))
        port = sock.getsockname()[1]
        sock.close()
        sock = None

        server_args = {
            'hostname': 'localhost',
            'port': port,
            'raw_csv_point_data_path': empty_point_data_path,
            'cache_workspace': self.workspace_dir,
            'min_year': 2004,
            'max_year': 2015,
        }

        server_thread = threading.Thread(
            target=recmodel_server.execute, args=(server_args,))
        server_thread.daemon = True
        server_thread.start()

        client_args = {
            'aoi_path': os.path.join(
                REGRESSION_DATA, 'test_aoi_for_subset.shp'),
            'cell_size': 7000.0,
            'hostname': 'localhost',
            'port': port,
            'compute_regression': False,
            'start_year': '2005',
            'end_year': '2014',
            'grid_aoi': False,
            'results_suffix': u'',
            'workspace_dir': self.workspace_dir,
        }
        recmodel_client.execute(client_args)

        # testing for file existence seems reasonable since mostly we are
        # testing that a local server starts and a client connects to it
        _test_same_files(
            os.path.join(REGRESSION_DATA, 'file_list_empty_local_server.txt'),
            self.workspace_dir)

    @scm.skip_if_data_missing(SAMPLE_DATA)
    @scm.skip_if_data_missing(REGRESSION_DATA)
    def test_local_aggregate_points(self):
        """Recreation test single threaded local AOI aggregate calculation."""
        from natcap.invest.recreation import recmodel_client
        from natcap.invest.recreation import recmodel_server

        recreation_server = recmodel_server.RecModel(
            os.path.join(REGRESSION_DATA, 'sample_data.csv'),
            2005, 2014, os.path.join(self.workspace_dir, 'server_cache'))

        if not os.path.exists(self.workspace_dir):
            os.makedirs(self.workspace_dir)

        aoi_path = os.path.join(REGRESSION_DATA, 'test_aoi_for_subset.shp')

        basename = os.path.splitext(aoi_path)[0]
        aoi_archive_path = os.path.join(
            self.workspace_dir, 'aoi_zipped.zip')
        with zipfile.ZipFile(aoi_archive_path, 'w') as myzip:
            for filename in glob.glob(basename + '.*'):
                myzip.write(filename, os.path.basename(filename))

        # convert shapefile to binary string for serialization
        zip_file_binary = open(aoi_archive_path, 'rb').read()

        # transfer zipped file to server
        date_range = (('2005-01-01'), ('2014-12-31'))
        out_vector_filename = 'test_aoi_for_subset_pud.shp'
        zip_result, workspace_id = (
            recreation_server.calc_photo_user_days_in_aoi(
                zip_file_binary, date_range, out_vector_filename))

        # unpack result
        result_zip_path = os.path.join(self.workspace_dir, 'pud_result.zip')
        open(result_zip_path, 'wb').write(zip_result)
        zipfile.ZipFile(result_zip_path, 'r').extractall(self.workspace_dir)

        result_vector_path = os.path.join(
            self.workspace_dir, out_vector_filename)
        expected_vector_path = os.path.join(
            REGRESSION_DATA, 'test_aoi_for_subset_pud.shp')
        natcap.invest.pygeoprocessing_0_3_3.testing.assert_vectors_equal(
            expected_vector_path, result_vector_path)

        # ensure the remote workspace is as expected
        workspace_zip_binary = recreation_server.fetch_workspace_aoi(
            workspace_id)
        out_workspace_dir = os.path.join(self.workspace_dir, 'workspace_zip')
        os.makedirs(out_workspace_dir)
        workspace_zip_path = os.path.join(out_workspace_dir, 'workspace.zip')
        open(workspace_zip_path, 'wb').write(workspace_zip_binary)
        zipfile.ZipFile(workspace_zip_path, 'r').extractall(out_workspace_dir)
        natcap.invest.pygeoprocessing_0_3_3.testing.assert_vectors_equal(
            aoi_path,
            os.path.join(out_workspace_dir, 'test_aoi_for_subset.shp'))

    @scm.skip_if_data_missing(SAMPLE_DATA)
    @scm.skip_if_data_missing(REGRESSION_DATA)
    def test_local_calc_poly_pud(self):
        """Recreation test single threaded local PUD calculation."""
        from natcap.invest.recreation import recmodel_client
        from natcap.invest.recreation import recmodel_server

        recreation_server = recmodel_server.RecModel(
            os.path.join(REGRESSION_DATA, 'sample_data.csv'),
            2005, 2014, os.path.join(self.workspace_dir, 'server_cache'))

        date_range = (
            numpy.datetime64('2005-01-01'),
            numpy.datetime64('2014-12-31'))

        poly_test_queue = Queue.Queue()
        poly_test_queue.put(0)
        poly_test_queue.put('STOP')
        pud_poly_feature_queue = Queue.Queue()
        recmodel_server._calc_poly_pud(
            recreation_server.qt_pickle_filename,
            os.path.join(REGRESSION_DATA, 'test_aoi_for_subset.shp'),
            date_range, poly_test_queue, pud_poly_feature_queue)

        # assert annual average PUD is the same as regression
        self.assertEqual(
            53.3, pud_poly_feature_queue.get()[1][0])

    @scm.skip_if_data_missing(SAMPLE_DATA)
    @scm.skip_if_data_missing(REGRESSION_DATA)
    def test_local_calc_existing_cached(self):
        """Recreation local PUD calculation on existing quadtree."""
        from natcap.invest.recreation import recmodel_client
        from natcap.invest.recreation import recmodel_server

        recreation_server = recmodel_server.RecModel(
            os.path.join(REGRESSION_DATA, 'sample_data.csv'),
            2005, 2014, os.path.join(self.workspace_dir, 'server_cache'))
        recreation_server = None
        # This will not generate a new quadtree but instead load existing one
        recreation_server = recmodel_server.RecModel(
            os.path.join(REGRESSION_DATA, 'sample_data.csv'),
            2005, 2014, os.path.join(self.workspace_dir, 'server_cache'))

        date_range = (
            numpy.datetime64('2005-01-01'),
            numpy.datetime64('2014-12-31'))

        poly_test_queue = Queue.Queue()
        poly_test_queue.put(0)
        poly_test_queue.put('STOP')
        pud_poly_feature_queue = Queue.Queue()
        recmodel_server._calc_poly_pud(
            recreation_server.qt_pickle_filename,
            os.path.join(REGRESSION_DATA, 'test_aoi_for_subset.shp'),
            date_range, poly_test_queue, pud_poly_feature_queue)

        # assert annual average PUD is the same as regression
        self.assertEqual(
            53.3, pud_poly_feature_queue.get()[1][0])

    @scm.skip_if_data_missing(SAMPLE_DATA)
    @scm.skip_if_data_missing(REGRESSION_DATA)
    def test_parse_input_csv(self):
        """Recreation test parsing raw CSV."""
        from natcap.invest.recreation import recmodel_server

        csv_path = os.path.join(REGRESSION_DATA, 'sample_data.csv')
        block_offset_size_queue = Queue.Queue()
        block_offset_size_queue.put((0, 2**10))
        block_offset_size_queue.put('STOP')
        numpy_array_queue = Queue.Queue()
        recmodel_server._parse_input_csv(
            block_offset_size_queue, csv_path, numpy_array_queue)
        val = numpy_array_queue.get()
        # we know what the first date is
        self.assertEqual(val[0][0], datetime.date(2013, 3, 17))

    @scm.skip_if_data_missing(SAMPLE_DATA)
    @scm.skip_if_data_missing(REGRESSION_DATA)
    @_timeout(30.0)
    def test_regression_local_server(self):
        """Recreation base regression test on sample data on local server.

        Executes Recreation model with default data and default arguments.
        """
        from natcap.invest.recreation import recmodel_client
        from natcap.invest.recreation import recmodel_server

        natcap.invest.pygeoprocessing_0_3_3.create_directories([self.workspace_dir])
        point_data_path = os.path.join(REGRESSION_DATA, 'sample_data.csv')

        # attempt to get an open port; could result in race condition but
        # will be okay for a test. if this test ever fails because of port
        # in use, that's probably why
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(('', 0))
        port = sock.getsockname()[1]
        sock.close()
        sock = None

        server_args = {
            'hostname': 'localhost',
            'port': port,
            'raw_csv_point_data_path': point_data_path,
            'cache_workspace': self.workspace_dir,
            'min_year': 2004,
            'max_year': 2015,
            'max_points_per_node': 50,
        }

        server_thread = threading.Thread(
            target=recmodel_server.execute, args=(server_args,))
        server_thread.daemon = True
        server_thread.start()

        args = {
            'aoi_path': os.path.join(SAMPLE_DATA, 'andros_aoi.shp'),
            'cell_size': 40000.0,
            'compute_regression': True,
            'start_year': '2005',
            'end_year': '2014',
            'grid_aoi': True,
            'grid_type': 'hexagon',
            'predictor_table_path': os.path.join(
                REGRESSION_DATA, 'predictors.csv'),
            'results_suffix': u'',
            'scenario_predictor_table_path': os.path.join(
                REGRESSION_DATA, 'predictors_scenario.csv'),
            'workspace_dir': self.workspace_dir,
        }

        recmodel_client.execute(args)

        RecreationRegressionTests._assert_regression_results_eq(
            args['workspace_dir'],
            os.path.join(REGRESSION_DATA, 'file_list_base.txt'),
            os.path.join(args['workspace_dir'], 'scenario_results.shp'),
            os.path.join(REGRESSION_DATA, 'scenario_results_40000.csv'))

    def tearDown(self):
        """Delete workspace."""
        shutil.rmtree(self.workspace_dir, ignore_errors=True)


class TestLocalRecServer(unittest.TestCase):
    """Tests using a local rec server."""

    def setUp(self):
        """Setup workspace and server."""
        from natcap.invest.recreation import recmodel_server
        self.workspace_dir = tempfile.mkdtemp()
        self.recreation_server = recmodel_server.RecModel(
            os.path.join(REGRESSION_DATA, 'sample_data.csv'),
            2005, 2014, os.path.join(self.workspace_dir, 'server_cache'))

    @scm.skip_if_data_missing(SAMPLE_DATA)
    @scm.skip_if_data_missing(REGRESSION_DATA)
    def test_local_aoi(self):
        """Recreation test local AOI with local server."""
        aoi_path = os.path.join(REGRESSION_DATA, 'test_aoi_for_subset.shp')
        date_range = (
            numpy.datetime64('2005-01-01'),
            numpy.datetime64('2014-12-31'))
        out_vector_filename = os.path.join(self.workspace_dir, 'pud.shp')
        self.recreation_server._calc_aggregated_points_in_aoi(
            aoi_path, self.workspace_dir, date_range, out_vector_filename)

        output_lines = open(os.path.join(
            self.workspace_dir, 'monthly_table.csv'), 'rb').readlines()
        expected_lines = open(os.path.join(
            REGRESSION_DATA, 'expected_monthly_table_for_subset.csv'),
                              'rb').readlines()

        if output_lines != expected_lines:
            raise ValueError(
                "Output table not the same as input. "
                "Expected:\n%s\nGot:\n%s" % (expected_lines, output_lines))

    def tearDown(self):
        """Delete workspace."""
        shutil.rmtree(self.workspace_dir)


class RecreationRegressionTests(unittest.TestCase):
    """Regression tests for InVEST Seasonal Water Yield model."""

    def setUp(self):
        """Setup workspace directory."""
        # this lets us delete the workspace after its done no matter the
        # the rest result
        self.workspace_dir = tempfile.mkdtemp()

    def tearDown(self):
        """Delete workspace."""
        shutil.rmtree(self.workspace_dir)

    @scm.skip_if_data_missing(SAMPLE_DATA)
    @scm.skip_if_data_missing(REGRESSION_DATA)
    def test_data_missing_in_predictors(self):
        """Recreation raise exception if predictor data missing."""
        from natcap.invest.recreation import recmodel_client

        response_vector_path = os.path.join(SAMPLE_DATA, 'andros_aoi.shp')
        table_path = os.path.join(
            REGRESSION_DATA, 'predictors_data_missing.csv')

        with self.assertRaises(ValueError):
            recmodel_client._validate_same_projection(
                response_vector_path, table_path)

    @scm.skip_if_data_missing(SAMPLE_DATA)
    @scm.skip_if_data_missing(REGRESSION_DATA)
    def test_data_different_projection(self):
        """Recreation raise exception if data in different projection."""
        from natcap.invest.recreation import recmodel_client

        response_vector_path = os.path.join(SAMPLE_DATA, 'andros_aoi.shp')
        table_path = os.path.join(
            REGRESSION_DATA, 'predictors_wrong_projection.csv')

        with self.assertRaises(ValueError):
            recmodel_client._validate_same_projection(
                response_vector_path, table_path)

    @scm.skip_if_data_missing(SAMPLE_DATA)
    @scm.skip_if_data_missing(REGRESSION_DATA)
    def test_different_tables(self):
        """Recreation exception if scenario ids different than predictor."""
        from natcap.invest.recreation import recmodel_client

        base_table_path = os.path.join(
            REGRESSION_DATA, 'predictors_data_missing.csv')
        scenario_table_path = os.path.join(
            REGRESSION_DATA, 'predictors_wrong_projection.csv')

        with self.assertRaises(ValueError):
            recmodel_client._validate_same_ids_and_types(
                base_table_path, scenario_table_path)

    def test_delay_op(self):
        """Recreation coverage of delay op function."""
        from natcap.invest.recreation import recmodel_client

        # not much to test here but that the function is invoked
        # guarantee the time has exceeded since we can't have negative time
        last_time = -1.0
        time_delay = 1.0
        called = [False]

        def func():
            """Set `called` to True."""
            called[0] = True
        recmodel_client.delay_op(last_time, time_delay, func)
        self.assertTrue(called[0])

    @scm.skip_if_data_missing(SAMPLE_DATA)
    @scm.skip_if_data_missing(REGRESSION_DATA)
    def test_raster_sum_mean_no_nodata(self):
        """Recreation test sum/mean if raster doesn't have nodata defined."""
        from natcap.invest.recreation import recmodel_client

        # The following raster has no nodata value
        raster_path = os.path.join(REGRESSION_DATA, 'no_nodata_raster.tif')

        response_vector_path = os.path.join(SAMPLE_DATA, 'andros_aoi.shp')
        tmp_indexed_vector_path = os.path.join(
            self.workspace_dir, 'tmp_indexed_vector.shp')
        fid_values = recmodel_client._raster_sum_mean(
            response_vector_path, raster_path, tmp_indexed_vector_path)

        # These constants were calculated by hand by Rich.
        numpy.testing.assert_equal(fid_values['count'][0], 5065)
        numpy.testing.assert_equal(fid_values['sum'][0], 65377)

    @scm.skip_if_data_missing(SAMPLE_DATA)
    @scm.skip_if_data_missing(REGRESSION_DATA)
    def test_raster_sum_mean_nodata(self):
        """Recreation test sum/mean if raster is all nodata."""
        from natcap.invest.recreation import recmodel_client

        # The following raster has no nodata value
        raster_path = os.path.join(REGRESSION_DATA, 'nodata_raster.tif')

        response_vector_path = os.path.join(SAMPLE_DATA, 'andros_aoi.shp')
        tmp_indexed_vector_path = os.path.join(
            self.workspace_dir, 'tmp_indexed_vector.shp')
        fid_values = recmodel_client._raster_sum_mean(
            response_vector_path, raster_path, tmp_indexed_vector_path)

        # These constants were calculated by hand by Rich.
        numpy.testing.assert_equal(fid_values['count'][0], 0)
        numpy.testing.assert_equal(fid_values['sum'][0], 0)
        numpy.testing.assert_equal(fid_values['mean'][0], 0)

    @scm.skip_if_data_missing(SAMPLE_DATA)
    @scm.skip_if_data_missing(REGRESSION_DATA)
    @_timeout(50.0)
    def test_base_regression(self):
        """Recreation base regression test on fast sample data.

        Executes Recreation model with default data and default arguments.
        """
        from natcap.invest.recreation import recmodel_client

        args = {
            'aoi_path': os.path.join(SAMPLE_DATA, 'andros_aoi.shp'),
            'cell_size': 40000.0,
            'compute_regression': True,
            'start_year': '2005',
            'end_year': '2014',
            'grid_aoi': True,
            'grid_type': 'hexagon',
            'predictor_table_path': os.path.join(
                REGRESSION_DATA, 'predictors.csv'),
            'results_suffix': u'',
            'scenario_predictor_table_path': os.path.join(
                REGRESSION_DATA, 'predictors_scenario.csv'),
            'workspace_dir': self.workspace_dir,
        }

        recmodel_client.execute(args)
        RecreationRegressionTests._assert_regression_results_eq(
            args['workspace_dir'],
            os.path.join(REGRESSION_DATA, 'file_list_base.txt'),
            os.path.join(args['workspace_dir'], 'scenario_results.shp'),
            os.path.join(REGRESSION_DATA, 'scenario_results_40000.csv'))

    @scm.skip_if_data_missing(SAMPLE_DATA)
    @scm.skip_if_data_missing(REGRESSION_DATA)
    def test_square_grid_regression(self):
        """Recreation square grid regression test."""
        from natcap.invest.recreation import recmodel_client

        out_grid_vector_path = os.path.join(
            self.workspace_dir, 'square_grid_vector_path.shp')

        recmodel_client._grid_vector(
            os.path.join(SAMPLE_DATA, 'andros_aoi.shp'), 'square', 20000.0,
            out_grid_vector_path)

        expected_grid_vector_path = os.path.join(
            REGRESSION_DATA, 'square_grid_vector_path.shp')

        natcap.invest.pygeoprocessing_0_3_3.testing.assert_vectors_equal(
            out_grid_vector_path, expected_grid_vector_path)

    @scm.skip_if_data_missing(SAMPLE_DATA)
    @scm.skip_if_data_missing(REGRESSION_DATA)
    def test_all_metrics(self):
        """Recreation test with all but trivial predictor metrics."""
        from natcap.invest.recreation import recmodel_client
        args = {
            'aoi_path': os.path.join(
                REGRESSION_DATA, 'andros_aoi_with_extra_fields.shp'),
            'compute_regression': True,
            'start_year': '2005',
            'end_year': '2014',
            'grid_aoi': False,
            'predictor_table_path': os.path.join(
                REGRESSION_DATA, 'predictors_all.csv'),
            'scenario_predictor_table_path': os.path.join(
                REGRESSION_DATA, 'predictors_all.csv'),
            'results_suffix': u'',
            'workspace_dir': self.workspace_dir,
        }
        recmodel_client.execute(args)

        out_grid_vector_path = os.path.join(
            self.workspace_dir, 'regression_coefficients.shp')
        expected_grid_vector_path = os.path.join(
            REGRESSION_DATA, 'trivial_regression_coefficients.shp')
        natcap.invest.pygeoprocessing_0_3_3.testing.assert_vectors_equal(
            out_grid_vector_path, expected_grid_vector_path)

        out_scenario_path = os.path.join(
            self.workspace_dir, 'scenario_results.shp')
        expected_scenario_path = os.path.join(
            REGRESSION_DATA, 'trivial_scenario_results.shp')
        natcap.invest.pygeoprocessing_0_3_3.testing.assert_vectors_equal(
            out_scenario_path, expected_scenario_path)

    @scm.skip_if_data_missing(SAMPLE_DATA)
    @scm.skip_if_data_missing(REGRESSION_DATA)
    def test_hex_grid_regression(self):
        """Recreation hex grid regression test."""
        from natcap.invest.recreation import recmodel_client

        out_grid_vector_path = os.path.join(
            self.workspace_dir, 'hex_grid_vector_path.shp')

        recmodel_client._grid_vector(
            os.path.join(SAMPLE_DATA, 'andros_aoi.shp'), 'hexagon', 20000.0,
            out_grid_vector_path)

        expected_grid_vector_path = os.path.join(
            REGRESSION_DATA, 'hex_grid_vector_path.shp')

        natcap.invest.pygeoprocessing_0_3_3.testing.assert_vectors_equal(
            out_grid_vector_path, expected_grid_vector_path)

    @scm.skip_if_data_missing(SAMPLE_DATA)
    @scm.skip_if_data_missing(REGRESSION_DATA)
    def test_no_grid_regression(self):
        """Recreation base regression on ungridded AOI."""
        from natcap.invest.recreation import recmodel_client

        args = {
            'aoi_path': os.path.join(SAMPLE_DATA, 'andros_aoi.shp'),
            'compute_regression': False,
            'start_year': '2005',
            'end_year': '2014',
            'grid_aoi': False,
            'results_suffix': u'',
            'workspace_dir': self.workspace_dir,
        }

        recmodel_client.execute(args)

        output_lines = open(os.path.join(
            self.workspace_dir, 'monthly_table.csv'), 'rb').readlines()
        expected_lines = open(os.path.join(
            REGRESSION_DATA, 'expected_monthly_table_for_no_grid.csv'),
                              'rb').readlines()

        if output_lines != expected_lines:
            raise ValueError(
                "Output table not the same as input. "
                "Expected:\n%s\nGot:\n%s" % (expected_lines, output_lines))

    @scm.skip_if_data_missing(SAMPLE_DATA)
    @scm.skip_if_data_missing(REGRESSION_DATA)
    def test_predictor_id_too_long(self):
        """Recreation test ID too long raises ValueError."""
        from natcap.invest.recreation import recmodel_client

        args = {
            'aoi_path': os.path.join(SAMPLE_DATA, 'andros_aoi.shp'),
            'compute_regression': True,
            'start_year': '2005',
            'end_year': '2014',
            'grid_aoi': True,
            'grid_type': 'square',
            'cell_size': 20000,
            'predictor_table_path': os.path.join(
                REGRESSION_DATA, 'predictors_id_too_long.csv'),
            'results_suffix': u'',
            'workspace_dir': self.workspace_dir,
        }

        with self.assertRaises(ValueError):
            recmodel_client.execute(args)

    @scm.skip_if_data_missing(SAMPLE_DATA)
    @scm.skip_if_data_missing(REGRESSION_DATA)
    def test_existing_output_shapefiles(self):
        """Recreation grid test when output files need to be overwritten."""
        from natcap.invest.recreation import recmodel_client

        out_grid_vector_path = os.path.join(
            self.workspace_dir, 'hex_grid_vector_path.shp')

        recmodel_client._grid_vector(
            os.path.join(SAMPLE_DATA, 'andros_aoi.shp'), 'hexagon', 20000.0,
            out_grid_vector_path)
        # overwrite output
        recmodel_client._grid_vector(
            os.path.join(SAMPLE_DATA, 'andros_aoi.shp'), 'hexagon', 20000.0,
            out_grid_vector_path)

        expected_grid_vector_path = os.path.join(
            REGRESSION_DATA, 'hex_grid_vector_path.shp')

        natcap.invest.pygeoprocessing_0_3_3.testing.assert_vectors_equal(
            out_grid_vector_path, expected_grid_vector_path)

    @scm.skip_if_data_missing(SAMPLE_DATA)
    @scm.skip_if_data_missing(REGRESSION_DATA)
    def test_existing_regression_coef(self):
        """Recreation test regression coefficients handle existing output."""
        from natcap.invest.recreation import recmodel_client

        response_vector_path = os.path.join(
            self.workspace_dir, 'hex_grid_vector_path.shp')

        recmodel_client._grid_vector(
            os.path.join(SAMPLE_DATA, 'andros_aoi.shp'), 'hexagon', 20000.0,
            response_vector_path)

        predictor_table_path = os.path.join(REGRESSION_DATA, 'predictors.csv')

        tmp_indexed_vector_path = os.path.join(
            self.workspace_dir, 'tmp_indexed_vector.shp')
        out_coefficient_vector_path = os.path.join(
            self.workspace_dir, 'out_coefficient_vector.shp')
        out_predictor_id_list = []

        recmodel_client._build_regression_coefficients(
            response_vector_path, predictor_table_path,
            tmp_indexed_vector_path, out_coefficient_vector_path,
            out_predictor_id_list)

        # build again to test against overwriting output
        recmodel_client._build_regression_coefficients(
            response_vector_path, predictor_table_path,
            tmp_indexed_vector_path, out_coefficient_vector_path,
            out_predictor_id_list)

        expected_coeff_vector_path = os.path.join(
            REGRESSION_DATA, 'test_regression_coefficients.shp')

        natcap.invest.pygeoprocessing_0_3_3.testing.assert_vectors_equal(
            out_coefficient_vector_path, expected_coeff_vector_path)

    @scm.skip_if_data_missing(SAMPLE_DATA)
    @scm.skip_if_data_missing(REGRESSION_DATA)
    def test_absolute_regression_coef(self):
        """Recreation test validation from full path."""
        from natcap.invest.recreation import recmodel_client

        response_vector_path = os.path.join(
            self.workspace_dir, 'hex_grid_vector_path.shp')

        recmodel_client._grid_vector(
            os.path.join(SAMPLE_DATA, 'andros_aoi.shp'), 'hexagon', 20000.0,
            response_vector_path)

        predictor_table_path = os.path.join(
            self.workspace_dir, 'predictors.csv')

        # these are absolute paths for predictor data
        predictor_list = [
            ('ports', os.path.join(SAMPLE_DATA, 'dredged_ports.shp'),
             'point_count'),
            ('airdist', os.path.join(SAMPLE_DATA, 'airport.shp'),
             'point_nearest_distance'),
            ('bonefish', os.path.join(SAMPLE_DATA, 'bonefish.shp'),
             'polygon_percent_coverage'),
            ('bathy', os.path.join(SAMPLE_DATA, 'dem90m.tif'),
             'raster_mean'),
            ]

        with open(predictor_table_path, 'wb') as table_file:
            table_file.write('id,path,type\n')
            for predictor_id, path, predictor_type in predictor_list:
                table_file.write(
                    '%s,%s,%s\n' % (predictor_id, path, predictor_type))

        # The expected behavior here is that _validate_same_projection does
        # not raise a ValueError.  The try/except block makes that explicit
        # and also explictly fails the test if it does.  Note if a different
        # exception is raised the teest will Error, thus differentating
        # between a failed test and an error.
        try:
            recmodel_client._validate_same_projection(
                response_vector_path, predictor_table_path)
        except ValueError:
            self.fail(
                "_validate_same_projection raised ValueError unexpectedly!")

    @scm.skip_if_data_missing(SAMPLE_DATA)
    @scm.skip_if_data_missing(REGRESSION_DATA)
    def test_year_order(self):
        """Recreation ensure that end year < start year raise ValueError."""
        from natcap.invest.recreation import recmodel_client

        args = {
            'aoi_path': os.path.join(SAMPLE_DATA, 'andros_aoi.shp'),
            'cell_size': 7000.0,
            'compute_regression': True,
            'start_year': '2014',  # note start_year > end_year
            'end_year': '2005',
            'grid_aoi': True,
            'grid_type': 'hexagon',
            'predictor_table_path': os.path.join(
                REGRESSION_DATA, 'predictors.csv'),
            'results_suffix': u'',
            'scenario_predictor_table_path': os.path.join(
                REGRESSION_DATA, 'predictors_scenario.csv'),
            'workspace_dir': self.workspace_dir,
        }

        with self.assertRaises(ValueError):
            recmodel_client.execute(args)

    @scm.skip_if_data_missing(SAMPLE_DATA)
    @scm.skip_if_data_missing(REGRESSION_DATA)
    def test_bad_grid_type(self):
        """Recreation ensure that bad grid type raises ValueError."""
        from natcap.invest.recreation import recmodel_client

        args = {
            'aoi_path': os.path.join(SAMPLE_DATA, 'andros_aoi.shp'),
            'cell_size': 7000.0,
            'compute_regression': False,
            'start_year': '2005',
            'end_year': '2014',
            'grid_aoi': True,
            'grid_type': 'circle',  # intentionally bad gridtype
            'results_suffix': u'',
            'workspace_dir': self.workspace_dir,
        }

        with self.assertRaises(ValueError):
            recmodel_client.execute(args)

    @scm.skip_if_data_missing(SAMPLE_DATA)
    @scm.skip_if_data_missing(REGRESSION_DATA)
    def test_start_year_out_of_range(self):
        """Recreation that start_year out of range raise ValueError."""
        from natcap.invest.recreation import recmodel_client

        args = {
            'aoi_path': os.path.join(SAMPLE_DATA, 'andros_aoi.shp'),
            'cell_size': 7000.0,
            'compute_regression': True,
            'start_year': '2219',  # start year ridiculously out of range
            'end_year': '2250',
            'grid_aoi': True,
            'grid_type': 'hexagon',
            'predictor_table_path': os.path.join(
                REGRESSION_DATA, 'predictors.csv'),
            'results_suffix': u'',
            'scenario_predictor_table_path': os.path.join(
                REGRESSION_DATA, 'predictors_scenario.csv'),
            'workspace_dir': self.workspace_dir,
        }

        with self.assertRaises(ValueError):
            recmodel_client.execute(args)

    @scm.skip_if_data_missing(SAMPLE_DATA)
    @scm.skip_if_data_missing(REGRESSION_DATA)
    def test_end_year_out_of_range(self):
        """Recreation that end_year out of range raise ValueError."""
        from natcap.invest.recreation import recmodel_client

        args = {
            'aoi_path': os.path.join(SAMPLE_DATA, 'andros_aoi.shp'),
            'cell_size': 7000.0,
            'compute_regression': True,
            'start_year': '2005',
            'end_year': '2219',  # end year ridiculously out of range
            'grid_aoi': True,
            'grid_type': 'hexagon',
            'predictor_table_path': os.path.join(
                REGRESSION_DATA, 'predictors.csv'),
            'results_suffix': u'',
            'scenario_predictor_table_path': os.path.join(
                REGRESSION_DATA, 'predictors_scenario.csv'),
            'workspace_dir': self.workspace_dir,
        }

        with self.assertRaises(ValueError):
            recmodel_client.execute(args)

    @staticmethod
    def _assert_regression_results_eq(
            workspace_dir, file_list_path, result_vector_path,
            agg_results_path):
        """Test workspace against the expected list of files and results.

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
        try:
            # Test that the workspace has the same files as we expect
            _test_same_files(file_list_path, workspace_dir)

            # we expect a file called 'aggregated_results.shp'
            result_vector = ogr.Open(result_vector_path)
            result_layer = result_vector.GetLayer()

            # The tolerance of 3 digits after the decimal was determined by
            # experimentation on the application with the given range of
            # numbers.  This is an apparently reasonable approach as described
            # by ChrisF: http://stackoverflow.com/a/3281371/42897
            # and even more reading about picking numerical tolerance
            # https://randomascii.wordpress.com/2012/02/25/comparing-floating-point-numbers-2012-edition/
            tolerance_places = 3

            headers = [
                'FID', 'PUD_YR_AVG', 'PUD_JAN', 'PUD_FEB', 'PUD_MAR',
                'PUD_APR', 'PUD_MAY', 'PUD_JUN', 'PUD_JUL', 'PUD_AUG',
                'PUD_SEP', 'PUD_OCT', 'PUD_NOV', 'PUD_DEC', 'bonefish',
                'airdist', 'ports', 'bathy', 'PUD_EST']

            with open(agg_results_path, 'rb') as agg_result_file:
                header_line = agg_result_file.readline().strip()
                error_in_header = False
                for expected, actual in zip(headers, header_line.split(',')):
                    if actual != expected:
                        error_in_header = True
                if error_in_header:
                    raise ValueError(
                        "Header not as expected, got\n%s\nexpected:\n%s" % (
                            str(header_line.split(',')), headers))
                for line in agg_result_file:
                    try:
                        expected_result_lookup = dict(
                            zip(headers, [float(x) for x in line.split(',')]))
                    except ValueError:
                        LOGGER.error(line)
                        raise
                    feature = result_layer.GetFeature(
                        int(expected_result_lookup['FID']))
                    for field, value in expected_result_lookup.iteritems():
                        numpy.testing.assert_almost_equal(
                            feature.GetField(field), value,
                            decimal=tolerance_places)
                    feature = None
        finally:
            result_layer = None
            result_vector = None


def _test_same_files(base_list_path, directory_path):
    """Assert expected files are in the `directory_path`.

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
                # skip blank lines
                continue
            if not os.path.isfile(full_path):
                missing_files.append(full_path)
    if len(missing_files) > 0:
        raise AssertionError(
            "The following files were expected but not found: " +
            '\n'.join(missing_files))
