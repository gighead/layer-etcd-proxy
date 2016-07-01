#!/usr/bin/python3

from charms.reactive import when
from charms.reactive import when_not
from charms.reactive import is_state
from charms.reactive import set_state
from charms.reactive import remove_state
from charms.reactive import hook

from charms.templating.jinja2 import render

from charmhelpers.core.hookenv import status_set
from charmhelpers.core.hookenv import log
from charmhelpers.core.hookenv import resource_get

from charmhelpers.core.hookenv import open_port
from charmhelpers.core.hookenv import close_port
from charmhelpers.core import hookenv
from charmhelpers.core import host
from charmhelpers.fetch import apt_update
from charmhelpers.fetch import apt_install

from pwd import getpwnam
from shlex import split
from subprocess import check_call
from subprocess import CalledProcessError

import os
import shutil
import time


@when_not('etcd.installed')
def install_etcd():
    ''' Attempt resource get on the "etcd" and "etcdctl" resources. If no
    resources are provided attempt to install from the archive only on the
    16.04 (xenial) series. '''
    status_set('maintenance', 'Installing etcd.')

    codename = host.lsb_release()['DISTRIB_CODENAME']

    try:
        etcd_path = resource_get('etcd')
        etcdctl_path = resource_get('etcdctl')
    # Not obvious but this blocks juju 1.25 clients
    except NotImplementedError:
        status_set('blocked', 'This charm requires the resource feature available in juju 2+')  # noqa
        return

    if not etcd_path or not etcdctl_path:
        if codename == 'xenial':
            # edge case where archive allows us a nice fallback on xenial
            status_set('maintenance', 'Attempting install of etcd from apt')
            pkg_list = ['etcd']
            apt_update()
            apt_install(pkg_list, fatal=True)
            # Stop the service and remove the defaults
            # I hate that I have to do this. Sorry short-lived local data #RIP
            # State control is to prevent upgrade-charm from nuking cluster
            # data.
            if not is_state('etcd.package.adjusted'):
                host.service('stop', 'etcd')
                if os.path.exists('/var/lib/etcd/default'):
                    shutil.rmtree('/var/lib/etcd/default')
                set_state('etcd.package.adjusted')
            set_state('etcd.installed')
            return
        else:
            # edge case
            status_set('blocked', 'Missing Resource: see README')
    else:
        install(etcd_path, '/usr/bin/etcd')
        install(etcdctl_path, '/usr/bin/etcdctl')

        host.add_group('etcd')

        if not host.user_exists('etcd'):
            host.adduser('etcd')
            host.add_user_to_group('etcd', 'etcd')

        os.makedirs('/var/lib/etcd/', exist_ok=True)
        etcd_uid = getpwnam('etcd').pw_uid

        os.chmod('/var/lib/etcd/', 0o775)
        os.chown('/var/lib/etcd/', etcd_uid, -1)

        # Trusty was the EOL for upstart, render its template if required
        if codename == 'trusty':
            render('upstart', '/etc/init/etcd.conf',
                   {}, owner='root', group='root')
            set_state('etcd.installed')
            return

        if not os.path.exists('/etc/systemd/system/etcd.service'):
            render('systemd', '/etc/systemd/system/etcd.service',
                   {}, owner='root', group='root')
            # This will cause some greif if its been run before
            # so allow it to be chatty and fail if we ever re-render
            # and attempt re-enablement.
            try:
                check_call(split('systemctl enable etcd'))
            except CalledProcessError:
                pass

        set_state('etcd.installed')


@when('etcd.installed')
@when('proxy.tls.available')
def configure_etcd(proxy):
    proxy.save_client_credentials('/tmp/etcd_key',
                                  '/tmp/etcd_cert',
                                  '/tmp/etcd_ca')
    cluster = proxy.get_remote('cluster')
    # Render the proxy's configuration with the new values.
    render('defaults',
           '/etc/default/etcd',
           {'port': hookenv.config('port'),
            'cluster': cluster,
            'server_certificate': '/tmp/etcd_cert',
            'server_key': '/tmp/etcd_key',
            'ca_certificate': '/tmp/etcd_ca'},
           owner='root',
           group='root')
    # Close the previous client port and open the new one.
    close_open_ports()
    host.service_restart('etcd')


@hook('upgrade-charm')
def remove_states():
    # upgrade-charm issues when we rev resources and the charm. Assume an upset
    remove_state('etcd.installed')


def close_open_ports():
    ''' Close the previous port and open the port from configuration. '''
    configuration = hookenv.config()
    previous_port = configuration.previous('port')
    port = configuration.get('port')
    if previous_port is not None and previous_port != port:
        log('The port changed; closing {0} opening {1}'.format(previous_port,
            port))
        close_port(previous_port)
        open_port(port)


def install(src, tgt):
    ''' This method wraps the bash "install" command '''
    return check_call(split('install {} {}'.format(src, tgt)))
