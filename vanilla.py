# Organ pipe arrangement of imports; because Guido likes it

import collections
import traceback
import functools
import urlparse
import hashlib
import logging
import urllib
import base64
import socket
import select
import struct
import heapq
import fcntl
import cffi
import uuid
import time
import sys
import os


from greenlet import getcurrent
from greenlet import greenlet


__version__ = '0.0.1'


log = logging.getLogger(__name__)


class Timeout(Exception):
    pass


class Closed(Exception):
    pass


class Stop(Closed):
    pass


class Filter(Exception):
    pass


class Reraise(Exception):
    pass


class preserve_exception(object):
    """
    Marker to pass exceptions through channels
    """
    def __init__(self):
        self.typ, self.val, self.tb = sys.exc_info()

    def reraise(self):
        try:
            raise Reraise('Unhandled exception')
        except:
            traceback.print_exc()
            sys.stderr.write('\nOriginal exception -->\n\n')
            raise self.typ, self.val, self.tb


def init_C():
    ffi = cffi.FFI()

    ffi.cdef("""

    int pipe2(int pipefd[2], int flags);

    #define O_NONBLOCK ...
    #define O_CLOEXEC ...

    ssize_t read(int fd, void *buf, size_t count);

    int eventfd(unsigned int initval, int flags);

    #define SIG_BLOCK ...
    #define SIG_UNBLOCK ...
    #define SIG_SETMASK ...

    typedef struct { ...; } sigset_t;

    int sigprocmask(int how, const sigset_t *set, sigset_t *oldset);

    int sigemptyset(sigset_t *set);
    int sigfillset(sigset_t *set);
    int sigaddset(sigset_t *set, int signum);
    int sigdelset(sigset_t *set, int signum);
    int sigismember(const sigset_t *set, int signum);

    #define SFD_NONBLOCK ...
    #define SFD_CLOEXEC ...

    #define EAGAIN ...

    #define EPOLLIN ...
    #define EPOLLERR ...
    #define EPOLLHUP ...
    #define EPOLLRDHUP ...

    #define SIGALRM ...
    #define SIGINT ...
    #define SIGTERM ...
    #define SIGCHLD ...

    struct signalfd_siginfo {
        uint32_t ssi_signo;   /* Signal number */
        ...;
    };

    int signalfd(int fd, const sigset_t *mask, int flags);

    /*
        INOTIFY */

    #define IN_ACCESS ...         /* File was accessed. */
    #define IN_MODIFY ...         /* File was modified. */
    #define IN_ATTRIB ...         /* Metadata changed. */
    #define IN_CLOSE_WRITE ...    /* Writtable file was closed. */
    #define IN_CLOSE_NOWRITE ...  /* Unwrittable file closed. */
    #define IN_OPEN ...           /* File was opened. */
    #define IN_MOVED_FROM ...     /* File was moved from X. */
    #define IN_MOVED_TO ...       /* File was moved to Y. */
    #define IN_CREATE ...         /* Subfile was created. */
    #define IN_DELETE ...         /* Subfile was deleted. */
    #define IN_DELETE_SELF ...    /* Self was deleted. */
    #define IN_MOVE_SELF ...      /* Self was moved. */

    /* Events sent by the kernel. */
    #define IN_UNMOUNT ...    /* Backing fs was unmounted. */
    #define IN_Q_OVERFLOW ... /* Event queued overflowed. */
    #define IN_IGNORED ...    /* File was ignored. */

    /* Helper events. */
    #define IN_CLOSE ... /* Close. */
    #define IN_MOVE ...  /* Moves. */

    /* Special flags. */
    #define IN_ONLYDIR ...      /* Only watch the path if it is a directory. */
    #define IN_DONT_FOLLOW ...  /* Do not follow a sym link. */
    #define IN_EXCL_UNLINK ...  /* Exclude events on unlinked objects. */
    #define IN_MASK_ADD ...     /* Add to the mask of an already existing
                                   watch. */
    #define IN_ISDIR ...        /* Event occurred against dir. */
    #define IN_ONESHOT ...      /* Only send event once. */

    /* All events which a program can wait on. */
    #define IN_ALL_EVENTS ...

    #define IN_NONBLOCK ...
    #define IN_CLOEXEC ...

    int inotify_init(void);
    int inotify_init1(int flags);
    int inotify_add_watch(int fd, const char *pathname, uint32_t mask);

    /*
        PRCTL */

    #define PR_SET_PDEATHSIG ...

    int prctl(int option, unsigned long arg2, unsigned long arg3,
              unsigned long arg4, unsigned long arg5);
    """)

    C = ffi.verify("""
        #include <unistd.h>
        #include <signal.h>
        #include <fcntl.h>

        #include <sys/signalfd.h>
        #include <sys/eventfd.h>
        #include <sys/inotify.h>
        #include <sys/epoll.h>
        #include <sys/prctl.h>
    """)

    # stash some conveniences on C
    C.ffi = ffi
    C.NULL = ffi.NULL

    def Cdot(f):
        setattr(C, f.__name__, f)

    @Cdot
    def sigset(*nums):
        s = ffi.new('sigset_t *')
        assert not C.sigemptyset(s)

        for num in nums:
            rc = C.sigaddset(s, num)
            assert not rc, "signum: %s doesn't specify a valid signal." % num
        return s

    return C


