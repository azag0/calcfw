import json
import subprocess
from pathlib import Path
from signal import signal, SIGINT, SIGTERM, SIGXCPU
import sys
import os
from datetime import datetime
from abc import ABCMeta, abstractmethod
from urllib.request import urlopen
from urllib.error import HTTPError, URLError
from http.client import HTTPSConnection
from urllib.parse import urlencode
import socket
from contextlib import contextmanager

from caflib.Utils import cd, Configuration


class Worker(metaclass=ABCMeta):
    verify_lock = True

    def __init__(self, myid, root, dry=False, limit=None, debug=False):
        self.myid = myid
        self.root = Path(root)
        self.dry = dry
        self.limit = limit
        self.debug = debug
        signal(SIGINT, self.signal_handler)
        signal(SIGTERM, self.signal_handler)
        self.info('Alive and ready.')

    def signal_handler(self, sig, frame):
        self.info('Interrupted, quitting.')
        sys.exit()

    def info(self, msg):
        print('Worker {}: {:%Y-%m-%d %H:%M:%S}: {}'.format(
            self.myid, datetime.today(), msg
        ))

    def debug_info(self, msg):
        if self.debug:
            self.info(msg)

    @abstractmethod
    def get_task(self):
        pass

    @abstractmethod
    def put_back(self, label, taskid):
        pass

    @abstractmethod
    def task_done(self, taskid):
        pass

    @abstractmethod
    def task_error(self, taskid):
        pass

    def work(self):
        n_done = 0
        for label, taskid in self.locked_tasks():
            self.info('Started working on {} ({})...'.format(label, taskid))
            if not self.dry:
                self.run_command(taskid)
            self.info('Finished working on {}.'.format(label))
            n_done += 1
            if self.limit and n_done >= self.limit:
                self.info('Reached limit of tasks, quitting.')
                break

    def locked_tasks(self):
        while True:
            with self.get_locked_task() as (label, taskid):
                if not taskid:
                    return
                yield label, taskid

    @contextmanager
    def get_locked_task(self):
        skipped = set()
        for label, taskid in self.tasks(skipped):
            self.debug_info('Trying task {}...'.format(taskid))
            taskpath = self.root/taskid
            self.current_taskid = taskid
            lockpath = taskpath/'.lock'
            if (taskpath/'.caf/seal').is_file():
                self.debug_info('Task {} is sealed, continue.'.format(taskid))
                self.task_done(taskid)
            elif (taskpath/'.caf/error').is_file() and self.verify_lock:
                self.debug_info('Task {} is in error, continue.'.format(taskid))
                self.task_error(taskid)
            elif not all(
                (p/'.caf/seal').is_file() for p in get_children(taskpath)
            ) and not self.dry:
                self.debug_info(
                    'Task {} has unsealed children, put back and continue.'
                    .format(taskid)
                )
                self.put_back(label, taskid)
                skipped.add(taskid)
            else:
                try:
                    lockpath.mkdir()
                except OSError:
                    if not self.verify_lock:
                        break
                    self.debug_info('Task {} is locked, continue.'.format(taskid))
                else:
                    break  # we have acquired lock
        else:  # there is no task left
            label = None
            taskid = None
            lockpath = None
        try:
            yield label, taskid
        finally:
            if lockpath:
                lockpath.rmdir()

    def tasks(self, skipped):
        while True:
            label, taskid = self.get_task()
            if taskid is None:
                self.info('No more tasks in queue, quitting.')
                return
            elif taskid in skipped:
                self.put_back(label, taskid)
                self.info('All tasks have been skipped, quitting.')
                return
            else:
                yield label, taskid

    def run_command(self, taskid):
        with cd(self.root/taskid):
            if Path('command').is_file():
                with open('command') as f:
                    command = f.read()
            else:
                command = ''
            if Path('.caf/env').is_file():
                command = 'source .caf/env\n' + command
            with open('run.out', 'w') as stdout, open('run.err', 'w') as stderr:
                try:
                    subprocess.check_call(
                        command, shell=True, stdout=stdout, stderr=stderr
                    )
                except subprocess.CalledProcessError as e:
                    print(e)
                    self.info(
                        'error: There was an error when working on {}'.format(taskid)
                    )
                    with Path('.caf/error').open('w') as f:
                        f.write(self.myid + '\n')
                    self.task_error(taskid)
                else:
                    if 'CAFWAIT' in os.environ:
                        from time import sleep
                        sleep(int(os.environ['CAFWAIT']))
                    with Path('.caf/seal').open('w') as f:
                        f.write(self.myid + '\n')
                    if Path('.caf/error').is_file():
                        Path('.caf/error').unlink()
                    self.task_done(taskid)


