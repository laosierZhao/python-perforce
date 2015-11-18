# -*- coding: utf-8 -*-

"""
perforce.models
~~~~~~~~~~~~~~~

This module implements the main data models used by perforce

:copyright: (c) 2015 by Brett Dixon
:license: MIT, see LICENSE for more details
"""

import subprocess
import datetime
import traceback
import os
import marshal
import logging
from collections import namedtuple

import path

from perforce import errors


LOGGER = logging.getLogger('Perforce')
FORMAT = """Change: {change}

Client: {client}

User:   {user}

Status: {status}

Description:
\t{description}

Files:
{files}
"""

NEW_FORMAT = """Change: new

Client: {client}

Status: new

Description:
\t{description}

"""


#: Error levels enum
ErrorLevel = namedtuple('ErrorLevel', 'EMPTY, INFO, WARN, FAILED, FATAL')(*range(5))
#: Connections status enum
ConnectionStatus = namedtuple('ConnectionStatus', 'OK, OFFLINE, NO_AUTH, INVALID_CLIENT')(*range(4))


class Connection(object):
    """This is the connection to perforce and does all of the communication with the perforce server"""
    def __init__(self, port=None, client=None, user=None, executable='p4', level=ErrorLevel.FAILED):
        self._executable = executable
        self._port = port or os.getenv('P4PORT')
        self._client = client or os.getenv('P4CLIENT')
        self._user = user or os.getenv('P4USER')

        # -- Make sure we can even proceed with anything
        if self._port is None:
            raise errors.ConnectionError('Perforce host could not be found, please set P4PORT or provide the hostname\
and port')
        if self._client is None:
            raise errors.ConnectionError('No client could be found, please set P4CLIENT or provide one')
        if self._user is None:
            raise errors.ConnectionError('No user could be found, please set P4USER or provide the user')

        self._level = level

    def __repr__(self):
        return '<Connection: {0}, {1}, {2}>'.format(self._port, self._client, self._user)

    @property
    def client(self):
        return self._client

    @property
    def user(self):
        return self._user

    @property
    def level(self):
        """The current exception level"""
        return self._level

    @level.setter
    def level(self, value):
        """Set the current exception level"""
        self._level = value

    @property
    def status(self):
        try:
            # -- Check client
            res = self.run('info')
            if res[0]['clientName'] == '*unknown*':
                return ConnectionStatus.INVALID_CLIENT
            # -- Trigger an auth error if not logged in
            self.run('user -o')
        except errors.CommandError as err:
            if 'password (P4PASSWD) invalid or unset' in err.args[0]:
                return ConnectionStatus.NO_AUTH
            if 'Connect to server failed' in err.args[0]:
                return ConnectionStatus.OFFLINE

        return ConnectionStatus.OK

    def run(self, cmd, stdin=None, marshal_output=True):
        """Runs a p4 command and returns a list of dictionary objects

        :param cmd: Command to run
        :type cmd: str
        :param stdin: Standard Input to send to the process
        :type stdin: str
        :param marshal_output: Whether or not to marshal the output from the command
        :type marshal_output: bool
        :raises: :class:`.error.CommandError`
        :returns: list, records of results
        """
        records = []
        command = [self._executable, "-u", self._user, "-p", self._port, "-c", self._client]
        if marshal_output:
            command.append('-G')
        command += cmd.split()

        startupinfo = None
        if os.name == 'nt':
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

        proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            startupinfo=startupinfo
        )

        if stdin:
            proc.stdin.write(stdin)
        proc.stdin.close()

        if marshal_output:
            try:
                while True:
                    record = marshal.load(proc.stdout)
                    if record.get('code', '') == 'error' and record['severity'] >= self._level:
                        raise errors.CommandError(record['data'], record, ' '.join(command))
                    records.append(record)
            except EOFError:
                pass

            stdout, stderr = proc.communicate()
        else:
            records, stderr = proc.communicate()

        if stderr:
            raise errors.CommandError(stderr, command)

        return records

    def ls(self, files, silent=True):
        """List files

        :param files: Perforce file spec
        :type files: str
        :param silent: Will not raise error for invalid files or files not under the client
        :type silent: bool
        :raises: :class:`.errors.RevisionError`
        :returns: list<:class:`.Revision`>
        """
        if not isinstance(files, (tuple, list)):
            files = [files]

        try:
            results = self.run('fstat {}'.format(' '.join(files)))
        except errors.CommandError as err:
            if silent:
                results = []
            elif "is not under client's root" in str(err):
                raise errors.RevisionError(err.args[0])
            else:
                raise

        return [Revision(r, self) for r in results if r.get('code') != 'error']

    def findChangelist(self, description=None):
        """Gets or creates a Changelist object with a description

        :param description: The description to set or lookup
        :type description: str
        :returns: :class:`.Changelist`
        """
        if description is None:
            change = Default(self)
        else:
            if isinstance(description, (int)):
                change = Changelist(self, description)
            else:
                pending = self.run('changes -s pending -c {} -u {}'.format(self._client, self._user))
                for cl in pending:
                    if cl['desc'].strip() == description.strip():
                        LOGGER.debug('Changelist found: {}'.format(cl['change']))
                        change = Changelist(self, int(cl['change']))
                        break
                else:
                    LOGGER.debug('No changelist found, creating one')
                    change = Changelist.create(self, description)
                    change.client = self._client
                    change.save()

        return change

    def add(self, filename, change=None):
        """Adds a new file to a changelist

        :param filename: File path to add
        :type filename: str
        :param change: Changelist to add the file to
        :type change: int
        :returns: :class:`.Revision`
        """
        try:
            if not self.canAdd(filename):
                return None

            if change is None:
                self.run('add %s' % filename)
            else:
                self.run('add -c %i %s' % (int(change), filename))

            data = self.run('fstat {}'.format(filename))[0]
        except errors.CommandError:
            raise errors.RevisionError('File is not under client path')

        rev = Revision(data, self)

        if isinstance(change, Changelist):
            change.append(rev)

        return rev

    def canAdd(self, filename):
        """Determines if a filename can be added to the depot under the current client

        :param filename: File path to add
        :type filename: str
        """
        try:
            result = self.run('add -n {}'.format(filename))[0]
        except errors.CommandError as err:
            return False

        if result.get('code') not in ('error', 'info'):
            return True

        LOGGER.warn('Unable to add {}: {}'.format(filename, result['data']))

        return False


