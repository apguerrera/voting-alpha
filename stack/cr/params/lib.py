import binascii
import json
import logging
import os, secrets
import string
import time
import urllib
import hashlib

import boto3

from eth_account.account import Account



def http_get(url: str) -> bytes:
    with urllib.request.urlopen(url) as r:
        return r.read()


def _hash(bs: bytes) -> bytes:
    return hashlib.sha512(bs).digest()


def get_some_entropy() -> bytes:
    '''Use various online sources + urandom to generate entropy'''
    sources = [secrets.token_bytes(128), _hash(http_get("https://www.grc.com/passwords.htm"))]
    return _hash(b''.join(sources))


def remove_s3_bucket(StaticBucketName, StackTesting, **params):
    if StackTesting:
        return {'DeletedS3Bucket': str(boto3.resource('s3').Bucket(StaticBucketName).objects.filter().delete())}
    return {}


def generate_ec2_key(ShouldGenEc2SSHKey: bool, NamePrefix: str, SSHEncryptionPassword: str, AdminEmail, **kwargs):
    logging.info("gen_ec2_key: %s", {'ShouldGenEc2SSHKey': ShouldGenEc2SSHKey, 'NamePrefix': NamePrefix})
    ret = {'CreatedEc2KeyPair': False}
    KeyPairName = "{}-sv-node-ec2-ssh-key".format(NamePrefix)  # , int(time.time()))
    ret['KeyPairName'] = KeyPairName
    ec2 = boto3.client('ec2')
    kps = ec2.describe_key_pairs()
    if sum([kp['KeyName'] == KeyPairName for kp in
            kps['KeyPairs']]) == 0:  # this should always be 0 if we add a timestamp to the SSH key
        ret['CreatedEc2KeyPair'] = True
        if ShouldGenEc2SSHKey:
            raise Exception("ssh key gen not supported yet")
            # ssh_pem = ec2.create_key_pair(KeyName=KeyPairName)['KeyMaterial']
            # sns = boto3.client('sns')
        elif 'SSHKey' in kwargs:
            ec2.import_key_pair(KeyName=KeyPairName, PublicKeyMaterial=kwargs['SSHKey'].encode())
        else:
            raise Exception(
                "`SSHKey` must be provided or an SSH key must be generated which requires SSHEncryptionPassword.")
    else:
        # we already have a key with this name. Keep it?
        # We probs want to delete the key when we remove the stack...
        pass
    return ret


def priv_to_addr(_priv):
    return Account.privateKeyToAccount(_priv).address


def generate_node_keys(NConsensusNodes, NamePrefix, **kwargs) -> (list, list, list):
    _e = get_some_entropy()

    def gen_next_key():
        nonlocal _e
        _h = _hash(_e)
        _privkey = '0x' + binascii.hexlify(_h[:32]).decode()
        _e = _h[32:]
        assert len(_e) >= 32
        pk = priv_to_addr(_privkey)
        return _privkey, pk

    keys = []
    poa_pks = []
    service_pks = []
    for i in range(int(NConsensusNodes)):  # max nConsensusNodes
        (_privkey, pk) = gen_next_key()
        keys.append({'Name': "sv-{}-nodekey-consensus-{}".format(NamePrefix, i),
                     'Description': "Private key for consensus node #{}".format(i),
                     'Value': _privkey, 'Type': 'SecureString'})
        keys.append({'Name': "sv-{}-param-poa-pk-{}".format(NamePrefix, i),
                     'Description': "Public address of consensus node #{}".format(i),
                     'Value': pk, 'Type': 'String'})
        poa_pks.append(pk)

    for eth_service in ["publish", "members", "castvote"]:
        (_privkey, pk) = gen_next_key()
        keys.append({'Name': "sv-{}-nodekey-service-{}".format(NamePrefix, eth_service),
                     'Description': "Private key for service lambda: {}".format(eth_service),
                     'Value': _privkey, 'Type': 'SecureString'})
        service_pks.append(pk)

    return keys, poa_pks, service_pks


def save_node_keys(keys: list, NamePrefix, **kwargs):
    ssm = boto3.client('ssm')
    existing_ssm = ssm.describe_parameters(ParameterFilters=[
        {'Key': 'Name', 'Option': 'BeginsWith',
         'Values': [
             "sv-{}-nodekey-consensus".format(NamePrefix),
             "sv-{}-param-poa-pk".format(NamePrefix)
         ]}
    ], MaxResults=50)['Parameters']
    existing_ssm_names = {p['Name'] for p in existing_ssm}
    logging.info('existing_ssm_names: %s', existing_ssm_names)
    skipped_params = []
    for k in keys:
        if k['Name'] in existing_ssm_names:
            logging.info("Skipping SSM Param as it exists: {}".format(k['Name']))
            skipped_params.append(k['Name'])
        else:
            logging.info("Creating SSM param: %s", k['Name'])
            try:
                ssm.put_parameter(**k)
            except Exception as e:
                if 'ParameterAlreadyExists' not in repr(e):
                    raise e
    return {'SavedConsensusNodePrivKeys': True, 'SkippedConsensusNodePrivKeys': skipped_params}


def gen_network_id(**kwargs):
    return {'NetworkId': secrets.randbits(32)}


