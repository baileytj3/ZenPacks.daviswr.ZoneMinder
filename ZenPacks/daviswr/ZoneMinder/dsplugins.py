"""Monitors the ZoneMinder daemon using its JSON API"""

import logging
LOG = logging.getLogger('zen.ZoneMinder')

import json
import re
import urllib

from twisted.internet.defer \
    import inlineCallbacks, returnValue
from twisted.web.client \
    import getPage

from ZenPacks.zenoss.PythonCollector.datasources.PythonDataSource \
    import PythonDataSourcePlugin


class Daemon(PythonDataSourcePlugin):
    """ZoneMinder daemon data source plugin"""

    @classmethod
    def config_key(cls, datasource, context):
        return(
            context.device().id,
            datasource.getCycleTime(context),
            context.id,
            'zoneminder-daemon',
            )

    @classmethod
    def params(cls, datasource, context):
        return {
            'username': context.zZoneMinderUsername,
            'password': context.zZoneMinderPassword,
            'hostname': context.zZoneMinderHostname,
            'port': context.zZoneMinderPort,
            'path': context.zZoneMinderPath,
            'ssl': context.zZoneMinderSSL,
            'base_url': context.zZoneMinderURL,
            }

    @inlineCallbacks
    def collect(self, config):
        data = self.new_data()

        for datasource in config.datasources:
            # LOG.debug('%s: parameters\n%s', config.id, datasource.params)
            username = datasource.params['username']
            password = datasource.params['password']
            hostname = datasource.params['hostname']
            port = datasource.params['port']
            path = datasource.params['path']
            ssl = datasource.params['ssl']
            base_url = datasource.params['base_url']

            if not username or not password:
                LOG.error(
                    '%s: zZoneMinderUsername or zZoneMinderPassword not set',
                    config.id
                    )
                returnValue(None)

             # If custom URL not provided, assemble one
            if not base_url:
                if not hostname:
                    hostname = config.id
                    if '.' not in hostname:
                        hostname = hostname.replace('_', '.')
                port_str = ':' + str(port) if port else ''
                if not path.startswith('/'):
                    path = '/' + path
                if not path.endswith('/'):
                    path = path + '/'
                protocol = 'https' if ssl else 'http'
                base_url = '{0}://{1}{2}{3}'.format(
                    protocol,
                    hostname,
                    port_str,
                    path
                    )

            url_regex = r'^https?:\/\/\S+:?\d*\/?\S*\/$'
            if re.match(url_regex, base_url) is None:
                LOG.error('%s: %s is not a valid URL', config.id, base_url)
                returnValue(None)
            else:
                LOG.debug(
                    '%s: using base ZoneMinder URL %s',
                    config.id,
                    base_url
                    )

            login_params = urllib.urlencode({
                'action': 'login',
                'view': 'login',
                'username': username,
                'password': password,
                # 1.34+ requires OPT_USE_LEGACY_API_AUTH
                'stateful': 1,
                })
            login_url = '{0}index.php?{1}'.format(base_url, login_params)
            api_url = '{0}api/'.format(base_url)

            cookies = dict()
            try:
                # Attempt login
                login_response = yield getPage(
                    login_url,
                    method='POST',
                    cookies=cookies
                    )

                if 'Invalid username or password' in login_response:
                    LOG.error(
                        '%s: ZoneMinder login credentials invalid',
                        config.id,
                        )
                    returnValue(None)
                elif len(cookies) == 0:
                    LOG.error('%s: No cookies received', config.id)
                    returnValue(None)

                output = dict()

                # Console
                # Session cookies on 1.34 require view=login on action=login
                # This returns a 302 to the console page
                # rather than just the console
                response = yield getPage(
                    '{0}index.php?view=console'.format(base_url),
                    method='GET',
                    cookies=cookies
                    )

                # Scrape disk and (/dev/shm|/run/shm) utilization from HTML
                stats_130_regex = r'Load.?\s+\d+\.\d+.*Disk.?\s+(\d+)%?.*\/w+\/shm.?\s(\d+)%?'  # noqa
                stats_132_regex = r'Storage.?\s+(\d+)%?<?\/?[span]*>?.*\/\w+\/shm.?\s+(\d+)%?'  # noqa
                storage_regex = r'Storage.?\s+(\d+)%?'
                shm_regex = r'/\w+\/shm.?\s+(\d+)%?'
                match = (re.search(stats_130_regex, response)
                         or re.search(stats_132_regex, response))
                if match:
                    output['console'] = match.groups()
                else:
                    storage_match = re.search(storage_regex, response)
                    shm_match = re.search(shm_regex, response)
                    output['console'] = [
                        storage_match.groups()[0] if storage_match else '',
                        shm_match.groups()[0] if shm_match else '',
                        ]

                # Scrape total capture bandwidth from HTML
                bandwidth_regex = r'<td class="colFunction">(\S+)B\/s'
                match = re.search(bandwidth_regex, response)
                if match:
                    bandwidth_str = match.groups()[0]
                    if bandwidth_str[-1] not in '0123456789':
                        unit_multi = {
                            'K': 1000,
                            'M': 1000000,
                            'G': 1000000000,
                            }
                        bandwidth = float(bandwidth_str[:-1])
                        bandwidth = bandwidth * unit_multi.get(
                            bandwidth_str[-1],
                            1
                            )
                    else:
                        bandwidth = float(bandwidth_str)
                    output['bandwidth'] = bandwidth

                # Daemon status
                response = yield getPage(
                    api_url + 'host/daemonCheck.json',
                    method='GET',
                    cookies=cookies
                    )
                output.update(json.loads(response))

                # Run state
                response = yield getPage(
                    api_url + 'states.json',
                    method='GET',
                    cookies=cookies
                    )
                output.update(json.loads(response))

                # Host Load
                response = yield getPage(
                    api_url + 'host/getLoad.json',
                    method='GET',
                    cookies=cookies
                    )
                output.update(json.loads(response))

                # Five-minute event counts
                response = yield getPage(
                    api_url + 'events/consoleEvents/300%20second.json',
                    method='GET',
                    cookies=cookies
                    )
                output.update(json.loads(response))

                # Log out
                yield getPage(
                    base_url + 'index.php?action=logout',
                    method='POST',
                    cookies=cookies
                    )
            except Exception, e:
                LOG.exception('%s: failed to get daemon data', config.id)
                continue

            LOG.debug('%s: ZM daemon output:\n%s', config.id, output)

            stats = dict()
            # Daemon status ("result")
            stats['result'] = output.get('result', '0')

            states = output.get('states', list())
            if len(states) > 0:
                for state in states:
                    if state.get('State', dict()).get('IsActive', '0') == '1':
                        stats['state'] = state['State']['Id']
                        break

            load = output.get('load', list())
            if len(load) >= 3:
                (stats['load-1'], stats['load-5'], stats['load-15']) = load

            console = output.get('console', list())
            if len(console) >= 2:
                (stats['disk'], stats['devshm']) = console

            if 'bandwidth' in output:
                stats['bandwidth'] = output.get('bandwidth')

            # Event counts ("results", plural)
            events = output.get('results', list())
            stats['events'] = 0
            # "results" will be an empty *list* if no monitors have events
            if len(events) > 0:
                for key in events.keys():
                    stats['events'] += int(events.get(key, 0))

            for datapoint_id in (x.id for x in datasource.points):
                if datapoint_id not in stats:
                    continue

                try:
                    if datapoint_id.startswith('load-'):
                        value = float(stats.get(datapoint_id))
                    else:
                        value = int(stats.get(datapoint_id))
                except (TypeError, ValueError):
                    continue

                dpname = '_'.join((datasource.datasource, datapoint_id))
                data['values'][datasource.component][dpname] = (value, 'N')

        returnValue(data)