def get_children(path):
    with (path/'.caf/children').open() as f:
        return [path/child for child in json.load(f)]


class LocalWorker(Worker):
    def __init__(self, myid, root, queue, dry=False, limit=None, debug=False):
        super().__init__(myid, root, dry, limit, debug)
        self.queue = queue

    def get_task(self):
        try:
            taskid, label = self.queue.pop(0)
            return label, taskid
        except IndexError:
            return None, None

    def put_back(self, label, taskid):
        self.queue.append((taskid, label))

    def task_done(self, taskid):
        pass

    def task_error(self, taskid):
        pass


curl_pushover = """\
-F "token={token:}" -F "user={user:}" -F "title=Worker" -F "message={message:}" \
https://api.pushover.net/1/messages.json >/dev/null"""


class QueueWorker(Worker):
    verify_lock = False

    def __init__(self, myid, root, url, dry=False, limit=None, debug=False):
        super().__init__(myid, root, dry, limit, debug)
        conf = Configuration(os.environ['HOME'] + '/.config/caf/conf.yaml')
        self.curl = conf.get('curl')
        self.pushover = conf.get('pushover')
        self.url = url + '?caller=' + socket.gethostname()
        self.url_state = {}
        self.url_putback = {}
        self.has_warned = False
        signal(SIGXCPU, self.signal_handler)

    def interrupt(self):
        self.call_pushover(
            'Worker #{} on {} will be soon interrupted'
            .format(self.myid, socket.gethostname())
        )
        self.put_back(None, self.current_taskid)
        sys.exit()

    def signal_handler(self, sig, frame):
        if not self.has_warned:
            self.has_warned = True
            self.info('Will be soon interrupted.')
            self.interrupt()
        elif not sig == SIGXCPU:
            super().signal_handler(sig, frame)

    def call_url(self, url):
        if self.curl:
            subprocess.check_call(self.curl % url, shell=True)
        else:
            with urlopen(url, timeout=30):
                pass

    def call_pushover(self, msg):
        if not self.pushover:
            return
        token = self.pushover['token']
        user = self.pushover['user']
        if self.curl:
            subprocess.check_call(
                self.curl % curl_pushover.format(token=token, user=user, message=msg),
                shell=True
            )
        else:
            conn = HTTPSConnection('api.pushover.net:443')
            conn.request(
                'POST',
                '/1/messages.json',
                urlencode({'token': token, 'user': user, 'message': msg}),
                {'Content-type': 'application/x-www-form-urlencoded'}
            )
            conn.getresponse()

    def get_task(self):
        if self.curl:
            try:
                response = subprocess.check_output(
                    self.curl % self.url, shell=True).decode()
            except subprocess.CalledProcessError as e:
                if e.returncode == 22:
                    return None, None
                else:
                    raise
        else:
            try:
                with urlopen(self.url, timeout=30) as r:
                    response = r.read().decode()
            except HTTPError:
                return None, None
            except URLError as e:
                self.info(
                    'error: Cannot connect to {}: {}'.format(self.url, e.reason)
                )
                return None, None
        task, label, url_state, url_putback = response.split()
        self.url_state[task] = url_state
        self.url_putback[task] = url_putback
        return label, task

    def put_back(self, label, taskid):
        self.call_url(self.url_putback.pop(taskid))

    def task_done(self, taskid):
        self.call_url(self.url_state.pop(taskid) + '?state=Done')

    def task_error(self, taskid):
        self.call_url(self.url_state.pop(taskid) + '?state=Error')