C = init_C()


class FD(object):
    def __init__(self, hub, fileno):
        self.hub = hub
        self.fileno = fileno

        self.events = hub.register(fileno, C.EPOLLIN | C.EPOLLHUP | C.EPOLLERR)

        self.hub.spawn(self.loop)
        self.pending = self.hub.channel()

    def loop(self):
        while True:
            try:
                fileno, event = self.events.recv()
                if event & select.EPOLLERR or event & select.EPOLLHUP:
                    raise Stop

                # read until exhaustion
                while True:
                    try:
                        data = os.read(self.fileno, 4096)
                    except OSError, e:
                        # resource unavailable, block until it is
                        if e.errno == 11:  # EAGAIN
                            break
                        raise Stop

                    if not data:
                        raise Stop

                    self.pending.send(data)

            except Stop:
                self.close()
                return

    def close(self):
        self.hub.unregister(self.fileno)
        try:
            os.close(self.fileno)
        except:
            pass

    def recv_bytes(self, n):
        if n == 0:
            return ''

        received = 0
        segments = []
        while received < n:
            segment = self.pending.recv()
            segments.append(segment)
            received += len(segment)

        # if we've received too much, break the last segment and return the
        # additional portion to pending
        overage = received - n
        if overage:
            self.pending.items.appendleft(segments[-1][-1*(overage):])
            segments[-1] = segments[-1][:-1*(overage)]

        return ''.join(segments)

    def recv_partition(self, sep):
        received = ''
        while True:
            received += self.pending.recv()
            keep, matched, additonal = received.partition(sep)
            if matched:
                if additonal:
                    self.pending.items.appendleft(additonal)
                return keep

    def send(self, data):
        while True:
            n = os.write(self.fileno, data)
            if n == len(data):
                break
            data = data[n:]


class Event(object):
    """
    An event object manages an internal flag that can be set to true with the
    set() method and reset to false with the clear() method. The wait() method
    blocks until the flag is true.
    """

    __slots__ = ['hub', 'fired', 'waiters']

    def __init__(self, hub, fired=False):
        self.hub = hub
        self.fired = fired
        self.waiters = collections.deque()

    def __nonzero__(self):
        return self.fired

    def wait(self):
        if self.fired:
            return
        self.waiters.append(getcurrent())
        self.hub.pause()

    def set(self):
        self.fired = True
        # isolate this group of waiters in case of a clear
        waiters = self.waiters
        while waiters:
            waiter = waiters.popleft()
            self.hub.switch_to(waiter)

    def clear(self):
        self.fired = False
        # start a new list of waiters, which will block until the next set
        self.waiters = collections.deque()
        return self


class Channel(object):

    __slots__ = ['hub', 'closed', 'pipeline', 'items', 'waiters']

    def __init__(self, hub):
        self.hub = hub
        self.closed = False
        self.pipeline = None
        self.items = collections.deque()
        self.waiters = collections.deque()

    def __call__(self, f):
        if not self.pipeline:
            self.pipeline = []
        self.pipeline.append(f)

    def send(self, item):
        if self.closed:
            raise Closed

        if self.pipeline and not isinstance(item, Closed):
            try:
                for f in self.pipeline:
                    item = f(item)
            except Filter:
                return
            except Exception, e:
                item = e

        if not self.waiters:
            self.items.append(item)
            return

        getter = self.waiters.popleft()
        if isinstance(item, Exception):
            self.hub.throw_to(getter, item)
        else:
            self.hub.switch_to(getter, (self, item))

    def recv(self, timeout=-1):
        if self.items:
            item = self.items.popleft()
            if isinstance(item, preserve_exception):
                item.reraise()
            if isinstance(item, Exception):
                raise item
            return item

        if timeout == 0:
            raise Timeout('timeout: %s' % timeout)

        self.waiters.append(getcurrent())
        try:
            item = self.hub.pause(timeout=timeout)
            ch, item = item
        except Timeout:
            self.waiters.remove(getcurrent())
            raise
        return item

    def throw(self):
        self.send(preserve_exception())

    def __iter__(self):
        while True:
            try:
                yield self.recv()
            except Closed:
                raise StopIteration

    def close(self):
        self.send(Closed('closed'))
        self.closed = True


