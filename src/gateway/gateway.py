import pika
import json
import os

from pika.adapters import twisted_connection
from twisted.internet import defer, reactor, task, protocol
from twisted.words.protocols import irc
from config import Config

base_dir = os.getenv('BURGER_DIR') or './'
env = os.getenv('BURGER_ENV') or 'DEV'

CONFIG = Config(base_dir, env)
print CONFIG.irc_channels

class IRC(irc.IRCClient):
    nickname = 'burgerbot'
    realname = 'burgerbot'

    @defer.inlineCallbacks
    def on_outbound_command(self, queue_object):
        op, tag = queue_object
        ch, method, prop, body = yield op.get()
        msg = json.loads(body)
        self.msg(msg["channel"].encode('utf8'), msg["content"].encode('utf8'))
        ch.basic_ack(delivery_tag=method.delivery_tag)

    def signedOn(self):
        for channel in self.factory.config.irc_channels:
            self.join(channel.encode('ascii'))

        self.factory.amqp.start_outbound_queue(self.on_outbound_command)

    def get_command(self, msg):
        if msg == "":
            raise LookupError('empty message')

        first_word = msg.split(' ')[0]

        if self.is_command(first_word):
            return first_word[1:]

        return False

    def is_command(self, word):
        return word[0] == self.factory.config.command_character

    def get_command_params(self, msg):
        return ' '.join(msg.split(' ')[1:])

    def privmsg(self, user, channel, msg):
        user = user.split('!', 1)[0]

        to_send = {
            "is_privmsg": channel == self.nickname,
            "from": user,
            "channel": channel,
            "content": msg,
        }

        command = self.get_command(msg)
        routing_key = "burger.msg"

        if command:
            print "got command"
            to_send["content"] = self.get_command_params(msg)
            routing_key = "burger.command.%s" % command

        print "publishing to: ", routing_key
        self.factory.amqp.channel.basic_publish(exchange='bus',
                                                routing_key=routing_key,
                                                body=json.dumps(to_send))

    def irc_unknown(self, prefix, command, params):
        channel = params[1]
        allowed_channels = self.factory.config.allowed_channels
        if command == "INVITE" and channel in allowed_channels:
            self.join(channel)


class IRCFactory(protocol.ClientFactory):
    def __init__(self, config, amqp):
        self.config = config
        self.amqp = amqp

    def buildProtocol(self, addr):
        p = IRC()
        p.factory = self
        return p

    def clientConnectionLost(self, connector, reason):
        connector.connect()

    def clientConnectionFailed(self, connector, reason):
        reactor.stop()


class AMQP(object):
    @defer.inlineCallbacks
    def setup(self, conn):
        self.conn = conn
        self.channel = yield self.conn.channel()
        self.exchange = yield self.channel.exchange_declare(
            exchange="bus",
            exchange_type='topic')

        self.outbound_queue = yield self.channel.queue_declare(
            queue="outbound",
            exclusive=True)

        yield self.channel.queue_bind(
            exchange='bus',
            queue=self.outbound_queue.method.queue,
            routing_key='burger.outbound.send')

    @defer.inlineCallbacks
    def start_outbound_queue(self, callback):
        qo = yield self.channel.basic_consume(
            consumer_callback=callback,
            queue=self.outbound_queue.method.queue)

        l = task.LoopingCall(callback, qo)
        l.start(0.01)


def on_error():
    print 'error!'


def on_close():
    print 'close!'

amqp = AMQP()
factory = IRCFactory(CONFIG, amqp)
params = pika.ConnectionParameters(CONFIG.amqp_server, CONFIG.amqp_port)
conn = twisted_connection.TwistedConnection(parameters=params,
                                            on_open_callback=amqp.setup,
                                            on_open_error_callback=on_error,
                                            on_close_callback=on_close)
reactor.connectTCP(CONFIG.irc_server,
                   CONFIG.irc_port,
                   factory)
reactor.run()
