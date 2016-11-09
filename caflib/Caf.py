from pathlib import Path
import os
import sys
import io
import tarfile
from base64 import b64encode
from itertools import takewhile
import imp
from textwrap import dedent
import hashlib
from collections import OrderedDict
import subprocess as sp
import shutil

from caflib.Utils import Configuration, mkdir, get_timestamp, filter_cmd, \
    timing, relink, print_timing
from caflib.Logging import error, info, colstr, Table, warn, log_caf, dep_error
from caflib.Context import get_stored, cellar, brewery
from caflib.CLI import CLI, CLIExit
from caflib.Context import Context
from caflib.Worker import QueueWorker, LocalWorker
from caflib.Remote import Remote
from caflib.Listing import find_tasks

try:
    from docopt import docopt, DocoptExit
except ImportError:
    dep_error('docopt')


latest = 'Latest'


def load_module(pathname, unpack):
    path = Path(pathname)
    modulename = path.stem
    module = imp.new_module(modulename)
    for i in range(2):
        try:
            exec(compile(path.open().read(), path.name, 'exec'), module.__dict__)
        except Exception as e:
            if isinstance(e, ImportError) and i == 0:
                unpack(None, path=None)
                continue
            import traceback
            traceback.print_exc()
            raise RuntimeError('Could not load "{}"'.format(pathname))
    return module


class Caf(CLI):
    def __init__(self, libpath):
        super().__init__('caf')
        self.conf = Configuration('.caf/conf.yaml')
        self.conf.set_global(Configuration('{}/.config/caf/conf.yaml'
                                           .format(os.environ['HOME'])))
        for cscriptname in ['cscript', 'cscript.py']:
            if Path(cscriptname).is_file():
                break
        else:
            cscriptname = None
        with timing('reading cscript'):
            try:
                self.cscript = load_module(cscriptname, self.commands[('unpack',)]._func) \
                    if cscriptname else object()
            except RuntimeError:
                error('There was an error while reading cscript.')
        self.out = Path(getattr(self.cscript, 'out', 'build'))
        self.cache = Path(getattr(self.cscript, 'cache', '.caf/db'))
        self.top = Path(getattr(self.cscript, 'top', '.'))
        self.cellar = self.cache/cellar
        self.brewery = self.cache/brewery
        self.remotes = {name: Remote(r['host'], r['path'], self.top)
                        for name, r in self.conf.get('remotes', {}).items()}
        self.libpath = libpath

    def __call__(self, argv):
        log_caf(argv)
        try:
            super().__call__(argv)  # try CLI as if local
        except CLIExit as e:  # store exception to reraise below if remote fails as well
            cliexit = e
        else:
            print_timing()
            return  # finished
        # the local CLI above did not succeed
        # make a usage without local CLI
        usage = '\n'.join(l for l in str(self).splitlines() if 'caf COMMAND' not in l)
        try:  # remote CLI failed as well, reraise CLIExit
            args = docopt(usage, argv=argv[1:], options_first=True, help=False)  # parse local
        except DocoptExit:
            raise cliexit
        rargv = [argv[0], args['COMMAND']] + args['ARGS']  # remote argv
        try:  # try CLI as if remote
            rargs = self.parse(rargv)  # remote parsed arguments
        except DocoptExit:  # remote CLI failed as well, reraise CLIExit
            raise cliexit
        if 'make' in rargs:
            if rargs['--queue']:  # substitute URL
                url = self.get_queue_url(rargs['--queue'], 'get')
                if url:
                    rargv = [arg if arg != rargs['--queue'] else url for arg in rargv]
            elif rargs['--last']:
                with open('.caf/LAST_QUEUE') as f:
                    queue_url = f.read().strip()
                last_index = rargv.index('--last')
                rargv = rargv[:last_index] + ['--queue', queue_url] + rargv[last_index+1:]
        remotes = self.proc_remote(args['REMOTE'])  # get Remote objects
        if args['COMMAND'] in ['conf', 'make']:
            for remote in remotes:
                remote.update()
        has_no_check = args['--no-check'] or self.conf.get('no_check')
        if 'make' in rargs and not rargs['conf'] and not has_no_check:
            for remote in remotes:
                remote.check(self.out)
        for remote in remotes:
            remote.command(' '.join(arg if ' ' not in arg else repr(arg)
                                    for arg in rargv[1:]))
            if 'make' in rargs and rargs['conf'] and not has_no_check:
                remote.check(self.out)

    def __format__(self, fmt):
        if fmt == 'header':
            return 'Caf -- Calculation framework.'
        elif fmt == 'usage':
            s = """\
            Usage:
                caf COMMAND [ARGS...]
                caf [--no-check] REMOTE COMMAND [ARGS...]
            """.rstrip()
            return dedent(s)
        elif fmt == 'options':
            s = """\
            Options:
                --no-check           Do not check remote cellar.
            """.rstrip()
            return dedent(s)
        else:
            return super().__format__(fmt)

    def get_queue_url(self, queue, action):
        if 'queue' in self.conf:
            if action == 'submit':
                if queue in self.conf['queue']:
                    return '{0[host]}/token/{0[token]}/submit'.format(self.conf['queue'][queue])
            elif action == 'get':
                host, queue = queue.split(':', 1)
                if host in self.conf['queue']:
                    return '{0[host]}/token/{0[token]}/queue/{1}/get' \
                        .format(self.conf['queue'][host], queue)
            elif action == 'append':
                host, queue = queue.split(':', 1)
                if host in self.conf['queue']:
                    return '{0[host]}/token/{0[token]}/queue/{1}/append' \
                        .format(self.conf['queue'][host], queue)

    def finalize(self, sig, frame):
        print_timing()
        sys.exit()

    def proc_remote(self, remotes):
        if remotes == 'all':
            remotes = self.remotes.values()
        else:
            try:
                remotes = [self.remotes[r] for r in remotes.split(',')]
            except KeyError as e:
                error('Remote "{}" is not defined'.format(e.args[0]))
        return remotes