class Signal(object):
    def __init__(self, hub):
        self.hub = hub
        self.fd = -1
        self.count = 0
        self.mapper = {}
        self.reverse_mapper = {}

    def start(self, fd):
        self.fd = fd

        info = C.ffi.new('struct signalfd_siginfo *')
        size = C.ffi.sizeof('struct signalfd_siginfo')

        ready = self.hub.register(fd, select.EPOLLIN)

        @self.hub.spawn
        def _():
            while True:
                try:
                    fd, event = ready.recv()
                except Closed:
                    self.stop()
                    return

                rc = C.read(fd, info, size)
                assert rc == size

                num = info.ssi_signo
                for ch in self.mapper[num]:
                    ch.send(num)

    def stop(self):
        if self.fd == -1:
            return

        fd = self.fd
        self.fd = -1
        self.count = 0
        self.mapper = {}
        self.reverse_mapper = {}

        self.hub.unregister(fd)
        os.close(fd)

    def reset(self):
        if self.count == len(self.mapper):
            return

        self.count = len(self.mapper)

        if not self.count:
            self.stop()
            return

        mask = C.sigset(*self.mapper.keys())
        rc = C.sigprocmask(C.SIG_SETMASK, mask, C.NULL)
        assert not rc
        fd = C.signalfd(self.fd, mask, C.SFD_NONBLOCK | C.SFD_CLOEXEC)

        if self.fd == -1:
            self.start(fd)

    def subscribe(self, *signals):
        out = self.hub.channel()
        self.reverse_mapper[out] = signals
        for num in signals:
            self.mapper.setdefault(num, []).append(out)
        self.reset()
        return out

    def unsubscribe(self, ch):
        for num in self.reverse_mapper[ch]:
            self.mapper[num].remove(ch)
            if not self.mapper[num]:
                del self.mapper[num]
        del self.reverse_mapper[ch]
        self.reset()


class INotify(object):
    FLAG_TO_HUMAN = [
        (C.IN_ACCESS, 'access'),
        (C.IN_MODIFY, 'modify'),
        (C.IN_ATTRIB, 'attrib'),
        (C.IN_CLOSE_WRITE, 'close_write'),
        (C.IN_CLOSE_NOWRITE, 'close_nowrite'),
        (C.IN_OPEN, 'open'),
        (C.IN_MOVED_FROM, 'moved_from'),
        (C.IN_MOVED_TO, 'moved_to'),
        (C.IN_CREATE, 'create'),
        (C.IN_DELETE, 'delete'),
        (C.IN_DELETE_SELF, 'delete_self'),
        (C.IN_MOVE_SELF, 'move_self'),
        (C.IN_UNMOUNT, 'unmount'),
        (C.IN_Q_OVERFLOW, 'queue_overflow'),
        (C.IN_IGNORED, 'ignored'),
        (C.IN_ONLYDIR, 'only_dir'),
        (C.IN_DONT_FOLLOW, 'dont_follow'),
        (C.IN_MASK_ADD, 'mask_add'),
        (C.IN_ISDIR, 'is_dir'),
        (C.IN_ONESHOT, 'one_shot'), ]

    @staticmethod
    def humanize_mask(mask):
        s = []
        for k, v in INotify.FLAG_TO_HUMAN:
            if k & mask:
                s.append(v)
        return s

    def __init__(self, hub):
        self.hub = hub
        self.fileno = C.inotify_init1(C.IN_NONBLOCK | C.IN_CLOEXEC)
        self.fd = FD(self.hub, self.fileno)
        self.wds = {}

        @hub.spawn
        def _():
            while True:
                notification = self.fd.recv_bytes(16)
                wd, mask, cookie, size = struct.unpack("=LLLL", notification)
                if size:
                    name = self.fd.recv_bytes(size).rstrip('\0')
                else:
                    name = None
                self.wds[wd].send((mask, name))

    def watch(self, path, mask=C.IN_ALL_EVENTS):
        wd = C.inotify_add_watch(self.fileno, path, mask)
        ch = self.hub.channel()
        self.wds[wd] = ch
        return ch