class Changelist(object):
    """
    A Changelist is a collection of files that will be submitted as a single entry with a description and
    timestamp
    """
    def __init__(self, connection, changelist=None):
        super(Changelist, self).__init__()

        self._connection = connection
        self._files = []
        self._dirty = False
        self._reverted = False

        self._change = changelist
        self._description = ''
        self._client = ''
        self._time = datetime.datetime.now()
        self._status = 'pending'
        self._user = ''

        if self._change:
            data = self._connection.run('describe {0}'.format(changelist))[0]
            self._description = data['desc']
            self._client = data['client']
            self._time = datetime.datetime.fromtimestamp(int(data['time']))
            self._status = data['status']
            self._user = data['user']

            for k, v in data.iteritems():
                if k.startswith('depotFile'):
                    self.append(v)

    def __repr__(self):
        return '<Changelist {}>'.format(self._change)

    def __int__(self):
        return int(self._change)

    def __nonzero__(self):
        return True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, exc_traceback):
        if exc_type:
            LOGGER.debug(traceback.format_exc())
            raise errors.ChangelistError(exc_value)

        self.save()

    def __contains__(self, other):
        if not isinstance(other, Revision):
            raise TypeError('Value needs to be a Revision instance')

        names = [f.depotFile for f in self._files]

        return other.depotFile in names

    def __getitem__(self, name):
        return self._files[name]

    def __len__(self):
        return len(self._files)

    def __format__(self, *args, **kwargs):
        kwargs = {
            'change': self._change,
            'client': self._client,
            'user': self._user,
            'status': self._status,
            'description': self._description.replace('\n', '\n\t'),
            'files': '\n'.join(['\t{}'.format(f.depotFile) for f in self._files])
        }

        return FORMAT.format(**kwargs)

    def query(self):
        """Queries the depot to get the current status of the changelist"""
        self._files = []
        data = self._connection.run('describe {}'.format(self._change))[0]
        self._description = data['desc']
        self._client = data['client']
        self._time = datetime.datetime.fromtimestamp(int(data['time']))
        self._status = data['status']
        self._user = data['user']

        for k, v in data.iteritems():
            if k.startswith('depotFile'):
                self.append(v)

    def append(self, rev):
        """Adds a :py:class:Revision to this changelist and adds or checks it out if needed

        :param rev: Revision to add
        :type rev: :class:`.Revision`
        """
        if not isinstance(rev, Revision):
            results = self._connection.ls(rev)
            if not results:
                self._connection.add(rev, self)
                return

            rev = results[0]

        if not rev in self:
            if rev.isMapped:
                rev.edit(self)

            self._files.append(rev)
            rev.changelist = self

            self._dirty = True

    def remove(self, rev, permanent=False):
        """Removes a revision from this changelist

        :param rev: Revision to remove
        :type rev: :class:`.Revision`
        :param permanent: Whether or not we need to set the changelist to default
        :type permanent: bool
        """
        if not isinstance(rev, Revision):
            raise TypeError('argument needs to be an instance of Revision')

        if rev not in self:
            raise ValueError('{} not in changelist'.format(rev))

        self._files.remove(rev)
        if not permanent:
            rev.changelist = self._connection.default

    def revert(self):
        """Revert all files in this changelist

        :raises: :class:`.ChangelistError`
        """
        if self._reverted:
            raise errors.ChangelistError('This changelist has been reverted')

        change = self._change
        if self._change == 0:
            change = 'default'

        filelist = [str(f) for f in self]
        if filelist:
            self._connection.run('revert -c {0} {1}'.format(change, ' '.join(filelist)))

        self._files = []
        self._reverted = True

    def save(self):
        """Saves the state of the changelist"""
        self._connection.run('change -i', stdin=format(self), marshal_output=False)
        self._dirty = False

    def submit(self):
        """Submits a chagelist to the depot"""
        if self._dirty:
            self.save()

        self._connection.run('submit -c {}'.format(int(self)), marshal_output=False)

    def delete(self):
        """Reverts all files in this changelist then deletes the changelist from perforce"""
        try:
            self.revert()
        except errors.ChangelistError:
            pass

        self._connection.run('change -d {}'.format(self._change))

    @property
    def change(self):
        """Changelist number"""
        return self._change

    @property
    def client(self):
        """Perforce client this changelist is under"""
        return self._client

    @client.setter
    def client(self, client):
        self._client = client
        self._dirty = True

    @property
    def description(self):
        """Changelist description"""
        return self._description.strip()

    @description.setter
    def description(self, desc):
        self._description = desc.strip()
        self._dirty = True

    @property
    def isDirty(self):
        """Does this changelist have unsaved changes"""
        return self._dirty

    @property
    def time(self):
        """Creation time of this changelist"""
        return self._time

    @property
    def status(self):
        """Status of this changelist.  Pending, Submitted, etc."""
        return self._status

    @property
    def user(self):
        """User who created this changelist"""
        return self._user

    @staticmethod
    def create(connection, description='<Created by Python>'):
        """Creates a new changelist

        :param connection: Connection to use to create the changelist
        :type connection: :class:`.Connection`
        :param description: Description for new changelist
        :type description: str
        :returns: :class:`.Changelist`
        """
        description = description.replace('\n', '\n\t')
        form = NEW_FORMAT.format(client=connection.client, description=description)
        result = connection.run('change -i', form, marshal_output=False)

        return Changelist(connection, int(result.split()[1]))