def init(caf):
    if 'cache' in caf.conf:
        timestamp = get_timestamp()
        cache_path = Path(caf.conf['cache'])/'{}_{}'.format(Path().resolve().name, timestamp)
        mkdir(cache_path)
        relink(cache_path, caf.cache, relative=False)
    else:
        cache_path = caf.cache
        if cache_path.exists():
            error('{} exists, cannot overwrite'.format(cache_path))
        mkdir(cache_path)
    info('Initializing an empty repository at {}.'.format(cache_path))
    mkdir(caf.cellar)
    mkdir(caf.brewery)
    with open('.gitignore', 'w') as f:
        f.write('\n'.join(['.caf']))
    with open(os.devnull, 'w') as null:
        sp.call(['git', 'init'], stdout=null)
        sp.call(['git', 'add', 'caf', 'cscript.py', '.gitignore'], stdout=null)
        sp.call(['git', 'commit', '-m', 'initial commit'], stdout=null)


@Caf.command()
def conf(caf, dry: '--dry'):
    """
    Prepare tasks and targets defined in cscript.

    Usage:
        caf conf [--dry]

    Options:
        -n, --dry                  Dry run (do not write to disk).

    Tasks are created in .caf/db/Brewery/Latest and if their preparation does
    not depened on unfinished tasks, they are prepared and stored in
    .caf/db/Cellar based on their SHA1 hash. Targets (collections of symlinks to
    tasks) are created in ./build.
    """
    if not hasattr(caf.cscript, 'configure'):
        error('cscript has to contain function configure(ctx)')
    if not Path('.caf/db').exists():
        init(caf)
    ctx = Context(caf.cache/cellar, caf.top, caf.libpath)
    with timing('dependency tree'):
        caf.cscript.configure(ctx)
    if not dry:
        timestamp = get_timestamp()
        mkdir(caf.brewery/timestamp)
        relink(timestamp, caf.brewery/latest, relative=False)
        with timing('configure'):
            ctx.configure(caf.brewery/latest)
        if caf.out.is_dir():
            shutil.rmtree(str(caf.out))
        mkdir(caf.out)
        with timing('targets'):
            ctx.make_targets(caf.out, caf.cache)
        if hasattr(caf.cscript, 'json'):
            warn('Make sure json is not printing dictionaries in features')
    with open(os.devnull, 'w') as null:
        sp.call(['git', 'add', '--all', 'build'], stdout=null)
        sp.call(['git', 'commit', '-a', '-m', '#configuration'], stdout=null)


