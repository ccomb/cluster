#!/usr/bin/python3
# coding: utf-8
# TODO identify pure functions
import hashlib
import json
import logging
import re
import requests
import socket
import time
import yaml
from base64 import b64decode, b64encode
from contextlib import contextmanager
from datetime import datetime
from os.path import basename, join, exists
from subprocess import run as srun, CalledProcessError, PIPE
from sys import stdin, argv
from urllib.parse import urlparse
from uuid import uuid1
DTFORMAT = "%Y-%m-%dT%H:%M:%S.%f"
DEPLOY = '/deploy'
log = logging.getLogger()
HANDLED = '/deploy/events.log'
if not exists(HANDLED):
    open(HANDLED, 'x')


def _run(cmd, cwd=None):
    try:
        if cwd:
            cmd = 'cd "{}" && {}'.format(cwd, cmd)
        log.info(cmd)
        res = srun(cmd, shell=True, check=True, stdout=PIPE, stderr=PIPE)
        return res.stdout.decode().strip()
    except CalledProcessError as e:
        log.error("Failed to run %s: %s", e.cmd, e.stderr.decode())
        raise


class Application(object):
    def __init__(self, repo_url, branch, cwd=None):
        self.repo_url, self.branch = repo_url.strip(), branch.strip()
        if self.repo_url.endswith('.git'):
            self.repo_url = self.repo_url[:-4]
        md5 = hashlib.md5(urlparse(self.repo_url.lower()).path.encode('utf-8')
                          ).hexdigest()
        repo_name = basename(self.repo_url.strip('/'))
        self.name = repo_name + ('_' + self.branch if self.branch else ''
                                 ) + '.' + md5[::7]  # don't need full md5
        self._services = None
        self._volumes = None
        self._compose = None
        self._deploy_date = None

    @property
    def deploy_date(self):
        """date of the last deployment"""
        if self._deploy_date is None:
            try:
                self._deploy_date = self.valueof(self.name, 'deploy_date')
            except:
                log.info("No current deploy date found in the kv")
        return self._deploy_date

    def _path(self, deploy_date=None):
        """path of the deployment checkout"""
        deploy_date = deploy_date or self.deploy_date
        if deploy_date:
            return join(DEPLOY, self.name + '@' + self.deploy_date)
        return None

    @property
    def path(self):
        return self._path()

    def check(self):
        """consistency check"""
        sites = {s['key']: json.loads(b64decode(s['value']).decode('utf-8'))
                 for s in json.loads(self.do('consul kv export site/'))}
        # check urls are not already used
        for service in self.services:
            urls = [self.url(service)] + self.redirect_from(service)
            for site in sites.values():
                if site.get('name') == self.name:
                    continue
                for url in urls:
                    if (url in site.get('redirect_from', [])
                            or url == site.get('url')):
                        msg = ('Aborting! Site {} is already deployed by {}'
                               .format(url, site['name']))
                        log.error(msg)
                        raise ValueError(msg)

    def do(self, cmd, cwd=None):
        return _run(cmd, cwd=cwd)

    @property
    def compose(self):
        """read the compose file
        """
        if self._compose is None:
            try:
                with open(join(self.path, 'docker-compose.yml')) as c:
                    self._compose = yaml.load(c.read())
            except:
                log.error('Could not read docker-compose.yml')
                raise EnvironmentError('Could not read docker-compose.yml')
        return self._compose

    @contextmanager
    def notify_transfer(self):
        try:
            yield
            self.do('consul kv put transfer/{}/success'.format(self.name))
        except:
            log.error('Volume transfer FAILED!')
            self.do('consul kv put transfer/{}/failure'.format(self.name))
            self.up()  # TODO move in the deploy
            raise
        log.info('Volume transfer SUCCEEDED!')

    def wait_transfer(self):
        loops = 0
        while loops < 60:
            log.info('Waiting transfer notification for %s', self.name)
            res = self.do('consul kv get -keys transfer/{}'.format(self.name))
            if res:
                status = res.split('/')[-1]
                self.do('consul kv delete -recurse transfer/{}/'
                        .format(self.name))
                log.info('Transfer notification status: %s', status)
                return status
            time.sleep(1)
            loops += 1
        msg = ('Waited too much :( Master did not send a notification for %s')
        log.info(msg, self.name)
        raise RuntimeError(msg % self.name)

    @property
    def services(self):
        """name of the services in the compose file
        """
        if self._services is None:
            self._services = self.compose['services'].keys()
        return self._services

    @property
    def project(self):
        return re.sub(r'[^a-z0-9]', '', self.name)

    @property
    def volumes(self):
        """btrfs volumes defined in the compose,
        or in the kv if no compose available
        """
        if self._volumes is None:
            try:
                self._volumes = [
                    Volume(self.project + '_' + v[0])
                    for v in self.compose.get('volumes', {}).items()
                    if v[1] and v[1].get('driver') == 'btrfs']
            except:
                log.info("No compose available,"
                         "reading volumes from the kv store")
                try:
                    self._volumes = [
                        Volume(v) for v in self.valueof(self.name, 'volumes')]
                except:
                    log.info("No volumes found in the kv store")
                    self._volumes = []
            self._volumes = self._volumes or None
        return self._volumes

    def container_name(self, service):
        """did'nt find a way to query reliably so do it static
        It assumes there is only 1 container for a project/service couple
        """
        return self.project + '_' + service + '_1'

    def valueof(self, name, key):
        """ return the current value of the key in the kv"""
        cmd = 'consul kv get site/{}'.format(name)
        return json.loads(self.do(cmd))[key]

    def compose_domain(self):
        """domain name of the first exposed service in the compose"""
        # FIXME prevents from exposing two domains in a compose
        domains = [self.domain(s) for s in self.services]
        domains = [d for d in domains if d is not None]
        return domains[0] if domains else ''

    @property
    def slave_node(self):
        """slave node for the current app """
        try:
            return self.valueof(self.name, 'slave')
        except:
            log.warn('Could not determine the slave node for %s', self.name)
            return None

    @property
    def master_node(self):
        """master node for the current app """
        try:
            return self.valueof(self.name, 'node')
        except:
            log.warn('Could not determine the master node for %s', self.name)
            return None

    def clean(self):
        if self.path and exists(self.path):
            self.do('rm -rf "{}"'.format(self.path))

    def fetch(self, retrying=False):
        try:
            self.clean()
            deploy_date = datetime.now().strftime(DTFORMAT)
            path = self._path(deploy_date)
            self.do('git clone --depth 1 {} "{}" "{}"'
                    .format('-b "%s"' % self.branch if self.branch else '',
                            self.repo_url, path),
                    cwd=DEPLOY)
            self._deploy_date = deploy_date
            self._services = None
            self._volumes = None
            self._compose = None
        except CalledProcessError:
            if not retrying:
                log.warn("Failed to fetch %s, retrying", self.repo_url)
                self.fetch(retrying=True)
            else:
                raise

    def up(self):
        if self.path and exists(self.path):
            log.info("Starting %s", self.name)
            self.do('docker-compose -p "{}" up -d --build'
                    .format(self.project),
                    cwd=self.path)
        else:
            log.info("No deployment, cannot start %s", self.name)

    def down(self, deletevolumes=False):
        if self.path and exists(self.path):
            log.info("Stopping %s", self.name)
            v = '-v' if deletevolumes else ''
            self.do('docker-compose -p "{}" down {}'.format(self.project, v),
                    cwd=self.path)
        else:
            log.info("No deployment, cannot stop %s", self.name)

    def _members(self):
        # TODO move outside this class
        return self.do('consul members')

    @property
    def members(self):
        members = {}
        for m in self._members().split('\n')[1:]:
            name, ip, status = m.split()[:3]
            members[name] = {'ip': ip.split(':')[0], 'status': status}
        return members

    def compose_env(self, service, name, default=None):
        """retrieve an environment variable from the service in the compose
        """
        try:
            val = self.compose['services'][service]['environment'][name]
            log.info('Found a %s environment variable for '
                     'service %s in the compose file of %s',
                     name, service, self.name)
            return val
        except:
            log.info('No %s environment variable for '
                     'service %s in the compose file of %s',
                     name, service, self.name)
            return default

    def tls(self, service):
        """used to disable tls with TLS: self_signed """
        return self.compose_env(service, 'TLS')

    def url(self, service):
        """end url to expose the service """
        return self.compose_env(service, 'URL')

    def redirect_from(self, service):
        """ list of redirects transmitted to caddy """
        lines = self.compose_env(service, 'REDIRECT_FROM', '').split('\n')
        return [l.strip() for l in lines if len(l.split()) == 1]

    def redirect_to(self, service):
        """ list of redirects transmitted to caddy """
        lines = self.compose_env(service, 'REDIRECT_TO', '').split('\n')
        return [l.strip() for l in lines if 1 <= len(l.split()) <= 3]

    def domain(self, service):
        """ domain computed from the URL
        """
        return urlparse(self.url).netloc.split(':')[0]

    def proto(self, service):
        """frontend protocol configured in the compose for the service.
        """
        return self.compose_env(service, 'PROTO', 'http://')

    def port(self, service):
        """frontend port configured in the compose for the service.
        """
        return self.compose_env(service, 'PORT', '80')

    def ps(self, service):
        ps = self.do('docker ps -f name=%s --format "table {{.Status}}"'
                     % self.container_name(service))
        return ps.split('\n')[-1].strip()

    def register_kv(self, target, slave, myself):
        """register a service in the key/value store
        so that consul-template can regenerate the
        caddy and haproxy conf files
        """
        log.info("Registering URLs of %s in the key/value store",
                 self.name)
        for service in self.services:
            url = self.url(service)
            redirect_from = self.redirect_from(service)
            redirect_to = self.redirect_to(service)
            tls = self.tls(service)
            if not url:
                # service not exposed to the web
                continue
            domain = urlparse(url).netloc.split(':')[0]
            # store the domain and name in the kv
            ct = self.container_name(service)
            port = self.port(service)
            proto = self.proto(service)
            value = {
                'name': self.name,  # name of the service, and key in the kv
                'deploy_date': self._deploy_date,
                'domain': domain,  # used by haproxy
                'ip': self.members[target]['ip'],  # used by haproxy
                'node': target,  # used by haproxy and caddy
                'url': url,  # used by caddy
                'redirect_from': redirect_from,  # used by caddy
                'redirect_to': redirect_to,  # used by caddy
                'tls': tls,  # used by caddy
                'slave': slave,  # used by the handler
                'volumes': [v.name for v in self.volumes],
                'ct': '{proto}{ct}:{port}'.format(**locals())}  # used by caddy
            self.do("consul kv put site/{} '{}'"
                    .format(self.name, json.dumps(value)))
            log.info("Registered %s", self.name)

    def unregister_kv(self):
        self.do("consul kv delete site/{}".format(self.name))

    def register_consul(self):
        """register a service and check in consul
        """
        urls = [self.url(s) for s in self.services]
        svc = json.dumps({
            'Name': self.name,
            'Checks': [{
                'HTTP': url,
                'Interval': '60s'} for url in urls if url]})
        url = 'http://localhost:8500/v1/agent/service/register'
        res = requests.post(url, svc)
        if res.status_code != 200:
            msg = 'Consul service register failed: {}'.format(res.reason)
            log.error(msg)
            raise RuntimeError(msg)
        log.info("Registered %s in consul", self.name)

    def unregister_consul(self):
        for service in self.services:
            url = self.url(service)
            if not url:
                continue
            url = ('http://localhost:8500/v1/agent/service/deregister/{}'
                   .format(self.name))
            res = requests.put(url)
            if res.status_code != 200:
                msg = 'Consul service deregister failed: {}'.format(res.reason)
                log.error(msg)
                raise RuntimeError(msg)
            log.info("Deregistered %s in consul", self.name)

    def enable_snapshot(self, enable):
        """enable or disable scheduled snapshots
        """
        for volume in self.volumes:
            volume.schedule_snapshots(60 if enable else 0)

    def enable_replicate(self, enable, ip):
        """enable or disable scheduled replication
        """
        for volume in self.volumes:
            volume.schedule_replicate(60 if enable else 0, ip)

    def enable_purge(self, enable):
        for volume in self.volumes:
            volume.schedule_purge(1440 if enable else 0, '1h:1d:1w:4w:1y')