class Process(object):

    class Child(object):
        def __init__(self, hub, pid):
            self.hub = hub
            self.pid = pid
            # TODO: should use an event here
            self.done = self.hub.channel()

        def check_liveness(self):
            pid, code = os.waitpid(self.pid, os.WNOHANG)

            if (pid, code) == (0, 0):
                return True

            self.exitcode = code >> 8
            self.exitsignal = code & (2**8-1)
            self.done.send(self)
            return False

        def terminate(self):
            raise Exception('eep')

    def __init__(self, hub):
        self.hub = hub
        self.children = {}
        self.sigchld = None

    def set_pdeathsig(self):
        """
        Ask Linux to ensure out children are sent a SIGTERM when our process
        dies, to avoid orphaned children.
        """
        rc = C.prctl(C.PR_SET_PDEATHSIG, C.SIGTERM, 0, 0, 0)
        assert not rc, 'PR_SET_PDEATHSIG failed: %s' % rc

    def watch(self):
        while self.children:
            try:
                self.sigchld.recv()
            except Stop:
                for child in self.children:
                    child.terminate()
                continue
            self.children = [
                child for child in self.children if child.check_liveness()]
        self.hub.signal.unsubscribe(self.sigchld)

    def bootstrap(self, f, inpipe, outpipe, *a, **kw):
        import pickle
        import json

        # Depending on dill for the moment to be able to quickly push on in
        # this direction, and to see if it's a good idea
        import dill

        self.set_pdeathsig()

        pipe_r, pipe_w = os.pipe()

        os.write(pipe_w, json.dumps((pickle.dumps(f), a, kw)))
        os.close(pipe_w)

        bootstrap = '\n'.join(x.strip() for x in ("""
            import pickle
            import json
            import sys
            import os

            import dill

            code, a, kw = json.loads(os.read(%(pipe_r)s, 4096))
            os.close(%(pipe_r)s)

            os.dup2(%(inpipe)s, sys.stdin.fileno())
            os.close(%(inpipe)s)

            os.dup2(%(outpipe)s, sys.stdout.fileno())
            os.close(%(outpipe)s)

            f = pickle.loads(code)
            f(*a, **kw)
        """ % {
            'pipe_r': pipe_r,
            'inpipe': inpipe,
            'outpipe': outpipe}).split('\n') if x)

        argv = [sys.executable, '-c', bootstrap]
        os.execv(argv[0], argv)

    def spawn(self, f, *a, **kw):
        if not self.sigchld:
            self.sigchld = self.hub.signal.subscribe(C.SIGCHLD)
            self.hub.spawn(self.watch)

        infds = C.ffi.new('int[2]')
        C.pipe2(infds, C.O_NONBLOCK)
        inpipe_r, inpipe_w = infds

        outfds = C.ffi.new('int[2]')
        C.pipe2(outfds, C.O_NONBLOCK)
        outpipe_r, outpipe_w = outfds

        pid = os.fork()

        if pid == 0:
            os.close(inpipe_w)
            os.close(outpipe_r)
            self.bootstrap(f, inpipe_r, outpipe_w, *a, **kw)
            return

        # parent continues
        os.close(inpipe_r)
        os.close(outpipe_w)

        child = self.Child(self.hub, pid)
        child.stdin = inpipe_w
        child.stdout = outpipe_r
        self.children[child] = child
        return child


class Hub(object):
    def __init__(self):
        self.ready = collections.deque()
        self.scheduled = Scheduler()
        self.stopped = self.event()

        self.epoll = select.epoll()
        self.registered = {}

        self.signal = Signal(self)
        self.process = Process(self)
        self.tcp = TCP(self)
        self.http = HTTP(self)

        self.loop = greenlet(self.main)

    def event(self, fired=False):
        return Event(self, fired)

    def channel(self):
        return Channel(self)

    # allows you to wait on a list of channels
    def select(self, *channels):
        for ch in channels:
            try:
                item = ch.recv(timeout=0)
                return ch, item
            except Timeout:
                continue

        for ch in channels:
            ch.waiters.append(getcurrent())

        try:
            fired, item = self.pause()
        except:
            for ch in channels:
                if getcurrent() in ch.waiters:
                    ch.waiters.remove(getcurrent())
            raise

        for ch in channels:
            if ch != fired:
                ch.waiters.remove(getcurrent())
        return fired, item

    def inotify(self):
        return INotify(self)

    def pause(self, timeout=-1):
        if timeout > -1:
            item = self.scheduled.add(
                timeout, getcurrent(), Timeout('timeout: %s' % timeout))

        resume = self.loop.switch()

        if timeout > -1:
            if isinstance(resume, Timeout):
                raise resume

            # since we didn't timeout, remove ourselves from scheduled
            self.scheduled.remove(item)

        # TODO: clean up stopped handling here
        if self.stopped:
            raise Closed('closed')

        return resume

    def switch_to(self, target, *a):
        self.ready.append((getcurrent(), ()))
        return target.switch(*a)

    def throw_to(self, target, *a):
        self.ready.append((getcurrent(), ()))
        if len(a) == 1 and isinstance(a[0], preserve_exception):
            return target.throw(a[0].typ, a[0].val, a[0].tb)
        return target.throw(*a)

    def spawn(self, f, *a):
        self.ready.append((f, a))

    def spawn_later(self, ms, f, *a):
        self.scheduled.add(ms, f, *a)

    def sleep(self, ms=1):
        self.scheduled.add(ms, getcurrent())
        self.loop.switch()

    def register(self, fd, mask):
        self.registered[fd] = self.channel()
        self.epoll.register(fd, mask)
        return self.registered[fd]

    def unregister(self, fd):
        if fd in self.registered:
            try:
                # TODO: investigate why this could error
                self.epoll.unregister(fd)
            except:
                pass
            self.registered[fd].close()
            del self.registered[fd]

    def stop(self):
        self.sleep(1)

        for fd, ch in self.registered.items():
            ch.send(Stop('stop'))

        while self.scheduled:
            task, a = self.scheduled.pop()
            self.throw_to(task, Stop('stop'))

        try:
            self.stopped.wait()
        except Closed:
            return

    def stop_on_term(self):
        done = self.signal.subscribe(C.SIGINT, C.SIGTERM)
        done.recv()
        self.stop()

    def main(self):
        """
        Scheduler steps:
            - run ready until exhaustion

            - if there's something scheduled
                - run overdue scheduled immediately
                - or if there's nothing registered, sleep until next scheduled
                  and then go back to ready

            - if there's nothing registered and nothing scheduled, we've
              deadlocked, so stopped

            - epoll on registered, with timeout of next scheduled, if something
              is scheduled
        """
        def run_task(task, *a):
            if isinstance(task, greenlet):
                task.switch(*a)
            else:
                greenlet(task).switch(*a)

        while True:
            while self.ready:
                task, a = self.ready.popleft()
                run_task(task, *a)

            if self.scheduled:
                timeout = self.scheduled.timeout()
                # run overdue scheduled immediately
                if timeout < 0:
                    task, a = self.scheduled.pop()
                    run_task(task, *a)
                    continue

                # if nothing registered, just sleep until next scheduled
                if not self.registered:
                    time.sleep(timeout)
                    task, a = self.scheduled.pop()
                    run_task(task, *a)
                    continue
            else:
                timeout = -1

            # TODO: add better handling for deadlock
            if not self.registered:
                self.stopped.set()
                return

            # run epoll
            events = None
            while True:
                try:
                    events = self.epoll.poll(timeout=timeout)
                    break
                # ignore IOError from signal interrupts
                except IOError:
                    continue

            if not events:
                # timeout
                task, a = self.scheduled.pop()
                run_task(task, *a)

            else:
                for fd, event in events:
                    if fd in self.registered:
                        self.registered[fd].send((fd, event))


