"""
Base classes / logic for marshalling go here.
"""
from marshmallow import Schema, fields
from testplan.common.serialization import fields as custom_fields


from testplan.common.serialization.schemas import SchemaRegistry
from .. import base

class AssertionSchemaRegistry(SchemaRegistry):

    def get_category(self, obj):
        return obj.meta_type


registry = AssertionSchemaRegistry()


class GenericEntryList(fields.Field):

    def _serialize(self, value, attr, obj):
        return [registry.serialize(entry) for entry in value]


@registry.bind_default()
class BaseSchema(Schema):
    utc_time = fields.LocalDateTime()
    machine_time = custom_fields.UTCDateTime()
    type = custom_fields.ClassName()
    meta_type = fields.String()
    description = custom_fields.Unicode()
    line_no = fields.Integer()
    category = fields.String()

    def load(self, *args, **kwargs):
        raise NotImplementedError('Only serialization is supported.')


@registry.bind(base.Group, base.Summary)
class GroupSchema(Schema):

    type = custom_fields.ClassName()
    passed = fields.Boolean()
    meta_type = fields.String()
    description = custom_fields.Unicode(allow_none=True)
    entries = GenericEntryList(allow_none=True)


@registry.bind(base.Log)
class LogSchema(BaseSchema):

    message = fields.Raw()


@registry.bind(base.MatPlot)
class MatPlotSchema(BaseSchema):

    image_file_path = fields.String()
    width = fields.Float()
    height = fields.Float()


@registry.bind(base.TableLog)
class TableLogSchema(BaseSchema):

    table = fields.List(custom_fields.NativeOrPrettyDict())
    indices = fields.List(fields.Integer(), allow_none=True)
    display_index = fields.Boolean()
    columns = fields.List(fields.String(), allow_none=False)


@registry.bind(
    base.DictLog,
    base.FixLog
)
class DictLogSchema(BaseSchema):

    flattened_dict = fields.Raw()