class Volume(object):
    """wrapper for buttervolume cli
    """
    def __init__(self, name):
        self.name = name

    def do(self, cmd, cwd=None):
        return _run(cmd, cwd=cwd)

    def snapshot(self):
        """snapshot the volume
        """
        log.info(u'Snapshotting volume: {}'.format(self.name))
        return self.do("buttervolume snapshot {}".format(self.name))

    def schedule_snapshots(self, timer):
        """schedule snapshots of the volume
        """
        self.do("buttervolume schedule snapshot {} {}"
                .format(timer, self.name))

    def schedule_replicate(self, timer, slavehost):
        """schedule a replication of the volume
        """
        self.do("buttervolume schedule replicate:{} {} {}"
                .format(slavehost, timer, self.name))

    def schedule_purge(self, timer, pattern):
        """schedule a purge of the snapshots
        """
        self.do("buttervolume schedule purge:{} {} {}"
                .format(pattern, timer, self.name))

    def delete(self):
        """destroy a volume
        """
        log.info(u'Destroying volume: {}'.format(self.name))
        return self.do("docker volume rm {}".format(self.name))

    def restore(self, snapshot=None, target=''):
        if snapshot is None:  # use the latest snapshot
            snapshot = self.name
        log.info(u'Restoring snapshot: {}'.format(snapshot))
        restored = self.do("buttervolume restore {} {}"
                           .format(snapshot, target))
        target = 'as {}'.format(target) if target else ''
        log.info('Restored %s %s (after a backup: %s)',
                 snapshot, target, restored)

    def send(self, snapshot, target):
        log.info(u'Sending snapshot: {}'.format(snapshot))
        self.do("buttervolume send {} {}".format(target, snapshot))