class Scheduler(object):
    Item = collections.namedtuple('Item', ['due', 'action', 'args'])

    def __init__(self):
        self.count = 0
        self.queue = []
        self.removed = {}

    def add(self, delay, action, *args):
        due = time.time() + (delay / 1000.0)
        item = self.Item(due, action, args)
        heapq.heappush(self.queue, item)
        self.count += 1
        return item

    def __len__(self):
        return self.count

    def remove(self, item):
        self.removed[item] = True
        self.count -= 1

    def prune(self):
        while True:
            if self.queue[0] not in self.removed:
                break
            item = heapq.heappop(self.queue)
            del self.removed[item]

    def timeout(self):
        self.prune()
        return self.queue[0].due - time.time()

    def pop(self):
        self.prune()
        item = heapq.heappop(self.queue)
        self.count -= 1
        return item.action, item.args


# TCP ######################################################################


class TCP(object):
    def __init__(self, hub):
        self.hub = hub

    def listen(self, port=0, host='127.0.0.1'):
        return TCPListener(self.hub, host, port)

    def connect(self, port, host='127.0.0.1'):
        return TCPConn.connect(self.hub, host, port)


"""
struct.pack reference
uint32: "I"
uint64: 'Q"

Packet
    type|size: uint32 (I)
        type (2 bits):
            PUSH    = 0
            REQUEST = 1
            REPLY   = 2
            OP      = 3
        size (30 bits, 1GB)    # for type PUSH/REQUEST/REPLY
        or OPCODE for type OP
            1  = OP_PING
            2  = OP_PONG

    route: uint32 (I)          # optional for REQUEST and REPLY
    buffer: bytes len(size)

TCPConn supports Bi-Directional Push->Pull and Request<->Response
"""

PACKET_PUSH = 0
PACKET_REQUEST = 1 << 30
PACKET_REPLY = 2 << 30
PACKET_TYPE_MASK = PACKET_REQUEST | PACKET_REPLY
PACKET_SIZE_MASK = ~PACKET_TYPE_MASK


