"""
Functions to assist with remote logging of InVEST usage.
"""

import os
import datetime
import sys
import tempfile
import traceback
import logging
import sqlite3
import zipfile
import glob

import Pyro4
from osgeo import ogr
from osgeo import osr

logging.basicConfig(format='%(asctime)s %(name)-20s %(levelname)-8s \
%(message)s', level=logging.DEBUG, datefmt='%m/%d/%Y %H:%M:%S ')

LOGGER = logging.getLogger('natcap.invest.remote_logging')

Pyro4.config.SERIALIZER = 'marshal' #lets us pass null bytes in strings

class LoggingServer(object):
    """Pyro4 RPC server for logging invest runs and getting database
    summaries"""

    _FIELD_NAMES = [
        'model_name',
        'invest_release',
        'time',
        'ip_address',
        'bounding_box_union',
        'bounding_box_intersection',
        'node_hash',
        'system_full_platform_string',
        'system_preferred_encoding',
        'system_default_language',
        ]
    _TABLE_NAME = 'natcap_model_log_table'
    def __init__(self, database_filepath):
        """Launches a new logger and initalizes an sqlite database at
        `database_filepath` if not previously defined.

        Args:
            database_filepath (string): path to a database filepath, will create
                the file and directory path if it doesn't exists

        Returns:
            None."""

        self.database_filepath = database_filepath
        # make the directory if it doesn't exist and isn't the current directory
        filepath_directory = os.path.dirname(self.database_filepath)
        if filepath_directory != '' and not os.path.exists(filepath_directory):
            os.mkdir(os.path.dirname(self.database_filepath))
        db_connection = sqlite3.connect(self.database_filepath)
        db_cursor = db_connection.cursor()
        db_cursor.execute(
            'CREATE TABLE IF NOT EXISTS %s (%s)' % (
                self._TABLE_NAME,
                ','.join([
                    '%s text' % field_id for field_id in self._FIELD_NAMES])))
        db_connection.commit()
        db_connection.close()


    def log_invest_run(self, data):
        """Logs an invest run to the sqlite database found at database_filepath
        Any self._FIELD_NAMES that match data keys will be inserted into the
        database.

        Args:
            data (dict): a flat dictionary with data about the InVEST run where
                the keys of the dictionary are at least self._FIELD_NAMES

                TODO: list keys here

        Returns:
            None."""

        try:
            # Add info about the client's IP
            data_copy = data.copy()
            data_copy['ip_address'] = (
                Pyro4.current_context.client.sock.getpeername()[0])
            data_copy['time'] = datetime.datetime.now().isoformat(' ')

            # Get data into the same order as the field names
            ordered_data = [
                data_copy[field_id] for field_id in self._FIELD_NAMES]
            # get as many '?'s as there are fields for the insert command
            position_format = ','.join(['?'] * len(self._FIELD_NAMES))

            insert_command = (
                'INSERT OR REPLACE INTO natcap_model_log_table'
                '(%s) VALUES (%s)' % (
                    ','.join(self._FIELD_NAMES), position_format))

            db_connection = sqlite3.connect(self.database_filepath)
            db_cursor = db_connection.cursor()
            # pass in ordered_data to the command
            db_cursor.execute(insert_command, ordered_data)
            db_connection.commit()
            db_connection.close()
        except:
            # print something locally for our log and raise back to client
            traceback.print_exc()
            raise
        extra_fields = set(data_copy).difference(self._FIELD_NAMES)
        if len(extra_fields) > 0:
            LOGGER.warn(
                "Warning there were extra fields %s passed to logger. "
                " Expected: %s Received: %s", sorted(extra_fields),
                sorted(self._FIELD_NAMES), sorted(data_copy))

    def get_model_run_summary_as_shapefile(self):
        """Builds a shapefile that has a unique polygon for each unique bounding
        box, a field with the number of run counts, and a field with the model
        name

        Returns:
            a compressed zipfile of shapefile as a binary string"""

        #ZIP and stream the result back
        workspace_dir = tempfile.mkdtemp()
        model_run_summary_shapefile_name = os.path.join(
            workspace_dir, 'model_run_summary.shp')
        driver = ogr.GetDriverByName('ESRI Shapefile')

        if os.path.isfile(model_run_summary_shapefile_name):
            os.remove(model_run_summary_shapefile_name)
        datasource = driver.CreateDataSource(model_run_summary_shapefile_name)

        lat_lng_ref = osr.SpatialReference()
        lat_lng_ref.ImportFromEPSG(4326) #EPSG 4326 is lat/lng

        polygon_layer = datasource.CreateLayer(
            'model_run_summary', lat_lng_ref, ogr.wkbPolygon)

        polygon_layer.CreateField(ogr.FieldDefn('n_runs', ogr.OFTInteger))
        polygon_layer.CreateField(ogr.FieldDefn('model', ogr.OFTString))

        db_connection = sqlite3.connect(self.database_filepath)
        db_cursor = db_connection.cursor()
        db_cursor.execute(
            """SELECT model_name, bounding_box_intersection, count(model_name)
FROM natcap_model_log_table WHERE bounding_box_intersection not LIKE 'None'
GROUP BY model_name, bounding_box_intersection;""")

        for line in db_cursor:
            try:
                model_name, bounding_box_string, n_runs = line
                n_runs = int(n_runs)
                bounding_box = list(
                    [float(x) for x in bounding_box_string[1:-1].split(',')])
                ring = ogr.Geometry(ogr.wkbLinearRing)
                ring.AddPoint(bounding_box[0], bounding_box[3])
                ring.AddPoint(bounding_box[0], bounding_box[1])
                ring.AddPoint(bounding_box[2], bounding_box[1])
                ring.AddPoint(bounding_box[2], bounding_box[3])
                ring.AddPoint(bounding_box[0], bounding_box[3])
                poly = ogr.Geometry(ogr.wkbPolygon)
                poly.AddGeometry(ring)
                feature = ogr.Feature(polygon_layer.GetLayerDefn())
                feature.SetGeometry(poly)
                feature.SetField('n_runs', n_runs)
                feature.SetField('model', str(model_name))
                polygon_layer.CreateFeature(feature)
            except:
                LOGGER.warn('unable to create a bounding box for %s', line)

        datasource.SyncToDisk()

        model_run_summary_zip_name = os.path.join(
            workspace_dir, 'model_run_summary.zip')
        with zipfile.ZipFile(model_run_summary_zip_name, 'w') as myzip:
            for filename in glob.glob(
                    os.path.splitext(
                        model_run_summary_shapefile_name)[0] + '.*'):
                myzip.write(filename, os.path.basename(filename))
        model_run_summary_binary = open(model_run_summary_zip_name, 'rb').read()
        return model_run_summary_binary


def launch_logging_server(database_filepath, hostname, port):
    """Function to start a remote procedure call server

    Args:
        database_filepath (string): local filepath to the sqlite database
        hostname (string): network interface to bind to
        port (int): TCP port to bind to

    Returns:
        never"""

    daemon = Pyro4.Daemon(hostname, port)
    uri = daemon.register(
        LoggingServer(database_filepath), 'natcap.invest.remote_logging')
    LOGGER.info("natcap.invest.recreation ready. Object uri = %s", uri)
    daemon.requestLoop()


if __name__ == '__main__':
    DATABASE_FILEPATH = sys.argv[1]
    HOSTNAME = sys.argv[2]
    PORT = int(sys.argv[3])
    launch_logging_server(DATABASE_FILEPATH, HOSTNAME, PORT)
