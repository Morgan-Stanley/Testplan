"""Remote worker pool module."""

import os
import sys
import time
import signal
import socket
import getpass
import platform
import subprocess
import six
import itertools

from schema import Or

import testplan
from testplan.common.utils.logger import TESTPLAN_LOGGER
from testplan.common.config import ConfigOption
from testplan.common.utils.path import (module_abspath,
                                        pwd, makedirs, fix_home_prefix)
from testplan.common.utils.strings import slugify
from testplan.common.utils.remote import (
    ssh_cmd, copy_cmd, link_cmd, remote_filepath_exists)
from testplan.common.utils import path as pathutils

from .base import Pool, PoolConfig
from .process import ProcessWorker, ProcessWorkerConfig
from .connection import TCPConnectionManager
from .communication import Message


class WorkerSetupMetadata(object):
    """
    Metadata used on worker setup stage execution.
    Pushed dirs and files will be registered for deletion at exit.
    """

    def __init__(self):
        self.push_dirs = None
        self.push_files = None
        self.push_dir = None
        self.setup_script = None
        self.env = None
        self.workspace_paths = None
        self.workspace_pushed = False


class RemoteWorkerConfig(ProcessWorkerConfig):
    """
    Configuration object for
    :py:class:`~testplan.runners.pools.remote.RemoteWorker` resource entity.

    :param workers: Number of remote workers of remote pool of child worker.
    :type workers: ``int``
    :param pool_type: Remote pool type that child worker will use.
    :type pool_type: ``str``

    Also inherits all :py:class:`~testplan.runners.pools.process.ProcessWorkerConfig`
    options.
    """

    @classmethod
    def get_options(cls):
        """
        Schema for options validation and assignment of default values.
        """
        return {
            'workers': int,
            'pool_type': str,
        }


class _LocationPaths(object):
    """Store local and remote equivalent paths."""

    def __init__(self, local=None, remote=None):
        self.local = local
        self.remote = remote

    def __iter__(self):
        return iter((self.local, self.remote))