@Caf.command(triggers=['conf make'])
def make(caf, profile: '--profile', n: ('-j', int), targets: 'TARGET',
         limit: ('--limit', int), queue: '--queue', myid: '--id',
         dry: '--dry', do_conf: 'conf', verbose: '--verbose',
         last_queue: '--last', maxdepth: ('--maxdepth', int)):
    """
    Execute all prepared build tasks.

    Usage:
        caf [conf] make [-v] [--limit N]
                                [--profile PROFILE [-j N] | [--id ID] [--dry]]
                                [--last | --queue URL | [TARGET...] [--maxdepth N]]

    Options:
        -n, --dry                  Dry run (do not write to disk).
        --id ID                    ID of worker [default: 1].
        -p, --profile PROFILE      Run worker via ~/.config/caf/worker_PROFILE.
        -q, --queue URL            Take tasks from web queue.
        --last                     As above, but use the last submitted queue.
        -j N                       Number of launched workers [default: 1].
        -l, --limit N              Limit number of tasks to N.
        -v, --verbose              Be more verbose.
        --maxdepth N               Maximal depth.
    """
    import subprocess
    if do_conf:
        configure(['caf', 'conf'], caf)
    if profile:
        for _ in range(n):
            cmd = ['{}/.config/caf/worker_{}'.format(os.environ['HOME'], profile),
                   '-v' if verbose else None, ('--limit', limit),
                   ('--queue', queue), targets, ('--maxdepth', maxdepth)]
            try:
                subprocess.check_call(filter_cmd(cmd))
            except subprocess.CalledProcessError:
                error('Running ~/.config/caf/worker_{} did not succeed.'
                      .format(profile))
    else:
        if queue or last_queue:
            if last_queue:
                with open('.caf/LAST_QUEUE') as f:
                    queue = f.read().strip()
            url = caf.get_queue_url(queue, 'get') or queue
            worker = QueueWorker(myid, caf.cache, url,
                                 dry=dry, limit=limit, debug=verbose)
        else:
            roots = [caf.out/t for t in targets] \
                if targets else (caf.out).glob('*')
            tasks = OrderedDict()
            for path in find_tasks(*roots, unsealed=True, maxdepth=maxdepth):
                cellarid = get_stored(path)
                if cellarid not in tasks:
                    tasks[cellarid] = str(path)
            worker = LocalWorker(myid, caf.cache,
                                 list(reversed(tasks.items())),
                                 dry=dry, limit=limit, debug=verbose)
        worker.work()


@Caf.command(triggers=['conf submit'])
def submit(caf, targets: 'TARGET', queue: 'URL', maxdepth: ('--maxdepth', int),
           do_conf: 'conf'):
    """
    Submit the list of prepared tasks to a queue server.

    Usage:
        caf [conf] submit URL [TARGET...] [--maxdepth N]

    Options:
        --maxdepth N             Maximum depth.
    """
    from urllib.request import urlopen
    if do_conf:
        configure(['caf', 'conf'], caf)
    url = caf.get_queue_url(queue, 'submit') or queue
    roots = [caf.out/t for t in targets] \
        if targets else (caf.out).glob('*')
    tasks = OrderedDict()
    for path in find_tasks(*roots, unsealed=True, maxdepth=maxdepth):
        cellarid = get_stored(path)
        if cellarid not in tasks:
            tasks[cellarid] = path
    if not tasks:
        error('No tasks to submit')
    data = '\n'.join('{} {}'.format(label, h)
                     for h, label in reversed(tasks.items())).encode()
    with urlopen(url, data=data) as r:
        queue_url = r.read().decode()
        print('./caf make --queue {}'.format(queue_url))
    with open('.caf/LAST_QUEUE', 'w') as f:
        f.write(queue_url)


