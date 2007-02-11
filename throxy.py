#! /usr/bin/env python
# throxy.py - HTTP proxy to simulate dial-up access
# Copyright (c) 2007 Johann C. Rocholl <johann@browsershots.org>
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
Throxy: throttling HTTP proxy in one Python file

To use it, run this script on your local machine and adjust your
browser settings to use 127.0.0.1:8080 as HTTP proxy.

* Simulate a slow connection (like dial-up).
* Adjustable bandwidth limit for download and upload.
* Optionally dump HTTP headers and content for debugging.
* Decompress gzip content encoding for debugging.
* Multiple connections, without threads (uses asyncore).
* Only one source file, written in pure Python.

Simulate analog modem connection:
$ python throxy.py -u28.8 -d57.6

Show all HTTP headers (request & reply):
$ python throxy.py -qrs

Dump HTTP headers and content to a file, without size limits:
$ python throxy.py -rsRS -l0 -L0 -g0 > dump.txt

Tell command line tools to use the proxy:
$ export http_proxy=127.0.0.1:8080
"""

import sys
import asyncore
import socket
import time
import gzip
import struct
import cStringIO
import re

__revision__ = '$Rev$'

KILO = 1000 # decimal or binary kilo

request_match = re.compile(r'^([A-Z]+) (\S+) (HTTP/\S+)$').match


class Header:
    """HTTP (request or reply) header parser."""

    def __init__(self):
        self.data = ''
        self.lines = []
        self.complete = False

    def append(self, new_data):
        """
        Add more data to the header.

        Any data after the end of the header is returned, as it may
        contain content, or even the start of the next request.
        """
        self.data += new_data
        while not self.complete:
            newline = self.data.find('\n')
            if newline < 0:
                break # No complete line found
            line = self.data[:newline].rstrip('\r')
            if len(line):
                self.lines.append(line)
            else:
                self.complete = True
                self.content_type = self.extract('Content-Type')
                self.content_encoding = self.extract('Content-Encoding')
                if self.content_encoding == 'gzip':
                    self.gzip_data = cStringIO.StringIO()
            self.data = self.data[newline+1:]
        if self.complete:
            rest = self.data
            self.data = ''
            return rest
        else:
            return ''

    def extract(self, name, default=''):
        """Extract a header field."""
        name = name.lower()
        for line in self.lines:
            if not line.count(':'):
                continue
            key, value = line.split(':', 1)
            if key.lower() == name:
                return value.strip()
        return default

    def extract_host(self):
        """Extract host and perform DNS lookup."""
        self.host = self.extract('Host')
        if self.host is None:
            return
        if self.host.count(':'):
            self.host_name, self.host_port = self.host.split(':')
        else:
            self.host_name = self.host
            self.host_port = 80
        self.host_ip = socket.gethostbyname(self.host_name)
        self.host_addr = (self.host_ip, self.host_port)

    def extract_request(self):
        """Extract path from HTTP request."""
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
        """Print a title before dumping headers or content."""
        print '==== %s %s (%s:%d => %s:%d) ====' % (
            direction, what,
            from_addr[0], from_addr[1],
            to_addr[0], to_addr[1])

    def dump(self, from_addr, to_addr, direction='sending'):
        """Dump header lines to stdout."""
        self.dump_title(from_addr, to_addr, direction, 'headers')
        print '\n'.join(self.lines)
        print

    def dump_content(self, content, from_addr, to_addr, direction='sending'):
        """Dump content to stdout."""
        self.dump_title(from_addr, to_addr, direction, 'content')
        if self.content_encoding:
            print "(%d bytes of %s with %s encoding)" % (len(content),
                repr(self.content_type), repr(self.content_encoding))
        else:
            print "(%d bytes of %s)" % (len(content), repr(self.content_type))
        if self.content_encoding == 'gzip':
            if options.gzip_size_limit == 0 or \
                   self.gzip_data.tell() < options.gzip_size_limit:
                self.gzip_data.write(content)
            try:
                content = self.gunzip()
            except IOError, error:
                content = 'Could not gunzip: ' + str(error)
        if self.content_type.startswith('text/'):
            limit = options.text_dump_limit
        elif self.content_type.startswith('application/') and \
                 self.content_type.count('xml'):
            limit = options.text_dump_limit
        else:
            limit = options.data_dump_limit
            content = repr(content)
        if len(content) < limit or limit == 0:
            print content
        else:
            print content[:limit] + '(showing only %d bytes)' % limit
        print

    def gunzip(self):
        """Decompress gzip content."""
        if options.gzip_size_limit and \
               self.gzip_data.tell() > options.gzip_size_limit:
            raise IOError("More than %d bytes" % options.gzip_size_limit)
        self.gzip_data.seek(0) # Seek to start of data
        try:
            gzip_file = gzip.GzipFile(
                fileobj=self.gzip_data, mode='rb')
            result = gzip_file.read()
            gzip_file.close()
        except struct.error:
            raise IOError("Caught struct.error from gzip module")
        self.gzip_data.seek(0, 2) # Seek to end of data
        return result


class ThrottleSender(asyncore.dispatcher):
    """Data connection with send buffer and bandwidth limit."""

    def __init__(self, kbps, channel=None):
        if channel is None:
            asyncore.dispatcher.__init__(self)
        else:
            asyncore.dispatcher.__init__(self, channel)
        self.interval = 1.0
        self.bytes_per_second = int(kbps * KILO) / 8
        self.fragment_size = min(512, self.bytes_per_second / 4)
        self.transmit_log = []
        self.buffer = []

    def log_sent_bytes(self, bytes):
        """Add timestamp and byte count to transmit log."""
        self.transmit_log.append((time.time(), bytes))

    def trim_log(self, horizon):
        """Forget transmit log entries that are too old."""
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
            age = now - timestamp # Event's age in seconds
            assert 0 <= age <= self.interval
            weight = 2.0 * (self.interval - age) / self.interval
            assert 0.0 <= weight <= 2.0
            weighted += weight * bytes # Newer entries count more
        return int(weighted / self.interval)

    def weighted_kbps(self):
        """Compute recent bandwidth usage, in kbps."""
        return 8 * self.weighted_bytes() / float(KILO)

    def sendable(self):
        """How many bytes can we send without exceeding bandwidth?"""
        return max(0, self.bytes_per_second - self.weighted_bytes())

    def writable(self):
        """Check if this channel is ready to write some data."""
        return (len(self.buffer) and
                self.sendable() / 2 > self.fragment_size)

    def handle_write(self):
        """Write some data to the socket."""
        max_bytes = self.sendable() / 2
        if max_bytes < self.fragment_size:
            return
        bytes = self.send(self.buffer[0][:max_bytes])
        self.log_sent_bytes(bytes)
        if bytes == len(self.buffer[0]):
            self.buffer.pop(0)
        else:
            self.buffer[0] = self.buffer[0][bytes:]


class ClientChannel(ThrottleSender):
    """A client connection."""

    def __init__(self, channel, addr):
        ThrottleSender.__init__(self, options.download, channel)
        self.addr = addr
        self.header = Header()
        self.content_length = 0
        self.server = None
        self.handle_connect()

    def readable(self):
        """Check if this channel is ready to receive some data."""
        return self.server is None or len(self.server.buffer) == 0

    def handle_read(self):
        """Read some data from the client."""
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
            if not len(data):
                break
            if self.header.complete and self.content_length == 0:
                if not options.quiet:
                    print >> sys.stderr, \
                          "client %s:%d sends a new request" % self.addr
                self.header = Header()
                self.server = None
            data = self.header.append(data)
            if self.header.complete:
                self.content_length = int(
                    self.header.extract('Content-Length', 0))
                self.header.extract_host()
                if options.dump_send_headers:
                    self.header.dump(self.addr, self.header.host_addr)
                self.server = ServerChannel(self, self.header)

    def handle_connect(self):
        """Print connect message to stderr."""
        if not options.quiet:
            print >> sys.stderr, "client %s:%d connected" % self.addr

    def handle_close(self):
        """Print disconnect message to stderr."""
        self.close()
        if not options.quiet:
            print >> sys.stderr, "client %s:%d disconnected" % self.addr


class ServerChannel(ThrottleSender):
    """Connection to HTTP server."""

    def __init__(self, client, header):
        ThrottleSender.__init__(self, options.upload)
        self.client = client
        self.addr = header.host_addr
        self.create_socket(socket.AF_INET, socket.SOCK_STREAM)
        self.connect(self.addr)
        self.send_header(header)
        self.header = Header()

    def send_header(self, header):
        """Send HTTP request header to the server."""
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
        """Send one line of the request header to the server."""
        self.buffer.append(line + '\r\n')

    def receive_header(self, header):
        """Send HTTP reply header to the client."""
        for line in header.lines:
            if not (line.startswith('Keep-Alive: ') or
                    line.startswith('Connection: ') or
                    line.startswith('Proxy-')):
                self.receive_line(line)
        self.receive_line('')

    def receive_line(self, line):
        """Send one line of the reply header to the client."""
        self.client.buffer.append(line + '\r\n')

    def readable(self):
        """Check if this channel is ready to receive some data."""
        return len(self.client.buffer) == 0

    def handle_read(self):
        """Read some data from the server."""
        data = self.recv(8192)
        if not self.header.complete:
            data = self.header.append(data)
            if self.header.complete:
                if options.dump_recv_headers:
                    self.header.dump(self.addr, self.client.addr, 'receiving')
                self.receive_header(self.header)
        if self.header.complete and len(data):
            if options.dump_recv_content:
                self.header.dump_content(
                    data, self.addr, self.client.addr, 'receiving')
            self.client.buffer.append(data)

    def handle_connect(self):
        """Print connect message to stderr."""
        if not options.quiet:
            print >> sys.stderr, "server %s:%d connected" % self.addr

    def handle_close(self):
        """Print disconnect message to stderr."""
        self.close()
        if not options.quiet:
            print >> sys.stderr, "server %s:%d disconnected" % self.addr


class ProxyServer(asyncore.dispatcher):
    """Listen for client connections."""

    def __init__(self):
        asyncore.dispatcher.__init__(self)
        self.create_socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addr = (options.interface, options.port)
        self.bind(self.addr)
        self.listen(5)
        if not options.quiet:
            print >> sys.stderr, "listening on %s:%d" % self.addr

    def handle_accept(self):
        """Accept a new connection from a client."""
        channel, addr = self.accept()
        if addr[0] == '127.0.0.1' or options.allow_remote:
            ClientChannel(channel, addr)
        else:
            channel.close()
            if not options.quiet:
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
    parser.add_option('-l', dest='text_dump_limit', action='store',
        metavar='<bytes>', type='int', default=1024,
        help="maximum length of dumped text content (default 1024)")
    parser.add_option('-L', dest='data_dump_limit', action='store',
        metavar='<bytes>', type='int', default=256,
        help="maximum length of dumped binary content (default 256)")
    parser.add_option('-g', dest='gzip_size_limit', action='store',
        metavar='<bytes>', type='int', default=8192,
        help="maximum size for gzip decompression (default 8192)")
    options, args = parser.parse_args()
    proxy = ProxyServer()
    try:
        asyncore.loop(timeout=0.1)
    except:
        proxy.shutdown(2)
        proxy.close()
        raise
