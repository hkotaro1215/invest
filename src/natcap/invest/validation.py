# TODO: add a module docstring?
import contextlib
import collections
import inspect
import logging
import pprint
import functools  # TODO: unused import

from osgeo import gdal


#: A flag to pass to the validation context manager indicating that all keys
#: should be checked.
CHECK_ALL_KEYS = None
LOGGER = logging.getLogger(__name__)


# TODO: can we talk about whether this is the way we want to do this?  I don't know much about pythonic use of context managers, but I feel like it's a good paradigm for locking and releasing resources w/r/t exceptions and other regular program flow.  But this use feels like it's using a side effect of context managers to have a side effect on a parameter list.  I'd prefer to publish the "_append_gdal_warnings" function to this module, then manually handle the gdal error handling in the validate function.  I don't mind explicitly pushing and popping the error handler, especially if it's a validate function.  OR if there's a common check like "is this a path to a raster" making an explicit function for that.  But let's talk if we haven't already...
@contextlib.contextmanager
def append_gdal_warnings(warnings_list):
    """Append GDAL warnings within this context manager to a list.

    Parameters:
        warnings_list (list): A list to which formatted GDAL warnings will
            be appended.

    Example:
        # Show an example here.  # TODO: show the example?
    """
    def _append_gdal_warnings(err_level, err_no, err_msg):
        warnings_list.append('[errno {err}] {msg}'.format(
            err=err_no, msg=err_msg.replace('\n', ' ')))

    gdal.PushErrorHandler(_append_gdal_warnings)
    yield
    gdal.PopErrorHandler()


class ValidationContext:  # TODO: add a docstring and maybe even an (Object) if it doesn't inherit from anything?
    def __init__(self, args, limit_to):
        self.args = args
        self.limit_to = limit_to
        self.warnings = []

    def warn(self, message, keys):  # TODO: add a docstring here
        if isinstance(keys, basestring):
            keys = (keys,)
        keys = tuple(sorted(keys))
        self.warnings.append((keys, message))

    def is_arg_complete(self, key, require=False): # TODO: add a docstring here
        try:
            value = self.args[key]
            if isinstance(value, basestring):
                value = value.strip()
        except KeyError:
            value = None

        if (value in ('', None) or
                self.limit_to not in (key, None)):
            if require:
                self.require(key)
            return False
        return True

    def require(self, *keys):
        for key in keys:
            if key not in self.args or self.args[key] in ('', None):
                self.warn(('Parameter is required but is missing '
                           'or has no value'), keys=(key,))


def validator(validate_func):  # TODO: I wonder if a better name of this may be execute_validator?  to indicate it's specifically for the InVEST execute API?  I otherwise like that this is enforcing the input and return values of these things so the UI doesn't have a problem later.
    """Decorator to enforce characteristics of validation inputs and outputs.

    Attributes of inputs and outputs that are enforced are:

        * ``args`` parameter to ``validate`` must be a ``dict``
        * ``limit_to`` parameter to ``validate`` must be either ``None`` or a
          string (``str`` or ``unicode``) that exists in the ``args`` dict.
        *  All keys in ``args`` must be strings
        * Decorated ``validate`` func must return a list of 2-tuples, where
          each 2-tuple conforms to these rules:

            * The first element of the 2-tuple is an iterable of strings.
              It is an error for the first element to be a string.
            * The second element of the 2-tuple is a string error message.

    Raises:
        AssertionError when an invalid format is found.

    Example:
        from natcap.invest import validation
        @validation.validator
        def validate(args, limit_to=None):
            # do your validation here
    """
    def _wrapped_validate_func(args, limit_to=None):
        # TODO: since you're doing all this checking, should it also enforce that the `validate_func` is called `validate`?  Or maybe not because the UI is the one that has to find it in the first place?
        validate_func_args = inspect.getargspec(validate_func)
        assert validate_func_args.args == ['args', 'limit_to'], (
            'validate has invalid parameters: parameters are: %s.' % (
                validate_func_args.args))

        assert isinstance(args, dict), 'args parameter must be a dictionary.'
        assert (isinstance(limit_to, type(None)) or
                isinstance(limit_to, basestring)), (
                    'limit_to parameter must be either a string key or None.')
        if limit_to is not None:
            assert limit_to in args, 'limit_to key must exist in args.'

        for key, value in args.iteritems():
            assert isinstance(key, basestring), (
                'All args keys must be strings.')

        warnings_ = validate_func(args, limit_to)
        LOGGER.debug('Validation warnings: %s',
                     pprint.pformat(warnings_))

        assert isinstance(warnings_, list), (
            'validate function must return a list of 2-tuples.')
        for keys_iterable, error_string in warnings_:
            assert (isinstance(keys_iterable, collections.Iterable) and not
                    isinstance(keys_iterable, basestring)), (
                        'Keys entry %s must be a non-string iterable' % (
                            keys_iterable))
            for key in keys_iterable:
                assert key in args, 'Key %s (from %s) must be in args.' % (
                    key, keys_iterable)
            assert isinstance(error_string, basestring), (
                'Error string must be a string, not a %s' % type(error_string))
        return warnings_

    return _wrapped_validate_func