@Caf.command()
def append(caf, targets: 'TARGET', queue: 'URL', maxdepth: ('--maxdepth', int)):
    """
    Append the list of prepared tasks to a given queue.

    Usage:
        caf append URL [TARGET...] [--maxdepth N]

    Options:
        --maxdepth N             Maximum depth.
    """
    from urllib.request import urlopen
    url = caf.get_queue_url(queue, 'append') or queue
    roots = [caf.out/t for t in targets] \
        if targets else (caf.out).glob('*')
    tasks = OrderedDict()
    for path in find_tasks(*roots, unsealed=True, maxdepth=maxdepth):
        cellarid = get_stored(path)
        if cellarid not in tasks:
            tasks[cellarid] = path
    if not tasks:
        error('No tasks to submit')
    data = '\n'.join('{} {}'.format(label, h)
                     for h, label in reversed(tasks.items())).encode()
    with urlopen(url, data=data) as r:
        queue_url = r.read().decode()
        print('./caf make --queue {}'.format(queue_url))
    with open('.caf/LAST_QUEUE', 'w') as f:
        f.write(queue_url)


@Caf.command()
def reset(caf, targets: 'TARGET'):
    """
    Remove working lock and error on tasks.

    Usage:
        caf reset [TARGET...]
    """
    roots = [caf.out/t for t in targets] if targets else (caf.out).glob('*')
    for path in find_tasks(*roots):
        if (path/'.lock').is_dir():
            (path/'.lock').rmdir()
        if (path/'.caf/error').is_file():
            (path/'.caf/error').unlink()


caf_list = CLI('list', header='List various entities.')
Caf.commands[('list',)] = caf_list


@caf_list.add_command(name='profiles')
def list_profiles(caf, _):
    """
    List profiles.

    Usage:
        caf list profiles
    """
    for p in Path(os.environ['HOME']).glob('.config/caf/worker_*'):
        print(p.name)


@caf_list.add_command(name='remotes')
def list_remotes(caf, _):
    """
    List remotes.

    Usage:
        caf list remotes
    """
    remote_conf = Configuration()
    remote_conf.update(caf.conf.get('remotes', {}))
    print(remote_conf)


@caf_list.add_command(name='tasks')
def list_tasks(caf, _, do_finished: '--finished', do_stored: '--stored',
               do_error: '--error', do_unfinished: '--unfinished',
               in_cellar: '--cellar', both_paths: '--both',
               maxdepth: ('--maxdepth', int), targets: 'TARGET'):
    """
    List tasks.

    Usage:
        caf list tasks [TARGET...] [--finished | --stored | --error | --unfinished]
                       [--cellar | --both] [--maxdepth N]

    Options:
        --finished                 List finished tasks.
        --unfinished               List unfinished tasks.
        --stored                   List stored tasks.
        --error                    List tasks in error.
        --cellar                   Print path in cellar.
        --both                     Print path in build and cellar.
        --maxdepth N               Specify maximum depth.
    """
    roots = [caf.out/t for t in targets] if targets else (caf.out).glob('*')
    if do_finished:
        paths = find_tasks(*roots, sealed=True, maxdepth=maxdepth)
    elif do_unfinished:
        paths = find_tasks(*roots, unsealed=True, maxdepth=maxdepth)
    elif do_stored:
        paths = find_tasks(*roots, stored=True, maxdepth=maxdepth)
    elif do_error:
        paths = find_tasks(*roots, error=True, maxdepth=maxdepth)
    else:
        paths = find_tasks(*roots, maxdepth=maxdepth)
    if in_cellar:
        for path in paths:
            print(get_stored(path, require=False))
    elif both_paths:
        for path in paths:
            print(path, get_stored(path, require=False))
    else:
        for path in paths:
            print(path)