class TCPConn(object):
    @classmethod
    def connect(klass, hub, host, port):
        conn = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        conn.connect((host, port))
        conn.setblocking(0)
        return klass(hub, conn)

    def __init__(self, hub, conn):
        self.hub = hub
        self.conn = conn
        self.conn.setblocking(0)
        self.stopping = False
        self.closed = False

        # used to track calls, and incoming requests
        self.call_route = 0
        self.call_outstanding = {}

        self.pull = hub.channel()

        self.serve = hub.channel()
        self.serve_in_progress = 0
        ##

        self.recv_ready = hub.event(True)
        self.recv_buffer = ''
        self.recv_closed = False

        self.pong = hub.event(False)

        self.events = hub.register(
            conn.fileno(),
            select.EPOLLIN | select.EPOLLHUP | select.EPOLLERR)

        hub.spawn(self.event_loop)
        hub.spawn(self.recv_loop)

    def event_loop(self):
        while True:
            try:
                fd, event = self.events.recv()
                if event & select.EPOLLERR or event & select.EPOLLHUP:
                    self.close()
                    return
                if event & select.EPOLLIN:
                    if self.recv_closed:
                        if not self.serve_in_progress:
                            self.close()
                        return
                    self.recv_ready.set()
            except Closed:
                self.stop()

    def recv_loop(self):
        def recvn(n):
            if n == 0:
                return ''

            ret = ''
            while True:
                m = n - len(ret)
                if self.recv_buffer:
                    ret += self.recv_buffer[:m]
                    self.recv_buffer = self.recv_buffer[m:]

                if len(ret) >= n:
                    break

                try:
                    self.recv_buffer = self.conn.recv(max(m, 4096))
                except socket.error, e:
                    # resource unavailable, block until it is
                    if e.errno == 11:  # EAGAIN
                        self.recv_ready.clear().wait()
                        continue
                    raise

                if not self.recv_buffer:
                    raise socket.error('closing connection')

            return ret

        while True:
            try:
                typ_size, = struct.unpack('<I', recvn(4))

                # handle ping / pong
                if PACKET_TYPE_MASK & typ_size == PACKET_TYPE_MASK:
                    if typ_size & PACKET_SIZE_MASK == 1:
                        # ping received, send pong
                        self._send(struct.pack('<I', PACKET_TYPE_MASK | 2))
                    else:
                        # pong recieved
                        self.pong.set()
                        self.pong.clear()
                    continue

                if PACKET_TYPE_MASK & typ_size:
                    route, = struct.unpack('<I', recvn(4))

                data = recvn(typ_size & PACKET_SIZE_MASK)

                if typ_size & PACKET_REQUEST:
                    self.serve_in_progress += 1
                    self.serve.send((route, data))
                    continue

                if typ_size & PACKET_REPLY:
                    if route not in self.call_outstanding:
                        log.warning('Missing route: %s' % route)
                        continue
                    self.call_outstanding[route].send(data)
                    del self.call_outstanding[route]
                    if not self.call_outstanding and self.stopping:
                        self.close()
                        break
                    continue

                # push packet
                self.pull.send(data)
                continue

            except Exception, e:
                if type(e) != socket.error:
                    log.exception(e)
                self.recv_closed = True
                self.stop()
                break

    def push(self, data):
        self.send(0, PACKET_PUSH, data)

    def call(self, data):
        # TODO: handle wrap around
        self.call_route += 1
        self.call_outstanding[self.call_route] = self.hub.channel()
        self.send(self.call_route, PACKET_REQUEST, data)
        return self.call_outstanding[self.call_route]

    def reply(self, route, data):
        self.send(route, PACKET_REPLY, data)
        self.serve_in_progress -= 1
        if not self.serve_in_progress and self.stopping:
            self.close()

    def ping(self):
        self._send(struct.pack('<I', PACKET_TYPE_MASK | 1))

    def send(self, route, typ, data):
        assert len(data) < 2**30, 'Data must be less than 1Gb'

        # TODO: is there away to avoid the duplication of data here?
        if PACKET_TYPE_MASK & typ:
            message = struct.pack('<II', typ | len(data), route) + data
        else:
            message = struct.pack('<I', typ | len(data)) + data

        self._send(message)

    def _send(self, message):
        try:
            self.conn.send(message)
        except Exception, e:
            if type(e) != socket.error:
                log.exception(e)
            self.close()
            raise

    def stop(self):
        if self.call_outstanding or self.serve_in_progress:
            self.stopping = True
            # if we aren't waiting for a reply, shutdown our read pipe
            if not self.call_outstanding:
                self.hub.unregister(self.conn.fileno())
                self.conn.shutdown(socket.SHUT_RD)
            return

        # nothing in progress, just close
        self.close()

    def close(self):
        if not self.closed:
            self.closed = True
            self.hub.unregister(self.conn.fileno())
            try:
                self.conn.shutdown(socket.SHUT_RDWR)
            except:
                pass
            self.conn.close()
            for ch in self.call_outstanding.values():
                ch.send(Exception('connection closed.'))
            self.serve.close()


class TCPListener(object):
    def __init__(self, hub, host, port):
        self.hub = hub
        self.sock = s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, port))
        s.listen(socket.SOMAXCONN)
        s.setblocking(0)

        self.port = s.getsockname()[1]
        self.accept = hub.channel()

        self.ch = hub.register(s.fileno(), select.EPOLLIN)
        hub.spawn(self.loop)

    def loop(self):
        while True:
            try:
                self.ch.recv()
                conn, host = self.sock.accept()
                conn = TCPConn(self.hub, conn)
                self.accept.send(conn)
            except Stop:
                self.stop()
                return

    def stop(self):
        self.hub.unregister(self.sock.fileno())
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except:
            pass
        self.sock.close()


# HTTP #####################################################################


HTTP_VERSION = 'HTTP/1.1'


class Insensitive(object):
    Value = collections.namedtuple('Value', ['key', 'value'])

    def __init__(self):
        self.store = {}

    def __setitem__(self, key, value):
        self.store[key.lower()] = self.Value(key, value)

    def __getitem__(self, key):
        return self.store[key.lower()].value

    def __repr__(self):
        return repr(dict(self.store.itervalues()))

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default


class HTTP(object):
    def __init__(self, hub):
        self.hub = hub

    def listen(self, port=0, host='127.0.0.1', server=None):
        if server:
            return HTTPListener(self.hub, host, port, server)
        return functools.partial(HTTPListener, self.hub, host, port)

    def connect(self, url):
        return HTTPClient(self.hub, url)


