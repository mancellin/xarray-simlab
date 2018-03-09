from inspect import isclass

import attr

from .variable import VarType


def filter_variables(process, var_type=None, intent=None, group=None,
                     func=None):
    """Filter the variables declared in a process.

    Parameters
    ----------
    process : object or class
        Process class or object.
    var_type : {'variable', 'on_demand', 'foreign', 'group'}, optional
        Return only variables of a specified type.
    intent : {'in', 'out', 'inout'}, optional
        Return only input, output or input/output variables.
    group : str, optional
        Return only variables that belong to a given group.
    func : callable, optional
        A callable that takes a variable (i.e., a :class:`attr.Attribute`
        object) as input and return True or False. Useful for more advanced
        filtering.

    Returns
    -------
    attributes : dict
        A dictionary of variable names as keys and :class:`attr.Attribute`
        objects as values.

    """
    if not isclass(process):
        process = process.__class__

    # TODO: use fields_dict instead (attrs 18.1.0)
    fields = {a.name: a for a in attr.fields(process)}

    if var_type is not None:
        fields = {k: a for k, a in fields.items()
                  if a.metadata.get('var_type') == VarType(var_type)}

    if intent is not None:
        fields = {k: a for k, a in fields.items()
                  if a.metadata.get('intent') == intent}

    if group is not None:
        fields = {k: a for k, a in fields.items()
                  if a.metadata.get('group') == group}

    if func is not None:
        fields = {k: a for k, a in fields.items() if func(a)}

    return fields


def _get_original_variable(var):
    """Return the target, original variable of a given variable and
    this process class in which the original variable is declared.

    If `var` is not a foreign variable, return itself (and None for
    the process).

    In case where the foreign variable point to another foreign
    variable (and so on...), this function follow the links until the
    original variable is found.

    """
    orig_process_cls = None
    orig_var = var

    while orig_var.metadata['var_type'] == VarType.FOREIGN:
        orig_process_cls = orig_var.metadata['other_process_cls']
        var_name = orig_var.metadata['var_name']
        orig_var = filter_variables(orig_process_cls)[var_name]

    return orig_process_cls, orig_var


def _attrify_class(cls):
    """Return a `cls` after having passed through :func:`attr.attrs`.

    This pulls out and converts `attr.ib` declared as class attributes
    into :class:`attr.Attribute` objects and it also adds
    dunder-methods such as `__init__`.

    The following instance attributes are also defined (values will be
    set later at model creation):

    __xsimlab_name__ : str
        Name given for this process in a model.
    __xsimlab_store__ : dict or object
        Simulation data store.
    __xsimlab_keys__ : dict
        Dictionary that maps variable names to their corresponding key
        (or list of keys for group variables) in the store.
        Such key consist of pairs like `('foo', 'bar')` where
        'foo' is the name of any process in the same model and 'bar' is
        the name of a variable declared in that process.
    __xsimlab_od_keys__ : dict
        Dictionary that maps variable names to the location of their target
        on-demand variable, or None if the target variable is not an on
        demand variable), or a list of locations for group variables.
        Location here consists of pairs like `(foo_obj, 'bar')`, where
        `foo_obj` is any process in the same model 'bar' is the name of a
        variable declared in that process.

    """
    def init_process(self):
        self.__xsimlab_name__ = None
        self.__xsimlab_store__ = None
        self.__xsimlab_keys__ = {}
        self.__xsimlab_od_keys__ = {}

    setattr(cls, '__attrs_post_init__', init_process)

    return attr.attrs(cls)


