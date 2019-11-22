import json
import datetime
import os
from enum import Enum
from typing import TypeVar, Literal, Union

import toolz
from pymonad import Nothing, Maybe, Just
from pynamodb.indexes import GlobalSecondaryIndex, AllProjection
from pynamodb.models import Model
from pynamodb.attributes import UnicodeAttribute, BooleanAttribute, UTCDateTimeAttribute, ListAttribute, MapAttribute, \
    Attribute, BinaryAttribute
from pynamodb.constants import STRING
import pynamodb.exceptions as pddb_ex
from attrdict import AttrDict


T = TypeVar('T')
env = AttrDict(os.environ)


def gen_table_name(name):
    return f"{env.pNamePrefix}-{name}"


class EnumAttribute(Attribute):
    attr_type = STRING

    def __init__(self, enum_cls, *args, **kwargs):
        self.enum = enum_cls
        self.enum_values = list([e.value for e in enum_cls])
        super().__init__(*args, **kwargs)

    def serialize(self, value):
        if value not in self.enum:
            raise pddb_ex.PutError(f"Invalid value in EnumAttribute: {value}. Allowed values: {self.enum_values}")
        return str(value)

    def deserialize(self, value):
        if value not in self.enum_values:
            raise AttributeError(f"Invalid value for enum: {value}. Expected: {self.enum_values}")
        return self.enum(value)


class ModelEncoder(json.JSONEncoder):
    def default(self, obj):
        if hasattr(obj, 'attribute_values'):
            return obj.attribute_values
        elif isinstance(obj, datetime.datetime):
            return obj.isoformat()
        return json.JSONEncoder.default(self, obj)


class BaseModel(Model):
    @classmethod
    def get_or(cls, *args, default=None):
        if default is None:
            raise Exception('must provide a default')
        try:
            return super().get(*args)
        except super().DoesNotExist as e:
            return default

    @classmethod
    def get_maybe(cls, *args):
        try:
            return Just(super().get(*args))
        except super().DoesNotExist as e:
            return Nothing

    def to_json(self):
        return json.dumps(self, cls=ModelEncoder)

    def to_python(self):
        return json.loads(self.to_json())

    def strip_private(self):
        return self.to_python()


class UidPrivate(BaseModel):
    def strip_private(self) -> dict:
        return {k: v for k, v in super().strip_private() if k != 'uid'}


class Ix(GlobalSecondaryIndex):
    class Meta:
        projection = AllProjection()


'''
establish session
-> GET lambda/session (JWT encoded)

client
- sends {msg:<json stringified>,sig:ethAccountSig}

server
- auths by doing sig reverse, tracks session by address (todo: future: session token too)

JWT has a token that lets the user de-anon themselves in logs
Can we store users public key and use to encrypt? yes
Encrypt logs with users voting key

-> POST address + email + secret
** send email to user with OTP
<- JWT
-> POST jwt + address + secret + OTP
** mark email as "in-progress"
-> POST jwt + address + encrypted_payload
** send email backup, mark awaiting_confirmation
-> POST jwt + address + confirm(hash(encrypted_payload))
** publish address to smart contract
<- txid, maybe local proof? client should validate tx

-> POST proxy vote payload + JWT
** validates payload and submits
'''


class SessionState(Enum):
    s010_SENT_OTP_EMAIL = "_010_SENT_OTP_EMAIL"
    s020_CONFIRMED_OTP = "_020_CONFIRMED_OTP"
    s030_SENT_BACKUP_EMAIL = "_030_SENT_BACKUP_EMAIL"
    s040_MADE_ID_CONF_TX = "_040_MADE_ID_CONF_TX"


class OtpState(MapAttribute):
    not_valid_after: UTCDateTimeAttribute
    not_valid_before: UTCDateTimeAttribute
    otp_hash: BinaryAttribute()


class SessionModel(BaseModel):
    class Meta:
        table_name = gen_table_name('session-db')
    session_id = UnicodeAttribute(hash_key=True)
    session_anon_id = UnicodeAttribute()
    state = EnumAttribute(SessionState)
    otp = OtpState()


class QuestionModel(UidPrivate):
    class Meta:
        table_name = gen_table_name("qanda-questions-ddb")
        region = env.AWS_REGION

    qid = UnicodeAttribute(hash_key=True)
    uid = UnicodeAttribute()
    display_name = UnicodeAttribute()
    is_anon = BooleanAttribute()
    question = UnicodeAttribute()
    title = UnicodeAttribute()
    prev_q = UnicodeAttribute(null=True)
    next_q = UnicodeAttribute(null=True)
    ts = UTCDateTimeAttribute()


class UserQuestionLogEntry(MapAttribute):
    ts = UTCDateTimeAttribute()
    qid = UnicodeAttribute()


class UserQuestionsModel(BaseModel):
    class Meta:
        table_name = gen_table_name("qanda-user-qs-ddb")
        region = env.AWS_REGION

    uid = UnicodeAttribute(hash_key=True)
    qs = ListAttribute(of=UserQuestionLogEntry, default=list)


class GenericPointer(MapAttribute):
    ts = UTCDateTimeAttribute()
    id = UnicodeAttribute()


class ReplyIdsByQid(BaseModel):
    class Meta:
        table_name = gen_table_name("qanda-reply-ids-ddb")
        region = env.AWS_REGION

    qid = UnicodeAttribute(hash_key=True)
    rids = ListAttribute(of=GenericPointer, default=list)


class ReplyIdsByUid(BaseModel):
    class Meta:
        table_name = gen_table_name("qanda-reply-ids-by-uid-ddb")
        region = env.AWS_REGION

    uid = UnicodeAttribute(hash_key=True)
    rids = ListAttribute(of=GenericPointer, default=list)


class Reply(UidPrivate):
    class Meta:
        table_name = gen_table_name("qanda-replies-ddb")
        region = env.AWS_REGION

    rid = UnicodeAttribute(hash_key=True)
    qid = UnicodeAttribute()
    uid = UnicodeAttribute()
    body = UnicodeAttribute()
    ts = UTCDateTimeAttribute()
    parent_rid = UnicodeAttribute(null=True)
    child_rids = ListAttribute(of=GenericPointer, default=list)
    is_staff = BooleanAttribute()
    display_name = UnicodeAttribute()