class HTTPSocket(object):

    Status = collections.namedtuple('Status', ['version', 'code', 'message'])

    Request = collections.namedtuple(
        'Request', ['method', 'path', 'version', 'headers'])

    def __init__(self, fd):
        self.fd = fd

    def send(self, data):
        self.fd.send(data)

    def recv_bytes(self, n):
        return self.fd.recv_bytes(n)

    def recv_line(self):
        return self.fd.recv_partition('\r\n')

    def send_headers(self, headers):
        headers = '\r\n'.join(
            '%s: %s' % (k, v) for k, v in headers.iteritems())
        self.send(headers+'\r\n'+'\r\n')

    def recv_headers(self):
        headers = Insensitive()
        while True:
            line = self.recv_line()
            if not line:
                break
            k, v = line.split(': ', 1)
            headers[k] = v
        return headers

    def recv_request(self):
        method, path, version = self.recv_line().split(' ', 2)
        headers = self.recv_headers()
        return self.Request(method, path, version, headers)

    def recv_response(self):
        version, code, message = self.recv_line().split(' ', 2)
        code = int(code)
        status = self.Status(version, code, message)
        return status

    def send_response(self, code, message):
        self.send('HTTP/1.1 %s %s\r\n' % (code, message))

    def send_chunk(self, chunk):
        self.send('%s\r\n%s\r\n' % (hex(len(chunk))[2:], chunk))

    def recv_chunk(self):
        length = int(self.recv_line(), 16)
        if length:
            chunk = self.recv_bytes(length)
        else:
            chunk = ''
        assert self.recv_bytes(2) == '\r\n'
        return chunk


class WebSocket(object):
    MASK = FIN = 0b10000000
    RSV = 0b01110000
    OP = 0b00001111
    PAYLOAD = 0b01111111

    OP_TEXT = 0x1
    OP_BIN = 0x2
    OP_CLOSE = 0x8
    OP_PING = 0x9
    OP_PONG = 0xA

    SANITY = 1024**3  # limit fragments to 1GB

    def __init__(self, fd, is_client=True):
        self.fd = fd
        self.is_client = is_client

    @staticmethod
    def mask(mask, s):
        mask_bytes = [ord(c) for c in mask]
        return ''.join(
            chr(mask_bytes[i % 4] ^ ord(c)) for i, c in enumerate(s))

    @staticmethod
    def accept_key(key):
        value = key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
        return base64.b64encode(hashlib.sha1(value).digest())

    def send(self, data):
        length = len(data)

        MASK = WebSocket.MASK if self.is_client else 0

        if length <= 125:
            header = struct.pack(
                '!BB',
                WebSocket.OP_TEXT | WebSocket.FIN,
                length | MASK)

        elif length <= 65535:
            header = struct.pack(
                '!BBH',
                WebSocket.OP_TEXT | WebSocket.FIN,
                126 | MASK,
                length)
        else:
            assert length < WebSocket.SANITY, \
                "Frames limited to 1Gb for sanity"
            header = struct.pack(
                '!BBQ',
                WebSocket.OP_TEXT | WebSocket.FIN,
                127 | MASK,
                length)

        if self.is_client:
            mask = os.urandom(4)
            self.fd.send(header + mask + self.mask(mask, data))
        else:
            self.fd.send(header + data)

    def recv(self):
        b1, length, = struct.unpack('!BB', self.fd.recv_bytes(2))
        assert b1 & WebSocket.FIN, "Fragmented messages not supported yet"

        if self.is_client:
            assert not length & WebSocket.MASK
        else:
            assert length & WebSocket.MASK
            length = length & WebSocket.PAYLOAD

        if length == 126:
            length, = struct.unpack('!H', self.fd.recv_bytes(2))

        elif length == 127:
            length, = struct.unpack('!Q', self.fd.recv_bytes(8))

        assert length < WebSocket.SANITY, "Frames limited to 1Gb for sanity"

        if self.is_client:
            return self.fd.recv_bytes(length)

        mask = self.fd.recv_bytes(4)
        return self.mask(mask, self.fd.recv_bytes(length))


