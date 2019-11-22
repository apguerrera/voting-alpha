from stack.app.members.api.lib import mk_logger

log = mk_logger('members-onboard')


def onboard_handler(event, ctx):
    print('hi', event, ctx)
    log.info(f'onboard lambda: {event}, {ctx}')
    return {'statusCode': 200, 'body': '-'}


def list_members_handler(event, ctx):
    print('list members', event, ctx)
    log.info(f'list members lambda: {event}, {ctx}')
    return {'statusCode': 200, 'body': '-'}
