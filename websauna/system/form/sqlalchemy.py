"""SQLAlchemy integration for Colander and Deform frameworks."""
import colander
from abc import abstractmethod
from colander.compat import is_nonstr_iter
from sqlalchemy import Column
from sqlalchemy.orm import Query, Session

from websauna.compat.typing import List
from websauna.compat.typing import Tuple
from websauna.compat.typing import Union
from websauna.compat.typing import Callable
from websauna.utils.slug import slug_to_uuid, uuid_to_slug


def convert_query_to_tuples(query: Query, first_column: Union[str, Callable], second_column: Union[str, Callable]) -> List[Tuple]:
    """Convert SQLAlchemy query results to (id, name) tuples for select and checkbox widgets.

    :param first_column: Column name used to populate value in the first tuple
    :param second_column: Column name used to populate value in the second tuple
    """

    if type(first_column) == str:
        first_column_getter = lambda item: getattr(item, first_column)
    else:
        first_column_getter = first_column

    if type(second_column) == str:
        second_column_getter = lambda item: getattr(item, second_column)
    else:
        second_column_getter = second_column

    return [(first_column_getter(item), second_column_getter(item)) for item in query]


class ModelSetResultList(list):
    """Mark that the result is through SQLAlchemy query."""


class ModelSet(colander.Set):
    """Presents set of chosen SQLAlchemy models instances.

    This automatically turns SQLAlchemy objects to (id, label) tuples, so that they can be referred in various widgets (select, checkbox).
    """

    #: Point this to the model this set is supposed to query
    model = None

    #: Name of the column on the model we use to fetch objects using IN query
    match_column = None

    #: Name of the column which provides label or such for items in sequence
    label_column = None

    def serialize(self, node, appstruct):

        assert self.match_column, "match_column not configured"
        assert self.label_column, "label_column not configured"

        if appstruct is colander.null:
            return colander.null

        values = self.preprocess_appstruct_values(node, appstruct)
        return values

    def convert_to_id(self, item):
        id = getattr(item, self.match_column)
        value = getattr(item, self.label_column)
        return (id, value)

    def deserialize_set_to_models(self, node, cstruct):
        dbsession = self.get_dbsession(node)
        model = self.get_model(node)
        match_column = self.get_match_column(node, model)
        values = self.preprocess_cstruct_values(node, cstruct)
        return self.query_items(node, dbsession, model, match_column, values)

    def get_dbsession(self, node) -> Session:
        return node.bindings["request"].dbsession

    def get_model(self, node) -> type:
        """Which model we are quering."""
        return self.model

    def preprocess_cstruct_values(self, node: colander.SchemaNode, cstruct: set) -> Union[set, List]:
        """Parse incoming form values to Python objects if needed.
        """
        return cstruct

    def preprocess_appstruct_values(self, node: colander.SchemaNode, appstruct: set) -> List[str]:
        """Convert items to appstruct ids.
        """
        return [getattr(i, self.label_column) for i in appstruct]

    def get_match_column(self, node: colander.SchemaNode, model:type) -> Column:
        """Get the column we are filtering out."""
        assert self.match_column, "match_column undefined"
        return getattr(model, self.match_column)

    def query_items(self, node: colander.SchemaNode, dbsession: Session, model: type, match_column: Column, values: set) -> List[object]:
        """Query the actual model to get the concrete SQLAlchemy objects."""
        if not values:
            # Empty IN queries are not allowed
            return []
        return ModelSetResultList(dbsession.query(model).filter(match_column.in_(values)).all())

    def deserialize(self, node, cstruct):

        if cstruct is colander.null:
            return colander.null

        if not is_nonstr_iter(cstruct):
            raise colander.Invalid(
                node,
                _('${cstruct} is not iterable', mapping={'cstruct': cstruct})
            )

        return self.deserialize_set_to_models(node, cstruct)


class UUIDModelSet(ModelSet):
    """A set of SQLAlchemy objects queried by base64 encoded UUID value."""

    match_column = "uuid"

    def preprocess_cstruct_values(self, node, cstruct):
        """Parse incoming form values to Python objects if needed.
        """
        return [slug_to_uuid(v) for v in cstruct]

    def preprocess_appstruct_values(self, node: colander.SchemaNode, appstruct: set) -> List[str]:
        """Convert items to appstruct ids.
        """
        return [uuid_to_slug(getattr(i, self.match_column)) for i in appstruct]