class HTTPClient(object):
    def __init__(self, hub, url):
        self.hub = hub

        parsed = urlparse.urlsplit(url)
        assert parsed.query == ''
        assert parsed.fragment == ''
        host, port = urllib.splitnport(parsed.netloc, 80)

        self.conn = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.conn.connect((host, port))
        self.conn.setblocking(0)

        self.http = HTTPSocket(FD(hub, self.conn.fileno()))

        self.agent = 'vanilla/%s' % __version__

        self.default_headers = dict([
            ('Accept', '*/*'),
            ('User-Agent', self.agent),
            ('Host', parsed.netloc), ])
            # ('Connection', 'Close'), ])

        self.responses = collections.deque()
        hub.spawn(self.receiver)

    def receiver(self):
        while True:
            status = self.http.recv_response()
            ch = self.responses.popleft()
            ch.send(status)

            headers = self.http.recv_headers()
            ch.send(headers)

            # If our connection is upgraded, shutdown the HTTP receive loop, as
            # this is no longer a HTTP connection.
            if headers.get('connection') == 'Upgrade':
                ch.close()
                return

            if headers.get('transfer-encoding') == 'chunked':
                while True:
                    chunk = self.http.recv_chunk()
                    if not chunk:
                        break
                    ch.send(chunk)
            else:
                # TODO:
                # http://www.w3.org/Protocols/rfc2616/rfc2616-sec4.html#sec4.4
                body = self.http.recv_bytes(int(headers['content-length']))
                ch.send(body)

            ch.close()

    def request(
            self,
            method,
            path='/',
            params=None,
            headers=None,
            version=HTTP_VERSION):

        request_headers = {}
        request_headers.update(self.default_headers)
        if headers:
            request_headers.update(headers)

        if params:
            path += '?' + urllib.urlencode(params)

        request = '%s %s %s\r\n' % (method, path, version)
        headers = '\r\n'.join(
            '%s: %s' % (k, v) for k, v in request_headers.iteritems())

        self.http.send(request+headers+'\r\n'+'\r\n')

        ch = self.hub.channel()
        self.responses.append(ch)
        return ch

    def get(self, path='/', params=None, headers=None, version=HTTP_VERSION):
        return self.request('GET', path, params, headers, version)

    def websocket(
            self, path='/', params=None, headers=None, version=HTTP_VERSION):

        key = base64.b64encode(uuid.uuid4().bytes)

        headers = headers or {}
        headers.update({
            'Upgrade': 'WebSocket',
            'Connection': 'Upgrade',
            'Sec-WebSocket-Key': key,
            'Sec-WebSocket-Version': 13, })

        response = self.request('GET', path, params, headers, version)

        status = response.recv()
        assert status.code == 101

        headers = response.recv()
        assert headers['Upgrade'].lower() == 'websocket'
        assert headers['Sec-WebSocket-Accept'] == WebSocket.accept_key(key)

        ws = WebSocket(self.http.fd)
        # TODO: the connection gets garbage collector unless we keep a
        # reference to it
        ws.conn = self.conn
        return ws


class HTTPListener(object):

    class Response(object):
        """
        manages the state of a HTTP Response
        """
        def __init__(self, request, http, chunks):
            self.request = request
            self.http = http
            self.chunks = chunks

            self.status = (200, 'OK')
            self.headers = {}

            self.is_init = False
            self.is_upgraded = False

        def send(self, data):
            self.chunks.send(data)

        def upgrade(self):
            assert self.request.headers['Connection'].lower() == 'upgrade'
            assert self.request.headers['Upgrade'].lower() == 'websocket'

            key = self.request.headers['Sec-WebSocket-Key']
            accept = WebSocket.accept_key(key)

            self.status = (101, 'Switching Protocols')
            self.headers.update({
                "Upgrade": "websocket",
                "Connection": "Upgrade",
                "Sec-WebSocket-Accept": accept, })

            self.init()
            self.is_upgraded = True
            self.chunks.close()

            return WebSocket(self.http.fd, is_client=False)

        def init(self):
            assert not self.is_init
            self.is_init = True
            self.http.send_response(*self.status)
            self.http.send_headers(self.headers)

        def end(self, data):
            if not self.is_upgraded:
                self.data = data or ''
                self.chunks.close()

        def send_chunk(self, chunk):
            if not self.is_init:
                self.headers['Transfer-Encoding'] = 'chunked'
                self.init()
            self.http.send_chunk(chunk)

        def send_body(self, body):
            if not self.is_init:
                self.headers['Content-Length'] = len(body)
                self.init()
            self.http.send(body)

    def __init__(self, hub, host, port, server):
        self.hub = hub

        self.sock = s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, port))
        s.listen(socket.SOMAXCONN)
        s.setblocking(0)

        self.port = s.getsockname()[1]
        self.server = server

        hub.spawn(self.accept)

    def accept(self):
        ready = self.hub.register(self.sock.fileno(), select.EPOLLIN)
        while True:
            try:
                ready.recv()
                conn, host = self.sock.accept()
                self.hub.spawn(self.serve, conn)
            except Stop:
                self.stop()
                return

    def serve(self, conn):
        conn.setblocking(0)
        http = HTTPSocket(FD(self.hub, conn.fileno()))

        #TODO: support http keep alives
        request = http.recv_request()
        response = self.Response(request, http, self.hub.channel())

        @self.hub.spawn
        def _():
            data = self.server(request, response)
            response.end(data)

        for chunk in response.chunks:
            response.send_chunk(chunk)

        if response.is_upgraded:
            # connection was upgraded, bail, as this is no longer a HTTP
            # connection
            return

        if response.is_init:
            # this must be a chunked transfer
            if response.data:
                http.send_chunk(response.data)
            http.send_chunk('')

        else:
            response.send_body(response.data)

        # TODO: work through cleanup
        # http.close()

    def stop(self):
        self.hub.unregister(self.sock.fileno())
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except:
            pass
        self.sock.close()
