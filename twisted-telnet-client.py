import re
import logging

from twisted.internet import defer
from twisted.internet.endpoints import TCP4ClientEndpoint
from twisted.internet.protocol import ReconnectingClientFactory
from twisted.conch.telnet import TelnetTransport, StatefulTelnetProtocol

log = logging.getLogger('TelnetClient')

def connect_telnet(host,port,username,password,prompt='$',timeout=30):
    endpoint = TCP4ClientEndpoint(reactor, host, port,timeout)
    telnetFactory = TelnetFactory(username,password,prompt)
    telnetFactory.protocol = TelnetClient
    d = endpoint.connect(telnetFactory)
    def connection_failed(reason):
        raise TelnetConnectionError(reason)
    d.addErrback(connection_failed)
    return d

class TelnetConnectionError(Exception):
    pass

class TelnetClient(StatefulTelnetProtocol):
    output_buffer = ''
    search_output = []

    def rawDataReceived(self,bytes):
        """The login and password prompt on some systems are not
        received in lineMode. Therefore we do the authentication in 
        raw mode and switch back to line mode when we decect the 
        shell prompt.

        TODO: Need to handle authentication failure
        """
        if self.factory.prompt.strip() == '$':
            self.re_prompt = re.compile('\$')
        else:
            self.re_prompt = re.compile(self.factory.prompt)

        #print 'Received raw data:', repr(bytes)
        if re.search('([L|l]ogin:\s+$)', bytes):
            self.sendLine(self.factory.username)
        elif re.search('([P|p]assword:\s+$)', bytes):
            self.sendLine(self.factory.password)
        elif self.re_prompt.search(bytes):
            self.setLineMode(bytes)
        self.output_buffer += bytes

    def connectionMade(self):
        """ Set rawMode since we do not reveice the 
        login and password prompt in line mode. We return to default
        line mode when we detect the prompt in the received data stream
        """
        self.setRawMode()

    def lineReceived(self, line):
        if len(self.search_output) == 0:
            self.output_buffer += line + '\n'
            return

        re_expected, search_deffered = self.search_output[0]
        if not search_deffered.called:
            match = re_expected.search(line)
            if match:
                data = self.output_buffer + line[:match.end()]
                self.clearBuffer()
                self.search_output.pop(0)
                search_deffered.callback(data)
                return

        self.output_buffer += line + '\n'

    def clearBuffer(self):
        self.clearLineBuffer()
        self.output_buffer = ''

    def write(self,command):
        return self.sendLine(command)

    def expect(self,expected,timeout=5):
        re_expected = re.compile(expected)
        expect_deffered = defer.Deferred()

        def expectTimedOut():
            self.search_output.pop(0)
            expect_deffered.callback(self.output_buffer)
            self.clearBuffer()

        cancel_defered = reactor.callLater(timeout,expectTimedOut)

        def cancelTimeout(result):
            if not cancel_defered.cancelled and not cancel_defered.called:
                cancel_defered.cancel()
            return result

        expect_deffered.addCallback(cancelTimeout)

        data = ''
        for line in self.output_buffer.split('\n'):
            match = re_expected.search(line)
            if match:
                data += line[:match.end()]
                self.clearBuffer()
                expect_deffered.callback(data)
                break
            data += line + '\n'

        if not expect_deffered.called:
            self.search_output.append((re_expected,expect_deffered))

        return expect_deffered

    def close(self):
        self.sendLine(self.factory.logout_command)
        self.factory.transport.loseConnection()


class TelnetFactory(ReconnectingClientFactory):
    logout_command = 'exit'

    def __init__(self,username,password,prompt='$'):
        self.username = username
        self.password = password
        self.prompt = prompt
        self.transport = None

    def buildProtocol(self, addr):
        self.transport = TelnetTransport(TelnetClient)
        self.transport.factory = self
        return self.transport

    def setLogoutCommand(self,cmd):
        self.exit_command = cmd
 
    def clientConnectionLost(self, connector, reason):
        log.error('Lost telnet connection.  Reason: %s ' % reason)
        ReconnectingClientFactory.clientConnectionLost(self, connector, reason)

    def clientConnectionFailed(self, connector, reason):
        log.error('Telnet connection failed. Reason:%s ' % reason)
        ReconnectingClientFactory.clientConnectionFailed(self, connector,
                                                         reason)

if __name__ == '__main__':
    logging.basicConfig()
    tn = None

    def read(expect,timeout=1):
        def print_output(res):
            print res
            return res
        d = tn.expect(expect,timeout)
        d.addCallback(print_output)

    def testProtocol(protocol):
        global tn
        tn = protocol.protocol
        reactor.callLater(1, tn.clearBuffer)
        reactor.callLater(1.5, tn.write,'dmesg')
        reactor.callLater(1.6, read,'abcdef',2)
        reactor.callLater(5.1, tn.write,'uname -a')
        reactor.callLater(5, read,'Linux',0.5)
        reactor.callLater(6, tn.close)
        reactor.callLater(7, reactor.stop)

    import sys
    from twisted.internet import reactor
    host = sys.argv[1]
    port = 23
    username = sys.argv[2]
    password = sys.argv[3]
    prompt = '$'
    whenConnected = connect_telnet(host,port,username,password,prompt)
    whenConnected.addCallback(testProtocol)
    reactor.run()

