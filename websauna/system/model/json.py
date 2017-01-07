"""Mutable JSON data support for SQLAlchemy.

Based on the original Kotti CMS implementation:

* https://github.com/Kotti/Kotti/blob/5a33384e7b11994371c415b489edbec88ecbf044/kotti/sqla.py#L79
"""

import json

from sqlalchemy import event
from sqlalchemy.dialects.postgresql.json import JSONB
from sqlalchemy.dialects.postgresql.json import JSON
from sqlalchemy_utils.types.json import JSONType
from sqlalchemy.ext.mutable import Mutable
from sqlalchemy.sql.schema import Column


def _default(obj):
    if isinstance(obj, MutationDict):
        return obj._d
    elif isinstance(obj, MutationList):
        return obj._d


def json_serializer(d):
    """MutationDict friendly json_serializer for create_engine().

    See :py:meth:`websauna.system.model.meta.get_engine`.

    http://stackoverflow.com/a/36438671/315168
    """
    return json.dumps(d, default=_default)


class MutationDict(Mutable):
    """http://www.sqlalchemy.org/docs/orm/extensions/mutable.html
    """
    _wraps = dict

    def __init__(self, data):
        self._d = data
        super(MutationDict, self).__init__()

    @classmethod
    def coerce(cls, key, value):
        if not isinstance(value, MutationDict):
            if isinstance(value, dict):
                return cls(value)
            return Mutable.coerce(key, value)
        else:
            return value

    def __json__(self, request=None):
        return dict([(key, value.__json__(request))
                     if hasattr(value, '__json__') else (key, value)
                     for key, value in self._d.items()])


class MutationList(Mutable):
    _wraps = list

    def __init__(self, data):
        self._d = data
        super(MutationList, self).__init__()

    @classmethod
    def coerce(cls, key, value):
        if not isinstance(value, MutationList):
            if isinstance(value, list):
                return cls(value)
            return Mutable.coerce(key, value)
        else:
            return value

    def __radd__(self, other):
        return other + self._d

    def __json__(self, request=None):
        return [item.__json__(request) if hasattr(item, '__json__') else item
                for item in self._d]


def _make_mutable_method_wrapper(wrapper_class, methodname, mutates):
    def replacer(self, *args, **kwargs):
        method = getattr(self._d, methodname)
        value = method(*args, **kwargs)
        if mutates:
            self.changed()
        return value

    replacer.__name__ = methodname
    return replacer


for wrapper_class in (MutationDict, MutationList):
    for methodname, mutates in (
            ('__iter__', False),
            ('__len__', False),
            ('__eq__', False),
            ('__add__', False),
            ('__getitem__', False),
            ('__getslice__', False),
            ('__repr__', False),
            ('get', False),
            ('keys', False),

            ('__setitem__', True),
            ('__delitem__', True),
            ('__setslice__', True),
            ('__delslice__', True),
            ('append', True),
            ('clear', True),
            ('extend', True),
            ('insert', True),
            ('pop', True),
            ('setdefault', True),
            ('update', True),
            ('remove', True),
    ):
        # Only wrap methods that do exist on the wrapped type!
        if getattr(wrapper_class._wraps, methodname, False):
            setattr(
                wrapper_class, methodname,
                _make_mutable_method_wrapper(
                    wrapper_class, methodname, mutates),
            )


class NestedMixin(object):

    #: Pointer to parent NestedMutatedDict or NestedMutatedList. If the parent is Column then this is None.
    __parent__ = None

    def __init__(self, *args, **kwargs):
        self.__parent__ = kwargs.pop('__parent__', None)
        super(NestedMixin, self).__init__(*args, **kwargs)

    def __getitem__(self, key):
        value = self._d.__getitem__(key)
        return self.try_wrap(value)

    def changed(self):

        # We do not have parent dict() or list().
        # We do not have parent SQLAlchemy model.
        # This object has constructed without binding it to any parent.
        if False and self.__parent__ is None and not self._parents:
            if self:  # Check for emptiness
                assert self.__parent__, "Somehow got a modification event for a NestedMutationDict or NestedMutationList where we do not know for which parent SQLAlchemy model instances it belongs. Data is {}. Make sure you do not pass Column(default=) argument to NestedMutationList or NestedMutationList. See JSONB documentation how to properly initalized JSONB data default values.".format(self)

        if self.__parent__ is not None:
            # Direct dict or tuple parent here
            self.__parent__.changed()
        else:
            # Parent is SQLALchemy model instance, let Mutable base class take over
            super(NestedMixin, self).changed()

    def try_wrap(self, value):
        for typ, wrapper in MUTATION_WRAPPERS.items():
            if isinstance(value, typ):
                value = wrapper(value, __parent__=self)
                break
        return value

    def __eq__(self, other):
        return self._d == other

    def __str__(self):
        return "#{} {}: {}".format(id(self), self.__class__, self._d)

    def __repr__(self):
        return self.__str__()


class NestedMutationDict(NestedMixin, MutationDict):
    def setdefault(self, key, default):
        if isinstance(default, list):
            default = NestedMutationList(default, __parent__=self)
        elif isinstance(default, dict):
            default = NestedMutationDict(default, __parent__=self)
        return super(NestedMutationDict, self).setdefault(key, default)


class NestedMutationList(NestedMixin, MutationList):
    pass



MUTATION_WRAPPERS = {
    dict: NestedMutationDict,
    list: NestedMutationList,
}


def wrap_as_nested(key: str, data: object, parent: object) -> object:
    """Convert raw dict or list data to a NestedMutable version with a parent pointer set.

    After this is in-place, the object dirty state should be automatically tracked.

    :param key: Column name
    :param data: dict or list
    :param parent: SQLAlchemy model instance
    :return: data in the nested mutable wrapper
    """

    for typ, wrapper in MUTATION_WRAPPERS.items():
        if isinstance(data, typ):

            # We do data.copy() here to ensure we don't run into "Mutable defaults" Python issue
            # __parent__ = None means we dont' have parent dict or list
            data = wrapper(data.copy(), __parent__=None)  # TODO: do a deep copy?

            # SQLAlchemy model instance is directly parent of this NestedMutate
            data._parents[parent] = key
            return data

    # Unknown type :(
    return data


def is_json_like_column(c: Column) -> bool:
    """Check if the colum."""
    return isinstance(c.type, (JSONType, JSON, JSONB))



def _get_column_default(target: object, column: Column):
    if column.default is not None:
        if callable(column.default.arg):
            return column.default.arg(target)
        else:
            return column.default.arg


def with_json_columns(cls):
    """A SQLAlchemy model class decorator that ensures JSON/JSONB default values are correctly handled.

    Settings default values for JSON fields have a bunch of issues, because dict and list instances need to be wrapped in ``NestedMutationDict`` and ``NestedMutationList`` that correclty set the parent object dirty state if the container content is modified. This class decorator sets up hooks, so that default values are automatically converted to their wrapped versions.

    .. note ::

        We are only concerned about initial values. When values are loaded from the database, ``Mutable`` base class of ``NestedMutationDict`` correctly sets up the parent pointers.

    .. note ::

        This must be applid to the class directly, as SQLAlchemy does not seem ot pick it up if it's applied to an (abstract) parent class.

    This might be addressed in future SQLAlchemy / Websauna versions so that we can get rid of this ugly little decorator.
    """

    @event.listens_for(cls, "init")
    def init(target, args, kwargs):
        for c in target.__table__.columns:
            if is_json_like_column(c):
                default =_get_column_default(target, c)
                default = wrap_as_nested(c.name, default, target)
                setattr(target, c.name, default)

    return cls