# @Caf.command()
def search(caf, older: '--older', contains: '--contains',
           contains_not: '--contains-not'):
    """
    Search within stored tasks.

    Usage:
        caf search [--contains PATTERN] [--contains-not PATTERN] [--older TIME]

    Options:
        --contains PATTERN         Search tasks containing PATTERN.
        --contains-not PATTERN     Search tasks not containing PATTERN.
        --older TIME               Search tasks older than.
    """
    import subprocess
    cmd = ['find', str(caf.cellar), '-maxdepth', '3',
           '-mindepth', '3', '-type', 'd']
    if older:
        lim = older
        if lim[0] not in ['-', '+']:
            lim = '+' + lim
        cmd.extend(['-ctime', lim])
    if contains:
        cmd.extend(['-exec', 'test', '-e', '{{}}/{}'.format(contains), ';'])
    if contains_not:
        cmd.extend(['!', '-exec', 'test', '-e', '{{}}/{}'.format(contains_not), ';'])
    cmd.append('-print')
    subprocess.call(cmd)


@Caf.command()
def status(caf, targets: 'TARGET'):
    """
    Print number of initialized, running and finished tasks.

    Usage:
        caf status [TARGET...]
    """
    def colored(stat):
        colors = 'blue green cyan red yellow normal'.split()
        return [colstr(s, color) if s else colstr(s, 'normal')
                for s, color in zip(stat, colors)]

    dirs = []
    if not targets:
        dirs.append((caf.brewery/latest, (caf.brewery/latest).glob('*')))
    targets = [caf.out/t for t in targets] \
        if targets else (caf.out).glob('*')
    for target in targets:
        if not target.is_dir() or str(target).startswith('.'):
            continue
        if target.is_symlink():
            dirs.append((target, [target]))
        else:
            dirs.append((target, target.glob('*')))
    print('number of {} tasks:'
          .format('/'.join(colored('running finished marked error prepared all'.split()))))
    table = Table(align=['<', *6*['>']], sep=[' ', *5*['/']])
    for directory, paths in sorted(dirs):
        stats = []
        locked = []
        for p in paths:
            stats.append(((p/'.lock').is_dir(), (p/'.caf/seal').is_file(),
                          (p/'.caf/mark').is_file(),
                          (p/'.caf/error').is_file(), (p/'.caf/lock').is_file(),
                          (p/'.caf').is_dir()))
            if (p/'.lock').is_dir():
                locked.append(p)
        stats = colored([stat.count(True) for stat in zip(*stats)])
        table.add_row(str(directory) + ':', *stats)
        if directory.parts[1] != 'Brewery':
            for path in locked:
                table.add_row('{} {}'.format(colstr('>>', 'blue'), path), free=True)
    print(table)


@Caf.command()
def cmd(caf, cmd: 'CMD'):
    """
    Execute any shell command.

    Usage:
        caf cmd CMD

    This is a simple convenience alias for running commands remotely.
    """
    import subprocess
    subprocess.call(cmd, shell=True)


caf_remote = CLI('remote', header='Manage remotes.')
Caf.commands[('remote',)] = caf_remote


@caf_remote.add_command(name='add')
def remote_add(caf, _, url: 'URL', name: 'NAME'):
    """
    Add a remote.

    Usage:
        caf remote add URL [NAME]
    """
    host, path = url.split(':')
    name = name or host
    if 'remotes' not in caf.conf:
        caf.conf['remotes'] = {}
    caf.conf['remotes'][name] = {'host': host, 'path': path}
    caf.conf.save()


