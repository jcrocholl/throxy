#! /usr/bin/env python
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
Throxy (Throttling Proxy)

This is a simple HTTP proxy, with the following features:

* Simulates a slow connection (like dial-up).
* Optionally dumps HTTP headers and content for debugging.
* Supports multiple connections, without threading.
* Written in pure Python.

To use it, run this script on your local machine and adjust your
browser settings to use 127.0.0.1:8080 as HTTP proxy.
"""

import sys
import asyncore
import socket
import time
import re

__revision__ = '$Rev$'

KILO = 1000 # decimal or binary kilo
MIN_FRAGMENT_SIZE = 512 # bytes

request_match = re.compile(r'^([A-Z]+) (\S+) (HTTP/\S+)$').match


class Header:

    def __init__(self):
        self.data = ''
        self.lines = []
        self.complete = False

    def append(self, new_data):
        self.data += new_data
        while not self.complete:
            newline = self.data.find('\n')
            if newline < 0:
                break # No complete line found.
            line = self.data[:newline].rstrip('\r')
            if len(line):
                self.lines.append(line)
            else:
                self.complete = True
            self.data = self.data[newline+1:]
        if self.complete:
            # Give remaining data back to caller. It may contain
            # content, or even the start of the next request.
            rest = self.data
            self.data = ''
            return rest
        else:
            return ''

    def extract_header(self, name, default=None):
        name = name.lower()
        for line in self.lines:
            if not line.count(':'):
                continue
            key, value = line.split(':', 1)
            if key.lower() == name:
                return value.strip()
        return default

    def extract_host(self):
        self.host = self.extract_header('Host')
        if self.host is None:
            return
        # print "client %s:%d wants to talk to" % self.client.addr, self.host
        if self.host.count(':'):
            self.host_name, self.host_port = self.host.split(':')
        else:
            self.host_name = self.host
            self.host_port = 80
        self.host_ip = socket.gethostbyname(self.host_name)
        self.host_addr = (self.host_ip, self.host_port)

    def extract_request(self):
        match = request_match(self.lines[0])
        if not match:
            raise ValueError("malformed request line " + self.lines[0])
        self.method, self.url, self.proto = match.groups()
        if self.method.upper() == 'CONNECT':
            raise ValueError("method CONNECT is not supported")
        prefix = 'http://' + self.host
        if not self.url.startswith(prefix):
            raise ValueError("URL doesn't start with " + prefix)
        self.path = self.url[len(prefix):]

    def dump_title(self, from_addr, to_addr, direction, what):
        print '==== %s %s (%s:%d => %s:%d) ====' % (
            direction, what,
            from_addr[0], from_addr[1],
            to_addr[0], to_addr[1])

    def dump(self, from_addr, to_addr, direction='sending'):
        self.dump_title(from_addr, to_addr, direction, 'headers')
        print '\n'.join(self.lines)
        print

    def dump_content(self, content, from_addr, to_addr, direction='sending'):
        self.dump_title(from_addr, to_addr, direction, 'content')
        print repr(content)
        print


class ThrottleSender(asyncore.dispatcher):

    def __init__(self, kbps, channel=None):
        if channel is None:
            asyncore.dispatcher.__init__(self)
        else:
            asyncore.dispatcher.__init__(self, channel)
        self.interval = 1.0
        self.bytes_per_second = int(kbps * KILO) / 8
        self.transmit_log = []
        self.buffer = []

    def log_sent_bytes(self, bytes):
        self.transmit_log.append((time.time(), bytes))

    def trim_log(self, horizon):
        while len(self.transmit_log) and self.transmit_log[0][0] <= horizon:
            self.transmit_log.pop(0)

    def weighted_bytes(self):
        """Compute recent bandwidth usage, in bytes per second."""
        now = time.time()
        self.trim_log(now - self.interval)
        if len(self.transmit_log) == 0:
            return 0
        weighted = 0.0
        for timestamp, bytes in self.transmit_log:
            age = now - timestamp # Event's age in seconds.
            assert 0 <= age <= self.interval
            weight = 2.0 * (self.interval - age) / self.interval
            assert 0.0 <= weight <= 2.0
            weighted += weight * bytes # Newer entries count more.
        return int(weighted / self.interval)

    def weighted_kbps(self):
        """Compute recent bandwidth usage, in kbps."""
        return 8 * self.weighted_bytes() / float(KILO)

    def sendable(self):
        """How many bytes can we send without exceeding bandwidth?"""
        return max(0, self.bytes_per_second - self.weighted_bytes())

    def writable(self):
        return (len(self.buffer) and
                self.sendable() / 2 > MIN_FRAGMENT_SIZE)

    def handle_write(self):
        max_bytes = self.sendable() / 2
        if max_bytes < MIN_FRAGMENT_SIZE:
            return
        # print "sendable", max_bytes
        bytes = self.send(self.buffer[0][:max_bytes])
        self.log_sent_bytes(bytes)
        if bytes == len(self.buffer[0]):
            self.buffer.pop(0)
        else:
            self.buffer[0] = self.buffer[0][bytes:]


class ClientChannel(ThrottleSender):

    def __init__(self, channel, addr):
        ThrottleSender.__init__(self, options.download, channel)
        self.addr = addr
        self.header = Header()
        self.content_length = 0
        self.server = None
        self.handle_connect()

    def readable(self):
        return self.server is None or len(self.server.buffer) == 0

    def handle_read(self):
        data = self.recv(8192)
        while len(data):
            if self.content_length:
                bytes = min(self.content_length, len(data))
                self.server.buffer.append(data[:bytes])
                if options.dump_send_content:
                    self.header.dump_content(
                        data[:bytes], self.addr, self.header.host_addr)
                data = data[bytes:]
                self.content_length -= bytes
                if self.content_length == 0:
                    self.header = Header()
                    self.server = None
            if not len(data):
                break
            data = self.header.append(data)
            if self.header.complete:
                self.content_length = int(
                    self.header.extract_header('Content-Length', 0))
                self.header.extract_host()
                if options.dump_send_headers:
                    self.header.dump(self.addr, self.header.host_addr)
                self.server = ServerChannel(self, self.header)

    def handle_connect(self):
        if not options.quiet:
            print >> sys.stderr, "client %s:%d connected" % self.addr

    def handle_close(self):
        self.close()
        if not options.quiet:
            print >> sys.stderr, "client %s:%d disconnected" % self.addr


class ServerChannel(ThrottleSender):

    def __init__(self, client, header):
        ThrottleSender.__init__(self, options.upload)
        self.client = client
        self.addr = header.host_addr
        self.create_socket(socket.AF_INET, socket.SOCK_STREAM)
        self.connect(self.addr)
        self.send_header(header)
        self.header = Header()

    def send_header(self, header):
        header.extract_request()
        self.send_line(' '.join(
            (header.method, header.path, header.proto)))
        self.send_line('Connection: close')
        for line in header.lines[1:]:
            if not (line.startswith('Keep-Alive: ') or
                    line.startswith('Connection: ') or
                    line.startswith('Proxy-')):
                self.send_line(line)
        self.send_line('')

    def send_line(self, line):
        self.buffer.append(line + '\r\n')

    def readable(self):
        return len(self.client.buffer) == 0

    def handle_read(self):
        data = self.recv(8192)
        if not self.header.complete:
            data = self.header.append(data)
            if self.header.complete and options.dump_recv_headers:
                self.header.dump(self.addr, self.client.addr, 'receiving')
        if self.header.complete:
            self.client.buffer.append(data)

    def handle_connect(self):
        if not options.quiet:
            print >> sys.stderr, "server %s:%d connected" % self.addr

    def handle_close(self):
        self.client.should_close = True
        self.close()
        if not options.quiet:
            print >> sys.stderr, "server %s:%d disconnected" % self.addr


class ProxyServer(asyncore.dispatcher):

    def __init__(self):
        asyncore.dispatcher.__init__(self)
        self.create_socket(socket.AF_INET, socket.SOCK_STREAM)
        self.bind((options.interface, options.port))
        self.listen(5)
        print >> sys.stderr, "listening on port", options.port

    def handle_accept(self):
        channel, addr = self.accept()
        if addr[0] == '127.0.0.1' or options.allow_remote:
            ClientChannel(channel, addr)
        else:
            channel.close()
            print >> sys.stderr, "remote client %s:%d not allowed" % addr


if __name__ == '__main__':
    from optparse import OptionParser
    version = '%prog ' + __revision__.strip('$').replace('Rev: ', 'r')
    parser = OptionParser(version=version)
    parser.add_option('-i', dest='interface', action='store', type='string',
                      metavar='<ip>', default='',
                      help="listen on this interface only (default all)")
    parser.add_option('-p', dest='port', action='store', type='int',
                      metavar='<port>', default=8080,
                      help="listen on this port number (default 8080)")
    parser.add_option('-d', dest='download', action='store', type='float',
                      metavar='<kbps>', default=28.8,
                      help="download bandwidth in kbps (default 28.8)")
    parser.add_option('-u', dest='upload', action='store', type='float',
                      metavar='<kbps>', default=28.8,
                      help="upload bandwidth in kbps (default 28.8)")
    parser.add_option('-o', dest='allow_remote', action='store_true',
                      help="allow remote clients (WARNING: open proxy)")
    parser.add_option('-q', dest='quiet', action='store_true',
                      help="don't show connect and disconnect messages")
    parser.add_option('-s', dest='dump_send_headers', action='store_true',
                      help="dump headers sent to server")
    parser.add_option('-r', dest='dump_recv_headers', action='store_true',
                      help="dump headers received from server")
    parser.add_option('-S', dest='dump_send_content', action='store_true',
                      help="dump content sent to server")
    parser.add_option('-R', dest='dump_recv_content', action='store_true',
                      help="dump content received from server")
    options, args = parser.parse_args()
    proxy = ProxyServer()
    try:
        asyncore.loop(timeout=0.1)
    except:
        proxy.shutdown(2)
        proxy.close()
        raise
