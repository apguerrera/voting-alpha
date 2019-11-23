import asyncio
import base64
import datetime
import json
import os

import jwt
from eth_account.messages import encode_defunct, SignableMessage
import eth_utils
from api.exceptions import LambdaError
from attrdict import AttrDict
from hexbytes import HexBytes
from pymonad.Maybe import Nothing
from pynamodb.attributes import BooleanAttribute
from utils import can_base64_decode
from .common.lib import _hash
from web3.auto import w3
from .send_mail import send_email, format_backup
from .db import new_session, gen_otp_hash, gen_otp_and_otp_hash, gen_session_anon_id, bs_to_base64, hash_up
from .models import SessionModel, OtpState, SessionState, TimestampMap
from .handler_utils import post_common, Message, RequestTypes, verify, ensure_session, encode_sv_signed_msg, \
    verifyDictKeys, encode_and_sign_msg
from .lib import mk_logger, now
from .env import get_env

log = mk_logger('members-onboard')


@post_common
@ensure_session
async def message_handler(event, ctx, msg: Message, eth_address, jwt_claim, session):
    _h = {
        RequestTypes.ESTABLISH_SESSION.value: establish_session,
        RequestTypes.PROVIDE_OTP.value: provide_otp,
        RequestTypes.RESEND_OTP.value: resend_otp,
        RequestTypes.PROVIDE_BACKUP.value: send_backup_email,
        RequestTypes.FINAL_CONFIRM.value: confirm_and_finalize_onboarding,
    }
    return await _h[msg.request](event, ctx, msg, eth_address, jwt_claim, session)


async def establish_session(event, ctx, msg, eth_address, jwt_claim, session, *args, **kwargs):
    verify(verifyDictKeys(msg.payload, ['email_addr', 'address']), 'establish_session: verify session payload')
    verify(eth_address == msg.payload.address, 'verify ethereum addresses match')

    sess = await new_session(msg.payload.email_addr, eth_address)
    jwt_token, session = sess
    claim = AttrDict(jwt.decode(jwt_token, algorithms=['HS256'], verify=False))
    otp, otp_hash = gen_otp_and_otp_hash(msg.payload.email_addr, eth_address, claim.token)

    session.update([
        SessionModel.otp.set(OtpState(
            not_valid_before=datetime.datetime.now(),
            not_valid_after=datetime.datetime.now() + datetime.timedelta(minutes=15),
            otp_hash=otp_hash,
            succeeded=False,
        ))
    ])
    resp = send_email(source=get_env('pAdminEmail', 'test@api.secure.vote'), to_addrs=[msg.payload.email_addr], subject="Test Email", body_txt=f"""
Your OTP is:

{otp}
""")
    if not resp:
        raise LambdaError(500, "Failed to send email OTP email")
    else:
        session.update([
            SessionModel.otp.emails_sent_at.set(SessionModel.otp.emails_sent_at.prepend([TimestampMap()])),
            SessionModel.state.set(SessionState.s010_SENT_OTP_EMAIL)
        ])
        log.error("** ** ** OTP UNSAFE RETURNED IN PAYLOAD ** ** **")
        return {'result': 'success', 'jwt': jwt_token, 'otp_unsafe_todo_remove': otp}


async def resend_otp(event, ctx, msg, eth_address, jwt_claim, session, *args, **kwargs):
    raise LambdaError(555, 'unimplemented')


async def provide_otp(event, ctx, msg, eth_address, jwt_claim, session, *args, **kwargs):
    verify(verifyDictKeys(msg.payload, ['email_addr', 'otp']), 'payload keys')
    verify(session.state == SessionState.s010_SENT_OTP_EMAIL, 'session state is as expected')

    if session.otp.incorrect_attempts >= 10:
        raise LambdaError(429, 'too many incorrect otp attempts', {'error': "TOO_MANY_OTP_ATTEMPTS"})

    otp_hash = gen_otp_hash(msg.payload.email_addr, eth_address, jwt_claim.token, msg.payload.otp)
    if bs_to_base64(otp_hash) != session.otp.otp_hash.decode():
        session.update([SessionModel.otp.incorrect_attempts.set(SessionModel.otp.incorrect_attempts + 1)])
        if session.otp.incorrect_attempts >= 10:
            raise LambdaError(429, 'too many incorrect otp attempts', {'error': "TOO_MANY_OTP_ATTEMPTS"})
        raise LambdaError(422, "otp hash didn't match", {'error': "OTP_MISMATCH"})

    session.update([
        SessionModel.otp.succeeded.set(SessionModel.otp.succeeded.set(True)),
        SessionModel.state.set(SessionState.s020_CONFIRMED_OTP)
    ])

    return {'result': 'success'}


