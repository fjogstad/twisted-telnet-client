# * ----------------------------------------------------------------------------
# * "THE BEER-WARE LICENSE" (Revision 42):
# * <sindre@fjogstad.name> wrote this file. As long as you retain this notice you
# * can do whatever you want with this stuff. If we meet some day, and you think
# * this stuff is worth it, you can buy me a beer in return Sindre Fjogstad
# * ----------------------------------------------------------------------------
# 

import re
import logging

from twisted.internet import defer
from twisted.internet.endpoints import TCP4ClientEndpoint
from twisted.internet.protocol import ReconnectingClientFactory
from twisted.conch.telnet import TelnetTransport, StatefulTelnetProtocol

log = logging.getLogger('TelnetClient')

def connect_telnet(host,port,username,password,greeting,prompt='$',timeout=30):
    endpoint = TCP4ClientEndpoint(reactor, host, port,timeout)
    telnetFactory = TelnetFactory(username,password,greeting,prompt)
    telnetFactory.protocol = TelnetClient
    d = endpoint.connect(telnetFactory)

    def check_connection_state(transport):
        """Since we can't use the telnet connection before we have
        logged in and the client is in line_mode 1
        we pause the deferred here until the client is ready
        The client unpuase the defer when wee are logged in
        """
        if transport.protocol.line_mode == 1:
            return transport
        d.pause()
        transport.protocol.connected_deferred= d
        return transport
    d.addCallback(check_connection_state)

    def connection_failed(reason):
        raise TelnetConnectionError(reason)
    
    d.addErrback(connection_failed)
    return d

class TelnetConnectionError(Exception):
    pass

class TelnetClient(StatefulTelnetProtocol):
    output_buffer = []
    search_output = []

    # The deferred from the connect_telnet method.
    # We need to tell the deferred that it can continue when we are
    # ready ant when the socket are connected
    connected_deferred = None

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

        log.debug('Received raw telnet data: %s' % repr(bytes))
        if re.search('([Ll]ogin:\s+$)', bytes):
            self.sendLine(self.factory.username)
        elif re.search('([Pp]assword:\s+$)', bytes):
            self.sendLine(str(self.factory.password))
        elif self.re_prompt.search(bytes) or re.search(self.factory.greeting, bytes):
            log.debug('Telnet client logged in. We are ready for commands')
            self.setLineMode()
            if self.connected_deferred:
                self.connected_deferred.unpause()

    def connectionMade(self):
        """ Set rawMode since we do not receive the 
        login and password prompt in line mode. We return to default
        line mode when we detect the prompt in the received data stream
        """
        self.setRawMode()

    def lineReceived(self, line):
        log.debug('Received telnet line: %s' % repr(line))
        if len(self.search_output) == 0:
            self.output_buffer.append(line)
            return

        re_expected = self.search_output[0][0]
        search_deferred = self.search_output[0][1]
        if not search_deferred.called:
            match = re_expected.search(line)
            if match:
                data = '\n'.join(self.output_buffer)
                data += '\n%s' % line[:match.end()]
                self.search_output.pop(0)
                # Start the timeout of the next epect message
                if len(self.search_output) > 0:
                    re_expected = self.search_output[0][0]
                    search_deferred = self.search_output[0][1]
                    timeout = self.search_output[0][3]
                    self.search_output[0][2] = self.getTimeoutDefer(timeout,
                                        search_deferred, re_expected)
                search_deferred.callback(data)
                log.debug('clear telnet buffer in lineReceived. We have a match: %s' % line)
                self.clearBuffer()
                return

        self.output_buffer.append(line)

    def clearBuffer(self,to_line=-1):
        self.clearLineBuffer()
        if to_line == -1:
            self.output_buffer = []
        else:
            self.output_buffer = self.output_buffer[to_line:]

    def getTimeoutDefer(self,timeout,expect_deferred,re_expected):
        """ Create the cancel expect messages deffer that we use to 
        to timeout the expect deffer.
        """
        def expectTimedOut(expect_deferred):
            result = ''
            # we only test the buffer against the oldest wait. Therfore
            # do not clear the buffer if a older exoect still waits for
            # a messages. This should probaly rause an exceotion or somethine...

            if expect_deferred.called:
                log.error('Expected message deferrer "%s" is already called. ' % expect_deferred)
                log.error('This is a sign that something is wrong with the telnet client')
                return

            if self.search_output[0][1] == expect_deferred:
                # set the result since this is the current search
                result = '\n'.join(self.output_buffer)
                self.clearBuffer()
                self.search_output.pop(0)
                # Start the timeout of the next epect message
                if len(self.search_output) > 0:
                    next_re_expected = self.search_output[0][0]
                    next_search_deferred = self.search_output[0][1]
                    next_timeout = self.search_output[0][3]
                    self.search_output[0][2] = self.getTimeoutDefer(next_timeout,
                                        next_search_deferred, next_re_expected)
            else:
                msg = 'Search for message "%s" timed ' % re_expected.pattern
                msg += 'out before the search started.'
                log.error(msg)
                msg = 'We are currently searhing for: '
                msg += '"%s". ' % self.search_output[0][0].pattern
                log.error(msg)
                index = 1
                while index <= len(self.output_buffer):
                    if self.search_output[index][1] == expect_deferred:
                        self.search_output.pop(index)
                    index += 1

            expect_deferred.callback(result)

        cancel_deferred = reactor.callLater(timeout,expectTimedOut,expect_deferred)

        def cancelTimeout(result):
            if not cancel_deferred.cancelled and not cancel_deferred.called:
                cancel_deferred.cancel()
            return result

        expect_deferred.addCallback(cancelTimeout)
        return cancel_deferred

    def write(self,command):
        return self.sendLine(command)

    def expect(self,expected,timeout=5):
        re_expected = re.compile(expected)
        expect_deferred = defer.Deferred()

        if len(self.search_output) == 0:
            data = ''
            for line in self.output_buffer:
                match = re_expected.search(line)
                if match:
                    data += line[:match.end()]
                    log.debug('Match "%s" found in (%s), clear buffer' % (expected,line))
                    self.clearBuffer()
                    expect_deferred.callback(data)
                    break
                data += line + '\n'
        else:
            # We are not allow to start the timeout until we have started the search
            expect_deferred.pause()

        if not expect_deferred.called:
            cancel_deferred = None
            # Only create the cancel_deferred if this is current search.
            # if we are not we risk that the search times out before the search
            # started
            if len(self.search_output) == 0:
                cancel_deferred = self.getTimeoutDefer(timeout,expect_deferred,re_expected)
            self.search_output.append([re_expected,expect_deferred,cancel_deferred,timeout])

        return expect_deferred

    def close(self):
        self.sendLine(self.factory.logout_command)
        self.factory.transport.loseConnection()
        return True


class TelnetFactory(ReconnectingClientFactory):
    logout_command = 'exit'

    def __init__(self,username,password,greeting='',prompt='$'):
        self.username = username
        self.password = password
        self.prompt = prompt
        self.greeting = greeting
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
    prompt = '\$'
    whenConnected = connect_telnet(host,port,username,password,prompt)
    whenConnected.addCallback(testProtocol)
    reactor.run()

