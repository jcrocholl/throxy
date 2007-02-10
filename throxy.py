import asyncore
import socket
import re

__revision__ = '$Rev$'

request_match = re.compile(r'^(GET) (\S+) (HTTP/\S+)$').match
host_match = re.compile(r'^Host: (\S+)$').match


class ProxyServer(asyncore.dispatcher):

    def __init__(self, interface='', port=8080):
        asyncore.dispatcher.__init__(self)
        self.port = port
        self.create_socket(socket.AF_INET, socket.SOCK_STREAM)
        self.bind((interface, port))
        self.listen(5)
        print "listening on port", port

    def handle_accept(self):
        channel, addr = self.accept()
        ClientChannel(channel, addr)


class ClientChannel(asyncore.dispatcher):

    def __init__(self, channel, addr):
        asyncore.dispatcher.__init__(self, channel)
        self.addr = addr
        self.incomplete_input = ''
        self.request = []
        self.buffer = []
        print "client %s:%d connected" % self.addr

    def handle_read(self):
        data = self.recv(8192)
        if data:
            lines = data.split('\n')
            if len(lines) == 1:
                self.incomplete_input += lines[0]
            else:
                self.handle_request_line(self.incomplete_input + lines[0])
                for line in lines[1:-1]:
                    self.handle_request_line(line)
                self.incomplete_input = lines[-1]

    def handle_request_line(self, line):
        line = line.rstrip('\r')
        if line:
            # print "client %s:%d said" % self.addr, repr(line)
            self.request.append(line)
        else:
            # print '#####################################'
            # print '\n'.join(self.request)
            # print '#####################################'
            ServerChannel(self, self.request)
            self.request = []

    def writable(self):
        return len(self.buffer)

    def handle_write(self):
        sent = self.send(self.buffer[0])
        if sent == len(self.buffer[0]):
            self.buffer.pop(0)
        else:
            self.buffer[0] = self.buffer[0][sent:]

    def handle_close(self):
        self.close()
        print "client %s:%d disconnected" % self.addr


class ServerChannel(asyncore.dispatcher):

    def __init__(self, client, request):
        asyncore.dispatcher.__init__(self)
        self.client = client
        self.extract_host(request)
        self.extract_path(request)
        self.create_socket(socket.AF_INET, socket.SOCK_STREAM)
        self.connect(self.addr)
        self.buffer = []
        self.send_request(request)

    def extract_host(self, request):
        for line in request:
            match = host_match(line)
            if match:
                self.host = match.group(1)
                break
        else:
            raise ValueError("host header missing")
        print "client %s:%d wants to talk to" % self.client.addr, self.host
        if self.host.count(':'):
            self.host_name, self.host_port = self.host.split(':')
        else:
            self.host_name = self.host
            self.host_port = 80
        self.host_ip = socket.gethostbyname(self.host_name)
        self.addr = (self.host_ip, self.host_port)

    def extract_path(self, request):
        match = request_match(request[0])
        if not match:
            raise ValueError("invalid request " + request[0])
        self.method, self.url, self.proto = match.groups()
        prefix = 'http://' + self.host
        if not self.url.startswith(prefix):
            raise ValueError("URL doesn't start with " + prefix)
        self.path = self.url[len(prefix):]

    def send_request(self, request):
        print '#####################################'
        self.send_line(' '.join((self.method, self.path, self.proto)))
        self.send_line('Host: ' + self.host)
        for line in request[1:]:
            if not line.startswith('Host: '):
                self.send_line(line)
        print '#####################################'

    def send_line(self, line):
        print line
        self.buffer.append(line + '\r\n')

    def writable(self):
        return len(self.buffer)

    def handle_write(self):
        sent = self.send(self.buffer[0])
        if sent == len(self.buffer[0]):
            self.buffer.pop(0)
        else:
            self.buffer[0] = self.buffer[0][sent:]

    def handle_read(self):
        data = self.recv(8192)
        if data:
            self.client.buffer.append(data)

    def handle_connect(self):
        print "server %s:%d connected" % self.addr

    def handle_close(self):
        self.close()
        print "server %s:%d disconnected" % self.addr


def _main():
    from optparse import OptionParser
    version = '%prog ' + __revision__.strip('$').replace('Rev: ', 'r')
    parser = OptionParser(version=version)
    parser.add_option("-i", "--interface", action="store", type="string",
                      metavar="<ip>", default="",
                      help="listen on this interface (default 127.0.0.1)")
    parser.add_option("-p", "--port", action="store", type="int",
                      metavar="<number>", default=8080,
                      help="listen on this port number (default 8080)")
    options, args = parser.parse_args()
    server = ProxyServer(options.interface, options.port)
    try:
        asyncore.loop()
    except:
        server.shutdown(2)
        server.close()
        raise


if __name__ == '__main__':
    _main()
