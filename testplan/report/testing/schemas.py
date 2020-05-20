"""Schema classes for test Reports."""

import functools
import json
import copy
import random
import six
from six.moves import range

# pylint: disable=no-name-in-module,import-error
if six.PY2:
    from collections import Mapping, Sequence, Iterable
    ByteString = (bytearray,)
else:
    from collections.abc import Mapping, Iterable, ByteString
# pylint: enable=no-name-in-module,import-error

from marshmallow import Schema, fields, post_load

from testplan.common.serialization.schemas import load_tree_data
from testplan.common.report.schemas import ReportSchema
from testplan.common.serialization import fields as custom_fields

from testplan.common.utils import timing

from .base import TestCaseReport, TestGroupReport, TestReport

__all__ = ["TestCaseReportSchema", "TestGroupReportSchema", "TestReportSchema"]


class IntervalSchema(Schema):
    """Schema for ``timer.Interval``"""

    start = custom_fields.UTCDateTime()
    end = custom_fields.UTCDateTime(allow_none=True)

    @post_load
    def make_interval(self, data):  # pylint: disable=no-self-use
        """Create an Interal object."""
        return timing.Interval(**data)


class TagField(fields.Field):
    """Field for serializing tag data, which is a ``dict`` of ``set``."""

    def _serialize(self, value, attr, obj):
        return {
            tag_name: list(tag_values)
            for tag_name, tag_values in value.items()
        }

    def _deserialize(self, value, attr, data):
        return {
            tag_name: set(tag_values) for tag_name, tag_values in value.items()
        }


class TimerField(fields.Field):
    """
    Field for serializing ``timer.Timer`` objects, which is a ``dict``
    of ``timer.Interval``.
    """

    def _serialize(self, value, attr, obj):
        return {
            k: IntervalSchema(strict=True).dump(v).data
            for k, v in value.items()
        }

    def _deserialize(self, value, attr, data):
        return timing.Timer(
            {
                k: IntervalSchema(strict=True).load(v).data
                for k, v in value.items()
            }
        )


class EntriesField(fields.Field):

    _BYTES_KEY = "_BYTES_KEY"
    _json_dumps_exceptions = (UnicodeDecodeError, TypeError, ValueError)

    def _binary_serialize(self, bytes_obj):
        hex_list = []
        for b in bytes_obj:
            hex_b = "0x{}".format(hex(b)[2:].upper().zfill(2))
            hex_list.append(hex_b)
        return {self._BYTES_KEY: hex_list}

    def _binary_deserialize(self, hex_list):
        ba = bytearray()
        for hx in hex_list:
            ba.append(int(hx, 16))
        return bytes(ba)

    def _serialize(self, value, attr, obj):
        try:
            json.dumps(value)
            return value
        except self._json_dumps_exceptions:

            if isinstance(value, ByteString):
                return self._binary_serialize(value)

            if isinstance(value, Mapping):
                value_mutable = {}
                for key, val in six.iteritems(value):
                    if isinstance(key, ByteString):
                        raise TypeError(
                            'Byte-like keys are not allowed in Mapping types'
                        )
                    value_mutable[key] = self._serialize(val, None, None)
                # assumes `value.__class__(<mutable mapping>)` works
                return value.__class__(value_mutable)

            if isinstance(value, Iterable):
                value_mutable = []
                for i, el in enumerate(value):
                    value_mutable.append(self._serialize(el, None, None))
                # assumes `value.__class__(<mutable sequence>)` works
                return value.__class__(value_mutable)

            return str(value)  # our "give up" serialization

    def _deserialize(self, value, attr, obj):
        # Note that deserialization from the above tested-for types is lossy
        # since all `Mapping` types are deserialized as `dict` and all
        # non-`Mapping` `Iterable` types are deserialized as `list`.
        # An improvement would be to note the data type during serialization.
        if isinstance(value, dict) and self._BYTES_KEY in value:
            return self._binary_deserialize(value[self._BYTES_KEY])
        return value


class TestCaseReportSchema(ReportSchema):
    """Schema for ``testing.TestCaseReport``"""

    source_class = TestCaseReport

    status_override = fields.String(allow_none=True)

    entries = fields.List(EntriesField())

    status = fields.String(dump_only=True)
    runtime_status = fields.String(dump_only=True)
    counter = fields.Dict(dump_only=True)
    suite_related = fields.Bool()
    timer = TimerField(required=True)
    tags = TagField()
    category = fields.String(dump_only=True)

    status_reason = fields.String(allow_none=True)

    @post_load
    def make_report(self, data):
        """
        Create the report object, assign ``timer`` &
        ``status_override`` attributes explicitly
        """
        status_override = data.pop("status_override", None)
        timer = data.pop("timer")

        # We can discard the type field since we know what kind of report we
        # are making.
        if "type" in data:
            data.pop("type")

        rep = super(TestCaseReportSchema, self).make_report(data)
        rep.status_override = status_override
        rep.timer = timer
        return rep


