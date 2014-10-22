import logging
import socket
import threading
import time
from collections import defaultdict, deque
from heapq import heappush, heappop

__author__ = 'Quantum'
logger = logging.getLogger('event_socket_server')


class SendMessage(object):
    __slots__ = ('data', 'callback')

    def __init__(self, data, callback):
        self.data = data
        self.callback = callback


class ScheduledJob(object):
    __slots__ = ('time', 'func', 'args', 'kwargs', 'cancel', 'dispatched')

    def __init__(self, time, func, args, kwargs):
        self.time = time
        self.func = func
        self.args = args
        self.kwargs = kwargs
        self.cancel = False
        self.dispatched = False


class BaseServer(object):
    def __init__(self, host, port, client):
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setblocking(0)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind((host, port))
        self._stop = threading.Event()
        self._clients = set()
        self._ClientClass = client
        self._send_queue = defaultdict(deque)
        self._job_queue = []
        self._job_queue_lock = threading.Lock()

    def _serve(self):
        raise NotImplementedError()

    def _accept(self):
        conn, address = self._server.accept()
        conn.setblocking(0)
        client = self._ClientClass(self, conn)
        self._clients.add(client)
        return client

    def schedule(self, delay, func, *args, **kwargs):
        with self._job_queue_lock:
            job = ScheduledJob(time.time() + delay, func, args, kwargs)
            heappush(self._job_queue, job)
            return job

    def unschedule(self, job):
        with self._job_queue_lock:
            if job.dispatched or job.cancel:
                return False
            job.cancel = True
            return True

    def _register_write(self, client):
        raise NotImplementedError()

    def _register_read(self, client):
        raise NotImplementedError()

    def _clean_up_client(self, client, finalize=False):
        try:
            del self._send_queue[client.fileno()]
        except KeyError:
            pass
        client.on_close()
        client._socket.close()
        if not finalize:
            self._clients.remove(client)

    def _dispatch_event(self):
        t = time.time()
        tasks = []
        with self._job_queue_lock:
            while True:
                dt = self._job_queue[0].time - t if self._job_queue else 1
                if dt > 0:
                    break
                task = heappop(self._job_queue)
                task.dispatched = True
                if not task.cancel:
                    tasks.append(task)
        for task in tasks:
            task.func(*task.args, **task.kwargs)
        if not self._job_queue or dt > 1:
            dt = 1
        return dt

    def _nonblock_read(self, client):
        try:
            data = client._socket.recv(1024)
        except socket.error:
            self._clean_up_client(client)
        else:
            if not data:
                self._clean_up_client(client)
            else:
                try:
                    client._recv_data(data)
                except Exception:
                    logger.exception('Client recv_data failure')
                    self._clean_up_client(client)

    def _nonblock_write(self, client):
        fd = client.fileno()
        queue = self._send_queue[fd]
        try:
            top = queue[0]
            cb = client._socket.send(top.data)
            top.data = top.data[cb:]
            if not top.data:
                if top.callback is not None:
                    try:
                        top.callback()
                    except Exception:
                        logger.exception('Client write callback failure')
                        self._clean_up_client(client)
                        return
                queue.popleft()
                if not queue:
                    self._register_read(client)
                    del self._send_queue[fd]
        except socket.error:
            self._clean_up_client(client)

    def send(self, client, data, callback=None):
        self._send_queue[client.fileno()].append(SendMessage(data, callback))
        self._register_write(client)

    def stop(self):
        self._stop.set()

    def serve_forever(self):
        self._serve()

    def on_shutdown(self):
        pass