def gen_eth_stats_secret(**kwargs):
    return {
        'EthStatsSecret': ''.join([secrets.choice(string.ascii_letters + string.digits) for _ in range(20)])
    }


def upload_chain_config(poa_pks, service_pks, StaticBucketName, **params):
    chainspec = json.dumps(gen_chainspec_json(poa_pks, service_pks, **params))
    ret = {'ChainSpec': chainspec}
    obj_key = 'chain/chainspec.json'
    s3 = boto3.client('s3')
    put_resp = s3.put_object(Key=obj_key, Body=chainspec, Bucket=StaticBucketName, ACL='public-read')
    ret['ChainSpecUrl'] = '{}/{}/{}'.format(s3.meta.endpoint_url, StaticBucketName, obj_key)
    return ret


def gen_chainspec_json(poa_addresses: list, service_addresses: list, **params) -> dict:
    """
    :param poa_addresses: list of addresses for the proof of authority nodes
    :param service_addresses: list of addresses for services (i.e the lambdas that do things like onboarding members)
    :return: dict: the chainspec
    """

    NamePrefix = params['NamePrefix']
    gas_limit = hex(int(params['BlockGasLimit']))

    NetworkId = params['NetworkId']
    hex_network_id = hex(NetworkId)

    INIT_BAL = 160693804425899027554196209234

    builtins = {"0000000000000000000000000000000000000001": {
        "builtin": {"balance": "1", "name": "ecrecover", "pricing": {"linear": {"base": 3000, "word": 0}}}},
        "0000000000000000000000000000000000000002": {
            "builtin": {"balance": "1", "name": "sha256", "pricing": {"linear": {"base": 60, "word": 12}}}},
        "0000000000000000000000000000000000000003": {
            "builtin": {"balance": "1", "name": "ripemd160", "pricing": {"linear": {"base": 600, "word": 120}}}},
        "0000000000000000000000000000000000000004": {
            "builtin": {"balance": "1", "name": "identity", "pricing": {"linear": {"base": 15, "word": 3}}}},
        "0000000000000000000000000000000000000005": {
            "builtin": {"name": "modexp", "pricing": {"modexp": {"divisor": 20}}}},
        "0000000000000000000000000000000000000006": {
            "builtin": {"name": "alt_bn128_add", "pricing": {"linear": {"base": 500, "word": 0}}}},
        "0000000000000000000000000000000000000007": {
            "builtin": {"name": "alt_bn128_mul", "pricing": {"linear": {"base": 40000, "word": 0}}}},
        "0000000000000000000000000000000000000008": {"builtin": {"name": "alt_bn128_pairing", "pricing": {
            "alt_bn128_pairing": {"base": 100000, "pair": 80000}}}}}

    accounts = {addr: {"balance": INIT_BAL} for addr in service_addresses}
    accounts.update(builtins)

    return {
        "name": "{} PoA Network - Powered by SecureVote and Flux".format(NamePrefix),
        "dataDir": "sv-{}-poa-net".format(NamePrefix),
        "engine": {
            "authorityRound": {
                "params": {
                    "stepDuration": "5",
                    "blockReward": "0x4563918244F40000",
                    "validators": {
                        "multi": {
                            "0": {
                                "list": poa_addresses
                            }
                        }
                    },
                    "maximumUncleCountTransition": 0,
                    "maximumUncleCount": 0
                }
            }
        },
        "genesis": {
            "seal": {
                "authorityRound": {
                    "step": "0x0",
                    "signature": "0x0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
                }
            },
            "difficulty": "0x20000",
            "gasLimit": gas_limit,
            "timestamp": 1546128058,
            "extraData": "0x" + "{}-PoweredBySecureVote".format(NamePrefix)[:32].encode().hex()
        },
        "params": {
            "networkID": hex_network_id,
            "maximumExtraDataSize": "0x20",
            "minGasLimit": "0x3fffff",
            "gasLimitBoundDivisor": "0x400",
            # "registrar": "0xfAb104398BBefbd47752E7702D9fE23047E1Bca3",
            "maxCodeSize": 65536,
            "maxCodeSizeTransition": 0,
            "validateChainIdTransition": 0,
            "validateReceiptsTransition": 0,
            "eip140Transition": 0,
            "eip211Transition": 0,
            "eip214Transition": 0,
            "eip658Transition": 0,
            "wasmActivationTransition": 0
        },
        "accounts": accounts,
        # "nodes": [
        #     "enode://304c9dbcca45409785539b227f273c3329197c86de6cc8d73252870f91176eb3588db2774fc7db4a6011519faa5fa4f39d63aeb341db672901ebdf3555fda095@13.238.183.223:30303",
        #     "enode://7a75777e450bd552ff55b08746a10873e141ba15984fbd1d89cc132e468d4f9ed5ddf011008fbf39be2e45c03b0930328faa074b6c040d46b1a543a49b47ee06@52.213.81.2:30303",
        #     "enode://828d0acaad0e9dc726ceac4d0a6e21451d80cbcfb24931c92219b9be713c2f66fa2ca5dd81d19a660ef182c94758e2ff0e2ad94b27fe78dbce9d107027ae5a68@13.211.132.131:30303"
        # ]
    }
