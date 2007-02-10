# throxy.py - HTTP proxy to simulate dial-up access
# Copyright (C) 2007 Johann C. Rocholl <johann@rocholl.net>
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation files
# (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS
# BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
# ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

"""
Throxy = Throttling Proxy.

This is a simple HTTP proxy, written in pure Python, that simulates a
slow connection, as you would typically find on dial-up access.

To use it, run this script on your local machine and adjust your
browser settings to use 127.0.0.1:8080 as HTTP proxy.
"""

import asyncore
import socket
import time
import re

__revision__ = '$Rev$'

KILO = 1000 # decimal or binary kilo

request_match = re.compile(r'^(GET) (\S+) (HTTP/\S+)$').match
host_match = re.compile(r'^Host: (\S+)$').match


class BandwidthMonitor:

    def __init__(self,
                 receive_bytes_per_second=3600,
                 send_bytes_per_second=3600,
                 interval=1.0):
        self.recv_bps = receive_bytes_per_second
        self.send_bps = send_bytes_per_second
        self.interval = interval
        self.recv_log = []
        self.send_log = []

    def log_received_bytes(self, bytes):
        self.recv_log.append((time.time(), bytes))

    def log_sent_bytes(self, bytes):
        self.send_log.append((time.time(), bytes))

    def trim_log(self, log, horizon):
        while len(log) and log[0][0] <= horizon:
            log.pop(0)

    def weighted_bytes(self, log):
        """Compute recent bandwidth usage, in bytes per second."""
        now = time.time()
        self.trim_log(log, now - self.interval)
        if len(log) == 0:
            return 0
        weighted = 0.0
        for timestamp, bytes in log:
            age = now - timestamp # event's age in seconds
            assert 0 <= age <= self.interval
            weight = 2.0 * (self.interval - age) / self.interval
            assert 0.0 <= weight <= 2.0
            weighted += weight * bytes # newer entries count more
        return int(weighted / self.interval)

    def sendable(self):
        """How many bytes can we send without exceeding bandwidth?"""
        return max(0, self.send_bps - self.weighted_bytes(self.send_log))

    def receivable(self):
        """How many bytes can we receive without exceeding bandwidth?"""
        return max(0, self.recv_bps - self.weighted_bytes(self.recv_log))

    def weighted_kbps(self, log):
        """Compute bandwidth usage, in kbps."""
        return 8 * self.weighted_bytes(log) / float(KILO)

    def receiving_kpbs(self):
        """Compute download bandwidth usage, in kbps."""
        return self.weighted_kbps(self.recv_log)

    def sending_kpbs(self):
        """Compute upload bandwidth usage, in kbps."""
        return self.weighted_kbps(self.send_log)


class ProxyServer(asyncore.dispatcher):

    def __init__(self, interface, port, monitor, allow_remote=False):
        asyncore.dispatcher.__init__(self)
        self.interface = interface
        self.port = port
        self.monitor = monitor
        self.allow_remote = allow_remote
        self.create_socket(socket.AF_INET, socket.SOCK_STREAM)
        self.bind((interface, port))
        self.listen(5)
        print "listening on port", port

    def handle_accept(self):
        channel, addr = self.accept()
        if addr[0] == '127.0.0.1' or self.allow_remote:
            ClientChannel(channel, addr, self.monitor)
        else:
            channel.close()
            print "remote client %s:%d not allowed" % addr


class ClientChannel(asyncore.dispatcher):

    def __init__(self, channel, addr, monitor):
        asyncore.dispatcher.__init__(self, channel)
        self.addr = addr
        self.monitor = monitor
        self.incomplete_input = ''
        self.request = []
        self.buffer = []
        print "client %s:%d connected" % self.addr

    def readable(self):
        return self.monitor.receivable()

    def handle_read(self):
        max_bytes = self.monitor.receivable()
        print "receivable", max_bytes
        data = self.recv(max_bytes)
        if len(data):
            self.monitor.log_received_bytes(len(data))
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
        return len(self.buffer) and self.monitor.sendable()

    def handle_write(self):
        max_bytes = self.monitor.sendable()
        print "sendable", max_bytes
        bytes = self.send(self.buffer[0][:max_bytes])
        self.monitor.log_sent_bytes(bytes)
        if bytes == len(self.buffer[0]):
            self.buffer.pop(0)
        else:
            self.buffer[0] = self.buffer[0][bytes:]

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
        # print "client %s:%d wants to talk to" % self.client.addr, self.host
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
        # print '#####################################'
        self.send_line(' '.join((self.method, self.path, self.proto)))
        self.send_line('Host: ' + self.host)
        self.send_line('Connection: close')
        for line in request[1:]:
            if line.startswith('Host: '):
                pass
            elif line.startswith('Keep-Alive: '):
                pass
            elif line.startswith('Connection: '):
                pass
            elif line.startswith('Proxy-'):
                pass
            else:
                self.send_line(line)
        self.send_line('')
        # print '#####################################'

    def send_line(self, line):
        # print line
        self.buffer.append(line + '\r\n')

    def writable(self):
        return len(self.buffer)

    def handle_write(self):
        bytes = self.send(self.buffer[0])
        if bytes == len(self.buffer[0]):
            # print "sent", repr(self.buffer[0])
            self.buffer.pop(0)
        else:
            # print "sent", repr(self.buffer[0][:bytes])
            self.buffer[0] = self.buffer[0][bytes:]

    def handle_read(self):
        data = self.recv(8192)
        # print "received", repr(data)
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
    parser.add_option('-i', dest='interface', action='store', type='string',
                      metavar='<ip>', default='',
                      help="listen on this interface (default 127.0.0.1)")
    parser.add_option('-p', dest='port', action='store', type='int',
                      metavar='<number>', default=8080,
                      help="listen on this port number (default 8080)")
    parser.add_option('-d', dest='download', action='store', type='float',
                      metavar='<kbps>', default=28.8,
                      help="download bandwidth in kbps (default 28.8)")
    parser.add_option('-u', dest='upload', action='store', type='float',
                      metavar='<kbps>', default=28.8,
                      help="upload bandwidth in kbps (default 28.8)")
    parser.add_option('-R', dest='allow_remote',
                      default=False, action='store_true',
                      help="allow remote clients (WARNING: open proxy)")
    options, args = parser.parse_args()
    monitor = BandwidthMonitor(
        int(options.upload * KILO) / 8,
        int(options.download * KILO) / 8)
    proxy = ProxyServer(options.interface, options.port,
                        monitor, options.allow_remote)
    try:
        asyncore.loop(timeout=0.1)
    except:
        proxy.shutdown(2)
        proxy.close()
        raise


if __name__ == '__main__':
    _main()
