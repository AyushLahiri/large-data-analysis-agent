"""Readable Python imports for the numbered phase directories."""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys

for _alias, _folder in {'main': '1_main', 'managers': '2_managers', 'sql_assets': '3_manageSQLassets'}.items():
    _name = __name__ + '.' + _alias
    _path = Path(__file__).parent / _folder
    _spec = spec_from_file_location(_name, _path / '__init__.py', submodule_search_locations=[str(_path)])
    _module = module_from_spec(_spec)
    sys.modules[_name] = _module
    _spec.loader.exec_module(_module)
    globals()[_alias] = _module
del _alias, _folder, _name, _path, _spec, _module
