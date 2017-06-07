import errno
import os
import sys

from middlewared.client import Client
from middlewared.schema import accepts, Bool, Dict, Int, List, Str
from middlewared.service import private, CallError, Service
from middlewared.utils import run

# FIXME: temporary imports while license methods are still in django
if '/usr/local/www' not in sys.path:
    sys.path.append('/usr/local/www')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'freenasUI.settings')
import django
django.setup()
from freenasUI.freeadmin.sqlite3_ha.base import Journal
from freenasUI.failover.detect import ha_hardware, ha_node
from freenasUI.failover.enc_helper import LocalEscrowCtl
from freenasUI.support.utils import get_license

INTERNAL_IFACE_NF = '/tmp/.failover_internal_iface_not_found'


class FailoverService(Service):

    class Config:
        private = True

    @accepts()
    def licensed(self):
        """
        Checks whether this instance is licensed as a HA unit.
        """
        license, error = get_license()
        if license is None or not license.system_serial_ha:
            return False
        return True

    @accepts()
    def hardware(self):
        """
        Gets the hardware type of HA.

          ECHOSTREAM
          AIC
          PUMA
          SBB
          ULTIMATE
          MANUAL
        """
        return ha_hardware()

    @accepts()
    def node(self):
        """
        Gets the node identification.
          A - First node
          B - Seconde Node
          MANUAL - could not be identified, its in manual mode
        """
        node = ha_node()
        if node is None:
            return 'MANUAL'
        return node

    @private
    @accepts()
    def internal_interfaces(self):
        """
        Interfaces used internally for HA.
        It is a direct link between the nodes.
        """
        hardware = self.hardware()
        if hardware == 'ECHOSTREAM':
            proc = run('/usr/sbin/pciconf -lv | grep "card=0xa01f8086 chip=0x10d38086"')
            if not proc.stdout:
                if not os.path.exists(INTERNAL_IFACE_NF):
                    open(INTERNAL_IFACE_NF, 'w').close()
                return []
            return [proc.stdout.split('@')[0]]
        elif hardware == 'SBB':
            return ['ix0']
        elif hardware in ('AIC', 'PUMA'):
            return ['ntb0']
        elif hardware == 'ULTIMATE':
            return ['igb1']
        return []

    @accepts(
        Str('method'),
        List('args'),
        Int('timeout'),
    )
    def call_remote(self, method, args=None, timeout=None):
        if args is None:
            args = []

        node = self.node()
        if node == 'A':
            remote = '169.254.10.2'
        elif node == 'B':
            remote = '169.254.10.1'
        else:
            raise CallError(f'Node {node} invalid for call_remote', errno.EBADRPC)
        # 860 is the iSCSI port and blocked by the failover script
        with Client(f'ws://{remote}:6000/websocket', reserved_ports=True, reserved_ports_blacklist=[860]) as c:
            kwargs = {}
            if timeout:
                kwargs['timeout'] = timeout
            return c.call(method, *args, **kwargs)

    @accepts()
    def encryption_getkey(self):
        # FIXME: we could get rid of escrow, middlewared can do that job
        escrowctl = LocalEscrowCtl()
        return escrowctl.getkey()

    @accepts(Str('passphrase'))
    def encryption_setkey(self, passphrase):
        # FIXME: we could get rid of escrow, middlewared can do that job
        escrowctl = LocalEscrowCtl()
        return escrowctl.setkey(passphrase)

    @accepts(
        Str('action', enum=['ENABLE', 'DISABLE']),
        Dict(
            'options',
            Bool('active'),
        ),
    )
    def control(self, action, options=None):
        if options is None:
            options = {}

        failover = self.middleware.call('datastore.config', 'failover.failover')
        if action == 'ENABLE':
            if failover['disabled'] is False:
                # Already enabled
                return False
            failover.update({
                'disabled': False,
                'master': False,
            })
            self.middleware.call('datastore.update', 'failover.failover', failover['id'], failover)
            self.middleware.call('service.start', 'ix-devd')
        elif action == 'DISABLE':
            if failover['disabled'] is True:
                # Already disabled
                return False
            failover['master'] = True if options.get('active') else False
            self.middleware.call('datastore.update', 'failover.failover', failover['id'], failover)
            self.middleware.call('service.start', 'ix-devd')

    @accepts()
    def database_sync(self):
        dump = self.middleware.call('datastore.dump')
        with Journal() as j:
            restore = self.call_remote('datastore.restore', [dump])
            if restore:
                j.queries = []
        return restore


def ha_permission(app):
    remote_addr = app.ws.environ['REMOTE_ADDR']
    remote_port = int(app.ws.environ['REMOTE_PORT'])

    if remote_port <= 1024 and remote_addr in (
        '169.254.10.1',
        '169.254.10.2',
        '169.254.10.20',
        '169.254.10.80',
    ):
        app.authenticated = True


def setup(middleware):
    middleware.register_hook('core.on_connect', ha_permission, sync=True)