class TestGroupReportSchema(TestCaseReportSchema):
    """
    Schema for ``testing.TestGroupReportSchema``, supports tree serialization.
    """

    source_class = TestGroupReport
    # category = fields.String()
    part = fields.List(fields.Integer, allow_none=True)
    extra_attributes = fields.Dict(allow_none=True)
    fix_spec_path = fields.String(allow_none=True)
    env_status = fields.String(allow_none=True)

    # status_reason = fields.String(allow_none=True)
    # runtime_status = fields.String(dump_only=True)
    # counter = fields.Dict(dump_only=True)

    entries = custom_fields.GenericNested(
        schema_context={
            TestCaseReport: TestCaseReportSchema,
            TestGroupReport: "self",
        },
        many=True,
    )

    @post_load
    def make_report(self, data):
        """
        Propagate tag indices after deserialization
        """
        rep = super(TestGroupReportSchema, self).make_report(data)
        rep.propagate_tag_indices()
        return rep


class TestReportSchema(Schema):
    """Schema for test report root, ``testing.TestReport``."""

    timer = TimerField()
    name = fields.String()
    uid = fields.String()
    meta = fields.Dict()

    status = fields.String(dump_only=True)
    runtime_status = fields.String(dump_only=True)
    tags_index = TagField(dump_only=True)
    status_override = fields.String(allow_none=True)
    information = fields.List(fields.List(fields.String()))
    counter = fields.Dict(dump_only=True)

    attachments = fields.Dict()

    entries = custom_fields.GenericNested(
        schema_context={TestGroupReport: TestGroupReportSchema}, many=True
    )
    category = fields.String(dump_only=True)

    @post_load
    def make_test_report(self, data):  # pylint: disable=no-self-use
        """Create report object & deserialize sub trees."""
        load_tree = functools.partial(
            load_tree_data,
            node_schema=TestGroupReportSchema,
            leaf_schema=TestCaseReportSchema,
        )

        entry_data = data.pop("entries")
        status_override = data.pop("status_override")
        timer = data.pop("timer")

        test_plan_report = TestReport(**data)
        test_plan_report.entries = [load_tree(c_data) for c_data in entry_data]
        test_plan_report.propagate_tag_indices()

        test_plan_report.status_override = status_override
        test_plan_report.timer = timer
        return test_plan_report


class ShallowTestReportSchema(Schema):
    """Schema for shallow serialization of ``TestReport``."""

    name = fields.String(required=True)
    uid = fields.String(required=True)
    timer = TimerField(required=True)
    meta = fields.Dict()
    status = fields.String(dump_only=True)
    runtime_status = fields.String(dump_only=True)
    tags_index = TagField(dump_only=True)
    status_override = fields.String(allow_none=True)
    counter = fields.Dict(dump_only=True)
    attachments = fields.Dict()
    entry_uids = fields.List(fields.Str(), dump_only=True)
    parent_uids = fields.List(fields.Str())
    hash = fields.Integer(dump_only=True)
    category = fields.String(dump_only=True)

    @post_load
    def make_test_report(self, data):
        status_override = data.pop("status_override", None)
        timer = data.pop("timer")

        test_plan_report = TestReport(**data)
        test_plan_report.propagate_tag_indices()

        test_plan_report.status_override = status_override
        test_plan_report.timer = timer
        return test_plan_report


class ShallowTestGroupReportSchema(Schema):
    """
    Schema for shallow serialization of ``TestGroupReport``.
    """

    name = fields.String(required=True)
    uid = fields.String(required=True)
    timer = TimerField(required=True)
    description = fields.String(allow_none=True)
    part = fields.List(fields.Integer, allow_none=True)
    fix_spec_path = fields.String(allow_none=True)
    status_override = fields.String(allow_none=True)
    status = fields.String(dump_only=True)
    runtime_status = fields.String(dump_only=True)
    counter = fields.Dict(dump_only=True)
    suite_related = fields.Bool()
    tags = TagField()
    entry_uids = fields.List(fields.Str(), dump_only=True)
    parent_uids = fields.List(fields.Str())
    hash = fields.Integer(dump_only=True)
    category = fields.String()
    env_status = fields.String(allow_none=True)

    @post_load
    def make_testgroup_report(self, data):
        status_override = data.pop("status_override", None)
        timer = data.pop("timer")

        group_report = TestGroupReport(**data)
        group_report.status_override = status_override
        group_report.timer = timer
        group_report.propagate_tag_indices()

        return group_report