class Monitor(PythonDataSourcePlugin):
    """ZoneMinder monitor data source plugin"""

    @classmethod
    def config_key(cls, datasource, context):
        return(
            context.device().id,
            datasource.getCycleTime(context),
            context.id,
            'zoneminder-monitor',
            )

    @classmethod
    def params(cls, datasource, context):
        return {
            'username': context.zZoneMinderUsername,
            'password': context.zZoneMinderPassword,
            'hostname': context.zZoneMinderHostname,
            'port': context.zZoneMinderPort,
            'path': context.zZoneMinderPath,
            'ssl': context.zZoneMinderSSL,
            'base_url': context.zZoneMinderURL,
            }

    @inlineCallbacks
    def collect(self, config):
        data = self.new_data()

        url_regex = r'^https?:\/\/\S+:?\d*\/?\S*\/$'
        online_regex = r'<td class="colSource">.*<span class="(\w+)Text">'
        online_map = {
            'error': 0,
            'info': 1,
            }

        for datasource in config.datasources:
            # LOG.debug('%s: parameters\n%s', config.id, datasource.params)
            username = datasource.params['username']
            password = datasource.params['password']
            hostname = datasource.params['hostname']
            port = datasource.params['port']
            path = datasource.params['path']
            ssl = datasource.params['ssl']
            base_url = datasource.params['base_url']
            comp_id = datasource.component.replace('zmMonitor', '')

            if not username or not password:
                LOG.error(
                    '%s: zZoneMinderUsername or zZoneMinderPassword not set',
                    config.id
                    )
                returnValue(None)

             # If custom URL not provided, assemble one
            if not base_url:
                if not hostname:
                    hostname = config.id
                    if '.' not in hostname:
                        hostname = hostname.replace('_', '.')
                port_str = ':' + str(port) if port else ''
                if not path.startswith('/'):
                    path = '/' + path
                if not path.endswith('/'):
                    path = path + '/'
                protocol = 'https' if ssl else 'http'
                base_url = '{0}://{1}{2}{3}'.format(
                    protocol,
                    hostname,
                    port_str,
                    path
                    )

            if re.match(url_regex, base_url) is None:
                LOG.error('%s: %s is not a valid URL', config.id, base_url)
                returnValue(None)
            else:
                LOG.debug(
                    '%s: using base ZoneMinder URL %s',
                    config.id,
                    base_url
                    )

            login_params = urllib.urlencode({
                'action': 'login',
                'view': 'login',
                'username': username,
                'password': password,
                # 1.34+ requires OPT_USE_LEGACY_API_AUTH
                'stateful': 1,
                })
            login_url = '{0}index.php?{1}'.format(base_url, login_params)
            api_url = '{0}api/'.format(base_url)
            mon_url = 'monitors/daemonStatus/id:{0}/daemon:zmc.json'.format(
                comp_id
                )

            cookies = dict()
            try:
                # Attempt login
                login_response = yield getPage(
                    login_url,
                    method='POST',
                    cookies=cookies
                    )

                output = dict()

                if 'Invalid username or password' in login_response:
                    LOG.error(
                        '%s: ZoneMinder login credentials invalid',
                        config.id,
                        )
                    returnValue(None)
                elif len(cookies) == 0:
                    LOG.error('%s: No cookies received', config.id)
                    returnValue(None)

                # Console
                # Session cookies on 1.34 require view=login on action=login
                # This returns a 302 to the console page
                # rather than just the console
                response = yield getPage(
                    '{0}index.php?view=console'.format(base_url),
                    method='GET',
                    cookies=cookies
                    )

                # Scrape monitor online status from HTML
                if 'zmWatch' in response:
                    # 1.30
                    watch_prefix = 'zmWatch'
                    watch_offset = 2
                elif 'zmMonitor' in response:
                    # 1.34
                    watch_prefix = 'zmMonitor'
                    watch_offset = 0
                elif 'monitor_id-' in response:
                    # 1.32
                    watch_prefix = 'monitor_id-'
                    watch_offset = 9
                else:
                    watch_prefix = ''

                watch_id = watch_prefix + comp_id

                if watch_id in response:
                    watch_index = -1
                    console = response.split('\n')
                    for ii in range(0, len(console) - 1):
                        if watch_id in console[ii]:
                            watch_index = ii
                            break
                    if watch_index > -1:
                        online_line = console[watch_index + watch_offset]
                        online_match = re.search(online_regex, online_line)
                        if online_match:
                            online_state = online_match.groups()[0]
                            output['online'] = online_map.get(online_state, 2)

                else:
                    LOG.warn(
                        '%s: %s not found in ZM web console',
                        config.id,
                        datasource.component
                        )

                # Monitor enabled
                response = yield getPage(
                    api_url + 'monitors/{0}.json'.format(comp_id),
                    method='GET',
                    cookies=cookies
                    )
                output.update(json.loads(response))

                # Monitor process status
                response = yield getPage(
                    api_url + mon_url,
                    method='GET',
                    cookies=cookies
                    )
                output.update(json.loads(response))

                # Five-minute event counts
                response = yield getPage(
                    api_url + 'events/consoleEvents/300%20second.json',
                    method='GET',
                    cookies=cookies
                    )
                output.update(json.loads(response))

                # Log out
                yield getPage(
                    base_url + 'index.php?action=logout',
                    method='POST',
                    cookies=cookies
                    )
            except Exception, e:
                LOG.exception('%s: failed to get monitor data', config.id)
                continue

            LOG.debug('%s: ZM monitor output:\n%s', config.id, output)

            stats = dict()

            if 'online' in output:
                stats['online'] = output['online']

            monitor = output.get('monitor', dict()).get('Monitor', dict())

            if len(monitor) > 0:
                stats['enabled'] = monitor.get('Enabled', '0')

            # 1.30 Framerates
            if 'CaptureFPS' in monitor:
                stats['CaptureFPS'] = monitor['CaptureFPS']
            if 'AnalysisFPS' in monitor:
                stats['AnalysisFPS'] = monitor['AnalysisFPS']

            # 1.32 Monitor Status
            stats.update(output.get('monitor', dict()).get(
                'Monitor_Status',
                dict()
                ))

            # 1.30
            stats['status'] = 1 if output.get('status') else 0
            # 1.32
            stats['status'] = 1 if stats.get('Status', '') == 'Connected' \
                else 0

            events = output.get('results', list())
            # "results" will be an empty *list* if no monitors have events
            if len(events) > 0:
                stats['events'] = int(events.get(comp_id, 0))
            else:
                stats['events'] = 0

            for datapoint_id in (x.id for x in datasource.points):
                if datapoint_id not in stats:
                    continue

                value = stats.get(datapoint_id)
                dpname = '_'.join((datasource.datasource, datapoint_id))
                data['values'][datasource.component][dpname] = (value, 'N')

        returnValue(data)