async def send_backup_email(event, ctx, msg, eth_address, jwt_claim, session, *args, **kwargs):
    verify(verifyDictKeys(msg.payload, ['email_addr', 'encrypted_backup']), 'payload keys')
    verify(session.state == SessionState.s020_CONFIRMED_OTP, 'expected state')
    verify(len(msg.payload.encrypted_backup) < 2000, 'expected size of backup')
    verify(can_base64_decode(msg.payload.encrypted_backup), 'backup is base64 encoded')

    send_email(to_addrs=[msg.payload.email_addr], subject="Blockchain Australia AGM Elections Voting Identity Backup",
               body_txt=f"""
Below you will find a backup of your voting identity for use in the Blockchain Australia 2019 AGM.
Combined with the password shown to you during the registration stage, this backup provides an
important means of support, audit, and dispute resolution if such needs would ever arise.

It is important you retain the password shown to you on your voting device.

{format_backup(msg.payload.encrypted_backup)}
""")

    backup_hash = _hash(msg.payload.encrypted_backup.encode())

    session.update([
        SessionModel.state.set(SessionState.s030_SENT_BACKUP_EMAIL),
        SessionModel.backup_hash.set(backup_hash)
    ])
    return {'result': 'success'}


async def confirm_and_finalize_onboarding(event, ctx, msg, eth_address, jwt_claim, session, *args, **kwargs):
    verify(verifyDictKeys(msg.payload, ['email_addr', 'backup_hash']), 'payload keys')
    verify(session.state == SessionState.s030_SENT_BACKUP_EMAIL, 'expected state')

    if session.backup_hash.decode() != msg.payload.backup_hash:
        raise LambdaError(422, 'backup hash does not match', {"error": "BACKUP_HASH_MISMATCH"})

    # w3.eth.
    # todo: publish data to smart contract
    membership_txid = HexBytes("0x1234")

    # todo: get proof of tx in blockchain via merkle branch to block header

    session.update([
        SessionModel.state.set(SessionState.s040_MADE_ID_CONF_TX),
        SessionModel.tx_proof.set(hash_up(membership_txid, eth_address, msg.payload.email_addr, jwt_claim.token))
    ])
    return {'result': 'success'}


async def test_establish_session():
    addr = '0x1234'
    r = await establish_session('', '', AttrDict(payload={'email_addr': 'max-test@xk.io', 'address': addr}), addr, AttrDict(token='asdf'))
    print(r)


def mk_msg(msg_to_sign, sig):
    return {'body': {'msg': msg_to_sign.body.decode(), 'sig': sig.hex()}}


def test_establish_session_via_handler():
    ctx = AttrDict(loop='loop')
    acct = w3.eth.account.privateKeyToAccount(_hash(b'hello')[:32])
    test_email_addr = 'max-test@xk.io'

    msg = AttrDict(
        payload={'email_addr': test_email_addr, 'address': acct.address},
        request=RequestTypes.ESTABLISH_SESSION.value
    )
    msg_to_sign, full_msg, signed = encode_and_sign_msg(msg, acct)

    print(signed)
    print(msg_to_sign)
    print('recover msg', w3.eth.account.recover_message(msg_to_sign, signature=signed.signature))
    print('recover hash1', w3.eth.account.recoverHash(signed.messageHash, signature=signed.signature))
    print('recover hash2', w3.eth.account.recoverHash(eth_utils.keccak(b'\x19' + full_msg), signature=signed.signature))

    sig: HexBytes = signed.signature
    r = message_handler(mk_msg(msg_to_sign, sig), ctx)
    print(r)

    body = AttrDict(json.loads(r['body']))
    jwt_token = body.jwt
    otp = body.otp_unsafe_todo_remove

    msg_2, _, sig_2 = encode_and_sign_msg(AttrDict(
        payload={'email_addr': test_email_addr, 'otp': otp}, jwt=jwt_token,
        request=RequestTypes.PROVIDE_OTP.value
    ), acct)

    r = message_handler(mk_msg(msg_2, sig_2.signature), ctx)
    print(r)

    encrypted_backup = bs_to_base64(os.urandom(512))

    msg_3, _, sig_3 = encode_and_sign_msg(AttrDict(
        payload={'email_addr': test_email_addr, 'encrypted_backup': encrypted_backup},
        jwt=jwt_token, request=RequestTypes.PROVIDE_BACKUP.value
    ), acct)

    r = message_handler(mk_msg(msg_3, sig_3.signature), ctx)


def tests(loop):
    # await test_establish_session()
    test_establish_session_via_handler()


if __name__ == "__main__":
    tests('')
    # loop = asyncio.new_event_loop()
    # tests(loop)
    # loop.run_forever()