def _make_property_variable(var):
    """Create a property for a variable or a foreign variable.

    The property get/set functions either read/write values from/to
    the simulation data store or get (and trigger computation of) the
    value of an on-demand variable.

    The property is read-only if `var` is declared as input or if
    `var` is a foreign variable and its target (original) variable is
    an on-demand variable.

    """
    var_name = var.name

    def get_from_store(self):
        key = self.__xsimlab_keys__[var_name]
        return self.__xsimlab_store__[key]

    def get_on_demand(self):
        od_key = self.__xsimlab_od_keys__[var_name]
        return getattr(*od_key)

    def put_in_store(self, value):
        key = self.__xsimlab_keys__[var_name]
        self.__xsimlab_store__[key] = value

    orig_process_cls, orig_var = _get_original_variable(var)

    if orig_var.metadata['var_type'] == VarType.ON_DEMAND:
        if var.metadata['intent'] != 'in':
            orig_var_str = '.'.join([orig_process_cls.__name__, orig_var.name])

            raise ValueError(
                "variable {} has intent '{}' but its target "
                "variable {} is an on-demand variable (read-only)"
                .format(var_name, var.metadata['intent'], orig_var_str)
            )

        return property(fget=get_on_demand)

    elif var.metadata['intent'] == 'in':
        return property(fget=get_from_store)

    else:
        return property(fget=get_from_store, fset=put_in_store)


def _make_property_on_demand(var):
    """Create a read-only property for an on-demand variable.

    This property is a simple wrapper around the variable's compute method.

    """
    if 'compute' not in var.metadata:
        raise KeyError("no compute method found for on_demand variable "
                       "'{name}': a method decorated with '@{name}.compute' "
                       "is required in the class definition."
                       .format(name=var.name))

    get_method = var.metadata['compute']

    return property(fget=get_method)


def _make_property_group(var):
    """Create a read-only property for a group variable."""

    var_name = var.name

    def getter_store_or_on_demand(self):
        keys = self.__xsimlab_keys__[var_name]
        od_keys = self.__xsimlab_od_keys__[var_name]

        for key, od_key in zip(keys, od_keys):
            if od_key is None:
                yield self.__xsimlab_store__[key]
            else:
                yield getattr(*od_key)

    return property(fget=getter_store_or_on_demand)


class _ProcessBuilder(object):
    """Used to iteratively create a new process class.

    The original class must be already "attr-yfied", i.e., it must
    correspond to a class returned by `attr.attrs`.

    """
    _make_prop_funcs = {
        VarType.VARIABLE: _make_property_variable,
        VarType.ON_DEMAND: _make_property_on_demand,
        VarType.FOREIGN: _make_property_variable,
        VarType.GROUP: _make_property_group
    }

    def __init__(self, attr_cls):
        self._cls = attr_cls
        self._cls_dict = {}

    def add_properties(self, var_type):
        make_prop_func = self._make_prop_funcs[var_type]

        for var_name, var in filter_variables(self._cls, var_type).items():
            self._cls_dict[var_name] = make_prop_func(var)

    def render_docstrings(self):
        # self._cls_dict['__doc__'] = "Process-ified class."
        raise NotImplementedError("autodoc is not yet implemented.")

    def build_class(self):
        cls = self._cls

        # Attach properties (and docstrings)
        for name, value in self._cls_dict.items():
            setattr(cls, name, value)

        return cls


def process(maybe_cls=None, autodoc=False):
    """A class decorator that adds everything needed to use the class
    as a process.

    A process represents a logical unit in a computational model.

    A process class usually implements:

    - An interface as a set of variables defined as class attributes
      (see :func:`variable`, :func:`on_demand`, :func:`foreign` and
      :func:`group`). This decorator automatically adds properties to
      get/set values for these variables.

    - One or more methods among `initialize()`, `run_step()`,
      `finalize_step()` and `finalize()`, which are called at different
      stages of a simulation and perform some computation based on the
      variables defined in the process interface.

    - Decorated methods to compute, validate or set a default value for one or
      more variables.

    Parameters
    ----------
    maybe_cls : class, optional
        Allows to apply this decorator to a class either as `@process` or
        `@process(*args)`.
    autodoc : bool, optional
        If True, render the docstrings given as a template and fill the
        corresponding sections with metadata found in the class
        (default: False).

    """
    def wrap(cls):
        attr_cls = _attrify_class(cls)

        builder = _ProcessBuilder(attr_cls)

        for var_type in VarType:
            builder.add_properties(var_type)

        if autodoc:
            builder.render_docstrings()

        return builder.build_class()

    if maybe_cls is None:
        return wrap
    else:
        return wrap(maybe_cls)
