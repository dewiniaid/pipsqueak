import pydle
import configparser
import re




class Configuration:
    nicknames = None
    channels = []
    servers = []
    verify_ssl = True
    realname = None
    username = None
    tls_client_cert = None
    tls_client_cert_key = None
    tls_client_cert_password = None

    def __init__(self, filename=None, data=None):
        self._parser = configparser.ConfigParser()
        if data:
            self.read_data(data)
        if filename:
            self.read_file(filename)
        self.configure()

    def read_file(self, filename):
        self._parser.read(filename)

    def read_data(self, data):
        if isinstance(data, str):
            self._parser.read_string(data)
        elif isinstance(data, dict):
            self._parser.read_string(data)

    def configure(self):
        p = self._parser
        main = p['main']
        self.nicknames = re.split(r'[\s+,]', main.get('nick', '').strip())
        self.verify_ssl = main.getboolean('verify_ssl', True)
        self.realname = main.get('realname', self.nicknames[0])
        self.username = main.get('username', self.nicknames[0])
        servers = []
        for server in re.split(r',+', main.get('server', '')):
            server = server.strip()
            if not server:
                continue
            d = {'port': '6667'}
            d.update(zip(('hostname', 'port'), re.split(r'[/:]', server, 1)))
            d['tls'] = (d['port'][0] == '+')
            d['port'] = int(d['port'])
            servers.append(d)

        channels = []
        for channel in re.split(r',+', main.get('channels', '')):
            channel = channel.strip()
            if not channel:
                continue
            d = {'password': None}
            d.update(zip(('channel', 'password'), channel.split('=', 1)))
            channels.append(d)

        for attr in (
            'auth_method', 'auth_username', 'auth_password',
            'tls_client_cert', 'tls_client_cert_key', 'tls_client_cert_password'
        ):
            setattr(self, attr, main.get(attr))


class Ratbot(pydle.Client, pydle.BasicClient, pydle.features.TLSSupport):
    def __init__(self, config=None, filename=None, data=None, **kwargs):
        self.server_index = -1

        if config is None:
            config = Configuration(filename=filename, data=data)
        self.config = config

        kwargs.setdefault('nickname', config.nicknames[0])
        kwargs.setdefault('fallback_nickname', config.nicknames[1:])
        for attr in (
            'tls_client_cert', 'tls_client_cert_key', 'tls_client_cert_password',
            'username', 'realname'
        ):
            kwargs.setdefault(attr, getattr(config, attr))
        super.__init__(**kwargs)

    def connect(self, hostname=None, **kwargs):
        kwargs['hostname'] = hostname
        if hostname is None and self.config.servers:
            self.server_index += 1
            if self.server_index >= len(self.config.servers):
                self.server_index = 0
                kwargs.update(self.config.servers[self.server_index])
        return super().connect(**kwargs)

    def on_connect(self):
        super().on_connect()
        for channel in self.config.channels:
            try:
                self.join(**channel)
            except pydle.AlreadyInChannel:
                pass