class RemoteWorker(ProcessWorker):
    """
    Remote worker resource that pulls tasks from the transport provided,
    executes them in a local pool of workers and sends back task results.
    """

    CONFIG = RemoteWorkerConfig

    def __init__(self, **options):
        super(RemoteWorker, self).__init__(**options)
        self._remote_testplan_path = None
        self._user = getpass.getuser()
        self._workspace_paths = _LocationPaths()
        self._child_paths = _LocationPaths()
        self._working_dirs = _LocationPaths()
        self._should_transfer_workspace = True
        self._remote_testplan_runpath = None
        self.setup_metadata = WorkerSetupMetadata()
        self.remote_push_dir = None

    def _execute_cmd(
            self, cmd, label=None, check=True, stdout=None, stderr=None):
        """
        Execute a subprocess command.

        :param cmd: Command to execute - list of parameters.
        :param label: Optional label for debugging
        :param check: When True, check that the return code of the command is 0 to
                ensure success - raises a RuntimeError otherwise. Defaults to
                True - should be explicitly disabled for commands that may
                legitimately return non-zero return codes.
        :param stdout: Optional file-like object to redirect stdout to.
        :param stderr: Optional file-like object to redirect stderr to.
        :return: Return code of the command (always 0 unless check=False is
                 set).
        """
        self.logger.debug('Executing command{}: {}'.format(
            ' [{}]'.format(label) if label else '', cmd))
        start_time = time.time()

        if stdout is None:
            stdout = sys.stdout
        if stderr is None:
            stderr = sys.stderr

        handler = subprocess.Popen(
            [str(a) for a in cmd],
            stdout=stdout, stderr=stderr, stdin=subprocess.PIPE)
        handler.stdin.write(bytes('y\n'.encode('utf-8')))
        handler.wait()
        if label:
            self.logger.debug('Command [{}] finished in {}s.'.format(
                label, time.time()-start_time))

        if check and handler.returncode != 0:
            raise RuntimeError(
                'Command "{cmd}" returned non-zero exit code {rc}'
                .format(cmd=cmd, rc=handler.returncode))

        return handler.returncode

    def _execute_cmd_remote(self, cmd, label=None, check=True):
        """
        Execute a command on the remote host.

        :param cmd: Remote command to execute - list of parameters.
        :param label: Optional label for debugging.
        :param check: Whether to check command return-code - defaults to True.
                      See self._execute_cmd for more detail.
        """
        self._execute_cmd(
            self.cfg.ssh_cmd(self.cfg.index, ' '.join([str(a) for a in cmd])),
            label=label,
            check=check)

    def _mkdir_remote(self, remote_dir, label=None):
        """
        Create a directory path on the remote host.

        :param remote_dir: Path to create.
        :param label: Optional debug label.
        """
        if not label:
            label = 'remote mkdir'

        cmd = self.cfg.remote_mkdir + [remote_dir]
        self._execute_cmd(self.cfg.ssh_cmd(
            self.cfg.index, ' '.join([str(a) for a in cmd])),
            label=label)

    def _define_remote_dirs(self):
        """Define mandatory directories in remote host."""
        testplan_path_dirs = ['', 'var', 'tmp', getpass.getuser(), 'testplan']
        self._remote_testplan_path = '/'.join(
            testplan_path_dirs + ['remote_workspaces',
                                  slugify(self.cfg.parent.parent.name)])
        self._remote_testplan_runpath = '/'.join(
            [self._remote_testplan_path, 'runpath', str(self.cfg.index)])

    def _create_remote_dirs(self):
        """Create mandatory directories in remote host."""
        cmd = self.cfg.remote_mkdir + [self._remote_testplan_path]
        self._execute_cmd(
            self.cfg.ssh_cmd(self.cfg.index, ' '.join([str(a) for a in cmd])),
            label='create remote dirs')

    def _copy_child_script(self):
        """Copy the remote worker executable file."""
        self._child_paths.remote = '{}/child.py'.format(
            self._remote_testplan_path)
        self._transfer_data(
            source=self._child_paths.local,
            target=self._child_paths.remote,
            remote_target=True)

    def _copy_dependencies_module(self):
        """Copy mandatory dependencies need to be imported before testplan."""
        path = os.environ.get('TESTPLAN_DEPENDENCIES_PATH')
        if path is None:
            return
        local_path = '{}/dependencies.py'.format(path)
        remote_path = '{}/dependencies.py'.format(self._remote_testplan_path)
        self._transfer_data(
            source=local_path,
            target=remote_path,
            remote_target=True)

    def _push_files(self):
        """Push files and directories to remote host."""
        # Short-circuit if we've been given no files to push.
        if not self.cfg.push:
            if self.cfg.push_exclude or self.cfg.push_relative_dir:
                self.logger.warning('Not been given any files to push - '
                                    'ignoring push configuration options.')
            return

        # First enumerate the files and directories to be pushed, including
        # both their local source and remote destinations.
        push_files, push_dirs = self._build_push_lists()

        # Add the remote paths to the setup metadata.
        self.setup_metadata.push_files = [path.remote for path in push_files]
        self.setup_metadata.push_dirs = [path.remote for path in push_dirs]

        # Now actually push the files to the remote host.
        self._push_files_to_dst(push_files, push_dirs)

    def _build_push_lists(self):
        """
        Create lists of the source and destination paths of files and
        directories to be pushed. Eliminate duplication of sub-directories.

        :return: Tuple containing lists of files and directories to be pushed.
        """
        # Inspect types. Push config may either be a list of string paths, e.g:
        # ['/path/to/file1', '/path/to/file1']
        #
        # Or it may be a list of tuples where the destination for each source
        # is specified also:
        # [('/local/path/to/file1', '/remote/path/to/file1'),
        #  ('/local/path/to/file2', '/remote/path/to/file2')]
        if all(isinstance(cfg, six.string_types) for cfg in self.cfg.push):
            push_sources = self.cfg.push
            push_dsts = self._build_push_dests(push_sources)
            push_locations = zip(push_sources, push_dsts)
        else:
            if not all(len(pair) == 2 for pair in self.cfg.push):
                raise TypeError(
                    'Expected either a list of 2-tuples or list of strings for '
                    'push config.')
            if self.cfg.push_relative_dir:
                self.logger.warning(
                    'Ignoring push_relative_dir configuration '
                    'as explicit destination paths have been provided.')
            push_locations = self.cfg.push

        # Now seperate the push sources into lists of files and directories.
        push_files = []
        push_dirs = []

        for source, dest in push_locations:
            source = source.rstrip(os.sep)
            if os.path.isfile(source):
                push_files.append(_LocationPaths(source, dest))
            elif os.path.isdir(source):
                push_dirs.append(_LocationPaths(source, dest))
            else:
                self.logger.error('Item "{}" cannot be pushed!'.format(source))

        # Eliminate push duplications
        if push_dirs and len(push_dirs) > 1:
            push_dirs.sort(key=lambda x: x.local)
            for idx in range(len(push_dirs) - 1):
                if push_dirs[idx + 1].local.startswith(push_dirs[idx].local):
                    push_dirs[idx] = None
            push_dirs = [_dir for _dir in push_dirs if _dir is not None]

        return push_files, push_dirs

    def _build_push_dests(self, push_sources):
        """
        When the destination paths have not been explicitly specified, build
        them automatically. By default we try to push to the same absolute path
        on the remote host, converted to POSIX format. However if a relative
        directory root has been configured we will build a remote destination
        based on that.
        """
        if self.cfg.push_relative_dir:
            self.logger.debug('local push dir = %s', self.cfg.push_relative_dir)

            # Set up the remote push dir.
            self._remote_push_dir = '/'.join(
                (self._remote_testplan_path, 'push_files'))
            self._mkdir_remote(self._remote_push_dir)
            self.setup_metadata.push_dir = self._remote_push_dir
            self.logger.debug('Created remote push dir %s',
                                       self._remote_push_dir)

            push_dsts = [self._to_relative_push_dest(path)
                         for path in push_sources]
        else:
            push_dsts = [pathutils.to_posix_path(path)
                         for path in push_sources]

        return push_dsts

    def _to_relative_push_dest(self, local_path):
        """
        :param local_path: Full local path in local OS format.
        :return: Remote file and directory paths in POSIX format.
        """
        relative_root = self.cfg.push_relative_dir
        if not pathutils.is_subdir(local_path, relative_root):
            raise RuntimeError('Cannot push path {path} - is not within the '
                               'specified local root {root}'
                               .format(path=local_path, root=relative_root))

        local_rel_path = os.path.relpath(local_path, relative_root)
        return '/'.join((self._remote_push_dir,
                                pathutils.to_posix_path(local_rel_path)))

    def _push_files_to_dst(self, push_files, push_dirs):
        """
        Push files and directories to the remote host. Both the source and
        destination paths should be specified.

        :param push_files: Files to push.
        :param push_dirs:  Directories to push.
        """
        for source, dest in itertools.chain(push_files, push_dirs):
            remote_dir = dest.rpartition('/')[0]
            self.logger.debug('Create remote dir: %s', remote_dir)
            self._mkdir_remote(remote_dir)

            self._transfer_data(
                source=source,
                target=dest,
                remote_target=True,
                exclude=self.cfg.push_exclude)

    def _copy_workspace(self):
        """Copy the local workspace to remote host."""
        self._workspace_paths.remote = '{}/{}'.format(
            self._remote_testplan_path,
            self._workspace_paths.local.split(os.sep)[-1])
        if self.cfg.remote_workspace:
            # User defined the remote workspace to be used.
            # Make a soft link instead of copying workspace.
            self._execute_cmd(self.cfg.ssh_cmd(
                self.cfg.index,
                ' '.join(self.cfg.link_cmd(
                    path=fix_home_prefix(self.cfg.remote_workspace),
                    link=self._workspace_paths.remote))),
                label='linking to remote workspace (1).')
        elif self._should_transfer_workspace is True:
            # Workspace should be copied to remote.
            self._transfer_data(
                source=self._workspace_paths.local,
                target=self._remote_testplan_path,
                remote_target=True,
                exclude=self.cfg.workspace_exclude)
            # Mark that workspace pushed is safe to delete. Not some NFS.
            self.setup_metadata.workspace_pushed = True
        else:
            # Make a soft link instead of copying workspace.
            self._execute_cmd(self.cfg.ssh_cmd(
                self.cfg.index,
                ' '.join(self.cfg.link_cmd(
                    path=self._workspace_paths.local,
                    link=self._workspace_paths.remote))),
                label='linking to remote workspace (2).')

    def _remote_copy_path(self, path):
        """
        Return a path on the remote host in the format user@host:path,
        suitable for use in a copy command such as `scp`.
        """
        return '{user}@{host}:{path}'.format(
            user=self._user, host=self.cfg.index, path=path)

    def _transfer_data(self,
                       source,
                       target,
                       remote_source=False,
                       remote_target=False,
                       **copy_args):
        if remote_source:
            source = self._remote_copy_path(source)
        if remote_target:
            target = self._remote_copy_path(target)
        self.logger.debug('Copying %(source)s to %(target)s', locals())
        cmd = self.cfg.copy_cmd(source, target, **copy_args)
        with open(os.devnull, 'w') as devnull:
            self._execute_cmd(cmd,
                              'transfer data [..{}]'.format(
                                  os.path.basename(target)),
                              stdout=devnull)

    @property
    def _remote_working_dir(self):
        """Choose a working directory to use on the remote host."""
        if not pathutils.is_subdir(self._working_dirs.local,
                                   self._workspace_paths.local):
            raise RuntimeError(
                'Current working dir is not within the workspace.\n'
                'Workspace = {ws}\n'
                'Working dir = {cwd}'
                .format(ws=self._workspace_paths.local,
                        cwd=self._working_dirs.local))

        # Current working directory is within the workspace - use the same
        # path relative to the remote workspace.
        return pathutils.to_posix_path(os.path.join(
            self._workspace_paths.remote,
            os.path.relpath(self._working_dirs.local,
                            self._workspace_paths.local)))

    def _prepare_remote(self):
        """Transfer local data to remote host."""
        self._child_paths.local = self._child_path()
        self._workspace_paths.local = fix_home_prefix(self.cfg.workspace)

        if self.cfg.copy_workspace_check:
            cmd = self.cfg.copy_workspace_check(
                self.cfg.ssh_cmd,
                self.cfg.index,
                self._workspace_paths.local)
            self._should_transfer_workspace = self._execute_cmd(
                cmd, label='copy workspace check', check=False) != 0

        self._define_remote_dirs()
        self._create_remote_dirs()
        self._copy_child_script()
        self._copy_dependencies_module()
        self._copy_workspace()

        self._working_dirs.local = pwd()
        self._working_dirs.remote = self._remote_working_dir
        self.logger.debug('Remote working path = %s',
                          self._working_dirs.remote)

        self._push_files()
        self.setup_metadata.setup_script = self.cfg.setup_script
        self.setup_metadata.env = self.cfg.env
        self.setup_metadata.workspace_paths = self._workspace_paths

    def _pull_files(self):
        """Push custom files to be available on remotes."""
        for entry in [itm.rstrip('/') for itm in self.cfg.pull]:
            # Prepare target path for possible windows usage.
            dirname = os.sep.join(os.path.dirname(entry).split('/'))
            try:
                makedirs(dirname)
            except Exception as exc:
                self.logger.error('Cound not create {} directory - {}'.format(
                    dirname, exc))
            else:
                self._transfer_data(
                    source=entry,
                    remote_source=True,
                    target=dirname,
                    exclude=self.cfg.pull_exclude)

    def _fetch_results(self):
        """Fetch back to local host the results generated remotely."""
        self.logger.debug('Fetch results stage - {}'.format(self.cfg.index))
        self._transfer_data(
            source=self._remote_testplan_runpath,
            remote_source=True,
            target=self.parent.runpath)

    def _add_testplan_import_path(self, cmd, flag=None):
        if self.cfg.testplan_path:
            if flag is not None:
                cmd.append(flag)
            cmd.append(self.cfg.testplan_path)
            return

        import testplan
        testplan_path = os.path.abspath(
            os.path.join(
                os.path.dirname(module_abspath(testplan)),
                '..'))
        # Import testplan from outside the local workspace
        if not testplan_path.startswith(self._workspace_paths.local):
            return
        common_prefix = os.path.commonprefix([testplan_path,
                                              self._workspace_paths.local])
        if flag is not None:
            cmd.append(flag)
        cmd.append('{}/{}'.format(
            self._workspace_paths.remote,
            '/'.join(os.path.relpath(
                testplan_path, common_prefix).split(os.sep))))

    def _add_testplan_deps_import_path(self, cmd, flag=None):
        if os.environ.get(testplan.TESTPLAN_DEPENDENCIES_PATH):
            if flag is not None:
                cmd.append(flag)
            cmd.append(os.environ[testplan.TESTPLAN_DEPENDENCIES_PATH])

    def _proc_cmd(self):
        """Command to start child process."""
        if platform.system() == 'Windows':
            if platform.python_version().startswith('3'):
                python_binary = os.environ['PYTHON3_REMOTE_BINARY']
            else:
                python_binary = os.environ['PYTHON2_REMOTE_BINARY']
        else:
            python_binary = sys.executable
        cmd = [python_binary, '-uB',
               self._child_paths.remote,
               '--index', str(self.cfg.index),
               '--address', self.transport.address,
               '--type', 'remote_worker',
               '--log-level', str(TESTPLAN_LOGGER.getEffectiveLevel()),
               '--wd', self._working_dirs.remote,
               '--runpath', self._remote_testplan_runpath,
               '--remote-pool-type', self.cfg.pool_type,
               '--remote-pool-size', str(self.cfg.workers)]
        self._add_testplan_import_path(cmd, flag='--testplan')
        if not self._should_transfer_workspace:
            self._add_testplan_deps_import_path(cmd, flag='--testplan-deps')
        return self.cfg.ssh_cmd(self.cfg.index, ' '.join(cmd))

    def starting(self):
        """Start a child remote worker."""
        self._prepare_remote()
        super(RemoteWorker, self).starting()

    def stopping(self):
        """Stop child process worker."""
        self._fetch_results()
        if self.cfg.pull:
            self._pull_files()
        super(RemoteWorker, self).stopping()

    def aborting(self):
        """Abort child process worker."""
        try:
            self._fetch_results()
        except Exception as exc:
            self.logger.error('Could not fetch results, {}'.format(exc))
        super(RemoteWorker, self).aborting()