def handle(events, myself):
    for event in json.loads(events):
        event_id = event.get('ID')
        if event_id + '\n' in open(HANDLED, 'r').readlines():
            log.info('Event already handled in the past: %s', event_id)
            continue
        open(HANDLED, 'a').write(event_id + '\n')
        event_name = event.get('Name')
        payload = b64decode(event.get('Payload', '')).decode('utf-8')
        if not payload:
            return
        log.info(u'**** Received event: {} with ID: {} and payload: {}'
                 .format(event_name, event_id, payload))
        try:
            payload = json.loads(payload)
        except:
            msg = 'Wrong event payload format. Please provide json'
            log.error(msg)
            raise Exception(msg)

        if event_name == 'deploy':
            deploy(payload, myself)
        elif event_name == 'destroy':
            destroy(payload, myself)
        elif event_name == 'migrate':
            migrate(payload, myself)
        else:
            log.error('Unknown event name: {}'.format(event_name))


def deploy(payload, myself):
    """Keep in mind this is executed in the consul container
    Deployments are done in the DEPLOY folder. Needs:
    {"repo"': <url>, "branch": <branch>, "target": <host>, "slave": <host>}
    """
    repo_url = payload['repo']
    newmaster = payload['target']
    newslave = payload.get('slave')
    if newmaster == newslave:
        msg = "Slave must be different than the target Master"
        log.error(msg)
        raise AssertionError(msg)
    branch = payload.get('branch')
    if not branch:
        msg = "Branch is mandatory"
        log.error(msg)
        raise AssertionError(msg)

    oldapp = Application(repo_url, branch=branch)
    oldmaster = oldapp.master_node
    oldslave = oldapp.slave_node
    newapp = Application(repo_url, branch=branch)
    members = newapp.members

    if oldmaster == myself:  # master ->
        log.info('** I was the master of %s', oldapp.name)
        oldapp.down()
        if oldslave:
            oldapp.enable_replicate(False, members[oldslave]['ip'])
        else:
            oldapp.enable_snapshot(False)
        oldapp.enable_purge(False)
        if newmaster == myself:  # master -> master
            log.info("** I'm still the master of %s", newapp.name)
            for volume in oldapp.volumes:
                volume.snapshot()
            newapp.fetch()
            newapp.check()
            newapp.up()
            if newslave:
                newapp.enable_replicate(True, members[newslave]['ip'])
            else:
                newapp.enable_snapshot(True)
            newapp.enable_purge(True)
            newapp.register_kv(newmaster, newslave, myself)  # for consul-templ
            newapp.register_consul()  # for consul check
        elif newslave == myself:  # master -> slave
            log.info("** I'm now the slave of %s", newapp.name)
            with newapp.notify_transfer():
                for volume in newapp.volumes:
                    volume.send(volume.snapshot(), members[newmaster]['ip'])
            oldapp.unregister_consul()
            oldapp.down(deletevolumes=True)
            newapp.enable_purge(True)
        else:  # master -> nothing
            log.info("** I'm nothing now for %s", newapp.name)
            with newapp.notify_transfer():
                for volume in newapp.volumes:
                    volume.send(volume.snapshot(), members[newmaster]['ip'])
            oldapp.unregister_consul()
            oldapp.down(deletevolumes=True)
        oldapp.clean()

    elif oldslave == myself:  # slave ->
        log.info("** I was the slave of %s", oldapp.name)
        oldapp.enable_purge(False)
        if newmaster == myself:  # slave -> master
            log.info("** I'm now the master of %s", newapp.name)
            newapp.fetch()
            newapp.check()
            newapp.wait_transfer()  # wait for master notification
            for volume in newapp.volumes:
                volume.restore()
            newapp.up()
            if newslave:
                newapp.enable_replicate(True, members[newslave]['ip'])
            else:
                newapp.enable_snapshot(True)
            newapp.enable_purge(True)
            newapp.register_kv(newmaster, newslave, myself)  # for consul-templ
            newapp.register_consul()  # for consul check
        elif newslave == myself:  # slave -> slave
            log.info("** I'm still the slave of %s", newapp.name)
            newapp.enable_purge(True)
        else:  # slave -> nothing
            log.info("** I'm nothing now for %s", newapp.name)

    else:  # nothing ->
        log.info("** I was nothing for %s", oldapp.name)
        if newmaster == myself:  # nothing -> master
            log.info("** I'm now the master of %s", newapp.name)
            newapp.fetch()
            newapp.check()
            if oldslave:
                newapp.wait_transfer()  # wait for master notification
                for volume in newapp.volumes:
                    volume.restore()
            newapp.up()
            if newslave:
                newapp.enable_replicate(True, members[newslave]['ip'])
            else:
                newapp.enable_snapshot(True)
            newapp.enable_purge(True)
            newapp.register_kv(newmaster, newslave, myself)  # for consul-templ
            newapp.register_consul()  # for consul check
        elif newslave == myself:  # nothing -> slave
            log.info("** I'm now the slave of %s", newapp.name)
            newapp.fetch()
            newapp.enable_purge(True)
        else:  # nothing -> nothing
            log.info("** I'm still nothing for %s", newapp.name)


