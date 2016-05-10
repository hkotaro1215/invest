import sys
import os
import platform
import time
import json
from optparse import OptionParser
import logging
import multiprocessing

from PyQt4 import QtGui, QtCore

import base_widgets
import executor

CMD_FOLDER = '.'

# Set up logging for the modelUI
import natcap.invest
import natcap.invest.iui
LOGGER = natcap.invest.iui.get_ui_logger('modelUI')

class ModelUIRegistrar(base_widgets.ElementRegistrar):
    def __init__(self, root_ptr):
        super(ModelUIRegistrar, self).__init__(root_ptr)

        changes = {'file': base_widgets.FileEntry,
                   'folder': base_widgets.FileEntry,
                   'text': base_widgets.YearEntry
                    }

        self.update_map(changes)

class ModelUI(base_widgets.ExecRoot):
    def __init__(self, uri, main_window):
        """Constructor for the DynamicUI class, a subclass of DynamicGroup.
            DynamicUI loads all setting from a JSON object at the provided URI
            and recursively creates all elements.

            uri - the string URI to the JSON configuration file.
            main_window - an instance of base_widgets.MainWindow

            returns an instance of DynamicUI."""

        #the top buttonbox needs to be initialized before super() is called,
        #since super() also creates all elements based on the user's JSON config
        #this is important because QtGui displays elements in the order in which
        #they are added.
        layout = QtGui.QVBoxLayout()

        self.links = QtGui.QLabel()
        self.links.setOpenExternalLinks(True)
        self.links.setAlignment(QtCore.Qt.AlignRight)
        layout.addWidget(self.links)

        registrar = ModelUIRegistrar(self)
        self.okpressed = False

        base_widgets.ExecRoot.__init__(self, uri, layout, registrar, main_window)

        self.layout().setSizeConstraint(QtGui.QLayout.SetMinimumSize)

        try:
            title = self.attributes['label']
        except KeyError:
            title = 'InVEST'
        window_title = "%s" % (title)
        main_window.setWindowTitle(window_title)

        self.addLinks()

    def addLinks(self):
        links = []
        try:
            architecture = platform.architecture()[0]
            links.append('InVEST Version %s (%s)' % (natcap.invest.__version__,
                architecture))
        except AttributeError:
            links.append('InVEST Version UNKNOWN')

        try:
            doc_uri = 'file:///' + os.path.abspath(self.attributes['localDocURI'])
            links.append('<a href=\"%s\">Model documentation</a>' % doc_uri)
        except KeyError:
            # Thrown if attributes['localDocURI'] is not present
            print 'Attribute localDocURI not found for this model; skipping.'

        feedback_uri = 'http://forums.naturalcapitalproject.org/'
        links.append('<a href=\"%s\">Report an issue</a>' % feedback_uri)

        self.links.setText(' | '.join(links))

    def queueOperations(self):
        modelArgs = self.assembleOutputDict()
        self.operationDialog.exec_controller.add_operation('model',
                                                   modelArgs,
                                                   self.attributes['targetScript'])


def getFlatDefaultArgumentsDictionary(args):
    flatDict = {}
    if isinstance(args, dict):
        if 'args_id' in args and 'defaultValue' in args:
            flatDict[args['args_id']] = args['defaultValue']
        if 'elements' in args:
            flatDict.update(getFlatDefaultArgumentsDictionary(args['elements']))
    elif isinstance(args, list):
        for element in args:
            flatDict.update(getFlatDefaultArgumentsDictionary(element))

    return flatDict


def main(uri, use_gui=True):
    multiprocessing.freeze_support()
    # get the existing QApplication instance, or creating a new one if
    # necessary.
    app = QtGui.QApplication.instance()
    if app is None:
        app = QtGui.QApplication(sys.argv)

#    validate(json_args)

    # Check to see if the URI exists in the current directory.  If not, assume
    # it exists in the directory where this module exists.
    if not os.path.exists(uri):
        file_path = os.path.dirname(os.path.abspath(__file__))
        uri = os.path.join(file_path, os.path.basename(uri))

        # If the URI still doesn't exist, raise a helpful exception.
        if not os.path.exists(uri):
            raise Exception('Can\'t find the file %s.'%uri)

    window = base_widgets.MainWindow(ModelUI, uri)
    window.ui.resetParametersToDefaults()
    window.show()
    if use_gui:
        result = app.exec_()
    else:
        from PyQt4.QtTest import QTest
        window.ui.runButton.click()
        while not window.ui.operationDialog.backButton.isEnabled():
            QTest.qWait(50)

        thread_failed = False
        if window.ui.operationDialog.exec_controller.thread_failed:
            thread_failed = True

        window.ui.operationDialog.closeWindow()

        # exit not-so-peacefully if we're running in test mode AND the thread
        # failed.  I'm assuming this is not an oft-used option!
        if thread_failed:
            sys.exit(1)

if __name__ == '__main__':
    #Optparse module is deprecated since python 2.7.  Using here since OSGeo4W
    #is version 2.5.
    parser = OptionParser()
    parser.add_option('-t', '--test', action='store_false', default=True, dest='test')
    parser.add_option('-d', '--debug', action='store_true', default=False, dest='debug')
    (options, uri) = parser.parse_args(sys.argv)
    print uri

    if options.debug == True:
        level = logging.NOTSET
    else:
        level = logging.WARNING
    LOGGER.setLevel(level)
    LOGGER.debug('Level set to %s, option_passed = %s', level, options.debug)

    main(uri[1], options.test)

