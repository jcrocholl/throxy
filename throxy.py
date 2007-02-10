import socket
import select

__revision__ = '$Rev$'


def throttle_proxy(interface, port):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind((interface, port))
    listener.listen(1)
    try:
        clients = []
        while True:
            readables, writables, errors = select.select(
                clients + [listener], [], [])
            for readable in readables:
                if readable is listener:
                    client, addr = listener.accept()
                    clients.append(client)
                    print 'connection from', addr
                else:
                    data = readable.recv(1024)
                    if data:
                        print data,
                        readable.send(data)
                    else:
                        clients.remove(readable)
                        print 'closed'
    finally:
        listener.shutdown()
        listener.close()


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
    throttle_proxy(options.interface, options.port)


if __name__ == '__main__':
    _main()