class Default(Changelist):
    def __init__(self, connection):
        super(Default, self).__init__(connection, None)

        data = self._connection.run('opened -c default')

        for f in data:
            self._files.append(Revision(f, self._connection))

        data = self._connection.run('change -o')[0]
        self._change = 0
        self._description = data['Description']
        self._client = connection.client
        self._time = None
        self._status = 'new'
        self._user = connection.user

    def save(self):
        """Saves the state of the changelist"""
        files = ','.join([f.depotFile for f in self._files])
        self._connection.run('reopen -c default {}'.format(files))
        self._dirty = False


class Revision(object):
    """A Revision represents a file on perforce at a given point in it's history"""
    def __init__(self, data, connection):
        self._p4dict = data
        self._connection = connection
        self._head = HeadRevision(self._p4dict)
        self._changelist = None
        self._filename = None

    def __len__(self):
        if 'fileSize' not in self._p4dict:
            self._p4dict = self._connection.run('fstat -m 1 -Ol %s' % self.depotFile)[0]

        return int(self._p4dict['fileSize'])

    def __str__(self):
        return self.depotFile

    def __repr__(self):
        return '<%s: %s#%s>' % (self.__class__.__name__, self.depotFile, self.revision)

    def __int__(self):
        return self.revision

    def query(self):
        """Runs an fstat for this file and repopulates the data"""

        self._p4dict = self._connection.run('fstat -m 1 %s' % self._p4dict['depotFile'])[0]
        self._head = HeadRevision(self._p4dict)

        self._filename = self.depotFile

    def edit(self, changelist=0):
        """Checks out the file

        :param changelist: Optional changelist to checkout the file into
        :type changelist: :class:`.Changelist`
        """
        command = 'reopen' if self.action in ('add', 'edit') else 'edit'
        if int(changelist):
            self._connection.run('{0} -c {1} {2}'.format(command, int(changelist), self.depotFile))
        else:
            self._connection.run('{0} {1}'.format(command, self.depotFile))

        self.query()

    def lock(self, lock=True, changelist=0):
        """Locks or unlocks the file

        :param lock: Lock or unlock the file
        :type lock: bool
        :param changelist: Optional changelist to checkout the file into
        :type changelist: :class:`.Changelist`
        """

        cmd = 'lock' if lock else 'unlock'
        if changelist:
            self._connection.run('%s -c %i %s' % (cmd, changelist, self.depotFile))
        else:
            self._connection.run('%s %s' % (cmd, self.depotFile))

        self.query()

    def sync(self, force=False, safe=True, revision=0):
        """Syncs the file at the current revision

        :param force: Force the file to sync
        :type force: bool
        :param safe: Don't sync files that were changed outside perforce
        :type safe: bool
        :param revision: Sync to a specific revision
        :type revision: int
        """
        args = ''
        if force:
            args += ' -f'

        if safe:
            args += ' -s'

        args += ' %s' % self.depotFile
        if revision:
            args += '#{}'.format(revision)
        self._connection.run('sync %s' % args)

        self.query()

    def revert(self, unchanged=False):
        """Reverts any file changes

        :param unchanged: Only revert if the file is unchanged
        :type unchanged: bool
        """
        args = ''
        if unchanged:
            args += ' -a'

        wasadd = self.action == 'add'

        args += ' %s' % self.depotFile
        self._connection.run('revert %s' % args)

        if not wasadd:
            self.query()

        if self._changelist:
            self._changelist.remove(self, permanent=True)

    def shelve(self, changelist=None):
        """Shelves the file if it is in a changelist

        :param changelist: Changelist to add the move to
        :type changelist: :class:`.Changelist`
        """
        if changelist is None and self.changelist.description == 'default':
            raise errors.ShelveError('Unabled to shelve files in the default changelist')

        cmd = 'shelve '
        if changelist:
            cmd += '-c {0} '.format(int(changelist))

        cmd += self.depotFile

        self._connection.run(cmd)

        self.query()

    def move(self, dest, changelist=0, force=False):
        """Renames/moves the file to dest

        :param dest: Destination to move the file to
        :type dest: str
        :param changelist: Changelist to add the move to
        :type changelist: :class:`.Changelist`
        :param force: Force the move to an existing file
        :type force: bool
        """
        args = ''
        if force:
            args += '-f'

        if changelist:
            args += ' -c {} '.format(int(changelist))

        if not self.isEdit:
            self.edit(changelist)

        args += '{0} {1}'.format(self.depotFile, dest)
        LOGGER.info('move {}'.format(args))
        self._connection.run('move {}'.format(args))

        self.query()

    def delete(self, changelist=0):
        """Marks the file for delete

        :param changelist: Changelist to add the move to
        :type changelist: :class:`.Changelist`
        """
        args = ''

        if changelist:
            args += ' -c {}'.format(int(changelist))

        args += ' %s' % self.depotFile
        self._connection.run('delete %s' % args)

        self.query()

    @property
    def hash(self):
        """The hash value of the current revision"""
        if 'digest' not in self._p4dict:
            self._p4dict = self._connection.run('fstat -m 1 -Ol %s' % self.depotFile)[0]

        return self._p4dict['digest']

    @property
    def clientFile(self):
        """The local path to the revision"""
        return path.path(self._p4dict['clientFile'])

    @property
    def depotFile(self):
        """The depot path to the revision"""
        return path.path(self._p4dict['depotFile'])

    @property
    def movedFile(self):
        """Was this file moved"""
        return self._p4dict['movedFile']

    @property
    def isMapped(self):
        """Is the file mapped to the current workspace"""
        return 'isMapped' in self._p4dict

    @property
    def isShelved(self):
        """Is the file shelved"""
        return 'shelved' in self._p4dict

    @property
    def revision(self):
        """Revision number"""
        rev = self._p4dict.get('haveRev', -1)
        if rev == 'none':
            rev = 0
        return int(rev)

    @property
    def description(self):
        return self._p4dict.get('desc')

    @property
    def action(self):
        """The current action: add, edit, etc."""
        return self._p4dict.get('action')

    @property
    def changelist(self):
        """Which :class:`.Changelist` is this revision in"""
        if self._changelist:
            return self._changelist

        if self._p4dict['change'] == 'default':
            return self._connection.default
        else:
            return Changelist(self._connection, int(self._p4dict['change']))

    @changelist.setter
    def changelist(self, value):
        if not isinstance(value, Changelist):
            raise TypeError('argument needs to be an instance of Changelist')

        self._changelist = value

    @property
    def type(self):
        """Best guess at file type. text or binary"""
        if self.action == 'edit':
            return self._p4dict['type']

        return None

    @property
    def isResolved(self):
        """Is the revision resolved"""
        return self.unresolved == 0

    @property
    def resolved(self):
        """Is the revision resolved"""
        return int(self._p4dict.get('resolved', 0))

    @property
    def unresolved(self):
        """Is the revision unresolved"""
        return int(self._p4dict.get('unresolved', 0))

    @property
    def openedBy(self):
        """Who has this file open for edit"""
        return self._p4dict.get('otherOpen', [])

    @property
    def lockedBy(self):
        """Who has this file locked"""
        return self._p4dict.get('otherLock', [])

    @property
    def isLocked(self):
        """Is the file locked by anyone excluding the current user"""
        return 'ourLock' in self._p4dict or 'otherLock' in self._p4dict

    @property
    def head(self):
        """The :class:`.HeadRevision` of this file"""
        return self._head

    @property
    def isSynced(self):
        """Is the local file the latest revision"""
        return self.revision == self.head.revision

    @property
    def isEdit(self):
        """Is the file open for edit"""
        return self.action == 'edit'


class HeadRevision(object):
    """The HeadRevision represents the latest version on the Perforce server"""
    def __init__(self, filedict):
        self._p4dict = filedict

    @property
    def action(self):
        return self._p4dict['headAction']

    @property
    def change(self):
        return int(self._p4dict['headChange']) if self._p4dict['headChange'] else 0

    @property
    def revision(self):
        return int(self._p4dict['headRev'])

    @property
    def type(self):
        return self._p4dict['headType']

    @property
    def time(self):
        return datetime.datetime.fromtimestamp(int(self._p4dict['headTime']))

    @property
    def modifiedTime(self):
        return datetime.datetime.fromtimestamp(int(self._p4dict['headModTime']))
