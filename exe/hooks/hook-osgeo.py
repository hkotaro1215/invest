import sys
if sys.platform.startswith('linux'):
    from PyInstaller.hooks.hookutils import collect_data_files
    datas = collect_data_files('osgeo')