def destroy(payload, myself):
    """Destroy containers, unregister, remove schedules and volumes,
    but keep snapshots. Needs:
    {"repo"': <url>, "branch": <branch>}
    """
    repo_url = payload['repo']
    branch = payload.get('branch')
    if not branch:
        msg = "Branch is mandatory"
        log.error(msg)
        raise AssertionError(msg)

    oldapp = Application(repo_url, branch=branch)
    oldmaster = oldapp.master_node
    oldslave = oldapp.slave_node
    members = oldapp.members

    if oldmaster == myself:  # master ->
        log.info('I was the master of %s', oldapp.name)
        oldapp.down()
        oldapp.unregister_consul()
        if oldslave:
            oldapp.enable_replicate(False, members[oldslave]['ip'])
        else:
            oldapp.enable_snapshot(False)
        oldapp.enable_purge(False)
        oldapp.unregister_kv()
        for volume in oldapp.volumes:
            volume.snapshot()
        oldapp.down(deletevolumes=True)
        oldapp.clean()
    elif oldslave == myself:  # slave ->
        log.info("I was the slave of %s", oldapp.name)
        oldapp.enable_purge(False)
    else:  # nothing ->
        log.info("I was nothing for %s", oldapp.name)
    log.info("Successfully destroyed")


def migrate(payload, myself):
    """migrate volumes from one app to another. Needs:
    {"repo"': <url>, "branch": <branch>,
     "target": {"repo": <url>, "branch": <branch>}}
    If the "repo" or "branch" of the "target" is not given, it is the same as
    the source
    """
    repo_url = payload['repo']
    branch = payload['branch']
    target = payload['target']
    assert(target.get('repo') or target.get('branch'))

    sourceapp = Application(repo_url, branch=branch)
    targetapp = Application(target.get('repo', repo_url),
                            branch=target.get('branch', branch))
    if sourceapp.master_node != myself and targetapp.master_node != myself:
        log.info('Not concerned by this event')
        return
    source_volumes = []
    target_volumes = []
    # find common volumes
    for source_volume in sourceapp.volumes:
        source_name = source_volume.name.split('_', 1)[1]
        for target_volume in targetapp.volumes:
            target_name = target_volume.name.split('_', 1)[1]
            if source_name == target_name:
                source_volumes.append(source_volume)
                target_volumes.append(target_volume)
            else:
                continue
    log.info('Found %s volumes to restore: %s',
             len(source_volumes), repr([v.name for v in source_volumes]))
    # tranfer and restore volumes
    if sourceapp.master_node != targetapp.master_node:
        if sourceapp.master_node == myself:
            with sourceapp.notify_transfer():
                for volume in source_volumes:
                    volume.send(
                        volume.snapshot(),
                        targetapp.members[targetapp.master_node]['ip'])
        if targetapp.master_node == myself:
            sourceapp.wait_transfer()
    if targetapp.master_node == myself:
        targetapp.down()
        for source_vol, target_vol in zip(source_volumes, target_volumes):
            source_vol.restore(target=target_vol.name)
        targetapp.up()
    log.info('Restored %s to %s', sourceapp.name, targetapp.name)


if __name__ == '__main__':
    logging.basicConfig(level=logging.DEBUG,
                        format='{asctime}\t{levelname}\t{message}',
                        filename=join(DEPLOY, 'handler.log'),
                        style='{')
    myself = socket.gethostname()
    manual_input = None
    if len(argv) >= 3:
        # allow to launch manually inside consul docker
        event = argv[1]
        payload = b64encode(' '.join(argv[2:]).encode('utf-8')).decode('utf-8')
        manual_input = json.dumps({
            'ID': str(uuid1()), 'Name': event,
            'Payload': payload, 'Version': 1, 'LTime': 1})

    handle(manual_input or stdin.read(), myself)