class RemotePoolConfig(PoolConfig):
    """
    Configuration object for
    :py:class:`~testplan.runners.pools.remote.RemotePool` executor
    resource entity.

    :param hosts: Map of host(ip): number of their local workers.
      i.e {'hostname1': 2, '10.147.XX.XX': 4}
    :type hosts: ``dict`` of ``str``:``int``
    :param abort_signals: Signals to trigger abort logic. Default: INT, TERM.
    :type abort_signals: ``list`` of ``int``
    :param worker_type: Type of worker to be initialized.
    :type worker_type: :py:class:`~testplan.runners.pools.remote.RemoteWorker`
    :param pool_type: Local pool that will be initialized in remote workers.
      i.e ``thread``, ``process``.
    :type pool_type: ``str``
    :param host: Host that pool binds and listens for requests. Defaults to
      local hostname.
    :type host: ``str``
    :param port: Port that pool binds. Default: 0 (random)
    :type port: ``int``
    :param copy_cmd: Creates the remote copy command.
    :type copy_cmd: ``callable``
    :param link_cmd: Creates the solf link command.
    :type link_cmd: ``callable``
    :param ssh_cmd: Creates the ssh command.
    :type ssh_cmd: ``callable``
    :param workspace: Current project workspace to be transferred.
    :type workspace: ``str``
    :param workspace_exclude: Patterns to exclude files when pushing workspace.
    :type workspace_exclude: ``list`` of ``str``
    :param remote_workspace: Use a workspace that already exists in remote host.
    :type remote_workspace: ``str``
    :param copy_workspace_check: Check to indicate whether to copy workspace.
    :type copy_workspace_check: ``callable`` or ``NoneType``
    :param env: Environment variables to be propagated.
    :type env: ``dict``
    :param setup_script: Script to be executed on remote as very first thing.
    :type setup_script: ``list`` of ``str``
    :param push: Files and directories to push to the remote.
    :type push: ``list`` of ``str``
    :param push_exclude: Patterns to exclude files on push stage.
    :type push_exclude: ``list`` of ``str``
    :param delete_pushed: Deleted pushed files and workspace on remote at exit.
    :type delete_pushed: ``bool``
    :param pull: Files and directories to be pulled from the remote at the end.
    :type pull: ``list`` of ``str``
    :param pull_exclude: Patterns to exclude files on pull stage..
    :type pull_exclude: ``list`` of ``str``
    :param remote_mkdir: Command to make directories in remote worker.
    :type remote_mkdir: ``list`` of ``str``
    :param testplan_path: Path to import testplan from.
    :type testplan_path: ``str``
    :param worker_heartbeat: Worker heartbeat period.
    :type worker_heartbeat: ``int`` or ``float`` or ``NoneType``

    Also inherits all :py:class:`~testplan.runners.pools.base.PoolConfig`
    options.
    """

    @classmethod
    def get_options(cls):
        """
        Schema for options validation and assignment of default values.
        """
        hostname = socket.gethostbyname(socket.gethostname())
        return {
            'hosts': dict,
            ConfigOption('abort_signals', default=[signal.SIGINT,
                                                   signal.SIGTERM]): [int],
            ConfigOption('worker_type', default=RemoteWorker): object,
            ConfigOption('pool_type', default='thread'): str,
            ConfigOption('host', default=hostname): str,
            ConfigOption('port', default=0): int,
            ConfigOption('copy_cmd', default=copy_cmd):
                lambda x: callable(x),
            ConfigOption('link_cmd', default=link_cmd):
                lambda x: callable(x),
            ConfigOption('ssh_cmd', default=ssh_cmd):
                lambda x: callable(x),
            ConfigOption('workspace', default=pwd()): str,
            ConfigOption('workspace_exclude', default=[]): Or(list, None),
            ConfigOption('remote_workspace', default=None): Or(str, None),
            ConfigOption('copy_workspace_check',
                         default=remote_filepath_exists):
                Or(lambda x: callable(x), None),
            ConfigOption('env', default=None): Or(dict, None),
            ConfigOption('setup_script', default=None): Or(list, None),
            ConfigOption('push', default=[]): Or(list, None),
            ConfigOption('push_exclude', default=[]): Or(list, None),
            ConfigOption('push_relative_dir', default=None): Or(str, None),
            ConfigOption('delete_pushed', default=False): bool,
            ConfigOption('pull', default=[]): Or(list, None),
            ConfigOption('pull_exclude', default=[]): Or(list, None),
            ConfigOption('remote_mkdir', default=['/bin/mkdir', '-p']): list,
            ConfigOption('testplan_path', default=None): Or(str, None),
            ConfigOption('worker_heartbeat', default=30): Or(int, float, None)
        }


class RemotePool(Pool):
    """
    Pool task executor object that initializes remote workers and dispatches
    tasks.
    """

    CONFIG = RemotePoolConfig
    CONN_MANAGER = TCPConnectionManager

    def __init__(self, **options):
        super(RemotePool, self).__init__(**options)
        self._request_handlers[Message.MetadataPull] =\
            self._worker_setup_metadata

    @staticmethod
    def _worker_setup_metadata(worker, response):
        worker.respond(response.make(
            Message.Metadata, data=worker.setup_metadata))

    def _add_workers(self):
        """TODO."""
        for host, workers in self.cfg.hosts.items():
            worker = self.cfg.worker_type(
                index=host, workers=workers, pool_type=self.cfg.pool_type)
            self.logger.debug('Created {}'.format(worker))
            worker.parent = self
            worker.cfg.parent = self.cfg
            self._workers.add(worker, uid=host)

