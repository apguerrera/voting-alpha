import json
import os
from base64 import b64encode
from enum import Enum
from typing import NamedTuple, Union

import eth_utils
import web3
from attrdict import AttrDict
from eth_account.messages import SignableMessage
from hexbytes import HexBytes
from lambda_decorators import async_handler, dump_json_body, load_json_body, LambdaDecorator, after
from .db import verify_session_token
from .lib import mk_logger
from .env import env
from web3.auto import w3

from .exceptions import LambdaError


log = mk_logger('members-api')


class RequestTypes(Enum):
    ESTABLISH_SESSION = "ESTABLISH_SESSION"
    PROVIDE_OTP = "PROVIDE_OTP"
    RESEND_OTP = "RESEND_OTP"


class Message(AttrDict):
    jwt: str
    request: Union[RequestTypes, str]
    payload: AttrDict


class Signature(AttrDict):
    messageHash: HexBytes
    r: bytes
    s: bytes
    v: bytes
    signature: HexBytes


def verify(condition, failure_msg=None):
    if not condition:
        error_key = b64encode(os.urandom(8))
        log.error(f"Verification failed: {failure_msg}. Error key: {error_key}")
        raise LambdaError(400, f'Verification of payload failed. Error key: {error_key}')


def verifyDictKeys(d, keys):
    for key in keys:
        if key not in d:
            return False
    return len(d) == len(keys)


def encode_sv_signed_msg(msg_bytes: bytes) -> SignableMessage:
    return SignableMessage(b'S', f'V.light.msg.v0.1:{len(msg_bytes)}:'.encode(), msg_bytes)


def encode_and_sign_msg(msg, acct) -> (SignableMessage, bytes, Signature):
    msg_str = json.dumps(msg)
    msg_bytes = eth_utils.to_bytes(text=msg_str)
    msg_to_sign = encode_sv_signed_msg(msg_bytes)
    full_msg = msg_to_sign.version + msg_to_sign.header + msg_to_sign.body
    signed = acct.sign_message(msg_to_sign)
    return msg_to_sign, full_msg, signed


def ensure_session(f):
    async def inner(event, ctx, *args, **kwargs):
        async def inner2():
            data = event['body']
            msg_encoded: str = data.msg
            signable_msg = encode_sv_signed_msg(eth_utils.to_bytes(text=msg_encoded))
            print(data.sig)
            signature_bytes = eth_utils.to_bytes(hexstr=data.sig)
            address = w3.eth.account.recover_message(signable_msg, signature=signature_bytes)
            if not eth_utils.is_address(address):
                raise LambdaError(403, "Invalid signature.")
            msg: Message = AttrDict(json.loads(msg_encoded))
            if 'request' not in msg or not (2 <= len(msg) <= 3) or len(msg.request) > 30 or type(msg.request) is not str:
                raise LambdaError(403, "Invalid message.")
            if 'jwt' not in msg and msg.request != RequestTypes.ESTABLISH_SESSION.value:
                raise LambdaError(403, "Invalid message.")
            claim = None
            if msg.request != RequestTypes.ESTABLISH_SESSION.value:
                # verify token
                claim = await verify_session_token(msg.jwt, msg.payload.email_addr, address)
                if not claim:
                    raise LambdaError(403, "Invalid jwt.")
            # todo: more?
            return await f(event, ctx, *args, msg=msg, eth_address=address, jwt_claim=claim, **kwargs)
        try:
            return await inner2()
        except Exception as e:
            import traceback
            traceback.print_exc()
            log.error(f"[ERROR]: {repr(e)} {str(e)}, {type(e)}")
            raise e
    return inner


class attrdict_body(LambdaDecorator):
    def before(self, event, context):
        event.update({'body': AttrDict(event['body'])})
        return event, context


class default_good_unless_exception(LambdaDecorator):
    def after(self, retval):
        if 'statusCode' not in retval:
            return {'body': retval, 'statusCode': 200}
        return retval

    def on_exception(self, exception):
        if type(exception) is LambdaError:
            return {'statusCode': exception.code, 'body': exception.msg}
        raise exception


@after
def cors_headers(retval: dict):
    headers = retval.setdefault('headers', {})
    headers.update({
        'access-control-allow-headers': "Content-Type,X-Amz-Date,Authorization,X-Api-Key,X-Amz-Security-Token,X-Amz-User-Agent",
        'access-control-allow-methods': "GET,POST,OPTIONS",
        'access-control-allow-origin': "*"
    })
    retval['headers'] = headers
    return retval


def all_common(f):
    return cors_headers(dump_json_body(default_good_unless_exception(f)))


# note: async_handler should be last in chain (i.e. all async functions after)
def get_common(f):
    return all_common(async_handler(f))


# note: async_handler should be last in chain (i.e. all async functions after)
def post_common(f):
    return all_common(load_json_body(attrdict_body(async_handler(f))))