@caf_remote.add_command(name='path')
def remote_path(caf, _, name: 'NAME'):
    """
    Print a remote path in the form HOST:PATH.

    Usage:
        caf remote path NAME
    """
    print('{0[host]}:{0[path]}'.format(caf.conf['remotes'][name]))


@Caf.command()
def update(caf, delete: '--delete', remotes: ('REMOTE', 'proc_remote')):
    """
    Sync the contents of . to remote excluding .caf/db and ./build.

    Usage:
        caf update REMOTE [--delete]

    Options:
        --delete                   Delete files when syncing.
    """
    for remote in remotes:
        remote.update(delete=delete)


@Caf.command()
def check(caf, remotes: ('REMOTE', 'proc_remote')):
    """
    Verify that hashes of the local and remote tasks match.

    Usage:
        caf check REMOTE
    """
    for remote in remotes:
        remote.check(caf.out)


@Caf.command()
def push(caf, targets: 'TARGET', dry: '--dry', remotes: ('REMOTE', 'proc_remote')):
    """
    Push targets to remote and store them in remote Cellar.

    Usage:
        caf push REMOTE [TARGET...] [--dry]

    Options:
        -n, --dry                  Dry run (do not write to disk).
    """
    for remote in remotes:
        remote.push(targets, caf.cache, caf.out, dry=dry)


@Caf.command()
def fetch(caf, dry: '--dry', targets: 'TARGET', remotes: ('REMOTE', 'proc_remote'),
          get_all: '--all', follow: '--follow'):
    """
    Fetch targets from remote and store them in local Cellar.

    Usage:
        caf fetch REMOTE [TARGET...] [--dry] [--all] [--follow]

    Options:
        -n, --dry         Dry run (do not write to disk).
        --all             Do not check which tasks are finished.
        --follow          Follow dependencies.
    """
    for remote in remotes:
        remote.fetch(targets, caf.cache, caf.out, dry=dry, get_all=get_all, follow=follow)


@Caf.command()
def template(caf):
    """
    Write a template cscript.

    Usage:
        caf template
    """
    with open('cscript', 'w') as f:
        f.write(dedent("""\
            #!/usr/bin/env python3


            def configure(ctx):
                pass
        """))


@Caf.command()
def go(caf, remotes: ('REMOTE', 'proc_remote')):
    """
    SSH into the remote caf repository.

    Usage:
        caf go REMOTE
    """
    for remote in remotes:
        remote.go()


@Caf.command()
def strip(caf):
    """
    Strip packed caflib from the caf executable.

    Usage:
        caf strip
    """
    with open('caf') as f:
        lines = takewhile(lambda l: l != '# ==>\n', f.readlines())
    with open('caf', 'w') as f:
        for line in lines:
            f.write(line)


@Caf.command()
def pack(caf):
    """
    Pack caflib into the caf executable.

    Usage:
        caf pack
    """
    strip(['caf', 'strip'], caf)
    h = hashlib.new('md5')
    with io.BytesIO() as ftar:
        archive = tarfile.open(mode='w|gz', fileobj=ftar)
        for path in sorted(Path('caflib').glob('**/*.py')):
            archive.add(str(path))
            with path.open('rb') as f:
                h.update(f.read())
        archive.close()
        archive = ftar.getvalue()
    version = h.hexdigest()
    with open('caf', 'a') as f:
        f.write('# ==>\n')
        f.write('# version: {}\n'.format(version))
        f.write('# archive: {}\n'.format(b64encode(archive).decode()))
        f.write('# <==\n')


@Caf.command()
def upgrade(caf):
    """
    Update itself from https://pub.janhermann.cz/.

    Usage:
        caf upgrade
    """
    os.system('curl https://pub.janhermann.cz/static/caf >caf && chmod +x caf')
